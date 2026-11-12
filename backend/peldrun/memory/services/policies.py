# backend/peldrun/memory/services/policies.py
"""
PELDRUN Memory Platform - Write-Time Memory Policies.

Pure decision functions that determine WHAT TO DO with a candidate
memory at write time. This module answers three questions:

    1. **Is this text a memory candidate at all?**
       (``is_worth_saving``)

    2. **What kind of memory is it?**
       (``classify_memory_type``)

    3. **Given existing entries in the same scope, what write
       action should the caller take?**
       (``decide_write_action``, composed of ``detect_duplicate``
       and ``detect_conflict``)

Public surface
--------------

    ``WriteAction``         -- CREATE / SKIP / SUPERSEDE / REACTIVATE.
    ``WriteDecision``       -- frozen decision + diagnostic reason.
    ``is_worth_saving``     -- quality gate for candidate text.
    ``classify_memory_type``-- text -> MemoryType hint, or None.
    ``detect_duplicate``    -- find an existing duplicate, or None.
    ``detect_conflict``     -- find an existing conflict, or None.
    ``decide_write_action`` -- the composed decision entry point.

Why a policies module, separate from the extractor
-------------------------------------------------

``extractor.py`` (next file, P1.4) scans conversation turns and
PRODUCES candidate memories. ``policies.py`` DECIDES what to do
with each candidate. The separation matters because the SAME
policies are consulted by TWO different callers:

  * ``extractor.py`` -- to filter out candidates that would be
    rejected at write time anyway (efficiency: don't emit
    candidates we know will be dropped),
  * ``omweb.memory.manager.write_pipeline`` (P1.7) -- to decide
    the write action for candidates from ANY source (extractor,
    ``memory_save`` tool, import manager).

Keeping the decision logic in one place prevents the classic
divergence bug: extractor says "worth saving", write pipeline
says "actually, it's a duplicate" -- and the user sees their
memory fail to save after the extractor already told them it
was fine.

Phase 1 conservatism -- the core design choice
----------------------------------------------

``detect_conflict`` is DELIBERATELY conservative in Phase 1. It
fires ONLY when:

    * the candidate's memory type is PREFERENCE or DECISION
      (the two types where a new entry can legitimately
      supersede an old one),
    * the candidate contains an EXPLICIT replacement marker
      ("no longer", "changed to", "switched to", "instead of",
      ... -- see ``_REPLACEMENT_MARKERS``),
    * an existing entry with the same memory_type and at least
      one shared tag exists.

It does NOT attempt semantic conflict detection. The reason is
asymmetric cost:

  * **False negative** (missed conflict, both entries stored):
    the LLM sees both "user prefers Python" and "user prefers
    Rust" in context. It may be slightly confused, but no
    information is lost -- the user can correct the LLM, and
    the next "user now prefers X" statement is explicitly
    marked and DOES supersede.

  * **False positive** (spurious supersede, old entry archived):
    the old fact is GONE from future retrievals. The LLM never
    sees "user prefers Python" again. The user has no path to
    recover it except by manually re-adding it. This is a data
    loss bug.

On a system whose entire purpose is "remember what the user told
you", data loss is unacceptable -- far worse than a mild
ambiguity the LLM can handle at read time. Phase 1 therefore
errs strongly toward saving too much rather than saving too
little.

Phase 2 will add embedding-based semantic conflict detection
(which can distinguish "user prefers X" vs "user prefers Y" as
coexisting preferences from "user lives in Cairo" vs "user
lives in Paris" as conflicting facts). That capability does
NOT belong in a heuristic Phase 1.

What ``decide_write_action`` does NOT do
----------------------------------------

    * Does NOT query the storage layer. The caller (P1.7's
      WritePipeline) retrieves existing entries via
      ``provider.search`` or ``provider.list_by_scope`` and
      passes them in. Policies is pure.

    * Does NOT persist anything. The returned ``WriteDecision``
      is a plan, not an action. The caller executes.

    * Does NOT modify existing entries. When action is
      SUPERSEDE, the caller is responsible for archiving the
      target and creating the candidate. Both operations are
      done by the write pipeline as one atomic transaction.

Pre-conditions on the caller
----------------------------

    * ``existing`` entries MUST all belong to the SAME scope as
      the candidate. Duplicate / conflict detection is only
      meaningful within a scope. Filtering by scope is the
      caller's responsibility -- this module cannot do it
      because it does not have the provider.

    * ``existing`` SHOULD be a reasonably-sized list (dozens,
      not millions). The functions here are O(n) per candidate.
      For Phase 1, the write pipeline retrieves the top-K
      matching entries by tag or recency; a full-scope scan
      would work but is inefficient.

    * ``existing`` entries SHOULD include entries in EVERY
      status (ACTIVE, ARCHIVED, SUPERSEDED). An exact match
      against an ARCHIVED entry produces a REACTIVATE action
      (see ``WriteAction`` below), which is the desired
      behavior when a user re-saves a memory they previously
      deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, List, Optional

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryStatus,
    MemoryType,
)


# ---------------------------------------------------------------------------
# Phase 1 thresholds (module constants)
# ---------------------------------------------------------------------------
#
# Tuned conservatively; a future Phase 2 might expose these via
# ``MemorySettings``. For Phase 1 they are constants so tuning
# requires code review and test updates -- not an operator slider
# that silently changes write behavior for every deployment.

_MIN_CONTENT_LENGTH: Final[int] = 10
"""Minimum non-whitespace length for a text to be worth saving.

Short strings like "ok", "yes", "?" carry no information worth
a memory slot. The threshold is deliberately low (10) so that
legitimately concise facts ("uses uv", "based in Berlin") pass.
"""

_MAX_QUESTION_LENGTH: Final[int] = 200
"""Questions longer than this are treated as facts.

Short questions ("what's the API key?") are intent signals, not
memory candidates -- the user is ASKING, not STATING. Long
"questions" are usually prose that happens to end with "?" and
should be considered as candidate content. 200 characters is
generous enough that most real questions are correctly classified.
"""

_DUPLICATE_TOKEN_JACCARD: Final[float] = 0.75
"""Token-set Jaccard similarity for near-duplicate detection.

Two entries with Jaccard >= 0.75 are considered the SAME fact
stated slightly differently. Deliberately high: at 0.75, minor
edits like "the user prefers dark mode" vs "the user prefers
dark mode." still match (they normalize to the same token set),
but "the user prefers dark mode" vs "the user always enables
dark mode" does NOT (substantively different phrasing that
might reflect nuance worth preserving separately).
"""

_CONFLICT_TOKEN_JACCARD_MIN: Final[float] = 0.4
"""Lower bound for conflict detection's token overlap.

Conflict candidates must share AT LEAST this much vocabulary
with the existing entry -- otherwise they are about different
topics and should not be compared. Below this threshold, the
"conflict" is really "different subject", and supersede would
be wrong.
"""


# ---------------------------------------------------------------------------
# Replacement markers -- the ONLY Phase 1 conflict signal
# ---------------------------------------------------------------------------
#
# A candidate containing one of these phrases is EXPLICITLY
# declaring a change. This is the safest possible conflict signal
# because the user's own words describe the transition.
#
# Anything less explicit ("I think I prefer X now") is treated as
# a NEW preference rather than a replacement, and both entries
# are kept. See module docstring for the cost asymmetry that
# justifies this choice.
#
# Multilingual: English + Arabic + Russian, matching the
# normalizer's coverage.
_REPLACEMENT_MARKERS: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    # English
    r"no\s+longer|"
    r"changed\s+to|"
    r"switched\s+to|"
    r"updated\s+to|"
    r"now\s+use[sd]?|"
    r"instead\s+of|"
    r"rather\s+than|"
    r"replaced\s+(?:\S+\s+)?with|"
    # Arabic
    r"لم\s+أعد|"
    r"تغير\s+إلى|"
    r"تحولت\s+إلى|"
    r"بدلاً\s+من|"
    r"عوضاً\s+عن|"
    r"الآن\s+أستخدم|"
    # Russian
    r"больше\s+не|"
    r"изменилось\s+на|"
    r"переключил[ся]?\s+на|"
    r"вместо|"
    r"теперь\s+использую"
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Type classification patterns -- shared with the extractor
# ---------------------------------------------------------------------------
#
# Ordered from most-specific to least-specific. The classifier
# returns the FIRST match. The ORDER matters: PREFERENCE wins
# over SEMANTIC (because "I prefer X" is more specific than "X"),
# and DECISION wins over SEMANTIC for the same reason.
#
# These are HINTS, not authoritative classifications. The
# extractor may override based on context (e.g. a decision
# statement inside an explicit "remember that I decided..." save
# is definitely a DECISION regardless of what this returns).
#
# A test in ``tests/memory/test_policies.py`` (P2) will verify
# that each pattern reliably matches its intended type without
# false-positives on the SEMANTIC fallback.

_TYPE_HINTS: Final[List[tuple[re.Pattern[str], MemoryType]]] = [
    # ---- PREFERENCE -- "user likes/prefers/wants X" ------------
    (re.compile(
        r"(?:"
        r"\bprefers?\b|\blikes?\b|\bwants?\b|\bdislikes?\b|"
        r"\bhates?\b|\bfavou?rs?\b|\balways\b|\bnever\b|"
        r"أفضل|يفضل|تفضل|يحب|تحب|يكره|تكره|دائماً|أبداً|"
        r"предпочитает|любит|не\s+любит|всегда|никогда"
        r")",
        re.IGNORECASE,
    ), MemoryType.PREFERENCE),

    # ---- DECISION -- "we decided/chose X" ----------------------
    (re.compile(
        r"(?:"
        r"\bdecided\b|\bchose\b|\bchosen\b|\bgoing\s+with\b|"
        r"\bwe\s+will\s+use\b|\bselected\b|\bsettled\s+on\b|"
        r"قررنا|قرر|اخترنا|اختر|سنستخدم|وقع\s+الاختيار|"
        r"решили|решил|выбрали|выбрал"
        r")",
        re.IGNORECASE,
    ), MemoryType.DECISION),

    # ---- PROCEDURAL -- commands and step-by-step ---------------
    (re.compile(
        r"(?:"
        r"\brun\s+`|\bexecute\s+`|`\w+\s+[^`]+`|"
        r"\bstep\s+\d|\bfirst\b.*\bthen\b.*\bfinally\b|"
        r"شغل|نفذ|الخطوة|أولاً|ثم|أخيراً|"
        r"запустить|выполнить|шаг\s+\d|сначала|затем"
        r")",
        re.IGNORECASE,
    ), MemoryType.PROCEDURAL),

    # ---- EPISODIC -- dated events, past tense ------------------
    (re.compile(
        r"(?:"
        r"\b(?:yesterday|today|last\s+(?:week|month|year))\b|"
        r"\b\d{4}-\d{2}-\d{2}\b|"
        r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{4}\b|"
        r"\b(?:happened|occurred|was\s+deployed|failed|succeeded)\b|"
        r"أمس|اليوم|الأسبوع\s+الماضي|حدث|فشل|نجح|"
        r"вчера|сегодня|на\s+прошлой\s+неделе|случилось|произошло"
        r")",
        re.IGNORECASE,
    ), MemoryType.EPISODIC),
]


# ---------------------------------------------------------------------------
# WriteAction
# ---------------------------------------------------------------------------


class WriteAction(str, Enum):
    """The action the write pipeline should take for a candidate.

    Four actions, each with clear semantics and clear recovery
    implications:

    ``CREATE``
        The candidate is new. Persist it as a fresh entry.

    ``SKIP``
        The candidate duplicates an existing ACTIVE entry. Do not
        persist. The user sees a "already remembered" confirmation
        rather than a confusing duplicate error.

    ``SUPERSEDE``
        The candidate conflicts with an existing ACTIVE entry that
        the user has explicitly marked as replaced. Archive the
        existing entry (status=superseded) and persist the
        candidate as the new active entry. Both rows remain in the
        database for audit.

    ``REACTIVATE``
        The candidate duplicates an existing entry that is
        ARCHIVED or SUPERSEDED -- the user is re-saving something
        they previously removed or that was previously replaced.
        Set the existing entry's status back to ACTIVE and update
        it with the candidate's content (which may differ from
        the archived content in tags / importance). This preserves
        the original memory_id and creation timestamp while
        honoring the user's intent.
    """

    CREATE = "create"
    SKIP = "skip"
    SUPERSEDE = "supersede"
    REACTIVATE = "reactivate"


# ---------------------------------------------------------------------------
# WriteDecision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteDecision:
    """The decision returned by ``decide_write_action``.

    Frozen so that callers cannot mutate the decision between the
    moment it is computed and the moment it is executed. The write
    pipeline reads ``action`` to branch, ``candidate`` to persist,
    ``target_entry`` (when present) to archive / reactivate, and
    ``reason`` for diagnostics.

    Attributes
    ----------
    action
        One of the four ``WriteAction`` members.
    candidate
        The candidate memory entry that was evaluated. Echoed
        back so the caller does not need to correlate by identity
        (which breaks when the candidate is deep-copied).
    target_entry
        For SKIP / SUPERSEDE / REACTIVATE: the existing entry
        that triggered the action. ``None`` for CREATE.
    reason
        A short, non-empty, human-readable explanation of WHY
        this action was chosen. Format:
        ``"<rule>: <detail>"``. Used only for logging and
        diagnostics -- never parsed by application code.
    """

    action: WriteAction
    candidate: MemoryEntry
    target_entry: Optional[MemoryEntry] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set:
    """Return the set of lowercased word tokens in ``text``.

    Uses a simple ``\\w+`` extraction. The set (not list) makes
    Jaccard computation O(min(|a|, |b|)) rather than O(|a| * |b|).
    """
    return set(re.findall(r"\w+", text.lower()))


def _jaccard(a: set, b: set) -> float:
    """Return the Jaccard similarity of two token sets.

    ``|A ∩ B| / |A ∪ B|``. Returns 0.0 when both sets are empty
    (equivalent to "no overlap") -- this matches the caller's
    expectation that empty inputs should not produce a
    conflict-or-duplicate signal.
    """
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# ---------------------------------------------------------------------------
# is_worth_saving -- quality gate
# ---------------------------------------------------------------------------


def is_worth_saving(text: str) -> bool:
    """Return True when ``text`` is a reasonable memory candidate.

    This is the FIRST filter in the extraction pipeline. It
    rejects obvious non-candidates (empty strings, one-word
    greetings, short questions) so that later stages -- which
    cost more compute -- do not waste time on them.

    Rejection rules (ANY of these fires -> not worth saving):

      * Text is not a string, or is empty after trim.
      * Trimmed length is less than ``_MIN_CONTENT_LENGTH`` (10).
      * Text is a short question (ends with ``?`` and shorter
        than ``_MAX_QUESTION_LENGTH``).
      * Text contains no word characters after ``\\w+``
        extraction (pure punctuation / symbols).

    Explicitly NOT rejected (deliberately):

      * Compact facts of at least 10 characters -- e.g.
        "based in Berlin" (15 chars), "prefers dark mode"
        (17 chars). These are the concise memories that
        provide the most value per token.
      * Questions above the length threshold -- long "questions"
        are usually prose that happens to end with "?", and
        their content is a legitimate candidate.
      * Text with mixed scripts (Arabic + English fragments,
        etc.) -- normalizer handles these correctly.

    Very short facts (7-9 characters) such as "uses uv" or
    "is vegan" are BELOW the threshold and will NOT pass this
    filter. That is intentional and conservative for Phase 1:

      * This filter runs ONLY in the automatic extraction
        pipeline. It is NOT applied to explicit saves via the
        ``memory_save`` tool, which accept any user-confirmed
        content regardless of length.
      * Lowering the threshold to 7-8 would also admit noise
        like "yes sir", "not yet", "hmm ok" -- strings that
        are semantically ambiguous without conversation
        context.
      * Users who want short facts stored should save them
        explicitly. The extractor's job is to surface
        IMPLICIT facts from prose, and prose-level signals
        are rarely 7 characters long.

    The function is INTENTIONALLY permissive. False negatives
    (rejecting a memory that would have been useful) are worse
    than false positives (accepting text that the extractor's
    later heuristics filter out) -- because a rejected memory is
    invisibly lost, while an accepted non-memory is at worst a
    noisy extra entry that the user can delete.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped) < _MIN_CONTENT_LENGTH:
        return False
    if not _tokenize(stripped):
        return False
    if stripped.endswith("?") and len(stripped) < _MAX_QUESTION_LENGTH:
        return False
    return True


# ---------------------------------------------------------------------------
# classify_memory_type -- type hint for the extractor
# ---------------------------------------------------------------------------


def classify_memory_type(text: str) -> Optional[MemoryType]:
    """Return the most likely ``MemoryType`` for ``text``, or None.

    The classification uses ordered regex hints defined in
    ``_TYPE_HINTS``. The FIRST pattern that matches wins, so the
    ordering encodes priority (PREFERENCE > DECISION > PROCEDURAL
    > EPISODIC > SEMANTIC-fallback).

    Returns ``None`` when NO pattern matches. In that case the
    caller (extractor) is expected to either:
      * default to ``MemoryType.SEMANTIC`` (the catch-all), or
      * reject the candidate if no explicit type signal exists.

    The choice is left to the extractor because the "default to
    semantic" behavior differs by extraction context: a saved fact
    from ``memory_save`` should default to SEMANTIC; a heuristically
    extracted line from a run should probably be dropped if it
    doesn't fit any pattern.

    This function is a HINT, not an oracle. The extractor may
    override the result based on context (e.g. a candidate from an
    explicit "remember this" call is worth keeping even if the
    pattern returns None).
    """
    if not isinstance(text, str):
        return None
    for pattern, memory_type in _TYPE_HINTS:
        if pattern.search(text):
            return memory_type
    return None


# ---------------------------------------------------------------------------
# detect_duplicate
# ---------------------------------------------------------------------------


def detect_duplicate(
    candidate: MemoryEntry,
    existing: List[MemoryEntry],
) -> Optional[MemoryEntry]:
    """Return an existing entry that duplicates ``candidate``, or None.

    Two tiers of duplicate detection, checked in order:

    1. **Exact hash match.** ``candidate.content_hash`` equals
       ``existing.content_hash``. This is the definitive signal
       -- both entries normalize to the same string in the same
       scope, so they are semantically identical.

    2. **Near-duplicate.** Both entries share enough vocabulary
       (Jaccard >= ``_DUPLICATE_TOKEN_JACCARD``) and have at
       least one tag in common. Near-duplicates are "the same
       fact stated slightly differently" -- saving both would
       waste memory slots and confuse the ranker with redundant
       matches.

    Pre-condition: all entries in ``existing`` are in the SAME
    scope as ``candidate``. The caller filters by scope before
    calling this function (see module docstring).

    Returns the FIRST matching entry. When multiple duplicates
    exist (rare, usually from legacy data or import), the exact
    match is preferred over a near-duplicate, then any ACTIVE
    entry is preferred over ARCHIVED (though the caller usually
    inspects the returned entry's status to decide between SKIP
    and REACTIVATE).

    Never raises -- an empty ``existing`` list, or a candidate
    with no match, both return ``None``.
    """
    if not existing:
        return None

    # --- Tier 1: exact hash match -------------------------------------
    # Fast path, also the most confident signal.
    for entry in existing:
        if entry.content_hash == candidate.content_hash:
            return entry

    # --- Tier 2: near-duplicate (token Jaccard + tag overlap) ---------
    candidate_tokens = _tokenize(candidate.content)
    candidate_tags = set(candidate.tags)
    if not candidate_tokens:
        return None

    best_match: Optional[MemoryEntry] = None
    best_jaccard: float = 0.0

    for entry in existing:
        # Require at least one shared tag -- a pure vocabulary
        # overlap without any tag overlap is usually coincidence
        # ("the user said X" vs "the user said Y" share "the user
        # said" but are different facts).
        if candidate_tags and entry.tags:
            if not candidate_tags & set(entry.tags):
                continue

        entry_tokens = _tokenize(entry.content)
        similarity = _jaccard(candidate_tokens, entry_tokens)
        if similarity < _DUPLICATE_TOKEN_JACCARD:
            continue

        if similarity > best_jaccard:
            best_jaccard = similarity
            best_match = entry

    return best_match


# ---------------------------------------------------------------------------
# detect_conflict
# ---------------------------------------------------------------------------


def detect_conflict(
    candidate: MemoryEntry,
    existing: List[MemoryEntry],
) -> Optional[MemoryEntry]:
    """Return an existing entry that ``candidate`` conflicts with, or None.

    See module docstring for the deliberate conservatism. In
    short: this function ONLY fires when the candidate EXPLICITLY
    declares a replacement via a marker phrase ("no longer",
    "changed to", "switched to", etc.).

    Rules (ALL must hold):

      1. ``candidate.memory_type`` is PREFERENCE or DECISION.
      2. ``candidate.content`` contains a replacement marker.
      3. Some entry in ``existing`` has:
         - the same ``memory_type`` as the candidate,
         - at least one tag in common with the candidate,
         - content token overlap with the candidate of at least
           ``_CONFLICT_TOKEN_JACCARD_MIN`` (so the entries are
           demonstrably about the same topic),
         - a DIFFERENT ``content_hash`` (so it is not an exact
           duplicate -- that case is handled by
           ``detect_duplicate`` and produces SKIP/REACTIVATE,
           not SUPERSEDE).

    Pre-condition: all entries in ``existing`` are in the SAME
    scope as ``candidate``.

    Returns the FIRST matching entry. When multiple conflicts
    exist (rare), returns the one with the highest token overlap
    -- the closest topical match is the one the candidate is most
    likely replacing.

    Never raises. Returns ``None`` for non-PREFERENCE/DECISION
    candidates, or candidates without replacement markers, or
    when no topic-matching entry exists.
    """
    # --- Rule 1: only PREFERENCE / DECISION can conflict -------
    if candidate.memory_type not in (
        MemoryType.PREFERENCE,
        MemoryType.DECISION,
    ):
        return None

    # --- Rule 2: candidate must explicitly declare replacement --
    if not _REPLACEMENT_MARKERS.search(candidate.content):
        return None

    if not existing:
        return None

    # --- Rule 3: find the best topic-matching conflict target --
    candidate_tokens = _tokenize(candidate.content)
    candidate_tags = set(candidate.tags)
    if not candidate_tokens:
        return None

    best_match: Optional[MemoryEntry] = None
    best_similarity: float = 0.0

    for entry in existing:
        # Same memory_type -- a PREFERENCE cannot supersede a
        # SEMANTIC fact.
        if entry.memory_type != candidate.memory_type:
            continue

        # Different content_hash -- exact duplicates are handled
        # by detect_duplicate, not here.
        if entry.content_hash == candidate.content_hash:
            continue

        # Tag overlap required. If neither entry has tags, we
        # cannot confirm the conflict is about the same topic,
        # so we skip. This is conservative by design.
        if not candidate_tags or not entry.tags:
            continue
        if not candidate_tags & set(entry.tags):
            continue

        # Token overlap check -- ensures the conflict is topical,
        # not coincidental.
        entry_tokens = _tokenize(entry.content)
        similarity = _jaccard(candidate_tokens, entry_tokens)
        if similarity < _CONFLICT_TOKEN_JACCARD_MIN:
            continue

        if similarity > best_similarity:
            best_similarity = similarity
            best_match = entry

    return best_match


# ---------------------------------------------------------------------------
# decide_write_action -- the composed decision
# ---------------------------------------------------------------------------


def decide_write_action(
    candidate: MemoryEntry,
    existing: List[MemoryEntry],
) -> WriteDecision:
    """Decide what the caller should do with ``candidate``.

    The composed decision point, called by ``WritePipeline``
    (P1.7). The pipeline has already retrieved existing entries
    for the candidate's scope (via ``provider.search`` or
    ``provider.list_by_scope``). This function returns a
    ``WriteDecision`` that the pipeline then executes.

    Decision tree (ORDERED, first match wins):

      1. **Exact/near duplicate of an ACTIVE entry** -> ``SKIP``.
         The user already has this fact; re-saving it would be
         noise.

      2. **Exact/near duplicate of an ARCHIVED or SUPERSEDED
         entry** -> ``REACTIVATE``. The user is re-asserting
         something they previously removed; honor the intent by
         bringing the archived entry back.

      3. **Conflict with an ACTIVE entry** -> ``SUPERSEDE``.
         The candidate explicitly declares replacement; the old
         entry should be archived to preserve audit while the new
         one takes over.

      4. **None of the above** -> ``CREATE``. This is the
         catch-all for genuinely new memories.

    Note on ordering: step 1 (SKIP) comes before step 3
    (SUPERSEDE) because an exact duplicate should never be
    treated as a conflict -- the user is re-asserting the SAME
    fact, which is a no-op, not a change.

    Note on status ordering within step 1 vs step 2: an ACTIVE
    duplicate is SKIP; an ARCHIVED duplicate is REACTIVATE. Same
    content, different status of the target => different action.
    This is intentional: SKIP means "nothing to do"; REACTIVATE
    means "the user wants this back".

    Returns a frozen ``WriteDecision``. Always sets
    ``candidate``; sets ``target_entry`` for SKIP, SUPERSEDE, and
    REACTIVATE; always sets a non-empty ``reason``.
    """
    # --- Step 1 & 2: duplicate detection ------------------------------
    duplicate = detect_duplicate(candidate, existing)
    if duplicate is not None:
        if duplicate.status == MemoryStatus.ACTIVE:
            return WriteDecision(
                action=WriteAction.SKIP,
                candidate=candidate,
                target_entry=duplicate,
                reason=(
                    f"duplicate_of_active:{duplicate.memory_id}:"
                    f"content_hash={duplicate.content_hash[:8]}"
                ),
            )
        # ARCHIVED or SUPERSEDED -- user is re-asserting.
        return WriteDecision(
            action=WriteAction.REACTIVATE,
            candidate=candidate,
            target_entry=duplicate,
            reason=(
                f"reactivate:{duplicate.memory_id}:"
                f"previous_status={duplicate.status.value}"
            ),
        )

    # --- Step 3: conflict detection ----------------------------------
    conflict = detect_conflict(candidate, existing)
    if conflict is not None:
        # Only ACTIVE entries can be superseded. An archived entry
        # that would conflict is already inert -- the CREATE will
        # succeed without it.
        if conflict.status == MemoryStatus.ACTIVE:
            return WriteDecision(
                action=WriteAction.SUPERSEDE,
                candidate=candidate,
                target_entry=conflict,
                reason=(
                    f"conflict_with_active:{conflict.memory_id}:"
                    f"replacement_marker_detected"
                ),
            )

    # --- Step 4: default -- create new --------------------------------
    return WriteDecision(
        action=WriteAction.CREATE,
        candidate=candidate,
        target_entry=None,
        reason="no_duplicate_no_conflict",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "WriteAction",
    "WriteDecision",
    "is_worth_saving",
    "classify_memory_type",
    "detect_duplicate",
    "detect_conflict",
    "decide_write_action",
]
