# backend/peldrun/memory/services/router.py
"""
PELDRUN Memory Platform - Retrieval Router.

Deterministic, LLM-free classification of whether a given turn
SHOULD trigger a memory retrieval, and if so, HOW broadly to search.

Why a dedicated router
----------------------

Retrieval is not free: it costs an FTS5 query + scope filtering +
BM25 ranking + token budget fitting. On a deployment with 10k
memories and 100 concurrent chats, that is measurable load. More
importantly, retrieval on EVERY turn injects irrelevant memories
into the prompt, which makes the model's context noisier and
degrades its answers even when the retrieval itself succeeds.

The router's job is to answer "is THIS turn worth a retrieval?".
Its decision is one of four:

    * ``NO_NEED``           -- do not retrieve. Greetings, short
                               queries, and queries with no memory
                               intent. The dispatcher returns
                               ``MemoryContext.empty()`` immediately.
    * ``DIRECTED``          -- retrieve. The user explicitly asked
                               to recall something ("remember...",
                               "what did we decide?"). The ranker
                               receives the query as-is and returns
                               the top-ranked matches.
    * ``PRECISE``           -- retrieve, scoped tighter. The user
                               asked about this project / this file
                               / this session. Scope filters are
                               narrowed (see P1.5's dispatcher) and
                               the ranker's ``boost_pinned`` weight
                               is elevated.
    * ``DURING_EXECUTION``  -- the ``memory_search`` tool called
                               the service from inside a run. The
                               router is BYPASSED for this value:
                               the tool provides its own query and
                               already knows it wants retrieval.

Why deterministic, not LLM
--------------------------

An LLM-based router would need a sub-second latency budget, would
add non-determinism to a decision whose consequences are visible
to the user, and would itself consume tokens (so it would save
nothing on cost). The deterministic approach below is:

  * FAST (microseconds -- compiled regexes, no I/O),
  * EXPLAINABLE (every decision has a ``reason`` string that
    names the matching rule),
  * STABLE (adding a memory does not change how future turns
    are classified), and
  * SAFE (the worst case is a missed retrieval -- the model sees
    one fewer memory than it could have. There is no data loss
    and no security concern).

The classification rules are intentionally simple. The plan
explicitly notes that Phase 1 uses heuristics; P2 may layer an
LLM classifier on top, but it would be a NEW ``classify_route``
implementation behind the same signature -- not a change to this
module's structure.

Design invariants
-----------------

1. Pure domain.
   No imports beyond ``enum``, ``re``, ``typing``. No
   ``MemoryContext``, no repository, no provider. The router
   answers a classification question; it does not touch storage.

2. Deterministic.
   ``classify_route(q, has_active_project=X)`` returns the SAME
   result for the same inputs, on every process, every platform,
   every Python version (3.10+). No clock, no randomness, no
   locale-dependence.

3. Bounded patterns.
   Every regex is compiled ONCE at module load. There is no
   ``re.compile()`` inside ``classify_route`` -- the per-call
   cost is O(query length).

4. Bilingual by design.
   The patterns cover Arabic, Russian, and English out of the
   box, matching the normalizer's three-language scope. Adding a
   language means adding alternatives to the existing pattern
   constants -- not creating a new code path.

5. Traceable decisions.
   ``classify_route`` returns a ``Route`` object with a
   ``reason`` field naming the rule that fired. When a user asks
   "why didn't my agent remember?", the reason pinpoints the
   decision without re-running the classification with debug
   prints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final


# ---------------------------------------------------------------------------
# RouteDecision
# ---------------------------------------------------------------------------


class RouteDecision(str, Enum):
    """The four possible retrieval classifications.

    The values MUST stay in sync with
    ``peldrun.memory.domain.context._VALID_ROUTER_DECISIONS``. That
    frozenset is the runtime check; this enum is the typed API. A
    test in ``tests/memory/test_domain.py`` verifies the two agree.
    """

    NO_NEED = "no_need"
    """Do not retrieve. Fast path in the dispatcher."""

    DIRECTED = "directed"
    """Retrieve; the user explicitly asked to recall something."""

    PRECISE = "precise"
    """Retrieve, scoped tighter to this project / file / session."""

    DURING_EXECUTION = "during_execution"
    """The ``memory_search`` tool invoked retrieval from inside a run."""


# ---------------------------------------------------------------------------
# Route -- the classification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """Result of ``classify_route`` -- the decision plus its reason.

    Frozen so that callers cannot mutate the decision mid-flight.
    The ``reason`` field is used exclusively for diagnostics; it is
    never parsed by application code. It appears in log lines,
    in ``MemoryContext.router_decision`` (as a comment), and in
    the ``GET /api/memory/usage`` breakdown when the operator
    drills into a low-recall period.

    Attributes
    ----------
    decision
        One of the four ``RouteDecision`` members.
    reason
        A short, non-empty, human-readable explanation of the
        rule that fired. Format: ``"<rule_name>: <detail>"``,
        e.g. ``"short_query: len=3 < 5"``. Stable within a
        release (changing a reason string is fine, but the format
        is a documented convention).
    """

    decision: RouteDecision
    reason: str


# ---------------------------------------------------------------------------
# Pattern constants -- compiled once at import
# ---------------------------------------------------------------------------


#: Minimum query length below which the router always returns
#: ``NO_NEED``. The plan's stub uses 5. Short queries ("hi", "ok",
#: "مرحبا") carry no retrievable intent and can only waste budget
#: if retrieved.
_MIN_QUERY_LENGTH: Final[int] = 5

#: Greetings and other trivially short openings. Anchored to the
#: START of the (stripped) query so that "hello, remember when..."
#: does NOT match here and falls through to the directed check.
#: Languages covered: English, Arabic, Russian, Arabic-Latin
#: transliteration ("salam", "assalam").
_GREETING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    r"hi|hello|hey|yo|sup"  # English
    r"|مرحبا|أهلا|اهلا|السلام\s*عليكم"  # Arabic
    r"|привет|здравствуй(?:те)?"  # Russian
    r"|salam|assalam|salaam"  # Latin transliteration
    r")\b",
    re.IGNORECASE,
)

#: Explicit recall requests. Anywhere in the query (not
#: anchored) -- "before we continue, please remember that X" is a
#: directed request even though it does not start with a keyword.
#: The alternatives cover the three languages symmetrically.
_DIRECTED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"remember|recall|do\s+you\s+know|did\s+(?:we|I)|"  # English
    r"what\s+(?:did\s+we|was\s+the)|"
    r"تذكر|تتذكر|هل\s+تذكر|ما\s+القرار|ما\s+قررنا|"  # Arabic
    r"помнишь|помните|что\s+мы|какое\s+решение"  # Russian
    r")",
    re.IGNORECASE,
)

#: Project / file / session scope hints. When one of these fires
#: AND the caller has an active project, the router returns
#: ``PRECISE`` so the dispatcher can narrow its scope filters.
#: Without an active project, they are ignored -- a request to
#: "remember the file we discussed" in a project-less chat is
#: directed, not precise.
_PRECISE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"in\s+this\s+(?:project|repo|repository|workspace)|"  # English
    r"the\s+file|this\s+codebase|"
    r"هذا\s+المشروع|هذه\s+المحفظة|الملف|"  # Arabic
    r"в\s+этом\s+проекте|в\s+этом\s+файле"  # Russian
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# classify_route
# ---------------------------------------------------------------------------


def classify_route(
    query: str,
    has_active_project: bool = False,
) -> Route:
    """Classify a user query's retrieval intent.

    Parameters
    ----------
    query
        The user's message. May be ``None`` (treated as ``""``),
        may be whitespace-only (treated as ``""``), may contain
        multi-line content (only the first 500 characters are
        inspected -- the intent signal is always near the start).
    has_active_project
        True when the caller has a project id in its runtime
        context. When False, ``PRECISE`` is NEVER returned even
        if the query matches ``_PRECISE_PATTERN`` -- the caller
        has no project scope to narrow TO, so precise filtering
        would be a no-op. The router falls through to the
        directed check in that case.

    Returns
    -------
    Route
        A frozen ``Route`` object with ``decision`` and ``reason``.
        ``decision`` is never ``DURING_EXECUTION`` here -- that
        value is set by the tool-invocation path in P1.4's
        ``context_builder``/``memory_search`` tool, not by this
        function.

    Raises
    ------
    TypeError
        When ``query`` is neither ``str`` nor ``None``. A
        mis-typed query is a caller bug, not a classification
        ambiguity -- raising surfaces it immediately.

    Behavior summary
    ----------------
    The decision tree below is EXHAUSTIVE and ORDERED. Every
    query matches exactly one rule (the first that fires), and
    every rule produces a non-empty ``reason`` string. The
    sequence matters:

      1. Empty                      -> NO_NEED
      2. Greeting                   -> NO_NEED
      3. Too short                  -> NO_NEED
      4. Precise hint (with proj)   -> PRECISE
      5. Directed recall request    -> DIRECTED
      6. Fallback                   -> NO_NEED

    Step 4 comes before step 5 so that "in this project, what did
    we decide about X?" classifies as PRECISE -- the project scope
    is the more actionable signal because it lets the dispatcher
    narrow the search.

    Step 2 (greeting) comes before step 3 (too short) because
    short greetings ("hi", "hey", "yo") are more accurately
    described as greetings than as length-rejections. The decision
    is NO_NEED either way; the earlier rule produces a more useful
    ``reason`` for diagnostics.
    """
    if query is None:
        return Route(
            decision=RouteDecision.NO_NEED,
            reason="empty_query",
        )

    if not isinstance(query, str):
        raise TypeError(
            f"classify_route expects str or None, "
            f"got {type(query).__name__}"
        )

    # Only the first 500 chars matter for intent detection. The
    # signal is always at the start; truncating keeps the regex
    # cost bounded even for pathological inputs (10 KB paste).
    inspected = query.strip()[:500]

    # --- Rule 1: empty ------------------------------------------------
    if not inspected:
        return Route(
            decision=RouteDecision.NO_NEED,
            reason="empty_query",
        )

    # --- Rule 2: greeting --------------------------------------------
    # Fires BEFORE the length check so that short greetings
    # ("hi", "hey", "yo", "مرحبا", "привет") are labeled as
    # greetings, not as length-rejections. Both produce
    # NO_NEED, but the reason field feeds diagnostics.
    if _GREETING_PATTERN.match(inspected):
        return Route(
            decision=RouteDecision.NO_NEED,
            reason="greeting_pattern",
        )

    # --- Rule 3: too short -------------------------------------------
    if len(inspected) < _MIN_QUERY_LENGTH:
        return Route(
            decision=RouteDecision.NO_NEED,
            reason=f"short_query: len={len(inspected)} < {_MIN_QUERY_LENGTH}",
        )

    # --- Rule 4: precise scope hint (only if we have a project) ------
    if has_active_project and _PRECISE_PATTERN.search(inspected):
        return Route(
            decision=RouteDecision.PRECISE,
            reason="precise_pattern_with_active_project",
        )

    # --- Rule 5: directed recall request ------------------------------
    if _DIRECTED_PATTERN.search(inspected):
        return Route(
            decision=RouteDecision.DIRECTED,
            reason="directed_pattern",
        )

    # --- Rule 6: fallback --------------------------------------------
    return Route(
        decision=RouteDecision.NO_NEED,
        reason="no_intent_signal",
    )


# ---------------------------------------------------------------------------
# Convenience constructor (used by the memory_search tool in P1.4)
# ---------------------------------------------------------------------------


def route_for_tool_invocation(query: str) -> Route:
    """Return the ``DURING_EXECUTION`` route for tool-initiated retrieval.

    Unlike ``classify_route``, this NEVER inspects ``query`` --
    the tool has already declared its intent by invoking the
    ``memory_search`` tool. The route exists to:

      * make the ``router_decision`` field on the resulting
        ``MemoryContext`` accurate (it becomes
        ``"during_execution"``), which the monitoring layer
        (P1.9) uses to attribute token spend to the tool
        vs. the automatic dispatcher path,
      * provide a well-defined ``reason`` for diagnostics
        (``"tool_invoked: memory_search"``).

    Parameters
    ----------
    query
        The tool's search query. Passed through for the ``Route``
        object's context but not inspected.

    Returns
    -------
    Route
        Always ``RouteDecision.DURING_EXECUTION``.
    """
    # Truncate for the same reason as classify_route -- the reason
    # field embeds a prefix of the query and should stay bounded.
    preview = (query or "").strip()[:80]
    return Route(
        decision=RouteDecision.DURING_EXECUTION,
        reason=f"tool_invoked: memory_search (query={preview!r})",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "RouteDecision",
    "Route",
    "classify_route",
    "route_for_tool_invocation",
]
