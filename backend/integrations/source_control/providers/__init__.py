# backend/integrations/source_control/providers/__init__.py
"""
Built-in provider registration for the Source Control Integration
subsystem.

This module is the single place where the built-in provider
adapters (GitHub, and later GitLab and Bitbucket) are registered
with the `ProviderRegistry`. Application startup calls
`register_builtin_providers()` once, after the registry has been
constructed and before any operation is accepted.

Design rules:
    - The module MUST NOT import any provider adapter at import
      time. Adapters are imported lazily inside the registration
      function, which prevents circular imports (adapters depend on
      `ports`, `domain`, and `errors`, but not on this module).
    - Registration MUST be idempotent by default. Calling
      `register_builtin_providers()` twice MUST NOT raise and MUST
      NOT duplicate adapters. The optional `force=True` flag
      replaces existing adapters, which is useful for tests.
    - A failure to import or construct a single adapter MUST NOT
      prevent the application from starting. The failure is logged
      and the remaining adapters are registered.
    - This module never instantiates adapters eagerly. Each adapter
      is built by a factory that is called only during registration.

The set of built-in providers is exposed as `BUILTIN_PROVIDER_IDS`
for callers that need to know which providers ship with the
application.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..domain.enums import ProviderId
from ..errors import ValidationError
from ..registry import (
    ProviderRegistry,
    get_default_registry,
)

__all__ = [
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
BUILTIN_PROVIDER_IDS: Tuple[ProviderId, ...] = (ProviderId.GITHUB,)


# ---------------------------------------------------------------------------
# Provider factories
# ---------------------------------------------------------------------------


def _build_github_adapter() -> Any:
    """
    Construct the GitHub provider adapter.

    Imported lazily so that a broken GitHub adapter does not prevent
    the module from being imported. The caller (register_builtin)
    handles any exception raised here.
    """
    # Import inside the function to avoid importing the adapter at
    # module import time.
    from .github import GitHubProviderAdapter  # type: ignore[import-not-found]

    return GitHubProviderAdapter()


#: Ordered mapping from provider ID to a zero-argument factory that
#: builds a fresh adapter. The order defines the registration order.
_BUILTIN_FACTORIES: Dict[ProviderId, Callable[[], Any]] = {
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

    This is a thin wrapper around `ProviderRegistry.register` that
    adds idempotency: when the adapter's `provider_id` is already
    registered and `force` is False, the call is a no-op.

    Returns:
        True when the adapter was registered or replaced, False when
        registration was skipped because the ID already existed.

    Raises:
        TypeError: When `adapter` does not satisfy the
            `SourceControlProvider` protocol.
        ValidationError: When the adapter's ID is invalid.
    """
    target = registry or get_default_registry()
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
    Unregister a single provider.

    Returns True when a provider was removed, False when nothing was
    registered under that ID.
    """
    target = registry or get_default_registry()
    return target.unregister(provider_id)


# ---------------------------------------------------------------------------
# Built-in registration
# ---------------------------------------------------------------------------


def register_builtin_providers(
    *,
    registry: Optional[ProviderRegistry] = None,
    force: bool = False,
    providers: Optional[Sequence[ProviderId]] = None,
) -> Dict[str, str]:
    """
    Register every built-in provider adapter.

    This function is idempotent by default: calling it twice has the
    same effect as calling it once. A failure to import or construct
    a single adapter is logged and does not prevent the remaining
    adapters from being registered.

    Args:
        registry: The target registry. When `None`, the process-wide
            default registry is used.
        force: When True, existing registrations are replaced. Use
            with caution; this is intended for tests.
        providers: Optional subset of provider IDs to register. When
            `None`, every provider in `BUILTIN_PROVIDER_IDS` is
            registered.

    Returns:
        A mapping from provider ID to a status string. Possible
        statuses are:

            - `"registered"`  : the adapter was registered.
            - `"replaced"`    : the adapter replaced a prior one.
            - `"skipped"`     : the adapter was already registered.
            - `"failed"`      : the adapter could not be constructed.
    """
    target = registry or get_default_registry()
    selected = (
        list(providers) if providers else list(_BUILTIN_FACTORIES.keys())
    )

    status: Dict[str, str] = {}

    for provider_id in selected:
        key = provider_id.value if isinstance(provider_id, ProviderId) else str(provider_id)
        factory = _BUILTIN_FACTORIES.get(
            provider_id if isinstance(provider_id, ProviderId) else ProviderId(str(provider_id))
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
            # A single adapter failure MUST NOT prevent the rest.
            logger.warning(
                "Failed to build adapter for provider '%s': %s",
                key,
                type(exc).__name__,
            )
            status[key] = "failed"
            continue

        already_registered = target.has(key)

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

        if already_registered:
            if force:
                logger.info(
                    "Replaced source-control provider '%s'.", key
                )
                status[key] = "replaced"
            else:
                logger.debug(
                    "Provider '%s' was already registered.", key
                )
                status[key] = "skipped"
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
    target = registry or get_default_registry()
    results: Dict[str, bool] = {}
    for provider_id in _BUILTIN_FACTORIES.keys():
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
    return tuple(_BUILTIN_FACTORIES.keys())


def list_registered_providers(
    *,
    registry: Optional[ProviderRegistry] = None,
) -> Sequence[str]:
    """
    Return the IDs of every provider currently registered.
    """
    target = registry or get_default_registry()
    return target.list_provider_ids()
