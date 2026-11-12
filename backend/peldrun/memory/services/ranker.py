# backend/peldrun/memory/services/ranker.py
"""
PELDRUN Memory Platform - Memory Ranker.

Combines five orthogonal signals into a single relevance score for
each candidate memory entry, then orders the candidates by that
score. Pure function -- no I/O, no storage, no settings loading.

The five signals
----------------

1. **BM25 relevance** (from ``search_ranked``) -- measures "how
   well does this entry match the query?". Provided by the
   repository. Already normalized to [0, 1] (1.0 = best in the
   result set).

2. **Importance** (``entry.importance / 100``) -- the user's or
   policy's judgment of how critical this memory is. Independent
   of any specific query. An importance-100 entry about the
   user's language preference should surface even when it only
   partially matches.

3. **Recency** (exponential decay) -- "how recently was this
   entry touched?". Uses ``last_used_at`` when available, falls
   back to ``created_at``. Half-life of 30 days: an entry touched
   today scores 1.0; one from 30 days ago scores 0.5; one from
   60 days ago scores 0.25.

4. **Scope precedence** -- USER > PROJECT > CHAT > AGENT.
   Reflects the durability of the scope: user preferences are
   the most durable signal; chat-scoped facts are the least.

5. **Pinned boost** -- a MULTIPLIER applied to the final
   weighted sum. Pinned entries dominate proportionally: a
   pinned weak match can beat an unpinned strong match, but a
   pinned *terrible* match still cannot outrank a strongly
   relevant unpinned one.

The weighted-sum formula
------------------------

::

    final = (
        0.50 * relevance +
        0.20 * importance +
        0.15 * recency +
        0.15 * scope_precedence
    ) * (settings.boost_pinned if entry.is_pinned else 1.0)

The weights sum to 1.00 so that, in the absence of pinned
boosts, the final score is bounded by [0, 1]. This makes the
numbers interpretable: 0.8 means "strong on every signal", 0.3
means "weak overall". Operators inspecting the monitoring
dashboard can therefore set meaningful expectations.

Why a pure function
-------------------

``rank_memories`` accepts a LIST of candidates and returns a
LIST of ranked results. It does NOT:
  * query the storage layer,
  * load settings from disk,
  * read the system clock implicitly (see ``now`` parameter),
  * mutate any input.

This makes it:
  * **Unit-testable** without DB, filesystem, or time mocking
    (``now`` is injectable),
  * **Deterministic** for a given ``(candidates, settings, now)``
    triple,
  * **Trivially replayable** -- given the same input, the same
    output. Useful for incident investigation: "what did the
    ranker see at 14:23?"

``min_relevance_score`` is applied BEFORE the weighted sum
-----------------------------------------------------------

Entries whose BM25 relevance is below
``settings.min_relevance_score`` are DROPPED, not demoted. The
semantics are deliberate:

  * A weak BM25 match cannot become a strong final score via
    high importance or recency. That would defeat the point of
    running the query in the first place (retrieval should
    retrieve relevant things, not merely well-aged ones).
  * Filtering is cheap: it happens before we compute four
    signals per surviving entry, so the per-candidate cost of
    a dropped entry is O(1) instead of O(1) + sortable work.
  * The dropped count is what ``MemoryContext.dropped_for_budget``
    attributes to "relevance filtering" in P1.4's
    ``context_builder`` (the total drop count is a combination
    of relevance-drops and budget-drops; the builder decides
    how to attribute them, but the ranker surfaces the count).

Edge cases
----------

* Empty candidate list -> empty result. The caller (the
  context builder) produces an empty ``MemoryContext`` in that
  case.
* All candidates below ``min_relevance_score`` -> empty result.
  Same handling as empty candidates -- the caller will produce
  an empty context. This is the desired behavior: a query that
  matched only weak entries should not inject them.
* Single candidate -> its full weighted score is returned (with
  relevance 1.0 from the repository's magnitude normalization).
* ``last_used_at`` is None (entry was never retrieved) -> the
  recency calculation falls back to ``created_at``. An entry
  created 5 minutes ago and never used scores high on recency;
  one created 5 months ago and never used scores low. This
  matches user intuition: "freshly stored" implies "worth
  surfacing".
* All entries tie on the weighted score -> tie-breaking by
  ``importance DESC``, then ``last_used_at DESC`` (None treated
  as oldest), then ``created_at DESC``. This gives a stable,
  deterministic ordering across runs.

Version tracking
----------------

``RANKER_VERSION`` is a module-level constant embedded in every
``RankedEntry``. It propagates to ``MemoryContext.ranker_version``
via the context builder. Historical monitoring data carries the
version that produced it, so future ranker improvements can be
compared A/B without ambiguity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from peldrun.memory.domain import (
    MemoryEntry,
    MemorySettings,
)


# ---------------------------------------------------------------------------
# Signal weights (module constants)
# ---------------------------------------------------------------------------
#
# Tuning these changes the ranker's behavior in ways that affect
# every retrieval. They are constants (not settings) so that tuning
# happens through code review, tests, and a version bump -- not
# through a UI slider that operators change without seeing the
# downstream effects.
#
# If P2 needs per-deployment tunable weights, they can be added to
# ``MemorySettings`` as a nested ``RankingWeights`` model. For
# Phase 1, the values below are the correct defaults and only
# tests should override them (via ``monkeypatch`` on the module).
#
# The weights sum to 1.0. Changing this invariant requires
# re-normalizing the final score, which in turn changes what the
# monitoring dashboard's percentages mean. Document any such
# change in the changelog.

WEIGHT_RELEVANCE: float = 0.50
"""BM25 relevance from ``search_ranked``. Dominant weight --
relevance is the whole reason we ran the query."""

WEIGHT_IMPORTANCE: float = 0.20
"""Entry's user-assigned / policy-assigned importance."""

WEIGHT_RECENCY: float = 0.15
"""Exponential recency decay from ``last_used_at`` or
``created_at``."""

WEIGHT_SCOPE: float = 0.15
"""Scope precedence (USER > PROJECT > CHAT > AGENT)."""


#: Half-life of the recency decay, in days. An entry last touched
#: ``HALF_LIFE`` days ago scores exactly 0.5 on the recency signal;
#: one touched ``2 * HALF_LIFE`` days ago scores 0.25; and so on.
#: 30 days is a deliberate choice -- "about a month" matches how
#: users think about "recent".
RECENCY_HALF_LIFE_DAYS: float = 30.0


#: Maximum scope precedence value. ``MemoryScope.USER.precedence``
#: is 4, so this is the divisor used to normalize scope precedence
#: to [0, 1]. If a future scope is added with precedence 5, this
#: constant must be updated AND ``tests/memory/test_domain.py``
#: must be re-run to verify the ordering assertion.
_MAX_SCOPE_PRECEDENCE: float = 4.0


#: Version string embedded in every ``RankedEntry``. Bump on any
#: change to the weights, the recency half-life, the tie-breaking
#: rules, or the filtering order. Historical monitoring data
#: carries this value, so a bump lets analyses distinguish old
#: data from new without needing a separate schema version.
RANKER_VERSION: str = "v1"


# ---------------------------------------------------------------------------
# RankedEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankedEntry:
    """A single ranked candidate with its full scoring breakdown.

    Frozen so that callers cannot mutate the ranking mid-flight.
    The ``breakdown`` dict exists for diagnostics: when the
    monitoring dashboard shows "this entry was injected at rank
    3", the breakdown answers "why was it rank 3?".

    Attributes
    ----------
    entry
        The original ``MemoryEntry`` -- unchanged, exactly as the
        repository returned it.
    final_score
        Weighted sum * pinned boost. In ``[0.0, settings.boost_pinned]``
        when unpinned entries dominate, or ``[0.0, boost_pinned]``
        when pinned entries do. For typical ``boost_pinned=1.5``,
        the effective max is 1.5.
    breakdown
        Per-signal contributions, keyed by short signal name:
          * ``"relevance"``   -- the raw BM25 magnitude [0, 1],
          * ``"importance"``  -- ``importance / 100``,
          * ``"recency"``     -- the decay value [0, 1],
          * ``"scope"``       -- normalized precedence [0, 1],
          * ``"pinned_multiplier"`` -- 1.0 or ``settings.boost_pinned``.
        Sum of the four base signals * multiplier == final_score
        (modulo floating-point rounding). The context builder
        embeds this dict in monitoring records when debug verbosity
        is enabled.
    ranker_version
        The module-level ``RANKER_VERSION`` at the time of ranking.
        Propagates to ``MemoryContext.ranker_version``.
    """

    entry: MemoryEntry
    final_score: float
    breakdown: Dict[str, float] = field(default_factory=dict)
    ranker_version: str = RANKER_VERSION


# ---------------------------------------------------------------------------
# rank_memories -- the main function
# ---------------------------------------------------------------------------


def rank_memories(
    candidates: List[Tuple[MemoryEntry, float]],
    settings: MemorySettings,
    *,
    now: Optional[datetime] = None,
) -> List[RankedEntry]:
    """Rank memory candidates by combined relevance signals.

    Parameters
    ----------
    candidates
        ``(entry, relevance)`` pairs as returned by
        ``SqliteMemoryRepository.search_ranked``. ``relevance`` is
        expected to be a normalized float in ``[0, 1]`` (the
        repository's ``search_ranked`` guarantees this). An empty
        list is a valid input and returns an empty result.

    settings
        ``MemorySettings`` for this run. Two fields are consulted:
          * ``settings.min_relevance_score`` -- filter threshold,
          * ``settings.boost_pinned`` -- pinned multiplier.

    now
        The "current time" for recency calculation. Injectable so
        tests can pin it. When ``None`` (default), uses
        ``datetime.now(timezone.utc)``.

    Returns
    -------
    List[RankedEntry]
        Ranked entries in ``final_score``-descending order. Empty
        list when ``candidates`` is empty OR when every candidate
        falls below ``min_relevance_score``.

    Raises
    ------
    TypeError
        When ``candidates`` is not a list of 2-tuples or
        ``settings`` is not a ``MemorySettings``. Structural
        argument validation -- caller bugs surface immediately.

    Notes on the pipeline
    ---------------------
    The steps below are ORDERED and none may be swapped:

      1. **Filter by relevance.** Entries below
         ``min_relevance_score`` are DROPPED. This is the only
         step that removes entries -- later steps only reorder
         them.

      2. **Compute signals.** For each surviving entry, compute
         the four base signals (relevance, importance, recency,
         scope). The pinned multiplier is computed but not yet
         applied.

      3. **Weighted sum.** Combine the four signals with the
         module constants.

      4. **Pinned boost.** Multiply by ``settings.boost_pinned``
         when ``entry.is_pinned``, else by 1.0.

      5. **Sort.** Descending by ``final_score``. Ties broken by
         ``importance DESC``, then ``last_used_at DESC`` (None
         treated as oldest), then ``created_at DESC``. The
         multi-key tie-breaker guarantees determinism across
         runs -- two runs with identical inputs produce identical
         outputs.

      6. **Return.** Frozen ``RankedEntry`` objects, ready for
         the context builder's budget fitter.
    """
    # ------------------------------------------------------------------
    # 0. Structural argument validation (programmer errors)
    # ------------------------------------------------------------------
    if not isinstance(candidates, list):
        raise TypeError(
            f"candidates must be a list, got {type(candidates).__name__}"
        )
    if not isinstance(settings, MemorySettings):
        raise TypeError(
            f"settings must be MemorySettings, got "
            f"{type(settings).__name__}"
        )

    # Empty input: fast path, no work, no clock read.
    if not candidates:
        return []

    # Resolve "now" once so every recency computation uses the
    # same reference time. Reading the clock inside the loop would
    # produce microsecond-scale inconsistencies for entries in
    # the same batch.
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------
    # 1. Filter by relevance (BEFORE any signal computation)
    # ------------------------------------------------------------------
    #
    # Dropping below-threshold entries here (rather than at the
    # end) has three benefits:
    #   * matches caller intent ("don't inject weak matches"),
    #   * O(1) cost per dropped entry instead of O(4) signals +
    #     sort overhead,
    #   * avoids polluting the tie-breaker with entries that
    #     would never make it out anyway.
    threshold = float(settings.min_relevance_score)
    filtered: List[Tuple[MemoryEntry, float]] = [
        (entry, rel) for entry, rel in candidates if rel >= threshold
    ]
    if not filtered:
        return []

    # ------------------------------------------------------------------
    # 2-4. Compute signals and assemble RankedEntry
    # ------------------------------------------------------------------
    ranked: List[RankedEntry] = []

    for entry, relevance in filtered:
        # Clamp relevance defensively -- the repository guarantees
        # [0, 1] but a future backend might not.
        relevance = max(0.0, min(1.0, float(relevance)))

        # --- importance ---
        # 0-100 -> 0.0-1.0. The Pydantic field enforces the range
        # at construction time, so no clamping is needed.
        importance = float(entry.importance) / 100.0

        # --- recency ---
        # Exponential decay from the most recent touch.
        recency = _compute_recency(entry, now)

        # --- scope precedence ---
        # Normalized to [0, 1] by the max known precedence.
        # Division by 0 is impossible: _MAX_SCOPE_PRECEDENCE is
        # a positive constant.
        scope_score = float(entry.scope_type.precedence) / _MAX_SCOPE_PRECEDENCE

        # --- weighted sum ---
        base_score = (
            WEIGHT_RELEVANCE * relevance
            + WEIGHT_IMPORTANCE * importance
            + WEIGHT_RECENCY * recency
            + WEIGHT_SCOPE * scope_score
        )

        # --- pinned boost ---
        pinned_multiplier = (
            float(settings.boost_pinned) if entry.is_pinned else 1.0
        )
        final_score = base_score * pinned_multiplier

        ranked.append(
            RankedEntry(
                entry=entry,
                final_score=final_score,
                breakdown={
                    "relevance": relevance,
                    "importance": importance,
                    "recency": recency,
                    "scope": scope_score,
                    "pinned_multiplier": pinned_multiplier,
                },
                ranker_version=RANKER_VERSION,
            )
        )

    # ------------------------------------------------------------------
    # 5. Sort: final_score DESC, then tie-breakers
    # ------------------------------------------------------------------
    #
    # ``last_used_at`` can be None. Using ``-inf`` as the sort key
    # for None places never-used entries at the bottom of their
    # tied group (sorted DESC). This is correct: an entry that has
    # never been retrieved should not jump ahead of one that has,
    # all else equal.
    _NEG_INF = float("-inf")

    def _sort_key(r: RankedEntry) -> Tuple[float, int, float, float]:
        lu = (
            r.entry.last_used_at.timestamp()
            if r.entry.last_used_at is not None
            else _NEG_INF
        )
        created = (
            r.entry.created_at.timestamp()
            if r.entry.created_at is not None
            else _NEG_INF
        )
        return (
            # Negated for descending sort (higher final_score first)
            -r.final_score,
            # Importance DESC (higher importance first)
            -r.entry.importance,
            # last_used_at DESC (more recent first)
            -lu,
            # created_at DESC (newer entries first)
            -created,
        )

    ranked.sort(key=_sort_key)

    return ranked


# ---------------------------------------------------------------------------
# Helper -- recency
# ---------------------------------------------------------------------------


def _compute_recency(entry: MemoryEntry, now: datetime) -> float:
    """Return the recency score in ``[0, 1]`` for ``entry``.

    Uses ``last_used_at`` when available, ``created_at`` otherwise.
    Rationale: an entry that was just created is at least as
    "recent" as one that was last used months ago, regardless of
    whether it has been retrieved yet.

    Formula: ``0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)``.

      * age = 0 days   -> 1.0
      * age = 30 days  -> 0.5
      * age = 60 days  -> 0.25
      * age = 90 days  -> 0.125

    The reference time is the later of ``last_used_at`` and
    ``created_at``. A brand-new entry that has already been
    retrieved once should use ``last_used_at`` (which is newer
    than ``created_at``); a stale entry uses ``created_at`` (which
    equals ``last_used_at`` or is later than a None).

    Edge case: entries with a future timestamp (clock skew, test
    fixtures) get ``age_days = 0`` (clamped). This prevents the
    formula from producing values above 1.0.
    """
    reference_time: Optional[datetime] = entry.last_used_at
    if reference_time is None:
        reference_time = entry.created_at

    if reference_time is None:
        # No timestamp at all -- treat as maximally stale. Cannot
        # happen with the current schema (created_at is required)
        # but keeps the function total.
        return 0.0

    # Coerce to UTC so subtraction is correct even if an entry
    # was stored with a non-UTC aware datetime.
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)

    age_seconds = (now - reference_time).total_seconds()
    if age_seconds <= 0.0:
        # Future timestamp (clock skew or a test that set
        # now < created_at). Clamp to 1.0 rather than producing
        # values above 1.0 via a negative exponent.
        return 1.0

    age_days = age_seconds / 86_400.0
    # 0.5 ** (age / half_life) -- never negative, asymptote to 0.
    # The base is a literal because pow() with a negative base
    # would be undefined, but 0.5 is safe.
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    # Weights (for tests and diagnostics)
    "WEIGHT_RELEVANCE",
    "WEIGHT_IMPORTANCE",
    "WEIGHT_RECENCY",
    "WEIGHT_SCOPE",
    "RECENCY_HALF_LIFE_DAYS",
    "RANKER_VERSION",
    # Result type
    "RankedEntry",
    # Main entry point
    "rank_memories",
]
