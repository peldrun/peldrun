from __future__ import annotations

import threading
from typing import Optional, Sequence

from .capabilities import ProviderCapabilities
from .domain.enums import ProviderId
from .ports.provider import SourceControlProvider


class ProviderRegistry:
    """Thread-safe central registry for source control provider adapters."""

    def __init__(self) -> None:
        self._providers: dict[ProviderId, SourceControlProvider] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_id(provider_id: ProviderId | str) -> ProviderId:
        """Normalizes string or enum provider identifiers into validated ProviderId."""
        if isinstance(provider_id, ProviderId):
            return provider_id
        try:
            return ProviderId(str(provider_id).lower().strip())
        except ValueError as exc:
            raise ValueError(
                f"Unknown or invalid source control provider identifier: '{provider_id}'"
            ) from exc

    def register(
        self, provider: SourceControlProvider, overwrite: bool = False
    ) -> None:
        """Registers a source control provider adapter.

        Raises ValueError if provider is already registered and overwrite is False.
        """
        if not isinstance(provider, SourceControlProvider):
            raise TypeError(
                f"Expected instance conforming to SourceControlProvider, got '{type(provider).__name__}'"
            )

        norm_id = self._normalize_id(provider.provider_id)
        with self._lock:
            if norm_id in self._providers and not overwrite:
                raise ValueError(
                    f"Provider '{norm_id.value}' is already registered. "
                    "Use overwrite=True to explicitly replace."
                )
            self._providers[norm_id] = provider

    def unregister(self, provider_id: ProviderId | str) -> bool:
        """Removes a registered provider adapter. Returns True if removed."""
        norm_id = self._normalize_id(provider_id)
        with self._lock:
            return self._providers.pop(norm_id, None) is not None

    def get(self, provider_id: ProviderId | str) -> SourceControlProvider:
        """Retrieves a registered provider adapter.

        Raises ValueError if the provider is not found.
        """
        norm_id = self._normalize_id(provider_id)
        with self._lock:
            provider = self._providers.get(norm_id)
        if provider is None:
            raise ValueError(
                f"Source control provider '{norm_id.value}' is not registered."
            )
        return provider

    def get_optional(
        self, provider_id: ProviderId | str
    ) -> Optional[SourceControlProvider]:
        """Retrieves a provider adapter if registered, otherwise returns None."""
        try:
            norm_id = self._normalize_id(provider_id)
        except ValueError:
            return None
        with self._lock:
            return self._providers.get(norm_id)

    def has(self, provider_id: ProviderId | str) -> bool:
        """Checks if a provider adapter is currently registered."""
        try:
            norm_id = self._normalize_id(provider_id)
        except ValueError:
            return False
        with self._lock:
            return norm_id in self._providers

    def list_providers(self) -> Sequence[SourceControlProvider]:
        """Returns an immutable snapshot of all registered provider adapters."""
        with self._lock:
            return tuple(self._providers.values())

    def list_provider_ids(self) -> Sequence[ProviderId]:
        """Returns an immutable snapshot of all registered ProviderIds."""
        with self._lock:
            return tuple(self._providers.keys())

    def get_capabilities(
        self, provider_id: ProviderId | str
    ) -> ProviderCapabilities:
        """Retrieves the declared capabilities of a registered provider."""
        provider = self.get(provider_id)
        return provider.capabilities

    def clear(self) -> None:
        """Clears all registered providers (primarily for isolated test teardown)."""
        with self._lock:
            self._providers.clear()


# ============================================================
# Default Registry Singleton Accessors
# ============================================================

_DEFAULT_REGISTRY: Optional[ProviderRegistry] = None
_INIT_LOCK = threading.Lock()


def get_default_registry() -> ProviderRegistry:
    """Returns the process-wide default ProviderRegistry singleton."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        with _INIT_LOCK:
            if _DEFAULT_REGISTRY is None:
                _DEFAULT_REGISTRY = ProviderRegistry()
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Resets the process-wide default registry (used during testing)."""
    global _DEFAULT_REGISTRY
    with _INIT_LOCK:
        if _DEFAULT_REGISTRY is not None:
            _DEFAULT_REGISTRY.clear()
        _DEFAULT_REGISTRY = None
