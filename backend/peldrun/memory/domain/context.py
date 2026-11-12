# backend/peldrun/memory/domain/context.py
"""
PELDRUN Memory Platform - Transient Retrieval Context.

Defines ``MemoryContext``: the ONE-SHOT, IN-MEMORY container that
carries a set of retrieved ``MemoryEntry`` objects from
``MemoryService.retrieve()`` (P1.4/P1.7) to
``ContextManager.build_messages()`` (P1.5).

MemoryContext is EXPLICITLY TRANSIENT
-------------------------------------

It is:

  * **never persisted** -- not written to SQLite, not in any
    checkpoint, not in ``RunRequest``'s serialized form (it travels
    inside the live Python object only, as ``request.memory_context``).
  * **never restored** -- a resumed run (via ``RecoveryManager``)
    rebuilds a fresh context from a fresh retrieval; there is no
    "resume from old context" path.
  * **built once per turn** -- the runtime calls
    ``MemoryService.retrieve()`` exactly once at the start of each
    user turn and reuses the resulting context for the entire turn
    (both chat and agent modes). Rebuilding per agent step would
    double the FTS query cost for every step without any benefit --
    the memories relevant to the user's question do not change
    mid-execution.

Why a dedicated class instead of "just a list of entries"?
----------------------------------------------------------

A bare ``List[MemoryEntry]`` would lose the four pieces of metadata
that the injection layer needs and that observability depends on:

  1. **Budget traceability** -- ``used_tokens`` and
     ``dropped_for_budget`` let the monitoring layer (P1.9) attribute
     every wasted token to a specific cause.
  2. **Router traceability** -- ``router_decision`` records WHY a
     retrieval happened (or did not), so a missing-memory bug is
     distinguishable from a routing bug.
  3. **Ranker versioning** -- ``ranker_version`` allows A/B
     comparison across ranker improvements without losing the
     attribution of historical contexts.
  4. **Provenance of the query** -- ``query`` and ``scope_refs``
     record WHAT was searched and WHERE, which is required for the
     "why didn't you remember?" diagnostic flow.

Design invariants
-----------------

1. **Pure domain, no I/O, no framework coupling.**
   Like every other ``peldrun.memory.domain`` module, this file
   imports only stdlib + Pydantic + sibling domain modules. No
   repository, no provider, no FastAPI.

2. **Budget is a hard invariant, enforced at construction.**
   ``used_tokens`` MUST be ``<= token_budget``. A caller that
   overflows the budget has a bug in P1.4's fitter; catching it at
   ``MemoryContext(...)`` construction time prevents the bug from
   propagating into the prompt and silently blowing the model's
   context window.

3. **Empty is a first-class state, not an error.**
   ``MemoryContext.empty()`` is the correct return value for:
     * ``MemoryMode.DISABLED`` (null fast-path in P1.7),
     * ``RouteDecision.NO_NEED`` (greetings, independent questions),
     * any retrieval that found nothing above the relevance
       threshold.
   ``is_empty`` is the ONLY predicate the injection layer needs to
   check before emitting the ``[RETRIEVED MEMORY]`` block.

4. **Router decision strings are validated here, promoted to enum
   in P1.4.**
   The four legal values are declared as a frozen set in this module
   so that a typo like ``"no-need"`` (hyphen) or ``"NO_NEED"``
   (uppercase) is rejected at construction. When P1.4 introduces
   the ``RouteDecision`` enum in ``services/router.py``, this
   module's type annotation will be tightened to that enum, but
   the frozenset stays as a belt-and-suspenders runtime check
   (Pydantic validators on str-Enum fields occasionally accept
   mixed casing in edge cases). The string VALUES are stable
   because they appear in persisted monitoring records.

5. **Immutability by convention.**
   ``MemoryContext`` is technically mutable (Pydantic's default),
   but every caller treats it as read-only. Making it
   ``frozen=True`` was considered and rejected: the marginal
   safety benefit does not justify forcing every test and
   diagnostic tool to use ``model_copy(update=...)`` instead of
   direct assignment. The convention is enforced by review, not
   by the type system.

6. **No references to the ORIGINAL memories' full content in the
   context object beyond what was actually injected.**
   ``memories`` contains ONLY the entries that survived ranking
   AND budget fitting. Entries that were dropped for budget are
   recorded as a COUNT in ``dropped_for_budget`` -- the dropped
   entries themselves are NOT retained. This keeps the context
   small (the memory entries can be several KB each) and
   eliminates the risk of a downstream bug accidentally injecting
   a dropped entry by reading from a "candidates" list that was
   never meant to be consumed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from peldrun.memory.domain.entry import MemoryEntry


# ---------------------------------------------------------------------------
# Time helper (kept local to avoid cross-module private imports)
# ---------------------------------------------------------------------------
#
# A two-line function is cheaper than coupling to ``entry._utc_now``,
# which is technically importable but whose underscore-prefix signals
# "private to entry.py". Each domain module keeps its own tiny copy
# of this helper so the modules remain trivially readable in
# isolation.

def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


def _ensure_utc_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce ``value`` to timezone-aware UTC. ``None`` passes through."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Router decision vocabulary
# ---------------------------------------------------------------------------
#
# These four strings mirror the ``RouteDecision`` enum that P1.4 will
# introduce in ``peldrun/memory/services/router.py``. They are declared
# here as a frozenset so that:
#
#   1. ``MemoryContext`` can validate its ``router_decision`` field
#      BEFORE the router module exists (avoiding a forward dependency
#      from domain to services).
#   2. The values are visible in ONE place for readers who are only
#      reviewing the domain layer.
#
# When P1.4 lands, the ``router_decision`` field's type annotation
# will become ``RouteDecision`` (a ``str``-Enum), but this frozenset
# remains as a secondary check. There is NO data migration cost
# because the values are identical.

_VALID_ROUTER_DECISIONS: frozenset[str] = frozenset(
    {
        "no_need",           # greeting / independent question / short query
        "directed",          # user explicitly asked to recall something
        "precise",           # project or file scoped request
        "during_execution",  # invoked from the memory_search tool itself
    }
)


# ---------------------------------------------------------------------------
# MemoryContext
# ---------------------------------------------------------------------------


class MemoryContext(BaseModel):
    """Transient container for one turn's retrieved memories.

    See the module docstring for the full rationale. In short: this
    is the "output of retrieval" that the runtime passes to the
    context builder so the model can see its own memory.

    The class is deliberately small. Every field either:
      * feeds the prompt (``memories``),
      * proves the budget was respected (``token_budget``,
        ``used_tokens``, ``dropped_for_budget``), or
      * answers a diagnostic question (``query``, ``scope_refs``,
        ``retrieved_at``, ``router_decision``, ``ranker_version``).
    """

    model_config = ConfigDict(
        extra="forbid",
        # ``populate_by_name=True`` matches the convention used across
        # every other model in this package, so a caller can use
        # either the Python attribute or the JSON alias (currently
        # identical) without surprise.
        populate_by_name=True,
    )

    # ------------------------- what was asked -------------------------------

    query: str = Field(
        ...,
        description=(
            "The user-visible prompt (or the ``memory_search`` tool's "
            "query) that triggered this retrieval. Preserved verbatim "
            "so a diagnostic flow can answer 'why did you retrieve X "
            "for THIS question?'. Trimmed of leading/trailing "
            "whitespace by the validator."
        ),
    )
    scope_refs: List[str] = Field(
        ...,
        description=(
            "The scope references searched during this retrieval, in "
            "canonical ``<scope_type>:<scope_id>`` form -- for example "
            "``['user:default', 'project:acme']``. Used by the "
            "injection layer to label each memory with its origin, "
            "and by the monitoring layer to attribute token spend "
            "per scope. May be empty ONLY when no retrieval was "
            "attempted at all (NO_NEED route, DISABLED mode)."
        ),
    )

    # ------------------------- what was retrieved ---------------------------

    memories: List[MemoryEntry] = Field(
        default_factory=list,
        description=(
            "The entries that survived ranking AND budget fitting. "
            "This list is authoritative: every entry it contains "
            "WILL be injected into the prompt. Entries dropped for "
            "budget are NOT included here -- they are reflected only "
            "as the count in ``dropped_for_budget``."
        ),
    )

    # ------------------------- budget ---------------------------------------

    token_budget: int = Field(
        default=1024,
        ge=1,
        description=(
            "The maximum number of tokens the injection layer is "
            "allowed to consume from the model's context window for "
            "this memory block. Sourced from "
            "``MemorySettings.token_budget.max_tokens`` in P1.7. "
            "Must be at least 1 (a zero budget is the same as "
            "DISABLED mode -- use ``MemoryContext.empty()`` instead)."
        ),
    )
    used_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "The actual number of tokens consumed by this context "
            "in the assembled prompt. Includes the ``[RETRIEVED "
            "MEMORY]`` header, per-entry labels, and separators -- "
            "not just the raw content length. MUST be "
            "``<= token_budget`` (enforced by ``_check_budget``)."
        ),
    )
    dropped_for_budget: int = Field(
        default=0,
        ge=0,
        description=(
            "The number of entries that were retrieved by the "
            "ranker but then dropped during budget fitting. Count "
            "only -- the dropped entries are intentionally NOT "
            "retained in ``memories``. Used by the monitoring "
            "layer (P1.9) to answer 'are we under-provisioning the "
            "memory budget?' and by the UI to show a subtle "
            "'+N more memories omitted for size' hint."
        ),
    )

    # ------------------------- traceability ---------------------------------

    retrieved_at: datetime = Field(
        default_factory=_utc_now,
        description=(
            "When the retrieval was executed (aware UTC). Defaults "
            "to 'now' but can be supplied explicitly in tests that "
            "need deterministic timestamps."
        ),
    )
    router_decision: str = Field(
        default="no_need",
        description=(
            "The ``RouteDecision`` that drove this retrieval. One of: "
            "``no_need`` (greeting / short query / independent "
            "question), ``directed`` (user asked to recall), "
            "``precise`` (project- or file-scoped request), or "
            "``during_execution`` (invoked from the memory_search "
            "tool itself). Any other value is rejected by the "
            "validator -- a typo here would silently disable "
            "diagnostic attribution."
        ),
    )
    ranker_version: str = Field(
        default="v1",
        description=(
            "Version string identifying the ranking algorithm that "
            "ordered ``memories``. Allows A/B comparison of future "
            "ranker improvements. Free-form (``v1``, ``v2-beta``, "
            "``v1+recency``...), but MUST be non-empty so a "
            "missing-value bug is immediately visible in logs."
        ),
    )

    # ------------------------- validators -----------------------------------

    @field_validator("query", mode="after")
    @classmethod
    def _validate_query(cls, v: str) -> str:
        """Trim ``query``; allow empty string for NO_NEED-with-empty-input.

        The only legitimate empty-query case is a fast-path
        ``MemoryContext.empty()`` built when the caller had no
        user text yet (e.g., a system-initiated turn). Everything
        else should have at least one character.
        """
        if not isinstance(v, str):
            raise ValueError(
                f"query must be a string, got {type(v).__name__}"
            )
        return v.strip()

    @field_validator("scope_refs", mode="after")
    @classmethod
    def _validate_scope_refs(cls, v: List[str]) -> List[str]:
        """Structural validation of each ``scope_ref``.

        Each ref MUST:
          * be a non-empty string after trimming,
          * contain at least one ``:`` separator (the canonical
            ``<scope_type>:<scope_id>`` contract).

        The list itself MAY be empty -- that is the legitimate
        "no retrieval attempted" state. Duplicates are preserved
        (the retrieval layer might search the same scope twice with
        different filters; recording both is accurate).
        """
        if not isinstance(v, list):
            raise ValueError(
                f"scope_refs must be a list, got {type(v).__name__}"
            )

        result: List[str] = []
        for idx, raw in enumerate(v):
            if not isinstance(raw, str):
                raise ValueError(
                    f"scope_refs[{idx}] must be a string, "
                    f"got {type(raw).__name__}"
                )
            stripped = raw.strip()
            if not stripped:
                raise ValueError(
                    f"scope_refs[{idx}] must not be empty"
                )
            if ":" not in stripped:
                raise ValueError(
                    f"scope_refs[{idx}] = {stripped!r} is not in "
                    f"canonical '<scope_type>:<scope_id>' form; "
                    f"the ':' separator is mandatory"
                )
            result.append(stripped)
        return result

    @field_validator("router_decision", mode="after")
    @classmethod
    def _validate_router_decision(cls, v: str) -> str:
        """Enforce membership in the closed ``RouteDecision`` vocabulary."""
        if not isinstance(v, str):
            raise ValueError(
                f"router_decision must be a string, got "
                f"{type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError("router_decision must not be empty")
        if stripped not in _VALID_ROUTER_DECISIONS:
            raise ValueError(
                f"router_decision = {stripped!r} is not a valid "
                f"RouteDecision; expected one of "
                f"{sorted(_VALID_ROUTER_DECISIONS)}"
            )
        return stripped

    @field_validator("ranker_version", mode="after")
    @classmethod
    def _validate_ranker_version(cls, v: str) -> str:
        """Require a non-empty ranker version string."""
        if not isinstance(v, str):
            raise ValueError(
                f"ranker_version must be a string, got "
                f"{type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError("ranker_version must not be empty")
        return stripped

    @field_validator("retrieved_at", mode="after")
    @classmethod
    def _ensure_utc_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Coerce ``retrieved_at`` to aware UTC (matches entry.py behavior)."""
        return _ensure_utc_aware(v)

    @model_validator(mode="after")
    def _check_budget(self) -> "MemoryContext":
        """Enforce the hard ``used_tokens <= token_budget`` invariant.

        A violation here means P1.4's budget fitter has a bug. We
        reject the context at construction time so the bug cannot
        propagate into a prompt and silently overflow the model's
        context window (which would produce a 400 from the provider
        -- an opaque, expensive failure mode).
        """
        if self.used_tokens > self.token_budget:
            raise ValueError(
                f"MemoryContext budget violation: used_tokens="
                f"{self.used_tokens} > token_budget="
                f"{self.token_budget}. This is a bug in the caller "
                f"(the budget fitter in P1.4 must drop entries "
                f"before reaching this point)."
            )
        return self

    # ------------------------- derived properties ---------------------------

    @property
    def is_empty(self) -> bool:
        """True when there is nothing for the injection layer to emit.

        The single predicate the injection layer (P1.5) needs to
        check. Returning True means "do not emit the
        ``[RETRIEVED MEMORY]`` block at all" -- the prompt is
        bit-for-bit identical to the no-memory case.

        This property is defined purely in terms of ``memories`` --
        the header token count, the router decision, and the
        dropped-for-budget count are DIAGNOSTIC metadata, not
        reasons to inject an empty block.
        """
        return len(self.memories) == 0

    @property
    def utilization(self) -> float:
        """Fraction of the token budget consumed by this context.

        Returns a float in ``[0.0, 1.0]`` (the ``_check_budget``
        validator guarantees the upper bound). Used by:
          * the monitoring layer (P1.9) to flag under- or
            over-provisioning of the memory budget,
          * ``MemoryTokenBudget.warn_threshold`` warnings in P1.7,
          * unit tests that assert the fitter is accurate.
        """
        if self.token_budget <= 0:
            return 0.0
        return self.used_tokens / float(self.token_budget)

    # ------------------------- convenience constructor ----------------------

    @classmethod
    def empty(
        cls,
        *,
        query: str,
        scope_refs: Optional[List[str]] = None,
        token_budget: int = 1024,
        router_decision: str = "no_need",
        ranker_version: str = "v1",
        retrieved_at: Optional[datetime] = None,
    ) -> "MemoryContext":
        """Build an empty context for a given query/scope.

        This is the FAST-PATH return value for every situation where
        no memories will be injected:

          * ``MemoryMode.DISABLED`` (the null port never touches the
            repository),
          * ``RouteDecision.NO_NEED`` (greeting / short query),
          * retrieval ran but found zero entries above the relevance
            threshold.

        Constructing via this method (rather than ``MemoryContext(
        memories=[], ...)`` directly) guarantees that:
          * ``used_tokens`` is 0 (no header tokens were consumed),
          * ``dropped_for_budget`` is 0 (nothing was dropped),
          * ``is_empty`` returns True,
          * the same string values for ``router_decision`` are used
            regardless of which fast path triggered.

        The default ``router_decision="no_need"`` is correct for the
        most common fast path (DISABLED mode and NO_NEED route).
        Callers that reach the empty state via a DIFFERENT route
        (e.g., a ``precise`` query that found nothing) should pass
        the actual decision so the diagnostic record is accurate.
        """
        return cls(
            query=query,
            scope_refs=list(scope_refs or []),
            memories=[],
            token_budget=token_budget,
            used_tokens=0,
            dropped_for_budget=0,
            retrieved_at=_ensure_utc_aware(retrieved_at) or _utc_now(),
            router_decision=router_decision,
            ranker_version=ranker_version,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["MemoryContext"]
