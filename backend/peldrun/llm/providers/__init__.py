# backend/peldrun/llm/providers/__init__.py
"""
PELDRUN Core LLM Providers Subsystem.

Exports:
    * The pre-existing ``get_provider()`` factory — preserved verbatim
      for backward compatibility with any external caller. Its signature
      and return type are UNCHANGED.
    * The new ``ProviderFactory`` / ``provider_factory`` — the canonical
      creation path used by ``runtime/executor.py`` and
      ``omweb/agent_bridge_parts/dispatcher.py`` after M7.
    * The ``ProviderRegistry`` / ``provider_registry`` — populated with
      every built-in adapter at import time so that
      ``provider_factory.create()`` works without a separate bootstrap
      call.

Registered built-in adapters (8 total, as of M5):

    Legacy (unchanged contract):
        openai_compat  (aliases: openai, default)
        lmstudio       (aliases: lm_studio, lm-studio)
        ollama

    Native (M5 additions):
        openai_native  — Chat Completions + Responses API
        anthropic      (alias: claude)
        google_genai   (aliases: gemini, google-gemini, google_gemini)
        ollama_native  — /api/chat with think + tool_calls
        lmstudio_native — /api/v0 with reasoning field + stats

Design notes
------------

1. ``get_provider()`` is retained purely as a compatibility shim. It
   is no longer the recommended entry point — callers should migrate
   to ``provider_factory.create()`` (with a ``ConnectionContext``) so
   that capabilities and profiles are resolved correctly. The function
   is kept so that no external import breaks.

2. The registry bootstrap runs EXACTLY ONCE per process, on module
   import. It uses ``overwrite=True`` so a hot reload of this module
   (e.g. pytest with ``--reload``) does not raise
   ``ProviderAlreadyRegisteredError``. Built-in source protection
   remains intact because the registry rejects external overwrites of
   builtins regardless of the ``overwrite`` flag.

3. Aliases (``openai``, ``default``, ``lm_studio``, ``lm-studio``) are
   preserved so that any config value written by the pre-M2 UI keeps
   resolving. New aliases can be added without touching this file.

4. **External package discovery is EXPLICIT, not automatic.**
   Built-in adapters are registered at module import (below).
   External Provider Packages (installed as pip entry points) are only
   picked up when the caller invokes
   ``ensure_external_providers_discovered()``. This keeps app startup
   fast and predictable, and lets the Store trigger a fresh scan after
   a successful install without restarting the backend.

   Discovery paths (M9 phase 1):

   * **Entry-point discovery** — packages that were pip-installed into
     the MAIN Python environment and declare an entry point in
     ``peldrun.llm.providers.v1``. This path is handled by
     ``ensure_external_providers_discovered()`` below.

   * **M9-installed packages** — packages installed via
     ``/api/store/providers/upload`` or ``install-from-catalog``.
     These ship their own isolated venv and require the plugin_host
     (phase 2) to actually execute. They are visible in the Store UI
     immediately after install; they appear in LLM Settings once the
     plugin_host registers them at its startup.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from peldrun.llm.client import LLMConfig
from peldrun.llm.providers.lmstudio import LMStudioProvider
from peldrun.llm.providers.ollama import OllamaProvider
from peldrun.llm.providers.openai_compat import BaseLLMProvider, OpenAICompatProvider

# Native adapters (M5) are imported LAZILY via module-level
# `__getattr__` (PEP 562) and inside `_bootstrap_builtin_providers()`.
# They are NOT imported eagerly here because each requires a
# provider-specific SDK that may not be installed in the current
# interpreter. In particular, plugin_host workers run inside an
# isolated venv that only has the SDK for ONE package — eager imports
# would crash the worker with ModuleNotFoundError before any adapter
# could be loaded.

# New: registry, factory, and context types.
from peldrun.llm.providers.registry import (
    ProviderRegistration,
    ProviderRegistry,
    SOURCE_BUILTIN,
    SOURCE_EXTERNAL,
    SOURCE_RUNTIME,
    provider_registry,
)
from peldrun.llm.providers.factory import (
    ProviderContext,
    ProviderFactory,
    provider_factory,
)

logger = logging.getLogger("peldrun.llm.providers")


# ---------------------------------------------------------------------------
# Built-in adapter registration (runs once on module import)
# ---------------------------------------------------------------------------


def _bootstrap_builtin_providers() -> None:
    """
    Register every built-in adapter in the global registry.

    Two groups:

    1. **Legacy adapters** (``openai_compat`` / ``lmstudio`` / ``ollama``)
       — retained for full backward compatibility. Existing configs
       that point at these identifiers keep working unchanged. They
       do NOT declare ``API_CONTRACT`` as a class attribute, so the
       contract version is passed explicitly.

    2. **Native adapters** (``openai_native`` / ``anthropic`` /
       ``google_genai`` / ``ollama_native`` / ``lmstudio_native``) —
       added in M5. They declare ``ADAPTER_STYLE = "v1"`` and a
       ``PROFILE`` class attribute, so the factory can instantiate
       and profile them without a per-adapter bootstrap step.

    Idempotent: every registration passes ``overwrite=True`` so a hot
    reload of this module does not raise. The registry's built-in-
    source guard still prevents external packages from replacing any
    of these entries.

    No aliases are attached to the new native adapters. Users who
    want the native implementations must reference them by their
    canonical IDs. The legacy IDs remain aliased to their legacy
    implementations so that no existing configuration silently
    switches behavior.
    """
    provider_registry.register(
        provider_id="openai_compat",
        adapter_class=OpenAICompatProvider,
        api_contract="peldrun.llm.provider.v1",
        source=SOURCE_BUILTIN,
        display_name="OpenAI-Compatible",
        version="1.0.0",
        aliases=["openai", "default"],
        overwrite=True,
    )
    provider_registry.register(
        provider_id="lmstudio",
        adapter_class=LMStudioProvider,
        api_contract="peldrun.llm.provider.v1",
        source=SOURCE_BUILTIN,
        display_name="LM Studio",
        version="1.0.0",
        aliases=["lm_studio", "lm-studio"],
        overwrite=True,
    )
    provider_registry.register(
        provider_id="ollama",
        adapter_class=OllamaProvider,
        api_contract="peldrun.llm.provider.v1",
        source=SOURCE_BUILTIN,
        display_name="Ollama",
        version="1.0.0",
        overwrite=True,
    )

    # ------------------------------------------------------------------
    # Native adapters (M5).
    #
    # Each adapter is imported LAZILY inside this function so a missing
    # provider SDK (e.g. `anthropic` inside a GigaChat-only worker venv)
    # does NOT crash the bootstrap. Individual failures are logged and
    # skipped — the remaining adapters still register normally.
    # ------------------------------------------------------------------
    import importlib as _importlib

    def _register_native(
        provider_id: str,
        module_path: str,
        class_name: str,
        display_name: str,
        *,
        aliases: Optional[list] = None,
    ) -> None:
        try:
            module = _importlib.import_module(module_path)
            adapter_class = getattr(module, class_name)
        except Exception as exc:
            logger.warning(
                "Skipping built-in provider '%s' — SDK not available "
                "in this environment (%s: %s). This is expected for "
                "isolated plugin worker venvs.",
                provider_id,
                type(exc).__name__,
                exc,
            )
            return
        provider_registry.register(
            provider_id=provider_id,
            adapter_class=adapter_class,
            api_contract="peldrun.llm.provider.v1",
            source=SOURCE_BUILTIN,
            display_name=display_name,
            version="1.0.0",
            aliases=aliases or [],
            overwrite=True,
        )

    _register_native(
        "openai_native",
        "peldrun.llm.providers.openai_native",
        "OpenAINativeAdapter",
        "OpenAI (Native)",
    )
    _register_native(
        "anthropic",
        "peldrun.llm.providers.anthropic",
        "AnthropicAdapter",
        "Anthropic",
        aliases=["claude"],
    )
    _register_native(
        "google_genai",
        "peldrun.llm.providers.google_genai",
        "GoogleGenAIAdapter",
        "Google Gemini (Native)",
        aliases=["gemini", "google-gemini", "google_gemini"],
    )
    _register_native(
        "ollama_native",
        "peldrun.llm.providers.ollama_native",
        "OllamaNativeAdapter",
        "Ollama (Native)",
    )
    _register_native(
        "lmstudio_native",
        "peldrun.llm.providers.lmstudio_native",
        "LMStudioNativeAdapter",
        "LM Studio (Native)",
    )

    logger.debug(
        "Built-in provider registry bootstrapped: %s",
        [r.provider_id for r in provider_registry.list_registrations()],
    )


_bootstrap_builtin_providers()


# ---------------------------------------------------------------------------
# External provider discovery (M9)
# ---------------------------------------------------------------------------


#: Guard against concurrent ``discover_external_providers`` calls. The
#: underlying registry is thread-safe, but the discovery method scans
#: ``importlib.metadata.entry_points`` which is expensive — serializing
#: the scan avoids duplicate work when two callers race on the first
#: invocation (e.g. lifespan startup + first config request).
_external_discovery_lock = threading.Lock()

#: Set to ``True`` after the first successful (or empty) discovery
#: attempt. A subsequent call with ``force=False`` becomes a no-op.
_external_discovery_attempted = False


def ensure_external_providers_discovered(force: bool = False) -> int:
    """
    Discover Provider Packages installed as pip entry points.

    Call sites
    ----------
    * ``omweb.main`` lifespan startup — picks up packages installed
      before the current backend process started.
    * ``omweb.routers.config_rtr`` before ``fetch-models`` — ensures
      packages installed while the backend was running are visible
      to the discovery path.
    * Anywhere a caller needs a fresh scan — pass ``force=True``.

    Behavior
    --------
    * **Idempotent by default.** After the first call, subsequent calls
      with ``force=False`` return 0 immediately. This is intentional:
      scanning ``importlib.metadata`` is not free, and nothing changes
      between two sequential calls in the same process.
    * **Thread-safe.** Two concurrent first-calls serialize; the
      second sees ``_external_discovery_attempted=True`` and returns 0.
    * **Never raises.** Any failure is logged at ERROR level and
      returns 0. External discovery must never break backend startup
      — the built-in adapters are already registered regardless of
      what external discovery finds.
    * **Idempotent registration.** The underlying registry uses
      ``overwrite=False`` for external packages, so re-running
      discovery does NOT clobber an existing registration. It only
      adds new entries.

    Parameters
    ----------
    force
        When ``True``, re-scans even if a previous call completed. Use
        this after the Store installs a new entry-point package (rare —
        most M9 installs ship isolated venvs and are handled by the
        plugin_host instead).

    Returns
    -------
    int
        The number of NEW providers registered by this call. ``0``
        when nothing was added (either because none exist, or because
        the call was a no-op due to ``_external_discovery_attempted``).
    """
    global _external_discovery_attempted

    with _external_discovery_lock:
        if _external_discovery_attempted and not force:
            return 0

        try:
            count = provider_registry.discover_external_providers(
                force_refresh=force,
            )
        except Exception as exc:
            # A failure here must never break startup or a config
            # request. The built-in adapters are already registered.
            logger.error(
                "External provider discovery failed: %s",
                exc,
                exc_info=True,
            )
            _external_discovery_attempted = True
            return 0

        _external_discovery_attempted = True

        if count:
            logger.info(
                "Discovered %d external provider package(s).", count
            )
        else:
            logger.debug(
                "External provider discovery ran; no new packages found."
            )
        return count


# ---------------------------------------------------------------------------
# Legacy compatibility factory (unchanged signature, preserved behavior)
# ---------------------------------------------------------------------------


def get_provider(
    provider_name: str = "openai_compat",
    config: Optional[LLMConfig] = None,
    **kwargs: Any,
) -> BaseLLMProvider:
    """
    Compatibility factory for the pre-M2 provider identifiers.

    .. deprecated::
        Use ``provider_factory.create(provider_id, connection)``
        instead. That path resolves an ``AdapterProfile`` and wraps
        legacy adapters so both execution modes (agent + chat) share
        the same interface.

    Preserved behavior
    ------------------
    * Accepts the exact same identifiers the pre-M2 factory accepted:
      ``openai_compat`` / ``openai`` / ``default`` / ``lmstudio`` /
      ``lm_studio`` / ``lm-studio`` / ``ollama``.
    * Returns the raw adapter class, NOT wrapped in
      ``_LegacyAdapterBridge``. This matches the pre-M2 return type
      exactly.
    * Raises ``ValueError`` for unknown identifiers — matching the
      pre-M2 behavior (the new registry raises
      ``ProviderNotRegisteredError`` instead, which is a different
      exception type).

    Fixed quirk
    -----------
    The pre-M2 implementation silently dropped the ``config`` argument
    for ``lmstudio`` and ``ollama``::

        return LMStudioProvider(**kwargs)   # config=config was omitted

    This was an unintentional bug; ``config=config`` is now forwarded
    for every branch. Any caller relying on the old (broken) behavior
    would already have been passing the config through ``kwargs``
    explicitly, which continues to work.
    """
    key = (provider_name or "").lower().strip()
    if key in ("lmstudio", "lm_studio", "lm-studio"):
        return LMStudioProvider(config=config, **kwargs)
    elif key == "ollama":
        return OllamaProvider(config=config, **kwargs)
    elif key in ("openai", "openai_compat", "default"):
        return OpenAICompatProvider(config=config, **kwargs)
    else:
        raise ValueError(f"Unsupported LLM provider identifier: '{provider_name}'")


# ---------------------------------------------------------------------------
# Lazy attribute access (PEP 562)
# ---------------------------------------------------------------------------
#
# Adapter classes are imported on demand so that
# `from peldrun.llm.providers import OpenAINativeAdapter` still works
# without making the package __init__ eager-load every provider SDK.
# This is essential for plugin_host workers that run in isolated venvs
# containing only ONE provider's SDK.

_LAZY_ADAPTERS = {
    "BaseLLMProvider": "peldrun.llm.providers.openai_compat.BaseLLMProvider",
    "OpenAICompatProvider": "peldrun.llm.providers.openai_compat.OpenAICompatProvider",
    "LMStudioProvider": "peldrun.llm.providers.lmstudio.LMStudioProvider",
    "OllamaProvider": "peldrun.llm.providers.ollama.OllamaProvider",
    "OpenAINativeAdapter": "peldrun.llm.providers.openai_native.OpenAINativeAdapter",
    "AnthropicAdapter": "peldrun.llm.providers.anthropic.AnthropicAdapter",
    "GoogleGenAIAdapter": "peldrun.llm.providers.google_genai.GoogleGenAIAdapter",
    "OllamaNativeAdapter": "peldrun.llm.providers.ollama_native.OllamaNativeAdapter",
    "LMStudioNativeAdapter": "peldrun.llm.providers.lmstudio_native.LMStudioNativeAdapter",
}


def __getattr__(name: str) -> Any:
    """PEP 562 — import adapter classes on demand."""
    import importlib as _importlib

    target = _LAZY_ADAPTERS.get(name)
    if target is None:
        raise AttributeError(
            f"module 'peldrun.llm.providers' has no attribute {name!r}"
        )
    module_path, _, class_name = target.rpartition(".")
    module = _importlib.import_module(module_path)
    value = getattr(module, class_name)
    globals()[name] = value
    return value


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------


__all__ = [
    # Legacy (unchanged)
    "BaseLLMProvider",
    "OpenAICompatProvider",
    "LMStudioProvider",
    "OllamaProvider",
    "get_provider",
    # Native adapters (M5) — instantiable via provider_factory.create()
    "OpenAINativeAdapter",
    "AnthropicAdapter",
    "GoogleGenAIAdapter",
    "OllamaNativeAdapter",
    "LMStudioNativeAdapter",
    # Canonical creation path
    "ProviderFactory",
    "provider_factory",
    "ProviderContext",
    "ProviderRegistry",
    "provider_registry",
    "ProviderRegistration",
    # Registration source constants (used by Store in M10)
    "SOURCE_BUILTIN",
    "SOURCE_EXTERNAL",
    "SOURCE_RUNTIME",
    # M9 discovery entry point
    "ensure_external_providers_discovered",
]
