# backend/peldrun/llm/providers/factory.py
"""
PELDRUN Provider Adapter Factory.

Central instantiation point for every provider adapter. Both execution
paths (agent mode and direct chat) will call ``provider_factory.create()``
after M7 to obtain a ``ProviderAdapter``-conforming object — regardless
of whether the underlying provider is a built-in, a Native adapter added
in M5, or an external Provider Package loaded in M9.

Design invariants
-----------------

1. **Uniform return type.** ``create()`` always returns an object
   matching the ``ProviderAdapter`` Protocol from ``contracts.py``.
   Legacy adapters (``OpenAICompatProvider``, ``LMStudioProvider``,
   ``OllamaProvider``) are transparently wrapped in
   ``_LegacyAdapterBridge`` so callers cannot tell the difference.
   This is what makes the migration from the old factory a pure
   refactor with no behavior change.

2. **No caching.** The factory returns a fresh adapter per call. The
   existing code creates a new ``AsyncLLMClient`` (and therefore a new
   ``AsyncOpenAI``) per run already — caching would introduce lifecycle
   questions (who closes what, when) that do not exist today. Caching
   can be added later if profiling shows it is worth it.

3. **Constructor flexibility.** New adapters receive a
   ``ProviderContext`` (matching the plan example in §10.5, where the
   adapter reads ``context.credentials`` and ``context.connection``).
   Legacy adapters receive a constructed ``LLMConfig``. The factory
   dispatches on ``adapter_class.ADAPTER_STYLE``. Absence of the
   attribute means legacy — this is how the three existing adapters
   work today without modification.

4. **Safe defaults for legacy profiles.** Each legacy provider has a
   sensible ``AdapterProfile`` pre-declared. This lets
   ``compute_resolved_capabilities()`` work correctly even before the
   legacy adapters are converted to M5 native adapters. The profiles
   are conservative — they declare ONLY what the legacy adapters
   actually do, not what a hypothetical ideal adapter might do.

5. **Never raises from discovery.** ``_LegacyAdapterBridge.discover_models``
   and ``.validate_connection`` NEVER raise. A provider whose discovery
   endpoint is missing or broken must still be usable — the user enters
   models manually (plan §10.1 — "Compatible Provider: لا يحتاج كوداً").

6. **No imports of legacy adapters.** This module imports only
   ``contracts.py``, ``request.py``, ``client.py``, ``errors.py``, and
   ``registry.py``. The actual adapter classes are stored opaquely in
   the registry; the factory never needs to know their names.

See also
--------

    * ``backend/peldrun/llm/contracts.py``          — ProviderAdapter Protocol
    * ``backend/peldrun/llm/request.py``            — LLMRequest
    * ``backend/peldrun/llm/providers/registry.py`` — ProviderRegistry
    * ``backend/peldrun/llm/providers/__init__.py`` — bootstrap wiring
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Type

from peldrun.llm.client import (
    LLMConfig,
    LLMResponse,
    StreamChunk,
)
from peldrun.llm.contracts import (
    AdapterFeatures,
    AdapterProfile,
    ConnectionContext,
    LLMStreamEvent,
    ProviderAdapter,
    ReasoningStrategy,
    StreamEventType,
)
from peldrun.llm.errors import ProviderNotRegisteredError
from peldrun.llm.providers.registry import (
    ProviderRegistration,
    ProviderRegistry,
    provider_registry,
)
from peldrun.llm.request import LLMRequest

logger = logging.getLogger("peldrun.llm.providers.factory")


# ---------------------------------------------------------------------------
# Adapter style detection
# ---------------------------------------------------------------------------


class AdapterStyle(str, Enum):
    """
    Adapter protocol version.

    ``LEGACY`` — the pre-M2 adapters (``OpenAICompatProvider`` and
    subclasses). Constructor takes ``config: LLMConfig``; methods take
    ``messages / tools / model`` as keyword arguments and return
    ``LLMResponse`` / ``StreamChunk``.

    ``V1`` — the ``ProviderAdapter`` protocol from ``contracts.py``.
    Constructor takes a ``ProviderContext``; methods take a single
    ``LLMRequest`` and return ``LLMResponse`` / ``LLMStreamEvent``.

    Detection is via the ``ADAPTER_STYLE`` class attribute. Absence of
    the attribute defaults to ``LEGACY`` so the three existing adapters
    work without modification.
    """

    LEGACY = "legacy"
    V1 = "v1"


def _detect_adapter_style(adapter_class: Type) -> AdapterStyle:
    """Return the adapter style declared by the class, or LEGACY by default."""
    declared = getattr(adapter_class, "ADAPTER_STYLE", None)
    if isinstance(declared, str) and declared.strip().lower() == "v1":
        return AdapterStyle.V1
    return AdapterStyle.LEGACY


# ---------------------------------------------------------------------------
# Provider context (passed to V1 adapters)
# ---------------------------------------------------------------------------


@dataclass
class ProviderContext:
    """
    Construction-time context passed to V1 adapters.

    Mirrors the shape used in the plan's example (Section 10.5), where
    an adapter's ``__init__`` receives a single ``context`` object and
    reads ``context.connection``, ``context.profile``, and
    ``context.credentials``.

    ``credentials`` is a plain dict for forward compatibility with
    Provider Packages that need per-connection secret routing beyond a
    single API key. Today it only contains ``"api_key"``.
    """

    connection: ConnectionContext
    profile: AdapterProfile
    credentials: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        connection: ConnectionContext,
        profile: AdapterProfile,
    ) -> "ProviderContext":
        """Convenience constructor that populates ``credentials`` from the connection."""
        credentials: Dict[str, str] = {}
        if connection.api_key:
            credentials["api_key"] = connection.api_key
        return cls(
            connection=connection,
            profile=profile,
            credentials=credentials,
        )


# ---------------------------------------------------------------------------
# Legacy profiles (conservative — declare ONLY what legacy adapters do)
# ---------------------------------------------------------------------------


def _legacy_openai_profile() -> AdapterProfile:
    """Profile for OpenAICompatProvider (base class)."""
    return AdapterProfile(
        provider_id="openai_compat",
        api_surface="chat_completions",
        features=AdapterFeatures(
            model_discovery=False,        # Base class has no discover_models
            native_streaming=True,        # AsyncLLMClient supports true streaming
            buffered_streaming=True,      # Fallback path exists
            tool_calling=True,            # sanitize_tools_for_openai
            parallel_tool_calls=True,     # Passed through to provider
            forced_tool_choice=True,      # Passed through to provider
            vision=True,                  # image_url parts pass through
            audio=False,                  # Not wired in AsyncLLMClient
            structured_output=True,       # response_format passes through
            usage_reporting=True,         # stream_options.include_usage
            reasoning=True,               # reasoning_effort via extra_body
            prompt_caching=False,         # Not explicitly wired
        ),
        reasoning_strategies=[ReasoningStrategy.OPENAI_COMPATIBLE],
        # No supported_param_names restriction — legacy adapters forward
        # everything, so the compiler must not gate them either.
    )


def _legacy_lmstudio_profile() -> AdapterProfile:
    """Profile for LMStudioProvider — same as OpenAI-compat + discovery."""
    profile = _legacy_openai_profile()
    profile.provider_id = "lmstudio"
    profile.features.model_discovery = True  # Has discover_models()
    return profile


def _legacy_ollama_profile() -> AdapterProfile:
    """Profile for OllamaProvider — same as OpenAI-compat + discovery + health."""
    profile = _legacy_openai_profile()
    profile.provider_id = "ollama"
    profile.features.model_discovery = True
    # Older Ollama builds reject parallel_tool_calls in some cases;
    # the plan (§6.4) explicitly notes that Ollama native supports
    # `think` and tool calling, but parallel behavior varies by version.
    # Conservative: mark it unsupported. Adapters that know their
    # version can override via a per-connection profile.
    profile.features.parallel_tool_calls = False
    return profile


#: Default profiles for the three legacy built-ins. Populated lazily so
#: a test that clears the registry doesn't retain stale profiles.
_LEGACY_PROFILE_BUILDERS: Dict[str, "callable"] = {
    "openai_compat": _legacy_openai_profile,
    "openai": _legacy_openai_profile,     # alias
    "default": _legacy_openai_profile,    # alias
    "lmstudio": _legacy_lmstudio_profile,
    "lm-studio": _legacy_lmstudio_profile,
    "ollama": _legacy_ollama_profile,
}


def _default_profile_for(registration: ProviderRegistration) -> AdapterProfile:
    """
    Return the default ``AdapterProfile`` for a registration.

    For legacy adapters, uses the pre-declared conservative profile.
    For V1 adapters, the adapter itself MUST declare its profile as a
    class attribute (``adapter_class.PROFILE``). If it doesn't, we build
    the most minimal valid profile so the run still proceeds — but log
    a warning so the missing declaration is visible.
    """
    builder = _LEGACY_PROFILE_BUILDERS.get(registration.provider_id)
    if builder is not None:
        return builder()

    # V1 adapters should declare their profile explicitly.
    declared = getattr(registration.adapter_class, "PROFILE", None)
    if isinstance(declared, AdapterProfile):
        return declared

    logger.warning(
        "Adapter '%s' (class=%s) does not declare an AdapterProfile. "
        "Using a minimal default — capabilities will be reported as "
        "unknown. This may cause the RequestCompiler to skip optional "
        "parameters. Declare `PROFILE = AdapterProfile(...)` on the "
        "adapter class.",
        registration.provider_id,
        registration.class_path,
    )
    return AdapterProfile(
        provider_id=registration.provider_id,
        features=AdapterFeatures(buffered_streaming=True),
    )


# ---------------------------------------------------------------------------
# Legacy adapter bridge
# ---------------------------------------------------------------------------


class _LegacyAdapterBridge:
    """
    Adapts a legacy provider (``generate(messages, tools, model=...)``)
    to the ``ProviderAdapter`` protocol (``generate(request)``).

    This class is intentionally private. Callers of the factory never
    see it — they get a ``ProviderAdapter``-shaped object back.

    Behavior of each method:

    * ``generate`` — extracts generation kwargs from ``LLMRequest``,
      calls the legacy adapter with the legacy signature.

    * ``stream`` — iterates the legacy ``StreamChunk`` sequence and
      emits normalized ``LLMStreamEvent`` instances. The mapping is
      1:1 for content/reasoning/tool_call/usage/finish_reason.

    * ``discover_models`` — tries ``discover_models()``,
      ``fetch_installed_models()``, then returns ``[]``. Never raises.

    * ``validate_connection`` — tries ``check_health()``, then a
      generic HTTP probe against ``<base_url>/models``. Never raises.

    * ``get_model_metadata`` — always returns ``None``. Legacy
      adapters do not expose per-model metadata.

    * ``close`` — closes the legacy adapter's underlying client if
      one is reachable. Never raises.
    """

    def __init__(
        self,
        legacy_adapter: Any,
        profile: AdapterProfile,
        connection: ConnectionContext,
    ) -> None:
        self._legacy = legacy_adapter
        self.provider_id = profile.provider_id
        self.profile = profile
        self._connection = connection

    # ------------------------------------------------------------------
    # Request translation
    # ------------------------------------------------------------------

    def _build_legacy_kwargs(self, request: LLMRequest) -> Dict[str, Any]:
        """
        Translate an ``LLMRequest`` into the legacy adapter's kwargs.

        Only fields the legacy adapter understands are forwarded.
        Everything else is silently dropped — the legacy adapter would
        reject unknown kwargs, so the bridge is deliberately strict.
        """
        kwargs: Dict[str, Any] = {}

        gen = request.generation
        if gen.temperature is not None:
            kwargs["temperature"] = float(gen.temperature)
        if gen.max_output_tokens is not None:
            kwargs["max_tokens"] = int(gen.max_output_tokens)
        if gen.top_p is not None:
            kwargs["top_p"] = float(gen.top_p)
        if gen.stop:
            kwargs["stop"] = list(gen.stop)
        if gen.frequency_penalty is not None:
            kwargs["frequency_penalty"] = float(gen.frequency_penalty)
        if gen.presence_penalty is not None:
            kwargs["presence_penalty"] = float(gen.presence_penalty)

        # Reasoning: only forward when the user actually asked for it.
        # This preserves the existing behavior in dispatcher/executor
        # where reasoning_effort is only sent when configured.
        if request.reasoning.enabled and request.reasoning.effort:
            kwargs["reasoning_effort"] = str(request.reasoning.effort)

        # Response format: only forward when not the default.
        if request.response_format.kind == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        elif request.response_format.kind == "json_schema" and request.response_format.schema_:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_format.name or "response",
                    "schema": request.response_format.schema_,
                    "strict": bool(request.response_format.strict),
                },
            }

        return kwargs

    # ------------------------------------------------------------------
    # ProviderAdapter protocol implementation
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Execute a non-streaming completion via the legacy adapter."""
        kwargs = self._build_legacy_kwargs(request)
        return await self._legacy.generate(
            messages=request.messages,
            tools=request.tools or None,
            tool_choice=request.tool_choice.mode or "auto",
            model=request.model_id,
            **kwargs,
        )

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Execute a streaming completion via the legacy adapter.

        Converts the legacy ``StreamChunk`` sequence into normalized
        ``LLMStreamEvent`` instances. Yields incrementally — the caller
        (StreamNormalizer in M6) receives events as they arrive from
        the underlying provider.

        Terminal-event timing
        ---------------------
        OpenAI's streaming protocol puts ``finish_reason`` on the LAST
        content chunk, but sends the actual usage in a SEPARATE
        usage-only chunk that arrives AFTER it. If we emitted
        ``MESSAGE_COMPLETE`` on every chunk that carries a
        ``finish_reason``, the normalizer would return immediately and
        the subsequent ``USAGE_UPDATE`` would be lost.

        To prevent that, we accumulate the ``finish_reason`` and emit
        ``MESSAGE_COMPLETE`` exactly once at the end of iteration,
        AFTER the underlying stream is fully drained.
        """
        kwargs = self._build_legacy_kwargs(request)
        seq = 0
        pending_finish_reason: Optional[str] = None

        async for chunk in self._legacy.stream(
            messages=request.messages,
            tools=request.tools or None,
            tool_choice=request.tool_choice.mode or "auto",
            model=request.model_id,
            **kwargs,
        ):
            # Content delta
            if chunk.content_delta:
                seq += 1
                yield LLMStreamEvent(
                    type=StreamEventType.CONTENT_DELTA,
                    content_delta=chunk.content_delta,
                    sequence=seq,
                )

            # Reasoning delta
            if chunk.reasoning_delta:
                seq += 1
                yield LLMStreamEvent(
                    type=StreamEventType.REASONING_DELTA,
                    reasoning_delta=chunk.reasoning_delta,
                    sequence=seq,
                )

            # Tool call deltas — each is its own event
            for tc in chunk.tool_call_deltas or []:
                seq += 1
                yield LLMStreamEvent(
                    type=StreamEventType.TOOL_CALL_DELTA,
                    tool_call_id=getattr(tc, "id", None),
                    tool_name=getattr(tc, "name", None),
                    tool_index=getattr(tc, "index", None),
                    tool_arguments_delta=getattr(tc, "arguments", None),
                    sequence=seq,
                )

            # Usage — surface immediately so the normalizer can stash it.
            if chunk.usage:
                seq += 1
                yield LLMStreamEvent(
                    type=StreamEventType.USAGE_UPDATE,
                    usage=chunk.usage,
                    sequence=seq,
                )

            # Finish reason — captured but NOT emitted yet. See docstring.
            if chunk.finish_reason:
                pending_finish_reason = chunk.finish_reason

        # Terminal event — exactly once, after the stream is drained.
        seq += 1
        yield LLMStreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=pending_finish_reason or "stop",
            sequence=seq,
        )

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        """
        Discover models available on this connection.

        Tries the legacy adapter's own discovery method first
        (``discover_models`` or ``fetch_installed_models``), then falls
        back to a generic ``<base_url>/models`` probe. Never raises —
        the plan requires discovery to be optional.
        """
        # Try adapter's own discover_models() — LM Studio path
        discover = getattr(self._legacy, "discover_models", None)
        if callable(discover):
            try:
                result = discover()
                if asyncio.iscoroutine(result):
                    result = await result
                return _normalize_model_list(result)
            except Exception as exc:
                logger.debug(
                    "[%s] discover_models() raised: %s",
                    self.provider_id,
                    exc,
                )

        # Try fetch_installed_models() — Ollama path
        fetch = getattr(self._legacy, "fetch_installed_models", None)
        if callable(fetch):
            try:
                result = fetch()
                if asyncio.iscoroutine(result):
                    result = await result
                return [{"id": str(m)} for m in (result or [])]
            except Exception as exc:
                logger.debug(
                    "[%s] fetch_installed_models() raised: %s",
                    self.provider_id,
                    exc,
                )

        # Fallback: generic HTTP probe. Deliberately best-effort — a
        # provider whose discovery is broken remains usable via manual
        # model entry.
        try:
            return await self._probe_models_endpoint(
                connection or self._connection
            )
        except Exception as exc:
            logger.debug(
                "[%s] generic model probe raised: %s",
                self.provider_id,
                exc,
            )
            return []

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        """
        Validate the connection without sending a generation request.

        Prefers the adapter's own ``check_health()``. Falls back to a
        generic ``<base_url>/models`` probe. Never raises.
        """
        check_health = getattr(self._legacy, "check_health", None)
        if callable(check_health):
            try:
                start = time.time()
                result = check_health()
                if asyncio.iscoroutine(result):
                    result = await result
                latency_ms = round((time.time() - start) * 1000, 2)
                return {
                    "ok": bool(result),
                    "message": "Connection healthy" if result else "Connection unhealthy",
                    "latency_ms": latency_ms,
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "message": f"Health check failed: {exc}",
                    "latency_ms": 0.0,
                }

        # Fallback: probe /models
        effective_conn = connection or self._connection
        return await self._probe_connection(effective_conn)

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Legacy adapters do not expose per-model metadata.

        The vault (``models_metadata.json``) is populated via discovery
        instead; the factory does not duplicate that here.
        """
        return None

    async def close(self) -> None:
        """Close the legacy adapter's underlying HTTP client, if any."""
        client = getattr(self._legacy, "client", None)
        if client is None:
            return
        close_method = getattr(client, "close", None)
        if close_method is None:
            return
        try:
            result = close_method()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.debug(
                "[%s] Failed to close legacy client: %s",
                self.provider_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Fallback probes (never raise)
    # ------------------------------------------------------------------

    async def _probe_models_endpoint(
        self, connection: ConnectionContext
    ) -> List[Dict[str, Any]]:
        """
        Generic ``<base_url>/models`` probe.

        Best-effort: returns ``[]`` on any error. Used only when the
        adapter has no own discovery method.
        """
        try:
            import httpx
        except ImportError:
            return []

        base_url = (connection.base_url or "").rstrip("/")
        if not base_url:
            return []

        url = base_url if base_url.endswith("/models") else f"{base_url}/models"
        headers = {"Accept": "application/json"}
        if connection.api_key:
            headers["Authorization"] = f"Bearer {connection.api_key}"

        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return []
                data = resp.json()
                raw = data.get("data") or data.get("models") or []
                return _normalize_model_list(raw)
        except Exception:
            return []

    async def _probe_connection(
        self, connection: ConnectionContext
    ) -> Dict[str, Any]:
        """Generic connectivity probe — never raises."""
        try:
            import httpx
        except ImportError:
            return {
                "ok": False,
                "message": "httpx not available for fallback probe",
                "latency_ms": 0.0,
            }

        base_url = (connection.base_url or "").rstrip("/")
        if not base_url:
            return {
                "ok": False,
                "message": "No base_url configured",
                "latency_ms": 0.0,
            }

        url = base_url if base_url.endswith("/models") else f"{base_url}/models"
        headers = {"Accept": "application/json"}
        if connection.api_key:
            headers["Authorization"] = f"Bearer {connection.api_key}"

        try:
            start = time.time()
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url, headers=headers)
                latency_ms = round((time.time() - start) * 1000, 2)
                return {
                    "ok": resp.status_code in (200, 201),
                    "message": f"HTTP {resp.status_code}",
                    "latency_ms": latency_ms,
                }
        except Exception as exc:
            return {
                "ok": False,
                "message": str(exc),
                "latency_ms": 0.0,
            }


# ---------------------------------------------------------------------------
# Normalization helper
# ---------------------------------------------------------------------------


def _normalize_model_list(raw: Any) -> List[Dict[str, Any]]:
    """
    Normalize the heterogeneous model list formats returned by legacy
    adapters and generic probes.

    Accepts:
        * ``List[str]`` — returned by ``fetch_installed_models()``.
        * ``List[Dict]`` with ``id`` / ``name`` / ``key`` — returned by
          ``discover_models()`` and generic ``/models`` probes.
        * Anything else → ``[]``.

    Each output item has at minimum ``{"id": str}``.
    """
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                result.append({"id": item.strip()})
        elif isinstance(item, dict):
            model_id = (
                item.get("id")
                or item.get("name")
                or item.get("key")
            )
            if not model_id:
                continue
            entry: Dict[str, Any] = {"id": str(model_id)}
            # Preserve any additional metadata the adapter provided so
            # the metadata vault can store it verbatim.
            for key in (
                "display_name",
                "max_context_length",
                "max_output_tokens",
                "context_length",
                "type",
                "loaded_instances",
                "quantization",
                "publisher",
                "architecture",
                "format",
            ):
                if key in item:
                    entry[key] = item[key]
            result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class ProviderFactory:
    """
    Create ``ProviderAdapter``-conforming objects from the registry.

    Stateless by design: no caching, no instance tracking, no shutdown
    hooks. Callers own the adapters they create and are responsible for
    closing them (via ``await adapter.close()``) when done.

    Thread-safe: the factory itself holds no mutable state, so
    concurrent ``create()`` calls are safe.
    """

    def __init__(
        self, registry: Optional[ProviderRegistry] = None
    ) -> None:
        self._registry = registry or provider_registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        provider_id: str,
        connection: ConnectionContext,
        *,
        profile: Optional[AdapterProfile] = None,
    ) -> ProviderAdapter:
        """
        Create a ``ProviderAdapter``-conforming object for the given
        provider and connection.

        Parameters
        ----------
        provider_id
            Canonical identifier or alias. Looked up in the registry.
            Raises ``ProviderNotRegisteredError`` if unknown, or
            ``ProviderDisabledError`` if disabled.
        connection
            Per-connection credentials and endpoint configuration. Must
            have a matching ``provider_id`` when set (a mismatch is
            treated as a caller bug and raises ``ValueError``).
        profile
            Optional explicit ``AdapterProfile``. When provided, it is
            used verbatim — this is how M5 native adapters and M7
            legacy adapters coexist during migration. When omitted,
            the factory derives a default from the adapter class
            (V1: ``adapter_class.PROFILE``; legacy: pre-declared).

        Returns
        -------
        ProviderAdapter
            Always. Legacy adapters are wrapped in an internal bridge
            so the caller sees a uniform interface.

        Raises
        ------
        ProviderNotRegisteredError
            When the provider_id is unknown.
        ProviderDisabledError
            When the provider is registered but disabled.
        ValueError
            When ``connection.provider_id`` does not match the
            requested ``provider_id`` (defensive check against caller
            confusion between two providers).
        """
        registration = self._registry.get(provider_id)

        # Defensive: reject connections whose provider_id disagrees with
        # the requested provider. A mismatch is a programming error at
        # the call site, not something to silently paper over.
        conn_provider = (connection.provider_id or "").strip().lower()
        if conn_provider and conn_provider != registration.provider_id:
            # Allow alias form (e.g. connection says "lm-studio" while
            # registration is "lmstudio").
            alias_match = self._registry.find(conn_provider)
            if alias_match is None or alias_match.provider_id != registration.provider_id:
                raise ValueError(
                    f"Provider mismatch: factory asked for "
                    f"'{registration.provider_id}', but the connection "
                    f"belongs to '{connection.provider_id}'. "
                    f"Pass a matching ConnectionContext."
                )

        effective_profile = profile or _default_profile_for(registration)
        style = _detect_adapter_style(registration.adapter_class)

        if style == AdapterStyle.V1:
            adapter = self._instantiate_v1(
                registration, connection, effective_profile
            )
        else:
            adapter = self._instantiate_legacy(
                registration, connection, effective_profile
            )

        logger.debug(
            "Created provider adapter: id=%s style=%s class=%s "
            "connection_id=%s",
            registration.provider_id,
            style.value,
            registration.class_path,
            connection.connection_id,
        )
        return adapter

    def create_from_config(
        self,
        provider_id: str,
        config: LLMConfig,
        **constructor_kwargs: Any,
    ) -> Any:
        """
        Transitional helper for M7.

        Instantiate the raw adapter class with the legacy ``config=``
        signature. Used ONLY by callers that have not yet migrated to
        ``create()`` — this method exists so M7 can migrate
        ``executor.py`` and ``dispatcher.py`` one at a time without
        breaking either.

        Returns the raw adapter (NOT wrapped in ``_LegacyAdapterBridge``)
        so callers that still use ``messages/tools`` signatures keep
        working byte-for-byte.

        Raises
        ------
        TypeError
            When the provider uses the V1 protocol (callers should use
            ``create()`` instead).
        """
        registration = self._registry.get(provider_id)
        style = _detect_adapter_style(registration.adapter_class)
        if style == AdapterStyle.V1:
            raise TypeError(
                f"Provider '{provider_id}' uses the V1 adapter protocol. "
                f"Call create() with an LLMRequest-compatible "
                f"connection instead of create_from_config()."
            )
        return registration.adapter_class(config=config, **constructor_kwargs)

    # ------------------------------------------------------------------
    # Internal instantiation
    # ------------------------------------------------------------------

    def _instantiate_v1(
        self,
        registration: ProviderRegistration,
        connection: ConnectionContext,
        profile: AdapterProfile,
    ) -> ProviderAdapter:
        """
        Instantiate a V1-protocol adapter.

        Tries the ``ProviderContext`` signature first (matches the
        plan's example in §10.5). Falls back to explicit
        ``connection=`` / ``profile=`` kwargs for adapters that prefer
        that style. Both are valid; the plan does not mandate a
        specific form.
        """
        context = ProviderContext.build(connection, profile)
        adapter_class = registration.adapter_class

        try:
            return adapter_class(context=context)
        except TypeError as first_error:
            try:
                return adapter_class(
                    connection=connection, profile=profile
                )
            except TypeError:
                raise TypeError(
                    f"Adapter '{registration.provider_id}' "
                    f"(class={registration.class_path}) declares "
                    f"ADAPTER_STYLE='v1' but does not accept a supported "
                    f"constructor signature. Expected either "
                    f"`__init__(self, context)` or "
                    f"`__init__(self, *, connection, profile)`. "
                    f"Original error: {first_error}"
                ) from first_error

    def _instantiate_legacy(
        self,
        registration: ProviderRegistration,
        connection: ConnectionContext,
        profile: AdapterProfile,
    ) -> ProviderAdapter:
        """
        Instantiate a legacy adapter and wrap it in ``_LegacyAdapterBridge``.

        The legacy constructor takes ``config: LLMConfig``. We build the
        config from the connection — this preserves every field the
        legacy adapter reads, including ``extra_headers`` and
        ``extra_params`` (which the current built-ins pass through).
        """
        llm_config = self._connection_to_llm_config(connection)

        try:
            legacy = registration.adapter_class(config=llm_config)
        except TypeError as exc:
            raise TypeError(
                f"Legacy adapter '{registration.provider_id}' "
                f"(class={registration.class_path}) could not be "
                f"instantiated with `config=` kwarg. "
                f"Original error: {exc}"
            ) from exc

        return _LegacyAdapterBridge(
            legacy_adapter=legacy,
            profile=profile,
            connection=connection,
        )

    @staticmethod
    def _connection_to_llm_config(
        connection: ConnectionContext,
    ) -> LLMConfig:
        """
        Translate a ``ConnectionContext`` into a legacy ``LLMConfig``.

        Every field the legacy adapters read is preserved:

        * ``base_url`` / ``api_key`` / ``timeout`` — direct mapping.
        * ``provider`` — the connection's provider id.
        * ``extra_headers`` / ``extra_params`` — copied verbatim so
          OpenRouter's ``HTTP-Referer`` and similar per-connection
          headers keep working.
        * ``model`` — left as ``"default"``. The legacy ``generate`` /
          ``stream`` methods accept a per-call ``model=`` kwarg, which
          the bridge always passes from ``request.model_id``. Setting a
          model here would be overridden anyway.
        """
        return LLMConfig(
            model="default",
            base_url=connection.base_url,
            api_key=connection.api_key,
            provider=connection.provider_id,
            timeout=float(connection.timeout or 120.0),
            extra_headers=dict(connection.extra_headers or {}),
            extra_params=dict(connection.extra_params or {}),
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


#: Global factory instance. Stateless — safe to use across threads.
provider_factory = ProviderFactory()


def get_provider_factory() -> ProviderFactory:
    """Return the global provider factory (module-level singleton)."""
    return provider_factory


__all__ = [
    "AdapterStyle",
    "ProviderContext",
    "ProviderFactory",
    "provider_factory",
    "get_provider_factory",
]
