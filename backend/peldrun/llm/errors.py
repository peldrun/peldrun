# backend/peldrun/llm/errors.py
"""
PELDRUN Unified Provider Error Taxonomy.

Every provider adapter (built-in or external) maps its SDK's exceptions
to the types defined here. Downstream code (accounting, dispatch,
diagnostics, UI) only ever inspects the canonical taxonomy — it never
needs to know whether the underlying error came from OpenAI, Anthropic,
Google, httpx, or an SDK-specific class.

Design invariants
-----------------

1. **Canonical taxonomy only at the boundary.** Adapters raise subclasses
   of ``ProviderError``. The runtime layer (executor, dispatcher) never
   sees a raw ``openai.APIError`` or ``anthropic.RateLimitError``.
   This decouples the runtime from every SDK's error shape.

2. **Retryability is a property, not a caller decision.** Each error
   class declares ``retryable`` as a class attribute. Callers consult
   ``error.retryable`` instead of hard-coding "retry on 429" logic per
   call site. This is the single source of truth for retry policy.

3. **Cancellation is NOT a ProviderError.** ``asyncio.CancelledError``
   MUST propagate unwrapped. Wrapping it would make the runtime treat
   a user-initiated Stop button as a provider failure — exactly the bug
   the plan calls out (Section 3.1, "re-attempt after streaming
   failure"). A specific helper (``is_cancellation``) checks for it so
   call sites do not accidentally swallow it inside a broad ``except``.

4. **Provider identity is preserved.** Every error carries ``provider``
   and ``model`` so that accounting, diagnostics, and the UI can show
   *which* provider failed, without parsing the message string.

5. **Classification is deterministic for HTTP status codes.** A 401 is
   always auth, a 429 is always rate limit, a 5xx is always unavailable.
   Adapter-specific classification (e.g. Anthropic's "overloaded_error"
   in a 200 body) is layered on top by the adapter's own mapper — this
   module only handles the HTTP/transport baseline.

See also
--------

    * ``backend/peldrun/llm/contracts.py`` — adapter Protocol
    * ``backend/peldrun/llm/providers/registry.py`` — M2 (uses registry errors)
    * ``backend/peldrun/llm/accounting.py`` — M8 (records failures)
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Base error
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """
    Base class for all provider-related errors.

    Subclasses declare a class-level ``code`` (stable string identifier
    for logging, telemetry, and UI) and ``retryable`` (retry policy
    decision). Both may be overridden per-instance when the specific
    error carries more nuance than the class-level default.
    """

    code: str = "provider_error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        status_code: Optional[int] = None,
        retryable: Optional[bool] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.details: Dict[str, Any] = details or {}
        self.__cause__ = cause

        if retryable is not None:
            # Per-instance override. Used when a specific error within a
            # usually-retryable class is actually permanent (e.g. a 429
            # that also carries "insufficient quota" — retrying will
            # never succeed).
            self.retryable = retryable

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize for structured logging, SSE error payloads, and
        accounting ``failure_reason`` fields.

        The shape is stable — consumers may rely on these keys.
        """
        return {
            "code": self.code,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "details": self.details,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"code={self.code!r}, provider={self.provider!r}, "
            f"model={self.model!r}, message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# Authentication / authorization
# ---------------------------------------------------------------------------


class ProviderAuthError(ProviderError):
    """
    Credentials were rejected by the provider.

    Typically a 401 or 403. NOT retryable — the same credentials will
    fail identically until the user rotates them.
    """

    code = "auth_failed"
    retryable = False


class ProviderPermissionError(ProviderAuthError):
    """
    Credentials are valid but lack permission for the requested action
    (e.g. model access denied for this API key tier).

    Distinct from plain auth failure so the UI can suggest
    "upgrade your plan" vs "check your API key".
    """

    code = "permission_denied"


# ---------------------------------------------------------------------------
# Rate limit / quota
# ---------------------------------------------------------------------------


class ProviderRateLimitError(ProviderError):
    """
    The provider rejected the request due to rate limiting.

    Retryable with backoff (the caller is expected to honor
    ``retry_after_seconds`` when present). The ``retry_after_seconds``
    field is populated from the ``Retry-After`` header or from the
    provider's error body when available.
    """

    code = "rate_limited"
    retryable = True

    def __init__(self, *args: Any, retry_after_seconds: Optional[float] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class ProviderQuotaExceededError(ProviderError):
    """
    The account has no remaining quota or credits.

    NOT retryable — retrying will never succeed until the user tops up
    their account. Distinct from rate limit so the UI can show the
    correct billing message (plan Section 4 / smart error formatting).
    """

    code = "quota_exceeded"
    retryable = False


# ---------------------------------------------------------------------------
# Transport / availability
# ---------------------------------------------------------------------------


class ProviderTimeoutError(ProviderError):
    """
    The provider did not respond within the configured timeout.

    Retryable: a transient network stall is a common cause. The adapter
    SHOULD distinguish first-chunk timeout from inter-chunk timeout
    (plan Section 8, "STREAM_INTER_CHUNK_TIMEOUT") so the message is
    actionable. The ``phase`` field carries this distinction when
    available (``"connect"``, ``"first_chunk"``, ``"inter_chunk"``).
    """

    code = "timeout"
    retryable = True

    def __init__(self, *args: Any, phase: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.phase = phase


class ProviderUnavailableError(ProviderError):
    """
    The provider is reachable but returning server errors (5xx), or
    the connection could not be established at all.

    Retryable with backoff. ``status_code`` (when present) distinguishes
    "server up but broken" (500/502/503) from "server not reachable"
    (``None``).
    """

    code = "unavailable"
    retryable = True


class ProviderConnectionError(ProviderUnavailableError):
    """
    Network-level failure — DNS, TCP, TLS, or connection reset.

    Subclass of ``ProviderUnavailableError`` so retry policy treats both
    the same. Distinct so logs and diagnostics can tell "wrong URL"
    from "provider is down".
    """

    code = "connection_failed"


# ---------------------------------------------------------------------------
# Request validation / unsupported
# ---------------------------------------------------------------------------


class ProviderInvalidRequestError(ProviderError):
    """
    The provider rejected the request as malformed (400 / 422).

    NOT retryable — the same payload will be rejected identically.
    The runtime usually maps this to a "check your request" UI hint.
    """

    code = "invalid_request"
    retryable = False


class ProviderContextLengthError(ProviderInvalidRequestError):
    """
    The request exceeded the model's context window.

    Subclass of ``ProviderInvalidRequestError`` so retry policy is
    inherited, but with distinct ``code`` so the UI can offer the
    "reduce context" hint (matching the existing
    ``format_smart_error`` behavior in the bridge).

    ``actual_tokens`` and ``max_tokens`` are populated when the provider
    returns them; otherwise both are ``None``.
    """

    code = "context_length_exceeded"

    def __init__(
        self,
        *args: Any,
        actual_tokens: Optional[int] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actual_tokens = actual_tokens
        self.max_tokens = max_tokens


class ProviderUnsupportedFeatureError(ProviderError):
    """
    The provider does not accept a parameter the caller sent, or does
    not support the requested mode.

    This is the error the RequestCompiler (M4) is designed to PREVENT.
    When it still occurs — usually because the user force-enabled a
    capability via manual override that the adapter could not honor —
    the runtime surfaces this error with the specific ``feature`` so
    the UI can point at the exact setting to disable.

    NOT retryable.
    """

    code = "unsupported_feature"
    retryable = False

    def __init__(self, *args: Any, feature: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.feature = feature


# ---------------------------------------------------------------------------
# Content / moderation
# ---------------------------------------------------------------------------


class ProviderContentPolicyError(ProviderError):
    """
    The provider blocked the request or response due to content policy.

    NOT retryable — the same content will always be blocked. The runtime
    surfaces this with a "rephrase the request" hint.
    """

    code = "content_policy"
    retryable = False


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class ProviderResponseParseError(ProviderError):
    """
    The provider returned a response that could not be parsed into the
    expected shape.

    Distinct from ``ProviderUnavailableError`` because retrying is
    usually pointless (the provider's bug is deterministic) and the
    diagnostic value is high (which field was missing, which schema
    violated).
    """

    code = "response_parse_error"
    retryable = False


# ---------------------------------------------------------------------------
# Registry errors (not provider runtime errors — registration-time only)
# ---------------------------------------------------------------------------


class ProviderNotRegisteredError(ProviderError):
    """
    The requested ``provider_id`` has no adapter registered.

    Raised by ``provider_registry`` and by ``get_provider()`` when the
    identifier does not match any built-in, external, or runtime
    registration.
    """

    code = "provider_not_registered"


class ProviderDisabledError(ProviderError):
    """
    The provider is registered but explicitly disabled.

    Distinct from ``ProviderNotRegisteredError`` so the UI can show
    "enable this provider" instead of "install this provider".
    """

    code = "provider_disabled"


class ProviderContractIncompatibleError(ProviderError):
    """
    The adapter declares an ``api_contract`` version that this PELDRUN
    build does not support.

    Raised at registration time, never at request time — an incompatible
    adapter is rejected before any connection is made, so the user never
    sees a mysterious runtime failure.
    """

    code = "provider_contract_incompatible"


class ProviderAlreadyRegisteredError(ProviderError):
    """
    A registration with the same ``provider_id`` already exists.

    The registry supports explicit replacement via
    ``overwrite=True``. When not requested, this error prevents silent
    clobbering of a built-in adapter by an external package.
    """

    code = "provider_already_registered"


# ---------------------------------------------------------------------------
# Cancellation helper
# ---------------------------------------------------------------------------


def is_cancellation(exc: BaseException) -> bool:
    """
    Return True iff ``exc`` is a cancellation signal that MUST propagate
    unwrapped.

    Covers both ``asyncio.CancelledError`` (Python 3.8+; in 3.8+ it no
    longer inherits from ``Exception`` but from ``BaseException``) and
    the ``concurrent.futures.CancelledError`` raised by some SDKs.

    Call sites should check this BEFORE any broad ``except Exception``
    branch that would otherwise swallow the cancellation.
    """
    if isinstance(exc, asyncio.CancelledError):
        return True
    # ``concurrent.futures.CancelledError`` is a distinct class that
    # shares the name. Import lazily to avoid a runtime import at
    # module load (not needed in most deployments).
    try:
        import concurrent.futures as _cf

        if isinstance(exc, _cf.CancelledError):
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# HTTP status code classifier
# ---------------------------------------------------------------------------


#: Map of common HTTP status codes to the canonical error class.
#: This is the baseline every adapter applies before layering its own
#: SDK-specific classification on top.
_HTTP_STATUS_TO_CLASS: Dict[int, type] = {
    400: ProviderInvalidRequestError,
    401: ProviderAuthError,
    402: ProviderQuotaExceededError,
    403: ProviderPermissionError,
    404: ProviderInvalidRequestError,   # usually a wrong model / endpoint
    408: ProviderTimeoutError,
    409: ProviderInvalidRequestError,
    413: ProviderContextLengthError,
    422: ProviderInvalidRequestError,
    429: ProviderRateLimitError,
    500: ProviderUnavailableError,
    502: ProviderUnavailableError,
    503: ProviderUnavailableError,
    504: ProviderUnavailableError,
    529: ProviderUnavailableError,      # Anthropic "overloaded"
}


def classify_http_status(
    status_code: int,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    message: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    retry_after_seconds: Optional[float] = None,
) -> ProviderError:
    """
    Build a canonical ``ProviderError`` for the given HTTP status.

    This is a helper, not a substitute for adapter-level classification.
    Adapters SHOULD use it as the baseline, then refine:

    * Check ``body`` for provider-specific error codes that imply a
      different class (e.g. Anthropic's ``"overloaded_error"`` inside
      a 200 response — the adapter promotes it to
      ``ProviderUnavailableError``).
    * Detect context-length errors by scanning the message body for
      keywords when the status is 400/413 but the provider does not
      use those codes consistently.

    Parameters
    ----------
    status_code
        HTTP status returned by the provider (or synthesized by the SDK).
    provider / model
        Identity for the error object's telemetry fields.
    message
        Human-readable message extracted from the response body. When
        ``None``, a message is synthesized from the status code.
    body
        Raw parsed JSON body, when available. Currently unused by this
        baseline classifier — kept in the signature so adapters can
        pass it through for their own refinement without re-wrapping.
    retry_after_seconds
        Populated from the ``Retry-After`` header when present.
        Only meaningful for ``ProviderRateLimitError``.
    """
    exc_class = _HTTP_STATUS_TO_CLASS.get(status_code, ProviderUnavailableError)

    error_message = message or f"Provider returned HTTP {status_code}."

    kwargs: Dict[str, Any] = {
        "provider": provider,
        "model": model,
        "status_code": status_code,
    }

    if exc_class is ProviderRateLimitError and retry_after_seconds is not None:
        return ProviderRateLimitError(
            error_message,
            retry_after_seconds=retry_after_seconds,
            **kwargs,
        )

    if exc_class is ProviderTimeoutError:
        return ProviderTimeoutError(
            error_message,
            phase="connect",
            **kwargs,
        )

    return exc_class(error_message, **kwargs)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Message-based refinement helpers
# ---------------------------------------------------------------------------


#: Substrings that reliably indicate a context-length error regardless
#: of the HTTP status the provider attached. Providers are notoriously
#: inconsistent here — OpenAI uses 400, Anthropic uses 400, Google uses
#: 400, Ollama uses 500 with a specific message. Scanning the message
#: catches all of them without per-provider branching at the call site.
_CONTEXT_LENGTH_HINTS = (
    "maximum context length",
    "context length exceeded",
    "context_length_exceeded",
    "prompt is too long",
    "input is too long",
    "exceeds the maximum",
    "too many tokens",
    "token limit exceeded",
)

_AUTH_HINTS = (
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "authentication failed",
    "unauthorized",
    "invalid token",
)

_QUOTA_HINTS = (
    "insufficient quota",
    "insufficient credit",
    "credit balance",
    "billing",
    "exceeded your current quota",
)


def refine_by_message(
    error: ProviderError,
    raw_message: str,
) -> ProviderError:
    """
    Refine a baseline-classified error using the provider's message text.

    Providers are inconsistent about which HTTP status maps to which
    condition. This helper catches the well-known misclassifications:

    * A 400 or 422 that is actually a context-length error.
    * A 429 that is actually a permanent quota exhaustion (not retryable).
    * A 400 that is actually an auth error (some providers return 400
      instead of 401 when the API key is malformed rather than absent).

    Adapters SHOULD call this after ``classify_http_status`` — never
    instead of it — so that the status-code baseline is always present.
    The refinement never downgrades to a LESS specific class; it only
    promotes to a sibling class when the message clearly indicates one.
    """
    if not raw_message:
        return error

    lower = raw_message.lower()

    # Context length — refine 400/422 into ContextLengthError.
    if isinstance(error, ProviderInvalidRequestError) and not isinstance(
        error, ProviderContextLengthError
    ):
        if any(hint in lower for hint in _CONTEXT_LENGTH_HINTS):
            return ProviderContextLengthError(
                raw_message,
                provider=error.provider,
                model=error.model,
                status_code=error.status_code,
            )

    # Quota — refine 429 into QuotaExceeded when permanent.
    if isinstance(error, ProviderRateLimitError):
        if any(hint in lower for hint in _QUOTA_HINTS):
            return ProviderQuotaExceededError(
                raw_message,
                provider=error.provider,
                model=error.model,
                status_code=error.status_code,
            )

    # Auth — refine a 400 that mentions API key problems.
    if isinstance(error, ProviderInvalidRequestError) and not isinstance(
        error, (ProviderContextLengthError, ProviderUnsupportedFeatureError)
    ):
        if any(hint in lower for hint in _AUTH_HINTS):
            return ProviderAuthError(
                raw_message,
                provider=error.provider,
                model=error.model,
                status_code=error.status_code,
            )

    return error


__all__ = [
    # Base
    "ProviderError",
    # Auth
    "ProviderAuthError",
    "ProviderPermissionError",
    # Rate limit / quota
    "ProviderRateLimitError",
    "ProviderQuotaExceededError",
    # Transport
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "ProviderConnectionError",
    # Validation
    "ProviderInvalidRequestError",
    "ProviderContextLengthError",
    "ProviderUnsupportedFeatureError",
    # Content
    "ProviderContentPolicyError",
    # Parsing
    "ProviderResponseParseError",
    # Registry
    "ProviderNotRegisteredError",
    "ProviderDisabledError",
    "ProviderContractIncompatibleError",
    "ProviderAlreadyRegisteredError",
    # Helpers
    "is_cancellation",
    "classify_http_status",
    "refine_by_message",
]
