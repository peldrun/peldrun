### `docs/providers/06-adapter-protocol.md`

```markdown
<!-- docs/providers/06-adapter-protocol.md -->
# Adapter Protocol

> **Complete reference** for the `ProviderAdapter` contract.
>
> **Reading time**: 20-25 minutes.
>
> **Who this is for**: Developers writing a `native` or `custom` adapter.
>
> **What you'll learn**: Every class attribute, every method, every return
> shape, every error type — the complete contract between PELDRUN and your
> code.

---

## Table of Contents

- [Overview](#overview)
- [The protocol contract](#the-protocol-contract)
- [Class attributes (mandatory)](#class-attributes-mandatory)
- [The constructor](#the-constructor)
- [Method: generate()](#method-generate)
- [Method: stream()](#method-stream)
- [Method: discover_models()](#method-discover_models)
- [Method: validate_connection()](#method-validate_connection)
- [Method: get_model_metadata()](#method-get_model_metadata)
- [Method: close()](#method-close)
- [LLMStreamEvent types](#llmstreamevent-types)
- [Error taxonomy](#error-taxonomy)
- [The ProviderContext object](#the-providercontext-object)
- [Cancellation handling](#cancellation-handling)
- [Complete adapter template](#complete-adapter-template)
- [Anti-patterns](#anti-patterns)
- [Checklist](#checklist)

---

## Overview

The `ProviderAdapter` protocol is the contract between PELDRUN and your
adapter code. If your class implements it, PELDRUN can route requests to your
provider — using the same dispatch path, accounting, telemetry, and error
handling as built-in adapters.

### Where the protocol lives

```python
# backend/peldrun/llm/contracts.py

@runtime_checkable
class ProviderAdapter(Protocol):
    """Structural contract for every provider adapter."""

    provider_id: str
    profile: AdapterProfile

    async def generate(self, request: LLMRequest) -> LLMResponse: ...
    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]: ...
    async def discover_models(self, connection=None) -> list[dict]: ...
    async def validate_connection(self, connection=None) -> dict: ...
    async def get_model_metadata(self, model_id: str) -> dict | None: ...
    async def close(self) -> None: ...
```

### Structural vs nominal typing

The protocol is `runtime_checkable`, but PELDRUN uses **duck typing** for
instantiation. Your class does not need to inherit from `ProviderAdapter`:

```python
# This works (and is the expected style)
class MyAdapter:
    ADAPTER_STYLE = "v1"
    API_CONTRACT = "peldrun.llm.provider.v1"
    PROFILE = ...
    provider_id = "my-provider"

    async def generate(self, request): ...
    # ... rest of methods

# This also works (but is unnecessary)
class MyAdapter(ProviderAdapter):
    ...
```

### Where your adapter runs

| Transport | Where the adapter runs |
|-----------|-----------------------|
| `compatible` | **Never.** No adapter is created. The runtime delegates to a built-in adapter. |
| `native` | **In a worker subprocess**, with an isolated venv. |
| `custom` | **In a worker subprocess**, with an isolated venv. |

This document focuses on `native` / `custom` adapters, which run inside the
worker.

---

## The protocol contract

Here is the complete contract as a table. Every row is a **hard requirement**
for `native` / `custom` adapters.

| Requirement | Type | Mandatory? | Failure mode |
|-------------|------|-----------|--------------|
| `ADAPTER_STYLE` class attribute | `string` | ✅ Yes | Factory treats as legacy, fails to instantiate |
| `API_CONTRACT` class attribute | `string` | ✅ Yes | Registry rejects at registration |
| `PROFILE` class attribute | `AdapterProfile` | ✅ Yes | Factory warns, capabilities become unknown |
| `provider_id` class attribute | `string` | ✅ Yes | Factory logs warnings |
| `__init__(self, context)` | method | ✅ Yes | Worker fails to load |
| `generate(self, request)` | async method | ✅ Yes | Request fails |
| `stream(self, request)` | async iterator | ✅ Yes | Streaming fails |
| `discover_models(self, connection)` | async method | ✅ Yes | Model scan returns empty |
| `validate_connection(self, connection)` | async method | ✅ Yes | Test button fails |
| `get_model_metadata(self, model_id)` | async method | ❌ No | Returns `None` |
| `close(self)` | async method | ✅ Yes | Socket leak |

---

## Class attributes (mandatory)

These four class-level attributes tell the factory and the registry how to
treat your adapter.

### `ADAPTER_STYLE`

**Type**: `string`
**Value**: `"v1"`
**Purpose**: Tells the factory your adapter uses the modern protocol, not the
legacy one.

**Why it exists**: Before M2, adapters used a different constructor signature
(`__init__(self, config: LLMConfig)`) and method signatures
(`generate(messages, tools, model=...)`). The factory needs to distinguish
between old and new adapters to wrap them correctly.

**What happens if missing**: The factory treats the adapter as legacy, tries
to call `__init__(self, config=LLMConfig(...))`, and fails with a confusing
`TypeError`. Always declare it.

```python
class MyAdapter:
    ADAPTER_STYLE = "v1"   # ← this line
```

### `API_CONTRACT`

**Type**: `string`
**Value**: `"peldrun.llm.provider.v1"`
**Purpose**: Tells the provider registry which PELDRUN adapter API your code
implements.

**What happens if missing**: The registry rejects your package with
`ProviderContractIncompatibleError`. The current PELDRUN build only supports
`v1`.

**What happens if wrong**: Same rejection. If you declare
`"peldrun.llm.provider.v2"` (a future API that does not exist yet), the
registry rejects it with a clear message.

```python
class MyAdapter:
    API_CONTRACT = "peldrun.llm.provider.v1"   # ← this line
```

### `PROFILE`

**Type**: `AdapterProfile`
**Purpose**: Declares the transport capabilities of your adapter — which
features it can carry over the wire.

**What the profile controls**:

- Which API surface your adapter talks to (`chat_completions`, `responses`,
  `messages`, `generate_content`, `ollama_native`, `lmstudio_native`)
- Which features it supports (streaming, tools, vision, reasoning, ...)
- Which reasoning strategies it can translate
- Which tool-call style it uses

**What happens if missing**: The factory logs a warning and uses a minimal
profile with all features disabled. Your adapter still loads, but
`RequestCompiler` will not include optional parameters. Always declare it.

**Example**:

```python
from peldrun.llm.contracts import (
    AdapterFeatures,
    AdapterProfile,
    ReasoningStrategy,
    ToolCallStyle,
)

_PROFILE = AdapterProfile(
    provider_id="my-provider",
    api_surface="chat_completions",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        tool_calling=True,
        vision=True,
        reasoning=True,
        usage_reporting=True,
    ),
    reasoning_strategies=[ReasoningStrategy.OPENAI_COMPATIBLE],
    tool_call_style=ToolCallStyle.OPENAI_FUNCTION,
)


class MyAdapter:
    PROFILE = _PROFILE   # ← this line
```

**Full `AdapterFeatures` field list**:

| Field | Default | Meaning |
|-------|---------|---------|
| `model_discovery` | `False` | Adapter implements `discover_models()` meaningfully |
| `native_streaming` | `False` | Adapter streams tokens in real time |
| `buffered_streaming` | `True` | Adapter returns full response, runtime simulates chunks |
| `tool_calling` | `False` | Adapter supports tools and tool calls |
| `parallel_tool_calls` | `False` | Model can emit multiple tool calls in one turn |
| `forced_tool_choice` | `False` | Adapter honors `tool_choice="required"` |
| `vision` | `False` | Adapter accepts `image_url` parts |
| `audio` | `False` | Adapter accepts audio input |
| `structured_output` | `False` | Adapter supports `response_format` |
| `usage_reporting` | `False` | Provider returns token counts |
| `reasoning` | `False` | Adapter supports reasoning parameters |
| `prompt_caching` | `False` | Adapter supports prompt caching |

### `provider_id`

**Type**: `string`
**Purpose**: The canonical identifier for your provider. Must match
`manifest.id`.

**What happens if wrong**: The factory logs warnings and may fail to route
correctly if the id does not match the manifest. Always match.

```python
class MyAdapter:
    provider_id = "my-provider"   # ← this line, must match manifest.id
```

---

## The constructor

Every adapter receives a single `context` argument:

```python
def __init__(self, context: ProviderContext) -> None:
    """Initialize the adapter."""
```

`ProviderContext` is not the same as `ConnectionContext`. It is a wrapper
that includes both the connection and some factory metadata.

### The `ProviderContext` object

```python
@dataclass
class ProviderContext:
    connection: ConnectionContext
    profile: AdapterProfile
    credentials: Dict[str, str]
```

| Field | Type | Contents |
|-------|------|----------|
| `connection` | `ConnectionContext` | The user's connection (base_url, api_key, extra_params) |
| `profile` | `AdapterProfile` | The adapter profile (usually your declared `PROFILE`) |
| `credentials` | `Dict[str, str]` | Convenience: `{"api_key": "..."}` if present |

### The `ConnectionContext` object

The `ConnectionContext.connection` field carries everything the adapter needs:

```python
class ConnectionContext(BaseModel):
    connection_id: str
    provider_id: str
    base_url: Optional[str]
    api_key: Optional[str]
    timeout: float
    extra_headers: Dict[str, str]
    extra_params: Dict[str, Any]
    metadata_endpoint: Optional[str]
    verify_ssl: bool
    workspace_access: bool
```

| Field | Meaning |
|-------|---------|
| `base_url` | The connection's endpoint URL |
| `api_key` | The connection's API key (may be None) |
| `timeout` | Per-request timeout in seconds |
| `extra_headers` | Additional headers (e.g., OpenRouter's HTTP-Referer) |
| `extra_params` | **All custom fields** from `connection_fields`, keyed by their `key` |
| `verify_ssl` | Whether to verify TLS (default True) |

### Reading connection values

```python
def __init__(self, context):
    self._connection = context.connection

    # Standard fields
    base_url = self._connection.base_url or "https://api.example.com/v1"
    api_key = self._connection.api_key
    timeout = float(self._connection.timeout or 60.0)

    # Remove the reserved plugin descriptor key
    extra = dict(self._connection.extra_params or {})
    extra.pop("_peldrun_plugin", None)

    # Custom fields (from connection_fields)
    scope = str(extra.get("scope") or "personal")
    region = str(extra.get("region") or "us-east-1")
    ca_bundle_path = extra.get("ca_bundle_path")

    # Construct the SDK client
    self._client = MySDKClient(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        scope=scope,
        region=region,
        ca_bundle=ca_bundle_path,
    )
```

### The `_peldrun_plugin` key

`extra_params` contains one key that your adapter **must remove** before using
the dict:

```python
extra = dict(self._connection.extra_params or {})
extra.pop("_peldrun_plugin", None)   # ← always remove this
```

The value is a `dict` containing the plugin descriptor (transport, entry_point,
etc.). It is a PELDRUN-internal value. If you pass it to your SDK, you will get
a JSON serialization error.

### Constructor failure

If your constructor raises, the worker emits `ADAPTER_LOAD_FAILED` and the
`LOAD_ADAPTER` request fails. The supervisor reports the failure to the caller
as a `ProviderError`.

**Common causes**:

| Cause | Fix |
|-------|-----|
| Missing required field (e.g., `credentials`) | Raise `ProviderAuthError` with clear message |
| SDK initialization fails | Catch and map to `ProviderError` |
| Invalid config value (e.g., bad region) | Raise `ProviderInvalidRequestError` |

**Always raise a `ProviderError` subclass** — never a raw exception:

```python
if not api_key:
    raise ProviderAuthError(
        "The 'api_key' field is required for MyProvider.",
        provider="my-provider",
    )
```

---

## Method: generate()

Execute a single non-streaming completion.

### Signature

```python
async def generate(self, request: LLMRequest) -> LLMResponse:
    ...
```

### What you receive

An `LLMRequest` — the fully-compiled request from `RequestCompiler`. All
capability intersection, parameter renaming, and reasoning translation have
already been done.

**The `LLMRequest` shape** (simplified):

```python
class LLMRequest(BaseModel):
    provider_id: str
    model_id: str
    connection_id: Optional[str]

    messages: List[Dict[str, Any]]
    system_prompt: Optional[str]
    tools: List[Dict[str, Any]]
    tool_choice: ToolChoiceSpec

    generation: GenerationConfig           # temperature, max_output_tokens, ...
    reasoning: ReasoningConfig             # enabled, mode, effort, budget_tokens
    response_format: ResponseFormat        # kind="text" | "json_object" | "json_schema"

    model_capabilities: ModelCapabilities
    connection: ConnectionContext
    provider_options: Dict[str, Any]
    metadata: Dict[str, Any]
```

**Recommended pattern**: Use `compile_request()` to transform it into a
`CompiledRequest`:

```python
from peldrun.llm.request_compiler import compile_request

async def generate(self, request):
    compiled = compile_request(request, self._profile)
    # compiled.generation: {"temperature": 0.7, "max_tokens": 100}
    # compiled.messages: normalized message list
    # compiled.tools: sanitized tool definitions (or None)
    # compiled.reasoning: strategy-specific reasoning dict (or None)
    ...
```

`CompiledRequest` saves you from manually handling:
- Capability intersection
- Parameter renaming (`max_output_tokens` → `max_tokens` for Chat Completions)
- Reasoning translation
- Tool sanitization

If you prefer to read `LLMRequest` directly, that is also valid — just
remember to check capabilities yourself.

### What you return

An `LLMResponse` — the canonical response shape:

```python
from peldrun.llm.client import LLMResponse, TokenUsage, ToolCall

return LLMResponse(
    content="Hello!",                          # str | None
    reasoning=None,                             # str | None (thinking)
    tool_calls=[],                              # list[ToolCall]
    finish_reason="stop",                       # "stop" | "length" | "tool_calls" | ...
    usage=TokenUsage(                           # Optional
        input_tokens=12,
        output_tokens=3,
        total_tokens=15,
        source=UsageSource.PROVIDER,
        estimated=False,
    ),
    model="claude-3-5-sonnet",                  # the actual model returned
    raw=raw_sdk_response,                       # for diagnostics (optional)
    latency_ms=1234.5,                          # optional, measured by you
    ttft_ms=100.0,                              # optional, time-to-first-token
)
```

**Field reference**:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `content` | `str` \| None | ✅ | The text response. Empty string is valid. |
| `reasoning` | `str` \| None | — | Thinking/reasoning text. If present, populated to `reasoning`, `reasoning_content`, and `thought` (all three). |
| `tool_calls` | `List[ToolCall]` | — | One entry per tool the model wants to invoke. Empty list if none. |
| `finish_reason` | `str` \| None | ✅ | One of: `"stop"`, `"length"`, `"tool_calls"`, `"content_filter"`, `"error"` |
| `usage` | `TokenUsage` \| None | — | If provider reports. If None, runtime estimates. |
| `model` | `str` \| None | — | The actual model. Falls back to `request.model_id`. |
| `raw` | `Any` | — | The raw SDK response. Used for diagnostics only. |
| `latency_ms` | `float` \| None | — | If you measure it. Runtime measures otherwise. |
| `ttft_ms` | `float` \| None | — | Time to first token. Non-streaming → same as latency. |

### `ToolCall` shape

```python
@dataclass
class ToolCall:
    id: str                      # Unique call ID
    name: str                    # Function name
    arguments: Dict[str, Any]    # Parsed arguments
    type: str = "function"
```

**Important**: `arguments` must be a **dict**, not a JSON string. If your SDK
returns the arguments as a string, parse them:

```python
if isinstance(raw_args, str):
    try:
        parsed_args = json.loads(raw_args)
    except Exception:
        parsed_args = {"raw": raw_args}
else:
    parsed_args = raw_args
```

### `TokenUsage` shape

```python
class TokenUsage(BaseModel):
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    cached_input_tokens: Optional[int]      # for prompt caching
    reasoning_output_tokens: Optional[int]  # thinking tokens
    source: UsageSource                     # PROVIDER | ESTIMATED | UNKNOWN
    estimated: bool
```

**Rules**:
- If provider reports usage → `source=PROVIDER`, `estimated=False`
- If not → `source=UNKNOWN`, `estimated=True`
- **Never** fill in `0` for unknown values. Use `None`.

### Timeouts

The runtime enforces a global timeout (`REQUEST_TIMEOUT_SECONDS`, default
600s). If your `generate()` does not return within that window, the supervisor
kills the worker.

**Do not** add your own retry loop inside `generate()`. Let the runtime handle
retry policy (it is controlled by `ProviderError.retryable`).

### Example — complete generate()

```python
async def generate(self, request):
    compiled = compile_request(request, self._profile)

    try:
        raw_response = await self._client.completions.create(
            model=compiled.model,
            messages=compiled.messages,
            temperature=compiled.generation.get("temperature", 0.7),
            max_tokens=compiled.generation.get("max_tokens"),
        )
    except MySDKAuthError as exc:
        raise ProviderAuthError(str(exc), provider=self.provider_id) from exc
    except MySDKRateLimitError as exc:
        raise ProviderRateLimitError(str(exc), provider=self.provider_id) from exc
    except MySDKError as exc:
        raise ProviderUnavailableError(str(exc), provider=self.provider_id) from exc

    # Convert the SDK response to LLMResponse
    choice = raw_response.choices[0]
    return LLMResponse(
        content=choice.message.content or "",
        reasoning=getattr(choice.message, "reasoning_content", None),
        tool_calls=[
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=json.loads(tc.function.arguments) if tc.function.arguments else {},
            )
            for tc in (choice.message.tool_calls or [])
        ],
        finish_reason=choice.finish_reason,
        usage=TokenUsage(
            input_tokens=raw_response.usage.prompt_tokens,
            output_tokens=raw_response.usage.completion_tokens,
            total_tokens=raw_response.usage.total_tokens,
            source=UsageSource.PROVIDER,
            estimated=False,
        ),
        model=raw_response.model,
        raw=raw_response,
    )
```

---

## Method: stream()

Execute a streaming completion.

### Signature

```python
def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
    ...
```

**Note**: `stream()` is **not** `async def`. It is a regular function that
returns an async iterator. Use `yield` inside it.

### What you receive

Same `LLMRequest` as `generate()`.

### What you yield

`LLMStreamEvent` instances. Every stream must yield:

1. Zero or more content/reasoning/tool-call events
2. **Exactly one** `MESSAGE_COMPLETE` event
3. **OR** exactly one `PROVIDER_ERROR` event (then `return`)

### Event types

See [LLMStreamEvent types](#llmstreamevent-types) for the full reference.

### Streaming example

```python
async def stream(self, request):
    compiled = compile_request(request, self._profile)
    seq = 0

    try:
        stream = await self._client.completions.create(
            model=compiled.model,
            messages=compiled.messages,
            stream=True,
        )

        async for chunk in stream:
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            # Content delta
            if delta.content:
                seq += 1
                yield LLMStreamEvent(
                    type=StreamEventType.CONTENT_DELTA,
                    content_delta=delta.content,
                    sequence=seq,
                )

            # Reasoning delta (if SDK exposes)
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                seq += 1
                yield LLMStreamEvent(
                    type=StreamEventType.REASONING_DELTA,
                    reasoning_delta=reasoning,
                    sequence=seq,
                )

            # Tool calls
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    if tc.function and tc.function.name:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.TOOL_CALL_START,
                            tool_call_id=tc.id,
                            tool_name=tc.function.name,
                            tool_index=tc.index,
                            sequence=seq,
                        )
                    if tc.function and tc.function.arguments:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.TOOL_CALL_DELTA,
                            tool_call_id=tc.id,
                            tool_arguments_delta=tc.function.arguments,
                            tool_index=tc.index,
                            sequence=seq,
                        )

            # Finish
            if choice.finish_reason:
                seq += 1
                yield LLMStreamEvent(
                    type=StreamEventType.MESSAGE_COMPLETE,
                    finish_reason=choice.finish_reason,
                    usage=TokenUsage(
                        input_tokens=chunk.usage.prompt_tokens if chunk.usage else None,
                        output_tokens=chunk.usage.completion_tokens if chunk.usage else None,
                        source=UsageSource.PROVIDER if chunk.usage else UsageSource.UNKNOWN,
                    ),
                    sequence=seq,
                )
                return

    except asyncio.CancelledError:
        raise
    except MySDKAuthError as exc:
        seq += 1
        yield LLMStreamEvent(
            type=StreamEventType.PROVIDER_ERROR,
            error=ProviderAuthError(str(exc), provider=self.provider_id).to_dict(),
            sequence=seq,
        )
    except Exception as exc:
        seq += 1
        yield LLMStreamEvent(
            type=StreamEventType.PROVIDER_ERROR,
            error=ProviderUnavailableError(str(exc), provider=self.provider_id).to_dict(),
            sequence=seq,
        )
```

### Critical rules

**1. Exactly one `MESSAGE_COMPLETE`**

The supervisor uses `MESSAGE_COMPLETE` as the terminal signal. If you never
yield it, the chat hangs. If you yield two, the second is ignored (but the
supervisor logs a warning).

**2. `PROVIDER_ERROR` replaces `MESSAGE_COMPLETE`**

When an error occurs mid-stream, yield `PROVIDER_ERROR` and **return**. Do not
also yield `MESSAGE_COMPLETE` — that would confuse the supervisor.

**3. Yield `sequence`**

Every event should carry a monotonic `sequence`. The supervisor uses it to
preserve event order when bridging to SSE.

**4. `CancelledError` propagates**

Do not catch `asyncio.CancelledError`. Let it propagate. The supervisor
handles stream cancellation:

```python
except asyncio.CancelledError:
    raise   # ← must re-raise
```

**5. Close the stream on cancellation**

If your SDK opens a persistent HTTP connection, close it when cancelled:

```python
except asyncio.CancelledError:
    try:
        await stream.close()
    except Exception:
        pass
    raise
```

---

## Method: discover_models()

List available models for the given connection.

### Signature

```python
async def discover_models(
    self, connection: Optional[ConnectionContext] = None
) -> List[Dict[str, Any]]:
    ...
```

### What you return

A list of dicts. Each dict must contain at least `{"id": "..."}`.

```python
[
    {"id": "model-1", "display_name": "Model One", "max_context_length": 8192},
    {"id": "model-2", "display_name": "Model Two", "max_context_length": 32768},
]
```

**Optional fields**:

| Field | Type | Purpose |
|-------|------|---------|
| `display_name` | `str` | Shown in the model picker |
| `max_context_length` | `int` | Initial context window |
| `max_output_tokens` | `int` | Initial output limit |
| `publisher` | `str` | Shown in model details |
| `architecture` | `str` | Model architecture |
| `family` | `str` | Model family |
| `parameter_size` | `str` | Parameter count hint |
| `quantization` | `str` | Quantization type |
| `type` | `str` | `"llm"` / `"vlm"` / `"embeddings"` |

### Rules

**1. Never raise**

If discovery fails, return `[]`:

```python
async def discover_models(self, connection=None):
    try:
        models = await self._client.models.list()
    except Exception as exc:
        logger.warning("Discovery failed: %s", exc)
        return []   # ← do NOT raise
    return [{"id": m.id} for m in models.data]
```

Raising would kill the worker. Returning `[]` lets the UI show "no models discovered; enter manually".

**2. Ignore the connection argument if convenient**

The adapter was already constructed with a connection. The `connection`
argument exists for symmetry with the factory's discovery path (which
sometimes creates a fresh adapter just for discovery). For most adapters,
`self._connection` is sufficient.

### Example

```python
async def discover_models(self, connection=None):
    try:
        response = await self._client.models.list()
    except Exception as exc:
        logger.warning("GigaChat model discovery failed: %s", exc)
        return []

    results = []
    for model in response.data or []:
        results.append({
            "id": model.id_,
            "display_name": model.name,
            "max_context_length": getattr(model, "context_length", None),
            "type": "llm",
        })
    return results
```

---

## Method: validate_connection()

Probe the connection by calling a cheap endpoint.

### Signature

```python
async def validate_connection(
    self, connection: Optional[ConnectionContext] = None
) -> Dict[str, Any]:
    ...
```

### What you return

```python
{
    "ok": True,
    "message": "Connected",
    "latency_ms": 245.5,
}
```

Or on failure:

```python
{
    "ok": False,
    "message": "Authentication failed: invalid API key",
    "latency_ms": 12.3,
    "code": "auth_failed",         # optional
    "status_code": 401,            # optional
}
```

### Rules

**1. Never raise**

If validation fails, return `{"ok": False, ...}`:

```python
async def validate_connection(self, connection=None):
    import time
    started = time.time()
    try:
        await self._client.models.list()
        return {
            "ok": True,
            "message": "Connected",
            "latency_ms": round((time.time() - started) * 1000, 2),
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": str(exc),
            "latency_ms": round((time.time() - started) * 1000, 2),
        }
```

Raising would show a generic "Test failed" in the UI. Returning `{"ok": False}` lets the UI show the exact error message.

**2. Use the cheapest endpoint**

Do not send a chat completion. That costs money. Use:

- `GET /models` — usually free
- `GET /health` — if available
- `GET /me` or `GET /account` — if available

**3. Include `latency_ms`**

The UI shows it as "Connected — 245 ms". If absent, the UI omits the
measurement.

---

## Method: get_model_metadata()

Fetch per-model metadata, if the provider exposes it.

### Signature

```python
async def get_model_metadata(
    self, model_id: str
) -> Optional[Dict[str, Any]]:
    ...
```

### What you return

The full metadata dict for the model, or `None` if the provider has no
per-model endpoint.

```python
{
    "id": "model-1",
    "display_name": "Model One",
    "max_context_length": 8192,
    "max_output_tokens": 4096,
    "publisher": "Acme",
    "architecture": "transformer",
    "family": "acme-1",
}
```

### Rules

**1. Never raise**

Return `None` on any failure:

```python
async def get_model_metadata(self, model_id):
    try:
        response = await self._client.models.get(model_id)
    except Exception:
        return None
    return {
        "id": model_id,
        "display_name": response.name,
        "max_context_length": response.context_length,
    }
```

**2. Return `None` if no endpoint exists**

If your provider has no per-model endpoint, return `None`:

```python
async def get_model_metadata(self, model_id):
    return None
```

The runtime treats `None` as "no metadata available" and falls back to cached
data.

---

## Method: close()

Release all resources.

### Signature

```python
async def close(self) -> None:
    ...
```

### What you close

- SDK HTTP clients (`await client.aclose()`, `await client.close()`)
- WebSocket connections
- Any file handles
- Any background tasks

### Rules

**1. Never raise**

Wrap the close in try/except:

```python
async def close(self):
    try:
        await self._client.aclose()
    except Exception:
        pass
```

**2. Idempotent**

`close()` may be called multiple times. Ensure your close logic is safe:

```python
async def close(self):
    if self._closed:
        return
    self._closed = True
    try:
        await self._client.aclose()
    except Exception:
        pass
```

**3. Called on shutdown**

The supervisor calls `close()` before killing the worker. If you do not
implement it, sockets leak until the process exits.

---

## LLMStreamEvent types

Here is the complete list of event types your `stream()` can yield.

### `CONTENT_DELTA`

Yielded for each token of text content.

```python
LLMStreamEvent(
    type=StreamEventType.CONTENT_DELTA,
    content_delta="Hello",
    sequence=1,
)
```

**Supervisor behavior**: Fires `on_chunk` callback. Yields `StreamChunk(content_delta="Hello")` to consumers.

### `REASONING_DELTA`

Yielded for each token of thinking/reasoning content.

```python
LLMStreamEvent(
    type=StreamEventType.REASONING_DELTA,
    reasoning_delta="Let me think...",
    sequence=2,
)
```

**Supervisor behavior**: Fires `on_thought_chunk` callback. Yields `StreamChunk(reasoning_delta="...")` to consumers.

### `TOOL_CALL_START`

Emitted when the model begins a tool call.

```python
LLMStreamEvent(
    type=StreamEventType.TOOL_CALL_START,
    tool_call_id="call_123",
    tool_name="search_web",
    tool_index=0,
    sequence=3,
)
```

**Required**: `tool_call_id`, `tool_name`, `tool_index`.
**Supervisor behavior**: Opens a tool-call slot in the accumulator.

### `TOOL_CALL_DELTA`

Emitted for each fragment of the tool-call arguments.

```python
LLMStreamEvent(
    type=StreamEventType.TOOL_CALL_DELTA,
    tool_call_id="call_123",
    tool_name="search_web",       # repeated for convenience
    tool_index=0,
    tool_arguments_delta='{"q":',
    sequence=4,
)
```

**Required**: `tool_call_id`, `tool_arguments_delta`.
**Supervisor behavior**: Appends the fragment to the accumulated arguments.

### `TOOL_CALL_COMPLETE`

Emitted when the tool call is fully assembled.

```python
LLMStreamEvent(
    type=StreamEventType.TOOL_CALL_COMPLETE,
    tool_call_id="call_123",
    tool_name="search_web",
    tool_index=0,
    tool_arguments_complete={"q": "python"},
    sequence=5,
)
```

**Required**: `tool_call_id`, `tool_arguments_complete` (dict).
**Supervisor behavior**: Marks the tool call as complete; no further deltas are accepted.

**Optional in OpenAI-style streaming**: The runtime can reconstruct a tool call from `TOOL_CALL_START` + `TOOL_CALL_DELTA` messages without a `TOOL_CALL_COMPLETE`. Use `TOOL_CALL_COMPLETE` only if your SDK returns the final arguments as a single object (Ollama, Google style).

### `USAGE_UPDATE`

Emitted when the provider sends a usage update mid-stream.

```python
LLMStreamEvent(
    type=StreamEventType.USAGE_UPDATE,
    usage=TokenUsage(
        input_tokens=100,
        output_tokens=5,
        total_tokens=105,
        source=UsageSource.PROVIDER,
    ),
    sequence=6,
)
```

**Required**: `usage`.
**Supervisor behavior**: Stashes the usage. The final `MESSAGE_COMPLETE` carries the last known usage.

### `MESSAGE_COMPLETE`

Emitted exactly once at the end of the stream.

```python
LLMStreamEvent(
    type=StreamEventType.MESSAGE_COMPLETE,
    finish_reason="stop",
    usage=TokenUsage(...),
    sequence=7,
)
```

**Required**: `finish_reason`.
**Supervisor behavior**: Yields the final `StreamChunk(finish_reason=..., usage=...)` and closes the stream.

### `PROVIDER_ERROR`

Emitted on error. Replaces `MESSAGE_COMPLETE`.

```python
LLMStreamEvent(
    type=StreamEventType.PROVIDER_ERROR,
    error=ProviderRateLimitError(
        "Rate limit exceeded",
        provider="my-provider",
        retryable=True,
    ).to_dict(),
    sequence=8,
)
```

**Required**: `error` dict (from `ProviderError.to_dict()`).
**Supervisor behavior**: Converts the error to the corresponding exception and raises it to the caller.

---

## Error taxonomy

Every error your adapter raises (or yields as `PROVIDER_ERROR`) must be a
`ProviderError` subclass. Here is the full taxonomy.

| Exception | When | Retryable |
|-----------|------|-----------|
| `ProviderAuthError` | 401, 403, invalid API key | ❌ |
| `ProviderPermissionError` | Valid credentials, no permission for action | ❌ |
| `ProviderRateLimitError` | 429 | ✅ |
| `ProviderQuotaExceededError` | Out of credits | ❌ |
| `ProviderTimeoutError` | Timeout, no response | ✅ |
| `ProviderUnavailableError` | 5xx, server down | ✅ |
| `ProviderConnectionError` | DNS, TCP, TLS failure | ✅ |
| `ProviderInvalidRequestError` | 400, malformed request | ❌ |
| `ProviderContextLengthError` | Prompt too long | ❌ |
| `ProviderUnsupportedFeatureError` | Provider rejects a parameter | ❌ |
| `ProviderContentPolicyError` | Content filtered | ❌ |
| `ProviderResponseParseError` | Could not parse response | ❌ |

### Constructing errors

```python
from peldrun.llm.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderError,
)

# Simple
raise ProviderAuthError("Invalid API key", provider="my-provider")

# With details
raise ProviderRateLimitError(
    "Rate limit exceeded",
    provider="my-provider",
    status_code=429,
    retryable=True,
)
```

### Mapping SDK exceptions

Every SDK has its own exception classes. Map them:

```python
def _map_exception(self, exc: Exception) -> ProviderError:
    """Convert an SDK exception to a canonical ProviderError."""
    status = getattr(exc, "status_code", None)

    if status == 401:
        return ProviderAuthError(str(exc), provider=self.provider_id, status_code=401)
    if status == 403:
        return ProviderPermissionError(str(exc), provider=self.provider_id, status_code=403)
    if status == 429:
        return ProviderRateLimitError(str(exc), provider=self.provider_id, status_code=429)
    if status == 400:
        return ProviderInvalidRequestError(str(exc), provider=self.provider_id, status_code=400)
    if status in (500, 502, 503, 504):
        return ProviderUnavailableError(str(exc), provider=self.provider_id, status_code=status)
    if isinstance(exc, TimeoutError):
        return ProviderTimeoutError(str(exc), provider=self.provider_id, phase="request")
    if isinstance(exc, ConnectionError):
        return ProviderConnectionError(str(exc), provider=self.provider_id)

    return ProviderError(f"{type(exc).__name__}: {exc}", provider=self.provider_id)
```

### `to_dict()`

Every `ProviderError` has a `to_dict()` method:

```python
{
    "code": "auth_failed",
    "message": "Invalid API key",
    "provider": "my-provider",
    "model": None,
    "status_code": 401,
    "retryable": False,
    "details": {},
}
```

The supervisor uses this dict to reconstruct the exception at the caller
side. Do not modify it.

---

## The ProviderContext object

Here is the full shape your constructor receives.

### Full `ProviderContext` and nested types

```python
@dataclass
class ProviderContext:
    connection: ConnectionContext
    profile: AdapterProfile
    credentials: Dict[str, str]


class ConnectionContext(BaseModel):
    connection_id: str
    provider_id: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_type: Optional[str] = None
    timeout: float = 120.0
    extra_headers: Dict[str, str] = {}
    extra_params: Dict[str, Any] = {}
    metadata_endpoint: Optional[str] = None
    verify_ssl: bool = True
    workspace_access: bool = False


class AdapterProfile(BaseModel):
    provider_id: str
    adapter_version: str = "1.0.0"
    api_surface: str = "chat_completions"
    features: AdapterFeatures
    reasoning_strategies: List[ReasoningStrategy]
    tool_call_style: ToolCallStyle
    supported_param_names: List[str]
    notes: Optional[str] = None
```

### Common access patterns

```python
# Construct an adapter
def __init__(self, context):
    self._connection = context.connection
    self._profile = context.profile

    # Read base_url
    base_url = self._connection.base_url or "https://api.example.com/v1"

    # Read API key
    api_key = self._connection.api_key

    # Read extra_params
    extra = dict(self._connection.extra_params or {})
    extra.pop("_peldrun_plugin", None)
    scope = extra.get("scope", "personal")

    # Read credentials shortcut (redundant with connection.api_key)
    api_key = context.credentials.get("api_key") or api_key

    # Check timeout
    timeout = float(self._connection.timeout or 60.0)

    # Check verify_ssl
    verify = self._connection.verify_ssl
```

---

## Cancellation handling

`asyncio.CancelledError` is a signal that the caller wants to stop the
operation. It must propagate.

### The rule

**Never catch `BaseException`.** Always catch `Exception`.

```python
# WRONG — catches CancelledError
except BaseException as exc:
    logger.error("Error: %s", exc)

# RIGHT — lets CancelledError propagate
except Exception as exc:
    logger.error("Error: %s", exc)
```

### In `generate()`

```python
async def generate(self, request):
    try:
        return await self._client.generate(request)
    except asyncio.CancelledError:
        raise   # ← must re-raise
    except Exception as exc:
        raise self._map_exception(exc) from exc
```

### In `stream()`

```python
async def stream(self, request):
    try:
        async for chunk in self._client.stream(request):
            yield self._convert(chunk)
    except asyncio.CancelledError:
        # Close the underlying stream
        try:
            await self._client.close_stream()
        except Exception:
            pass
        raise   # ← must re-raise
    except Exception as exc:
        yield self._error_event(self._map_exception(exc))
```

### What happens on cancellation

1. User clicks Stop in the UI
2. `asyncio.CancelledError` propagates from the supervisor
3. Supervisor sends `stream_cancel` to the worker
4. Worker cancels the streaming task
5. Your adapter's `stream()` receives `CancelledError`
6. Your code closes the SDK stream and re-raises
7. Worker yields `MESSAGE_COMPLETE` with `cancelled=True`
8. Supervisor stops emitting events
9. Chat state becomes "cancelled"

If you swallow `CancelledError` (e.g., with `except BaseException`), the
Stop button does nothing. The user must wait for the stream to finish
naturally, or kill the process.

---

## Complete adapter template

Here is a complete, working adapter template you can copy and customize.

```python
# my_provider_peldrun/adapter.py
"""MyProvider adapter for PELDRUN."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

# Import the SDK
from my_sdk import AsyncMyClient, MyAuthError, MyRateLimitError, MySDKError

# Import PELDRUN contracts
from peldrun.llm.client import (
    LLMResponse,
    TokenUsage,
    ToolCall,
    UsageSource,
)
from peldrun.llm.contracts import (
    AdapterFeatures,
    AdapterProfile,
    ConnectionContext,
    LLMStreamEvent,
    ReasoningStrategy,
    StreamEventType,
    ToolCallStyle,
)
from peldrun.llm.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from peldrun.llm.request import LLMRequest
from peldrun.llm.request_compiler import compile_request


logger = logging.getLogger("my_provider_peldrun.adapter")


# ---------------------------------------------------------------------------
# Adapter profile
# ---------------------------------------------------------------------------

_PROFILE = AdapterProfile(
    provider_id="my-provider",
    api_surface="chat_completions",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=True,
        parallel_tool_calls=False,
        forced_tool_choice=False,
        vision=False,
        audio=False,
        structured_output=False,
        usage_reporting=True,
        reasoning=False,
    ),
    reasoning_strategies=[],
    tool_call_style=ToolCallStyle.OPENAI_FUNCTION,
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class MyProviderAdapter:
    """Native MyProvider adapter."""

    ADAPTER_STYLE = "v1"
    API_CONTRACT = "peldrun.llm.provider.v1"
    PROFILE = _PROFILE
    provider_id = "my-provider"

    def __init__(self, context: Any) -> None:
        self._connection: ConnectionContext = context.connection
        self._profile: AdapterProfile = context.profile or _PROFILE
        self.profile = self._profile

        # Extract custom connection fields
        extra = dict(self._connection.extra_params or {})
        extra.pop("_peldrun_plugin", None)

        base_url = self._connection.base_url or "https://api.myprovider.com/v1"
        api_key = self._connection.api_key or ""
        timeout = float(self._connection.timeout or 60.0)
        region = str(extra.get("region") or "us-east-1")

        if not api_key:
            raise ProviderAuthError(
                "MyProvider requires an API key.",
                provider=self.provider_id,
            )

        self._client = AsyncMyClient(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            region=region,
        )

        logger.debug(
            "MyProviderAdapter ready: base_url=%s region=%s",
            base_url, region,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        compiled = compile_request(request, self._profile)
        try:
            raw = await self._client.chat(
                model=compiled.model,
                messages=compiled.messages,
                temperature=compiled.generation.get("temperature", 0.7),
                max_tokens=compiled.generation.get("max_tokens"),
                tools=compiled.tools,
            )
        except MyAuthError as exc:
            raise ProviderAuthError(str(exc), provider=self.provider_id) from exc
        except MyRateLimitError as exc:
            raise ProviderRateLimitError(str(exc), provider=self.provider_id) from exc
        except MySDKError as exc:
            raise self._map_exception(exc) from exc

        choice = raw.choices[0]
        tool_calls = []
        for tc in (choice.message.tool_calls or []):
            raw_args = tc.function.arguments
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {"raw": raw_args}
            else:
                args = raw_args or {}
            tool_calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=args,
            ))

        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=TokenUsage(
                input_tokens=raw.usage.prompt_tokens,
                output_tokens=raw.usage.completion_tokens,
                total_tokens=raw.usage.total_tokens,
                source=UsageSource.PROVIDER,
                estimated=False,
            ),
            model=raw.model,
            raw=raw,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        compiled = compile_request(request, self._profile)
        seq = 0
        try:
            stream = await self._client.chat_stream(
                model=compiled.model,
                messages=compiled.messages,
                temperature=compiled.generation.get("temperature", 0.7),
                max_tokens=compiled.generation.get("max_tokens"),
                tools=compiled.tools,
            )

            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]

                if choice.delta.content:
                    seq += 1
                    yield LLMStreamEvent(
                        type=StreamEventType.CONTENT_DELTA,
                        content_delta=choice.delta.content,
                        sequence=seq,
                    )

                if choice.finish_reason:
                    seq += 1
                    yield LLMStreamEvent(
                        type=StreamEventType.MESSAGE_COMPLETE,
                        finish_reason=choice.finish_reason,
                        usage=TokenUsage(
                            input_tokens=chunk.usage.prompt_tokens if chunk.usage else None,
                            output_tokens=chunk.usage.completion_tokens if chunk.usage else None,
                            total_tokens=chunk.usage.total_tokens if chunk.usage else None,
                            source=UsageSource.PROVIDER if chunk.usage else UsageSource.UNKNOWN,
                        ),
                        sequence=seq,
                    )
                    return

        except asyncio.CancelledError:
            try:
                await stream.close()
            except Exception:
                pass
            raise
        except MySDKError as exc:
            seq += 1
            yield LLMStreamEvent(
                type=StreamEventType.PROVIDER_ERROR,
                error=self._map_exception(exc).to_dict(),
                sequence=seq,
            )

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        try:
            models = await self._client.list_models()
        except Exception as exc:
            logger.warning("Model discovery failed: %s", exc)
            return []
        return [
            {
                "id": m.id,
                "display_name": m.name,
                "max_context_length": m.context_length,
            }
            for m in models
        ]

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        started = time.time()
        try:
            await self._client.list_models()
            return {
                "ok": True,
                "message": "Connected",
                "latency_ms": round((time.time() - started) * 1000, 2),
            }
        except MyAuthError as exc:
            return {
                "ok": False,
                "message": f"Authentication failed: {exc}",
                "latency_ms": round((time.time() - started) * 1000, 2),
                "code": "auth_failed",
                "status_code": 401,
            }
        except Exception as exc:
            return {
                "ok": False,
                "message": str(exc),
                "latency_ms": round((time.time() - started) * 1000, 2),
            }

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        for m in await self.discover_models():
            if m["id"] == model_id:
                return m
        return None

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _map_exception(self, exc: Exception) -> ProviderError:
        status = getattr(exc, "status_code", None)
        if status == 401:
            return ProviderAuthError(str(exc), provider=self.provider_id)
        if status == 429:
            return ProviderRateLimitError(str(exc), provider=self.provider_id)
        if status == 400:
            return ProviderInvalidRequestError(str(exc), provider=self.provider_id)
        if status in (500, 502, 503, 504):
            return ProviderUnavailableError(str(exc), provider=self.provider_id)
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return ProviderTimeoutError(str(exc), provider=self.provider_id)
        return ProviderError(str(exc), provider=self.provider_id)
```

---

## Anti-patterns

### ❌ Missing class attributes

```python
# WRONG
class MyAdapter:
    async def generate(self, request): ...
```

Without `ADAPTER_STYLE`, `API_CONTRACT`, `PROFILE`, `provider_id`, the factory
fails to instantiate.

### ❌ `__init__` without `context`

```python
# WRONG
def __init__(self, config):  # ← legacy signature
    self._config = config
```

The factory calls `MyAdapter(context=...)`. If your constructor does not
accept `context`, `TypeError`.

### ❌ Raw SDK exceptions

```python
# WRONG
except MySDKError as exc:
    raise exc  # ← caller sees MySDKError, not ProviderError
```

Map to `ProviderError` subclass.

### ❌ Catching `BaseException`

```python
# WRONG
except BaseException as exc:
    ...
```

Breaks the Stop button. Use `except Exception`.

### ❌ Not yielding `MESSAGE_COMPLETE`

```python
# WRONG
async def stream(self, request):
    async for chunk in sdk.stream():
        yield LLMStreamEvent(...)   # ← no MESSAGE_COMPLETE
```

Chat hangs. Always yield `MESSAGE_COMPLETE` once.

### ❌ Yielding `MESSAGE_COMPLETE` after `PROVIDER_ERROR`

```python
# WRONG
except Exception as exc:
    yield LLMStreamEvent(type=PROVIDER_ERROR, ...)
# falls through and yields MESSAGE_COMPLETE too
```

Yielding both confuses the supervisor. Use `return` after `PROVIDER_ERROR`.

### ❌ Raising in `discover_models` / `validate_connection`

```python
# WRONG
async def discover_models(self, connection=None):
    return await self._client.list_models()   # ← raises on failure
```

Discovery fails = worker crash. Return `[]` instead.

### ❌ Not removing `_peldrun_plugin`

```python
# WRONG
extra = self._connection.extra_params
my_sdk.configure(**extra)   # ← crashes on _peldrun_plugin dict
```

Always `extra.pop("_peldrun_plugin", None)`.

### ❌ Synchronous SDK calls

```python
# WRONG
async def generate(self, request):
    return self._client.chat(request)   # ← blocks the event loop
```

Use `await self._client.achat(...)` or `await asyncio.to_thread(...)`.

### ❌ Hardcoded model names

```python
# WRONG
await self._client.chat(model="my-model")   # ← ignores user's choice
```

Use `request.model_id` or `compiled.model`.

### ❌ Retry loops inside the adapter

```python
# WRONG
for attempt in range(3):
    try:
        return await self._client.chat(...)
    except:
        await asyncio.sleep(1)
```

The runtime handles retries based on `ProviderError.retryable`. Adapter-level
retry loops cause 9x the rate-limit hits.

---

## Checklist

Before packaging your adapter, verify:

### Class attributes

- [ ] `ADAPTER_STYLE = "v1"`
- [ ] `API_CONTRACT = "peldrun.llm.provider.v1"`
- [ ] `PROFILE = AdapterProfile(...)` with correct features
- [ ] `provider_id = "<your-id>"` (matches manifest)

### Constructor

- [ ] `def __init__(self, context)` accepts context
- [ ] Reads `context.connection.base_url`
- [ ] Reads `context.connection.api_key`
- [ ] Reads `context.connection.extra_params`
- [ ] Pops `_peldrun_plugin` from extra_params
- [ ] Validates required fields (raises `ProviderAuthError` if missing)
- [ ] Constructs SDK client with `timeout` from connection

### `generate()`

- [ ] `async def generate(self, request) -> LLMResponse`
- [ ] Compiles request via `compile_request()`
- [ ] Uses `compiled.model`, not a hardcoded name
- [ ] Maps SDK exceptions to `ProviderError` subclasses
- [ ] Returns `LLMResponse` with `content`, `finish_reason`, `usage`
- [ ] Handles `CancelledError` by re-raising

### `stream()`

- [ ] `def stream(self, request) -> AsyncIterator[LLMStreamEvent]`
- [ ] Yields `CONTENT_DELTA` for each text token
- [ ] Yields `REASONING_DELTA` for each thinking token (if provider supports)
- [ ] Yields `TOOL_CALL_START` + `TOOL_CALL_DELTA` for tool calls
- [ ] Yields exactly one `MESSAGE_COMPLETE` at the end
- [ ] Yields `PROVIDER_ERROR` (not raises) on failure
- [ ] Does NOT yield `MESSAGE_COMPLETE` after `PROVIDER_ERROR`
- [ ] Re-raises `CancelledError` after closing SDK stream
- [ ] Every event has a monotonic `sequence`

### `discover_models()`

- [ ] Returns list of dicts with at least `{"id": "...", ...}`
- [ ] Returns `[]` on failure (does not raise)
- [ ] Includes `display_name` and `max_context_length` when available

### `validate_connection()`

- [ ] Returns `{"ok": bool, "message": str, "latency_ms": float}`
- [ ] Returns `{"ok": False, ...}` on failure (does not raise)
- [ ] Uses a cheap endpoint (`/models` or `/health`)

### `get_model_metadata()`

- [ ] Returns dict or `None` (does not raise)

### `close()`

- [ ] `async def close(self) -> None`
- [ ] Closes SDK client
- [ ] Never raises
- [ ] Idempotent

### Testing

- [ ] Imports cleanly: `python -c "from module import Adapter"`
- [ ] Local test script succeeds with valid credentials
- [ ] Error test shows `ProviderAuthError` with invalid credentials
- [ ] Streaming test shows tokens arriving incrementally
- [ ] `discover_models()` returns at least one model
- [ ] `validate_connection()` returns `{"ok": true, ...}`
- [ ] Cancellation works (Stop button interrupts stream)

---

## What's next

- **Need to declare UI warnings?** → [Runtime Hints](07-runtime-hints.md)
- **Need the permission model?** → [Permissions](08-permissions.md)
- **Need real-world examples?** → [Examples](13-examples.md) for GigaChat,
  Yandex, AWS Bedrock, Vertex, OpenRouter
- **Need the manifest reference?** → [Manifest Reference](04-manifest-reference.md)
- **Hitting an error?** → [Troubleshooting](14-troubleshooting.md)
```

---
