# backend/integrations/source_control/providers/github/client.py
"""
GitHub HTTP client for the Source Control Integration subsystem.

This module wraps `httpx.AsyncClient` and provides a single,
disciplined entry point for every HTTP call to the GitHub REST API.
All GitHub-specific request construction, header management, and
error mapping happens here.

The client is intentionally minimal:

    - It knows how to build URLs, headers, and timeouts.
    - It knows how to map HTTP responses to `SourceControlError`.
    - It knows how to retry transient failures with bounded
      exponential backoff.
    - It does NOT know about repositories, branches, pull requests,
      or any other domain concept. Those are handled by dedicated
      services that use this client.

Design rules:
    - The token is passed as `Authorization: Bearer <token>` on
      every request. Tokens are NEVER placed in URLs, in query
      strings, or in the client's repr.
    - The API version is pinned via `X-GitHub-Api-Version` so that
      GitHub cannot introduce a breaking change without our
      consent.
    - Every error message is redacted before being raised.
    - The client is safe to share across coroutines. Its only
      mutable state is a small rate-limit snapshot used for
      observability.
    - The client is not a context manager by obligation, but it
      supports `async with` for callers that prefer that style.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ...domain.enums import ProviderId
from ...domain.models import ProviderAccount
from ...errors import (
    AuthRequiredError,
    ConflictError,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    RateLimitError,
    SourceControlError,
    ValidationError,
)
from ...security.redaction import redact_text, redact_url

__all__ = [
    "GitHubClient",
    "GitHubApiConfig",
    "GitHubRateLimit",
    "DEFAULT_API_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "MAX_PAGE_SIZE",
]


logger = logging.getLogger(__name__)


#: Default REST API base URL for GitHub.com.
DEFAULT_API_URL: str = "https://api.github.com"

#: Default per-request timeout in seconds.
DEFAULT_TIMEOUT_SECONDS: float = 30.0

#: Default maximum number of retry attempts for transient failures.
DEFAULT_MAX_RETRIES: int = 3

#: GitHub's documented maximum page size for most endpoints.
MAX_PAGE_SIZE: int = 100

#: User-Agent sent on every request. GitHub rejects requests
#: without one.
_USER_AGENT: str = "peldrun-source-control/1.0"

#: API version pinned via the `X-GitHub-Api-Version` header.
_API_VERSION: str = "2022-11-28"

#: Accept header value requesting the current JSON representation.
_ACCEPT: str = "application/vnd.github+json"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubApiConfig:
    """
    Immutable configuration for a `GitHubClient`.

    Fields:
        api_url:         Base URL of the REST API.
        timeout_seconds: Per-request timeout.
        max_retries:     Retry attempts for transient failures.
        user_agent:      User-Agent header value.
        api_version:     Value sent in `X-GitHub-Api-Version`.
        default_headers: Extra headers merged into every request.
                         MUST NOT contain credentials.
        follow_redirects: When True, httpx follows redirects.
                          GitHub occasionally issues 301s for
                          renamed repositories.
    """

    api_url: str = DEFAULT_API_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    user_agent: str = _USER_AGENT
    api_version: str = _API_VERSION
    default_headers: Mapping[str, str] = field(default_factory=dict)
    follow_redirects: bool = True

    def __post_init__(self) -> None:
        # Normalize the API URL by stripping trailing slashes.
        normalized = (self.api_url or DEFAULT_API_URL).rstrip("/")
        object.__setattr__(self, "api_url", normalized)

        if self.timeout_seconds <= 0:
            raise ValidationError("timeout_seconds must be positive.")
        if self.max_retries < 0:
            raise ValidationError("max_retries must not be negative.")


# ---------------------------------------------------------------------------
# Rate limit snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubRateLimit:
    """
    Snapshot of the most recent rate-limit headers.

    Fields:
        limit:      Total allowance in the current window.
        remaining:  Remaining allowance.
        reset_at:   When the window resets.
        used:       Requests used in the current window.
    """

    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset_at: Optional[datetime] = None
    used: Optional[int] = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    """
    Thin async wrapper around `httpx.AsyncClient` for GitHub.

    The client caches a single `httpx.AsyncClient` instance and
    reuses it for every request. Callers SHOULD create one client
    per process and share it.

    The client is safe to share across coroutines. Its internal
    mutable state is limited to the httpx client and a rate-limit
    snapshot.
    """

    def __init__(
        self,
        *,
        config: Optional[GitHubApiConfig] = None,
        http_client: Optional[Any] = None,
    ) -> None:
        """
        Create a GitHub HTTP client.

        Args:
            config: Optional configuration. Defaults to a fresh
                `GitHubApiConfig`.
            http_client: Optional pre-built `httpx.AsyncClient`.
                When provided, the client uses it directly and does
                NOT own its lifecycle (i.e. `close` will not close
                it). This is intended for tests and for applications
                that manage the httpx client themselves.
        """
        self._config = config or GitHubApiConfig()
        self._owns_client = http_client is None
        self._http = http_client or self._build_default_client()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._rate_limit = GitHubRateLimit()

    @property
    def config(self) -> GitHubApiConfig:
        """Return the client's configuration."""
        return self._config

    @property
    def api_url(self) -> str:
        """Return the base URL of the REST API."""
        return self._config.api_url

    @property
    def rate_limit(self) -> GitHubRateLimit:
        """Return the most recent rate-limit snapshot."""
        return self._rate_limit

    @property
    def is_closed(self) -> bool:
        """Return True after `close` has been called."""
        return self._closed

    # --- Lifecycle ------------------------------------------------------

    def _build_default_client(self) -> Any:
        """
        Build the default `httpx.AsyncClient`.

        The import is performed lazily so that this module can be
        imported even when httpx is not installed. In that case,
        callers MUST provide their own `http_client`.
        """
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ProviderError(
                "The `httpx` package is required for GitHub HTTP "
                "requests.",
                details={"package": "httpx"},
                cause=exc,
            ) from exc

        return httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
            follow_redirects=self._config.follow_redirects,
        )

    async def close(self) -> None:
        """
        Close the underlying HTTP client.

        Idempotent: safe to call multiple times. Only closes the
        httpx client when the caller did not supply one.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_client:
                try:
                    await self._http.aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Error while closing GitHub HTTP client: %s",
                        type(exc).__name__,
                    )

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --- High-level API -------------------------------------------------

    async def get_authenticated_account(
        self, token: str
    ) -> ProviderAccount:
        """
        Fetch the account associated with `token` via `/user`.

        The returned `ProviderAccount` uses the account's stable
        numeric ID (as a string) as `account_id`.

        Raises:
            ValidationError: If `token` is empty.
            AuthRequiredError: If GitHub rejects the token.
            NetworkError: On transport failure.
        """
        if not token:
            raise ValidationError("token must not be empty.")

        payload = await self.get_json("/user", token=token)
        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for /user."
            )

        account_id_raw = payload.get("id")
        if account_id_raw is None:
            raise ProviderError(
                "GitHub did not return an account id."
            )
        account_id = str(account_id_raw)

        login = str(payload.get("login") or account_id)
        display_name = _opt_str(payload.get("name")) or login
        email = _opt_str(payload.get("email"))
        avatar = _opt_str(payload.get("avatar_url"))

        # GitHub exposes the API base URL via `url`. We prefer the
        # client's configured `api_url` so that the account key is
        # stable when the client is configured for an Enterprise
        # instance.
        base_url = self._config.api_url

        return ProviderAccount(
            provider=ProviderId.GITHUB,
            account_id=account_id,
            username=login,
            display_name=display_name,
            email=email,
            avatar_url=avatar,
            base_url=base_url,
            connected=True,
            last_validated_at=datetime.now(timezone.utc),
        )

    # --- Verb helpers ---------------------------------------------------

    async def get_json(
        self,
        path: str,
        *,
        token: str,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """GET `path` and return the parsed JSON body."""
        return await self.request(
            "GET",
            path,
            token=token,
            params=params,
            headers=headers,
            expected_json=True,
        )

    async def post_json(
        self,
        path: str,
        *,
        token: str,
        json_body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """POST `json_body` to `path` and return the parsed JSON body."""
        return await self.request(
            "POST",
            path,
            token=token,
            json_body=json_body,
            params=params,
            headers=headers,
            expected_json=True,
        )

    async def patch_json(
        self,
        path: str,
        *,
        token: str,
        json_body: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """PATCH `json_body` to `path` and return the parsed JSON body."""
        return await self.request(
            "PATCH",
            path,
            token=token,
            json_body=json_body,
            headers=headers,
            expected_json=True,
        )

    async def delete_json(
        self,
        path: str,
        *,
        token: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """DELETE `path` and return the parsed JSON body, if any."""
        return await self.request(
            "DELETE",
            path,
            token=token,
            headers=headers,
            expected_json=False,
        )

    # --- Core request ---------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: Optional[str],
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        expected_json: bool = True,
    ) -> Any:
        """
        Execute a single HTTP request against the GitHub API.

        Retries transient failures (network errors and 5xx
        responses) up to `config.max_retries` times with exponential
        backoff. Respects `Retry-After` on 429 and 503 responses.

        Args:
            method: HTTP verb. Case-insensitive.
            path: API path, starting with `/`. Absolute URLs are
                NOT accepted; the path is always joined to the
                configured `api_url`.
            token: Optional credential. When `None`, the request is
                sent unauthenticated. This is useful for public
                endpoints.
            params: Optional query string parameters.
            json_body: Optional JSON request body.
            headers: Optional extra headers. The `Authorization`
                header, when supplied, is ignored; the token is the
                authoritative credential source.
            expected_json: When True, the response body is parsed
                as JSON. When False, the response body (if any) is
                parsed only when the Content-Type indicates JSON.

        Raises:
            ValidationError: For malformed input.
            SourceControlError: For any HTTP error. The specific
                subclass depends on the status code.
            NetworkError: For transport failures after retries are
                exhausted.
        """
        if self._closed:
            raise ProviderError("GitHub client is closed.")

        method_upper = (method or "").upper()
        if method_upper not in ("GET", "POST", "PATCH", "DELETE", "PUT"):
            raise ValidationError(
                f"Unsupported HTTP method: {method!r}."
            )
        if not path or not isinstance(path, str):
            raise ValidationError("path must be a non-empty string.")
        if not path.startswith("/"):
            raise ValidationError(
                "path must start with '/'.",
                details={"path": path},
            )
        if "://" in path:
            raise ValidationError(
                "Absolute URLs are not accepted; use a path.",
                details={"path": path},
            )

        url = f"{self._config.api_url}{path}"
        request_headers = self._build_headers(
            token=token, extra=headers
        )

        # Reset any stale rate-limit information from a prior call.
        self._rate_limit = GitHubRateLimit()

        max_attempts = self._config.max_retries + 1
        last_exception: Optional[BaseException] = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._http.request(
                    method_upper,
                    url,
                    headers=request_headers,
                    params=dict(params) if params else None,
                    json=dict(json_body) if json_body else None,
                )
            except Exception as exc:  # noqa: BLE001
                # Transport-level failure. Retry if we have budget.
                if not _is_transient_transport_error(exc):
                    raise _translate_transport_error(exc) from exc
                last_exception = exc
                if attempt >= max_attempts:
                    break
                await self._sleep_backoff(attempt)
                continue

            # Capture rate-limit headers for observability.
            self._update_rate_limit(response)

            status = getattr(response, "status_code", 0)

            # Success path.
            if 200 <= status < 300:
                if not expected_json:
                    return _maybe_parse_json(response)
                return _parse_json_or_raise(response, url=url)

            # Server errors are retryable.
            if 500 <= status < 600:
                if attempt < max_attempts:
                    await self._sleep_backoff(
                        attempt, retry_after=self._retry_after(response)
                    )
                    continue
                raise _translate_status_error(response, url=url)

            # Rate limit responses. GitHub uses 403 with
            # `X-RateLimit-Remaining: 0` and 429 for secondary
            # limits.
            if status in (403, 429) and _is_rate_limited(response):
                retry_after = self._retry_after(response)
                if attempt < max_attempts and retry_after is not None:
                    await self._sleep_backoff(
                        attempt, retry_after=retry_after
                    )
                    continue
                raise _rate_limit_error(response, url=url)

            # Everything else is non-retryable.
            raise _translate_status_error(response, url=url)

        # If we exit the loop without returning, we exhausted
        # retries. Convert the last exception to a normalized error.
        if last_exception is not None:
            raise NetworkError(
                "GitHub API request failed after retries.",
                details={"reason": type(last_exception).__name__},
                cause=last_exception,
            )

        raise ProviderError(
            "GitHub API request failed without a response."
        )

    # --- Headers and rate limit ----------------------------------------

    def _build_headers(
        self,
        *,
        token: Optional[str],
        extra: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, str]:
        """
        Build the request headers.

        The token is placed in `Authorization: Bearer`. Extra
        headers are merged, but `Authorization` is never overridden
        by `extra`: the token argument is the authoritative source.
        """
        headers: Dict[str, str] = {
            "Accept": _ACCEPT,
            "User-Agent": self._config.user_agent,
            "X-GitHub-Api-Version": self._config.api_version,
        }
        for key, value in self._config.default_headers.items():
            if key.lower() == "authorization":
                # Defense-in-depth: never accept an Authorization
                # header from configuration.
                continue
            headers[key] = value
        if extra:
            for key, value in extra.items():
                if key.lower() == "authorization":
                    continue
                headers[key] = value
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _update_rate_limit(self, response: Any) -> None:
        """
        Capture rate-limit headers from `response`.
        """
        headers = getattr(response, "headers", None)
        if not headers:
            return
        self._rate_limit = GitHubRateLimit(
            limit=_safe_int(headers.get("x-ratelimit-limit")),
            remaining=_safe_int(headers.get("x-ratelimit-remaining")),
            reset_at=_parse_unix(
                _safe_int(headers.get("x-ratelimit-reset"))
            ),
            used=_safe_int(headers.get("x-ratelimit-used")),
        )

    @staticmethod
    def _retry_after(response: Any) -> Optional[float]:
        """
        Return the number of seconds to wait before retrying, or
        `None` when no `Retry-After` header is present.

        Handles both the integer-seconds and HTTP-date forms of the
        header.
        """
        headers = getattr(response, "headers", None)
        if not headers:
            return None
        raw = headers.get("retry-after")
        if not raw:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass

        # HTTP-date form.
        try:
            from email.utils import parsedate_to_datetime

            target = parsedate_to_datetime(raw)
            now = datetime.now(timezone.utc)
            delta = (target - now).total_seconds()
            return max(0.0, delta)
        except (TypeError, ValueError):
            return None

    @staticmethod
    async def _sleep_backoff(
        attempt: int, *, retry_after: Optional[float] = None
    ) -> None:
        """
        Sleep for a bounded exponential backoff period.

        The delay is `min(2 ** (attempt - 1), 8)` seconds, unless a
        `Retry-After` value is provided, in which case the larger of
        the two is used (capped at 60 seconds).
        """
        backoff = min(2 ** (attempt - 1), 8)
        if retry_after is not None:
            backoff = min(max(backoff, retry_after), 60)
        await asyncio.sleep(backoff)

    # --- Presentation ---------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"<GitHubClient api_url={self._config.api_url!r} "
            f"closed={self._closed} "
            f"rate_limit_remaining={self._rate_limit.remaining}>"
        )


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _parse_json_or_raise(response: Any, *, url: str) -> Any:
    """
    Parse the response body as JSON.

    Raises `ProviderError` when the body cannot be decoded. The URL
    is redacted before it appears in the error message.
    """
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(
            "GitHub returned a response that is not valid JSON.",
            details={"url": redact_url(url)},
            cause=exc,
        ) from exc


def _maybe_parse_json(response: Any) -> Any:
    """
    Parse the response body as JSON when the Content-Type indicates
    JSON, otherwise return the raw text.
    """
    headers = getattr(response, "headers", None) or {}
    content_type = (headers.get("content-type") or "").lower()
    if "json" in content_type:
        try:
            return response.json()
        except Exception:  # noqa: BLE001
            return None
    text = getattr(response, "text", None)
    return text if text else None


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _is_transient_transport_error(exc: BaseException) -> bool:
    """
    Return True for transport errors that merit a retry.

    `httpx` exceptions are inspected by name to avoid a hard
    dependency on httpx. Any exception whose class name includes
    `Timeout`, `TransportError`, `ConnectError`, `ReadTimeout`, or
    `RemoteProtocolError` is treated as transient.
    """
    name = type(exc).__name__.lower()
    if any(
        keyword in name
        for keyword in (
            "timeout",
            "transport",
            "connect",
            "read",
            "write",
            "remote",
            "network",
            "dns",
            "protocol",
        )
    ):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return False


def _translate_transport_error(exc: BaseException) -> SourceControlError:
    """Map a transport failure to a `NetworkError`."""
    return NetworkError(
        "GitHub API request failed due to a network error.",
        details={"reason": type(exc).__name__},
        cause=exc,
    )


def _is_rate_limited(response: Any) -> bool:
    """
    Return True when `response` indicates a rate limit.

    GitHub reports primary limits with a 403 status and a
    `x-ratelimit-remaining: 0` header. Secondary limits may use 429.
    """
    status = getattr(response, "status_code", 0)
    headers = getattr(response, "headers", None) or {}
    if status == 429:
        return True
    if status == 403:
        remaining = headers.get("x-ratelimit-remaining")
        if remaining == "0":
            return True
        # Secondary rate limit advisory.
        if headers.get("retry-after"):
            return True
    return False


def _rate_limit_error(response: Any, *, url: str) -> SourceControlError:
    """Build a `RateLimitError` from a rate-limited response."""
    headers = getattr(response, "headers", None) or {}
    reset_at = _parse_unix(_safe_int(headers.get("x-ratelimit-reset")))
    retry_after = _safe_int(headers.get("retry-after"))
    details: Dict[str, Any] = {"url": redact_url(url)}
    if reset_at is not None:
        details["reset_at"] = reset_at.isoformat()
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    return RateLimitError(
        "GitHub rate limit exceeded.",
        details=details,
    )


def _translate_status_error(
    response: Any, *, url: str
) -> SourceControlError:
    """
    Map an HTTP status to a `SourceControlError` subclass.

    The response body is NOT included in the error message. Only
    the status code and a short description are surfaced, so that
    accidental secrets in the body cannot leak into logs.
    """
    status = getattr(response, "status_code", 0)
    details = {"url": redact_url(url), "status": status}

    # Extract a short message from the body when it is JSON with a
    # `message` field. The message is redacted before being used.
    message = _extract_message(response)
    if message:
        details["provider_message"] = redact_text(message)[:300]

    if status == 400:
        return ValidationError(
            "GitHub rejected the request as invalid.", details=details
        )
    if status == 401:
        return AuthRequiredError(
            "GitHub rejected the credential.", details=details
        )
    if status == 403:
        return PermissionDeniedError(
            "GitHub denied the request.", details=details
        )
    if status == 404:
        return NotFoundError(
            "GitHub resource not found.", details=details
        )
    if status == 405:
        return ValidationError(
            "HTTP method not allowed for this resource.",
            details=details,
        )
    if status == 409:
        return ConflictError(
            "GitHub reported a conflict.", details=details
        )
    if status == 422:
        return ValidationError(
            "GitHub rejected the request as unprocessable.",
            details=details,
        )
    if status is not None and 500 <= status < 600:
        return ProviderError(
            f"GitHub returned a server error ({status}).",
            details=details,
        )
    return ProviderError(
        f"GitHub returned an unexpected status ({status}).",
        details=details,
    )


def _extract_message(response: Any) -> Optional[str]:
    """
    Return the `message` field from a JSON response body, when
    present.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(body, Mapping):
        raw = body.get("message")
        if isinstance(raw, str):
            return raw
    return None


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _safe_int(value: Any) -> Optional[int]:
    """Return `value` as an int, or `None` when not convertible."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_unix(value: Optional[int]) -> Optional[datetime]:
    """Convert a Unix-seconds value to a timezone-aware datetime."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _opt_str(value: Any) -> Optional[str]:
    """Return `value` as a string, or `None` when empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
