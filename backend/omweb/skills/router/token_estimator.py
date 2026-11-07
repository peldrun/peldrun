# backend/omweb/skills/router/token_estimator.py
"""
Cheap, deterministic token-count estimate for budgeting decisions.

Why not tiktoken?
    ``tiktoken`` loads a BPE model on first use (~100ms + ~50MB RAM).
    That cost is fine for a one-shot count, but the skills assignment
    layer runs on every chat turn and only needs to answer a single
    question: "did this catalog fit within the budget?".

    A char-based heuristic is accurate enough for that decision. When
    the estimate is 95% of the budget, the caller can still accept it —
    the cost of being slightly over is a few extra tokens in a prompt
    that already has room.

Accuracy:
    * English prose:  ~4 chars / token (calibrated against GPT-4 and
      Claude tokenizers for typical skill descriptions).
    * Code / JSON:    ~2 chars / token (more punctuation, more splits).
    * Mixed Arabic:   much worse (BPE models often use 1-2 chars/token
      for Arabic; the heuristic UNDER-estimates. This is acceptable
      because we only use the estimate for logging and budget checks,
      never for hard truncation.

    The caller does not need precision — it needs a stable, cheap,
    side-effect-free number that errs on the side of "close enough".
"""

from __future__ import annotations

# Calibration: ~4 characters per token for English prose. This is
# deliberately conservative — the real ratio for short identifiers
# and code-ish snippets is closer to 2.5, so the estimator tends to
# under-count. Under-counting means the budget check is slightly more
# permissive, which is the safer side: we would rather let a catalog
# through that is 5% over than reject one that is 5% under.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """
    Estimate the number of tokens in ``text`` using a char-count heuristic.

    Args:
        text: Any string. Non-string inputs are treated as empty
              (returns 0) so callers do not need to guard.

    Returns:
        A non-negative integer. ``0`` for empty/None input. Never
        returns less than 1 for non-empty input — a 2-character
        string still counts as "at least one token" to avoid
        surprising zero-cost behavior in tests and logs.
    """
    if not isinstance(text, str) or not text:
        return 0
    # max(1, ...) ensures a 1-3 char string still reports one token
    # rather than zero. The intent is "does this consume budget?", and
    # the answer for any non-empty string is always yes.
    return max(1, len(text) // _CHARS_PER_TOKEN)


__all__ = ["estimate_tokens"]
