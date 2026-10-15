from __future__ import annotations

from typing import Optional, Sequence

from ..domain.enums import ProviderId
from ..ports.provider import SourceControlProvider
from ..registry import ProviderRegistry, get_default_registry

# Manifest list of officially supported provider IDs in this release
BUILTIN_PROVIDERS: tuple[ProviderId, ...] = (ProviderId.GITHUB,)


def get_supported_provider_ids() -> tuple[str, ...]:
    """Returns a tuple of string identifiers for all natively supported source control providers."""
    return tuple(p.value for p in BUILTIN_PROVIDERS)


def is_provider_supported(provider: str | ProviderId) -> bool:
    """Verifies whether a given provider identifier is currently supported by the subsystem."""
    target_value = provider.value if isinstance(provider, ProviderId) else str(provider).strip().lower()
    return any(p.value == target_value for p in BUILTIN_PROVIDERS)


def register_builtin_providers(registry: Optional[ProviderRegistry] = None) -> None:
    """Registers all available built-in provider adapters into the target ProviderRegistry.

    Uses safe deferred imports to support incremental compilation and isolated testing.
    """
    target_registry = registry or get_default_registry()

    # 1. Register GitHub official adapter
    try:
        from .github import GitHubProviderAdapter  # type: ignore[import-not-found]

        if not target_registry.has_provider(ProviderId.GITHUB):
            target_registry.register(GitHubProviderAdapter())
    except (ImportError, AttributeError):
        # Gracefully handle intermediate loading phases during file-by-file assembly
        pass


__all__ = [
    "BUILTIN_PROVIDERS",
    "get_supported_provider_ids",
    "is_provider_supported",
    "register_builtin_providers",
]
