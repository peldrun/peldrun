# backend/peldrun/llm/accounting.py
"""
PELDRUN Unified LLM Invocation Accounting.

Single source of truth for recording every LLM invocation — success,
failure, or cancellation — into the canonical ``token_ledger`` store.

The tracker is called from BOTH dispatch paths (agent mode and direct
chat) so accounting is consistent regardless of which path produced the
invocation.

Design principles
-----------------

1. **Every invocation produces a record.**
   Pre-M8, direct-chat skipped recording when ``response.usage`` was
   None — the exact failure the plan warns against (Section 9.2:
   "لا نحول Unknown إلى Zero"). This tracker records every call, with
   ``usage_source = "unknown"`` when the provider returned no usage.

2. **Cost status is explicit.**
   ``cost_nano_usd = 0`` is ambiguous: "free" vs "unknown pricing".
   The tracker stores an explicit ``cost_status`` inside ``metadata``
   (``known`` / ``free`` / ``unknown`` / ``estimated``) so downstream
   aggregations do not conflate the two.

3. **Failures and cancellations are records.**
   A failed or cancelled LLM call still consumed provider resources
   (prompt tokens sent, partial output billed). Recording them enables
   accurate accounting and honest diagnostics.

4. **Idempotency via invocation_id.**
   Every record carries a unique ``invocation_id``. The usage store
   enforces ``INSERT OR IGNORE`` on this key, so re-recording a
   response (e.g. after a fallback retry) is safe.

5. **No heuristics.**
   Cost status is inferred from the ``pricing_version_id`` returned by
   the pricing store, which is the only component that knows the rules.
   No model-name inspection anywhere in this module.

See also
--------

    * ``backend/peldrun/runtime/usage_store.py``  — the ledger
    * ``backend/peldrun/runtime/pricing_store.py`` — cost calc
    * ``backend/peldrun/llm/client.py``           — LLMResponse, TokenUsage
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from peldrun.llm.client import LLMResponse, TokenUsage, UsageSource
from peldrun.runtime.pricing_store import get_pricing_store
from peldrun.runtime.usage_store import LLMInvocationRecord, get_usage_store

logger = logging.getLogger("peldrun.llm.accounting")


# ---------------------------------------------------------------------------
# Cost status constants
# ---------------------------------------------------------------------------


#: Provider rate known and applied. ``cost_nano_usd`` is authoritative.
COST_STATUS_KNOWN = "known"

#: Local / self-hosted endpoint with zero-billing basis. ``cost_nano_usd`` is 0.
COST_STATUS_FREE = "free"

#: No pricing rule matched. ``cost_nano_usd = 0`` does NOT mean free.
COST_STATUS_UNKNOWN = "unknown"

#: Cost derived from locally estimated usage (via tokenizer fallback).
COST_STATUS_ESTIMATED = "estimated"


# ---------------------------------------------------------------------------
# Invocation context
# ---------------------------------------------------------------------------


@dataclass
class InvocationContext:
    """
    Metadata that identifies an LLM invocation's context.

    Populated by the caller (dispatch, executor) so the tracker can
    build a complete ``LLMInvocationRecord``. Every field has a
    reasonable default, so partial contexts work.

    Callers MUST supply at least ``provider``, ``model_requested``,
    and ``mode``.
    """

    provider: str
    model_requested: str
    mode: str = "chat"          # "chat" or "agent"

    job_id: Optional[str] = None
    chat_id: Optional[str] = None
    project_id: Optional[str] = "default_project"
    run_id: Optional[str] = None
    turn_id: Optional[str] = None
    step_id: Optional[str] = None

    base_url: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    #: Provider-side request ID, if the SDK exposes one.
    request_id: Optional[str] = None
    #: Provider-side response ID, if the SDK exposes one.
    response_id: Optional[str] = None

    #: Number of retry attempts (1 = first try).
    attempt: int = 1

    #: Extra metadata merged into ``metadata_json`` on the record.
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cost status inference
# ---------------------------------------------------------------------------


def _infer_cost_status(
    pricing_version_id: Optional[str],
    cost_nano_usd: int,
    estimated: bool,
) -> str:
    """
    Map a pricing rule ID + computed cost + estimation flag into a
    cost status.

    The rule-ID prefixes are documented in ``pricing_store.py``:

        ``local_zero_*``        → free
        ``prc_unpriced_fallback`` → unknown
        ``prc_*`` (any other)   → known
        (empty)                 → unknown

    When ``estimated=True`` and the rule matched, we still report
    ``known`` if the pricing rule was authoritative — the estimated
    part is only the token count, not the per-token rate.
    """
    if not pricing_version_id:
        return COST_STATUS_UNKNOWN

    pid = pricing_version_id
    if pid.startswith("local_zero_"):
        return COST_STATUS_FREE
    if pid == "prc_unpriced_fallback":
        return COST_STATUS_UNKNOWN
    if pid.startswith("prc_"):
        return COST_STATUS_KNOWN

    return COST_STATUS_UNKNOWN


def _normalize_usage_source(usage: Optional[TokenUsage]) -> str:
    """Return the canonical ``usage_source`` string for a TokenUsage."""
    if usage is None:
        return UsageSource.UNKNOWN.value
    src = getattr(usage, "source", None)
    if src is None:
        return UsageSource.UNKNOWN.value
    if hasattr(src, "value"):
        return str(src.value)
    return str(src)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class LLMInvocationTracker:
    """
    Shared accounting service for both dispatch paths.

    Stateless across invocations. Every method writes through the
    module-level ``get_usage_store()`` and ``get_pricing_store()``
    singletons — the tracker itself holds no mutable state.

    Public API
    ----------
    complete(response, context, *, started_at=..., invocation_id=...)
        Record a successful invocation. Extracts usage from
        ``response.usage``, calls the pricing store, and writes the
        final record.

    fail(error, context, *, started_at=..., invocation_id=...)
        Record a failed invocation. Called from ``except`` blocks.
        No usage is available; the record uses
        ``usage_source="unknown"`` and ``cost_status="unknown"``.

    cancel(context, *, started_at=..., invocation_id=...)
        Record a cancelled invocation (user pressed Stop). Same shape
        as ``fail`` but with ``status="cancelled"``.
    """

    async def complete(
        self,
        response: LLMResponse,
        context: InvocationContext,
        *,
        started_at: Optional[float] = None,
        invocation_id: Optional[str] = None,
    ) -> LLMInvocationRecord:
        """
        Record a successful invocation.

        Returns the recorded ``LLMInvocationRecord`` so callers can
        reuse it for local accounting (``turn_usage``, etc.) without
        re-querying the ledger.
        """
        started_at = started_at or time.time()
        completed_at = time.time()

        usage = response.usage
        usage_source = _normalize_usage_source(usage)
        estimated = bool(getattr(usage, "estimated", False)) if usage else False

        # Pricing — safe-by-default. If calculation fails, we record
        # cost_status="unknown" and DO NOT lie about a zero cost.
        cost_nano_usd = 0
        pricing_version_id: Optional[str] = None
        if usage is not None:
            try:
                pricing_store = get_pricing_store()
                cost_nano_usd, pricing_version_id = await pricing_store.calculate_cost(
                    usage=usage,
                    provider=context.provider,
                    model=context.model_requested,
                    base_url=context.base_url,
                )
            except Exception as exc:
                logger.warning(
                    "Pricing calculation failed for %s::%s: %s — "
                    "recording cost as unknown.",
                    context.provider,
                    context.model_requested,
                    exc,
                )

        cost_status = _infer_cost_status(
            pricing_version_id, cost_nano_usd, estimated
        )

        inv_id = invocation_id or f"inv_{uuid.uuid4().hex[:12]}"
        record = LLMInvocationRecord(
            invocation_id=inv_id,
            job_id=context.job_id,
            chat_id=context.chat_id,
            project_id=context.project_id,
            turn_id=context.turn_id,
            step_id=context.step_id,
            run_id=context.run_id,
            mode=context.mode,
            provider=context.provider,
            model_requested=context.model_requested,
            model_returned=response.model or context.model_requested,
            request_id=context.request_id,
            response_id=context.response_id,
            attempt=context.attempt,
            status="completed",
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            cached_input_tokens=usage.cached_input_tokens if usage else None,
            reasoning_output_tokens=usage.reasoning_output_tokens if usage else None,
            usage_source=usage_source,
            estimated=estimated,
            tokenizer_id=getattr(usage, "tokenizer_id", None) if usage else None,
            tokenizer_version=getattr(usage, "tokenizer_version", None) if usage else None,
            estimation_method=getattr(usage, "estimation_method", None) if usage else None,
            started_at=started_at,
            first_token_at=None,
            completed_at=completed_at,
            latency_ms=(
                response.latency_ms
                or round((completed_at - started_at) * 1000.0, 2)
            ),
            ttft_ms=response.ttft_ms,
            finish_reason=response.finish_reason,
            failure_reason=None,
            pricing_version_id=pricing_version_id,
            cost_nano_usd=cost_nano_usd,
            metadata={**context.metadata, "cost_status": cost_status},
        )

        await self._record(record)
        return record

    # ------------------------------------------------------------------

    async def fail(
        self,
        error: BaseException,
        context: InvocationContext,
        *,
        started_at: Optional[float] = None,
        invocation_id: Optional[str] = None,
    ) -> LLMInvocationRecord:
        """
        Record a failed invocation.

        The record uses ``usage_source="unknown"`` and
        ``cost_status="unknown"`` because no usage data was captured.
        ``failure_reason`` carries the error message for diagnostics.
        """
        started_at = started_at or time.time()
        completed_at = time.time()

        inv_id = invocation_id or f"inv_{uuid.uuid4().hex[:12]}"
        record = LLMInvocationRecord(
            invocation_id=inv_id,
            job_id=context.job_id,
            chat_id=context.chat_id,
            project_id=context.project_id,
            turn_id=context.turn_id,
            step_id=context.step_id,
            run_id=context.run_id,
            mode=context.mode,
            provider=context.provider,
            model_requested=context.model_requested,
            model_returned=None,
            request_id=context.request_id,
            response_id=context.response_id,
            attempt=context.attempt,
            status="failed",
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cached_input_tokens=None,
            reasoning_output_tokens=None,
            usage_source=UsageSource.UNKNOWN.value,
            estimated=False,
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=round((completed_at - started_at) * 1000.0, 2),
            ttft_ms=None,
            finish_reason=None,
            failure_reason=str(error)[:1000] if error else None,
            pricing_version_id=None,
            cost_nano_usd=0,
            metadata={**context.metadata, "cost_status": COST_STATUS_UNKNOWN},
        )

        await self._record(record)
        return record

    # ------------------------------------------------------------------

    async def cancel(
        self,
        context: InvocationContext,
        *,
        started_at: Optional[float] = None,
        invocation_id: Optional[str] = None,
    ) -> LLMInvocationRecord:
        """
        Record a cancelled invocation.

        Called from ``asyncio.CancelledError`` handlers. Same shape as
        ``fail`` but with ``status="cancelled"`` and a stable reason
        string so aggregations can separate it from hard failures.
        """
        started_at = started_at or time.time()
        completed_at = time.time()

        inv_id = invocation_id or f"inv_{uuid.uuid4().hex[:12]}"
        record = LLMInvocationRecord(
            invocation_id=inv_id,
            job_id=context.job_id,
            chat_id=context.chat_id,
            project_id=context.project_id,
            turn_id=context.turn_id,
            step_id=context.step_id,
            run_id=context.run_id,
            mode=context.mode,
            provider=context.provider,
            model_requested=context.model_requested,
            model_returned=None,
            request_id=context.request_id,
            response_id=context.response_id,
            attempt=context.attempt,
            status="cancelled",
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cached_input_tokens=None,
            reasoning_output_tokens=None,
            usage_source=UsageSource.UNKNOWN.value,
            estimated=False,
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=round((completed_at - started_at) * 1000.0, 2),
            ttft_ms=None,
            finish_reason=None,
            failure_reason="cancelled_by_operator",
            pricing_version_id=None,
            cost_nano_usd=0,
            metadata={**context.metadata, "cost_status": COST_STATUS_UNKNOWN},
        )

        await self._record(record)
        return record

    # ------------------------------------------------------------------

    @staticmethod
    async def _record(record: LLMInvocationRecord) -> None:
        """
        Write the record to the ledger.

        Errors are logged but never raised — accounting must never
        break a run under any circumstance.
        """
        try:
            usage_store = get_usage_store()
            await usage_store.record_invocation(record)
        except Exception as exc:
            logger.error(
                "Failed to record LLM invocation %s: %s",
                record.invocation_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


#: Shared tracker. Stateless, so a single instance is safe across runs.
default_tracker = LLMInvocationTracker()


__all__ = [
    "COST_STATUS_KNOWN",
    "COST_STATUS_FREE",
    "COST_STATUS_UNKNOWN",
    "COST_STATUS_ESTIMATED",
    "InvocationContext",
    "LLMInvocationTracker",
    "default_tracker",
]
