# backend/integrations/source_control/capabilities.py
"""
Capability declarations and checks for the Source Control Integration
subsystem.

Every provider adapter declares a `ProviderCapabilities` instance that
describes which operations it supports. This module provides:

    - A canonical mapping from operation names to the capability
      attribute that gates them.
    - A `CapabilityChecker` wrapper that evaluates an operation against
      a `ProviderCapabilities` instance.
    - Helper functions used by the registry, the application service,
      the agent tools, and the HTTP layer.

Design rules:
    - Capability checks MUST happen in the service layer, never in the
      UI alone. Hiding a button is not a security control.
    - Adapters MUST NOT silently emulate an unsupported operation. If a
      provider cannot perform an action, the check MUST raise
      `UnsupportedCapabilityError`.
    - The mapping table is the single source of truth linking operation
      names to capabilities. Adding a new operation requires a new
      entry here.
"""

from __future__ import annotations

from typing import Optional

from .domain.models import ProviderCapabilities
from .errors import UnsupportedCapabilityError

__all__ = [
    "ProviderCapabilities",
    "CapabilityChecker",
    "CAPABILITY_OPERATIONS",
    "operation_to_capability",
    "require_capability",
    "list_supported_operations",
    "list_unsupported_operations",
]


# ---------------------------------------------------------------------------
# Operation -> capability mapping
# ---------------------------------------------------------------------------


# Maps a public operation name to the attribute name on
# `ProviderCapabilities` that gates it. Operations not listed here are
# considered "always available" for providers that implement the base
# protocol (e.g. `list_providers`).
CAPABILITY_OPERATIONS: dict[str, str] = {
    # Repository discovery
    "list_repositories": "list_repositories",
    "get_repository": "list_repositories",
    # File and branch reads
    "read_file": "read_files",
    "read_file_content": "read_files",
    "list_branches": "list_repositories",
    # Local-only operations are not capability-gated
    # (create_workspace, get_diff, run_checks, request_approval).
    # Branch writes
    "create_branch": "create_branch",
    "push_branch": "push_branch",
    # Change requests
    "create_change_request": "create_change_request",
    "create_pull_request": "create_change_request",
    "create_merge_request": "create_change_request",
    "get_change_request": "create_change_request",
    # Checks
    "list_checks": "list_checks",
    "get_check_summary": "list_checks",
    # Comments
    "comment_on_change_request": "comments",
    "list_comments": "comments",
    # Merge and delete
    "merge_change_request": "merge_change_request",
    "delete_branch": "delete_branch",
}


def operation_to_capability(operation: str) -> Optional[str]:
    """
    Return the capability attribute name that gates `operation`.

    Returns `None` for operations that are always available (e.g.
    `list_providers`) or unknown. Callers SHOULD treat `None` as
    "no capability gate" and proceed, allowing the adapter to raise a
    more specific error if it cannot perform the operation.
    """
    if not operation:
        return None
    return CAPABILITY_OPERATIONS.get(operation)


# ---------------------------------------------------------------------------
# Capability checker
# ---------------------------------------------------------------------------


class CapabilityChecker:
    """
    Stateless wrapper around `ProviderCapabilities`.

    The checker is cheap to instantiate and safe to share across
    coroutines. It never mutates the underlying capabilities instance.
    """

    def __init__(self, capabilities: ProviderCapabilities) -> None:
        if not isinstance(capabilities, ProviderCapabilities):
            raise TypeError(
                "CapabilityChecker expects a ProviderCapabilities instance."
            )
        self._capabilities = capabilities

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return the wrapped capabilities instance."""
        return self._capabilities

    def supports(self, operation: str) -> bool:
        """
        Return True if the underlying provider supports `operation`.

        Operations without a capability mapping are treated as
        supported. Use `require()` if you want a strict gate.
        """
        capability_name = operation_to_capability(operation)
        if capability_name is None:
            return True
        return bool(getattr(self._capabilities, capability_name, False))

    def require(
        self,
        operation: str,
        *,
        provider_id: Optional[str] = None,
    ) -> None:
        """
        Raise `UnsupportedCapabilityError` if `operation` is not
        supported.

        Args:
            operation: The public operation name to check.
            provider_id: Optional provider identifier included in the
                error details for diagnostics.
        """
        if self.supports(operation):
            return

        capability_name = operation_to_capability(operation)
        details: dict[str, object] = {
            "operation": operation,
            "capability": capability_name,
        }
        if provider_id:
            details["provider"] = provider_id

        message = (
            f"Operation '{operation}' is not supported"
            + (f" by provider '{provider_id}'" if provider_id else "")
            + (
                f" (requires capability '{capability_name}')."
                if capability_name
                else "."
            )
        )
        raise UnsupportedCapabilityError(message, details=details)

    def supported_operations(self) -> tuple[str, ...]:
        """
        Return the sorted tuple of supported operation names.

        The result is deterministic, which makes it safe to use in UI
        rendering and tests.
        """
        return tuple(
            sorted(
                operation
                for operation in CAPABILITY_OPERATIONS
                if self.supports(operation)
            )
        )

    def unsupported_operations(self) -> tuple[str, ...]:
        """
        Return the sorted tuple of unsupported operation names.

        The UI uses this to disable actions and display a clear reason
        for each disabled control.
        """
        return tuple(
            sorted(
                operation
                for operation in CAPABILITY_OPERATIONS
                if not self.supports(operation)
            )
        )

    def to_dict(self) -> dict[str, object]:
        """
        Serialize the capabilities and derived operation lists.

        Safe to embed in API responses; contains no secrets.
        """
        caps = self._capabilities
        return {
            "list_repositories": caps.list_repositories,
            "read_files": caps.read_files,
            "create_branch": caps.create_branch,
            "push_branch": caps.push_branch,
            "create_change_request": caps.create_change_request,
            "list_checks": caps.list_checks,
            "comments": caps.comments,
            "merge_change_request": caps.merge_change_request,
            "delete_branch": caps.delete_branch,
            "self_managed_instance": caps.self_managed_instance,
            "supported_operations": list(self.supported_operations()),
            "unsupported_operations": list(self.unsupported_operations()),
        }


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def require_capability(
    capabilities: ProviderCapabilities,
    operation: str,
    *,
    provider_id: Optional[str] = None,
) -> None:
    """
    Raise `UnsupportedCapabilityError` if `operation` is not supported
    by `capabilities`.

    This is a thin wrapper around `CapabilityChecker.require` for
    callers that do not need to keep a checker instance around.
    """
    checker = CapabilityChecker(capabilities)
    checker.require(operation, provider_id=provider_id)


def list_supported_operations(
    capabilities: ProviderCapabilities,
) -> tuple[str, ...]:
    """Return the sorted tuple of supported operation names."""
    return CapabilityChecker(capabilities).supported_operations()


def list_unsupported_operations(
    capabilities: ProviderCapabilities,
) -> tuple[str, ...]:
    """Return the sorted tuple of unsupported operation names."""
    return CapabilityChecker(capabilities).unsupported_operations()
