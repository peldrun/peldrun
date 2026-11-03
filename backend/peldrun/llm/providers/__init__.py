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

4. No discovery of external packages happens here. External Provider
   Packages (M9) are loaded by an explicit call to
   ``provider_registry.discover_external_providers()`` from the Store
   bootstrap path (M10). This keeps app startup fast and predictable.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from peldrun.llm.client import LLMConfig
from peldrun.llm.providers.lmstudio import LMStudioProvider
from peldrun.llm.providers.ollama import OllamaProvider
from peldrun.llm.providers.openai_compat import BaseLLMProvider, OpenAICompatProvider

# Native adapters (M5). Declared ADAPTER_STYLE = "v1" and PROFILE
# as class attributes. Registered below alongside the legacy adapters
# so that `provider_factory.create("openai_native", ...)` works
# immediately after this module is imported.
from peldrun.llm.providers.openai_native import OpenAINativeAdapter
from peldrun.llm.providers.anthropic import AnthropicAdapter
from peldrun.llm.providers.google_genai import GoogleGenAIAdapter
from peldrun.llm.providers.ollama_native import OllamaNativeAdapter
from peldrun.llm.providers.lmstudio_native import LMStudioNativeAdapter

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
    # No aliases — legacy IDs stay bound to the legacy implementations
    # to preserve existing configuration behavior. The native path is
    # reachable by its canonical ID only.
    # ------------------------------------------------------------------
    provider_registry.register(
        provider_id="openai_native",
        adapter_class=OpenAINativeAdapter,
        api_contract="peldrun.llm.provider.v1",
        source=SOURCE_BUILTIN,
        display_name="OpenAI (Native)",
        version="1.0.0",
        overwrite=True,
    )
    provider_registry.register(
        provider_id="anthropic",
        adapter_class=AnthropicAdapter,
        api_contract="peldrun.llm.provider.v1",
        source=SOURCE_BUILTIN,
        display_name="Anthropic",
        version="1.0.0",
        aliases=["claude"],
        overwrite=True,
    )
    provider_registry.register(
        provider_id="google_genai",
        adapter_class=GoogleGenAIAdapter,
        api_contract="peldrun.llm.provider.v1",
        source=SOURCE_BUILTIN,
        display_name="Google Gemini (Native)",
        version="1.0.0",
        aliases=["gemini", "google-gemini", "google_gemini"],
        overwrite=True,
    )
    provider_registry.register(
        provider_id="ollama_native",
        adapter_class=OllamaNativeAdapter,
        api_contract="peldrun.llm.provider.v1",
        source=SOURCE_BUILTIN,
        display_name="Ollama (Native)",
        version="1.0.0",
        overwrite=True,
    )
    provider_registry.register(
        provider_id="lmstudio_native",
        adapter_class=LMStudioNativeAdapter,
        api_contract="peldrun.llm.provider.v1",
        source=SOURCE_BUILTIN,
        display_name="LM Studio (Native)",
        version="1.0.0",
        overwrite=True,
    )

    logger.debug(
        "Built-in provider registry bootstrapped: %s",
        [r.provider_id for r in provider_registry.list_registrations()],
    )


_bootstrap_builtin_providers()


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
]
