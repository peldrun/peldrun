# backend/integrations/source_control/errors.py
"""
Error taxonomy for the Source Control Integration subsystem.

All exceptions raised by the subsystem MUST inherit from
`SourceControlError`. This guarantees that callers can:

    - Catch every subsystem error with a single `except` clause.
    - Map every error to a stable `SourceControlErrorCode` for API
      responses, audit logs, and agent tool results.
    - Detect retryable errors via the `is_retryable` property.
    - Obtain a redacted, human-readable message that never leaks
      tokens, credentials, or provider-internal identifiers.

Design rules:
    - Exceptions MUST NOT carry secrets in `details`.
    - Messages MUST be redacted before being surfaced to the user or
      the model. The `redact_message` helper is invoked automatically
      in the constructor.
    - The error hierarchy is intentionally shallow (2 levels) to keep
      `except` clauses predictable.
    - The `code` attribute is a `SourceControlErrorCode` enum member,
      not a string, so that HTTP routers can switch on it safely.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from .domain.enums import SourceControlErrorCode

__all__ = [
    "SourceControlError",
    # Authentication / authorization
    "AuthRequiredError",
    "AuthExpiredError",
    "PermissionDeniedError",
    # Resource existence
    "NotFoundError",
    # Rate limiting and transport
    "RateLimitError",
    "NetworkError",
    "ProviderError",
    # State and validation
    "ConflictError",
    "ValidationError",
    "RemoteStateChangedError",
    "UnsupportedCapabilityError",
    # Local workspace and Git
    "WorkspaceNotFoundError",
    "WorkspaceDirtyError",
    "PathTraversalError",
    "CommandTimeoutError",
    "CommandFailedError",
    # Approval and policy
    "ApprovalRequiredError",
    "ApprovalInvalidError",
    "ApprovalExpiredError",
    "ApprovalConsumedError",
    "PolicyDeniedError",
    "ProtectedBranchError",
    "WorkflowFileError",
    # Helpers
    "to_source_control_error",
    "error_code_of",
    "is_retryable_error",
    "redact_message",
]


# ---------------------------------------------------------------------------
# Message redaction
# ---------------------------------------------------------------------------


# Patterns applied to every error message before it is exposed to the
# outside world. Each pattern targets a specific class of secret. The
# list is intentionally conservative: it errs on the side of redacting
# too much rather than leaking a credential.
_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,255}\b"),
    # GitLab tokens
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,255}\b"),
    # Bitbucket app passwords / tokens
    re.compile(r"\bATBB[A-Za-z0-9]{16,}\b"),
    # Generic bearer tokens in Authorization headers
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{20,}"),
    # Basic auth
    re.compile(r"(?i)authorization\s*:\s*basic\s+[A-Za-z0-9+/=]{8,}"),
    # URLs with embedded credentials: scheme://user:pass@host
    re.compile(r"([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"),
    # Private key markers
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    # OAuth client secrets in query strings
    re.compile(r"(?i)(client_secret=)[^&\s]+"),
    re.compile(r"(?i)(code=)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(refresh_token=)[^&\s]+"),
    re.compile(r"(?i)(access_token=)[^&\s]+"),
)


def redact_message(message: str) -> str:
    """
    Return `message` with any recognizable secret replaced by
    `***REDACTED***`.

    The function is idempotent and safe to call multiple times. It is
    applied automatically in `SourceControlError.__init__`, but callers
    may also invoke it directly before logging.
    """
    if not message:
        return message
    redacted = message
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted


def _redact_match(match: re.Match[str]) -> str:
    """
    Redaction callback that preserves non-secret prefixes when the
    pattern captures them (e.g. `client_secret=`).
    """
    if match.lastindex and match.lastindex >= 1:
        prefix = match.group(1)
        return f"{prefix}***REDACTED***"
    return "***REDACTED***"


# ---------------------------------------------------------------------------
# Base exception
# ---------------------------------------------------------------------------


class SourceControlError(Exception):
    """
    Base class for every error raised by the subsystem.

    Attributes:
        code:         Stable error category from `SourceControlErrorCode`.
        message:      Redacted, human-readable message.
        details:      Immutable mapping of non-secret context.
        retryable:    Whether the operation may be retried safely.
        http_status:  Suggested HTTP status code for API responses.
    """

    #: Overridden by subclasses to declare the error category.
    default_code: SourceControlErrorCode = SourceControlErrorCode.PROVIDER_ERROR

    #: Overridden by subclasses to declare retry semantics.
    default_retryable: bool = False

    #: Overridden by subclasses to declare an HTTP status hint.
    default_http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: Optional[SourceControlErrorCode] = None,
        details: Optional[Mapping[str, Any]] = None,
        retryable: Optional[bool] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        safe_message = redact_message(message)
        super().__init__(safe_message)
        self.message = safe_message
        self.code = code or self.default_code
        self.details: Mapping[str, Any] = dict(details or {})
        self.retryable = (
            self.default_retryable if retryable is None else retryable
        )
        self.http_status = self.default_http_status
        if cause is not None:
            self.__cause__ = cause

    @property
    def is_retryable(self) -> bool:
        """Return True if the caller may retry the operation safely."""
        return bool(self.retryable)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the error for API responses and audit logs.

        The returned mapping is safe to log and to send to the
        frontend: `message` is already redacted, and `details` MUST
        NOT contain secrets by contract.
        """
        return {
            "code": self.code.value,
            "message": self.message,
            "details": dict(self.details),
            "retryable": self.retryable,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return f"[{self.code.value}] {self.message}"


# ---------------------------------------------------------------------------
# Authentication / authorization
# ---------------------------------------------------------------------------


class AuthRequiredError(SourceControlError):
    """Raised when no credential is present for the requested action."""

    default_code = SourceControlErrorCode.AUTH_REQUIRED
    default_retryable = False
    default_http_status = 401


class AuthExpiredError(SourceControlError):
    """Raised when the current credential has expired."""

    default_code = SourceControlErrorCode.AUTH_EXPIRED
    default_retryable = False
    default_http_status = 401


class PermissionDeniedError(SourceControlError):
    """Raised when the credential lacks the required permission."""

    default_code = SourceControlErrorCode.PERMISSION_DENIED
    default_retryable = False
    default_http_status = 403


# ---------------------------------------------------------------------------
# Resource existence
# ---------------------------------------------------------------------------


class NotFoundError(SourceControlError):
    """Raised when a repository, branch, or file cannot be found."""

    default_code = SourceControlErrorCode.NOT_FOUND
    default_retryable = False
    default_http_status = 404


# ---------------------------------------------------------------------------
# Rate limiting and transport
# ---------------------------------------------------------------------------


class RateLimitError(SourceControlError):
    """
    Raised when the provider rate limit has been exceeded.

    `details["retry_after_seconds"]` MAY carry the server's suggested
    backoff. Callers SHOULD respect it before retrying.
    """

    default_code = SourceControlErrorCode.RATE_LIMITED
    default_retryable = True
    default_http_status = 429


class NetworkError(SourceControlError):
    """Raised on transient transport failures (timeouts, DNS, TLS)."""

    default_code = SourceControlErrorCode.NETWORK_ERROR
    default_retryable = True
    default_http_status = 502


class ProviderError(SourceControlError):
    """
    Raised when the provider returns an unexpected error that does not
    map cleanly to any other category.
    """

    default_code = SourceControlErrorCode.PROVIDER_ERROR
    default_retryable = False
    default_http_status = 502


# ---------------------------------------------------------------------------
# State and validation
# ---------------------------------------------------------------------------


class ConflictError(SourceControlError):
    """Raised when the local or remote state conflicts with the request."""

    default_code = SourceControlErrorCode.CONFLICT
    default_retryable = False
    default_http_status = 409


class ValidationError(SourceControlError):
    """
    Raised when input fails validation before any side effect occurs.

    Note: this class is intentionally named `ValidationError` (not
    `SourceControlValidationError`) because it is referenced by that
    name across the subsystem and the test suite.
    """

    default_code = SourceControlErrorCode.VALIDATION_ERROR
    default_retryable = False
    default_http_status = 422


class RemoteStateChangedError(SourceControlError):
    """
    Raised when the remote branch or base commit changed between
    approval and execution. Always invalidates the approval ticket.
    """

    default_code = SourceControlErrorCode.REMOTE_STATE_CHANGED
    default_retryable = False
    default_http_status = 409


class UnsupportedCapabilityError(SourceControlError):
    """Raised when a provider does not support the requested operation."""

    default_code = SourceControlErrorCode.UNSUPPORTED_CAPABILITY
    default_retryable = False
    default_http_status = 501


# ---------------------------------------------------------------------------
# Local workspace and Git
# ---------------------------------------------------------------------------


class WorkspaceNotFoundError(SourceControlError):
    """Raised when a workspace ID does not resolve to a known workspace."""

    default_code = SourceControlErrorCode.WORKSPACE_NOT_FOUND
    default_retryable = False
    default_http_status = 404


class WorkspaceDirtyError(SourceControlError):
    """
    Raised when an operation requires a clean workspace but uncommitted
    changes are present. Never discards user changes automatically.
    """

    default_code = SourceControlErrorCode.WORKSPACE_DIRTY
    default_retryable = False
    default_http_status = 409


class PathTraversalError(SourceControlError):
    """Raised when a path escapes the configured workspace root."""

    default_code = SourceControlErrorCode.PATH_TRAVERSAL_DETECTED
    default_retryable = False
    default_http_status = 400


class CommandTimeoutError(SourceControlError):
    """Raised when a Git subprocess exceeds its allowed runtime."""

    default_code = SourceControlErrorCode.COMMAND_TIMEOUT
    default_retryable = True
    default_http_status = 504


class CommandFailedError(SourceControlError):
    """
    Raised when a Git subprocess exits with a non-zero status for a
    reason that is not covered by a more specific error class.
    """

    default_code = SourceControlErrorCode.COMMAND_FAILED
    default_retryable = False
    default_http_status = 500


# ---------------------------------------------------------------------------
# Approval and policy
# ---------------------------------------------------------------------------


class ApprovalRequiredError(SourceControlError):
    """Raised when an operation requires an approval that is missing."""

    default_code = SourceControlErrorCode.APPROVAL_REQUIRED
    default_retryable = False
    default_http_status = 428  # Precondition Required


class ApprovalInvalidError(SourceControlError):
    """
    Raised when an approval does not match the current operation
    snapshot (e.g. diff changed after approval).
    """

    default_code = SourceControlErrorCode.APPROVAL_INVALID
    default_retryable = False
    default_http_status = 409


class ApprovalExpiredError(SourceControlError):
    """Raised when an approval ticket has passed its expiry time."""

    default_code = SourceControlErrorCode.APPROVAL_EXPIRED
    default_retryable = False
    default_http_status = 410  # Gone


class ApprovalConsumedError(SourceControlError):
    """Raised when an approval ticket has already been consumed."""

    default_code = SourceControlErrorCode.APPROVAL_CONSUMED
    default_retryable = False
    default_http_status = 409


class PolicyDeniedError(SourceControlError):
    """Raised when the active policy forbids the requested operation."""

    default_code = SourceControlErrorCode.POLICY_DENIED
    default_retryable = False
    default_http_status = 403


class ProtectedBranchError(SourceControlError):
    """Raised when a push targets a protected branch without approval."""

    default_code = SourceControlErrorCode.PROTECTED_BRANCH
    default_retryable = False
    default_http_status = 403


class WorkflowFileError(SourceControlError):
    """
    Raised when a change modifies a workflow/CI file and the required
    elevated approval is missing.
    """

    default_code = SourceControlErrorCode.WORKFLOW_FILE_CHANGE
    default_retryable = False
    default_http_status = 428


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


# Mapping from `SourceControlErrorCode` to the corresponding class. Used
# by `to_source_control_error` to preserve the original category when
# wrapping foreign exceptions.
_CODE_TO_CLASS: dict[SourceControlErrorCode, type[SourceControlError]] = {
    SourceControlErrorCode.AUTH_REQUIRED: AuthRequiredError,
    SourceControlErrorCode.AUTH_EXPIRED: AuthExpiredError,
    SourceControlErrorCode.PERMISSION_DENIED: PermissionDeniedError,
    SourceControlErrorCode.NOT_FOUND: NotFoundError,
    SourceControlErrorCode.RATE_LIMITED: RateLimitError,
    SourceControlErrorCode.NETWORK_ERROR: NetworkError,
    SourceControlErrorCode.CONFLICT: ConflictError,
    SourceControlErrorCode.VALIDATION_ERROR: ValidationError,
    SourceControlErrorCode.REMOTE_STATE_CHANGED: RemoteStateChangedError,
    SourceControlErrorCode.UNSUPPORTED_CAPABILITY: UnsupportedCapabilityError,
    SourceControlErrorCode.PROVIDER_ERROR: ProviderError,
    SourceControlErrorCode.WORKSPACE_NOT_FOUND: WorkspaceNotFoundError,
    SourceControlErrorCode.WORKSPACE_DIRTY: WorkspaceDirtyError,
    SourceControlErrorCode.PATH_TRAVERSAL_DETECTED: PathTraversalError,
    SourceControlErrorCode.COMMAND_TIMEOUT: CommandTimeoutError,
    SourceControlErrorCode.COMMAND_FAILED: CommandFailedError,
    SourceControlErrorCode.APPROVAL_REQUIRED: ApprovalRequiredError,
    SourceControlErrorCode.APPROVAL_INVALID: ApprovalInvalidError,
    SourceControlErrorCode.APPROVAL_EXPIRED: ApprovalExpiredError,
    SourceControlErrorCode.APPROVAL_CONSUMED: ApprovalConsumedError,
    SourceControlErrorCode.POLICY_DENIED: PolicyDeniedError,
    SourceControlErrorCode.PROTECTED_BRANCH: ProtectedBranchError,
    SourceControlErrorCode.WORKFLOW_FILE_CHANGE: WorkflowFileError,
}


def to_source_control_error(
    exc: BaseException,
    *,
    default_code: Optional[SourceControlErrorCode] = None,
    default_message: Optional[str] = None,
) -> SourceControlError:
    """
    Normalize any exception into a `SourceControlError`.

    Rules:
        - If `exc` is already a `SourceControlError`, return it as-is.
        - If `exc` is a `TimeoutError` or `asyncio.TimeoutError`,
          return a `CommandTimeoutError`.
        - If `exc` is a `PermissionError`, return a `PermissionDeniedError`.
        - If `exc` is a `ValueError` or `TypeError`, return a
          `ValidationError`.
        - Otherwise, return a `ProviderError` (or the class matching
          `default_code`).
    """
    if isinstance(exc, SourceControlError):
        return exc

    # asyncio.TimeoutError is an alias of TimeoutError on Python 3.11+.
    if isinstance(exc, TimeoutError):
        return CommandTimeoutError(
            default_message or "Operation timed out.",
            cause=exc,
        )

    if isinstance(exc, PermissionError):
        return PermissionDeniedError(
            default_message or "Permission denied.",
            cause=exc,
        )

    if isinstance(exc, (ValueError, TypeError)):
        return ValidationError(
            default_message or redact_message(str(exc)),
            cause=exc,
        )

    if isinstance(exc, OSError):
        return NetworkError(
            default_message or "Network or OS error.",
            cause=exc,
        )

    if default_code is not None:
        cls = _CODE_TO_CLASS.get(default_code, ProviderError)
        return cls(
            default_message or redact_message(str(exc)),
            cause=exc,
        )

    return ProviderError(
        default_message or redact_message(str(exc)),
        cause=exc,
    )


def error_code_of(exc: BaseException) -> SourceControlErrorCode:
    """
    Return the `SourceControlErrorCode` associated with `exc`.

    Foreign exceptions are mapped using `to_source_control_error` so
    that callers always receive a stable code.
    """
    if isinstance(exc, SourceControlError):
        return exc.code
    return to_source_control_error(exc).code


def is_retryable_error(exc: BaseException) -> bool:
    """
    Return True if `exc` may be retried safely.

    The decision is delegated to `SourceControlError.is_retryable`
    after normalization.
    """
    if isinstance(exc, SourceControlError):
        return exc.is_retryable
    return to_source_control_error(exc).is_retryable
