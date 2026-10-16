# backend/integrations/source_control/registry.py
"""
Provider registry for the Source Control Integration subsystem.

The registry is the single place where provider adapters are
registered and looked up by their stable `ProviderId`. It is
constructed by the application startup code and populated with the
built-in providers (GitHub first, GitLab and Bitbucket later).

Design rules:
    - The registry is thread-safe. Registration typically happens at
      startup, but tests and dynamic feature flags may register or
      unregister providers at runtime.
    - Registration validates the adapter against the
      `SourceControlProvider` protocol. An adapter that does not
      satisfy the protocol is rejected with a `ValidationError`.
    - Lookups are explicit: `get()` raises `NotFoundError` for an
      unknown provider. `try_get()` returns `None` instead and is used
      by callers that want to branch on availability.
    - The default registry is a process-wide singleton, but tests can
      reset it via `reset_default_registry()`.
    - This module MUST NOT import any provider adapter. Adapters are
      registered from `providers/__init__.py` to avoid circular imports.
"""

from __future__ import annotations

import threading
from typing import Dict, Iterator, Mapping, Optional, Sequence

from .capabilities import CapabilityChecker
from .domain.enums import ProviderId
from .domain.models import ProviderCapabilities
from .errors import NotFoundError, ValidationError
from .ports.provider import SourceControlProvider

__all__ = [
    "ProviderRegistry",
    "get_default_registry",
    "set_default_registry",
    "reset_default_registry",
]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ProviderRegistry:
    """
    Thread-safe registry of source-control provider adapters.

    The registry owns the mapping from `provider_id` to adapter
    instance. It never instantiates adapters itself; the caller is
    responsible for constructing and configuring them.

    Example:
        registry = ProviderRegistry()
        registry.register(GitHubProviderAdapter(client=..., vault=...))
        adapter = registry.get(ProviderId.GITHUB)
    """

    def __init__(self) -> None:
        self._providers: Dict[str, SourceControlProvider] = {}
        self._lock = threading.RLock()

    # --- Registration ---------------------------------------------------

    def register(
        self,
        provider: SourceControlProvider,
        *,
        overwrite: bool = False,
    ) -> None:
        """
        Register a provider adapter.

        Args:
            provider: An object satisfying the `SourceControlProvider`
                protocol.
            overwrite: When False (default), attempting to register a
                provider whose ID already exists raises
                `ValidationError`. When True, the previous adapter is
                replaced.

        Raises:
            TypeError: If `provider` does not satisfy the protocol.
            ValidationError: If `provider_id` is empty, or if the ID
                is already registered and `overwrite` is False.
        """
        if not isinstance(provider, SourceControlProvider):
            raise TypeError(
                "Provider does not satisfy the SourceControlProvider "
                "protocol."
            )

        provider_id = getattr(provider, "provider_id", None)
        if not provider_id or not isinstance(provider_id, str):
            raise ValidationError(
                "Provider adapter must expose a non-empty string "
                "`provider_id`.",
                details={"provider_type": type(provider).__name__},
            )

        normalized_id = provider_id.strip().lower()

        with self._lock:
            if normalized_id in self._providers and not overwrite:
                raise ValidationError(
                    f"Provider '{normalized_id}' is already registered.",
                    details={"provider_id": normalized_id},
                )
            self._providers[normalized_id] = provider

    def unregister(self, provider_id: ProviderId | str) -> bool:
        """
        Remove a provider from the registry.

        Returns True if the provider was present and removed, False if
        it was not registered. Never raises for a missing provider,
        since callers frequently use `unregister` in cleanup paths.
        """
        normalized = _normalize_id(provider_id)
        with self._lock:
            return self._providers.pop(normalized, None) is not None

    def clear(self) -> None:
        """
        Remove every registered provider.

        Intended for tests and for controlled shutdown sequences.
        Normal application code should not call this method.
        """
        with self._lock:
            self._providers.clear()

    # --- Lookup ---------------------------------------------------------

    def get(self, provider_id: ProviderId | str) -> SourceControlProvider:
        """
        Return the adapter registered for `provider_id`.

        Raises:
            NotFoundError: If no adapter is registered under that ID.
        """
        normalized = _normalize_id(provider_id)
        with self._lock:
            provider = self._providers.get(normalized)
        if provider is None:
            raise NotFoundError(
                f"Source-control provider '{normalized}' is not "
                "registered.",
                details={"provider_id": normalized},
            )
        return provider

    def try_get(
        self, provider_id: ProviderId | str
    ) -> Optional[SourceControlProvider]:
        """
        Return the adapter registered for `provider_id`, or `None`.
        """
        normalized = _normalize_id(provider_id)
        with self._lock:
            return self._providers.get(normalized)

    def has(self, provider_id: ProviderId | str) -> bool:
        """
        Return True if an adapter is registered under `provider_id`.
        """
        normalized = _normalize_id(provider_id)
        with self._lock:
            return normalized in self._providers

    def has_provider(self, provider_id: ProviderId | str) -> bool:
        """
        Backward-compatible alias for `has`.

        Historically some call sites used `has_provider` while the
        registry only exposed `has`. Both names are now supported and
        refer to the same lookup.
        """
        return self.has(provider_id)

    # --- Enumeration ----------------------------------------------------

    def list_providers(self) -> Sequence[SourceControlProvider]:
        """
        Return a snapshot of all registered adapters.

        The order is not guaranteed. Callers that need a stable order
        SHOULD sort by `provider_id`.
        """
        with self._lock:
            return tuple(self._providers.values())

    def list_provider_ids(self) -> Sequence[str]:
        """Return a snapshot of all registered provider IDs."""
        with self._lock:
            return tuple(sorted(self._providers.keys()))

    def __iter__(self) -> Iterator[SourceControlProvider]:
        """Iterate over a snapshot of the registered adapters."""
        return iter(self.list_providers())

    def __len__(self) -> int:
        """Return the number of registered providers."""
        with self._lock:
            return len(self._providers)

    def __contains__(self, provider_id: object) -> bool:
        """Support `provider_id in registry` syntax."""
        if not isinstance(provider_id, (str, ProviderId)):
            return False
        return self.has(provider_id)

    # --- Capabilities ---------------------------------------------------

    def capabilities_of(
        self, provider_id: ProviderId | str
    ) -> ProviderCapabilities:
        """
        Return the declared capabilities of the given provider.

        Raises `NotFoundError` if the provider is not registered.
        """
        return self.get(provider_id).capabilities

    def checker_for(
        self, provider_id: ProviderId | str
    ) -> CapabilityChecker:
        """
        Return a `CapabilityChecker` bound to the given provider.

        Raises `NotFoundError` if the provider is not registered.
        """
        return CapabilityChecker(self.capabilities_of(provider_id))

    # --- Serialization --------------------------------------------------

    def to_dict(self) -> Mapping[str, object]:
        """
        Return a JSON-safe snapshot of the registry.

        Contains provider IDs and their declared capabilities. Never
        contains credentials, tokens, or adapter internals.
        """
        with self._lock:
            snapshot = {
                provider_id: provider.capabilities
                for provider_id, provider in self._providers.items()
            }
        return {
            "provider_count": len(snapshot),
            "providers": {
                provider_id: CapabilityChecker(caps).to_dict()
                for provider_id, caps in snapshot.items()
            },
        }


# ---------------------------------------------------------------------------
# Default registry singleton
# ---------------------------------------------------------------------------


_DEFAULT_REGISTRY: Optional[ProviderRegistry] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_registry() -> ProviderRegistry:
    """
    Return the process-wide default registry, creating it on first use.
    """
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = ProviderRegistry()
        return _DEFAULT_REGISTRY


def set_default_registry(registry: ProviderRegistry) -> None:
    """
    Replace the process-wide default registry.

    Intended for tests and for embedding scenarios where the
    application wants to supply a pre-populated registry.
    """
    if not isinstance(registry, ProviderRegistry):
        raise TypeError(
            "set_default_registry expects a ProviderRegistry instance."
        )
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = registry


def reset_default_registry() -> ProviderRegistry:
    """
    Discard the current default registry and create a fresh empty one.

    Used by the test suite to guarantee isolation between tests.
    Returns the new registry for convenience.
    """
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = ProviderRegistry()
        return _DEFAULT_REGISTRY


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_id(provider_id: ProviderId | str) -> str:
    """
    Normalize a provider ID to a lowercase string.

    Accepts either a `ProviderId` enum member or a plain string. Raises
    `ValidationError` for empty inputs and `TypeError` for values that
    are neither `str` nor `ProviderId`.
    """
    if isinstance(provider_id, ProviderId):
        return provider_id.value
    if isinstance(provider_id, str):
        normalized = provider_id.strip().lower()
        if not normalized:
            raise ValidationError("Provider ID must not be empty.")
        return normalized
    raise TypeError(
        "provider_id must be a ProviderId enum member or a string."
    )
