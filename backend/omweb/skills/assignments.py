# backend/omweb/skills/assignments.py
"""
Agent ↔ Skill assignment layer and unified injection resolver.

Responsibilities:
    * Read and write per-agent skill assignments, persisted in
      ``agent_settings.json`` alongside ``disabled_tools`` /
      ``extra_tools`` / ``disabled_mcp_servers`` (same storage
      pattern as tools and MCP servers — no parallel state files).
    * Validate skill IDs on write: unknown, disabled, or
      non-assignable skills are rejected with a precise error.
    * Provide the single, authoritative function the engine uses to
      inject skill instructions into the system prompt:
      ``resolve_skill_injection``.
    * Never raise at engine time: a broken skill must not take down a
      chat. Failures are collected in ``warnings`` and the run
      continues with the surviving skills.

Relationship with ``agents/registry.py``:
    The registry owns the persistence mechanics (read/write
    ``agent_settings.json``, atomic writes, per-agent merging) via
    ``get_agent_skills_state`` / ``save_agent_skills_state``.
    This module wraps those methods with:
        * skill-ID validation against the skill registry,
        * ``agent_assignable`` / ``chat_invocable`` gating,
        * the merge of assigned + invoked skills into one injection
          payload.

    This split keeps ``agents/registry.py`` free of skill-specific
    logic while still using it as the single source of truth for
    on-disk state.

Injection precedence:
    When the same skill appears in both channels, it is injected
    ONCE with source ``"invoked"`` (the user's explicit choice for
    this turn is preferred over the agent's ambient assignment).

    Ordering within the injection block:
        1. Invoked skills, sorted by ID.
        2. Assigned skills not already invoked, sorted by ID.

    This puts the user's explicit intent first while keeping the
    output deterministic across runs (essential for prompt caching
    and reproducibility).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Dict, List, Optional

from omweb.agents.registry import (
    AgentNotFoundError,
    agent_registry,
)
from omweb.skills.errors import (
    SkillAssignmentError,
    SkillLoadError,
    SkillSecurityError,
)
from omweb.skills.loader import get_injection_block
from omweb.skills.registry import (
    SkillRecord,
    normalize_skill_id,
    skill_registry,
)
from omweb.skills.router.bm25_router import BM25SkillRouter
from omweb.skills.router.catalog_builder import build_catalog_block
from omweb.skills.router.token_estimator import estimate_tokens


logger = logging.getLogger("omweb.skills.assignments")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillInjection:
    """
    Immutable result of resolving skill injection for a single run.

    Attributes:
        text:     Concatenated ``--- BEGIN SKILL --- ... --- END SKILL ---``
                  blocks, ready to be appended to the system prompt.
                  Empty string when no skill was successfully loaded.
        skills:   Per-skill metadata for telemetry and diagnostics::

                      [{"id": str, "version": str, "name": str,
                        "source": "assigned" | "invoked"}]

        warnings: Non-fatal issues encountered while resolving (a
                  skill was deleted after assignment, its instructions
                  failed to load, etc.). The engine logs these but
                  continues.
    """

    text: str
    skills: List[Dict[str, Any]]
    warnings: List[str]


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def get_agent_skills_state(agent_id: str) -> Dict[str, Any]:
    """
    Return the current skill assignment state for an agent.

    Delegates to ``agent_registry.get_agent_skills_state`` and enriches
    the result with resolved metadata (name, version, enabled) for each
    effective skill, so the frontend can render rich cards without a
    second round-trip per skill.

    Raises:
        AgentNotFoundError: when the agent does not exist. The API
            layer maps this to HTTP 404.
    """
    state = agent_registry.get_agent_skills_state(agent_id)

    # Enrich each effective ID with display metadata. Broken / missing
    # skills stay in the list so the UI can flag them, but ``resolved``
    # is set to False and the metadata fields are None.
    enriched: List[Dict[str, Any]] = []
    for skill_id in state.get("effective_skills", []):
        record: Optional[SkillRecord] = skill_registry.get_skill(skill_id)
        if record is None:
            enriched.append(
                {
                    "id": skill_id,
                    "resolved": False,
                    "name": skill_id,
                    "version": None,
                    "enabled": False,
                    "is_deprecated": False,
                    "agent_assignable": False,
                }
            )
            continue
        enriched.append(
            {
                "id": record.id,
                "resolved": True,
                "name": record.name,
                "version": record.version,
                "enabled": record.enabled,
                "is_deprecated": record.is_deprecated,
                "agent_assignable": record.agent_assignable,
            }
        )

    return {
        "manifest_skills": list(state.get("manifest_skills", [])),
        "disabled_skills": list(state.get("disabled_skills", [])),
        "extra_skills": list(state.get("extra_skills", [])),
        "effective_skills": list(state.get("effective_skills", [])),
        "skills_editable": bool(state.get("skills_editable", False)),
        "resolved_skills": enriched,
    }


def get_effective_skill_ids(agent_id: str) -> List[str]:
    """
    Return the effective skill IDs assigned to an agent.

    This is the low-level call used by the engine (via
    ``resolve_skill_injection``) on every run. It does NOT validate
    that each ID still resolves to a live skill — the caller does
    that lazily so a stale assignment never blocks the run.

    Returns ``[]`` when the agent does not exist, so engine callers
    never have to guard against a missing manifest.
    """
    try:
        state = agent_registry.get_agent_skills_state(agent_id)
    except AgentNotFoundError:
        logger.debug(
            "get_effective_skill_ids called for unknown agent %r; "
            "returning empty list.",
            agent_id,
        )
        return []
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "Failed to read skill assignment state for agent %r: %s",
            agent_id,
            exc,
        )
        return []

    return list(state.get("effective_skills", []))


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------

def save_agent_skills_state(
    agent_id: str,
    disabled: List[str],
    extra: List[str],
) -> Dict[str, Any]:
    """
    Persist skill overrides for an agent, validating each ID.

    Validation rules:
        * Every ``disabled`` and ``extra`` ID must resolve to an
          installed skill. Unknown IDs are rejected with a precise
          error naming the offending ID.
        * Every ``extra`` ID must additionally be:
            - currently ``active`` (you cannot add a disabled skill),
            - declared ``agent_assignable`` in its manifest.
          ``disabled`` IDs are NOT required to be active — excluding a
          skill is valid regardless of its current status.

    On success, returns the same shape as
    :func:`get_agent_skills_state` (including ``resolved_skills``).

    Raises:
        AgentNotFoundError:   when the agent does not exist.
        SkillAssignmentError: when any skill ID fails validation, or
                              when the agent's manifest locks skill
                              editing (``skills_editable: false``).
    """
    # Ensure the agent exists first so we fail with the right error.
    try:
        agent_registry.get_agent(agent_id)
    except AgentNotFoundError:
        raise

    # ── Validate disabled list ────────────────────────────────────
    for raw_id in disabled:
        record = skill_registry.get_skill(raw_id)
        if record is None:
            raise SkillAssignmentError(
                f"Cannot disable unknown skill {raw_id!r}.",
                code="SKILL_DISABLE_UNKNOWN",
                details={"skill_id": raw_id, "agent_id": agent_id},
            )

    # ── Validate extra list ───────────────────────────────────────
    for raw_id in extra:
        record = skill_registry.get_skill(raw_id)
        if record is None:
            raise SkillAssignmentError(
                f"Cannot add unknown skill {raw_id!r}.",
                code="SKILL_ASSIGN_UNKNOWN",
                details={"skill_id": raw_id, "agent_id": agent_id},
            )
        if not record.enabled:
            raise SkillAssignmentError(
                f"Cannot add disabled skill {record.id!r}. "
                f"Enable it first.",
                code="SKILL_ASSIGN_DISABLED",
                details={
                    "skill_id": record.id,
                    "agent_id": agent_id,
                },
            )
        if not record.agent_assignable:
            reason = (
                "marked deprecated"
                if record.is_deprecated
                else "not agent_assignable"
            )
            raise SkillAssignmentError(
                f"Cannot add skill {record.id!r}: it is {reason}.",
                code="SKILL_ASSIGN_NOT_ALLOWED",
                details={
                    "skill_id": record.id,
                    "agent_id": agent_id,
                    "reason": reason,
                },
            )

    # ── Delegate to registry for persistence ──────────────────────
    # The registry raises ValueError when skills_editable is false;
    # translate that into a SkillAssignmentError for a consistent
    # error type across the assignments layer.
    try:
        state = agent_registry.save_agent_skills_state(
            agent_id, disabled, extra
        )
    except ValueError as exc:
        raise SkillAssignmentError(
            str(exc),
            code="SKILL_EDIT_LOCKED",
            details={"agent_id": agent_id},
        ) from exc

    logger.info(
        "Saved skill assignment for agent %r: "
        "disabled=%d, extra=%d, effective=%d",
        agent_id,
        len(state.get("disabled_skills", [])),
        len(state.get("extra_skills", [])),
        len(state.get("effective_skills", [])),
    )

    # Re-read through the enriched path so the API returns the same
    # shape as the GET endpoint.
    return get_agent_skills_state(agent_id)


# ---------------------------------------------------------------------------
# Injection resolver — the single entry point for the engine
# ---------------------------------------------------------------------------

def resolve_skill_injection(
    agent_id: str,
    invoked_skill_ids: Optional[List[str]] = None,
) -> SkillInjection:
    """
    Merge assigned and invoked skills into one injection payload.

    This is THE function the engine calls before building the system
    prompt. It:

        1. Reads the agent's effective assigned skills (never raises —
           a missing agent yields an empty assigned list).
        2. Merges the ``invoked_skill_ids`` (from ``/`` in the chat
           composer) on top, with invoked skills taking priority when
           the same ID appears in both channels.
        3. Loads each skill's instructions via
           :func:`omweb.skills.loader.get_injection_block`.
        4. Skips any skill that fails to load (missing file,
           oversized body, security violation) with a warning; the
           run continues.
        5. Returns the concatenated block text plus metadata for
           telemetry.

    Args:
        agent_id:           The agent whose assigned skills to include.
        invoked_skill_ids:  Explicit ``/`` invocations for this run.

    Returns:
        A :class:`SkillInjection` with empty ``text`` when no skill
        could be loaded. The engine must check ``text`` before
        appending it to the prompt.
    """
    warnings: List[str] = []

    # ── 1. Read assigned skills (best-effort) ─────────────────────
    assigned_ids = get_effective_skill_ids(agent_id)
    if agent_id and not assigned_ids:
        logger.debug(
            "Agent %r has no effective assigned skills.", agent_id
        )

    # ── 2. Merge with invoked skills (invoked wins) ───────────────
    normalized_invoked: List[str] = []
    for raw in invoked_skill_ids or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        normalized = normalize_skill_id(raw)
        if normalized and normalized not in normalized_invoked:
            normalized_invoked.append(normalized)

    # Build the source map: dict preserves insertion order but we
    # re-sort deterministically below, so only priority matters.
    source_by_id: Dict[str, str] = {}
    for sid in assigned_ids:
        source_by_id[sid] = "assigned"
    for sid in normalized_invoked:
        # Invoked overrides assigned when the same skill appears in
        # both — this is the user's explicit intent for this turn.
        source_by_id[sid] = "invoked"

    if not source_by_id:
        return SkillInjection(text="", skills=[], warnings=[])

    # ── 3. Deterministic ordering ─────────────────────────────────
    invoked_set = {
        sid for sid, src in source_by_id.items() if src == "invoked"
    }
    ordered_ids: List[str] = []
    ordered_ids.extend(sorted(invoked_set))
    ordered_ids.extend(
        sorted(sid for sid in source_by_id if sid not in invoked_set)
    )

    # ── 4. Load each skill (never raise) ──────────────────────────
    blocks: List[str] = []
    injected: List[Dict[str, Any]] = []

    for sid in ordered_ids:
        source = source_by_id[sid]
        record = skill_registry.get_skill(sid)

        if record is None:
            warnings.append(
                f"Skill {sid!r} ({source}) is not installed; skipping."
            )
            logger.warning(
                "Skill %r (%s) not installed during injection for "
                "agent %r; skipping.",
                sid,
                source,
                agent_id,
            )
            continue

        if not record.enabled:
            # Assigned-but-disabled: skip silently (informational).
            # Invoked-but-disabled: skip with a warning so the user
            # sees why their / command had no effect.
            if source == "invoked":
                warnings.append(
                    f"Skill {record.id!r} is disabled; skipping."
                )
                logger.info(
                    "Invoked skill %r is disabled; skipping injection.",
                    record.id,
                )
            else:
                logger.debug(
                    "Assigned skill %r is disabled; skipping injection.",
                    record.id,
                )
            continue

        # Gate invoked skills on chat_invocable and assigned skills
        # on agent_assignable — matches the capabilities declared in
        # the manifest.
        if source == "invoked" and not record.chat_invocable:
            warnings.append(
                f"Skill {record.id!r} cannot be invoked from chat; "
                f"skipping."
            )
            continue
        if source == "assigned" and not record.agent_assignable:
            warnings.append(
                f"Skill {record.id!r} cannot be assigned to agents; "
                f"skipping."
            )
            continue

        try:
            block = get_injection_block(record)
        except (SkillLoadError, SkillSecurityError) as exc:
            warnings.append(
                f"Skill {record.id!r} failed to load: {exc}"
            )
            logger.warning(
                "Skill %r failed to load during injection: %s",
                record.id,
                exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001 — defensive
            warnings.append(
                f"Skill {record.id!r} failed with unexpected error: {exc}"
            )
            logger.exception(
                "Unexpected error loading skill %r for injection: %s",
                record.id,
                exc,
            )
            continue

        blocks.append(block)
        injected.append(
            {
                "id": record.id,
                "version": record.version,
                "name": record.name,
                "source": source,
            }
        )

    if not blocks:
        return SkillInjection(
            text="",
            skills=[],
            warnings=warnings,
        )

    # Two newlines between blocks keeps the delimiters visually
    # separated even when the markdown renderer collapses whitespace.
    combined = "\n\n".join(blocks)

    return SkillInjection(
        text=combined,
        skills=injected,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Progressive Disclosure resolver — catalog-based injection
# ---------------------------------------------------------------------------

def resolve_skill_injection_catalog(
    agent_id: str,
    invoked_skill_ids: Optional[List[str]] = None,
    user_message: str = "",
    *,
    top_k: int = 5,
    use_bm25: bool = True,
) -> SkillInjection:
    """
    Progressive-disclosure variant of :func:`resolve_skill_injection`.

    Instead of injecting every assigned skill's full body, this function
    returns a compact Catalog block listing the top-k most relevant
    skills (ranked by BM25 against ``user_message``). The model then
    calls ``load_skill``/``load_sub_skill`` at inference time to fetch
    only the bodies it needs.

    Pool of candidates:
        ALL enabled non-deprecated skills, NOT just the agent's
        assigned ones. This is the core of Progressive Disclosure —
        the catalog exposes the full toolkit so the model can pick the
        right tool regardless of static assignment.

        Invoked skills (via ``/<skill-id>``) are ALWAYS included in the
        catalog regardless of their BM25 score, because the user chose
        them explicitly. When the BM25 pool is empty (no user message,
        BM25 disabled, or all scores below ``min_score``), the invoked
        skills alone populate the catalog.

    Args:
        agent_id:           The agent whose assignment channel to
                            consult for the ``source`` label.
        invoked_skill_ids:  Explicit ``/`` invocations for this turn.
        user_message:       The current user prompt (BM25 query).
        top_k:              Maximum catalog size from BM25. Invoked
                            skills are added on top (they can push the
                            count above top_k).
        use_bm25:           If False, skip ranking and include every
                            enabled skill (capped by the catalog
                            builder's character budget).

    Returns:
        A :class:`SkillInjection` whose ``text`` is the formatted
        catalog block (or empty if no skill is included), whose
        ``skills`` list carries telemetry per entry, and whose
        ``warnings`` list accumulates non-fatal issues.

        Important: ``text`` here is NOT a list of full skill bodies.
        The engine appends it to the system prompt as instructions
        plus a compact catalog. The model is expected to call
        ``load_skill`` for the actual content.
    """
    warnings: List[str] = []

    # ── 1. Source map (assigned vs invoked) ───────────────────────
    assigned_ids = get_effective_skill_ids(agent_id)
    source_by_id: Dict[str, str] = {sid: "assigned" for sid in assigned_ids}

    invoked_set: set[str] = set()
    for raw in invoked_skill_ids or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        normalized = normalize_skill_id(raw)
        if not normalized:
            continue
        source_by_id[normalized] = "invoked"
        invoked_set.add(normalized)

    # ── 2. Candidate pool (all enabled, non-deprecated) ───────────
    all_enabled = skill_registry.list_skills(
        include_disabled=False,
        include_deprecated=False,
    )
    if not all_enabled:
        return SkillInjection(text="", skills=[], warnings=warnings)

    # ── 3. Ranking ────────────────────────────────────────────────
    scores: Dict[str, float] = {}
    if use_bm25 and user_message:
        try:
            router = BM25SkillRouter(all_enabled)
            ranked = router.rank(user_message, top_k=top_k)
            selected: List[SkillRecord] = [s for s, _ in ranked]
            scores = {s.id: float(sc) for s, sc in ranked}
        except Exception as exc:  # noqa: BLE001 — never raise
            warnings.append(
                f"BM25 routing failed; falling back to full catalog: {exc}"
            )
            logger.warning(
                "BM25 ranking failed for agent %r: %s", agent_id, exc
            )
            selected = list(all_enabled)
    else:
        selected = list(all_enabled)

    # ── 4. Always include invoked skills ──────────────────────────
    # A skill the user explicitly invoked must appear in the catalog
    # even if BM25 dropped it (short description, unusual query, or
    # an empty candidate pool). We iterate over the FULL pool so that
    # an invoked skill missing from `selected` is re-added in place.
    selected_ids = {s.id for s in selected}
    for skill in all_enabled:
        if skill.id in invoked_set and skill.id not in selected_ids:
            selected.append(skill)
            selected_ids.add(skill.id)

    if not selected:
        return SkillInjection(text="", skills=[], warnings=warnings)

    # ── 5. Build catalog block ────────────────────────────────────
    catalog = build_catalog_block(selected)
    if not catalog:
        return SkillInjection(text="", skills=[], warnings=warnings)

    injected: List[Dict[str, Any]] = [
        {
            "id": s.id,
            "version": s.version,
            "name": s.name,
            "source": source_by_id.get(s.id, "ranked"),
            "has_sub_skills": bool(s.sub_skills),
            "score": scores.get(s.id, 0.0),
        }
        for s in selected
    ]

    logger.info(
        "Progressive disclosure catalog: %d skill(s) (assigned=%d, "
        "invoked=%d, ranked=%d), ~%d tokens.",
        len(selected),
        sum(1 for s in selected if source_by_id.get(s.id) == "assigned"),
        sum(1 for s in selected if source_by_id.get(s.id) == "invoked"),
        sum(
            1
            for s in selected
            if source_by_id.get(s.id) not in ("assigned", "invoked")
        ),
        estimate_tokens(catalog),
    )

    return SkillInjection(
        text=catalog,
        skills=injected,
        warnings=warnings,
    )


__all__ = [
    "SkillInjection",
    "get_agent_skills_state",
    "get_effective_skill_ids",
    "save_agent_skills_state",
    "resolve_skill_injection",
    "resolve_skill_injection_catalog",
]
