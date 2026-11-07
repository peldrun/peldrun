# backend/omweb/skills/tools/load_reference.py
"""
The ``load_reference`` tool for on-demand reference file access.

Called by the model when a loaded skill's body mentions a reference
file (e.g. "see references/api/auth.md for details") and the model
needs that content to answer correctly. Fetches a single file from the
skill's declared ``execution.references_dir``.

Design:
    * Thin wrapper over ``loader.load_reference``. That loader already
      enforces: relative-path validation, extension whitelist, size
      cap, symlink rejection, containment check, and UTF-8 read.
    * This module adds the tool-shaped contract: argument validation,
      registry lookup, and code-based error mapping for the model.
    * Wrapping is intentional. If we let the model call
      ``loader.load_reference`` directly, we would leak
      ``SkillLoadError``/``SkillSecurityError`` tracebacks into the
      tool result. The tool surface must NEVER raise.

Error code mapping (from loader exceptions → stable codes):
    * SkillLoadError      → ``REFERENCE_LOAD_FAILED`` (missing file,
                            no references_dir, oversized, empty).
    * SkillSecurityError  → ``REFERENCE_SECURITY_VIOLATION`` (traversal,
                            bad extension, symlink, containment).
    * SkillNotFoundError  → ``SKILL_NOT_FOUND``.
    * Unknown exceptions  → ``REFERENCE_LOAD_FAILED`` (defensive).

Security model:
    All containment is enforced by ``loader.load_reference``. This
    module does NOT re-implement path checks — the loader's checks are
    the single source of truth for reference access, and duplicating
    them here would risk drift.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from omweb.skills.errors import (
    SkillLoadError,
    SkillSecurityError,
)
from omweb.skills.loader import load_reference as _load_reference
from omweb.skills.registry import skill_registry


logger = logging.getLogger("omweb.skills.tools.load_reference")


# OpenAI function-calling schema.
TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "load_reference",
        "description": (
            "Load a reference file from a skill's references directory. "
            "Use this when a loaded skill body points to a specific "
            "reference file (e.g. 'references/api/auth.md') and you "
            "need its content. Returns the file text wrapped in "
            "BEGIN/END delimiters. Paths are relative to the skill's "
            "references directory — do not include the directory name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": (
                        "The skill whose references directory contains "
                        "the file (e.g. 'code_review')."
                    ),
                },
                "rel_path": {
                    "type": "string",
                    "description": (
                        "Path of the reference file RELATIVE to the "
                        "skill's references directory "
                        "(e.g. 'api/auth.md' or 'style_guide.md'). "
                        "Must not be absolute and must not contain '..'."
                    ),
                },
            },
            "required": ["skill_id", "rel_path"],
            "additionalProperties": False,
        },
    },
}


def execute(skill_id: str, rel_path: str) -> Dict[str, Any]:
    """
    Execute the ``load_reference`` tool.

    Args:
        skill_id: The skill whose references directory holds the file.
                  Aliases are resolved by the registry.
        rel_path: Relative path inside the references directory. The
                  path is validated by ``loader.load_reference`` (no
                  traversal, whitelisted extension, size-capped).

    Returns:
        On success::

            {
                "ok": True,
                "skill_id": "<canonical id>",
                "version": "<skill version>",
                "rel_path": "<normalized relative path>",
                "content": "--- BEGIN REFERENCE: <id>:<path> ---\\n...\\n--- END ...",
            }

        On failure, one of the following stable codes:

            * ``INVALID_ARGUMENT``              — empty/non-string input.
            * ``SKILL_NOT_FOUND``               — skill does not resolve.
            * ``SKILL_DISABLED``                — skill is not active.
            * ``REFERENCE_LOAD_FAILED``         — file missing, empty,
                                                  oversized, or skill
                                                  has no references_dir.
            * ``REFERENCE_SECURITY_VIOLATION``  — traversal, absolute
                                                  path, bad extension,
                                                  symlink, or containment
                                                  escape.
            * ``REFERENCE_LOAD_FAILED``         — unexpected exception
                                                  (defensive).
    """
    # ── 1. Argument validation ────────────────────────────────────
    if not isinstance(skill_id, str) or not skill_id.strip():
        return {
            "ok": False,
            "error": "skill_id must be a non-empty string.",
            "code": "INVALID_ARGUMENT",
        }
    if not isinstance(rel_path, str) or not rel_path.strip():
        return {
            "ok": False,
            "error": "rel_path must be a non-empty string.",
            "code": "INVALID_ARGUMENT",
        }

    # ── 2. Resolve skill ──────────────────────────────────────────
    record = skill_registry.get_skill(skill_id)
    if record is None:
        return {
            "ok": False,
            "error": f"Skill {skill_id!r} not found.",
            "code": "SKILL_NOT_FOUND",
        }

    if not record.enabled:
        return {
            "ok": False,
            "error": f"Skill {record.id!r} is disabled.",
            "code": "SKILL_DISABLED",
        }

    # ── 3. Delegate to loader (single source of truth for path
    #      validation and containment) ─────────────────────────────
    try:
        text = _load_reference(record, rel_path)
    except SkillSecurityError as exc:
        logger.warning(
            "load_reference: security violation for %r/%r: %s",
            record.id,
            rel_path,
            exc,
        )
        return {
            "ok": False,
            "error": f"Reference path rejected: {exc}",
            "code": "REFERENCE_SECURITY_VIOLATION",
        }
    except SkillLoadError as exc:
        logger.info(
            "load_reference: failed to load %r/%r: %s",
            record.id,
            rel_path,
            exc,
        )
        return {
            "ok": False,
            "error": f"Failed to load reference {rel_path!r}: {exc}",
            "code": "REFERENCE_LOAD_FAILED",
        }
    except Exception as exc:  # noqa: BLE001 — must never raise
        logger.exception(
            "load_reference: unexpected error for %r/%r: %s",
            record.id,
            rel_path,
            exc,
        )
        return {
            "ok": False,
            "error": f"Unexpected error loading reference: {exc}",
            "code": "REFERENCE_LOAD_FAILED",
        }

    # ── 4. Wrap and return ────────────────────────────────────────
    header = (
        f"--- BEGIN REFERENCE: {record.id}:{rel_path} ---"
    )
    footer = (
        f"--- END REFERENCE: {record.id}:{rel_path} ---"
    )
    content = f"{header}\n{text.rstrip()}\n{footer}"

    logger.info(
        "load_reference: skill=%r path=%r chars=%d",
        record.id,
        rel_path,
        len(content),
    )

    return {
        "ok": True,
        "skill_id": record.id,
        "version": record.version,
        "rel_path": rel_path,
        "content": content,
    }


__all__ = ["TOOL_SCHEMA", "execute"]
