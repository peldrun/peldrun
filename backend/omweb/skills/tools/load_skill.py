# backend/omweb/skills/tools/load_skill.py
"""
The ``load_skill`` tool for the Progressive Disclosure flow.

Called by the model when it decides (based on the injected catalog)
that it needs a skill's full instructions. Returns the skill body
wrapped in BEGIN/END delimiters so downstream prompts and logs can
unambiguously identify where the skill content starts and ends.

For router skills, the response includes ``has_sub_skills: True`` and
the list of available sub-skill IDs. This tells the model the next
step: call ``load_sub_skill(parent_id, sub_id)`` to fetch a specific
sub-skill body instead of trying to use the router as-is.

Design notes:
    * Never raises. All failures are encoded as ``{"ok": False, ...}``
      dicts with a stable ``code``. The model sees the error, the
      engine logs it, and the run continues — a single broken skill
      must not abort the chat.
    * Reads the body via ``load_instructions`` (the same path the
      legacy full-injection mode uses), so ``limits.max_instructions_bytes``
      enforcement and empty-body checks are inherited automatically.
    * The response includes ``version`` so the model can detect drift
      (e.g. if it saw version 1.0.0 in the catalog but gets 1.1.0
      here, that is a signal the skill was upgraded mid-session —
      rare, but worth surfacing).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from omweb.skills.loader import load_instructions
from omweb.skills.registry import skill_registry


logger = logging.getLogger("omweb.skills.tools.load_skill")


# OpenAI function-calling schema. The description is deliberately
# directive: it tells the model EXACTLY when to call this tool and
# what the return contains. Vague descriptions lead to the model
# ignoring the tool and answering from memory instead.
TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "Load the full instructions (SKILL.md body) of a skill. "
            "Call this when the user's request matches a skill from the "
            "catalog. Returns the skill body wrapped in BEGIN/END "
            "delimiters. If the response includes 'has_sub_skills: true', "
            "the loaded skill is a router — read its body and then call "
            "'load_sub_skill' with the parent id and the chosen sub-skill "
            "id to fetch the actual instructions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": (
                        "The skill's canonical id from the catalog "
                        "(e.g. 'code_review')."
                    ),
                },
            },
            "required": ["skill_id"],
            "additionalProperties": False,
        },
    },
}


def execute(skill_id: str) -> Dict[str, Any]:
    """
    Execute the ``load_skill`` tool.

    Args:
        skill_id: The skill's canonical ID (or alias — the registry
                  resolves both forms).

    Returns:
        On success::

            {
                "ok": True,
                "skill_id": "<canonical id>",
                "version": "<version>",
                "has_sub_skills": bool,
                "sub_skills": ["<sub_id>", ...],   # empty when not a router
                "content": "--- BEGIN SKILL: <id>@<v> ---\\n...\\n--- END ...",
            }

        On failure::

            {
                "ok": False,
                "error": "<human-readable message>",
                "code": "<stable machine code>",
            }

        Codes used here:
            * ``SKILL_NOT_FOUND``    — ID does not resolve.
            * ``SKILL_DISABLED``     — Skill exists but is not active.
            * ``SKILL_LOAD_FAILED``  — Body load failed (empty, oversized,
                                       or security violation).
    """
    if not isinstance(skill_id, str) or not skill_id.strip():
        return {
            "ok": False,
            "error": "skill_id must be a non-empty string.",
            "code": "INVALID_ARGUMENT",
        }

    record = skill_registry.get_skill(skill_id)
    if record is None:
        logger.debug("load_skill: unknown skill id=%r", skill_id)
        return {
            "ok": False,
            "error": f"Skill {skill_id!r} not found.",
            "code": "SKILL_NOT_FOUND",
        }

    if not record.enabled:
        logger.info(
            "load_skill: skill %r is disabled (status=%r).",
            record.id,
            record.status,
        )
        return {
            "ok": False,
            "error": f"Skill {record.id!r} is disabled.",
            "code": "SKILL_DISABLED",
        }

    try:
        body = load_instructions(record)
    except Exception as exc:  # noqa: BLE001 — must never raise
        logger.warning(
            "load_skill: failed to load body for %r: %s",
            record.id,
            exc,
        )
        return {
            "ok": False,
            "error": f"Failed to load skill {record.id!r}: {exc}",
            "code": "SKILL_LOAD_FAILED",
        }

    header = f"--- BEGIN SKILL: {record.id}@{record.version} ---"
    footer = f"--- END SKILL: {record.id}@{record.version} ---"
    content = f"{header}\n{body}\n{footer}"

    sub_ids = [str(s.get("id")) for s in record.sub_skills if s.get("id")]

    logger.info(
        "load_skill: id=%r version=%s chars=%d sub_skills=%d",
        record.id,
        record.version,
        len(content),
        len(sub_ids),
    )

    return {
        "ok": True,
        "skill_id": record.id,
        "version": record.version,
        "has_sub_skills": bool(record.sub_skills),
        "sub_skills": sub_ids,
        "content": content,
    }


__all__ = ["TOOL_SCHEMA", "execute"]
