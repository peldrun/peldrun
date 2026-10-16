# backend/integrations/source_control/ports/checks.py
"""
CI check ports and value objects for the Source Control Integration
subsystem.

This module re-exports `ChecksPort` from `ports.provider` and adds
the value objects and helpers used by the application layer to query
and interpret CI check status:

    - `CheckListOptions` : filters for listing checks.
    - `CheckFilter`      : normalized filter values.
    - `CheckRunStatus`   : combined status+conclusion enum.
    - `summarize_checks()`      : aggregate a list of checks.
    - `checks_passed()`         : quick pass/fail check.
    - `wait_for_checks()`       : poll until completion or timeout.
    - `format_check_summary()`  : produce a short human-readable
                                  summary for agent tool results.

The provider-neutral `CheckRunRef` and `CheckSummary` are defined in
`domain.models` and are the only objects returned by provider
adapters.

Design rules:
    - A "passing" summary requires at least one check run. An empty
      list of checks is NEVER interpreted as success.
    - Adapters MUST raise `UnsupportedCapabilityError` when the
      provider has no checks API, rather than returning an empty list.
      The capability declaration prevents the call from reaching the
      adapter in normal flow.
    - Waiting for checks MUST be cancellable and MUST respect a
      timeout. PELDRUN never blocks indefinitely on a CI provider.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

from ..domain.enums import CheckConclusion, CheckStatus
from ..domain.models import CheckRunRef, CheckSummary, RepositoryRef
from .provider import ChecksPort  # re-exported

__all__ = [
    # Re-exports
    "ChecksPort",
    "CheckRunRef",
    "CheckSummary",
    "CheckStatus",
    "CheckConclusion",
    # Enums
    "CheckFilter",
    "CheckRunStatus",
    # Value objects
    "CheckListOptions",
    # Helpers
    "summarize_checks",
    "checks_passed",
    "wait_for_checks",
    "format_check_summary",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CheckFilter(str, Enum):
    """
    Normalized filter for listing checks.

    Adapters translate these to provider-specific query parameters.
    Unsupported filters MUST be treated as `ALL` rather than raising,
    so that listings remain available across providers.
    """

    ALL = "all"
    FAILED = "failed"
    PENDING = "pending"
    SUCCEEDED = "succeeded"


class CheckRunStatus(str, Enum):
    """
    Combined status and conclusion for a single check run.

    This enum collapses `CheckStatus` and `CheckConclusion` into a
    single decision-oriented value, which is what the UI and the
    application service actually need.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckListOptions:
    """
    Filters for listing checks.

    Filters are advisory: adapters MAY ignore filters they cannot
    express and MUST NOT raise when a filter is unsupported.
    """

    filter: CheckFilter = CheckFilter.ALL
    name: Optional[str] = None
    page: int = 1
    page_size: int = 100

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe mapping (no secrets)."""
        return {
            "filter": self.filter.value,
            "name": self.name,
            "page": self.page,
            "page_size": self.page_size,
        }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _run_status(run: CheckRunRef) -> CheckRunStatus:
    """
    Map a `CheckRunRef` to a single `CheckRunStatus` value.

    The mapping prioritizes the conclusion when present. When only the
    status is available, `queued` and `in_progress` map to `PENDING`.
    """
    if run.status in (CheckStatus.QUEUED, CheckStatus.IN_PROGRESS):
        return CheckRunStatus.PENDING
    if run.status == CheckStatus.UNKNOWN and run.conclusion == CheckConclusion.UNKNOWN:
        return CheckRunStatus.UNKNOWN

    mapping = {
        CheckConclusion.SUCCESS: CheckRunStatus.SUCCESS,
        CheckConclusion.FAILURE: CheckRunStatus.FAILURE,
        CheckConclusion.CANCELLED: CheckRunStatus.CANCELLED,
        CheckConclusion.SKIPPED: CheckRunStatus.SKIPPED,
        CheckConclusion.TIMED_OUT: CheckRunStatus.FAILURE,
        CheckConclusion.ACTION_REQUIRED: CheckRunStatus.FAILURE,
        CheckConclusion.STALE: CheckRunStatus.UNKNOWN,
        CheckConclusion.NEUTRAL: CheckRunStatus.SKIPPED,
        CheckConclusion.UNKNOWN: CheckRunStatus.UNKNOWN,
    }
    return mapping.get(run.conclusion, CheckRunStatus.UNKNOWN)


def summarize_checks(runs: Sequence[CheckRunRef]) -> CheckSummary:
    """
    Aggregate a sequence of `CheckRunRef` into a `CheckSummary`.

    The summary counts each run exactly once, based on its combined
    status and conclusion. `total` reflects the length of the input.
    """
    total = len(runs)
    succeeded = failed = pending = skipped = 0

    for run in runs:
        status = _run_status(run)
        if status == CheckRunStatus.SUCCESS:
            succeeded += 1
        elif status == CheckRunStatus.FAILURE:
            failed += 1
        elif status == CheckRunStatus.PENDING:
            pending += 1
        elif status == CheckRunStatus.SKIPPED:
            skipped += 1
        # UNKNOWN and CANCELLED do not fall into any bucket; they
        # remain uncounted, which keeps `all_passed()` conservative.

    return CheckSummary(
        total=total,
        succeeded=succeeded,
        failed=failed,
        pending=pending,
        skipped=skipped,
        runs=tuple(runs),
    )


def checks_passed(summary: CheckSummary) -> bool:
    """
    Return True only if the summary indicates a successful run.

    A summary is successful when there is at least one check, no
    failures, and no pending checks. Skipped and neutral checks do not
    block success. Unknown and cancelled checks are conservatively
    treated as blocking (returns False).
    """
    if summary.total == 0:
        return False
    if summary.failed > 0:
        return False
    if summary.pending > 0:
        return False
    # If total == succeeded + skipped, everything is accounted for.
    accounted = summary.succeeded + summary.skipped
    return accounted == summary.total


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


async def wait_for_checks(
    adapter: ChecksPort,
    repo: RepositoryRef,
    ref: str,
    *,
    token: Optional[str] = None,
    timeout_seconds: int = 600,
    poll_interval_seconds: int = 15,
    options: Optional[CheckListOptions] = None,
) -> CheckSummary:
    """
    Poll the provider until checks are complete or `timeout_seconds`
    elapses.

    The function never raises on timeout; it returns the last observed
    summary so the caller can decide whether to proceed. Cancellation
    is supported via `asyncio.CancelledError`, which propagates
    normally.

    A `poll_interval_seconds` of 0 performs a single check and returns
    immediately.
    """
    import time

    deadline = time.monotonic() + max(0, timeout_seconds)
    last_summary: Optional[CheckSummary] = None

    while True:
        runs = await adapter.list_checks(repo, ref, token=token)
        last_summary = summarize_checks(runs)

        # Stop when there is nothing pending.
        if last_summary.pending == 0:
            return last_summary

        if poll_interval_seconds <= 0:
            return last_summary

        if time.monotonic() >= deadline:
            return last_summary

        await asyncio.sleep(min(poll_interval_seconds, 5))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_check_summary(summary: CheckSummary) -> str:
    """
    Return a compact, human-readable summary of `summary`.

    The output is intended for agent tool results and log messages.
    It never includes credentials or unbounded provider output.
    """
    if summary.total == 0:
        return "No checks reported by the provider."

    parts = [
        f"{summary.total} check(s)",
        f"{summary.succeeded} passed",
    ]
    if summary.failed:
        parts.append(f"{summary.failed} failed")
    if summary.pending:
        parts.append(f"{summary.pending} pending")
    if summary.skipped:
        parts.append(f"{summary.skipped} skipped")

    verdict = "PASS" if checks_passed(summary) else "NOT PASSING"
    return f"[{verdict}] " + ", ".join(parts) + "."
