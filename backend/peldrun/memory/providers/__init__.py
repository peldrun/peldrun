# backend/peldrun/memory/providers/__init__.py
"""
PELDRUN Memory Platform - Provider Registry and Factory.

Public surface of the ``peldrun.memory.providers`` package. Exposes:

    * ``MemoryProviderFactory`` -- the factory that constructs the
      provider named by ``MemorySettings.provider_id``.
    * ``register_provider`` / ``unregister_provider`` -- the
      extension points for third-party providers.
    * ``list_providers`` / ``KNOWN_PROVIDERS`` -- introspection for
      diagnostics.
    * The built-in classes themselves (``BaseMemoryProvider``,
      ``SqliteFtsProvider``) so callers that want to instantiate
      a specific backend directly (tests, migration tooling) do not
      need to import from the submodules.

Architectural role
------------------

This package is the FACTORY LAYER between ``MemorySettings`` (which
names a provider as a string) and ``MemoryService`` (which holds the
constructed provider). Everything below it is either the abstract
base class (``base.py``) or a concrete implementation
(``sqlite_fts.py``).

The factory itself is:
  * STATELESS -- it holds the registry at class level; instances
    are not required, and constructing one is a no-op (``create``
    is a classmethod).
  * RESILIENT -- every failure path falls back to
    ``NullMemoryProvider`` with a loud ERROR log. The memory
    subsystem is an ENHANCEMENT, not core functionality: a
    misconfigured provider must never prevent the backend from
    starting or a request from being served. This mirrors the
    plan's established pattern for other optional subsystems
    (checkpoint persistence, provider packages, resource monitor).

Resilience contract
-------------------

When ``MemoryProviderFactory.create`` cannot produce a working
provider, it returns a ``NullMemoryProvider`` whose behavior is
well-defined and safe:

    * reads return empty / None / False (no crash),
    * writes raise ``MemoryUnavailableError`` (fail-fast).

Callers (``MemoryService`` in P1.7) can therefore skip the
distinction between "deliberately disabled" and "failed to
configure" -- both modes behave identically to the user, and the
difference is only in the log line. The operator fixes
misconfiguration by looking at the logs, not by inferring from
downstream symptoms.

No import of ``omweb``
----------------------

This module imports ``sqlite_fts`` eagerly (so the built-in
provider is registered by default), but ``sqlite_fts`` itself
imports ``omweb.memory.storage.repository`` only INSIDE
``open()``. Importing ``peldrun.memory.providers`` therefore
never touches ``omweb`` -- preserving the architectural rule that
the provider package is importable in isolation (unit tests,
headless CLI, mypy on the domain tree).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Final, List, Type

from peldrun.memory.domain import (
    MemoryProvider,
    MemoryValidationError,
    NullMemoryProvider,
)
from peldrun.memory.providers.base import BaseMemoryProvider
from peldrun.memory.providers.sqlite_fts import SqliteFtsProvider


logger = logging.getLogger("peldrun.memory.providers")


# ---------------------------------------------------------------------------
# Built-in provider registry
# ---------------------------------------------------------------------------
#
# A dict is used (rather than a list of classes) so that
# registration is O(1) at lookup time AND the canonical
# ``provider_id`` string is authoritative -- not the class name,
# which could change during refactors.
#
# Adding a built-in provider:
#   1. Create the class in its own module.
#   2. Add one entry here.
#   3. Nothing else changes.

_BUILTIN_PROVIDERS: Dict[str, Type[BaseMemoryProvider]] = {
    SqliteFtsProvider.provider_id: SqliteFtsProvider,
}


#: Frozen view of the built-in providers for external introspection.
#: Does NOT include providers registered at runtime via
#: ``register_provider`` -- use ``list_providers()`` for the live
#: set that includes runtime registrations.
KNOWN_PROVIDERS: Final[Dict[str, Type[BaseMemoryProvider]]] = dict(
    _BUILTIN_PROVIDERS
)


# ---------------------------------------------------------------------------
# Registry manipulation
# ---------------------------------------------------------------------------


def register_provider(
    provider_id: str,
    provider_class: Type[BaseMemoryProvider],
) -> None:
    """Register a provider implementation under ``provider_id``.

    Used by:
      * tests, to install a fake provider without touching the
        built-in registry,
      * third-party packages that ship their own provider and
        register it at import time,
      * P1.7's settings reload path, if a new backend was installed
        at runtime.

    Parameters
    ----------
    provider_id
        The canonical identifier to register under. Must match
        the ``MemorySettings.provider_id`` value that will be used
        to select this provider. Leading/trailing whitespace is
        stripped; case is preserved (``provider_id`` values are
        treated case-sensitively, unlike ``scope_id`` --
        provider_id is developer-supplied, not user-supplied).

    provider_class
        A ``BaseMemoryProvider`` subclass (the abstract base
        enforces the lifecycle and CRUD contract at class
        definition time).

    Raises
    ------
    TypeError
        When ``provider_id`` is not a non-empty string, or
        ``provider_class`` is not a ``BaseMemoryProvider``
        subclass.

    Note on overwriting
    -------------------
    Re-registering an existing ``provider_id`` OVERWRITES the
    previous class silently (a DEBUG log line is emitted). This
    is deliberate: tests need to install fake providers on top
    of the real ones and undo it in teardown; forcing them to
    unregister first would just add ceremony. Production code
    SHOULD use ``unregister_provider`` before re-registering a
    name.
    """
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise TypeError(
            f"provider_id must be a non-empty string, got "
            f"{provider_id!r}"
        )
    if not isinstance(provider_class, type) or not issubclass(
        provider_class, BaseMemoryProvider
    ):
        raise TypeError(
            f"provider_class must subclass BaseMemoryProvider, got "
            f"{provider_class!r}"
        )

    clean_id = provider_id.strip()
    previous = _BUILTIN_PROVIDERS.get(clean_id)
    if previous is not None and previous is not provider_class:
        logger.debug(
            "Overwriting memory provider registration %r "
            "(was %s, now %s).",
            clean_id,
            previous.__name__,
            provider_class.__name__,
        )

    _BUILTIN_PROVIDERS[clean_id] = provider_class
    logger.info(
        "Registered memory provider %r -> %s.",
        clean_id,
        provider_class.__name__,
    )


def unregister_provider(provider_id: str) -> bool:
    """Remove a provider registration.

    Returns True when a registration existed and was removed,
    False otherwise. Idempotent -- calling twice is safe.

    Primarily for tests (teardown after ``register_provider``) and
    for the "uninstall a provider package" flow in P1.7. Does NOT
    refuse to unregister built-ins: a test that replaces
    ``sqlite_fts`` with a fake must be able to unregister the
    fake without knowing it was a built-in override.

    Callers that want to restore a built-in after unregistering
    should use ``register_provider`` with the original class
    (``SqliteFtsProvider`` is importable from this module).
    """
    if not isinstance(provider_id, str):
        return False
    clean_id = provider_id.strip()
    if clean_id not in _BUILTIN_PROVIDERS:
        return False
    del _BUILTIN_PROVIDERS[clean_id]
    logger.info("Unregistered memory provider %r.", clean_id)
    return True


def list_providers() -> List[str]:
    """Return the sorted list of currently-registered provider ids.

    Includes built-ins plus any runtime registrations. Safe to
    call at any time; returns a NEW list each call so a caller
    that mutates the result does not affect the registry.
    """
    return sorted(_BUILTIN_PROVIDERS.keys())


def is_known_provider(provider_id: str) -> bool:
    """Return True when ``provider_id`` resolves to a registered class."""
    if not isinstance(provider_id, str):
        return False
    return provider_id.strip() in _BUILTIN_PROVIDERS


# ---------------------------------------------------------------------------
# MemoryProviderFactory
# ---------------------------------------------------------------------------


class MemoryProviderFactory:
    """Stateless factory for constructing memory providers.

    All methods are classmethods -- no instance state, no
    construction cost, and no risk of two factory instances
    disagreeing about the registry (they share the module-level
    dict by design).

    Public API
    ----------
    ``create(provider_id, **kwargs) -> MemoryProvider``
        The main entry point. Returns a ready-to-use provider
        instance (still needs ``await provider.open()``) OR a
        ``NullMemoryProvider`` fallback on any failure.

    ``known_provider_ids() -> List[str]``
        Convenience accessor matching ``list_providers`` --
        duplicated here so callers that only hold the factory
        class do not need to import the module-level helper.

    ``is_known(provider_id) -> bool``
        Matches ``is_known_provider`` -- same rationale.
    """

    @classmethod
    def create(
        cls,
        provider_id: str,
        **kwargs: Any,
    ) -> MemoryProvider:
        """Construct the provider named by ``provider_id``.

        Parameters
        ----------
        provider_id
            The value from ``MemorySettings.provider_id``. Leading
            and trailing whitespace is stripped. Case is preserved
            but case-insensitive matching is NOT performed -- the
            id is a developer contract, not user input.

        **kwargs
            Provider-specific constructor arguments. For
            ``sqlite_fts``: ``db_path=Path(...)``. For a future
            ``pgvector``: ``dsn=...``, ``table=...``. The factory
            does NOT validate which kwargs each provider needs --
            the provider's own ``__init__`` does that (via the
            abstract base contract, which requires the subclass
            to accept whatever it needs).

        Returns
        -------
        MemoryProvider
            A concrete ``BaseMemoryProvider`` instance when the
            requested provider is known and its ``__init__``
            succeeds. A ``NullMemoryProvider`` otherwise. In the
            null case, an ERROR log line explains WHY (unknown
            id, missing kwargs, provider-internal failure) so the
            operator can fix the configuration.

        Failure handling
        ----------------
        Every failure path returns ``NullMemoryProvider`` rather
        than raising. Rationale (from the module docstring):

          * Memory is non-critical. A request without memory
            works; a request that refuses to start because of
            memory misconfiguration does not.
          * The failure is LOUD in logs (ERROR level with
            ``exc_info=True``). An operator investigating "why
            is my memory never saving?" reads the log and finds
            the exact cause.
          * The alternative (raise and let the backend crash on
            startup) forces every deployment to have a working
            memory configuration. Not acceptable for an optional
            subsystem.

        The one case that DOES raise: when ``provider_id`` is a
        structurally invalid value (not a string). This is a
        caller bug in the type domain, not a configuration
        problem -- raising ``MemoryValidationError`` surfaces it
        as a programmer error rather than silently degrading.
        """
        # Structural argument validation -- programmer errors,
        # not operator errors. Raise rather than degrade.
        if not isinstance(provider_id, str):
            raise MemoryValidationError(
                f"provider_id must be a string, got "
                f"{type(provider_id).__name__}",
                field_name="provider_id",
                context={"provider_id": repr(provider_id)},
            )

        clean_id = provider_id.strip()

        # Empty provider_id = "memory deliberately disabled". This
        # matches the semantics of MemoryMode.DISABLED -- return
        # Null with a single INFO log line, not an ERROR (this is
        # a legitimate configuration, not a failure).
        if not clean_id:
            logger.info(
                "Empty provider_id requested; returning "
                "NullMemoryProvider (memory disabled by "
                "configuration)."
            )
            return NullMemoryProvider()

        provider_class = _BUILTIN_PROVIDERS.get(clean_id)

        if provider_class is None:
            # Unknown provider id. Likely a typo in settings, or a
            # provider package that was uninstalled without
            # updating memory_settings.json. WARN (not ERROR --
            # the deployment may have intentionally removed the
            # package).
            logger.warning(
                "Unknown memory provider_id %r; available: %s. "
                "Falling back to NullMemoryProvider (memory "
                "disabled until the configuration is fixed).",
                clean_id,
                list_providers(),
            )
            return NullMemoryProvider()

        try:
            instance = provider_class(**kwargs)
        except MemoryValidationError:
            # Pass through validation errors from the provider
            # itself -- those are actionable for the caller
            # (e.g. a future provider that pre-validates its
            # configuration in __init__).
            raise
        except Exception as exc:
            # Any other failure (wrong kwargs, missing config,
            # bug inside the provider's __init__). Log with full
            # traceback and degrade.
            logger.error(
                "Failed to instantiate memory provider %r (%s): "
                "%s. Falling back to NullMemoryProvider. The "
                "memory subsystem will be disabled until the "
                "configuration is fixed.",
                clean_id,
                provider_class.__name__,
                exc,
                exc_info=True,
            )
            return NullMemoryProvider()

        logger.info(
            "Created memory provider %r (%s).",
            clean_id,
            type(instance).__name__,
        )
        return instance

    @classmethod
    def known_provider_ids(cls) -> List[str]:
        """Return the sorted list of registered provider ids.

        Convenience alias for ``list_providers``. Provided so
        callers that only hold the factory class do not need to
        import the module-level helper.
        """
        return list_providers()

    @classmethod
    def is_known(cls, provider_id: str) -> bool:
        """Return True when ``provider_id`` is registered.

        Convenience alias for ``is_known_provider``.
        """
        return is_known_provider(provider_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    # Factory
    "MemoryProviderFactory",
    # Registry helpers
    "register_provider",
    "unregister_provider",
    "list_providers",
    "is_known_provider",
    "KNOWN_PROVIDERS",
    # Re-exports (so callers do not need to import from the
    # submodules directly)
    "BaseMemoryProvider",
    "SqliteFtsProvider",
]
