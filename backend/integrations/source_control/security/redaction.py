from __future__ import annotations

import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

# Known sensitive dictionary and parameter keys
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "client_secret",
    "password",
    "authorization",
    "private_key",
    "secret_key",
    "api_key",
    "credential",
    "bearer",
})

# Pre-compiled regex patterns for known source control token formats
_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # GitHub Tokens: ghp (PAT), gho (OAuth), ghu (User-to-Server), ghs (Server-to-Server), ghr (Refresh)
    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,255}\b"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    # GitLab Personal/Project Access Tokens: glpat-, gloas-, glcbt-, glptt-
    (
        re.compile(r"\bgl[a-z]{3,4}-[A-Za-z0-9_\-]{20,255}\b"),
        "[REDACTED_GITLAB_TOKEN]",
    ),
    # HTTP Authorization Bearer Header
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{16,}\b"),
        "Bearer [REDACTED]",
    ),
    # Generic Basic Auth in URIs (e.g., https://username:password@domain.com)
    (
        re.compile(r"(https?://)([^:\s/@]+):([^@\s/]+)@"),
        r"\1[REDACTED]:[REDACTED]@",
    ),
)


def redact_url(url: str) -> str:
    """Removes sensitive userinfo (username:password or token) from a URL string."""
    if not url:
        return url

    try:
        parts = urlsplit(url)
        if not parts.netloc or "@" not in parts.netloc:
            return url

        # Strip userinfo before '@'
        _, _, host_and_port = parts.netloc.rpartition("@")
        sanitized_netloc = f"[REDACTED]@{host_and_port}"
        return urlunsplit((
            parts.scheme,
            sanitized_netloc,
            parts.path,
            parts.query,
            parts.fragment,
        ))
    except Exception:
        # Fallback regex substitution if urlsplit fails
        return re.sub(r"https?://([^@\s]+)@", "https://[REDACTED]@", url)


def redact_text(text: str) -> str:
    """Sanitizes text by matching and replacing known token and credential patterns."""
    if not text or not isinstance(text, str):
        return text

    sanitized = text
    for pattern, replacement in _TOKEN_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)

    return sanitized


def redact_data(data: Any) -> Any:
    """Recursively traverses and sanitizes nested dictionaries, lists, and primitives."""
    if data is None:
        return None

    if isinstance(data, str):
        return redact_text(redact_url(data))

    if isinstance(data, Mapping):
        scrubbed_dict: dict[str, Any] = {}
        for key, value in data.items():
            str_key = str(key).lower()
            if any(k in str_key for k in SENSITIVE_KEYS):
                scrubbed_dict[key] = "[REDACTED]"
            else:
                scrubbed_dict[key] = redact_data(value)
        return scrubbed_dict

    if isinstance(data, (list, tuple, set)):
        scrubbed_seq = [redact_data(item) for item in data]
        if isinstance(data, tuple):
            return tuple(scrubbed_seq)
        if isinstance(data, set):
            return set(scrubbed_seq)
        return scrubbed_seq

    return data
