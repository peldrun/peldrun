from __future__ import annotations

import json
import time
from typing import Any, Mapping, Optional

from ...domain.enums import SourceControlErrorCode
from ...errors import (
    AuthExpiredError,
    AuthRequiredError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    RemoteStateChangedError,
    SourceControlError,
    ValidationError,
)
from ...security.redaction import redact_data, redact_text, redact_url


class GitHubAPIError(SourceControlError):
    """Fallback exception representing an unclassified or internal error from GitHub REST API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        details: Optional[dict[str, str]] = None,
    ) -> None:
        merged_details = {"status_code": str(status_code)}
        if details:
            merged_details.update(details)
        super().__init__(
            message=message,
            code=SourceControlErrorCode.PROVIDER_ERROR,
            retryable=status_code in {500, 502, 503, 504},
            details=merged_details,
        )
        self.status_code = status_code


def _get_header_ci(headers: Mapping[str, str], target: str) -> Optional[str]:
    """Retrieves a header value using case-insensitive matching."""
    lower_target = target.lower()
    for key, value in headers.items():
        if key.lower() == lower_target:
            return value
    return None


def extract_rate_limit_delay(headers: Mapping[str, str]) -> Optional[int]:
    """Calculates retry delay in seconds from standard GitHub rate-limit headers."""
    # 1. Check Retry-After header
    retry_after = _get_header_ci(headers, "retry-after")
    if retry_after and retry_after.isdigit():
        return int(retry_after)

    # 2. Check X-RateLimit-Reset epoch timestamp
    reset_epoch = _get_header_ci(headers, "x-ratelimit-reset")
    if reset_epoch and reset_epoch.isdigit():
        diff = int(reset_epoch) - int(time.time())
        return max(1, diff)

    return None


def parse_github_error_body(
    body: str,
) -> tuple[str, list[dict[str, Any]], Optional[str]]:
    """Parses raw GitHub JSON response into scrubbed message, field errors, and documentation URL."""
    if not body or not body.strip():
        return "Empty response from GitHub API.", [], None

    try:
        data = json.loads(body)
        if isinstance(data, dict):
            raw_message = data.get("message", "No error message provided.")
            raw_errors = data.get("errors", [])
            doc_url = data.get("documentation_url")

            clean_message = redact_text(str(raw_message))
            clean_errors = redact_data(raw_errors) if isinstance(raw_errors, list) else []
            clean_doc = redact_url(str(doc_url)) if doc_url else None

            return clean_message, clean_errors, clean_doc
    except Exception:
        pass

    return redact_text(body.strip()[:500]), [], None


def _rate_limit_details(
    base_details: Mapping[str, Any],
    delay: Optional[int],
) -> dict[str, Any]:
    """
    Build the ``details`` mapping for a ``RateLimitError``.

    ``RateLimitError`` does NOT accept a ``retry_after_seconds``
    keyword argument; the value MUST be carried inside ``details``
    (see the docstring of ``RateLimitError`` in
    ``integrations/source_control/errors.py``). This helper keeps
    the two rate-limit call sites consistent and avoids duplicating
    the merge logic.

    The value is stored as an ``int`` (matching the convention used
    by ``GitHubClient._rate_limit_error``) rather than a string, so
    that downstream consumers can use it directly as a backoff
    without re-parsing.
    """
    merged: dict[str, Any] = dict(base_details)
    if delay is not None:
        merged["retry_after_seconds"] = int(delay)
    return merged


def map_github_http_error(
    status_code: int,
    body: str,
    headers: Mapping[str, str],
    *,
    url: Optional[str] = None,
    method: str = "GET",
) -> SourceControlError:
    """Translates GitHub REST API HTTP errors into normalized domain exceptions with sanitized details.

    Scrubs tokens, extracts rate-limit delays, and produces actionable diagnostic context.
    """
    clean_message, field_errors, doc_url = parse_github_error_body(body)
    req_id = _get_header_ci(headers, "x-github-request-id")
    clean_url = redact_url(url) if url else None

    diagnostic_details: dict[str, Any] = {
        "status_code": str(status_code),
        "method": method.upper(),
    }
    if req_id:
        diagnostic_details["request_id"] = req_id
    if clean_url:
        diagnostic_details["url"] = clean_url
    if doc_url:
        diagnostic_details["documentation_url"] = doc_url

    # Append structured field validation issues if present
    if field_errors:
        diagnostic_details["field_errors"] = json.dumps(field_errors, ensure_ascii=False)

    lower_message = clean_message.lower()

    # 401 Unauthorized: Invalid or expired credentials
    if status_code == 401:
        if "bad credentials" in lower_message or "token expired" in lower_message:
            return AuthExpiredError(
                f"GitHub token is expired or revoked: {clean_message}",
                details=diagnostic_details,
            )
        return AuthRequiredError(
            f"GitHub authentication required: {clean_message}",
            details=diagnostic_details,
        )

    # 403 Forbidden: Permissions, SAML enforcement, or secondary rate limits
    if status_code == 403:
        # Check for abuse detection or rate limiting
        rem = _get_header_ci(headers, "x-ratelimit-remaining")
        is_rate_limited = (
            (rem is not None and rem == "0")
            or "rate limit" in lower_message
            or "secondary rate limit" in lower_message
            or "abuse" in lower_message
        )
        if is_rate_limited:
            delay = extract_rate_limit_delay(headers)
            return RateLimitedError(
                f"GitHub API rate limit exceeded: {clean_message}",
                details=_rate_limit_details(diagnostic_details, delay),
            )

        return PermissionDeniedError(
            f"GitHub permission denied: {clean_message}. "
            "Verify token scopes and repository access permissions.",
            details=diagnostic_details,
        )

    # 404 Not Found
    if status_code == 404:
        return NotFoundError(
            f"GitHub resource not found: {clean_message}. "
            "Resource does not exist or token lacks visibility scope.",
            details=diagnostic_details,
        )

    # 409 Conflict: Merge conflicts, stale state, or branch updates
    if status_code == 409:
        if "merge conflict" in lower_message or "conflict" in lower_message:
            return ConflictError(
                f"GitHub conflict encountered: {clean_message}",
                details=diagnostic_details,
            )
        return RemoteStateChangedError(
            f"GitHub remote state conflict: {clean_message}",
            details=diagnostic_details,
        )

    # 422 Unprocessable Entity: Validation failures
    if status_code == 422:
        reasons: list[str] = []
        for fe in field_errors:
            if isinstance(fe, dict):
                field_name = fe.get("field", "payload")
                code_str = fe.get("code", "")
                msg_str = fe.get("message", "")
                reasons.append(f"{field_name}: {msg_str or code_str}")

        composite_reason = f" ({'; '.join(reasons)})" if reasons else ""
        return ValidationError(
            f"GitHub validation rejected request: {clean_message}{composite_reason}",
            details=diagnostic_details,
        )

    # 429 Too Many Requests: Explicit rate limiting
    if status_code == 429:
        delay = extract_rate_limit_delay(headers)
        return RateLimitedError(
            f"GitHub API rate limit exceeded: {clean_message}",
            details=_rate_limit_details(diagnostic_details, delay),
        )

    # 5xx Server Errors: Ephemeral gateway / upstream failures
    if 500 <= status_code < 600:
        return GitHubAPIError(
            f"GitHub upstream server error ({status_code}): {clean_message}",
            status_code=status_code,
            details=diagnostic_details,
        )

    # All other status codes
    return GitHubAPIError(
        f"GitHub API request failed with status {status_code}: {clean_message}",
        status_code=status_code,
        details=diagnostic_details,
    )
