# backend/omweb/skills/router/bm25_router.py
"""
BM25-based skill router for Progressive Disclosure.

Purpose:
    Rank installed skills against the user's current message so the
    assignment layer can inject only the top-k most relevant ones into
    the system prompt, instead of the full catalog.

Design choices:
    * Pure in-process, deterministic ranking. No API calls, no GPU,
      no external service. Same query -> same ranking, every time.
    * Indexes ONLY routing metadata (id, name, description,
      short_description, tags, sub-skill triggers). The SKILL.md body
      is deliberately NOT indexed: it is huge and would drown the
      ranking signal in noise.
    * Sub-skill triggers are hoisted to the PARENT level. This lets
      the parent skill rank up when a query mentions a term that is
      specific to one of its sub-skills, so the router pattern stays
      useful at the top level.
    * ``min_score`` drops noise. Without it, a query like "hello"
      would return every skill with a tiny score above zero, which is
      worse than returning nothing.

Not supported:
    * Non-ASCII tokenization. ``_tokenize`` uses ``[a-z0-9_]+`` and
      lowercases the input first, so Arabic/CJK/etc. queries produce
      an empty token list and rank() returns []. This matches the
      English-only scope of the current BM25 integration. Multilingual
      routing is out of scope for this iteration.
"""

from __future__ import annotations

import logging
import re
from typing import List, Sequence, Tuple

from rank_bm25 import BM25Okapi

from omweb.skills.registry import SkillRecord


logger = logging.getLogger("omweb.skills.router.bm25")


# Word-level tokenizer. Restricting to ASCII alphanumerics + underscore
# means identifiers like "code_review" stay as a single token, which is
# what we want for skill IDs. Unicode is intentionally excluded (see
# module docstring).
_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# English stopwords filtered from both the index and the query.
#
# Why filter stopwords at all?
#   1. They carry almost no semantic signal for skill routing.
#   2. In tiny corpora (N <= 2) where the BM25 fallback path is
#      active, EVERY overlap counts as +1. Without this filter a
#      query like "what is the weather in tokyo" would match the
#      word "the" from any skill description and produce a false
#      positive. With this filter, only content-bearing terms
#      contribute.
#   3. In normal corpora (N >= 3), stopwords get near-zero IDF and
#      add noise to the ranking without contributing signal.
#
# Deliberately conservative: only high-frequency English function
# words are listed. Domain terms that might appear as stopwords in
# general English but are meaningful here (e.g. "not", "no",
# "off", "on") are KEPT because they matter for routing.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by",
    "for", "from", "has", "have", "he", "her", "his", "how",
    "i", "if", "in", "is", "it", "its", "me", "my", "no",
    "of", "on", "or", "our", "she", "so", "that", "the",
    "their", "them", "then", "there", "these", "they", "this",
    "those", "to", "was", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you",
    "your",
})


def _filter_stopwords(tokens: List[str]) -> List[str]:
    """Remove stopwords while preserving order and duplicates."""
    return [t for t in tokens if t not in _STOPWORDS]


def _tokenize(text: str) -> List[str]:
    """
    Lowercase, tokenize, and stopword-filter a string into BM25 terms.

    Stopword filtering is applied to BOTH the index corpus and the
    query so overlap counting is symmetric. Returns an empty list for
    non-string or empty input so callers do not need to guard.
    """
    if not isinstance(text, str) or not text:
        return []
    return _filter_stopwords(_TOKEN_RE.findall(text.lower()))


def _skill_corpus(skill: SkillRecord) -> List[str]:
    """
    Build the searchable token list for a single skill.

    The corpus is intentionally narrow: it is the skill's "identity
    card", not its body. Indexing the body would let a long skill
    dominate every query because of sheer term frequency.

    Fields included:
        * ``id``                 — canonical identifier
        * ``name``               — display name
        * ``description``        — short one-liner
        * ``short_description``  — optional variant from manifest
        * ``tags``               — manifest tags
        * sub-skill ``triggers`` — hoisted to the parent so router
                                   skills rank up when a query mentions
                                   one of their sub-skills
    """
    tokens: List[str] = []
    tokens.extend(_tokenize(skill.id))
    tokens.extend(_tokenize(skill.name or ""))
    tokens.extend(_tokenize(skill.description or ""))
    tokens.extend(
        _tokenize(str(skill.manifest.get("short_description") or ""))
    )
    for tag in skill.manifest.get("tags") or []:
        if isinstance(tag, str):
            tokens.extend(_tokenize(tag))
    for sub in skill.sub_skills:
        for trig in sub.get("triggers") or []:
            if isinstance(trig, str):
                tokens.extend(_tokenize(trig))
    return tokens


class BM25SkillRouter:
    """
    Deterministic BM25 skill router.

    Usage::

        router = BM25SkillRouter(skill_registry.list_skills())
        top = router.rank("fix a memory leak in python", top_k=5)
        for skill, score in top:
            print(skill.id, score)

    The index is built once in the constructor. Rebuild it whenever the
    skill registry changes (the assignment layer does this on every
    turn — the cost is negligible for tens of skills).
    """

    def __init__(self, skills: Sequence[SkillRecord]) -> None:
        self._skills: List[SkillRecord] = list(skills)
        if not self._skills:
            self._bm25: BM25Okapi | None = None
            return

        corpus = [_skill_corpus(s) for s in self._skills]

        # Defensive: BM25Okapi crashes when avgdl is zero, which would
        # happen if every skill in the corpus tokenized to []. That is
        # only possible if all skills have empty id/name/description/
        # tags — a data-integrity bug. Bail out with a clear log instead
        # of letting BM25 raise a cryptic ZeroDivisionError later.
        if not any(corpus):
            self._bm25 = None
            logger.warning(
                "BM25SkillRouter: all %d skills tokenized to an empty "
                "corpus; router will return no matches until skills "
                "have non-empty metadata.",
                len(self._skills),
            )
            return

        self._bm25 = BM25Okapi(corpus, k1=1.5, b=0.75)
        logger.debug(
            "BM25 index built over %d skills (avg corpus size=%.1f).",
            len(corpus),
            sum(len(c) for c in corpus) / len(corpus),
        )

    def rank(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.1,
    ) -> List[Tuple[SkillRecord, float]]:
        """
        Rank skills against a free-form query.

        Args:
            query:     The user's message (or any text to match).
            top_k:     Maximum number of results to return.
            min_score: Skills with a BM25 score strictly below this
                       threshold are dropped. Prevents near-random
                       matches from polluting the catalog. A skill
                       with zero matching terms scores 0.0 and is
                       always dropped.

        Returns:
            A list of ``(SkillRecord, score)`` sorted by descending
            score. Empty when the query is empty, the router has no
            index, or no skill clears ``min_score``.
        """
        if not query or self._bm25 is None:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(
            zip(self._skills, scores),
            key=lambda pair: pair[1],
            reverse=True,
        )

        # ── Tiny-corpus fallback ────────────────────────────────────
        # BM25's IDF formula turns non-positive when a query term
        # appears in at least half the corpus (see _calc_idf in
        # rank_bm25). With N=1, EVERY query term that exists in the
        # single document gets IDF < 0; with N=2 the IDF collapses
        # to 0. In both cases the min_score filter would drop the
        # strongest matches — sometimes the only match — because the
        # raw score is ≤ 0 even though the skill shares many tokens
        # with the query.
        #
        # The fallback below keeps the ranking meaningful for tiny
        # corpora by switching to a simple overlap-count score
        # (number of query tokens that appear in the skill's
        # corpus). This is monotone in token matches and equivalent
        # in spirit to BM25 for the small-N case. Skills with zero
        # overlap are excluded — the "no match" semantics are
        # preserved.
        #
        # The threshold N=2 falls out of the math: for N=3 and
        # above the IDF for a uniquely-matching term is positive,
        # so the normal BM25 path resumes automatically.
        if not ranked or float(ranked[0][1]) <= 0.0:
            query_set = set(query_tokens)
            overlap_ranked: List[Tuple[SkillRecord, float]] = []
            for skill in self._skills:
                overlap = len(query_set & set(_skill_corpus(skill)))
                if overlap > 0:
                    overlap_ranked.append((skill, float(overlap)))
            overlap_ranked.sort(key=lambda pair: pair[1], reverse=True)
            return overlap_ranked[:top_k]

        return [
            (skill, float(score))
            for skill, score in ranked
            if score >= min_score
        ][:top_k]


__all__ = ["BM25SkillRouter"]
