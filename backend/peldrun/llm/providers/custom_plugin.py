# backend/peldrun/llm/providers/custom_plugin.py
"""
PELDRUN Provider Package adapter bridge.

Bridges externally-installed Provider Packages (M9) into the
``ProviderAdapter`` Protocol that the runtime expects. Two very
different execution models share a single class:

    * **native / custom** transports — the adapter runs in an
      isolated subprocess managed by ``WorkerSupervisor``. Every
      call is an IPC round-trip; every exception is serialized and
      rehydrated. This is the "real" plugin execution path.

    * **compatible** transports — the package ships NO code. It is
      a manifest-only descriptor that reuses a built-in adapter
      (``openai_compat``, ``anthropic``, ``ollama_native``, ...).
      Every call is a direct delegation to the built-in adapter
      instance, in-process, with no IPC. This is the "cheap" path
      for the dozens of OpenAI-compatible providers that will never
      need their own Python code.

The class presents a single ``ProviderAdapter`` surface to the
caller regardless of which path was taken. ``dispatcher.py`` and
``executor.py`` cannot tell the difference.

How the descriptor reaches the adapter
--------------------------------------

The runtime factory (``ProviderFactory.create``) does NOT know about
provider packages — it only sees ``(provider_id, connection)``. That
is by design; making the factory package-aware would couple the core
to the admin layer.

Instead, the connection's ``extra_params`` carries a ``_peldrun_plugin``
key that the OMWeb connection resolver populates from
``provider_package_manager.get_active_adapter_descriptor(provider_id)``
before calling the factory. This module reads that key and constructs
the correct adapter shape.

Missing descriptor → clear error, not silent fallback. A plugin
provider that reaches the factory without the descriptor is a
programming bug in the connection resolver, and quietly returning a
non-working adapter would make it hard to diagnose.

Lazy worker startup
-------------------

The worker subprocess is spawned on the FIRST request, not in
``__init__`` (which is synchronous — Python does not allow async work
in ``__init__``). This means:

    * A plugin provider that is registered but never used spawns no
      subprocess — the backend stays lean.
    * The first request pays the spawn cost (typically 1-3 seconds
      for a cold Python import). Subsequent requests reuse the same
      worker.
    * If the caller does not ``close()`` the adapter, the worker
      survives until the parent process exits. The supervisor's
      shutdown is best-effort — the OS reaps orphaned children when
      the parent dies.

Callers SHOULD close the adapter when the run ends. For agent mode
this happens naturally (``executor.py`` creates one adapter per run
and closes it in a ``finally``). For direct chat, the same pattern
applies.

See also
--------

    * ``backend/peldrun/llm/plugin_host/supervisor.py`` — the worker
      manager used by native/custom transports
    * ``backend/peldrun/llm/providers/factory.py`` — creates this
      adapter from a registration
    * ``backend/peldrun/llm/contracts.py`` — the ``ProviderAdapter``
      Protocol
    * ``backend/omweb/provider_packages/package_manager.py`` —
      ``get_active_adapter_descriptor`` (the descriptor source)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from peldrun.llm.client import LLMResponse, TokenUsage, ToolCall
from peldrun.llm.contracts import (
    AdapterFeatures,
    AdapterProfile,
    ConnectionContext,
    LLMStreamEvent,
    ReasoningStrategy,
    ToolCallStyle,
)
from peldrun.llm.errors import ProviderError
from peldrun.llm.plugin_host.supervisor import (
    WorkerCrashedError,
    WorkerStartupError,
    WorkerSupervisor,
)
from peldrun.llm.request import LLMRequest


logger = logging.getLogger("peldrun.llm.providers.custom_plugin")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The key under ``ConnectionContext.extra_params`` where the OMWeb
#: connection resolver MUST place the ``AdapterDescriptor`` data.
#:
#: This constant is the ONLY contract between this module and the
#: connection resolver. If you rename it, both sides must change.
#: The value is a public symbol so the OMWeb side imports it from
#: here rather than duplicating the literal.
PLUGIN_DESCRIPTOR_KEY: str = "_peldrun_plugin"


#: Default adapter profile used when the registration does not carry
#: one. The factory requires ``adapter_class.PROFILE`` to be an
#: ``AdapterProfile`` instance (see ``factory._default_profile_for``);
#: this placeholder satisfies that requirement. The REAL profile is
#: set in ``__init__`` from the descriptor.
_PLACEHOLDER_PROFILE = AdapterProfile(
    provider_id="plugin_placeholder",
    adapter_version="0.0.0",
    api_surface="chat_completions",
    features=AdapterFeatures(),
    reasoning_strategies=[],
    tool_call_style=ToolCallStyle.OPENAI_FUNCTION,
    supported_param_names=[],
)


# ---------------------------------------------------------------------------
# Descriptor parsing
# ---------------------------------------------------------------------------


def _build_adapter_profile_from_descriptor(
    descriptor: Dict[str, Any],
) -> AdapterProfile:
    """
    Convert the ``features`` dict from an ``AdapterDescriptor`` into a
    complete ``AdapterProfile``.

    The descriptor only carries the boolean feature flags (matching
    ``omweb.provider_packages.schema.PackageFeatureFlags``) — it does
    NOT carry ``reasoning_strategies`` or ``tool_call_style`` because
    those are runtime concepts the manifest does not model.

    We infer sensible defaults:

        * ``reasoning_strategies`` — if ``features.reasoning`` is
          true, we assume ``OPENAI_COMPATIBLE`` (works for the vast
          majority of providers that expose ``reasoning_effort``).
          Adapters whose reasoning surface differs MUST override
          ``self.profile`` in their own ``__init__`` — this is
          documented in the manifest's contribution guide.
        * ``tool_call_style`` — always ``OPENAI_FUNCTION``, because
          that is the shape the ``RequestCompiler`` produces. An
          adapter that speaks a different wire format (e.g.
          Anthropic-compatible) overrides this via its own
          ``__init__``.

    The profile built here is a BEST EFFORT — a well-behaved adapter
    replaces it wholesale in its ``__init__`` after receiving
    ``context.profile``.
    """
    raw_features = descriptor.get("features") or {}
    features_obj = AdapterFeatures(
        model_discovery=bool(raw_features.get("model_discovery", False)),
        native_streaming=bool(raw_features.get("native_streaming", False)),
        buffered_streaming=bool(raw_features.get("buffered_streaming", True)),
        tool_calling=bool(raw_features.get("tool_calling", False)),
        parallel_tool_calls=bool(
            raw_features.get("parallel_tool_calls", False)
        ),
        forced_tool_choice=bool(
            raw_features.get("forced_tool_choice", False)
        ),
        vision=bool(raw_features.get("vision", False)),
        audio=bool(raw_features.get("audio", False)),
        structured_output=bool(
            raw_features.get("structured_output", False)
        ),
        usage_reporting=bool(raw_features.get("usage_reporting", False)),
        reasoning=bool(raw_features.get("reasoning", False)),
        prompt_caching=bool(raw_features.get("prompt_caching", False)),
    )

    reasoning_strategies: List[ReasoningStrategy] = []
    if features_obj.reasoning:
        reasoning_strategies.append(ReasoningStrategy.OPENAI_COMPATIBLE)

    return AdapterProfile(
        provider_id=str(descriptor.get("provider_id") or "plugin"),
        adapter_version=str(descriptor.get("version") or "1.0.0"),
        api_surface="chat_completions",
        features=features_obj,
        reasoning_strategies=reasoning_strategies,
        tool_call_style=ToolCallStyle.OPENAI_FUNCTION,
        supported_param_names=[],
        notes=(
            "Auto-generated profile for a Provider Package. "
            "Override this in your adapter's __init__ if the wire "
            "format differs from the defaults."
        ),
    )


def _extract_descriptor(
    connection: ConnectionContext,
) -> Optional[Dict[str, Any]]:
    """
    Return the plugin descriptor from ``connection.extra_params``, or
    ``None`` when absent.

    The descriptor is a plain dict so the payload crosses the
    ``ConnectionContext`` model boundary without a new dependency.
    Required keys:

        provider_id   str       canonical package id
        version       str       installed version (for diagnostics)
        transport     str       "native" | "custom" | "compatible"
        base_adapter  str|None  builtin to reuse when compatible
        entry_point   str|None  module:class for native/custom
        package_dir   str       extracted package root (native/custom)
        venv_python   str       venv interpreter (native/custom)
        features      dict      boolean feature flags
        permissions   list[str] flat permission strings

    Missing required keys for the active transport cause a clear error
    in ``__init__`` — we do not attempt to work around a malformed
    descriptor.
    """
    if not isinstance(connection.extra_params, dict):
        return None
    raw = connection.extra_params.get(PLUGIN_DESCRIPTOR_KEY)
    if not isinstance(raw, dict):
        return None
    return raw


# ---------------------------------------------------------------------------
# Response rehydration
# ---------------------------------------------------------------------------


def _rehydrate_tool_call(data: Any) -> ToolCall:
    """
    Rebuild a ``ToolCall`` from its JSON-safe dict form.

    The supervisor's ``_serialize_llm_response`` flattened every
    ToolCall field to a primitive; this reverses that flattening.
    Missing optional fields default to safe values so a malformed
    payload does not raise.
    """
    if not isinstance(data, dict):
        return ToolCall(id="", name="", arguments={})
    return ToolCall(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or ""),
        arguments=data.get("arguments") or {},
        type=str(data.get("type") or "function"),
    )


def _rehydrate_token_usage(data: Any) -> Optional[TokenUsage]:
    """
    Rebuild ``TokenUsage`` from its JSON-safe dict form.

    Returns ``None`` when the payload is missing or not a dict — this
    matches the in-process adapters that return ``None`` when the
    provider reports no usage.
    """
    if not isinstance(data, dict):
        return None
    try:
        return TokenUsage.model_validate(data)
    except Exception as exc:
        logger.debug(
            "Malformed TokenUsage payload (%s); using None.", exc
        )
        return None


def _rehydrate_llm_response(data: Dict[str, Any]) -> LLMResponse:
    """
    Rebuild an ``LLMResponse`` dataclass from the supervisor's
    serialized dict.

    The supervisor deliberately drops ``LLMResponse.raw`` (the SDK
    response) — it is not JSON-serializable and is only useful for
    in-process debugging. Every other field is rehydrated verbatim.
    """
    tool_calls: List[ToolCall] = [
        _rehydrate_tool_call(tc)
        for tc in (data.get("tool_calls") or [])
        if isinstance(tc, dict)
    ]

    usage = _rehydrate_token_usage(data.get("usage"))

    return LLMResponse(
        content=data.get("content"),
        reasoning=data.get("reasoning"),
        reasoning_content=data.get("reasoning_content"),
        thought=data.get("thought"),
        tool_calls=tool_calls,
        finish_reason=data.get("finish_reason"),
        usage=usage,
        model=data.get("model"),
        raw=None,  # deliberately not transmitted
        latency_ms=data.get("latency_ms"),
        ttft_ms=data.get("ttft_ms"),
    )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class PluginProviderAdapter:
    """
    Adapter that surfaces a Provider Package as a ``ProviderAdapter``.

    Instances are created by ``ProviderFactory.create`` via the
    standard V1 constructor signature:

        adapter_class(context=ProviderContext(...))

    The factory does not know it is creating a plugin adapter — the
    registration in ``provider_registry`` maps the package's ID to
    this class, and the factory treats it like any other V1 adapter.
    The plugin-specific data (descriptor) travels inside the
    ``ConnectionContext``.
    """

    #: V1 protocol marker — required by the factory's style detector.
    ADAPTER_STYLE = "v1"

    #: API contract version — required by the registry.
    API_CONTRACT = "peldrun.llm.provider.v1"

    #: Placeholder profile — the real one is set in ``__init__``.
    #: The factory reads this BEFORE calling ``__init__``; a valid
    #: ``AdapterProfile`` instance is required so the factory does not
    #: log a warning about the missing declaration.
    PROFILE = _PLACEHOLDER_PROFILE

    #: Provider identifier — replaced per-instance in ``__init__``.
    provider_id = "plugin"

    def __init__(
        self,
        context: Any = None,
        *,
        connection: Optional[ConnectionContext] = None,
        profile: Optional[AdapterProfile] = None,
    ) -> None:
        # --------------------------------------------------------------
        # 1. Extract connection + profile from either the context or
        #    the explicit kwargs (both forms are supported by the
        #    factory depending on the adapter's signature).
        # --------------------------------------------------------------
        if context is not None:
            self._connection: ConnectionContext = context.connection
            initial_profile: AdapterProfile = context.profile or _PLACEHOLDER_PROFILE
        else:
            if connection is None:
                raise TypeError(
                    "PluginProviderAdapter requires either `context=` "
                    "or `connection=` (and optionally `profile=`)."
                )
            self._connection = connection
            initial_profile = profile or _PLACEHOLDER_PROFILE

        # --------------------------------------------------------------
        # 2. Extract the plugin descriptor. Its presence is the ONLY
        #    signal that this adapter should be a plugin provider.
        # --------------------------------------------------------------
        descriptor = _extract_descriptor(self._connection)
        if descriptor is None:
            raise ProviderError(
                "PluginProviderAdapter was instantiated without a "
                "plugin descriptor in "
                f"connection.extra_params['{PLUGIN_DESCRIPTOR_KEY}']. "
                "This adapter is only created by the provider factory "
                "for registered Provider Packages. If you are seeing "
                "this error, the OMWeb connection resolver did not "
                "populate the descriptor — check the provider "
                "resolution path in dispatcher.py / peldrun_engine.py.",
                provider=getattr(
                    self._connection, "provider_id", None
                ),
                details={
                    "provider_id": self._connection.provider_id,
                    "connection_id": self._connection.connection_id,
                    "expected_extra_params_key": PLUGIN_DESCRIPTOR_KEY,
                },
            )
        self._descriptor: Dict[str, Any] = dict(descriptor)

        # --------------------------------------------------------------
        # 3. Determine the transport and set up the execution path.
        # --------------------------------------------------------------
        self._transport: str = str(
            self._descriptor.get("transport") or "native"
        ).strip().lower()

        if self._transport not in ("native", "custom", "compatible"):
            raise ProviderError(
                f"Plugin provider declares unknown transport "
                f"'{self._transport}'. Supported: native, custom, "
                f"compatible.",
                provider=str(
                    self._descriptor.get("provider_id") or ""
                ),
                details={
                    "transport": self._transport,
                    "descriptor_keys": sorted(self._descriptor.keys()),
                },
            )

        # Per-instance profile — the real one, derived from the
        # descriptor's features. The adapter (in the worker or the
        # delegate) may override it further in its own __init__.
        self.profile = _build_adapter_profile_from_descriptor(
            self._descriptor
        )

        self.provider_id = str(
            self._descriptor.get("provider_id")
            or initial_profile.provider_id
            or "plugin"
        )

        # Common state.
        self._delegate: Optional[Any] = None
        self._supervisor: Optional[WorkerSupervisor] = None
        self._closed: bool = False

        # --------------------------------------------------------------
        # 4. Branch: compatible → delegate; native/custom → supervisor.
        # --------------------------------------------------------------
        if self._transport == "compatible":
            self._setup_compatible_delegate()
        else:
            # Supervisor creation is deferred to _ensure_started() —
            # spawning a subprocess in __init__ is impossible (sync)
            # and wasteful (a registered-but-unused adapter should
            # not consume a process).
            self._supervisor_config = self._extract_supervisor_config()

        logger.debug(
            "PluginProviderAdapter created: provider=%r transport=%r "
            "version=%r",
            self.provider_id,
            self._transport,
            self._descriptor.get("version"),
        )

    # ------------------------------------------------------------------
    # Compatible delegate setup
    # ------------------------------------------------------------------

    def _setup_compatible_delegate(self) -> None:
        """
        Resolve the ``base_adapter`` and instantiate the built-in
        adapter that this package reuses.

        The built-in adapter is created with the SAME connection so
        it sees the same ``base_url``, ``api_key``, and headers the
        user configured. From the built-in adapter's perspective, it
        was created by the factory directly — it has no idea it is
        serving a "compatible" package.
        """
        base_adapter_id = self._descriptor.get("base_adapter")
        if not isinstance(base_adapter_id, str) or not base_adapter_id:
            raise ProviderError(
                f"Compatible transport requires 'base_adapter' in the "
                f"descriptor. Got: {base_adapter_id!r}.",
                provider=self.provider_id,
                details={
                    "transport": "compatible",
                    "base_adapter": base_adapter_id,
                },
            )

        # Lazy import to avoid a circular dependency at module load:
        # factory.py imports custom_plugin indirectly via
        # __init__.py, which would be a cycle if we imported it here.
        from peldrun.llm.providers.factory import provider_factory

        # When the delegate is created, it must NOT see the plugin
        # descriptor in its own connection.extra_params — otherwise
        # it would try to instantiate ANOTHER plugin adapter for the
        # base_adapter_id (which is a builtin and should not go
        # through the plugin path).
        delegate_connection = ConnectionContext(
            **{
                **self._connection.model_dump(),
                "extra_params": {
                    k: v
                    for k, v in (self._connection.extra_params or {}).items()
                    if k != PLUGIN_DESCRIPTOR_KEY
                },
            }
        )

        try:
            self._delegate = provider_factory.create(
                base_adapter_id, delegate_connection
            )
        except Exception as exc:
            raise ProviderError(
                f"Could not instantiate base_adapter "
                f"'{base_adapter_id}' for compatible package "
                f"'{self.provider_id}': {exc}",
                provider=self.provider_id,
                details={
                    "base_adapter": base_adapter_id,
                    "exception_type": type(exc).__name__,
                },
            ) from exc

        # Inherit the delegate's profile — it knows its own wire
        # format better than our manifest-derived default.
        try:
            if isinstance(self._delegate.profile, AdapterProfile):
                self.profile = self._delegate.profile
        except Exception:
            pass

        logger.debug(
            "Compatible package %r delegating to builtin adapter %r.",
            self.provider_id,
            base_adapter_id,
        )

    # ------------------------------------------------------------------
    # Supervisor configuration
    # ------------------------------------------------------------------

    def _extract_supervisor_config(self) -> Dict[str, Any]:
        """
        Validate the descriptor fields needed to spawn a supervisor.

        All required fields must be present and correctly typed. A
        missing field is a clear error at construction time, not a
        confusing failure on the first request.
        """
        d = self._descriptor

        entry_point = d.get("entry_point")
        if not isinstance(entry_point, str) or ":" not in entry_point:
            raise ProviderError(
                f"Native/custom plugin must declare a valid "
                f"'entry_point' (module:ClassName). Got: "
                f"{entry_point!r}.",
                provider=self.provider_id,
                details={"entry_point": entry_point},
            )

        package_dir_raw = d.get("package_dir")
        if not package_dir_raw:
            raise ProviderError(
                "Native/custom plugin must declare 'package_dir' in "
                "the descriptor.",
                provider=self.provider_id,
                details={"package_dir": package_dir_raw},
            )

        venv_python_raw = d.get("venv_python")
        if not venv_python_raw:
            raise ProviderError(
                "Native/custom plugin must declare 'venv_python' in "
                "the descriptor.",
                provider=self.provider_id,
                details={"venv_python": venv_python_raw},
            )

        # The descriptor carries the manifest's permission BLOCK (a dict
        # with keys "network", "secrets", "workspace_access",
        # "shell_execute"), not a pre-flattened list of PermissionKind
        # strings. The IPC protocol expects the flat list, so we convert
        # here — this is the ONLY place where the two representations
        # meet.
        #
        # The mapping mirrors
        # omweb.provider_packages.schema.PackagePermissions.to_permission_set():
        #
        #   network == "connection_endpoint"  → "network.connect"
        #   "connection.api_key" in secrets   → "secrets.read:connection"
        #   workspace_access == True          → forbidden; rejected at
        #                                       manifest validation, never
        #                                       emitted here
        #   shell_execute == True             → forbidden; same
        #
        # Before this fix, the dict was silently dropped (isinstance
        # check failed), the worker received permissions=[], and every
        # network call failed with "did not declare the 'network.connect'
        # permission" — even though the manifest clearly declared it.
        permissions_raw = d.get("permissions")

        if isinstance(permissions_raw, dict):
            permissions_list: List[str] = []
            if permissions_raw.get("network") == "connection_endpoint":
                permissions_list.append("network.connect")
            secrets_declared = permissions_raw.get("secrets") or []
            if (
                isinstance(secrets_declared, list)
                and "connection.api_key" in secrets_declared
            ):
                permissions_list.append("secrets.read:connection")

            # ------------------------------------------------------------------
            # Implicit permissions.
            #
            # The manifest's `permissions` block only models *consent-worthy*
            # choices: whether the adapter can reach the network and read
            # its own credentials. These three capabilities are NOT optional
            # choices — they are fundamental to what every provider adapter
            # does. The runtime worker REQUIRES them for the corresponding
            # message handlers to run:
            #
            #   models.discover → handle_discover_models()
            #   llm.generate    → handle_generate()
            #   llm.stream      → handle_stream_start()
            #   usage.read      → accounting (future)
            #
            # Without adding them here, the worker rejects every discovery /
            # generate / stream request with
            #   "attempted to use permission 'models.discover', which was
            #    not declared in its manifest"
            # even though the package is fully configured and functional.
            #
            # Manifest authors do NOT need to declare these — doing so would
            # be friction with zero security benefit, since a package that
            # runs an LLM adapter by definition calls generate / stream /
            # discover. Only `network` and `secrets` need explicit consent.
            # ------------------------------------------------------------------
            permissions_list.extend(
                [
                    "models.discover",
                    "llm.generate",
                    "llm.stream",
                    "usage.read",
                ]
            )
        elif isinstance(permissions_raw, list):
            # Defensive: accept the flat-list form in case a future
            # descriptor producer uses it directly.
            permissions_list = [
                str(p) for p in permissions_raw if isinstance(p, str)
            ]
            # Same implicit grants as the dict branch above.
            for implicit_perm in (
                "models.discover",
                "llm.generate",
                "llm.stream",
                "usage.read",
            ):
                if implicit_perm not in permissions_list:
                    permissions_list.append(implicit_perm)
        else:
            permissions_list = [
                "models.discover",
                "llm.generate",
                "llm.stream",
                "usage.read",
            ]

        profile_dict: Dict[str, Any] = {
            "provider_id": self.provider_id,
            "adapter_version": str(d.get("version") or "1.0.0"),
            "api_surface": self.profile.api_surface,
            "features": self.profile.features.model_dump(),
            "reasoning_strategies": [
                s.value for s in self.profile.reasoning_strategies
            ],
            "tool_call_style": self.profile.tool_call_style.value,
            "supported_param_names": list(
                self.profile.supported_param_names or []
            ),
        }

        return {
            "provider_id": self.provider_id,
            "version": str(d.get("version") or "0.0.0"),
            "connection": self._connection,
            "venv_python": Path(str(venv_python_raw)),
            "package_dir": Path(str(package_dir_raw)),
            "entry_point": str(entry_point),
            "adapter_profile_dict": profile_dict,
            "permissions": permissions_list,
        }

    async def _ensure_started(self) -> None:
        """
        Lazily spawn the worker supervisor.

        Idempotent. Called by every method that needs the supervisor.
        No-op for compatible transports (they use a delegate, not a
        worker).
        """
        if self._closed:
            raise ProviderError(
                f"Adapter for provider '{self.provider_id}' has been "
                f"closed and cannot be reused. Create a fresh "
                f"adapter instance.",
                provider=self.provider_id,
            )
        if self._delegate is not None:
            return  # compatible transport — no worker to start.
        if self._supervisor is not None:
            return  # already started.

        config = self._supervisor_config
        supervisor = WorkerSupervisor(
            provider_id=config["provider_id"],
            version=config["version"],
            connection=config["connection"],
            venv_python=config["venv_python"],
            package_dir=config["package_dir"],
            entry_point=config["entry_point"],
            adapter_profile_dict=config["adapter_profile_dict"],
            permissions=config["permissions"],
        )
        await supervisor.start()
        self._supervisor = supervisor

    # ------------------------------------------------------------------
    # ProviderAdapter protocol
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Execute a non-streaming completion.

        For compatible transports: delegated to the built-in adapter
        in-process.

        For native/custom transports: sent over IPC; the worker's
        response is rehydrated into an ``LLMResponse``.
        """
        if self._delegate is not None:
            return await self._delegate.generate(request)

        await self._ensure_started()
        assert self._supervisor is not None

        try:
            response_dict = await self._supervisor.generate(request)
        except WorkerStartupError:
            # Startup failure means the worker never came up — mark
            # as closed so subsequent calls fail fast instead of
            # retrying endlessly.
            self._closed = True
            raise
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Unexpected error from plugin provider "
                f"'{self.provider_id}': {exc}",
                provider=self.provider_id,
                details={"exception_type": type(exc).__name__},
            ) from exc

        return _rehydrate_llm_response(response_dict)

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Execute a streaming completion.

        Both execution paths yield ``LLMStreamEvent`` instances —
        compatible transports yield them directly from the built-in
        adapter; native/custom transports receive them from the
        supervisor (which has already parsed them from the worker's
        serialized form).
        """
        if self._delegate is not None:
            async for event in self._delegate.stream(request):
                yield event
            return

        await self._ensure_started()
        assert self._supervisor is not None

        try:
            async for event in self._supervisor.stream(request):
                yield event
        except WorkerStartupError:
            self._closed = True
            raise
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Unexpected error streaming from plugin provider "
                f"'{self.provider_id}': {exc}",
                provider=self.provider_id,
                details={"exception_type": type(exc).__name__},
            ) from exc

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        """
        Discover available models.

        The ``connection`` argument is ignored — the adapter already
        holds the connection passed at construction. This matches the
        in-process adapter contract (they accept the arg for
        interface symmetry but use their own stored connection).
        """
        if self._delegate is not None:
            return await self._delegate.discover_models(connection)

        await self._ensure_started()
        assert self._supervisor is not None
        return await self._supervisor.discover_models()

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        """Probe the connection."""
        if self._delegate is not None:
            return await self._delegate.validate_connection(connection)

        await self._ensure_started()
        assert self._supervisor is not None
        return await self._supervisor.validate_connection()

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch per-model metadata, or ``None`` if unsupported."""
        if self._delegate is not None:
            return await self._delegate.get_model_metadata(model_id)

        await self._ensure_started()
        assert self._supervisor is not None
        return await self._supervisor.get_model_metadata(model_id)

    async def close(self) -> None:
        """
        Release all resources.

        Idempotent. Safe to call on an adapter that was never used
        (no supervisor was started; no delegate was created).

        For compatible transports: closes the built-in delegate.
        For native/custom transports: shuts down the worker
        supervisor (which escalates SIGTERM → SIGKILL if the worker
        does not exit gracefully).
        """
        if self._closed:
            return
        self._closed = True

        # Close the delegate first (compatible path).
        if self._delegate is not None:
            try:
                await self._delegate.close()
            except Exception as exc:
                logger.debug(
                    "Delegate close raised for provider %r: %s",
                    self.provider_id,
                    exc,
                )
            self._delegate = None

        # Then shut down the supervisor (native/custom path).
        if self._supervisor is not None:
            try:
                await self._supervisor.shutdown()
            except Exception as exc:
                logger.debug(
                    "Supervisor shutdown raised for provider %r: %s",
                    self.provider_id,
                    exc,
                )
            self._supervisor = None

    # ------------------------------------------------------------------
    # Context manager convenience
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PluginProviderAdapter":
        await self._ensure_started()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------


def register_plugin_provider(
    descriptor: Dict[str, Any],
    *,
    overwrite: bool = False,
) -> bool:
    """
    Register a single Provider Package in the global provider registry.

    Called from the backend lifespan (``omweb.main``) at startup,
    AFTER ``provider_package_manager`` has been initialized. For
    each enabled package, this function registers
    ``PluginProviderAdapter`` under the package's canonical ID, with
    the package's version and a ``SOURCE_EXTERNAL`` tag.

    Parameters
    ----------
    descriptor
        The plugin descriptor dict (same shape as
        ``connection.extra_params[PLUGIN_DESCRIPTOR_KEY]``). Must
        include ``provider_id`` and ``version`` at minimum.
    overwrite
        When True, replace any existing registration with the same
        ID. Used when a package is reinstalled or upgraded and the
        backend does not restart. The default (False) fails
        silently for already-registered packages — the caller
        typically iterates over ``list_packages()`` and calls this
        for each, so a repeat is a no-op by design.

    Returns
    -------
    bool
        True when a registration was created (or replaced);
        False when the package was already registered and
        ``overwrite=False``.

    Notes
    -----
    This function NEVER raises for expected failures (already
    registered, incompatible contract) — those are logged at DEBUG
    level. Only truly unexpected exceptions (import errors from the
    registry itself) propagate, and the caller (lifespan startup)
    wraps this in a try/except for defense in depth.
    """
    provider_id = descriptor.get("provider_id")
    version = str(descriptor.get("version") or "0.0.0")
    if not isinstance(provider_id, str) or not provider_id:
        logger.warning(
            "register_plugin_provider called with missing/empty "
            "provider_id: %r",
            descriptor,
        )
        return False

    try:
        from peldrun.llm.providers.registry import (
            SOURCE_EXTERNAL,
            provider_registry,
        )
    except Exception as exc:
        logger.error(
            "Could not import provider_registry to register plugin "
            "'%s': %s",
            provider_id,
            exc,
        )
        return False

    # Already registered?
    existing = provider_registry.find(provider_id)
    if existing is not None and not overwrite:
        logger.debug(
            "Plugin provider %r already registered (source=%s); "
            "skipping.",
            provider_id,
            existing.source,
        )
        return False

    try:
        provider_registry.register(
            provider_id=provider_id,
            adapter_class=PluginProviderAdapter,
            api_contract=PluginProviderAdapter.API_CONTRACT,
            source=SOURCE_EXTERNAL,
            display_name=str(
                descriptor.get("display_name") or provider_id
            ),
            version=version,
            overwrite=overwrite,
            metadata={
                "transport": descriptor.get("transport", "native"),
                "descriptor_version": version,
            },
        )
    except Exception as exc:
        logger.warning(
            "Could not register plugin provider %r: %s",
            provider_id,
            exc,
        )
        return False

    logger.info(
        "Registered plugin provider %r (version=%r, transport=%r).",
        provider_id,
        version,
        descriptor.get("transport", "native"),
    )
    return True


def ensure_plugin_providers_registered() -> int:
    """
    Register every enabled Provider Package currently installed.

    Called from the backend lifespan (``omweb.main``) once at
    startup. Also safe to call from tests and from a future
    "reload providers" admin endpoint.

    Import boundary
    ---------------
    This function does NOT import ``omweb.provider_packages`` at
    module level — it imports it inside the function so that
    ``peldrun.llm`` does not depend on ``omweb`` at import time.
    The import is deferred until the caller actually requests
    registration, and the caller (``main.py``) already has both
    packages on its import path.

    Returns
    -------
    int
        The number of packages newly registered (or refreshed) by
        this call. Already-registered packages with the same
        version are skipped and not counted.

    Never raises. A failure to import the package manager or to
    iterate the package list is logged and reported as 0 — startup
    must not fail because the provider package subsystem has a
    transient problem.
    """
    try:
        from omweb.provider_packages.package_manager import (
            provider_package_manager,
        )
    except Exception as exc:
        logger.warning(
            "Could not import provider_package_manager to register "
            "plugin providers: %s",
            exc,
        )
        return 0

    registered = 0
    try:
        packages = provider_package_manager.list_packages(
            enabled_only=True
        )
    except Exception as exc:
        logger.warning(
            "Could not list installed provider packages: %s", exc
        )
        return 0

    for pkg in packages:
        try:
            descriptor_obj = (
                provider_package_manager.get_active_adapter_descriptor(
                    pkg.id
                )
            )
            if descriptor_obj is None:
                logger.debug(
                    "Skipping plugin registration for %r — no active "
                    "descriptor (version missing or files removed).",
                    pkg.id,
                )
                continue

            descriptor_dict: Dict[str, Any] = {
                "provider_id": descriptor_obj.provider_id,
                "version": descriptor_obj.version,
                "transport": descriptor_obj.transport,
                "api_contract": descriptor_obj.api_contract,
                "entry_point": descriptor_obj.entry_point,
                "class_name": descriptor_obj.class_name,
                "base_adapter": descriptor_obj.base_adapter,
                "package_dir": str(descriptor_obj.package_dir),
                "venv_python": (
                    str(descriptor_obj.venv_python)
                    if descriptor_obj.venv_python is not None
                    else None
                ),
                "features": dict(descriptor_obj.features or {}),
                "permissions": dict(descriptor_obj.permissions or {}),
                "display_name": pkg.display_name or pkg.name or pkg.id,
            }

            if register_plugin_provider(descriptor_dict):
                registered += 1
        except Exception as exc:
            logger.warning(
                "Failed to register plugin provider %r: %s",
                pkg.id,
                exc,
            )

    if registered:
        logger.info(
            "Plugin provider registration: %d package(s) registered.",
            registered,
        )
    return registered


__all__ = [
    "PLUGIN_DESCRIPTOR_KEY",
    "PluginProviderAdapter",
    "register_plugin_provider",
    "ensure_plugin_providers_registered",
]
