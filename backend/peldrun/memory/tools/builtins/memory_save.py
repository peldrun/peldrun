# backend/peldrun/memory/tools/builtins/memory_save.py
"""
PELDRUN Memory Platform - Memory Save Tool.

Tool for persisting a memory entry via the memory subsystem.
Invoked by agents during a run (``MemorySourceType.AGENT_TOOL``)
or by the UI when the user explicitly clicks "Save to memory"
(``MemorySourceType.EXPLICIT_USER``).

Design invariants
-----------------

1. **Stateless execution.**
   The tool holds no per-execution state. Multiple concurrent
   invocations against the same instance are safe because every
   mutable variable lives on the stack of ``execute``. This is
   required by the agent runtime: the same tool instance is
   reused across turns.

2. **Dependency injection, not global lookup.**
   ``port`` and ``settings`` are supplied at construction time.
   The tool never imports ``omweb.memory`` and never reads the
   ``MemoryService`` singleton directly. This keeps the tool
   testable without a FastAPI app and preserves the architectural
   rule that ``peldrun/`` does not import ``omweb/``.

3. **Mode enforcement at the tool boundary.**
   ``MemoryMode.READ_ONLY`` and ``MemoryMode.DISABLED`` both
   reject every save with a structured ``{"status": "error"}``
   result. The plan's acceptance test #12 ("read_only prevents
   memory_save") is enforced HERE, not left to the caller.

4. **Structured result, never raises.**
   Every failure mode -- mode rejection, validation error,
   duplicate, backend unavailable -- is returned as a dict with
   ``status`` and, where relevant, ``error`` and ``reason``. The
   agent runtime treats tool exceptions as run-fatal; the memory
   subsystem is an ENHANCEMENT and must never abort a run.

5. **Write policy applied inline.**
   The tool uses ``decide_write_action`` (P1.4) to determine
   CREATE / SKIP / SUPERSEDE / REACTIVATE. This is the SAME
   policy the future ``WritePipeline`` (P1.7) will use -- the
   two implementations share the decision function so their
   behavior cannot diverge.

6. **Scope resolution is explicit-or-context.**
   Callers either pass ``scope_type`` + ``scope_id`` directly
   (explicit) or pass ``chat_id`` + optional ``project_id`` and
   let ``resolve_scope`` pick the innermost scope (contextual).
   The tool never invents a scope: an unresolvable context
   returns an error rather than silently falling back to USER
   scope, which would leak project facts into the user's global
   memory.

Examples
--------

Agent-initiated save::

    tool = MemorySaveTool(port=provider, settings=settings)
    result = await tool.execute(
        content="User prefers dark mode",
        memory_type="preference",
        chat_id="chat_abc",
        project_id="proj_xyz",
        importance=70,
        tags=["ui", "preferences"],
    )
    # -> {"status": "created", "memory_id": "mem_...", "scope": "project:proj_xyz"}

User-initiated save (UI)::

    result = await tool.execute(
        content="Q3 deadline is October 15",
        memory_type="semantic",
        source_type="explicit_user",
        scope_type="project",
        scope_id="proj_xyz",
        importance=90,
    )

Duplicate detected (no write)::

    # -> {"status": "skipped",
    #     "existing_id": "mem_...",
    #     "reason": "duplicate_of_active:mem_...:content_hash=..."}

Conflict superseded (old archived, new active)::

    # -> {"status": "superseded",
    #     "memory_id": "mem_new",
    #     "replaced_id": "mem_old",
    #     "reason": "conflict_with_active:mem_old:replacement_marker_detected"}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryPort,
    MemoryProvenance,
    MemoryScope,
    MemorySettings,
    MemorySourceType,
    MemoryStatus,
    MemoryType,
    MemoryUnavailableError,
    MemoryValidationError,
)
from peldrun.memory.domain.scope import resolve_scope
from peldrun.memory.services.policies import (
    WriteAction,
    decide_write_action,
)


logger = logging.getLogger("peldrun.memory.tools.memory_save")


class MemorySaveTool:
    """Tool for persisting a memory entry.

    See the module docstring for the full contract, rationale,
    and usage examples. The class itself is intentionally thin:
    all policy logic lives in ``peldrun.memory.services.policies``
    (P1.4) and all storage logic lives in the injected
    ``MemoryPort``. The tool is only the GLUE between the two,
    plus the mode check that P1.4's pure functions cannot
    perform (they have no access to settings).

    Attributes
    ----------
    name : str
        Canonical tool name, matching the registry key the agent
        runtime uses to dispatch ``tool_call`` entries. Stable
        within a release.
    description : str
        Human-readable description surfaced to the LLM. Kept
        short -- the LLM already has the schema from the tool
        definition; this string is the natural-language summary
        the model reasons about.
    """

    name: str = "memory_save"
    description: str = (
        "Persist a memory entry for future retrieval. Use when the "
        "user explicitly asks to remember something, or when a "
        "durable fact, preference, or decision should be captured "
        "for later turns."
    )

    def __init__(
        self,
        port: MemoryPort,
        settings: MemorySettings,
    ) -> None:
        """Construct the tool.

        Parameters
        ----------
        port
            A ``MemoryPort`` instance (typically a
            ``SqliteFtsProvider`` from P1.3, but any object
            satisfying the protocol works). MUST already be open
            if it has an ``open`` lifecycle method -- the tool
            does not manage provider lifecycle.
        settings
            The current ``MemorySettings`` snapshot. The tool
            reads ``settings.mode`` to enforce DISABLED and
            READ_ONLY, and ``settings.default_limit`` when
            scanning existing entries for duplicates.

        Raises
        ------
        TypeError
            When ``port`` or ``settings`` are the wrong type.
            Structural argument validation -- caller bugs
            surface immediately.
        """
        if port is None:
            raise TypeError("port must be a MemoryPort instance, got None")
        if not isinstance(settings, MemorySettings):
            raise TypeError(
                f"settings must be MemorySettings, got "
                f"{type(settings).__name__}"
            )
        self._port: MemoryPort = port
        self._settings: MemorySettings = settings

    async def execute(
        self,
        *,
        content: str,
        memory_type: str = "semantic",
        source_type: str = "agent_tool",
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        importance: int = 50,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Persist a memory entry.

        Parameters
        ----------
        content
            The memory content. Trimmed at construction; empty
            content is rejected. Maximum length is enforced by
            ``MemoryEntry``'s validator (10,000 chars).

        memory_type
            One of ``semantic`` / ``episodic`` / ``procedural``
            / ``preference`` / ``decision``. Invalid values are
            rejected by ``MemoryEntry``'s enum coercion.

        source_type
            One of ``agent_tool`` (default -- an agent invoked
            this tool during a run) or ``explicit_user`` (the
            user explicitly asked). Any other value is rejected
            by ``MemoryProvenance``. ``IMPORT`` and ``EXTRACTION``
            are not valid here -- those flow through different
            subsystems.

        scope_type
            Optional explicit scope kind. Must be paired with
            ``scope_id``. When both are provided, they take
            precedence over ``chat_id`` / ``project_id``. Useful
            for the UI's "save to user memory" flow, which
            overrides the contextual scope.

        scope_id
            Optional explicit scope owner. See ``scope_type``.

        chat_id
            Context chat id. Used (with ``project_id``) by
            ``resolve_scope`` when explicit scope is not
            provided.

        project_id
            Context project id. When provided, project scope
            wins over chat scope (project memories outlive the
            chat session).

        importance
            0-100. Defaults to 50. Ignored when the write
            decision is SKIP or SUPERSEDE against an existing
            entry whose importance is higher -- the decision
            function preserves the higher value.

        tags
            Optional topic markers. Lowercased, deduplicated,
            and capped at 32 by ``MemoryEntry``'s validator.

        Returns
        -------
        Dict[str, Any]
            One of the following shapes:

            * ``{"status": "created", "memory_id": str, "scope": str}``
            * ``{"status": "skipped", "existing_id": str, "reason": str}``
            * ``{"status": "superseded", "memory_id": str,
                  "replaced_id": str, "reason": str}``
            * ``{"status": "reactivated", "memory_id": str,
                  "reason": str}``
            * ``{"status": "error", "error": str, "code": str}``

            The ``code`` field on errors is a stable string
            (``"mode_disabled"``, ``"validation_error"``,
            ``"backend_unavailable"``) so callers can branch
            without parsing ``error`` text.
        """
        # ------------------------------------------------------------------
        # 1. Mode enforcement (BEFORE any work)
        # ------------------------------------------------------------------
        #
        # The mode check happens first so that a save attempt in
        # DISABLED or READ_ONLY mode returns immediately without
        # touching the port, without allocating a MemoryEntry, and
        # without consuming any clock/CPU. This matters for the
        # READ_ONLY case in particular: an agent might call the
        # tool speculatively on every turn, and rejecting it here
        # keeps the overhead to a single enum comparison.
        if not self._settings.mode.can_write:
            logger.info(
                "MemorySaveTool rejected: mode=%s disallows writes "
                "(content_preview=%r).",
                self._settings.mode.value,
                content[:60] if content else "",
            )
            return {
                "status": "error",
                "error": (
                    f"Memory writes are disabled (mode="
                    f"{self._settings.mode.value}). Set "
                    f"memory mode to 'read_write' to enable saving."
                ),
                "code": "mode_disabled",
            }

        # ------------------------------------------------------------------
        # 2. Resolve scope
        # ------------------------------------------------------------------
        try:
            resolved_scope_type, resolved_scope_id = self._resolve_scope(
                scope_type=scope_type,
                scope_id=scope_id,
                chat_id=chat_id,
                project_id=project_id,
            )
        except ValueError as exc:
            return {
                "status": "error",
                "error": f"Cannot resolve memory scope: {exc}",
                "code": "validation_error",
            }

        # ------------------------------------------------------------------
        # 3. Build the candidate entry
        # ------------------------------------------------------------------
        #
        # MemoryEntry.create runs the full field validation (enum
        # coercion, length caps, tag normalization). Any violation
        # raises ValueError, which we translate into the structured
        # error result rather than letting it propagate. The agent
        # runtime treats tool exceptions as run-fatal; the memory
        # subsystem must never abort a run.
        try:
            candidate = MemoryEntry.create(
                content=content,
                memory_type=MemoryType(memory_type),
                scope_type=resolved_scope_type,
                scope_id=resolved_scope_id,
                provenance=MemoryProvenance(
                    source_type=MemorySourceType(source_type),
                    source_chat_id=chat_id,
                    source_turn_id=None,
                ),
                importance=importance,
                tags=tags or [],
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "MemorySaveTool rejected candidate (content=%r): %s",
                content[:60] if content else "",
                exc,
            )
            return {
                "status": "error",
                "error": f"Invalid memory entry: {exc}",
                "code": "validation_error",
            }

        scope_ref = f"{resolved_scope_type.value}:{resolved_scope_id}"

        # ------------------------------------------------------------------
        # 4. Fetch existing entries in the same scope
        # ------------------------------------------------------------------
        #
        # Duplicate and conflict detection need to see what is
        # already in the scope. ``search`` with an empty query
        # returns a recency-ordered list -- exactly what we need
        # to detect duplicates (a re-save of a recent entry) and
        # conflicts (a replacement marker against a recent fact).
        #
        # ``default_limit`` is used as the scan cap. The default
        # (5) is small but the plan's policy functions were
        # designed for this -- they check exact hash, then
        # near-duplicate Jaccard, then conflict markers. A larger
        # scan cap would improve conflict detection on very
        # active scopes, but the write path would pay the cost
        # on every save. The trade-off is documented and can be
        # tuned via ``default_limit`` in settings.
        try:
            existing = await self._port.search(
                query="",
                scope_refs=[scope_ref],
                limit=max(1, int(self._settings.default_limit)),
                # ``status="all"`` is REQUIRED for REACTIVATE to
                # work. The repository's default filter is
                # ``status='active'``, which hides ARCHIVED and
                # SUPERSEDED entries. Without them,
                # ``decide_write_action`` cannot detect that a
                # candidate matches a soft-deleted entry, so
                # saving the same content a second time (after the
                # user deleted it) would hit the UNIQUE constraint
                # instead of reactivating the archived entry.
                # See ``base_adapter._fetch_existing_for_dedupe``
                # for the full rationale -- same fix, same
                # failure mode.
                status="all",
            )
        except MemoryUnavailableError as exc:
            logger.error(
                "MemorySaveTool: backend unavailable during duplicate "
                "scan (scope=%s): %s",
                scope_ref,
                exc,
            )
            return {
                "status": "error",
                "error": (
                    "Memory backend unavailable; save could not be "
                    "completed. Try again shortly."
                ),
                "code": "backend_unavailable",
            }

        # ------------------------------------------------------------------
        # 5. Decide the write action
        # ------------------------------------------------------------------
        #
        # ``decide_write_action`` is a pure function (P1.4) that
        # never raises. It returns a frozen WriteDecision with an
        # action, the target entry (when applicable), and a
        # diagnostic reason.
        decision = decide_write_action(candidate, existing)

        # ------------------------------------------------------------------
        # 6. Execute the decision
        # ------------------------------------------------------------------
        try:
            return await self._execute_decision(decision, scope_ref)
        except MemoryUnavailableError as exc:
            logger.error(
                "MemorySaveTool: backend failure during %s (scope=%s): %s",
                decision.action.value,
                scope_ref,
                exc,
            )
            return {
                "status": "error",
                "error": (
                    f"Memory backend unavailable during "
                    f"{decision.action.value}. The change was not "
                    f"applied."
                ),
                "code": "backend_unavailable",
            }
        except MemoryValidationError as exc:
            # Rare -- a validation failure during the actual write
            # that the candidate constructor did not catch. Most
            # commonly a UNIQUE constraint race (two concurrent
            # saves of the same content).
            logger.warning(
                "MemorySaveTool: validation failure during %s: %s",
                decision.action.value,
                exc,
            )
            return {
                "status": "error",
                "error": f"Validation error: {exc.message}",
                "code": "validation_error",
            }

    # ------------------------------------------------------------------
    # Scope resolution
    # ------------------------------------------------------------------

    def _resolve_scope(
        self,
        *,
        scope_type: Optional[str],
        scope_id: Optional[str],
        chat_id: Optional[str],
        project_id: Optional[str],
    ) -> tuple[MemoryScope, str]:
        """Return ``(MemoryScope, scope_id_string)`` for the save.

        Three modes, in priority order:

          1. **Explicit.** Both ``scope_type`` and ``scope_id``
             supplied. Used by the UI's "Save to memory as X"
             dropdown.

          2. **Contextual.** ``chat_id`` and/or ``project_id``
             supplied, no explicit scope. Delegates to
             ``resolve_scope`` (P1.1), which picks the innermost
             scope (project > chat).

          3. **Invalid.** Neither explicit scope nor context.
             Raises ``ValueError`` -- a save without a target
             scope is a caller bug, and silently defaulting to
             USER scope would leak project facts across
             projects. Fail-fast is the correct response.

        Raises
        ------
        ValueError
            When neither branch produces a valid scope.
        """
        # Branch 1: explicit scope.
        if scope_type is not None or scope_id is not None:
            if scope_type is None or scope_id is None:
                raise ValueError(
                    "scope_type and scope_id must be provided "
                    "together (got scope_type="
                    f"{scope_type!r}, scope_id={scope_id!r})"
                )
            try:
                resolved_type = MemoryScope(scope_type)
            except ValueError:
                valid = ", ".join(
                    s.value for s in MemoryScope
                )
                raise ValueError(
                    f"unknown scope_type {scope_type!r}; "
                    f"expected one of: {valid}"
                ) from None
            if not scope_id.strip():
                raise ValueError("scope_id must be non-empty")
            return resolved_type, scope_id.strip()

        # Branch 2: contextual.
        try:
            ref = resolve_scope(chat_id=chat_id, project_id=project_id)
        except ValueError as exc:
            raise ValueError(
                f"no explicit scope provided, and no usable "
                f"chat/project context: {exc}"
            ) from exc
        return ref.scope_type, ref.scope_id

    # ------------------------------------------------------------------
    # Decision execution
    # ------------------------------------------------------------------

    async def _execute_decision(
        self,
        decision: Any,  # WriteDecision (avoid import-time circular risk)
        scope_ref: str,
    ) -> Dict[str, Any]:
        """Apply the ``WriteDecision`` to the backend.

        Dispatches on ``decision.action`` and returns the
        structured result for each branch. Exceptions from the
        port propagate to ``execute``'s caller, which translates
        them into structured errors.

        Parameters
        ----------
        decision
            The ``WriteDecision`` returned by
            ``decide_write_action``. Typed as ``Any`` to avoid
            importing ``WriteDecision`` at module scope -- the
            import is only needed for the ``isinstance`` check
            below, which is defensive and can be skipped.
        scope_ref
            The canonical scope reference string, used for the
            ``delete`` call in the SUPERSEDE branch.
        """
        # CREATE -- straightforward new entry.
        if decision.action == WriteAction.CREATE:
            created = await self._port.create(decision.candidate)
            logger.info(
                "MemorySaveTool created %s in scope %s "
                "(type=%s, importance=%d).",
                created.memory_id,
                scope_ref,
                created.memory_type.value,
                created.importance,
            )
            return {
                "status": "created",
                "memory_id": created.memory_id,
                "scope": scope_ref,
                "memory_type": created.memory_type.value,
            }

        # SKIP -- the exact/near duplicate already exists as ACTIVE.
        if decision.action == WriteAction.SKIP:
            target = decision.target_entry
            logger.debug(
                "MemorySaveTool skipped duplicate %s in scope %s.",
                target.memory_id if target else "<unknown>",
                scope_ref,
            )
            return {
                "status": "skipped",
                "existing_id": target.memory_id if target else None,
                "reason": decision.reason,
            }

        # SUPERSEDE -- archive the old entry, create the new one.
        #
        # Order matters: archive FIRST, then create. If the
        # create fails (UNIQUE constraint race, disk full), the
        # old entry is already archived and the caller sees an
        # error -- but the new entry is not partially written.
        # The reverse order would leave BOTH entries active if
        # the archive failed after a successful create, which is
        # worse (double-retrieval of contradictory facts).
        if decision.action == WriteAction.SUPERSEDE:
            target = decision.target_entry
            if target is None:
                # Should never happen -- SUPERSEDE always carries
                # a target. Defensive: fall back to a plain CREATE
                # rather than crash.
                logger.warning(
                    "MemorySaveTool: SUPERSEDE decision without "
                    "target_entry; falling back to CREATE."
                )
                created = await self._port.create(decision.candidate)
                return {
                    "status": "created",
                    "memory_id": created.memory_id,
                    "scope": scope_ref,
                    "memory_type": created.memory_type.value,
                }

            # Archive the old entry. ``delete`` is soft-delete
            # (status=archived) for the SQLite provider. A
            # provider that hard-deletes would break the audit
            # trail -- the plan's write policy assumes soft
            # delete, so a future provider MUST match that.
            await self._port.delete(target.memory_id, scope_ref)

            # Create the replacement.
            created = await self._port.create(decision.candidate)
            logger.info(
                "MemorySaveTool superseded %s with %s in scope %s.",
                target.memory_id,
                created.memory_id,
                scope_ref,
            )
            return {
                "status": "superseded",
                "memory_id": created.memory_id,
                "replaced_id": target.memory_id,
                "reason": decision.reason,
            }

        # REACTIVATE -- the target is archived or superseded and
        # the user is re-asserting. Update the existing row in
        # place (preserving memory_id, created_at) rather than
        # deleting and creating a new one. This keeps the entry's
        # history contiguous in the audit log.
        if decision.action == WriteAction.REACTIVATE:
            target = decision.target_entry
            if target is None:
                logger.warning(
                    "MemorySaveTool: REACTIVATE decision without "
                    "target_entry; falling back to CREATE."
                )
                created = await self._port.create(decision.candidate)
                return {
                    "status": "created",
                    "memory_id": created.memory_id,
                    "scope": scope_ref,
                    "memory_type": created.memory_type.value,
                }

            # Build the revived entry: same memory_id / created_at,
            # but with the candidate's content, metadata, and an
            # ACTIVE status. The ``model_copy`` preserves every
            # field we do not explicitly override, including
            # ``provenance`` from the original save. We DO update
            # ``provenance.source_chat_id`` to reflect this
            # reactivation -- the audit log should show "this was
            # revived in chat X" not just "created in chat Y".
            revived = target.model_copy(
                update={
                    "status": MemoryStatus.ACTIVE,
                    "content": decision.candidate.content,
                    "normalized_content": decision.candidate.normalized_content,
                    "content_hash": decision.candidate.content_hash,
                    "importance": decision.candidate.importance,
                    "tags": decision.candidate.tags,
                    "provenance": decision.candidate.provenance,
                }
            )
            updated = await self._port.update(revived)
            logger.info(
                "MemorySaveTool reactivated %s in scope %s.",
                updated.memory_id,
                scope_ref,
            )
            return {
                "status": "reactivated",
                "memory_id": updated.memory_id,
                "reason": decision.reason,
            }

        # Unreachable -- WriteAction has exactly four members and
        # all four are handled above. Defensive fallback so a
        # future fifth action does not silently return None.
        logger.error(
            "MemorySaveTool: unknown WriteAction %r; cannot execute.",
            decision.action,
        )
        return {
            "status": "error",
            "error": f"Unknown write action: {decision.action!r}",
            "code": "internal_error",
        }


__all__ = ["MemorySaveTool"]
