# backend/omweb/memory/manager/write_pipeline.py
"""
PELDRUN Memory Platform - Memory Write Pipeline.

The REUSABLE write path for memory entries. Every caller that
persists a candidate through the memory subsystem SHOULD go
through this pipeline so that:

    * write policies are applied consistently
      (``decide_write_action`` from P1.4),
    * mode enforcement happens in one place,
    * duplicate detection sees the SAME existing pool,
    * SUPERSEDE / REACTIVATE archival semantics are IDENTICAL
      across all sources.

Relationship to ``memory_save`` tool
------------------------------------

``memory_save`` (P1.5) has its own INLINE implementation of the
same policy. That duplication is deliberate for P1: when
``memory_save`` was written, it was the ONLY caller. Now that we
have (or will soon have):

    * extractor output (P1.4) -- candidates from conversation
      that need the same dedupe/supersede decisions,
    * import apply (P1.6) -- candidates from packages that need
      the same archival semantics,
    * future extraction outbox (P2) -- batch candidates from
      scheduled runs,

...a single shared pipeline eliminates the risk that a new
caller forgets one piece of the policy (e.g. forgets to fetch
``status="all"`` so REACTIVATE never fires).

The migration of ``memory_save`` to use this pipeline is
scheduled for P2. Until then, both paths produce IDENTICAL
outcomes by construction -- they call the same
``decide_write_action`` and the same provider primitives in the
same order. New callers MUST use this pipeline; existing
``memory_save`` is the ONLY tolerated exception.

Design invariants
-----------------

1. **Mode enforced at the pipeline.**
   ``save``/``save_many`` check ``settings.mode.can_write``
   before any work. A caller in ``READ_ONLY`` or ``DISABLED``
   gets ``MemoryUnavailableError`` from one place.

2. **Existing pool fetched ONCE per call.**
   The pool used for dedupe/conflict detection is fetched by
   the pipeline itself -- callers cannot accidentally pass a
   stale or partial pool. The ONLY exception is the optional
   ``existing_pool`` parameter on ``save``, used by
   ``save_many``'s internal batching (see invariant 3).

3. **Batched saves see their own prior creations.**
   ``save_many`` fetches the pool ONCE, then appends each
   successful creation. A batch of 10 entries where entry #7
   duplicates entry #3 correctly classifies #7 as SKIP. A naive
   per-entry loop would create the duplicate (racing the
   UNIQUE constraint -- one of the two would fail).

4. **Per-entry errors in batch mode do NOT abort.**
   A validation error on entry #4 does not prevent #5-#10 from
   being attempted. Each outcome carries its own error field.

5. **Scope match is verified up front.**
   A candidate whose scope does not match the requested
   ``scope_ref`` is a caller bug, not a data problem. In
   ``save_many``, this is verified BEFORE the batch starts so
   the error names the offending candidate index.

Cross-reference
---------------

* ``memory_save`` (P1.5) -- legacy inline implementation.
* ``base_adapter._apply_candidates`` (P1.6) -- parallel
  batch-write logic used by the import subsystem. Produces the
  same outcomes via the same primitives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryPort,
    MemorySettings,
    MemoryStatus,
    MemoryUnavailableError,
    MemoryValidationError,
)
from peldrun.memory.services import (
    WriteAction,
    WriteDecision,
    decide_write_action,
)


logger = logging.getLogger("omweb.memory.manager.write_pipeline")


_DEFAULT_SCAN_LIMIT: int = 500
"""Bounded fetch size for the existing-entries pool.

Matches the import adapter's dedupe scan cap (see
``base_adapter._DEDUPE_SCAN_LIMIT``). For most scopes this
fetches every entry; for larger scopes it fetches the top-500
by recency/importance -- enough to detect a re-save or an
explicit supersede.

``memory_save`` uses ``settings.default_limit`` (default 5) for
its pool, which is CHEAPER but less accurate. The pipeline uses
500 because it serves BATCH operations where spending an extra
~100ms to check 500 existing entries per batch is trivial
compared to the cost of creating 100 duplicates.
"""


@dataclass(frozen=True)
class WriteOutcome:
    """Result of one ``save`` or one element of ``save_many``.

    Attributes
    ----------
    action
        The ``WriteAction`` that was applied (or INTENDED to be
        applied, when ``error`` is set).
    memory
        The entry that is now active in the store:

          * CREATE     -> the newly persisted entry,
          * SKIP       -> the pre-existing entry (the target),
          * SUPERSEDE  -> the newly created replacement,
          * REACTIVATE -> the updated (revived) entry.

        ``None`` only on error.
    target_id
        For SKIP/SUPERSEDE/REACTIVATE: the ``memory_id`` of the
        entry the action targeted. ``None`` for CREATE.
    reason
        Diagnostic string, matching ``WriteDecision.reason``.
    error
        When non-None, the action FAILED and this describes why.
        ``action`` and ``reason`` are still populated with the
        INTENDED action so callers can report "tried to
        supersede, failed".
    """

    action: WriteAction
    memory: Optional[MemoryEntry] = None
    target_id: Optional[str] = None
    reason: str = ""
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    @property
    def created_new_active(self) -> bool:
        """True when a NEW active entry resulted from the call.

        SKIP returns ``False`` -- the store's state did not change.
        """
        return (
            self.succeeded
            and self.action
            in (
                WriteAction.CREATE,
                WriteAction.SUPERSEDE,
                WriteAction.REACTIVATE,
            )
        )


class MemoryWritePipeline:
    """Reusable write path applying ``decide_write_action`` + archival semantics.

    See the module docstring for the design rationale, the
    relationship to ``memory_save``, and the invariants.

    Lifecycle
    ---------
    Construct one pipeline per ``MemoryService`` instance (or per
    test). The pipeline is STATELESS between calls -- it holds
    only the immutable ``port`` and ``settings`` references. A
    settings reload between calls is observed automatically
    because ``save`` reads ``self._settings.mode`` on every
    call.

    Concurrency
    -----------
    Concurrent ``save`` calls against the same pipeline serialize
    at the SQLite level (single writer). No lock is needed at the
    Python layer -- the pipeline holds no mutable state.
    """

    def __init__(
        self,
        port: MemoryPort,
        settings: MemorySettings,
    ) -> None:
        if port is None:
            raise TypeError("port must be a MemoryPort instance, got None")
        if not isinstance(settings, MemorySettings):
            raise TypeError(
                f"settings must be MemorySettings, got "
                f"{type(settings).__name__}"
            )
        self._port: MemoryPort = port
        self._settings: MemorySettings = settings

    # ------------------------------------------------------------------
    # Single save
    # ------------------------------------------------------------------

    async def save(
        self,
        candidate: MemoryEntry,
        *,
        scope_ref: str,
        existing_pool: Optional[List[MemoryEntry]] = None,
    ) -> WriteOutcome:
        """Apply the write policy to ``candidate`` and persist the result.

        Parameters
        ----------
        candidate
            The ``MemoryEntry`` to save. Its scope MUST match
            ``scope_ref`` -- mismatch raises
            ``MemoryValidationError``.
        scope_ref
            The canonical scope reference string
            (``"<type>:<id>"``).
        existing_pool
            Optional pre-fetched pool of existing entries. When
            ``None`` (the default), the pipeline fetches it.
            Only pass a non-None value when you are confident
            it is complete enough for dedupe detection --
            ``save_many`` uses this internally.

        Returns
        -------
        WriteOutcome
            Never raises for write-time errors (backend failure,
            validation failure). The ONE exception that DOES
            propagate is ``MemoryUnavailableError`` when
            ``mode=DISABLED`` or the pool fetch itself fails --
            at that point NO write was attempted, so a per-entry
            error field would be misleading.
        """
        self._enforce_can_write()
        self._verify_scope_matches(candidate, scope_ref)

        pool = existing_pool
        if pool is None:
            pool = await self._fetch_existing_pool(scope_ref)

        try:
            decision = decide_write_action(candidate, pool)
        except Exception as exc:
            # ``decide_write_action`` is documented as never
            # raising; this is defense in depth against a
            # future regression.
            logger.exception(
                "WritePipeline: decide_write_action raised for "
                "scope %s: %s", scope_ref, exc,
            )
            return WriteOutcome(
                action=WriteAction.CREATE,  # safe default
                error=f"decision_failed: {exc}",
            )

        return await self._execute_decision(
            decision=decision,
            candidate=candidate,
            scope_ref=scope_ref,
        )

    # ------------------------------------------------------------------
    # Batch save
    # ------------------------------------------------------------------

    async def save_many(
        self,
        candidates: List[MemoryEntry],
        *,
        scope_ref: str,
    ) -> List[WriteOutcome]:
        """Apply the write policy to each candidate.

        Returns a list of the SAME length as ``candidates`` -- one
        outcome per input, in the same order. Callers can zip
        them together for per-candidate status.

        See the module docstring invariants 3 and 4 for the
        batch semantics.
        """
        self._enforce_can_write()

        if not candidates:
            return []

        # Verify scope for every candidate UP FRONT. A mismatch on
        # candidate #17 is cheaper to catch here than mid-batch.
        for idx, candidate in enumerate(candidates):
            try:
                self._verify_scope_matches(candidate, scope_ref)
            except MemoryValidationError as exc:
                raise MemoryValidationError(
                    f"candidate {idx + 1}: {exc.message}",
                    field_name=exc.field_name,
                    context={**exc.context, "candidate_index": idx},
                    cause=exc,
                ) from exc

        pool = await self._fetch_existing_pool(scope_ref)

        outcomes: List[WriteOutcome] = []
        for idx, candidate in enumerate(candidates):
            try:
                decision = decide_write_action(candidate, pool)
            except Exception as exc:
                outcomes.append(
                    WriteOutcome(
                        action=WriteAction.CREATE,
                        error=(
                            f"decision_failed: {exc} "
                            f"(candidate {idx + 1})"
                        ),
                    )
                )
                continue

            outcome = await self._execute_decision(
                decision=decision,
                candidate=candidate,
                scope_ref=scope_ref,
            )
            outcomes.append(outcome)

            # Update the pool so subsequent candidates in the
            # SAME batch dedupe correctly against their siblings.
            if outcome.succeeded:
                if outcome.action == WriteAction.CREATE and outcome.memory is not None:
                    pool.append(outcome.memory)
                elif outcome.action == WriteAction.SUPERSEDE and outcome.memory is not None:
                    if decision.target_entry is not None:
                        try:
                            pool.remove(decision.target_entry)
                        except ValueError:
                            pass
                    pool.append(outcome.memory)
                elif outcome.action == WriteAction.REACTIVATE and outcome.memory is not None:
                    if decision.target_entry is not None:
                        try:
                            old_idx = pool.index(decision.target_entry)
                            pool[old_idx] = outcome.memory
                        except ValueError:
                            pool.append(outcome.memory)

        logger.info(
            "WritePipeline.save_many: %d candidate(s) in scope %s "
            "-> create=%d, skip=%d, supersede=%d, reactivate=%d, "
            "failed=%d",
            len(candidates),
            scope_ref,
            sum(1 for o in outcomes if o.action == WriteAction.CREATE and o.succeeded),
            sum(1 for o in outcomes if o.action == WriteAction.SKIP and o.succeeded),
            sum(1 for o in outcomes if o.action == WriteAction.SUPERSEDE and o.succeeded),
            sum(1 for o in outcomes if o.action == WriteAction.REACTIVATE and o.succeeded),
            sum(1 for o in outcomes if not o.succeeded),
        )
        return outcomes

    # ------------------------------------------------------------------
    # Internal: enforcement
    # ------------------------------------------------------------------

    def _enforce_can_write(self) -> None:
        if not self._settings.mode.can_write:
            raise MemoryUnavailableError(
                f"Memory writes are disabled "
                f"(mode={self._settings.mode.value}). Set mode to "
                f"'read_write' to enable writes.",
                context={"mode": self._settings.mode.value},
            )

    @staticmethod
    def _verify_scope_matches(
        candidate: MemoryEntry, scope_ref: str
    ) -> None:
        """Ensure ``candidate``'s scope matches ``scope_ref``."""
        expected = f"{candidate.scope_type.value}:{candidate.scope_id}"
        if expected != scope_ref:
            raise MemoryValidationError(
                f"candidate scope ({expected!r}) does not match "
                f"the requested write scope ({scope_ref!r})",
                field_name="scope_ref",
                context={
                    "candidate_scope": expected,
                    "scope_ref": scope_ref,
                },
            )

    async def _fetch_existing_pool(
        self, scope_ref: str
    ) -> List[MemoryEntry]:
        """Fetch the existing-entries pool for the given scope.

        Uses ``status="all"`` (CRITICAL) so ARCHIVED and SUPERSEDED
        entries are visible. Without them, ``REACTIVATE`` could
        never fire and the "user deletes then re-saves" flow
        would hit the UNIQUE constraint instead of reactivating.
        """
        try:
            return await self._port.search(
                query="",
                scope_refs=[scope_ref],
                limit=_DEFAULT_SCAN_LIMIT,
                status="all",
            )
        except MemoryUnavailableError:
            raise
        except Exception as exc:
            raise MemoryUnavailableError(
                f"Failed to fetch existing entries for scope "
                f"{scope_ref!r}: {exc}",
                context={"scope_ref": scope_ref},
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Internal: execute decision
    # ------------------------------------------------------------------

    async def _execute_decision(
        self,
        *,
        decision: WriteDecision,
        candidate: MemoryEntry,
        scope_ref: str,
    ) -> WriteOutcome:
        """Execute one ``WriteDecision`` against the port.

        Exceptions from the port (``MemoryValidationError`` from a
        UNIQUE-constraint race, ``MemoryUnavailableError`` from a
        disk failure) propagate to the caller, which translates
        them into ``WriteOutcome.error``.
        """
        try:
            if decision.action == WriteAction.CREATE:
                created = await self._port.create(candidate)
                return WriteOutcome(
                    action=WriteAction.CREATE,
                    memory=created,
                    reason=decision.reason,
                )

            if decision.action == WriteAction.SKIP:
                target = decision.target_entry
                return WriteOutcome(
                    action=WriteAction.SKIP,
                    memory=target,
                    target_id=target.memory_id if target else None,
                    reason=decision.reason,
                )

            if decision.action == WriteAction.SUPERSEDE:
                target = decision.target_entry
                if target is None:
                    # Defensive -- should never happen.
                    created = await self._port.create(candidate)
                    return WriteOutcome(
                        action=WriteAction.CREATE,
                        memory=created,
                        reason="supersede_without_target_fallback_create",
                    )
                # Archive FIRST, then create.
                await self._port.delete(target.memory_id, scope_ref)
                created = await self._port.create(candidate)
                return WriteOutcome(
                    action=WriteAction.SUPERSEDE,
                    memory=created,
                    target_id=target.memory_id,
                    reason=decision.reason,
                )

            if decision.action == WriteAction.REACTIVATE:
                target = decision.target_entry
                if target is None:
                    created = await self._port.create(candidate)
                    return WriteOutcome(
                        action=WriteAction.CREATE,
                        memory=created,
                        reason="reactivate_without_target_fallback_create",
                    )
                revived = target.model_copy(
                    update={
                        "status": MemoryStatus.ACTIVE,
                        "content": candidate.content,
                        "normalized_content": candidate.normalized_content,
                        "content_hash": candidate.content_hash,
                        "importance": candidate.importance,
                        "tags": candidate.tags,
                        "provenance": candidate.provenance,
                    }
                )
                updated = await self._port.update(revived)
                return WriteOutcome(
                    action=WriteAction.REACTIVATE,
                    memory=updated,
                    target_id=target.memory_id,
                    reason=decision.reason,
                )

            # Unreachable -- all four WriteAction members handled.
            return WriteOutcome(
                action=decision.action,
                reason=f"unknown_action:{decision.action}",
                error=f"unsupported action: {decision.action}",
            )

        except MemoryValidationError as exc:
            logger.warning(
                "WritePipeline: validation failure during %s "
                "(scope=%s): %s",
                decision.action.value,
                scope_ref,
                exc.message,
            )
            return WriteOutcome(
                action=decision.action,
                target_id=(
                    decision.target_entry.memory_id
                    if decision.target_entry
                    else None
                ),
                reason=decision.reason,
                error=f"validation_error: {exc.message}",
            )
        except MemoryUnavailableError as exc:
            logger.error(
                "WritePipeline: backend failure during %s "
                "(scope=%s): %s",
                decision.action.value,
                scope_ref,
                exc.message,
            )
            return WriteOutcome(
                action=decision.action,
                target_id=(
                    decision.target_entry.memory_id
                    if decision.target_entry
                    else None
                ),
                reason=decision.reason,
                error=f"backend_unavailable: {exc.message}",
            )


__all__ = ["MemoryWritePipeline", "WriteOutcome"]
