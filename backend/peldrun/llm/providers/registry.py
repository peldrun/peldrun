# backend/peldrun/llm/providers/registry.py
"""
PELDRUN Provider Adapter Registry.

Central registration point for every LLM provider adapter:

    * Built-in adapters (openai_compat, lmstudio, ollama, ...).
    * Native adapters added in M5 (openai_native, anthropic, google_genai,
      ollama_native, lmstudio_native).
    * External Provider Packages loaded from ``importlib.metadata``
      entry points (M9 / M10).

Design invariants
-----------------

1. **No provider imports.** This module MUST NOT import any adapter
   class. It only stores opaque class references. This avoids a
   circular dependency between ``registry.py`` and the adapters, and
   lets tests exercise the registry in isolation.

2. **API contract validation is strict.** Every registration carries
   an ``api_contract`` version. Adapters declaring an unsupported
   version are rejected at registration time with a clear error — the
   user never sees a mysterious runtime failure from an incompatible
   Provider Package.

3. **Built-in providers are protected.** An external package cannot
   silently overwrite a built-in provider. This is a security guard:
   a malicious bundle cannot hijack ``openai`` or ``lmstudio`` by
   declaring the same ``provider_id``.

4. **Aliases are first-class.** The pre-existing ``get_provider()``
   accepted ``lm_studio``, ``lm-studio``, ``openai``, and ``default``
   in addition to the canonical identifiers. This registry preserves
   that behavior via explicit alias registration, so the migration
   from the old factory is a pure refactor with no behavior change.

5. **Discovery is explicit, not automatic.** Entry points are NOT
   scanned on import. The caller (``__init__.py`` bootstrap, or
   ``factory.py`` on first use) invokes ``discover_external_providers``
   exactly when it wants to pick up new packages. This keeps app
   startup fast and prevents surprise re-registration during a live
   session.

6. **Registration is idempotent for built-ins.** The bootstrap path
   in ``__init__.py`` passes ``overwrite=True`` for built-in
   registrations, so a hot reload or repeated import does not raise.
   External packages are registered with ``overwrite=False`` by
   default — see invariant 3.

See also
--------

    * ``backend/peldrun/llm/contracts.py``  — ProviderAdapter Protocol
    * ``backend/peldrun/llm/errors.py``     — registry error types
    * ``backend/peldrun/llm/providers/__init__.py`` — bootstrap wiring
    * ``backend/peldrun/llm/providers/factory.py``  — adapter instantiation
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Dict, List, Optional, Type

from peldrun.llm.errors import (
    ProviderAlreadyRegisteredError,
    ProviderContractIncompatibleError,
    ProviderDisabledError,
    ProviderNotRegisteredError,
)

logger = logging.getLogger("peldrun.llm.providers.registry")


# ---------------------------------------------------------------------------
# Contract version constants
# ---------------------------------------------------------------------------


#: The API contract version every adapter in this build is expected to
#: declare (via the ``API_CONTRACT`` class attribute, or via an explicit
#: ``api_contract`` argument to ``register()``).
CURRENT_API_CONTRACT: str = "peldrun.llm.provider.v1"


#: The set of contract versions this build can load. Adding a new
#: major contract version here is the ONLY place a breaking change to
#: the adapter protocol must be acknowledged.
SUPPORTED_API_CONTRACTS: frozenset[str] = frozenset({CURRENT_API_CONTRACT})


#: Entry point group used by external Provider Packages. Matches the
#: ``[project.entry-points."peldrun.llm.providers.v1"]`` table in the
#: package's ``pyproject.toml``.
ENTRY_POINT_GROUP: str = "peldrun.llm.providers.v1"


#: Registration source tags. Used by the security guard in ``register``
#: and by the UI to display where each provider came from.
SOURCE_BUILTIN: str = "builtin"
SOURCE_EXTERNAL: str = "external"
SOURCE_RUNTIME: str = "runtime"


# ---------------------------------------------------------------------------
# Registration record
# ---------------------------------------------------------------------------


@dataclass
class ProviderRegistration:
    """
    Immutable metadata for a single registered provider adapter.

    The ``adapter_class`` is stored as an opaque ``Type`` — this module
    never imports it, never introspects its constructor, never calls it.
    Instantiation happens in ``factory.py`` using the exact strategy the
    caller needs (legacy constructor signature vs new ``ProviderAdapter``
    Protocol).

    Fields
    ------
    provider_id
        Canonical identifier, lowercase. Used as the primary key.
    adapter_class
        The class object that will be instantiated by the factory.
    api_contract
        The contract version the adapter declares. Must be present in
        ``SUPPORTED_API_CONTRACTS`` — enforced at registration.
    source
        One of ``SOURCE_BUILTIN`` / ``SOURCE_EXTERNAL`` / ``SOURCE_RUNTIME``.
    display_name
        Human-readable name shown in the Store and settings UI.
    version
        Adapter version string (e.g. ``"1.2.0"``).
    enabled
        Runtime toggle. Disabled providers stay registered but cannot
        be instantiated.
    aliases
        Alternative identifiers that resolve to this registration.
        Preserved for backward compatibility with the legacy
        ``get_provider()`` factory.
    class_path
        ``"module:ClassName"`` — used in diagnostics and error messages.
    package / package_version
        Populated only for external packages; used in the Store UI.
    metadata
        Free-form metadata. Currently unused by the runtime; reserved
        for future Provider Package manifests.
    """

    provider_id: str
    adapter_class: Type
    api_contract: str
    source: str
    display_name: str
    version: str = "1.0.0"
    enabled: bool = True
    aliases: List[str] = field(default_factory=list)
    class_path: str = ""
    package: Optional[str] = None
    package_version: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Populate ``class_path`` once so callers (error messages,
        # diagnostics, logs) never need to introspect the class again.
        if not self.class_path:
            cls = self.adapter_class
            module = getattr(cls, "__module__", "<unknown>")
            name = getattr(cls, "__name__", "<unknown>")
            self.class_path = f"{module}:{name}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ProviderRegistry:
    """
    Thread-safe registry of provider adapters.

    All mutating and reading operations acquire ``self._lock``. Reads
    are cheap and lock contention is negligible for the scale of a
    provider registry (dozens of entries, not thousands).

    This class is designed to be instantiated exactly once per process.
    Tests that need isolation should instantiate a fresh
    ``ProviderRegistry()`` instead of using the module-level singleton.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._registrations: Dict[str, ProviderRegistration] = {}
        self._entry_points_discovered: bool = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        provider_id: str,
        adapter_class: Type,
        *,
        api_contract: Optional[str] = None,
        source: str = SOURCE_RUNTIME,
        display_name: Optional[str] = None,
        version: str = "1.0.0",
        aliases: Optional[List[str]] = None,
        enabled: bool = True,
        overwrite: bool = False,
        package: Optional[str] = None,
        package_version: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ProviderRegistration:
        """
        Register a provider adapter.

        Parameters
        ----------
        provider_id
            Canonical identifier. Normalized to lowercase and stripped.
        adapter_class
            The class that will be instantiated by the factory. Never
            imported or introspected by this method.
        api_contract
            Contract version. Resolution order:
                1. ``adapter_class.API_CONTRACT`` (if declared)
                2. The ``api_contract`` parameter
                3. ``CURRENT_API_CONTRACT``
            The resolved value MUST be in ``SUPPORTED_API_CONTRACTS``,
            otherwise ``ProviderContractIncompatibleError`` is raised.
        source
            One of ``SOURCE_BUILTIN`` / ``SOURCE_EXTERNAL`` /
            ``SOURCE_RUNTIME``. Used by the overwrite security guard.
        display_name
            Human-readable name. Defaults to ``provider_id``.
        version
            Adapter version. Display only.
        aliases
            Alternative identifiers that resolve to this registration.
            Aliases must be unique across the registry.
        enabled
            Initial enabled state.
        overwrite
            When ``True``, replace an existing registration. When
            ``False`` (default), an existing registration raises
            ``ProviderAlreadyRegisteredError``.
        package / package_version
            Populated only for external packages.
        metadata
            Free-form. Stored verbatim on the registration.

        Raises
        ------
        ProviderContractIncompatibleError
            When ``api_contract`` is not in ``SUPPORTED_API_CONTRACTS``.
        ProviderAlreadyRegisteredError
            When ``overwrite=False`` and the id (or any alias) is
            already taken, OR when an external package attempts to
            overwrite a builtin provider.
        """
        normalized_id = self._normalize(provider_id)
        if not normalized_id:
            raise ProviderAlreadyRegisteredError(
                "provider_id must be a non-empty string.",
                provider=provider_id,
            )

        resolved_contract = self._resolve_contract(adapter_class, api_contract)
        if resolved_contract not in SUPPORTED_API_CONTRACTS:
            raise ProviderContractIncompatibleError(
                f"Provider '{normalized_id}' declares API contract "
                f"'{resolved_contract}', which this build does not "
                f"support. Supported versions: "
                f"{sorted(SUPPORTED_API_CONTRACTS)}.",
                provider=normalized_id,
                details={
                    "declared": resolved_contract,
                    "supported": sorted(SUPPORTED_API_CONTRACTS),
                    "class_path": self._class_path(adapter_class),
                },
            )

        normalized_aliases = sorted({
            self._normalize(a) for a in (aliases or []) if a
        } - {normalized_id})

        registration = ProviderRegistration(
            provider_id=normalized_id,
            adapter_class=adapter_class,
            api_contract=resolved_contract,
            source=source,
            display_name=display_name or normalized_id,
            version=version,
            enabled=enabled,
            aliases=normalized_aliases,
            class_path=self._class_path(adapter_class),
            package=package,
            package_version=package_version,
            metadata=metadata or {},
        )

        with self._lock:
            existing = self._registrations.get(normalized_id)
            if existing is not None and not overwrite:
                raise ProviderAlreadyRegisteredError(
                    f"Provider '{normalized_id}' is already registered "
                    f"(source={existing.source}, "
                    f"class={existing.class_path}). "
                    f"Pass overwrite=True to replace it.",
                    provider=normalized_id,
                    details={
                        "existing_class": existing.class_path,
                        "existing_source": existing.source,
                    },
                )

            # Security guard: external packages cannot overwrite builtins.
            if (
                existing is not None
                and existing.source == SOURCE_BUILTIN
                and source != SOURCE_BUILTIN
            ):
                raise ProviderAlreadyRegisteredError(
                    f"Cannot overwrite built-in provider "
                    f"'{normalized_id}' from source '{source}'. "
                    f"Built-in providers are protected.",
                    provider=normalized_id,
                    details={
                        "existing_source": existing.source,
                        "attempted_source": source,
                    },
                )

            # Alias collision detection across the whole registry.
            for alias in normalized_aliases:
                for other in self._registrations.values():
                    if other.provider_id == normalized_id:
                        continue
                    if alias == other.provider_id or alias in other.aliases:
                        raise ProviderAlreadyRegisteredError(
                            f"Alias '{alias}' for provider "
                            f"'{normalized_id}' is already used by "
                            f"provider '{other.provider_id}'.",
                            provider=normalized_id,
                            details={"conflicting_alias": alias},
                        )

            self._registrations[normalized_id] = registration
            logger.debug(
                "Registered provider '%s' (source=%s, contract=%s, "
                "class=%s, aliases=%s)",
                normalized_id,
                source,
                resolved_contract,
                registration.class_path,
                normalized_aliases,
            )
            return registration

    def unregister(
        self,
        provider_id: str,
        *,
        require_source: Optional[str] = None,
    ) -> ProviderRegistration:
        """
        Remove a provider registration.

        Parameters
        ----------
        provider_id
            Canonical id or alias.
        require_source
            When provided, only remove the registration if its source
            matches. Used by the Store uninstall path to prevent an
            external package from accidentally removing a builtin.

        Raises
        ------
        ProviderNotRegisteredError
            When the id is not registered, or the source does not match.
        """
        normalized_id = self._normalize(provider_id)
        with self._lock:
            registration = self._lookup_locked(normalized_id)
            if registration is None:
                raise ProviderNotRegisteredError(
                    f"Provider '{provider_id}' is not registered.",
                    provider=provider_id,
                )
            if require_source and registration.source != require_source:
                raise ProviderNotRegisteredError(
                    f"Provider '{provider_id}' has source "
                    f"'{registration.source}', not '{require_source}'; "
                    f"refusing to unregister.",
                    provider=provider_id,
                    details={
                        "actual_source": registration.source,
                        "required_source": require_source,
                    },
                )
            del self._registrations[registration.provider_id]
            logger.debug(
                "Unregistered provider '%s' (source=%s)",
                registration.provider_id,
                registration.source,
            )
            return registration

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def enable(self, provider_id: str) -> ProviderRegistration:
        """Re-enable a previously disabled provider."""
        return self._set_enabled(provider_id, True)

    def disable(self, provider_id: str) -> ProviderRegistration:
        """Disable a provider without unregistering it."""
        return self._set_enabled(provider_id, False)

    def _set_enabled(self, provider_id: str, enabled: bool) -> ProviderRegistration:
        normalized_id = self._normalize(provider_id)
        with self._lock:
            registration = self._lookup_locked(normalized_id)
            if registration is None:
                raise ProviderNotRegisteredError(
                    f"Provider '{provider_id}' is not registered.",
                    provider=provider_id,
                )
            registration.enabled = enabled
            logger.debug(
                "Provider '%s' %s.",
                registration.provider_id,
                "enabled" if enabled else "disabled",
            )
            return registration

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get(self, provider_id: str) -> ProviderRegistration:
        """
        Resolve a provider id or alias to its registration.

        Raises
        ------
        ProviderNotRegisteredError
            When the id is unknown.
        ProviderDisabledError
            When the id is registered but disabled.
        """
        normalized_id = self._normalize(provider_id)
        with self._lock:
            registration = self._lookup_locked(normalized_id)
        if registration is None:
            raise ProviderNotRegisteredError(
                f"Provider '{provider_id}' is not registered. "
                f"Registered providers: "
                f"{sorted(self._registrations.keys())}.",
                provider=provider_id,
            )
        if not registration.enabled:
            raise ProviderDisabledError(
                f"Provider '{provider_id}' is registered but disabled. "
                f"Enable it in Settings before use.",
                provider=provider_id,
                details={
                    "source": registration.source,
                    "class_path": registration.class_path,
                },
            )
        return registration

    def find(self, provider_id: str) -> Optional[ProviderRegistration]:
        """
        Like ``get()`` but returns ``None`` instead of raising.

        Useful for callers that want to check existence without a
        try/except. Does NOT enforce the enabled state — returns the
        registration regardless.
        """
        normalized_id = self._normalize(provider_id)
        with self._lock:
            return self._lookup_locked(normalized_id)

    def is_registered(self, provider_id: str) -> bool:
        """Return True iff the id or alias is registered (regardless of enabled state)."""
        normalized_id = self._normalize(provider_id)
        with self._lock:
            return self._lookup_locked(normalized_id) is not None

    def is_enabled(self, provider_id: str) -> bool:
        """Return True iff the id is registered AND enabled."""
        registration = self.find(provider_id)
        return registration is not None and registration.enabled

    # ------------------------------------------------------------------
    # Bulk listing
    # ------------------------------------------------------------------

    def list_registrations(
        self,
        *,
        enabled_only: bool = False,
        source: Optional[str] = None,
    ) -> List[ProviderRegistration]:
        """
        Return a snapshot list of registrations.

        Sorted by ``display_name`` for stable UI ordering.
        """
        with self._lock:
            items = list(self._registrations.values())
        if enabled_only:
            items = [r for r in items if r.enabled]
        if source is not None:
            items = [r for r in items if r.source == source]
        items.sort(key=lambda r: r.display_name.lower())
        return items

    # ------------------------------------------------------------------
    # Entry point discovery
    # ------------------------------------------------------------------

    def discover_external_providers(
        self, *, force_refresh: bool = False
    ) -> int:
        """
        Scan ``importlib.metadata.entry_points`` for Provider Packages
        and register every adapter found under ``ENTRY_POINT_GROUP``.

        Behavior
        --------
        * Idempotent by default: subsequent calls return 0 without
          re-scanning. Pass ``force_refresh=True`` after a Store
          install to pick up newly added packages.
        * Per-entry-point failures are logged and skipped. A single
          broken package never breaks discovery.
        * Entry points that do not declare a compatible
          ``API_CONTRACT`` are rejected with a clear log message
          (never registered).
        * Registered via ``overwrite=False`` so a package cannot
          silently replace another package's id or a builtin.

        Returns
        -------
        int
            Number of adapters successfully registered by this call.
        """
        with self._lock:
            if self._entry_points_discovered and not force_refresh:
                return 0
            self._entry_points_discovered = True

        count = 0
        eps = self._iter_entry_points()
        for ep in eps:
            try:
                adapter_class = ep.load()
            except Exception as exc:
                logger.warning(
                    "Failed to load entry point '%s' for provider '%s': %s",
                    getattr(ep, "value", "<unknown>"),
                    ep.name,
                    exc,
                )
                continue

            # Capture package metadata for the Store UI. Best-effort:
            # ``ep.dist`` is not populated on every Python version.
            package_name: Optional[str] = None
            package_version: Optional[str] = None
            try:
                dist = getattr(ep, "dist", None)
                if dist is not None:
                    package_name = getattr(dist, "name", None)
                    package_version = getattr(dist, "version", None)
            except Exception:
                pass

            try:
                self.register(
                    provider_id=ep.name,
                    adapter_class=adapter_class,
                    source=SOURCE_EXTERNAL,
                    overwrite=False,
                    package=package_name,
                    package_version=package_version,
                )
                count += 1
                logger.info(
                    "Registered external provider '%s' from package '%s'.",
                    ep.name,
                    package_name or "<unknown>",
                )
            except ProviderContractIncompatibleError as exc:
                logger.error(
                    "Skipping external provider '%s': %s",
                    ep.name,
                    exc.message,
                )
            except ProviderAlreadyRegisteredError as exc:
                logger.warning(
                    "Skipping external provider '%s': %s",
                    ep.name,
                    exc.message,
                )
            except Exception as exc:
                logger.warning(
                    "Unexpected error registering external provider "
                    "'%s': %s",
                    ep.name,
                    exc,
                )

        return count

    # ------------------------------------------------------------------
    # Test / reset helper
    # ------------------------------------------------------------------

    def clear(self, *, preserve_builtins: bool = False) -> None:
        """
        Remove registrations. Intended for tests and clean shutdown.

        ``preserve_builtins=True`` keeps only built-in providers, which
        is useful in tests that want to reset external / runtime
        registrations without re-running the bootstrap.
        """
        with self._lock:
            if preserve_builtins:
                self._registrations = {
                    pid: reg
                    for pid, reg in self._registrations.items()
                    if reg.source == SOURCE_BUILTIN
                }
            else:
                self._registrations.clear()
            self._entry_points_discovered = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(value: str) -> str:
        """Normalize an identifier to its canonical lowercase form."""
        return (value or "").strip().lower()

    @staticmethod
    def _class_path(cls: Type) -> str:
        """Return ``"module:ClassName"`` for diagnostics."""
        module = getattr(cls, "__module__", "<unknown>")
        name = getattr(cls, "__name__", "<unknown>")
        return f"{module}:{name}"

    @staticmethod
    def _resolve_contract(
        adapter_class: Type, explicit: Optional[str]
    ) -> str:
        """
        Resolve the API contract version for a registration.

        Precedence:
            1. ``adapter_class.API_CONTRACT`` — the class declares truth.
            2. ``explicit`` — the caller vouches (used by built-in
               bootstrap before adapters declare the attribute).
            3. ``CURRENT_API_CONTRACT`` — final fallback.
        """
        class_value = getattr(adapter_class, "API_CONTRACT", None)
        if isinstance(class_value, str) and class_value.strip():
            return class_value.strip()
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        return CURRENT_API_CONTRACT

    def _lookup_locked(self, normalized_id: str) -> Optional[ProviderRegistration]:
        """
        Caller MUST already hold ``self._lock``.

        Tries the canonical id first, then falls back to scanning
        aliases. The alias scan is O(N) but N is small (dozens).
        """
        registration = self._registrations.get(normalized_id)
        if registration is not None:
            return registration
        for candidate in self._registrations.values():
            if normalized_id in candidate.aliases:
                return candidate
        return None

    @staticmethod
    def _iter_entry_points() -> List[Any]:
        """
        Cross-version entry point iteration.

        * Python 3.10+ / modern backports: ``entry_points(group=...)``
          returns a list directly.
        * Older Python: ``entry_points()`` returns a dict of
          ``{group: [EntryPoint]}``.
        """
        try:
            return list(entry_points(group=ENTRY_POINT_GROUP))
        except TypeError:
            # Old-style API (Python < 3.10).
            try:
                all_eps = entry_points()
                if hasattr(all_eps, "get"):
                    return list(all_eps.get(ENTRY_POINT_GROUP, []))
                return list(all_eps.select(group=ENTRY_POINT_GROUP))
            except Exception:
                return []


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


#: Global provider registry. Bootstrap wiring lives in
#: ``providers/__init__.py``, not here — this module stays free of any
#: provider imports.
provider_registry = ProviderRegistry()


__all__ = [
    "CURRENT_API_CONTRACT",
    "SUPPORTED_API_CONTRACTS",
    "ENTRY_POINT_GROUP",
    "SOURCE_BUILTIN",
    "SOURCE_EXTERNAL",
    "SOURCE_RUNTIME",
    "ProviderRegistration",
    "ProviderRegistry",
    "provider_registry",
]
