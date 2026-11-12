# backend/peldrun/memory/domain/usage.py
"""
PELDRUN Memory Platform - Per-Run Usage Telemetry.

Defines ``MemoryUsage``: the immutable snapshot of what a single
run consumed from (and contributed to) the memory subsystem.

Scope of responsibility
-----------------------

``MemoryUsage`` is a PURE VALUE OBJECT. It:

  * is built by ``MemoryUsageTracker.record()`` (P1.9) after
    ``MemoryService.retrieve()`` returns a ``MemoryContext``,
  * is consumed by the usage ledger writer (P1.9) and by the
    monitoring API (``GET /api/memory/usage/*``, P1.8),
  * is NEVER written to disk by itself -- the tracker is the
    sole writer, and the tracker writes ``LLMInvocationRecord``
    rows (or ``memory_usage_ledger`` rows in the dedicated
    backend the plan describes).

What ``MemoryUsage`` is NOT
---------------------------

  * It does NOT count tokens. Token counting is done by the
    ranker + context builder (P1.4) using the model's tokenizer.
    ``MemoryUsage`` merely records their results.
  * It does NOT enforce budgets. The budget is enforced BEFORE
    the tracker is invoked, by ``MemoryContext._check_budget``
    (P1.3/context.py) and by the fitter in P1.4. If a
    ``MemoryUsage`` instance ever found itself over budget, the
    bug is upstream, and the field validator here makes that
    bug immediately visible.
  * It does NOT carry conversation content. No raw memory text,
    no query text. Only COUNTS and identifiers. This is what
    allows the ledger to be indexed and aggregated without
    exposing user data to the monitoring dashboard.

Design invariants
-----------------

1. **Sum consistency is enforced at construction.**

   ``retrieval_tokens + context_tokens + overhead_tokens``
   MUST equal ``total_tokens``. Enforced by
   ``_check_token_sum``.

   ``user_tokens + project_tokens + chat_tokens + agent_tokens``
   MUST equal ``context_tokens``. Enforced by
   ``_check_scope_sum``.

   ``entries_used + entries_dropped`` MUST equal
   ``entries_retrieved``. Enforced by ``_check_entry_counts``.

   ``context_tokens <= budget_tokens``. Enforced by
   ``_check_budget_respected``.

   These four invariants are what make the monitoring dashboard
   trustworthy. Without them, a bug in one layer could be masked
   by a bug in another -- e.g. a token-accounting error in the
   ranker could silently disappear inside an aggregate that
   re-derives the total from its components. Enforcing the
   invariants at construction time surfaces the error at the
   FIRST point where the numbers stop adding up.

2. **Three token buckets, not one.**

   ``retrieval_tokens``, ``context_tokens``, and
   ``overhead_tokens`` are recorded SEPARATELY even though the
   dashboard shows their sum. The reason is diagnostic:

     * ``retrieval_tokens`` is what the FTS+ranker spent
       estimating relevance (cheap, but nonzero at scale).
     * ``context_tokens`` is what actually reached the model's
       prompt.
     * ``overhead_tokens`` is the cost of formatting (the
       ``[RETRIEVED MEMORY]`` header, per-entry labels, blank
       lines).

   If the dashboard shows "memory is using 40% of the budget"
   and the operator wants to reduce it, the breakdown tells
   them WHAT to reduce. A single ``total_tokens`` number does
   not.

3. **Per-scope breakdown is required.**

   ``user_tokens``, ``project_tokens``, ``chat_tokens``,
   ``agent_tokens`` let the ledger answer "how much of the
   budget is going to user preferences vs project facts?".
   The API's ``GET /api/memory/usage/breakdown?by=scope``
   (P1.8) reads these directly. Missing any of them would
   collapse the dashboard's most useful diagnostic.

4. **No float fields.**

   Every numeric field is an ``int``. Token counts are never
   fractional; storing them as floats invites rounding drift
   across saves and loads. The plan's ledger uses integer
   columns throughout; matching that here avoids silent
   conversion surprises at the boundary.

5. **Immutable snapshot, not a stream.**

   ``MemoryUsage`` represents ONE run's usage. Multiple runs
   require multiple instances. There is no aggregation logic
   in this class -- aggregation is the ledger's job (P1.9),
   which sums across rows using simple SQL. Keeping
   aggregation out of the domain object ensures that the
   ledger (the durable source of truth) and the domain model
   (the in-flight representation) can never disagree about
   what "sum" means.

6. **A ``run_id`` is mandatory; everything else is not.**

   ``run_id`` links the usage record to the run that produced
   it. Without it, the record is unattributable and useless
   for diagnosis. Everything else (chat_id, project_id,
   scope counts) can legitimately be missing in early
   integration tests, so they have safe defaults.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from peldrun.memory.domain.context import MemoryContext


# ---------------------------------------------------------------------------
# MemoryUsage
# ---------------------------------------------------------------------------


class MemoryUsage(BaseModel):
    """Per-run memory subsystem usage snapshot.

    Built by ``MemoryUsageTracker.record()`` (P1.9) from a
    ``MemoryContext`` plus the tracker's own overhead measurement.
    All counts are non-negative integers. All sum invariants are
    enforced at construction.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    # ------------------------- attribution ---------------------------------

    run_id: str = Field(
        ...,
        description=(
            "Identifier of the run this usage belongs to. Mandatory: "
            "without it the record cannot be linked to any other "
            "observability data (events, checkpoints, LLM "
            "invocations). Trimmed and required non-empty by the "
            "validator."
        ),
    )
    chat_id: Optional[str] = Field(
        default=None,
        description=(
            "Chat session id, when the run originated from a chat. "
            "Absent for background jobs or scheduled runs."
        ),
    )
    project_id: Optional[str] = Field(
        default=None,
        description=(
            "Project id, when the run belongs to a project. Absent "
            "for runs that only read user-scope memories (rare, "
            "e.g. a global preference lookup)."
        ),
    )

    # ------------------------- token buckets ------------------------------

    retrieval_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Tokens consumed by the retrieval pipeline itself, "
            "typically zero. Non-zero only when a retrieval "
            "backend counts its own tokenization (some providers "
            "do, e.g. a pgvector backend with a tokenizer-based "
            "BM25 hybrid). Included so future backends do not "
            "need a schema change."
        ),
    )
    context_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Tokens actually injected into the model's prompt for "
            "the retrieved memories. Equal to the sum of the four "
            "per-scope buckets below (``user_tokens``, "
            "``project_tokens``, ``chat_tokens``, ``agent_tokens``)."
        ),
    )
    overhead_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Fixed formatting cost of the memory block: the "
            "``[RETRIEVED MEMORY]`` header, the per-entry scope/"
            "type labels, separators, and trailing newline. "
            "Excluded from ``context_tokens`` so the per-scope "
            "breakdown reflects actual memory content, not "
            "formatting."
        ),
    )

    # ------------------------- per-scope breakdown ------------------------

    user_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Portion of ``context_tokens`` attributable to "
            "USER-scope entries. Comparison with "
            "``MemorySettings.token_budget.reserved_for_user_scope`` "
            "shows whether the reservation was actually consumed."
        ),
    )
    project_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Portion of ``context_tokens`` attributable to "
            "PROJECT-scope entries."
        ),
    )
    chat_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Portion of ``context_tokens`` attributable to "
            "CHAT-scope entries."
        ),
    )
    agent_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Portion of ``context_tokens`` attributable to "
            "AGENT-scope entries. Reserved for P2; kept here so "
            "the ledger schema does not need a migration when "
            "AGENT-scope retrieval lands."
        ),
    )

    # ------------------------- budget --------------------------------------

    budget_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "The ``MemoryTokenBudget.max_tokens`` in effect for "
            "this run. Recorded alongside the actual usage so "
            "the ledger can report utilization "
            "(``total_tokens / budget_tokens``) without joining "
            "against a settings snapshot. Zero is legal only "
            "when no budget was applied (e.g. a NO_NEED route "
            "that never invoked the builder); in every other "
            "case it matches the effective settings."
        ),
    )

    # ------------------------- entry counts --------------------------------

    entries_retrieved: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of entries the ranker returned BEFORE budget "
            "fitting. This is the DENOMINATOR for the drop rate "
            "the monitoring dashboard reports."
        ),
    )
    entries_used: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of entries that made it into the final "
            "``MemoryContext.memories`` list. These are the "
            "entries whose content contributed to "
            "``context_tokens``."
        ),
    )
    entries_dropped: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of entries dropped by the budget fitter "
            "(mirrors ``MemoryContext.dropped_for_budget``). "
            "``entries_used + entries_dropped`` MUST equal "
            "``entries_retrieved``."
        ),
    )

    # ------------------------- validators ----------------------------------

    @model_validator(mode="after")
    def _check_token_sum(self) -> "MemoryUsage":
        """Enforce ``retrieval + context + overhead == total_tokens``.

        The ``total_tokens`` field is intentionally ABSENT from the
        model: it is a DERIVED value, exposed as the
        ``total_tokens`` property below. Storing it as a field
        would create a fourth number that could disagree with the
        other three; deriving it at read time is the only way to
        make the sum invariant structurally impossible to violate.

        This validator is therefore a sanity check on the three
        stored values, not a cross-check against a fourth. Its
        purpose is to catch NEGATIVE token counts (already
        prevented by ``ge=0``) and, more importantly, to be the
        place future contributors add new invariants (e.g. "an
        empty context must have ``context_tokens == 0``").

        Empty-context invariant
        -----------------------
        When ``entries_used == 0``, ``context_tokens`` MUST be
        zero. Non-zero context tokens with zero used entries would
        mean the format overhead (``overhead_tokens``) or the
        retrieval cost was recorded for a run that injected
        nothing -- a bookkeeping bug that would inflate the
        dashboard's memory usage.
        """
        if self.entries_used == 0 and self.context_tokens != 0:
            raise ValueError(
                f"inconsistent usage: entries_used=0 but "
                f"context_tokens={self.context_tokens}; a run that "
                f"injected zero entries cannot have consumed "
                f"context tokens"
            )
        return self

    @model_validator(mode="after")
    def _check_scope_sum(self) -> "MemoryUsage":
        """Enforce per-scope breakdown sums to ``context_tokens``.

        If the four scope buckets do not sum to ``context_tokens``,
        either the tracker missed a scope (likely: it forgot
        CHAT-scope when the run had one) or the builder double-
        counted content. Both are bugs that would make
        ``GET /api/memory/usage/breakdown?by=scope`` lie to the
        operator. Failing construction surfaces the bug at the
        first inconsistent record.
        """
        scope_sum = (
            self.user_tokens
            + self.project_tokens
            + self.chat_tokens
            + self.agent_tokens
        )
        if scope_sum != self.context_tokens:
            raise ValueError(
                f"scope breakdown does not sum to context_tokens: "
                f"user={self.user_tokens} + project={self.project_tokens} "
                f"+ chat={self.chat_tokens} + agent={self.agent_tokens} "
                f"= {scope_sum}, expected context_tokens="
                f"{self.context_tokens}"
            )
        return self

    @model_validator(mode="after")
    def _check_entry_counts(self) -> "MemoryUsage":
        """Enforce ``entries_used + entries_dropped == entries_retrieved``.

        The entry-count invariant mirrors the token-count invariant
        at the entry level: the ranker produced N candidates, the
        fitter kept some and dropped the rest. If the two sides
        do not match, either a candidate vanished between the
        ranker and the fitter (a real bug) or the drop counter was
        incorrectly incremented (a bookkeeping bug). Both make
        the monitoring dashboard's "drop rate" untrustworthy.
        """
        if self.entries_used + self.entries_dropped != self.entries_retrieved:
            raise ValueError(
                f"entry count mismatch: entries_used={self.entries_used} "
                f"+ entries_dropped={self.entries_dropped} = "
                f"{self.entries_used + self.entries_dropped}, "
                f"expected entries_retrieved="
                f"{self.entries_retrieved}"
            )
        return self

    @model_validator(mode="after")
    def _check_budget_respected(self) -> "MemoryUsage":
        """Enforce ``context_tokens + overhead_tokens <= budget_tokens``.

        Uses ``context_tokens + overhead_tokens`` (not
        ``context_tokens`` alone) because the OVERHEAD is part of
        what the memory block actually consumes from the model's
        context window. A run that used 900 context tokens + 200
        overhead tokens against a 1000-token budget is over budget
        by 100 tokens, regardless of the fact that
        ``context_tokens`` alone was under 1000.

        The comparison is skipped when ``budget_tokens == 0``, which
        is the sentinel for "no budget was applied" (fast-path
        NO_NEED routes).
        """
        if self.budget_tokens <= 0:
            return self
        consumed = self.context_tokens + self.overhead_tokens
        if consumed > self.budget_tokens:
            raise ValueError(
                f"memory budget violated: context_tokens="
                f"{self.context_tokens} + overhead_tokens="
                f"{self.overhead_tokens} = {consumed} > "
                f"budget_tokens={self.budget_tokens}. This is a "
                f"bug in the budget fitter (P1.4 must drop entries "
                f"to stay within budget) or in the overhead "
                f"calculator (which must include the header and "
                f"labels in its estimate)."
            )
        return self

    # ------------------------- derived properties --------------------------

    @property
    def total_tokens(self) -> int:
        """Sum of ``retrieval + context + overhead`` tokens.

        This is the number the monitoring dashboard attributes to
        memory in the "how much of the model's prompt was memory?"
        calculation. It is derived, never stored.
        """
        return (
            self.retrieval_tokens
            + self.context_tokens
            + self.overhead_tokens
        )

    @property
    def budget_utilization(self) -> float:
        """Fraction of ``budget_tokens`` actually consumed.

        Returns a float in ``[0.0, 1.0]`` (the
        ``_check_budget_respected`` validator guarantees the upper
        bound when ``budget_tokens > 0``).

        Returns ``0.0`` when ``budget_tokens == 0`` (no budget was
        applied). This matches the ``MemoryContext.utilization``
        behavior and keeps the API free of special cases.
        """
        if self.budget_tokens <= 0:
            return 0.0
        return (self.context_tokens + self.overhead_tokens) / float(
            self.budget_tokens
        )

    @property
    def drop_rate(self) -> float:
        """Fraction of retrieved entries dropped by the budget fitter.

        Returns a float in ``[0.0, 1.0]``. Returns ``0.0`` when
        ``entries_retrieved == 0`` (nothing was retrieved, so
        nothing could be dropped).

        This metric drives the "should we increase the memory
        budget?" recommendation on the monitoring dashboard: a
        sustained drop rate above ~30% typically indicates the
        budget is too small for the deployment's actual usage.
        """
        if self.entries_retrieved <= 0:
            return 0.0
        return self.entries_dropped / float(self.entries_retrieved)

    @property
    def is_empty(self) -> bool:
        """True when the run consumed no memory at all.

        The fast path: DISABLED mode, NO_NEED route, or a retrieval
        that returned zero entries. Used by the ledger writer to
        skip no-op records (some deployments do not want to
        record zero-value runs) and by the API to filter "empty"
        runs from per-turn breakdowns.
        """
        return self.total_tokens == 0 and self.entries_used == 0

    # ------------------------- constructor helpers -------------------------

    @classmethod
    def from_context(
        cls,
        *,
        run_id: str,
        context: MemoryContext,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        retrieval_tokens: int = 0,
        overhead_tokens: int = 0,
    ) -> "MemoryUsage":
        """Build a ``MemoryUsage`` from a ``MemoryContext``.

        The ``context`` argument carries the CORE data:
        ``used_tokens`` (→ ``context_tokens``),
        ``token_budget`` (→ ``budget_tokens``),
        ``len(memories)`` (→ ``entries_used``),
        ``dropped_for_budget`` (→ ``entries_dropped``),
        and the per-scope token totals (which the tracker computes
        from the entries' ``scope_type``).

        The two numeric arguments (``retrieval_tokens``,
        ``overhead_tokens``) are supplied by the tracker because
        they are NOT stored on ``MemoryContext``:

          * ``retrieval_tokens`` is a property of the retrieval
            BACKEND (some count, some do not), not of the resulting
            context. The tracker queries the backend.
          * ``overhead_tokens`` is a property of the FORMATTER
            (P1.5's ``_format_memory_block``), not of the context.
            The tracker asks the formatter or measures the delta
            between content and formatted block.

        Per-scope token computation
        ---------------------------
        The per-scope buckets are computed by the tracker, not by
        this helper, because splitting individual entries' tokens
        across scopes requires re-tokenizing each entry's content.
        The tracker has the tokenizer available; this domain module
        intentionally does not import it.

        This helper therefore accepts the per-scope breakdown
        INDIRECTLY: the caller must pass it through the normal
        ``MemoryUsage(...)`` constructor after computing it. The
        convenience here focuses on the fields derivable from
        ``context`` alone, and leaves ``user_tokens`` and friends
        at 0 -- which the ``_check_scope_sum`` validator would
        REJECT unless ``context_tokens`` is also 0. That is
        deliberate: this helper is the fast-path constructor for
        the "no per-scope breakdown available" case (early
        integration tests, or a NO_NEED run with empty context).
        Production code paths go through ``MemoryUsageTracker``
        which supplies the full breakdown.
        """
        if not isinstance(context, MemoryContext):
            raise TypeError(
                f"context must be a MemoryContext, got "
                f"{type(context).__name__}"
            )

        # Fast path: fully-empty context. Everything is zero.
        # This is the ONLY case the helper can construct without
        # the per-scope breakdown (see the docstring above).
        if context.is_empty:
            return cls(
                run_id=run_id,
                chat_id=chat_id,
                project_id=project_id,
                retrieval_tokens=max(0, int(retrieval_tokens)),
                context_tokens=0,
                overhead_tokens=max(0, int(overhead_tokens)),
                user_tokens=0,
                project_tokens=0,
                chat_tokens=0,
                agent_tokens=0,
                budget_tokens=max(0, int(context.token_budget)),
                entries_retrieved=context.dropped_for_budget,
                entries_used=0,
                entries_dropped=context.dropped_for_budget,
            )

        # Non-empty context: the caller must supply the per-scope
        # breakdown. We raise a descriptive error rather than
        # silently constructing a context_tokens>0 / user_tokens=0
        # instance that the validator would reject anyway.
        raise ValueError(
            "MemoryUsage.from_context() cannot construct a non-empty "
            "usage without the per-scope token breakdown "
            "(user_tokens / project_tokens / chat_tokens / "
            "agent_tokens). Use MemoryUsageTracker.record() (P1.9) "
            "which supplies the full breakdown, or construct the "
            "MemoryUsage directly with all scope buckets populated."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["MemoryUsage"]
