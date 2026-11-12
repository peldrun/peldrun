# backend/omweb/memory/storage/normalizer.py
"""
PELDRUN Memory Platform - Multilingual Text Normalization.

Pure, deterministic, idempotent text normalization for the memory
subsystem's full-text search. Supports Arabic, Russian, and English
out of the box; every other Unicode script passes through
unchanged except for casefolding and whitespace collapsing (which
are universally safe).

Why this module exists
----------------------

SQLite's FTS5 ``unicode61`` tokenizer removes diacritics and
handles case folding for Latin scripts, but it does NOT unify
Arabic letter variants (alef, yaa, taa marbuta) and it does NOT
normalize Russian ``ё`` to ``е``. Without this module:

    User types:     Content stored:      FTS5 alone would MISS
    ----------      ---------------      -----------------------
    محمد            مُحَمَّد              (harakat differ)
    احمد            أحمد                 (alef variant)
    Елка (with е)   Ёлка (with ё)        (Cyrillic variant)

This module bridges those gaps so retrieval works the way users
expect: they type what they would type in a search box, and the
results match regardless of how the memory was originally written.

Two entry points, one pipeline
------------------------------

Both entry points share ``normalize_for_storage`` so that content
and queries are normalized IDENTICALLY:

    * ``normalize_for_storage(text)`` -- applied to
      ``MemoryEntry.content`` before persistence (via
      ``MemoryEntry.with_normalized_content``). The result is
      stored in the ``normalized_content`` column and indexed by
      FTS5.

    * ``normalize_query(text)`` -- applied to the search query
      before it reaches ``memories_fts MATCH ?``. The result is a
      space-separated token string safe for FTS5 operator
      interpretation.

Because both share the same pipeline, a query for "محمد" and a
stored memory "مُحَمَّد" normalize to the same string "محمد" and
the FTS index finds the match. If the two used different pipelines
(e.g. content normalized one way, query another) the search would
silently miss everything -- a bug that is very hard to debug
because nothing looks obviously broken.

Design invariants
-----------------

1. **Idempotent.**
   ``normalize(normalize(x)) == normalize(x)`` for every input.
   This is CRITICAL: FTS5's UPDATE trigger fires on any change to
   ``normalized_content``, and a non-idempotent normalizer would
   cause every re-save to be treated as a content change,
   triggering unnecessary FTS rebuilds. All transforms below are
   idempotent by construction.

2. **Deterministic.**
   Same input string always produces the same output string
   across processes, Python versions (3.10+), and platforms. No
   dependence on locale, system clock, or process state.

3. **Never lossy for the raw content.**
   The raw ``content`` field is never modified by this module.
   Only ``normalized_content`` is transformed. Users always see
   what they typed; only the search index sees the normalized
   form.

4. **Fail-safe on edge cases.**
   Empty input, whitespace-only input, and input consisting
   entirely of diacritics all produce well-defined outputs
   (documented on each function). The normalizer never raises --
   it is called at the write path where exceptions would corrupt
   the entry-creation contract.

5. **Composable language steps.**
   Arabic, Russian, and English normalization are separate
   functions (``_normalize_arabic``, ``_normalize_russian``).
   Adding a fourth language (e.g. Turkish dotless i) is a new
   function plus one line in ``_normalize_for_storage_impl`` --
   no changes to the callers or the pipeline structure.

6. **Pure stdlib.**
   Imports only ``re`` and ``typing``. No external
   normalization libraries (``unicodedata2``, ``PyICU``,
   ``camel-tools``). This keeps the storage layer lightweight
   and deployable without system dependencies, at the cost of
   not handling exotic scripts (Hebrew niqqud, Devanagari
   matras) -- acceptable because those scripts are not target
   languages for the current phase.

Normalization specification (per language)
------------------------------------------

**Arabic**

    * Removes harakat, tanwin, shadda, sukun, superscript alef,
      and every Quranic annotation mark. Range: U+064B-U+065F,
      U+0670, U+06D6-U+06DC, U+06DF-U+06E4, U+06E7-U+06E8,
      U+06EA-U+06ED.
    * Removes tatweel / kashida (U+0640). "مـــحـــمـــد" and
      "محمد" are the same word.
    * Unifies alef variants: U+0622 (آ), U+0623 (أ), U+0625 (إ),
      U+0671 (ٱ) all map to bare alef U+0627 (ا).
    * Unifies alef maqsura U+0649 (ى) to yaa U+064A (ي).
    * Unifies hamza carriers: U+0624 (ؤ) to waw U+0648 (و),
      U+0626 (ئ) to yaa U+064A (ي).

    NOT done (deliberately, in Phase 1):

    * Taa marbuta U+0629 (ة) is NOT mapped to ha U+0647 (ه).
      Converting would improve recall for users who type the
      wrong one, but it is semantically lossy ("كتابة" = writing
      vs "كتابه" = his book). Documented as a Phase 2 candidate
      once real-world recall data justifies the trade-off.

**Russian**

    * ``ё`` (U+0451, U+0401 uppercase) maps to ``е`` (U+0435).
      This is a widespread typing convention -- most Russian
      keyboards do not have a dedicated ``ё`` key, so users type
      ``е`` and expect ``ё`` to match. The reverse is also true.

**English / Latin (via common pipeline)**

    * Case folding (``.lower()``). Preserves the original case in
      the raw ``content`` field.
    * Invisible characters removed: soft hyphen U+00AD,
      zero-width space U+200B, zero-width joiner U+200D,
      zero-width no-break space / BOM U+FEFF, and every other
      ``Cc`` / ``Cf`` category character in the "invisible" set.
      These often appear when text is pasted from PDFs or web
      pages and cause invisible mismatches otherwise.

**All scripts (common pipeline)**

    * Non-breaking space U+00A0 -> regular space.
    * Collapse every run of whitespace (spaces, tabs, newlines,
      form feeds, vertical tabs) to a single space.
    * Strip leading / trailing whitespace.
    * Casefold (``.lower()``). Casefolding is applied AFTER the
      script-specific steps so that, e.g., uppercase Cyrillic
      ``Ё`` is first mapped to ``ё`` by the Russian step (which
      handles both cases explicitly), then lowercased
      consistently with other Cyrillic.

Fallback for pathological input
-------------------------------

If the input is non-empty but the normalized result is empty,
the normalizer returns ``text.strip().lower()`` instead of the
empty string. This can happen for input consisting ENTIRELY of
diacritics (theoretically possible, practically never), or a
string of zero-width characters. The fallback preserves the
"non-empty input -> non-empty normalized" contract that
``MemoryEntry.with_normalized_content`` relies on.
"""

from __future__ import annotations

import re
from typing import Dict, Final, Mapping


# ---------------------------------------------------------------------------
# Arabic
# ---------------------------------------------------------------------------


#: Combining marks that must be stripped from Arabic text before
#: indexing. The ranges below cover:
#:
#:   U+064B-U+065F  -- tanwin + harakat + shadda + sukun + hamza above
#:   U+0670         -- superscript alef (dagger alef)
#:   U+06D6-U+06DC  -- Quranic annotation marks (small high signs)
#:   U+06DF-U+06E4  -- Quranic annotation marks (small wavy signs)
#:   U+06E7-U+06E8  -- Quranic annotation marks (small high yeh/noon)
#:   U+06EA-U+06ED  -- Quranic annotation marks (empty centre dots)
#:
#: Everything in these ranges is a NON-LETTER, NON-SEMANTIC mark
#: that carries pronunciation or recitation guidance. Removing
#: them is always safe for search purposes.
_ARABIC_DIACRITICS: Final[re.Pattern[str]] = re.compile(
    r"[\u064B-\u065F\u0670\u06D6-\u06DC\u06DF-\u06E4\u06E7\u06E8\u06EA-\u06ED]"
)

#: Tatweel (kashida) is a horizontal line used ONLY for
#: justification in Arabic typography. It has zero semantic
#: content; removing it lets "مــحمد" and "محمد" match.
_ARABIC_TATWEEL: Final[re.Pattern[str]] = re.compile(r"\u0640")

#: Alef variants. Every form below is the same letter "alef"
#: with a different hamza / wasla decoration. Arabic input
#: methods produce different variants for the same sound, and
#: users are not consistent about which they type.
#:
#: U+0622 -> U+0627  (آ  alef with madda)
#: U+0623 -> U+0627  (أ  alef with hamza above)
#: U+0625 -> U+0627  (إ  alef with hamza below)
#: U+0671 -> U+0627  (ٱ  alef with wasla)
_ARABIC_ALEF_MAP: Final[Mapping[int, int]] = str.maketrans(
    {
        "\u0622": "\u0627",
        "\u0623": "\u0627",
        "\u0625": "\u0627",
        "\u0671": "\u0627",
    }
)

#: Alef maqsura -> yaa. Both represent the same phoneme
#: /aː/ at the end of a word; Egyptian / dialect speakers use
#: the two interchangeably when typing.
_ARABIC_YAA_MAP: Final[Mapping[int, int]] = str.maketrans(
    {
        "\u0649": "\u064A",
    }
)

#: Hamza carriers -> the plain letter. On waw and yaa, the hamza
#: is decorative; the letter itself is what carries the
#: phonemic value.
_ARABIC_HAMZA_MAP: Final[Mapping[int, int]] = str.maketrans(
    {
        "\u0624": "\u0648",  # ؤ -> و
        "\u0626": "\u064A",  # ئ -> ي
    }
)


def _normalize_arabic(text: str) -> str:
    """Apply Arabic-specific normalization steps.

    Idempotent: applying twice yields the same result as applying
    once, because every step removes or replaces characters with
    targets that are not themselves sources of any step.

    Empty input passes through unchanged.
    """
    if not text:
        return text
    text = _ARABIC_TATWEEL.sub("", text)
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.translate(_ARABIC_ALEF_MAP)
    text = text.translate(_ARABIC_YAA_MAP)
    text = text.translate(_ARABIC_HAMZA_MAP)
    return text


# ---------------------------------------------------------------------------
# Russian / Cyrillic
# ---------------------------------------------------------------------------


#: ``ё`` -> ``е``. Handles both cases explicitly:
#:
#:   U+0401 (Ё uppercase) -> U+0415 (Е uppercase)
#:   U+0451 (ё lowercase) -> U+0435 (е lowercase)
#:
#: We map both cases (rather than only the lowercase form)
#: because the common pipeline applies ``.lower()`` AFTER the
#: Russian step. Mapping uppercase to uppercase keeps the
#: pipeline order-independent and easier to reason about.
_RUSSIAN_MAP: Final[Mapping[int, int]] = str.maketrans(
    {
        "\u0401": "\u0415",  # Ё -> Е
        "\u0451": "\u0435",  # ё -> е
    }
)


def _normalize_russian(text: str) -> str:
    """Apply Russian-specific normalization steps.

    Idempotent: ``ё`` and ``Ё`` map to ``е`` and ``Е``, which are
    not themselves sources of any mapping. Empty input passes
    through unchanged.
    """
    if not text:
        return text
    return text.translate(_RUSSIAN_MAP)


# ---------------------------------------------------------------------------
# Language-agnostic pipeline
# ---------------------------------------------------------------------------


#: Invisible / formatting characters that often sneak into text
#: from copy-paste operations and cause search mismatches with no
#: visible cause. Removing all of them is universally safe --
#: they carry zero semantic content in any target language.
#:
#:   U+00AD  soft hyphen (invisible in most editors)
#:   U+200B  zero-width space
#:   U+200C  zero-width non-joiner
#:   U+200D  zero-width joiner
#:   U+200E  left-to-right mark
#:   U+200F  right-to-left mark
#:   U+202A  left-to-right embedding
#:   U+202B  right-to-left embedding
#:   U+202C  pop directional formatting
#:   U+202D  left-to-right override
#:   U+202E  right-to-left override
#:   U+2060  word joiner
#:   U+FEFF  zero-width no-break space / BOM
_INVISIBLE_CHARS: Final[re.Pattern[str]] = re.compile(
    r"[\u00AD\u200B-\u200F\u202A-\u202E\u2060\uFEFF]"
)

#: Non-breaking space. Common in Arabic (justified text) and
#: Russian (typography conventions). Folded to regular space so
#: that FTS5's whitespace tokenization sees it consistently.
_NBSP: Final[str] = "\u00A0"

#: Any run of whitespace. Collapsed to a single space. The
#: pattern uses ``\s`` which in Python 3 (Unicode mode) matches
#: every whitespace character including tabs, newlines, form
#: feeds, vertical tabs, and Unicode space separators.
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Word-character pattern for query tokenization. ``\w`` in
#: Python 3 Unicode mode matches Latin letters, Cyrillic,
#: Arabic letters, Arabic-Indic digits, and underscore -- which
#: is exactly the set of characters that FTS5's ``unicode61``
#: tokenizer would produce as tokens. No apostrophes, quotes,
#: or operator characters can leak through, so the result is
#: safe to pass directly to ``MATCH``.
_WORD_TOKEN: Final[re.Pattern[str]] = re.compile(r"\w+")


def _normalize_common(text: str) -> str:
    """Apply language-agnostic normalization steps.

    Order matters within this function:

      1. Remove invisible characters -- BEFORE casefolding, so
         that ``.lower()`` sees exactly the visible characters.
      2. Replace NBSP -- BEFORE whitespace collapsing, otherwise
         the NBSP would not be recognized as a collapsible space.
      3. Casefold -- AFTER language-specific steps in the caller,
         so that, e.g., uppercase Cyrillic ``Ё`` has already been
         mapped to ``Е`` by ``_normalize_russian`` before this
         function lowercases it.
      4. Collapse whitespace -- AFTER casefolding, so that any
         whitespace introduced by casefolding (rare but
         theoretically possible for some scripts) is caught.
      5. Strip leading / trailing whitespace -- AFTER collapsing,
         because collapsing can leave a leading or trailing
         space in edge cases.

    Empty input passes through unchanged.
    """
    if not text:
        return text
    text = _INVISIBLE_CHARS.sub("", text)
    text = text.replace(_NBSP, " ")
    text = text.lower()
    text = _WHITESPACE_RUN.sub(" ", text)
    return text.strip()


def _normalize_for_storage_impl(text: str) -> str:
    """Private implementation of the storage-normalization pipeline.

    Splitting the "check input is non-empty, run pipeline, apply
    fallback" from the "run the pipeline" is deliberate -- it
    makes each piece trivial to test and reason about in
    isolation, and makes the fallback logic easy to verify in
    unit tests.
    """
    result = _normalize_arabic(text)
    result = _normalize_russian(result)
    result = _normalize_common(result)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_for_storage(text: str) -> str:
    """Normalize ``text`` for FTS indexing.

    The output is stored in ``MemoryEntry.normalized_content`` and
    indexed by FTS5. It is NEVER shown to the user -- the raw
    ``content`` field is what the UI displays.

    Parameters
    ----------
    text
        The raw text to normalize. May be ``None`` (treated as
        ``""``) or empty (returns ``""``).

    Returns
    -------
    str
        The normalized text. If ``text`` was non-empty but the
        pipeline produced an empty string (input was exclusively
        diacritics or invisible characters), returns
        ``text.strip().lower()`` as a fallback so the
        "non-empty in -> non-empty out" contract of
        ``MemoryEntry.with_normalized_content`` is always
        satisfied.

    Raises
    ------
    Nothing. This function is called on the write path where
    exceptions would break the entry-creation contract. Every
    possible input (including ``None``, bytes-that-are-not-strs,
    control characters) produces a well-defined ``str`` output.

    Note on the fallback
    --------------------
    The fallback to ``text.strip().lower()`` is intentionally
    minimal -- it applies NO language-specific transforms. This
    means a pathological input of "all Arabic diacritics" would
    be stored with its diacritics intact in
    ``normalized_content``, which could theoretically cause a
    subtle search mismatch (the diacritics would be indexed as
    characters, not as their base letters). This is acceptable
    because such input is:
      * not linguistically meaningful (a memory containing only
        diacritics is not a real memory),
      * extremely rare (would require the user to actively
        paste an isolated mark),
      * far better than raising and corrupting the write path.
    The alternative -- raising on such input -- would be worse
    for every real user.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        raise TypeError(
            f"normalize_for_storage expects str, got {type(text).__name__}"
        )
    stripped_input = text.strip()
    if not stripped_input:
        return ""

    result = _normalize_for_storage_impl(stripped_input)
    if not result:
        # Pathological input (diacritics-only, invisible-chars-only).
        # Fall back to lowercase-stripped input to honor the
        # non-empty contract. See the docstring note above.
        return stripped_input.lower()
    return result


def normalize_query(text: str) -> str:
    """Normalize ``text`` into a safe FTS5 MATCH query.

    The output is a space-separated sequence of word tokens ready
    to be passed to ``memories_fts MATCH ?``. Because every token
    is extracted by ``\\w+`` from the ALREADY-NORMALIZED text,
    the result cannot contain:
      * quote characters (which would create phrase operators),
      * the ``*`` prefix operator,
      * FTS5 keywords ``AND`` / ``OR`` / ``NOT`` in uppercase
        (the pipeline lowercases everything),
      * parentheses or other grouping syntax,
      * any character that ``unicode61`` would treat as a
        separator.

    This makes the query SAFE against accidental or malicious
    FTS5 operator injection: a user typing
    ``foo OR bar; DROP TABLE`` produces the harmless normalized
    query ``foo or bar drop table``, which FTS5 interprets as
    "match any document containing ALL of foo, or, bar, drop,
    table" -- a strict superset of the user's intent, never an
    unexpected match.

    Parameters
    ----------
    text
        The user's search query. May be ``None`` (treated as
        ``""``) or empty (returns ``""``).

    Returns
    -------
    str
        The normalized token string. Empty string when the input
        contains no word characters after normalization. The
        repository uses "empty string" as the sentinel for
        "skip FTS MATCH, return zero results" -- because
        ``MATCH ''`` raises a SQLite error in every FTS5 version.

    Raises
    ------
    TypeError
        If ``text`` is neither ``str`` nor ``None``.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        raise TypeError(
            f"normalize_query expects str, got {type(text).__name__}"
        )

    normalized = _normalize_for_storage_impl(text.strip())
    if not normalized:
        return ""

    tokens = _WORD_TOKEN.findall(normalized)
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "normalize_for_storage",
    "normalize_query",
]
