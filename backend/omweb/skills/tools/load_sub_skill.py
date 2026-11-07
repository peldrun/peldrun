# backend/omweb/skills/tools/load_sub_skill.py
"""
The ``load_sub_skill`` tool for the Progressive Disclosure router flow.

Called by the model after it has loaded a router skill (via
``load_skill``) and decided which sub-skill to activate. Returns that
sub-skill's SKILL.md body wrapped in BEGIN/END delimiters.

Two-step flow enforced by the tool descriptions:

    1. ``load_skill("debugging")``        → returns router body + sub-skill list.
    2. ``load_sub_skill("debugging", "root_cause_tracing")`` → returns body.

Why the two arguments?
    Sub-skill IDs are only unique WITHIN a parent skill. Two different
    parent skills can both declare a sub-skill called ``verification``.
    The parent_id disambiguates and matches the router's mental model
    ("I'm inside debugging, I want its verification sub-skill").

Security model:
    * ``instructions_file`` is validated at manifest-load time to
      match ``^sub_skills/...\\.md$`` and to contain no ``..`` segments.
    * This executor re-validates containment via
      ``candidate.resolve().relative_to(package_dir.resolve())`` before
      reading. That is defense-in-depth: it catches any scenario where
      ``record.sub_skills`` was mutated in memory without going through
      the manifest validator (tests, migrations, future code paths).
    * No symlink traversal: ``Path.resolve()`` follows symlinks before
      the containment check, so a symlink pointing outside the package
      is rejected as a containment violation.
    * A file-size cap (matching ``load_instructions``) is applied
      before reading so a corrupted ``instructions_file`` cannot
      exhaust memory.

Design notes:
    * Never raises. All failures are ``{"ok": False, "code": ...}``.
    * Missing sub_skill returns the list of available IDs in the error
      payload so the model can self-correct without a second tool call.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from omweb.skills.manifest import (
    DEFAULT_MAX_INSTRUCTIONS_BYTES,
    MAX_INSTRUCTIONS_BYTES,
)
from omweb.skills.registry import SkillRecord, skill_registry


logger = logging.getLogger("omweb.skills.tools.load_sub_skill")


# OpenAI function-calling schema.
TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "load_sub_skill",
        "description": (
            "Load the full instructions of a sub-skill within a router "
            "skill. Call this AFTER load_skill(parent_id) has revealed "
            "the available sub-skills. Do NOT call this on a skill that "
            "is not a router. Returns the sub-skill body wrapped in "
            "BEGIN/END delimiters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "parent_id": {
                    "type": "string",
                    "description": (
                        "The parent router skill's canonical id "
                        "(e.g. 'debugging')."
                    ),
                },
                "sub_skill_id": {
                    "type": "string",
                    "description": (
                        "The sub-skill's id as shown in the parent's "
                        "router body (e.g. 'root_cause_tracing')."
                    ),
                },
            },
            "required": ["parent_id", "sub_skill_id"],
            "additionalProperties": False,
        },
    },
}


def _find_sub(
    record: SkillRecord, sub_skill_id: str
) -> Optional[Dict[str, Any]]:
    """Return the sub-skill dict from ``record`` or ``None``."""
    for sub in record.sub_skills:
        if sub.get("id") == sub_skill_id:
            return sub
    return None


def _containment_ok(candidate: Path, root: Path) -> bool:
    """
    Return True when ``candidate`` resolves inside ``root``.

    Uses ``Path.resolve()`` on both sides so symlinks that escape
    ``root`` are treated as NOT contained. Returns False on any
    filesystem error (resolution failure, missing parents, etc.) —
    fail-closed.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def execute(parent_id: str, sub_skill_id: str) -> Dict[str, Any]:
    """
    Execute the ``load_sub_skill`` tool.

    Args:
        parent_id:    The router skill's canonical ID (aliases resolved
                      by the registry).
        sub_skill_id: The sub-skill's ID as declared in the parent's
                      manifest.

    Returns:
        On success::

            {
                "ok": True,
                "parent_id": "<canonical id>",
                "sub_skill_id": "<sub id>",
                "content": "--- BEGIN SUB-SKILL: <p>/<s> ---\\n...\\n--- END ...",
            }

        On failure, one of the following stable codes:

            * ``INVALID_ARGUMENT``           — empty/non-string input.
            * ``SKILL_NOT_FOUND``            — parent does not resolve.
            * ``SKILL_DISABLED``             — parent is not active.
            * ``NOT_A_ROUTER``               — parent has no sub-skills.
            * ``SUB_SKILL_NOT_FOUND``        — sub ID not declared
                                               (``available`` list included).
            * ``SKILL_SECURITY_VIOLATION``   — path escapes package
                                               (defense-in-depth).
            * ``SUB_SKILL_FILE_MISSING``     — declared file not on disk.
            * ``SUB_SKILL_LOAD_FAILED``      — read failed or file
                                               exceeded size cap.
    """
    # ── 1. Argument validation ────────────────────────────────────
    if not isinstance(parent_id, str) or not parent_id.strip():
        return {
            "ok": False,
            "error": "parent_id must be a non-empty string.",
            "code": "INVALID_ARGUMENT",
        }
    if not isinstance(sub_skill_id, str) or not sub_skill_id.strip():
        return {
            "ok": False,
            "error": "sub_skill_id must be a non-empty string.",
            "code": "INVALID_ARGUMENT",
        }

    # ── 2. Resolve parent ─────────────────────────────────────────
    record = skill_registry.get_skill(parent_id)
    if record is None:
        return {
            "ok": False,
            "error": f"Parent skill {parent_id!r} not found.",
            "code": "SKILL_NOT_FOUND",
        }

    if not record.enabled:
        return {
            "ok": False,
            "error": f"Parent skill {record.id!r} is disabled.",
            "code": "SKILL_DISABLED",
        }

    if not record.sub_skills:
        return {
            "ok": False,
            "error": (
                f"Skill {record.id!r} is not a router "
                f"(it declares no sub-skills)."
            ),
            "code": "NOT_A_ROUTER",
        }

    # ── 3. Locate the sub-skill declaration ───────────────────────
    sub = _find_sub(record, sub_skill_id)
    if sub is None:
        available = [
            str(s.get("id"))
            for s in record.sub_skills
            if s.get("id")
        ]
        return {
            "ok": False,
            "error": (
                f"Sub-skill {sub_skill_id!r} not found in "
                f"{record.id!r}."
            ),
            "code": "SUB_SKILL_NOT_FOUND",
            "available": available,
        }

    rel = sub.get("instructions_file")
    if not isinstance(rel, str) or not rel:
        # Should never happen post-validation, but defend anyway.
        return {
            "ok": False,
            "error": (
                f"Sub-skill {sub_skill_id!r} has no "
                f"instructions_file declared."
            ),
            "code": "SUB_SKILL_FILE_MISSING",
        }

    # ── 4. Containment check (defense-in-depth) ───────────────────
    candidate = record.package_dir / rel
    if not _containment_ok(candidate, record.package_dir):
        logger.error(
            "load_sub_skill: containment violation for %r/%r "
            "(rel=%r, package_dir=%s).",
            record.id,
            sub_skill_id,
            rel,
            record.package_dir,
        )
        return {
            "ok": False,
            "error": (
                f"Sub-skill path {rel!r} escapes the parent package."
            ),
            "code": "SKILL_SECURITY_VIOLATION",
        }

    if not candidate.is_file():
        return {
            "ok": False,
            "error": f"Sub-skill file missing: {rel!r}.",
            "code": "SUB_SKILL_FILE_MISSING",
        }

    # ── 5. Read with size cap ─────────────────────────────────────
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        return {
            "ok": False,
            "error": f"Failed to stat sub-skill file: {exc}",
            "code": "SUB_SKILL_LOAD_FAILED",
        }

    # Effective cap: min(declared, hard). Matches load_instructions.
    limits_block = record.manifest.get("limits")
    declared_cap = (
        limits_block.get("max_instructions_bytes")
        if isinstance(limits_block, dict)
        else None
    )
    max_bytes = DEFAULT_MAX_INSTRUCTIONS_BYTES
    if isinstance(declared_cap, int) and declared_cap > 0:
        max_bytes = min(declared_cap, MAX_INSTRUCTIONS_BYTES)

    if size > max_bytes:
        return {
            "ok": False,
            "error": (
                f"Sub-skill file {rel!r} exceeds {max_bytes} bytes "
                f"({size} actual)."
            ),
            "code": "SUB_SKILL_LOAD_FAILED",
        }

    try:
        body = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "error": f"Failed to read sub-skill file: {exc}",
            "code": "SUB_SKILL_LOAD_FAILED",
        }

    if not body.strip():
        return {
            "ok": False,
            "error": f"Sub-skill file {rel!r} is empty.",
            "code": "SUB_SKILL_LOAD_FAILED",
        }

    # ── 6. Wrap and return ────────────────────────────────────────
    header = f"--- BEGIN SUB-SKILL: {record.id}/{sub_skill_id} ---"
    footer = f"--- END SUB-SKILL: {record.id}/{sub_skill_id} ---"
    content = f"{header}\n{body.strip()}\n{footer}"

    logger.info(
        "load_sub_skill: parent=%r sub=%r chars=%d",
        record.id,
        sub_skill_id,
        len(content),
    )

    return {
        "ok": True,
        "parent_id": record.id,
        "sub_skill_id": sub_skill_id,
        "content": content,
    }


__all__ = ["TOOL_SCHEMA", "execute"]
