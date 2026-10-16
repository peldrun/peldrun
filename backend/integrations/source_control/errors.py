from __future__ import annotations

from typing import Any, Mapping, Optional

from .domain.enums import SourceControlErrorCode


class SourceControlError(Exception):
    """Base exception for all source control integration errors."""

    def __init__(
        self,
        message: str,
        code: SourceControlErrorCode = SourceControlErrorCode.PROVIDER_ERROR,
        *,
        provider: Optional[str] = None,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
        diagnostic_code: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.provider = provider
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details) if details else {}
        self.diagnostic_code = diagnostic_code
        self.request_id = request_id

    def to_dict(self) -> dict[str, Any]:
        """Serializes the error to a safe dictionary suitable for API responses."""
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.provider:
            payload["provider"] = self.provider
        if self.diagnostic_code:
            payload["diagnostic_code"] = self.diagnostic_code
        if self.request_id:
            payload["request_id"] = self.request_id
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        prefix = f"[{self.code.value}]"
        if self.provider:
            prefix = f"[{self.provider}:{self.code.value}]"
        return f"{prefix} {self.message}"


class PermissionDeniedError(SourceControlError):
    """Raised when an operation is rejected due to insufficient privileges or policy denial."""

    def __init__(
        self,
        message: str = "Permission denied for the requested repository operation.",
        *,
        provider: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.PERMISSION_DENIED,
            provider=provider,
            retryable=False,
            details=details,
        )


class AuthenticationRequiredError(SourceControlError):
    """Raised when an operation requires an authenticated provider account."""

    def __init__(
        self,
        message: str = "Authentication required for this source control provider.",
        *,
        provider: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.AUTH_REQUIRED,
            provider=provider,
            retryable=False,
            details=details,
        )


class AuthenticationExpiredError(SourceControlError):
    """Raised when stored provider credentials or tokens have expired."""

    def __init__(
        self,
        message: str = "Provider session or token has expired. Re-authentication required.",
        *,
        provider: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.AUTH_EXPIRED,
            provider=provider,
            retryable=False,
            details=details,
        )


class NotFoundError(SourceControlError):
    """Raised when a remote repository, branch, file, or pull request is not found."""

    def __init__(
        self,
        message: str = "Requested source control resource not found.",
        *,
        provider: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        merged_details = dict(details) if details else {}
        if resource_type:
            merged_details["resource_type"] = resource_type
        if resource_id:
            merged_details["resource_id"] = resource_id

        super().__init__(
            message=message,
            code=SourceControlErrorCode.NOT_FOUND,
            provider=provider,
            retryable=False,
            details=merged_details,
        )


class RateLimitExceededError(SourceControlError):
    """Raised when provider API rate limits are exceeded."""

    def __init__(
        self,
        message: str = "Provider API rate limit exceeded.",
        *,
        provider: Optional[str] = None,
        retry_after_seconds: Optional[float] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        merged_details = dict(details) if details else {}
        if retry_after_seconds is not None:
            merged_details["retry_after_seconds"] = retry_after_seconds

        super().__init__(
            message=message,
            code=SourceControlErrorCode.RATE_LIMITED,
            provider=provider,
            retryable=True,
            details=merged_details,
        )
        self.retry_after_seconds = retry_after_seconds


class NetworkError(SourceControlError):
    """Raised on connection dropouts, DNS failures, or remote gateway timeouts."""

    def __init__(
        self,
        message: str = "Network communication error with source control provider.",
        *,
        provider: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.NETWORK_ERROR,
            provider=provider,
            retryable=True,
            details=details,
        )


class ConflictError(SourceControlError):
    """Raised when an operation encounters a state conflict (e.g., branch already exists)."""

    def __init__(
        self,
        message: str = "Source control operation encountered a conflicting state.",
        *,
        provider: Optional[str] = None,
        code: SourceControlErrorCode = SourceControlErrorCode.CONFLICT,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            provider=provider,
            retryable=False,
            details=details,
        )


class RemoteStateChangedError(ConflictError):
    """Raised when the remote base branch SHA has moved, invalidating local diffs and approvals."""

    def __init__(
        self,
        message: str = "Remote base state changed since diff generation. Re-inspection required.",
        *,
        provider: Optional[str] = None,
        expected_sha: Optional[str] = None,
        actual_sha: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        merged_details = dict(details) if details else {}
        if expected_sha:
            merged_details["expected_sha"] = expected_sha
        if actual_sha:
            merged_details["actual_sha"] = actual_sha

        super().__init__(
            message=message,
            provider=provider,
            code=SourceControlErrorCode.REMOTE_STATE_CHANGED,
            details=merged_details,
        )


class ValidationError(SourceControlError):
    """Raised when local input validation fails before dispatching to provider or Git."""

    def __init__(
        self,
        message: str,
        *,
        provider: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.VALIDATION_ERROR,
            provider=provider,
            retryable=False,
            details=details,
        )


class UnsupportedCapabilityError(SourceControlError):
    """Raised when attempting an operation not supported by the active provider adapter."""

    def __init__(
        self,
        capability: str,
        *,
        provider: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        merged_details = dict(details) if details else {}
        merged_details["unsupported_capability"] = capability

        super().__init__(
            message=f"Capability '{capability}' is not supported by provider '{provider or 'unknown'}'.",
            code=SourceControlErrorCode.UNSUPPORTED_CAPABILITY,
            provider=provider,
            retryable=False,
            details=merged_details,
        )
        self.capability = capability


class ProviderError(SourceControlError):
    """Raised when the upstream provider returns an unhandled 5xx or general error."""

    def __init__(
        self,
        message: str,
        *,
        provider: Optional[str] = None,
        http_status: Optional[int] = None,
        diagnostic_code: Optional[str] = None,
        request_id: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        merged_details = dict(details) if details else {}
        if http_status is not None:
            merged_details["http_status"] = http_status

        retryable = http_status in {500, 502, 503, 504} if http_status else False
        super().__init__(
            message=message,
            code=SourceControlErrorCode.PROVIDER_ERROR,
            provider=provider,
            retryable=retryable,
            details=merged_details,
            diagnostic_code=diagnostic_code,
            request_id=request_id,
        )
        self.http_status = http_status


class WorkspaceDirtyError(SourceControlError):
    """Raised when a workspace has uncommitted user modifications that would be lost."""

    def __init__(
        self,
        message: str = "Workspace contains modified or untracked files. Operation halted to prevent data loss.",
        *,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.WORKSPACE_DIRTY,
            retryable=False,
            details=details,
        )


class ApprovalRequiredError(PermissionDeniedError):
    """Raised when a mutating operation is attempted without an approved ticket."""

    def __init__(
        self,
        message: str = "Operation requires explicit human approval before execution.",
        *,
        operation_id: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        merged_details = dict(details) if details else {}
        if operation_id:
            merged_details["operation_id"] = operation_id

        super().__init__(
            message=message,
            details=merged_details,
        )
        self.code = SourceControlErrorCode.APPROVAL_REQUIRED
        self.operation_id = operation_id


class ApprovalInvalidError(PermissionDeniedError):
    """Raised when an approval ticket is expired, invalidated, or does not match current diff."""

    def __init__(
        self,
        message: str = "Approval ticket is expired, invalidated, or does not match current diff.",
        *,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            details=details,
        )
        self.code = SourceControlErrorCode.APPROVAL_INVALID


class PathTraversalError(SourceControlError):
    """Raised when a path escapes the sandbox or root workspace boundaries."""

    def __init__(
        self,
        attempted_path: str,
        *,
        message: str = "Security violation: path traversal detected outside workspace root.",
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        merged_details = dict(details) if details else {}
        merged_details["attempted_path"] = attempted_path

        super().__init__(
            message=message,
            code=SourceControlErrorCode.PATH_TRAVERSAL_DETECTED,
            retryable=False,
            details=merged_details,
        )
        self.attempted_path = attempted_path


class CommandTimeoutError(SourceControlError):
    """Raised when a local Git subprocess exceeds its configured execution timeout."""

    def __init__(
        self,
        command: str,
        timeout_seconds: float,
        *,
        message: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        merged_details = dict(details) if details else {}
        merged_details["command"] = command
        merged_details["timeout_seconds"] = timeout_seconds

        msg = message or f"Command '{command}' exceeded execution timeout of {timeout_seconds}s."
        super().__init__(
            message=msg,
            code=SourceControlErrorCode.COMMAND_TIMEOUT,
            retryable=True,
            details=merged_details,
        )
        self.command = command
        self.timeout_seconds = timeout_seconds


# Canonical aliases ensuring backwards-compatible imports across subsystem packages
AuthRequiredError = AuthenticationRequiredError
AuthExpiredError = AuthenticationExpiredError
RateLimitedError = RateLimitExceededError

__all__ = [
    "SourceControlError",
    "AuthenticationRequiredError",
    "AuthenticationExpiredError",
    "AuthRequiredError",
    "AuthExpiredError",
    "PermissionDeniedError",
    "NotFoundError",
    "RateLimitExceededError",
    "RateLimitedError",
    "NetworkError",
    "ConflictError",
    "RemoteStateChangedError",
    "ValidationError",
    "UnsupportedCapabilityError",
    "ProviderError",
    "WorkspaceDirtyError",
    "ApprovalRequiredError",
    "ApprovalInvalidError",
    "PathTraversalError",
    "CommandTimeoutError",
]
