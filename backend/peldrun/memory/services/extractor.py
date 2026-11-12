# backend/peldrun/memory/services/extractor.py
"""
PELDRUN Memory Platform - Heuristic Memory Extractor (Phase 1).

Converts conversation turns into memory candidate entries using
DETERMINISTIC regex heuristics. No LLM, no API calls, no I/O.

Scope of Phase 1 extraction
---------------------------

The extractor recognizes two kinds of memories:

    1. **Explicit user saves** -- the user literally typed
       "remember that X", "note to self: Y", "запомни что Z".
       These produce ``MemorySourceType.EXPLICIT_USER`` candidates
       with high importance (80), because the user has, by typing
       the marker, explicitly asked for X to be persisted.

    2. **Implicit preference/decision statements** -- the user
       typed "I prefer X" or "we decided Y" WITHOUT a remember
       marker. These produce ``MemorySourceType.EXTRACTION``
       candidates with lower importance (60-70), because the
       heuristic is inferring intent from phrasing.

Every other kind of text is SKIPPED in Phase 1:

    * General facts ("the API key is in .env") -- too easy to
      over-extract from prose; deferred to Phase 2's LLM-based
      extraction.
    * Opinions / analysis -- the model's own reasoning is
      discarded; only user assertions matter.
    * Assistant turns -- the assistant says "OK, I'll remember
      that"; the SOURCE is the user's previous turn, not the
      acknowledgment.

This conservatism is the same choice made in ``policies.py``:
better to miss an implicit fact than to pollute the memory
store with noise. Phase 2 (LLM-based) will relax the rules
with better signal.

Extraction markers (multilingual)
----------------------------------

The markers below are the SAME phrases ``policies._REPLACEMENT_MARKERS``
treats as conflict signals -- but the SEMANTICS differ:

    * ``policies._REPLACEMENT_MARKERS`` answers "does this
      statement describe a CHANGE from a previous state?"
    * ``_REMEMBER_MARKERS`` (below) answers "does this statement
      ask the system to REMEMBER something?"

A single statement could trigger both -- the user might say
"remember that I no longer use Python". That produces:
    * an EXPLICIT_USER candidate ("I no longer use Python"), and
    * when passed through ``decide_write_action``, a SUPERSEDE
      decision against the existing "uses Python" entry.

The two modules compose; neither subsumes the other.

Turn shape handling
-------------------

``turns`` may be shaped in two ways, both supported:

    * **Turn format** -- each element is
      ``{"prompt": "user text", "result": "assistant text", ...}``
      (the format produced by older chat serialization).
    * **Message format** -- each element is
      ``{"role": "user"|"assistant"|"system", "content": "...", ...}``
      (the format produced by newer serialization and by
      ``/messages`` arrays).

The extractor inspects ONLY user-role text from either format.
Assistant and system turns are skipped (source authority --
see ``_extract_from_turn``).

Design invariants
-----------------

1. **Pure function.** ``extract_candidates`` reads its input
   and produces a new list. It does not touch storage, the
   filesystem, or the clock (timestamps come from the turn data
   via ``_resolve_turn_timestamp``; when absent, ``None`` is
   used -- no ``datetime.now()`` inside this module).

2. **Deterministic.** Same input -> same output, across runs,
   Python versions, and platforms. All regexes are compiled
   once at module load.

3. **Idempotent for the caller.** Calling twice on the same
   ``turns`` produces two independent candidate lists with
   identical content. The caller (``WritePipeline``) is
   responsible for cross-batch deduplication via
   ``decide_write_action`` -- the extractor does NOT consult
   the existing store.

4. **Bounded.** ``max_candidates`` caps the output to prevent
   runaway extraction on long conversations. The cap applies
   AFTER prioritization: explicit-user candidates are emitted
   first, then preference, then decision.

5. **Respects ``min_turns``.** Nothing is extracted from a
   conversation with fewer than ``min_turns`` user turns --
   the plan mandates this because short chats rarely contain
   durable facts worth persisting. This is the pre-filter
   that makes the "auto extraction enabled" flag safe to
   default-on in a future phase.

6. **Provenance is complete.** Every candidate carries
   ``source_chat_id``, ``source_run_id`` (when known),
   ``source_turn_id``, and the correct ``source_type``. Audit
   reconstruction works for every candidate.

Non-extraction of assistant turns -- the rationale
--------------------------------------------------

The assistant often says "I'll remember that" or "Noted."
These are ACKNOWLEDGMENTS, not sources of truth. If the
assistant's acknowledgment were extracted as a memory, we
would store the SAME fact twice (once from the user's original
statement, once from the acknowledgment) with slightly
different phrasing, defeating deduplication and polluting the
store.

The rule is therefore strict: **only user-role text is
inspected**. This matches the plan's specification of
``extract_candidates(turns)`` as extracting "candidate
memories from turns" -- the phrase "from turns" implies "the
turn's USER prompt", not "any text the assistant produced".

What Phase 2 will add
---------------------

    * LLM-based extraction of implicit facts ("the API key is
      in .env" without a remember marker).
    * Confidence scoring per candidate (Phase 1 uses fixed
      values by source type).
    * Entity extraction for auto-tagging (Phase 1 tags are
      derived from ``memory_type`` only).
    * Multi-language fact detection beyond EN/AR/RU.
    * Cross-turn context (e.g. "as I mentioned before, X" --
      X might not be in the current turn).

Phase 1's contribution is a SAFE baseline: a candidate list
that is small, precise, and guaranteed non-harmful even if
auto-extraction is accidentally enabled on a deployment that
wasn't ready for it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Final, List, Optional

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
)
from peldrun.memory.services.policies import (
    classify_memory_type,
    is_worth_saving,
)


logger = logging.getLogger("peldrun.memory.services.extractor")


# ---------------------------------------------------------------------------
# Extraction markers (compiled once)
# ---------------------------------------------------------------------------
#
# The marker is matched at the START of the user text, optionally
# after a polite prefix ("please", "hey, "). Everything AFTER the
# marker (up to the next sentence terminator, per turn text) is the
# candidate content.
#
# The pattern is deliberately ANCHORED to avoid over-extraction
# from a sentence that merely mentions "remember" mid-paragraph
# ("I don't remember what he said" should NOT trigger extraction;
# the user is describing a failure to recall, not asking us to
# remember something).
#
# Case-insensitive: users type "Remember" and "remember" and
# "REMEMBER" interchangeably.
#
# CRITICAL invariant: this pattern must NEVER match neutral text.
# See ``_NEUTRAL_TEXTS`` in the test suite for the defensive list.
_REMEMBER_MARKER: Final[re.Pattern[str]] = re.compile(
    r"^\s*"
    r"(?:please\s+|pls\s+|hey[, ]+|hi[, ]+|ok[, ]+|okay[, ]+)?"
    r"(?:"
    # ---- English ----
    r"remember\b|"
    r"note\s+to\s+self\b|"
    r"don'?t\s+forget\b|"
    r"keep\s+in\s+mind\b|"
    # ---- Arabic ----
    r"تذكر|تذكري|تذكروا|احفظ|احفظي|لا\s+تنس(?:ى|ي)?\b|"
    # ---- Russian ----
    r"запомни(?:те)?\b|не\s+забудь(?:те)?\b|имей\s+в\s+виду\b"
    r")"
    r"\s*"
    r"(?:that\b|أن\b|что\b|:|,|—|–|-)?"
    r"\s*",
    re.IGNORECASE,
)
"""Anchored matcher for explicit "remember X" phrasing.

Anchored to ``^`` because a remember-marker mid-sentence is
almost always descriptive ("I forgot to remember..."), not
instructional. The one exception we tolerate is a polite
prefix ("please remember", "hey remember") which is common in
real usage and does not change the intent.
"""


#: Sentence terminator: the candidate content from a remember
#: marker runs from the end of the marker up to the first of
#: these. Prevents capturing a huge multi-sentence paragraph.
#:
#: A punctuation mark counts as a terminator ONLY when it is
#: followed by whitespace or the end of the input. This
#: distinguishes real sentence ends ("port 8080. Done.") from
#: punctuation embedded inside tokens -- file extensions
#: (".env", ".py"), decimal numbers ("1.80"), dotted identifiers
#: ("example.com"), emails ("user@example.com"), and common
#: abbreviations. Without the lookahead, "remember that my key
#: is in .env" would truncate to "my key is in", silently
#: discarding the filename -- the most important part of the
#: fact being remembered.
#:
#: Arabic ``؟`` (U+061F) is included in the class; in natural
#: Arabic prose it is always followed by space or end-of-input,
#: so the lookahead applies to it identically.
_SENTENCE_TERMINATOR: Final[re.Pattern[str]] = re.compile(
    r"[.!?؟](?=\s|$)"
)


# ---------------------------------------------------------------------------
# Turn text extraction -- tolerant of two turn shapes
# ---------------------------------------------------------------------------


def _extract_user_text_from_turn(
    turn: Dict[str, Any],
) -> Optional[str]:
    """Return the USER-role text of ``turn``, or None.

    Handles both turn shapes documented in the module docstring.
    Returns None when the turn has no user text (assistant-only
    turn, system message, empty turn, malformed turn).

    Priority order within turn format:
        ``prompt`` > ``user`` > ``query``

    Message format: extracts the FIRST ``role=="user"`` message's
    ``content``. A message array with multiple user messages
    (rare, but possible after compaction) still returns the first
    one -- subsequent user messages belong to separate extraction
    units and are handled by their own turns.
    """
    if not isinstance(turn, dict):
        return None

    # --- Turn format ------------------------------------------------
    if "prompt" in turn:
        return _coerce_to_text(turn.get("prompt"))
    if "user" in turn:
        return _coerce_to_text(turn.get("user"))
    if "query" in turn:
        return _coerce_to_text(turn.get("query"))

    # --- Message format --------------------------------------------
    if turn.get("role") == "user":
        return _coerce_to_text(turn.get("content"))

    # --- Messages array (nested) ------------------------------------
    messages = turn.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                text = _coerce_to_text(msg.get("content"))
                if text:
                    return text

    return None


def _coerce_to_text(value: Any) -> Optional[str]:
    """Coerce an arbitrary input value to a stripped string or None.

    Handles:
      * ``str`` -- stripped directly,
      * ``list`` of content parts (multimodal format) -- joined
        text parts, image parts ignored,
      * ``None`` / other -- returns None.

    The multimodal case matters because user messages in modern
    chat sessions can carry ``[{"type": "text", "text": "..."},
    {"type": "image_url", ...}]``. We extract the text parts only;
    image content is not memory material.
    """
    if value is None:
        return None

    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None

    if isinstance(value, list):
        text_parts: List[str] = []
        for part in value:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "text":
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        text_parts.append(text.strip())
                # Non-text parts (image_url, audio, ...) are skipped.
        if not text_parts:
            return None
        return " ".join(text_parts)

    return None


def _resolve_turn_id(turn: Dict[str, Any], index: int) -> str:
    """Return a stable turn identifier, preferring the turn's own id."""
    for key in ("id", "turn_id", "job_id"):
        val = turn.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return f"turn_{index + 1}"


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


def _make_candidate(
    *,
    content: str,
    memory_type: MemoryType,
    scope_type: MemoryScope,
    scope_id: str,
    source_type: MemorySourceType,
    source_chat_id: str,
    source_run_id: Optional[str],
    source_turn_id: str,
    importance: int,
) -> Optional[MemoryEntry]:
    """Build a ``MemoryEntry`` candidate, or return None on rejection.

    Returns None when:
      * ``content`` is empty / whitespace,
      * the domain constructor rejects the content (e.g. exceeds
        the maximum length),

    A rejection is NOT an error -- it means "this candidate is not
    viable; skip it". Logged at DEBUG level so a noisy conversation
    does not spam WARNING.
    """
    try:
        entry = MemoryEntry.create(
            content=content,
            memory_type=memory_type,
            scope_type=scope_type,
            scope_id=scope_id,
            provenance=MemoryProvenance(
                source_type=source_type,
                source_chat_id=source_chat_id,
                source_run_id=source_run_id,
                source_turn_id=source_turn_id,
            ),
            importance=importance,
            confidence=(
                0.95 if source_type == MemorySourceType.EXPLICIT_USER
                else 0.7  # heuristics: still decent, but not user-asserted
            ),
            tags=[memory_type.value],
        )
        return entry
    except (ValueError, TypeError) as exc:
        logger.debug(
            "Extractor skipping candidate (turn=%r, source=%s): %s",
            source_turn_id,
            source_type.value,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Per-turn extraction
# ---------------------------------------------------------------------------


def _extract_explicit_remember(
    user_text: str,
) -> Optional[str]:
    """Return the content after a remember-marker, or None.

    The content extends from the end of the marker up to the first
    sentence terminator. This handles cases like:

        "Remember that my API key is in .env. Don't share it."
        -> "my API key is in .env"

    A bare marker ("remember", "remember that") with no following
    content returns None -- nothing to extract.
    """
    match = _REMEMBER_MARKER.match(user_text)
    if not match:
        return None

    remainder = user_text[match.end():].strip()
    if not remainder:
        return None

    # Truncate at the first sentence terminator. Using ``search``
    # (not ``match``) so the terminator can be anywhere in the
    # remainder.
    term_match = _SENTENCE_TERMINATOR.search(remainder)
    if term_match:
        remainder = remainder[: term_match.start()].strip()

    return remainder or None


def _extract_from_turn(
    turn: Dict[str, Any],
    index: int,
    *,
    scope_type: MemoryScope,
    scope_id: str,
    chat_id: str,
    run_id: Optional[str],
) -> Optional[MemoryEntry]:
    """Attempt to extract ONE candidate from a single turn.

    Extraction priority (first match wins):

      1. **Explicit remember marker** -> EXPLICIT_USER, importance 80.
      2. **Implicit preference statement** -> EXTRACTION, importance 60.
      3. **Implicit decision statement** -> EXTRACTION, importance 70.
      4. No match -> None.

    The "first match wins" order is deliberate: an explicit marker
    is always more authoritative than an inferred type. A turn
    reading "remember that I prefer dark mode" is classified as
    EXPLICIT_USER (marker wins) even though the content also
    matches the PREFERENCE heuristic.

    The single-candidate-per-turn limitation is Phase 1's
    conservatism: multi-sentence turns with multiple memories
    would need disambiguation that only Phase 2's LLM can
    provide. A user who wants two facts remembered can use
    ``memory_save`` twice.
    """
    user_text = _extract_user_text_from_turn(turn)
    if not user_text:
        return None

    if not is_worth_saving(user_text):
        return None

    turn_id = _resolve_turn_id(turn, index)

    # ---- Tier 1: explicit remember marker -----------------------------
    explicit_content = _extract_explicit_remember(user_text)
    if explicit_content:
        if not is_worth_saving(explicit_content):
            # Marker was present but the extracted content is too
            # short / looks like a question. Skip -- the user's
            # "remember?" was rhetorical, not a save request.
            return None
        memory_type = (
            classify_memory_type(explicit_content) or MemoryType.SEMANTIC
        )
        return _make_candidate(
            content=explicit_content,
            memory_type=memory_type,
            scope_type=scope_type,
            scope_id=scope_id,
            source_type=MemorySourceType.EXPLICIT_USER,
            source_chat_id=chat_id,
            source_run_id=run_id,
            source_turn_id=turn_id,
            importance=80,
        )

    # ---- Tier 2/3: implicit preference or decision --------------------
    #
    # Only fires when the user's text unambiguously reads as a
    # preference or decision. ``classify_memory_type`` returns
    # SEMANTIC / EPISODIC / PROCEDURAL for other cases, which we
    # deliberately do not extract in Phase 1.
    #
    # Episodic and procedural facts are almost always derivable
    # from the conversation history itself; they rarely need a
    # separate memory slot. Preference and decision, by contrast,
    # are exactly the kinds of things the LLM needs reminded of
    # in future turns.
    classified = classify_memory_type(user_text)
    if classified == MemoryType.PREFERENCE:
        return _make_candidate(
            content=user_text,
            memory_type=MemoryType.PREFERENCE,
            scope_type=scope_type,
            scope_id=scope_id,
            source_type=MemorySourceType.EXTRACTION,
            source_chat_id=chat_id,
            source_run_id=run_id,
            source_turn_id=turn_id,
            importance=60,
        )
    if classified == MemoryType.DECISION:
        return _make_candidate(
            content=user_text,
            memory_type=MemoryType.DECISION,
            scope_type=scope_type,
            scope_id=scope_id,
            source_type=MemorySourceType.EXTRACTION,
            source_chat_id=chat_id,
            source_run_id=run_id,
            source_turn_id=turn_id,
            importance=70,
        )

    # No phase-1 rule matches -> skip.
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_candidates(
    turns: List[Dict[str, Any]],
    *,
    scope_type: MemoryScope,
    scope_id: str,
    chat_id: str,
    run_id: Optional[str] = None,
    min_turns: int = 3,
    max_candidates: int = 5,
) -> List[MemoryEntry]:
    """Extract memory candidates from a list of conversation turns.

    Parameters
    ----------
    turns
        Conversation turns, in chronological order. Each turn is
        either turn-shaped (``prompt``/``result``) or
        message-shaped (``role``/``content``). See the module
        docstring for the full format handling.

    scope_type, scope_id
        The scope every candidate will be written into. Fixed per
        call -- Phase 1 extraction is not cross-scope.

    chat_id
        The chat session id. Recorded as
        ``provenance.source_chat_id`` on every candidate.

    run_id
        Optional agent run id. Recorded as
        ``provenance.source_run_id`` when provided.

    min_turns
        Minimum number of USER turns required for extraction to
        run at all. Below this threshold, the conversation is
        considered too short to contain durable facts. Default 3
        (matches ``MemorySettings.auto_extraction_min_turns``).

    max_candidates
        Maximum number of candidates to return. Applied AFTER
        prioritization: explicit-user candidates come first,
        then preference, then decision. Default 5.

    Returns
    -------
    List[MemoryEntry]
        Freshly-created candidate entries. NOT yet persisted --
        the caller (P1.7's ``WritePipeline``) is responsible for
        running ``decide_write_action`` on each one and executing
        the resulting CREATE / SKIP / SUPERSEDE / REACTIVATE.

    Behavior summary
    ----------------
    * If fewer than ``min_turns`` user turns exist -> return [].
    * Iterate turns in order, extracting at most one candidate
      per turn (see ``_extract_from_turn``).
    * Sort candidates by (explicit_marker FIRST, then importance
      DESC, then turn order). This means the ``max_candidates``
      cap never truncates an EXPLICIT_USER candidate in favor
      of an EXTRACTION one.
    * Return the top ``max_candidates`` after deduplication
      (same normalized content -> keep first / most authoritative).

    Why prioritization happens BEFORE the cap
    -----------------------------------------
    A conversation with 20 turns might produce 8 candidates,
    6 of them from low-confidence heuristics and 2 from
    explicit "remember X" markers. The user's actual memory
    budget wants those 2 explicit ones even if the cap is 5.
    Applying the cap at the END (after sort) ensures this.

    Non-behavior on ``max_candidates=0``
    ------------------------------------
    Returns [] regardless of what the turns contain. Useful for
    a future "extraction disabled by config" fast path where
    the caller wants to exercise the extraction machinery
    without spending any memory slots.
    """
    # ---- Structural validation ----------------------------------------
    if not isinstance(turns, list):
        logger.debug(
            "extract_candidates: turns is not a list (%s); "
            "returning empty.",
            type(turns).__name__,
        )
        return []
    if not isinstance(scope_type, MemoryScope):
        raise TypeError(
            f"scope_type must be MemoryScope, got "
            f"{type(scope_type).__name__}"
        )
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise TypeError(
            f"scope_id must be a non-empty string, got {scope_id!r}"
        )
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise TypeError(
            f"chat_id must be a non-empty string, got {chat_id!r}"
        )

    if max_candidates <= 0:
        return []

    if min_turns < 0:
        raise TypeError(
            f"min_turns must be >= 0, got {min_turns!r}"
        )

    # ---- Count user turns to enforce min_turns ------------------------
    user_turn_count = sum(
        1 for t in turns if _extract_user_text_from_turn(t)
    )
    if user_turn_count < min_turns:
        logger.debug(
            "extract_candidates: %d user turns < min_turns=%d; "
            "returning empty.",
            user_turn_count,
            min_turns,
        )
        return []

    # ---- Per-turn extraction ------------------------------------------
    #
    # The tuple carried forward is (candidate, priority, original_index).
    # Priority is a sortable integer:
    #   3 = explicit remember marker (highest)
    #   2 = decision heuristic
    #   1 = preference heuristic
    #   0 = not extractable (never appears -- filtered above)
    #
    # The original_index is a tie-breaker so that two equally-prioritized
    # candidates preserve chronological order (earlier turn wins).
    extracted: List[tuple] = []
    for idx, turn in enumerate(turns):
        candidate = _extract_from_turn(
            turn,
            idx,
            scope_type=scope_type,
            scope_id=scope_id,
            chat_id=chat_id,
            run_id=run_id,
        )
        if candidate is None:
            continue

        if candidate.provenance.source_type == MemorySourceType.EXPLICIT_USER:
            priority = 3
        elif candidate.memory_type == MemoryType.DECISION:
            priority = 2
        elif candidate.memory_type == MemoryType.PREFERENCE:
            priority = 1
        else:
            # Should never happen (extractor only emits the three
            # types above), but defensive: emit at lowest priority.
            priority = 0

        extracted.append((candidate, priority, idx))

    if not extracted:
        return []

    # ---- Sort by (priority DESC, original_index ASC) ------------------
    extracted.sort(key=lambda t: (-t[1], t[2]))

    # ---- Dedupe by content_hash (preserve first = highest priority) ---
    seen_hashes: set = set()
    deduped: List[MemoryEntry] = []
    for candidate, _priority, _idx in extracted:
        if candidate.content_hash in seen_hashes:
            continue
        seen_hashes.add(candidate.content_hash)
        deduped.append(candidate)
        if len(deduped) >= max_candidates:
            break

    logger.debug(
        "extract_candidates: %d user turns -> %d candidates "
        "(capped at %d).",
        user_turn_count,
        len(deduped),
        max_candidates,
    )
    return deduped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "extract_candidates",
]
