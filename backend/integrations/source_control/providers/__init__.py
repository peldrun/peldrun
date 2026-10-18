# backend/integrations/source_control/providers/__init__.py
"""
Built-in provider registration for the Source Control Integration
subsystem.

This module is the single entry point for the source-control provider
suite. It exposes:

    - `GitHubProviderAdapter`, the concrete GitHub adapter.
    - `register_builtin_providers()`, the application-startup hook
      that populates a `ProviderRegistry` with every built-in adapter.
    - `unregister_builtin_providers()`, a test-oriented helper that
      removes every built-in adapter from a registry.
    - `BUILTIN_PROVIDER_IDS`, the canonical tuple of provider IDs
      that ship with PELDRUN.

Design rules:
    - This module MUST NOT import any provider adapter at module
      import time. Adapters are imported lazily inside registration
      helpers, which prevents circular imports and lets the module
      load even when a particular adapter is missing.
    - Registration MUST be idempotent by default. Calling
      `register_builtin_providers()` twice MUST NOT raise and MUST
      NOT duplicate providers.
    - A failure to import or construct a single adapter MUST NOT
      prevent the application from starting. The failure is logged
      and the remaining adapters are registered.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..domain.enums import ProviderId
from ..errors import ValidationError
from ..registry import ProviderRegistry, get_default_registry

__all__ = [
    "GitHubProviderAdapter",
    "register_builtin_providers",
    "unregister_builtin_providers",
    "register_provider",
    "unregister_provider",
    "list_builtin_providers",
    "list_registered_providers",
    "BUILTIN_PROVIDER_IDS",
]


logger = logging.getLogger(__name__)


#: Provider IDs that ship with PELDRUN. GitLab and Bitbucket are
#: intentionally absent until their adapters are implemented.
BUILTIN_PROVIDER_IDS: tuple[ProviderId, ...] = (ProviderId.GITHUB,)


# ---------------------------------------------------------------------------
# GitHub adapter re-export
# ---------------------------------------------------------------------------
#
# `GitHubProviderAdapter` is defined in `providers/github/__init__.py`.
# We expose it at the package level for callers that expect to import
# it directly from `providers`. The import is performed lazily via the
# module `__getattr__` so that a missing `github` sub-package does not
# break the package import.

_LAZY_EXPORTS: Dict[str, tuple[str, str]] = {
    "GitHubProviderAdapter": (".github", "GitHubProviderAdapter"),
}


def __getattr__(name: str) -> Any:
    """
    Lazily resolve names that are re-exported from sub-packages.

    Python calls this function when an attribute not found in the
    module's namespace is accessed. We use it to import the concrete
    provider adapter classes on demand, so that a missing adapter
    module does not prevent the rest of the package from importing.
    """
    spec = _LAZY_EXPORTS.get(name)
    if spec is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    module_name, class_name = spec
    try:
        import importlib

        module = importlib.import_module(module_name, package=__name__)
        return getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}: "
            f"{type(exc).__name__}"
        ) from exc


def __dir__() -> Sequence[str]:
    """Include the lazily-exported names in `dir(providers)`."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS) | set(__all__))


# ---------------------------------------------------------------------------
# Provider factories
# ---------------------------------------------------------------------------


def _build_github_adapter() -> Any:
    """
    Construct a fresh `GitHubProviderAdapter`.

    The import is performed inside the function body so that this
    module does not require the GitHub sub-package at import time.
    """
    from .github import GitHubProviderAdapter

    return GitHubProviderAdapter()


#: Ordered mapping from provider ID to a zero-argument factory. The
#: insertion order defines the registration order.
_PROVIDER_FACTORIES: Dict[ProviderId, Callable[[], Any]] = {
    ProviderId.GITHUB: _build_github_adapter,
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_provider(
    adapter: Any,
    *,
    registry: Optional[ProviderRegistry] = None,
    force: bool = False,
) -> bool:
    """
    Register a single provider adapter.

    Thin wrapper around `ProviderRegistry.register` that adds
    idempotency: when the adapter's `provider_id` is already
    registered and `force` is False, the call is a no-op.

    Returns True when the adapter was registered or replaced, False
    when registration was skipped because the ID already existed.
    """
    target = registry if registry is not None else get_default_registry()
    provider_id = getattr(adapter, "provider_id", None)
    if not provider_id or not isinstance(provider_id, str):
        raise ValidationError(
            "Provider adapter must expose a non-empty string "
            "`provider_id`.",
            details={"type": type(adapter).__name__},
        )

    if target.has(provider_id) and not force:
        logger.debug(
            "Provider '%s' is already registered; skipping.",
            provider_id,
        )
        return False

    target.register(adapter, overwrite=force)
    logger.info("Registered source-control provider '%s'.", provider_id)
    return True


def unregister_provider(
    provider_id: ProviderId | str,
    *,
    registry: Optional[ProviderRegistry] = None,
) -> bool:
    """
    Unregister a single provider. Returns True when a provider was
    removed, False when nothing was registered under that ID.
    """
    target = registry if registry is not None else get_default_registry()
    return target.unregister(provider_id)


def register_builtin_providers(
    *,
    registry: Optional[ProviderRegistry] = None,
    force: bool = False,
    providers: Optional[Sequence[ProviderId]] = None,
) -> Dict[str, str]:
    """
    Register every built-in provider adapter.

    Idempotent by default: calling this function twice has the same
    effect as calling it once. A failure to import or construct a
    single adapter is logged and does not prevent the remaining
    adapters from being registered.

    Returns a mapping from provider ID to a status string:
        - `"registered"` : the adapter was registered.
        - `"replaced"`   : the adapter replaced a prior one.
        - `"skipped"`    : the adapter was already registered.
        - `"failed"`     : the adapter could not be constructed.
    """
    target = registry if registry is not None else get_default_registry()
    selected = (
        list(providers) if providers else list(_PROVIDER_FACTORIES.keys())
    )

    status: Dict[str, str] = {}

    for provider_id in selected:
        key = (
            provider_id.value
            if isinstance(provider_id, ProviderId)
            else str(provider_id)
        )
        factory = _PROVIDER_FACTORIES.get(
            provider_id
            if isinstance(provider_id, ProviderId)
            else ProviderId(str(provider_id))
        )
        if factory is None:
            logger.warning(
                "No built-in adapter is registered for provider '%s'.",
                key,
            )
            status[key] = "failed"
            continue

        try:
            adapter = factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to build adapter for provider '%s': %s",
                key,
                type(exc).__name__,
            )
            status[key] = "failed"
            continue

        already_registered = target.has(key)

        # A duplicate registration without `force=True` is a normal
        # no-op, not an error. The previous implementation reported
        # it as `"failed"` because `ProviderRegistry.register()`
        # raises `ValidationError` for an existing key *before* the
        # `if already_registered` branch could classify it as
        # `"skipped"`. Detecting the duplicate here keeps the status
        # map truthful and stops the misleading WARNING from being
        # logged on every second-and-later startup.
        if already_registered and not force:
            logger.debug(
                "Provider '%s' was already registered; skipping.",
                key,
            )
            status[key] = "skipped"
            continue

        try:
            target.register(adapter, overwrite=force)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to register provider '%s': %s",
                key,
                type(exc).__name__,
            )
            status[key] = "failed"
            continue

        # Reached only when the provider was either new or `force=True`.
        if already_registered:
            logger.info(
                "Replaced source-control provider '%s'.", key
            )
            status[key] = "replaced"
        else:
            logger.info("Registered source-control provider '%s'.", key)
            status[key] = "registered"

    return status


def unregister_builtin_providers(
    *,
    registry: Optional[ProviderRegistry] = None,
) -> Dict[str, bool]:
    """
    Unregister every built-in provider.

    Returns a mapping from provider ID to True when a provider was
    removed, False when nothing was registered under that ID.
    Intended for tests that want to start from a clean slate.
    """
    target = registry if registry is not None else get_default_registry()
    results: Dict[str, bool] = {}
    for provider_id in _PROVIDER_FACTORIES.keys():
        key = provider_id.value
        results[key] = target.unregister(provider_id)
    return results


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def list_builtin_providers() -> Sequence[ProviderId]:
    """
    Return the list of built-in provider IDs, in registration order.
    """
    return tuple(_PROVIDER_FACTORIES.keys())


def list_registered_providers(
    *,
    registry: Optional[ProviderRegistry] = None,
) -> Sequence[str]:
    """
    Return the IDs of every provider currently registered.
    """
    target = registry if registry is not None else get_default_registry()
    return target.list_provider_ids()
