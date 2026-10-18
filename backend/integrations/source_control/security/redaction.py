# backend/integrations/source_control/security/redaction.py
"""
Secret redaction utilities for the Source Control Integration
subsystem.

This module is the SINGLE SOURCE OF TRUTH for redacting secrets from
strings, mappings, URLs, headers, and byte streams. It is used by:

    - The error hierarchy (`errors.py`) before storing messages.
    - The audit log before persisting events.
    - The Git subprocess wrapper before returning stdout/stderr.
    - The HTTP layer before sending responses to the frontend.
    - The GitHub provider adapters before surfacing payloads.

Design rules:
    - This module MUST NOT import from anywhere else in the package.
      It depends only on the Python standard library, which allows
      `errors.py` to import it without creating circular imports.
    - Redaction MUST be idempotent: applying it twice produces the
      same result as applying it once.
    - Redaction MUST NOT raise. Any unexpected condition MUST degrade
      to returning the input unchanged rather than propagating an
      exception into the caller's error path.
    - Redaction MUST preserve surrounding context so that log readers
      can still understand what happened. Replacing a token with
      `***REDACTED***` is preferred over removing the token entirely.

The module intentionally errs on the side of redacting too much. It is
safer to hide a non-secret than to leak a credential.

Functions:
    - `redact_text`     : redact a string.
    - `redact_bytes`    : redact a byte stream (decoded as UTF-8).
    - `redact_mapping`  : redact a `Mapping` recursively.
    - `redact_sequence` : redact an iterable of values.
    - `redact_data`     : redact an arbitrary JSON-like value,
                          preserving the input's type where possible.
    - `redact_url`      : redact a URL (credentials + query params).
    - `redact_headers`  : redact HTTP headers by name.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "REDACTED",
    "MAX_REDACTION_INPUT",
    "SENSITIVE_KEYS",
    "redact_text",
    "redact_mapping",
    "redact_sequence",
    "redact_data",
    "redact_url",
    "redact_headers",
    "redact_bytes",
    "contains_secret",
    "is_likely_secret",
    "SafeStr",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The placeholder that replaces a redacted secret.
REDACTED = "***REDACTED***"

#: Maximum input length (in characters) processed by `redact_text`.
#: Inputs larger than this are truncated before regex matching to
#: avoid unbounded CPU usage. The truncation happens at the string
#: level; the truncated tail is preserved in the output.
MAX_REDACTION_INPUT: int = 1_000_000


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------
#
# Order matters: more specific patterns MUST appear before more generic
# ones. For example, `github_pat_` MUST be tested before the generic
# `ghp_` pattern, otherwise the fine-grained token would be split
# incorrectly.
#
# Each pattern captures the secret-bearing portion. When a pattern
# captures a non-secret prefix (such as `client_secret=`), the
# redaction helper preserves the prefix and replaces only the value.


_PATTERNS: tuple[re.Pattern[str], ...] = (
    # --- GitHub --------------------------------------------------------
    # Fine-grained personal access tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"),
    # Classic personal / OAuth / user / server-to-server / refresh tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,255}\b"),
    # --- GitLab --------------------------------------------------------
    # Personal, project, group, and impersonation tokens
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{15,255}\b"),
    # Pipeline trigger tokens
    re.compile(r"\bglptt-[A-Za-z0-9_\-]{15,255}\b"),
    # Runner registration tokens
    re.compile(r"\bGR1348941[A-Za-z0-9_\-]{15,255}\b"),
    # --- Bitbucket -----------------------------------------------------
    # App passwords / access tokens start with a distinctive prefix
    re.compile(r"\bATBB[A-Za-z0-9]{16,}\b"),
    # --- AWS -----------------------------------------------------------
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # Access key ID
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),  # Temporary access key ID
    # --- Google --------------------------------------------------------
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # --- Slack ---------------------------------------------------------
    re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"),
    # --- Stripe --------------------------------------------------------
    re.compile(r"\bsk_live_[0-9A-Za-z]{24,}\b"),
    re.compile(r"\brk_live_[0-9A-Za-z]{24,}\b"),
    # --- JSON Web Tokens -----------------------------------------------
    re.compile(
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"
    ),
    # --- Private keys --------------------------------------------------
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]+?"
        r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
    ),
    # --- HTTP Authorization headers ------------------------------------
    re.compile(
        r"(?i)\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._\-+/=]{8,}"
    ),
    re.compile(
        r"(?i)\bauthorization\s*[:=]\s*basic\s+[A-Za-z0-9+/=]{8,}"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{20,}"),
    # --- Cookies -------------------------------------------------------
    re.compile(r"(?i)\bcookie\s*:\s*[^\r\n]{8,}"),
    re.compile(r"(?i)\bset-cookie\s*:\s*[^\r\n]{8,}"),
    # --- URLs with embedded credentials --------------------------------
    # scheme://user:password@host/path -> scheme://user:***REDACTED***@host/path
    # Two capture groups are used (prefix ending at the colon, suffix
    # starting at the '@') so that `_redact_match` can preserve both
    # the username and the `@` separator. A single-group pattern would
    # only preserve the scheme and produce a misleading URL that no
    # longer contains the username.
    #
    # PERFORMANCE: every length-quantified class is bounded. An
    # unbounded `[a-zA-Z0-9+.\-]*` would be O(n^2) on non-URL input
    # (for each of the n start positions, the engine would greedily
    # match n - i characters before failing to find `://`, then
    # backtrack). On a 1 MiB non-URL string this caused the redaction
    # pass to hang. The bounds below reflect real-world URL component
    # lengths (schemes up to 31 chars per RFC 3986's practical limit,
    # usernames up to 255 chars, passwords up to 511 chars). They
    # convert the worst-case cost from O(n^2) to O(n).
    re.compile(
        r"([a-zA-Z][a-zA-Z0-9+.\-]{1,31}://[^/\s:@]{1,255}:)"
        r"[^/\s@]{1,511}(@)"
    ),
    # --- OAuth and API parameters --------------------------------------
    re.compile(r"(?i)\b(client_secret=)[^&\s\"']+"),
    re.compile(r"(?i)\b(refresh_token=)[^&\s\"']+"),
    re.compile(r"(?i)\b(access_token=)[^&\s\"']+"),
    re.compile(r"(?i)\b(id_token=)[^&\s\"']+"),
    re.compile(r"(?i)\b(api[_-]?key=)[^&\s\"']+"),
    re.compile(r"(?i)\b(password=)[^&\s\"']+"),
    # OAuth authorization codes are short-lived but still sensitive
    re.compile(r"(?i)\b(code=)[A-Za-z0-9_\-]{8,}"),
    # --- Generic JSON fields ------------------------------------------
    # Matches `"client_secret": "..."` and similar in JSON blobs.
    # Two capture groups are used (the JSON key + opening quote +
    # separator, and the closing quote) so that the replacement
    # preserves the JSON structure. A single-group pattern would
    # collapse the entire fragment into `client_secret***REDACTED***`,
    # producing invalid JSON and breaking downstream parsers.
    #
    # LIMITATION: values that contain escaped double quotes (e.g.
    # "password": "a\"b") are truncated at the first unescaped quote;
    # the remainder is left in the output. This is acceptable for
    # known secret formats, which never contain quotes, but callers
    # SHOULD NOT treat the output as authoritative for arbitrary JSON.
    re.compile(
        r'(?i)("(?:client_secret|refresh_token|access_token|password|'
        r'api_key|private_key|secret)"\s*:\s*")[^"]*(")'
    ),
)


#: Reduced pattern set applied when the input exceeds
#: `MAX_REDACTION_INPUT`. Only the highest-signal patterns are kept to
#: keep processing time bounded.
_FAST_PATTERNS: tuple[re.Pattern[str], ...] = (
    _PATTERNS[0],   # github_pat_
    _PATTERNS[1],   # gh*_
    _PATTERNS[2],   # glpat-
    _PATTERNS[5],   # ATBB
    _PATTERNS[16],  # Bearer (generic)
    _PATTERNS[19],  # URL credentials
)


# ---------------------------------------------------------------------------
# Core redaction
# ---------------------------------------------------------------------------


def redact_text(text: Optional[str]) -> str:
    """
    Return `text` with any recognizable secret replaced by
    `REDACTED`.

    The function is idempotent and never raises. If `text` is `None`
    or empty, the empty string is returned. If `text` exceeds
    `MAX_REDACTION_INPUT`, a fast subset of patterns is applied to
    keep runtime bounded.

    Examples:
        >>> redact_text("token=ghp_abc123def456ghi789jkl012mno345pqr")
        'token=***REDACTED***'
        >>> redact_text("https://user:secret@example.com/repo.git")
        'https://user:***REDACTED***@example.com/repo.git'
    """
    if not text:
        return text or ""

    try:
        patterns = (
            _FAST_PATTERNS
            if len(text) > MAX_REDACTION_INPUT
            else _PATTERNS
        )
        redacted = text
        for pattern in patterns:
            redacted = pattern.sub(_redact_match, redacted)
        return redacted
    except Exception:  # noqa: BLE001
        # Redaction MUST NOT raise. Degrade to returning the input.
        return text


def redact_bytes(data: Optional[bytes]) -> bytes:
    """
    Return `data` with any recognizable secret replaced.

    Binary data is decoded as UTF-8 with `errors="replace"`, redacted
    as text, and re-encoded as UTF-8. Callers that need byte-exact
    preservation SHOULD NOT use this function.
    """
    if not data:
        return data or b""
    try:
        decoded = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return data
    return redact_text(decoded).encode("utf-8")


# ---------------------------------------------------------------------------
# Mapping and sequence redaction
# ---------------------------------------------------------------------------


#: Key names whose values are ALWAYS redacted when encountered in a
#: mapping, regardless of their content. Comparison is case-insensitive.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "private_key",
        "api_key",
        "apikey",
        "auth",
        "credentials",
        "cookie",
        "set-cookie",
        "session",
        "session_id",
        "x-api-key",
        "x-auth-token",
    }
)

#: Public alias for `_SENSITIVE_KEYS`.
#:
#: External callers (configuration UIs, provider adapters, tests)
#: sometimes need to know which keys are treated as secret by the
#: redaction layer. Exposing the set read-only via a public name
#: allows them to inspect the policy without reaching into the
#: module's private namespace.
SENSITIVE_KEYS: frozenset[str] = _SENSITIVE_KEYS

def redact_mapping(
    mapping: Optional[Mapping[Any, Any]],
    *,
    deep: bool = True,
    _depth: int = 0,
    _max_depth: int = 6,
) -> dict[Any, Any]:
    """
    Return a copy of `mapping` with secrets redacted.

    Args:
        mapping: The input mapping. `None` yields an empty dict.
        deep: When True (default), recursively redact nested mappings
            and sequences. When False, only top-level string values
            are processed.
        _depth, _max_depth: Internal recursion control. Callers SHOULD
            NOT pass these arguments.

    Behavior:
        - Keys in `_SENSITIVE_KEYS` are redacted unconditionally,
          regardless of value shape.
        - String values are passed through `redact_text`.
        - Nested mappings and sequences are processed recursively up
          to `_max_depth` levels.
        - Non-string scalars (numbers, booleans, `None`) are passed
          through unchanged.
    """
    if mapping is None:
        return {}

    if _depth >= _max_depth:
        # Depth limit reached; conservatively redact everything below.
        return {key: REDACTED for key in mapping}

    result: dict[Any, Any] = {}
    for key, value in mapping.items():
        if isinstance(key, str) and key.strip().lower() in _SENSITIVE_KEYS:
            result[key] = REDACTED
            continue

        if isinstance(value, str):
            result[key] = redact_text(value)
        elif deep and isinstance(value, Mapping):
            result[key] = redact_mapping(
                value, deep=True, _depth=_depth + 1, _max_depth=_max_depth
            )
        elif deep and isinstance(value, (list, tuple)):
            result[key] = redact_sequence(
                value, deep=True, _depth=_depth + 1, _max_depth=_max_depth
            )
        else:
            result[key] = value
    return result


def redact_sequence(
    sequence: Optional[Iterable[Any]],
    *,
    deep: bool = True,
    _depth: int = 0,
    _max_depth: int = 6,
) -> list[Any]:
    """
    Return a list copy of `sequence` with secrets redacted.

    Preserves the original order. Tuples are converted to lists, which
    is acceptable because the caller is producing a redacted view for
    logging or transport, not for round-tripping.
    """
    if sequence is None:
        return []

    if _depth >= _max_depth:
        return [REDACTED for _ in sequence]

    result: list[Any] = []
    for item in sequence:
        if isinstance(item, str):
            result.append(redact_text(item))
        elif deep and isinstance(item, Mapping):
            result.append(
                redact_mapping(
                    item, deep=True, _depth=_depth + 1, _max_depth=_max_depth
                )
            )
        elif deep and isinstance(item, (list, tuple)):
            result.append(
                redact_sequence(
                    item,
                    deep=True,
                    _depth=_depth + 1,
                    _max_depth=_max_depth,
                )
            )
        else:
            result.append(item)
    return result


def redact_data(
    value: Any,
    *,
    deep: bool = True,
) -> Any:
    """
    Return a redacted copy of an arbitrary JSON-like value.

    Unlike `redact_mapping`, this function preserves the input's
    top-level type:

        - `dict` / `Mapping`  -> `dict` (see `redact_mapping`)
        - `list` / `tuple`    -> `list` (see `redact_sequence`)
        - `str`               -> `str`  (see `redact_text`)
        - `bytes`             -> `bytes` (see `redact_bytes`)
        - `None`              -> `None`
        - any other scalar    -> returned unchanged

    This is the function the GitHub provider adapters use to sanitize
    arbitrary payload fragments before surfacing them in errors or in
    tool results. It never raises; on any internal failure it returns
    the input unchanged.

    Args:
        value: The value to redact.
        deep: When True (default), recurse into nested structures.
            When False, only the top level is processed.
    """
    try:
        if value is None:
            return None
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, bytes):
            return redact_bytes(value)
        if isinstance(value, Mapping):
            return redact_mapping(value, deep=deep)
        if isinstance(value, (list, tuple)):
            return redact_sequence(value, deep=deep)
        # Scalars (int, float, bool, etc.) are returned unchanged.
        return value
    except Exception:  # noqa: BLE001
        # Redaction MUST NOT raise. Degrade to returning the input.
        return value


# ---------------------------------------------------------------------------
# URL and header redaction
# ---------------------------------------------------------------------------


def redact_url(url: Optional[str]) -> str:
    """
    Return `url` with credentials and sensitive query parameters
    redacted.

    Handles both classic `scheme://user:pass@host` credentials and
    query-string parameters such as `?access_token=...`.
    """
    if not url:
        return ""
    try:
        redacted = redact_text(url)
        return redacted
    except Exception:  # noqa: BLE001
        return url


def redact_headers(
    headers: Optional[Mapping[str, str]],
) -> dict[str, str]:
    """
    Return a copy of `headers` with sensitive header values redacted.

    Header names are compared case-insensitively. The original casing
    of non-sensitive header names is preserved.
    """
    if not headers:
        return {}

    result: dict[str, str] = {}
    for name, value in headers.items():
        if isinstance(name, str) and name.strip().lower() in _SENSITIVE_KEYS:
            result[name] = REDACTED
        elif isinstance(value, str):
            result[name] = redact_text(value)
        else:
            result[name] = value
    return result


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def contains_secret(text: Optional[str]) -> bool:
    """
    Return True if `text` appears to contain a recognizable secret.

    This is a heuristic used by pre-commit checks and the policy
    engine. It is NOT a substitute for `redact_text`: some secrets
    will not match any pattern and will not be detected.

    The check is intentionally fast and does not attempt to
    de-obfuscate encoded or split secrets.
    """
    if not text:
        return False
    try:
        return any(pattern.search(text) is not None for pattern in _PATTERNS)
    except Exception:  # noqa: BLE001
        return False


def is_likely_secret(value: Optional[str]) -> bool:
    """
    Return True if a single value looks like a secret.

    Unlike `contains_secret`, this function requires the entire value
    to match a secret pattern (anchored), which reduces false
    positives on strings that merely mention a token format.
    """
    if not value:
        return False
    stripped = value.strip()
    if not stripped:
        return False
    try:
        for pattern in _PATTERNS:
            match = pattern.match(stripped)
            if match is not None and match.end() == len(stripped):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


# ---------------------------------------------------------------------------
# SafeStr wrapper
# ---------------------------------------------------------------------------


class SafeStr:
    """
    A string wrapper whose `__repr__` and `__str__` return a redacted
    view.

    Use this class to carry a secret through code paths that may log
    it, while still allowing the caller to obtain the raw value via
    `value`. This is a defensive convenience; it does NOT replace
    discipline around logging.

    Example:
        token = SafeStr("ghp_abc123...")
        logger.info("Using token %r", token)  # logs ***REDACTED***
        client.send(token.value)              # raw value available
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    @property
    def value(self) -> str:
        """Return the underlying raw value."""
        return self._value

    def redacted(self) -> str:
        """Return the redacted representation."""
        return redact_text(self._value)

    def __str__(self) -> str:
        return self.redacted()

    def __repr__(self) -> str:
        return f"SafeStr({self.redacted()!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SafeStr):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _redact_match(match: re.Match[str]) -> str:
    """
    Redaction callback that preserves captured non-secret context.

    Patterns in this module use three shapes, all handled here:

        - No capture groups: the entire match is replaced. Used by
          the token patterns (``ghp_*``, ``glpat-*``, JWTs, private
          keys) where the surrounding text carries no useful
          context.

        - One capture group: the group is a non-secret prefix such as
          ``client_secret=``. The prefix is preserved and only the
          secret that follows is replaced:

              ``client_secret=abc123`` -> ``client_secret=***REDACTED***``

        - Two capture groups: the groups are a non-secret prefix and a
          non-secret suffix. Both are preserved and the secret
          between them is replaced. This is required for:

              URLs with credentials:
                  ``https://user:pass@host``
                    -> ``https://user:***REDACTED***@host``

              JSON field values:
                  ``"client_secret": "abc"``
                    -> ``"client_secret": "***REDACTED***"``

              Without the two-group form, the URL pattern would lose
              the username and the ``@`` separator, and the JSON
              pattern would produce invalid JSON.
    """
    if (
        match.lastindex
        and match.lastindex >= 2
        and match.group(2) is not None
    ):
        return f"{match.group(1) or ''}{REDACTED}{match.group(2)}"
    if match.lastindex and match.lastindex >= 1:
        return f"{match.group(1) or ''}{REDACTED}"
    return REDACTED
