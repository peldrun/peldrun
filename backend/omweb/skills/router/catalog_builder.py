# backend/omweb/skills/router/catalog_builder.py
"""
Render a list of skills as a compact "catalog" block for the system prompt.

Purpose:
    In Progressive Disclosure mode, the engine injects a lightweight
    catalog into the system prompt instead of full skill bodies. This
    module is the single formatter for that catalog. Its output is
    consumed directly by ``assignments.resolve_skill_injection_catalog``
    and ultimately by the LLM.

Format (stable, intentionally easy for the model to parse):

    You have access to the following skills. ...
    AVAILABLE SKILLS:
    - id='debugging' | name='Debugging Toolkit' | <one-line description>
      sub-skills:
        * sub_skill='root_cause_tracing': <one-line description>
        * sub_skill='defense_in_depth': <one-line description>
    - id='code_review' | name='Code Review' | <one-line description>

Design decisions:
    * Single-line descriptions. Newlines inside descriptions are
      collapsed to spaces so one skill's text can never masquerade as
      another skill's line. Combined with the strict ``additionalProperties:
      false`` on the manifest schema, this makes prompt-injection via
      description fields effectively impossible.
    * Hard character budget (``max_total_chars``). The function is
      greedy in the order the caller provides — the caller (BM25
      ranker) already guarantees relevance ordering, so we do NOT
      re-sort. Once the budget is exceeded, remaining skills are
      dropped with a single "... N more omitted" line so the model
      knows the catalog is not exhaustive.
    * Sub-skill descriptions are truncated more aggressively than
      parent descriptions. A skill with many sub-skills could
      otherwise consume the entire budget in a single entry.
    * No token counting here. The caller uses ``token_estimator`` if
      it needs a token-level view. This module stays deterministic
      and dependency-free so it can be unit-tested without a tokenizer.
"""

from __future__ import annotations

import logging
from typing import Sequence

from omweb.skills.registry import SkillRecord


logger = logging.getLogger("omweb.skills.router.catalog")


# Fixed preamble. Prepended to every catalog block. The instructions
# here are critical to the router pattern: they tell the model the
# precise tool name to call and the two-step load_skill → load_sub_skill
# flow when the skill is a router.
CATALOG_HEADER = (
    "You have access to the following skills. "
    "Each entry shows the skill's name, description, and (if applicable) its sub-skills. "
    "To use a skill, call the `load_skill` tool with its id. "
    "If a skill is a router (has sub-skills), read its body then call "
    "`load_sub_skill` with the parent id and the chosen sub-skill id.\n"
    "Only load skills that are relevant to the user's request.\n\n"
    "AVAILABLE SKILLS:\n"
)


# Defaults for the two truncation levels. Parent descriptions get more
# room than sub-skill descriptions because the parent is what the model
# sees on the "first pass" of decision-making.
_DEFAULT_MAX_DESC_CHARS = 200
_DEFAULT_MAX_SUB_DESC_CHARS = 150

# Default total budget. At ~4 chars/token this is roughly 1000 tokens —
# a deliberate ceiling that leaves room for the actual skill body plus
# the base system prompt.
_DEFAULT_MAX_TOTAL_CHARS = 4000


def _truncate(text: str, max_chars: int) -> str:
    """
    Collapse newlines and truncate to ``max_chars`` with an ellipsis.

    Guarantees a single-line result suitable for embedding in a
    ``- id=... | name=... | <desc>`` entry. Trailing whitespace is
    stripped after truncation so a mid-word cut does not leave a space
    before the ellipsis.
    """
    cleaned = (text or "").strip().replace("\n", " ").replace("\r", " ")
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def _build_one(
    skill: SkillRecord,
    *,
    max_desc_chars: int = _DEFAULT_MAX_DESC_CHARS,
    max_sub_desc_chars: int = _DEFAULT_MAX_SUB_DESC_CHARS,
) -> str:
    """
    Build one catalog entry (parent line + optional sub-skills block).

    The output format is:

        - id='<id>' | name='<name>' | <description>
          sub-skills:
            * sub_skill='<id>': <description>
            * ...

    The two-space / four-space indent is intentional and mirrors the
    plan's expected output. Do NOT change it without updating the
    README examples and the model's prompt-tuned behavior.
    """
    desc = _truncate(skill.description or "", max_desc_chars)
    line = f"- id={skill.id!r} | name={skill.name!r} | {desc}"

    if skill.sub_skills:
        sub_lines = []
        for sub in skill.sub_skills:
            sub_id = str(sub.get("id") or "?")
            sub_desc = _truncate(
                sub.get("description") or "", max_sub_desc_chars
            )
            sub_lines.append(
                f"    * sub_skill={sub_id!r}: {sub_desc}"
            )
        line += "\n  sub-skills:\n" + "\n".join(sub_lines)

    return line


def build_catalog_block(
    skills: Sequence[SkillRecord],
    *,
    max_total_chars: int = _DEFAULT_MAX_TOTAL_CHARS,
) -> str:
    """
    Assemble the catalog block, respecting ``max_total_chars``.

    Args:
        skills:          The skills to render, already ordered by
                         relevance (typically BM25 output). The order
                         is preserved as-is; this function does NOT
                         re-sort.
        max_total_chars: Hard character budget for the whole block
                         (header + entries + separator + trailing
                         newline). Skills that would push the block
                         over budget are dropped.

    Returns:
        The formatted catalog block, ready to be appended to the
        system prompt. Returns ``""`` when ``skills`` is empty — the
        caller should treat that as "no catalog to inject" and skip
        appending anything.

    Behavior on over-budget:
        When the next entry would exceed the budget, iteration stops
        and a single line ``... (N more skill(s) omitted for brevity)``
        is appended. This tells the model the catalog is partial
        without enumerating the omitted entries (which would blow
        the budget anyway).
    """
    if not skills:
        return ""

    lines: list[str] = [CATALOG_HEADER]
    total = len(CATALOG_HEADER)
    included = 0

    for skill in skills:
        entry = _build_one(skill)
        # The join adds one "\n" between the previous element and this
        # one. Add +1 for safety so an entry that lands exactly at the
        # budget boundary does not push us over by an off-by-one.
        added = len(entry) + 2
        if total + added > max_total_chars:
            break
        lines.append(entry)
        total += added
        included += 1

    omitted = len(skills) - included
    if omitted > 0:
        lines.append(
            f"  ... ({omitted} more skill(s) omitted for brevity)"
        )

    block = "\n".join(lines) + "\n"

    logger.debug(
        "Catalog built: included=%d/%d chars=%d budget=%d.",
        included,
        len(skills),
        len(block),
        max_total_chars,
    )

    return block


__all__ = [
    "CATALOG_HEADER",
    "build_catalog_block",
]
