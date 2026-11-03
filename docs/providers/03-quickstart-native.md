### `docs/providers/03-quickstart-native.md`

```markdown
<!-- docs/providers/03-quickstart-native.md -->
# Quickstart — Native Provider Package

> **Goal**: Build, install, and use a `native` Provider Package that ships
> its own Python code to talk to a provider whose API is not
> OpenAI-compatible.
>
> **Prerequisites**:
> - PELDRUN is running locally
> - You are comfortable with async Python
> - You know the provider's API (or you have their SDK)
>
> **What you'll build**: A `native` package for **GigaChat** (Sber), a
> Russian LLM provider that requires OAuth authentication, custom TLS
> certificates, and its own request/response shapes.
>
> **Time**: ~2 hours.

---

## Table of Contents

- [When to use the native transport](#when-to-use-the-native-transport)
- [Package structure](#package-structure)
- [Step 1 — manifest.json](#step-1--manifestjson)
- [Step 2 — requirements.txt](#step-2--requirementstxt)
- [Step 3 — the adapter class](#step-3--the-adapter-class)
- [Step 4 — the AdapterProfile](#step-4--the-adapterprofile)
- [Step 5 — implementing generate()](#step-5--implementing-generate)
- [Step 6 — implementing stream()](#step-6--implementing-stream)
- [Step 7 — implementing discover_models()](#step-7--implementing-discover_models)
- [Step 8 — implementing validate_connection()](#step-8--implementing-validate_connection)
- [Step 9 — error handling](#step-9--error-handling)
- [Step 10 — testing locally](#step-10--testing-locally)
- [Step 11 — package and install](#step-11--package-and-install)
- [Understanding the execution model](#understanding-the-execution-model)
- [Common pitfalls](#common-pitfalls)
- [Checklist](#checklist)

---

## When to use the native transport

**Use `native`** when at least one of the following is true:

### The provider requires non-standard authentication

| Scenario | Why `compatible` fails |
|----------|------------------------|
| OAuth2 with client credentials | `openai_compat` sends `Authorization: Bearer <api_key>` only |
| AWS SigV4 signing | Requires HMAC-SHA256 request signing |
| Mutual TLS (client certificates) | Requires `httpx` client with custom certs |
| Azure AD token exchange | Token is short-lived and must be refreshed |

### The provider has its own wire format

| Scenario | Why `compatible` fails |
|----------|------------------------|
| Anthropic Messages API | `messages`, `system`, `tool_use`, `thinking` are structurally different from OpenAI |
| Google Gemini | `contents`, `parts`, `functionCall` are different |
| AWS Bedrock Converse | `messages` with content blocks and `toolUse` |
| Cohere Chat | `message`, `chat_history`, `connectors` |

### The provider's SDK pulls in heavy dependencies

Even if the wire format is OpenAI-compatible, if the SDK requires 20 transitive
dependencies, adding them to PELDRUN's main environment is unacceptable. Use
`native` with an isolated venv.

### The provider uses proprietary encrypted protocols

GigaChat is the canonical example — it requires OAuth, custom TLS, and a
specific request body shape.

### Decision table

| Provider | Transport | Why |
|----------|-----------|-----|
| OpenAI | `compatible` (builtin) | Already a built-in provider |
| DeepSeek | `compatible` | Speaks OpenAI protocol |
| Groq | `compatible` | Speaks OpenAI protocol |
| Mistral | `compatible` | Speaks OpenAI protocol |
| OpenRouter | `compatible` + custom headers | OpenAI protocol + `HTTP-Referer` |
| Anthropic | `native` (builtin) | Own protocol |
| Google Gemini | `native` (builtin) | Own protocol |
| **GigaChat** | **`native`** | **OAuth + custom TLS + own protocol** |
| AWS Bedrock | `native` | SigV4 + Converse API |
| YandexGPT | `native` | IAM token + own protocol |
| Vertex AI | `native` | OAuth2 service account + Google protocol |
| Azure OpenAI | `native` | Different path structure + AD auth |

---

## Package structure

A `native` package contains:

```
gigachat-package/
├── manifest.json                  # Required: identity + transport + fields
├── requirements.txt                # Required if adapter has external deps
├── pyproject.toml                 # Optional: alt to requirements.txt
└── gigachat_peldrun/              # The Python module (must match entry_point)
    ├── __init__.py
    └── adapter.py                 # Contains: GigaChatAdapter class
```

### What happens at install time

1. **Extract**: All files go into `versions/<version>/`
2. **venv**: `dependency_installer` creates `<version>/environments/`
3. **pip**: Installs packages from `requirements.txt` (or `pyproject.toml`)
4. **Register**: `registry.json` is updated

### What happens at runtime

1. Supervisor spawns `venv_python -m peldrun.llm.plugin_host.worker`
2. Worker imports `entry_point` (e.g., `gigachat_peldrun.adapter:GigaChatAdapter`)
3. Worker instantiates `GigaChatAdapter(context=...)`
4. Requests flow: supervisor → IPC → worker → adapter → provider

---

## Step 1 — manifest.json

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "gigachat",
  "name": "GigaChat (Sber)",
  "display_name": "GigaChat",
  "version": "1.0.0",
  "description": "GigaChat is a family of Russian language models from Sber. Requires OAuth authentication and a custom CA bundle.",

  "api_contract": "peldrun.llm.provider.v1",
  "transport": "native",
  "entry_point": "gigachat_peldrun.adapter:GigaChatAdapter",

  "dependencies": [
    "gigachat>=0.2.0,<1.0"
  ],

  "runtime": {
    "type": "python",
    "python": ">=3.12,<3.13"
  },

  "author": "PELDRUN Community",
  "author_url": "https://github.com/peldrun/peldrun-providers",
  "homepage": "https://developers.sber.ru/docs/ru/gigachat/guides/main",
  "license": "MIT",
  "tags": ["gigachat", "sber", "russian", "oauth"],

  "connection_fields": [
    {
      "key": "credentials",
      "label": "Authorization Key",
      "type": "password",
      "required": true,
      "placeholder": "Base64-encoded client_id:client_secret",
      "description": "From the Sber Studio dashboard. This is the 'Authorization key' for your GigaChat app."
    },
    {
      "key": "scope",
      "label": "Account Scope",
      "type": "select",
      "required": true,
      "default": "PERS",
      "options": [
        {"value": "PERS", "label": "Personal (PERS)"},
        {"value": "B2B", "label": "Business (B2B)"},
        {"value": "CORP", "label": "Corporate (CORP)"}
      ],
      "description": "Which Sber API scope to authenticate against."
    },
    {
      "key": "ca_bundle_path",
      "label": "Russian Root CA Bundle",
      "type": "file",
      "required": false,
      "accept": ".pem,.crt,.cer",
      "placeholder": "/etc/ssl/certs/russian_trusted_root_ca.pem",
      "visible_if": {"scope": "B2B"},
      "description": "B2B and CORP scopes use a different CA that certifi does not include. Required for those scopes."
    },
    {
      "key": "verify_ssl",
      "label": "Verify SSL Certificates",
      "type": "boolean",
      "required": false,
      "default": true,
      "description": "Disable ONLY for development against a local mock server."
    }
  ],

  "runtime_hints": {
    "requires_custom_tls": true,
    "requires_token_refresh": true,
    "token_lifetime_seconds": 1800,
    "docs_url": "https://developers.sber.ru/docs/ru/gigachat/guides/main"
  },

  "features": {
    "model_discovery": true,
    "native_streaming": true,
    "buffered_streaming": true,
    "tool_calling": true,
    "parallel_tool_calls": false,
    "forced_tool_choice": false,
    "vision": true,
    "audio": false,
    "structured_output": false,
    "usage_reporting": true,
    "reasoning": false
  },

  "permissions": {
    "network": "connection_endpoint",
    "secrets": ["connection.api_key"],
    "workspace_access": false,
    "shell_execute": false
  }
}
```

### Key fields explained

**`transport: "native"`** — Signals to the runtime that this package ships
code and must be executed in a worker subprocess.

**`entry_point`** — Format: `<module>.<path>:<ClassName>`. The module path
must resolve to a real `.py` file in the ZIP:

```
gigachat_peldrun.adapter:GigaChatAdapter
  → gigachat_peldrun/adapter.py
  → class GigaChatAdapter
```

The validator checks this file exists during install. If it does not, install
fails immediately.

**`dependencies`** — Informational. The actual `pip install` reads from
`requirements.txt` (or `pyproject.toml`). This list tells the UI what
dependencies the adapter needs, useful for auditing.

**`runtime.python`** — PEP 440 specifier. The installer validates the venv's
Python version against this. If the current Python does not match, install
fails before any file is created.

**`connection_fields`** — The four fields above generate this form:

```
┌─────────────────────────────────────────────────┐
│ Authorization Key                               │
│ [••••••••••••••••••••••••••••••••]              │
├─────────────────────────────────────────────────┤
│ Account Scope                                   │
│ [Personal (PERS)         ▼]                     │
├─────────────────────────────────────────────────┤
│ Verify SSL Certificates                         │
│ [✓] Enabled                                     │
└─────────────────────────────────────────────────┘
```

If the user selects B2B, the CA bundle field appears.

---

## Step 2 — requirements.txt

```
gigachat>=0.2.0,<1.0
httpx>=0.27,<0.28
```

### Why two dependencies?

- **`gigachat`** — The official Sber SDK. Handles OAuth token acquisition,
  refresh, and the protocol.
- **`httpx`** — Already used by the SDK, but declaring it ensures a compatible
  version range is installed even if the SDK later drops it.

### Alternative — pyproject.toml

If your package is structured as a proper Python package, use
`pyproject.toml` instead:

```toml
[project]
name = "peldrun-gigachat-provider"
version = "1.0.0"
description = "GigaChat provider adapter for PELDRUN"
requires-python = ">=3.12,<3.13"
dependencies = [
    "gigachat>=0.2.0,<1.0",
    "httpx>=0.27,<0.28",
]
```

The installer prefers `requirements.lock` > `requirements.txt` >
`pyproject.toml` > `manifest.dependencies`.

### Version pinning

Always pin to a major version:

- ✅ `gigachat>=0.2.0,<1.0` — allows 0.x updates
- ❌ `gigachat` — allows any future version, breaks silently
- ❌ `gigachat==0.2.1` — too strict, misses security fixes

---

## Step 3 — the adapter class

Create `gigachat_peldrun/adapter.py`:

```python
# gigachat_peldrun/adapter.py
"""
GigaChat adapter for PELDRUN.

Translates PELDRUN's LLMRequest into GigaChat's protocol and back.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from gigachat import GigaChat
from gigachat.exceptions import AuthenticationError as GigaAuthError
from gigachat.exceptions import GigaChatException
from gigachat.models import (
    Chat,
    ChatCompletion,
    ChatCompletionChunk,
    Messages,
    MessagesRole,
)

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
    ProviderConnectionError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from peldrun.llm.request import LLMRequest
from peldrun.llm.request_compiler import CompiledRequest, compile_request


logger = logging.getLogger("gigachat_peldrun.adapter")


_PROFILE = AdapterProfile(
    provider_id="gigachat",
    api_surface="chat_completions",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=True,
        vision=True,
        usage_reporting=True,
        reasoning=False,
    ),
    reasoning_strategies=[],
    tool_call_style=ToolCallStyle.OPENAI_FUNCTION,
)


class GigaChatAdapter:
    """Native GigaChat adapter."""

    ADAPTER_STYLE = "v1"
    API_CONTRACT = "peldrun.llm.provider.v1"
    PROFILE = _PROFILE
    provider_id = "gigachat"

    def __init__(self, context: Any) -> None:
        self._connection: ConnectionContext = context.connection
        self._profile = context.profile or _PROFILE
        self.profile = self._profile

        # Extract custom connection fields from extra_params.
        # `_peldrun_plugin` is reserved for the descriptor — never read it.
        extra = dict(self._connection.extra_params or {})
        extra.pop("_peldrun_plugin", None)

        credentials = extra.get("credentials") or self._connection.api_key or ""
        scope = str(extra.get("scope") or "PERS")
        ca_bundle_path = extra.get("ca_bundle_path") or None
        verify_ssl = bool(extra.get("verify_ssl", True))

        if not credentials:
            raise ProviderAuthError(
                "GigaChat requires an 'credentials' field (base64-encoded client_id:client_secret).",
                provider="gigachat",
            )

        # GigaChat SDK handles OAuth token acquisition internally.
        self._client = GigaChat(
            credentials=credentials,
            scope=scope,
            verify_ssl_certs=verify_ssl,
            ca_bundle_file=ca_bundle_path,
            model=str(self._connection.extra_params.get("model") or "GigaChat"),
            timeout=float(self._connection.timeout or 60.0),
        )

        logger.debug(
            "GigaChatAdapter ready: scope=%s verify_ssl=%s ca_bundle=%s",
            scope,
            verify_ssl,
            ca_bundle_path or "<system>",
        )

    # ------------------------------------------------------------------
    # generate()
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        compiled = compile_request(request, self._profile)
        try:
            chat = self._build_chat_payload(compiled)
            result: ChatCompletion = await self._client.achat(chat)
        except GigaAuthError as exc:
            raise ProviderAuthError(str(exc), provider="gigachat") from exc
        except GigaChatException as exc:
            raise self._map_exception(exc) from exc

        return self._convert_response(result, compiled.model)

    # ------------------------------------------------------------------
    # stream()
    # ------------------------------------------------------------------

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        compiled = compile_request(request, self._profile)
        seq = 0
        try:
            chat = self._build_chat_payload(compiled)
            chat.stream = True

            async for chunk in self._client.astream(chat):
                content_delta = self._extract_content_delta(chunk)
                if content_delta:
                    seq += 1
                    yield LLMStreamEvent(
                        type=StreamEventType.CONTENT_DELTA,
                        content_delta=content_delta,
                        sequence=seq,
                    )

                finish = self._extract_finish_reason(chunk)
                if finish:
                    usage = self._extract_usage(chunk)
                    seq += 1
                    yield LLMStreamEvent(
                        type=StreamEventType.MESSAGE_COMPLETE,
                        finish_reason=finish,
                        usage=usage,
                        sequence=seq,
                    )
                    return
        except GigaAuthError as exc:
            yield self._error_event(
                ProviderAuthError(str(exc), provider="gigachat"),
                sequence=seq + 1,
            )
        except GigaChatException as exc:
            yield self._error_event(
                self._map_exception(exc), sequence=seq + 1
            )

    # ------------------------------------------------------------------
    # discover_models()
    # ------------------------------------------------------------------

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        try:
            models = await self._client.aget_models()
        except Exception as exc:
            logger.warning("GigaChat model discovery failed: %s", exc)
            return []

        results: List[Dict[str, Any]] = []
        for model in models.data or []:
            results.append({
                "id": getattr(model, "id_", None) or getattr(model, "id", ""),
                "display_name": getattr(model, "name", None),
                "max_context_length": getattr(model, "context_length", None),
            })
        return results

    # ------------------------------------------------------------------
    # validate_connection()
    # ------------------------------------------------------------------

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        import time

        started = time.time()
        try:
            await self._client.aget_models()
            latency_ms = round((time.time() - started) * 1000.0, 2)
            return {
                "ok": True,
                "message": "GigaChat API reachable.",
                "latency_ms": latency_ms,
            }
        except GigaAuthError as exc:
            return {
                "ok": False,
                "message": f"Authentication failed: {exc}",
                "latency_ms": round((time.time() - started) * 1000.0, 2),
                "code": "auth_failed",
                "status_code": 401,
            }
        except Exception as exc:
            return {
                "ok": False,
                "message": str(exc),
                "latency_ms": round((time.time() - started) * 1000.0, 2),
            }

    # ------------------------------------------------------------------
    # get_model_metadata()
    # ------------------------------------------------------------------

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        for model in await self.discover_models():
            if model["id"] == model_id:
                return model
        return None

    # ------------------------------------------------------------------
    # close()
    # ------------------------------------------------------------------

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_chat_payload(self, compiled: CompiledRequest) -> Chat:
        messages: List[Messages] = []

        if compiled.system_prompt:
            messages.append(
                Messages(role=MessagesRole.SYSTEM, content=compiled.system_prompt)
            )

        for msg in compiled.messages:
            role = msg.get("role")
            content = msg.get("content")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            role_map = {
                "user": MessagesRole.USER,
                "assistant": MessagesRole.ASSISTANT,
                "system": MessagesRole.SYSTEM,
            }
            messages.append(
                Messages(role=role_map.get(role, MessagesRole.USER), content=str(content or ""))
            )

        return Chat(
            messages=messages,
            model=compiled.model,
            temperature=compiled.generation.get("temperature", 0.7),
            max_tokens=compiled.generation.get("max_tokens"),
        )

    def _convert_response(
        self, result: ChatCompletion, model: str
    ) -> LLMResponse:
        choice = (result.choices or [None])[0]
        content = ""
        reasoning: Optional[str] = None
        tool_calls: List[ToolCall] = []

        if choice is not None:
            content = choice.message.content or ""
            usage = self._extract_usage(result)

        return LLMResponse(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason if choice else "stop",
            usage=self._extract_usage(result),
            model=model,
            raw=result,
        )

    def _extract_usage(self, result: Any) -> Optional[TokenUsage]:
        usage = getattr(result, "usage", None)
        if usage is None:
            return None
        return TokenUsage(
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            source=UsageSource.PROVIDER,
            estimated=False,
        )

    def _extract_content_delta(self, chunk: ChatCompletionChunk) -> str:
        if not chunk.choices:
            return ""
        return chunk.choices[0].delta.content or ""

    def _extract_finish_reason(
        self, chunk: ChatCompletionChunk
    ) -> Optional[str]:
        if not chunk.choices:
            return None
        return chunk.choices[0].finish_reason

    def _map_exception(self, exc: GigaChatException) -> ProviderError:
        status = getattr(exc, "status_code", None)
        if status == 401:
            return ProviderAuthError(str(exc), provider="gigachat")
        if status == 429:
            return ProviderRateLimitError(str(exc), provider="gigachat")
        if status in (500, 502, 503, 504):
            return ProviderUnavailableError(str(exc), provider="gigachat")
        if status == 400:
            return ProviderInvalidRequestError(str(exc), provider="gigachat")
        return ProviderError(str(exc), provider="gigachat")

    def _error_event(
        self, err: ProviderError, *, sequence: int
    ) -> LLMStreamEvent:
        return LLMStreamEvent(
            type=StreamEventType.PROVIDER_ERROR,
            error=err.to_dict(),
            sequence=sequence,
        )
```

### Reading the `connection_fields` values

The most important part is `__init__`:

```python
extra = dict(self._connection.extra_params or {})
extra.pop("_peldrun_plugin", None)  # ← always remove this

credentials = extra.get("credentials") or self._connection.api_key or ""
scope = str(extra.get("scope") or "PERS")
ca_bundle_path = extra.get("ca_bundle_path") or None
verify_ssl = bool(extra.get("verify_ssl", True))
```

Every custom field declared in `manifest.json.connection_fields` arrives here
under `connection.extra_params[<key>]`. The adapter reads them like a normal
dict.

**Rules:**

- `base_url` and `api_key` are in `connection.base_url` /
  `connection.api_key` (not in `extra_params`).
- `_peldrun_plugin` is the descriptor. Always pop it before reading.
- Custom fields are in `extra_params[<key>]`.

---

## Step 4 — the AdapterProfile

The profile declares what the adapter can do. It is used by
`RequestCompiler` to decide which parameters to include in requests, and by
`PluginProviderAdapter` to determine the transport capabilities.

```python
_PROFILE = AdapterProfile(
    provider_id="gigachat",
    api_surface="chat_completions",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=True,
        vision=True,
        usage_reporting=True,
        reasoning=False,
    ),
    reasoning_strategies=[],
    tool_call_style=ToolCallStyle.OPENAI_FUNCTION,
)
```

### Fields explained

| Field | Meaning |
|-------|---------|
| `api_surface` | `"chat_completions"` — the wire surface. Other options: `"responses"`, `"messages"`, `"generate_content"`, `"ollama_native"`, `"lmstudio_native"` |
| `features.native_streaming` | Adapter supports real streaming (not buffered) |
| `features.tool_calling` | Adapter can send tools and parse tool calls |
| `features.vision` | Adapter can send images |
| `features.usage_reporting` | Provider reports token counts |
| `features.reasoning` | Adapter supports reasoning parameters |
| `reasoning_strategies` | Which strategies the adapter can express (`[]` means none) |
| `tool_call_style` | Wire format for tool calls |

### `ADAPTER_STYLE` is mandatory

```python
class GigaChatAdapter:
    ADAPTER_STYLE = "v1"
    API_CONTRACT = "peldrun.llm.provider.v1"
    PROFILE = _PROFILE
    provider_id = "gigachat"
```

Without `ADAPTER_STYLE = "v1"`, the `ProviderFactory` treats the adapter as
legacy (pre-M2) and tries to instantiate it with a different constructor
signature. This will fail with a confusing error. **Always declare the four
class attributes.**

---

## Step 5 — implementing generate()

The full flow:

```
1. LLMRequest arrives (from supervisor over IPC)
2. compile_request(request, self._profile) → CompiledRequest
3. Adapter builds provider-specific payload from CompiledRequest
4. Adapter calls provider SDK
5. Adapter converts response → LLMResponse
6. Supervisor serializes LLMResponse → GENERATE_RESULT
```

Your job is steps 3-5:

```python
async def generate(self, request: LLMRequest) -> LLMResponse:
    compiled = compile_request(request, self._profile)
    try:
        chat = self._build_chat_payload(compiled)
        result = await self._client.achat(chat)
    except GigaAuthError as exc:
        raise ProviderAuthError(str(exc), provider="gigachat") from exc
    except GigaChatException as exc:
        raise self._map_exception(exc) from exc

    return self._convert_response(result, compiled.model)
```

### What `compile_request` does

It returns a `CompiledRequest` — a fully-compiled payload ready for your
adapter's wire format. It handles:

- Capability intersection (only sends what both model and adapter support)
- Parameter renaming (`max_output_tokens` → `max_tokens`)
- Reasoning translation
- Tool sanitization
- Temperature/reasoning conflict resolution

You do NOT need to check capabilities yourself — the compiler has done it.
You receive a payload with only the parameters that should be sent.

### What you must return

A fully populated `LLMResponse`:

```python
LLMResponse(
    content="The model's text response",
    reasoning=None,               # Or extracted reasoning text
    tool_calls=[],                # Or parsed tool calls
    finish_reason="stop",         # "stop", "length", "tool_calls", ...
    usage=TokenUsage(...),        # Or None if provider does not report
    model="GigaChat",
    raw=result,                   # The raw SDK object (for diagnostics)
)
```

---

## Step 6 — implementing stream()

Streaming is critical for UX — the user sees tokens arriving one by one.

```python
async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
    compiled = compile_request(request, self._profile)
    seq = 0
    try:
        chat = self._build_chat_payload(compiled)
        chat.stream = True

        async for chunk in self._client.astream(chat):
            content_delta = self._extract_content_delta(chunk)
            if content_delta:
                seq += 1
                yield LLMStreamEvent(
                    type=StreamEventType.CONTENT_DELTA,
                    content_delta=content_delta,
                    sequence=seq,
                )

            finish = self._extract_finish_reason(chunk)
            if finish:
                usage = self._extract_usage(chunk)
                seq += 1
                yield LLMStreamEvent(
                    type=StreamEventType.MESSAGE_COMPLETE,
                    finish_reason=finish,
                    usage=usage,
                    sequence=seq,
                )
                return
    except GigaAuthError as exc:
        yield self._error_event(
            ProviderAuthError(str(exc), provider="gigachat"),
            sequence=seq + 1,
        )
```

### Event types you can yield

| Type | Payload | When |
|------|---------|------|
| `CONTENT_DELTA` | `content_delta="text"` | Each text token |
| `REASONING_DELTA` | `reasoning_delta="text"` | Each reasoning token |
| `TOOL_CALL_START` | `tool_call_id, tool_name, tool_index` | Tool call begins |
| `TOOL_CALL_DELTA` | `tool_arguments_delta="json fragment"` | Argument fragment |
| `TOOL_CALL_COMPLETE` | `tool_arguments_complete=dict` | Tool call complete |
| `USAGE_UPDATE` | `usage=TokenUsage(...)` | Usage update |
| `MESSAGE_COMPLETE` | `finish_reason="stop", usage=...` | End of stream |
| `PROVIDER_ERROR` | `error=err.to_dict()` | Streaming error |

### Critical rules

1. **Always yield exactly one `MESSAGE_COMPLETE`** — the supervisor
   depends on it to close the stream. Missing it causes hangs.
2. **Always yield exactly one `PROVIDER_ERROR` on failure** — followed by
   `return` (do not fall through to `MESSAGE_COMPLETE`).
3. **Do not yield `MESSAGE_COMPLETE` after an error** — the supervisor
   treats it as a "recovered" stream and it corrupts accounting.
4. **Track a `sequence` counter** — the supervisor uses it to preserve order.

---

## Step 7 — implementing discover_models()

The Store calls this when the user clicks "Scan Models" on the connection.

```python
async def discover_models(
    self, connection: Optional[ConnectionContext] = None
) -> List[Dict[str, Any]]:
    try:
        models = await self._client.aget_models()
    except Exception as exc:
        logger.warning("GigaChat model discovery failed: %s", exc)
        return []

    results: List[Dict[str, Any]] = []
    for model in models.data or []:
        results.append({
            "id": model.id_,
            "display_name": model.name,
            "max_context_length": model.context_length,
        })
    return results
```

### Response shape

Every result MUST contain at least:

```json
{"id": "GigaChat-Pro"}
```

Optional fields the Store uses:

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

### Error handling

**Do NOT raise** from `discover_models`. Return `[]` on any failure. The
supervisor treats an exception as "the whole worker crashed" — which is worse
than "the model list is empty".

---

## Step 8 — implementing validate_connection()

Called when the user clicks the "Test" button.

```python
async def validate_connection(
    self, connection: Optional[ConnectionContext] = None
) -> Dict[str, Any]:
    import time
    started = time.time()
    try:
        await self._client.aget_models()
        return {
            "ok": True,
            "message": "GigaChat API reachable.",
            "latency_ms": round((time.time() - started) * 1000.0, 2),
        }
    except GigaAuthError as exc:
        return {
            "ok": False,
            "message": f"Authentication failed: {exc}",
            "latency_ms": round((time.time() - started) * 1000.0, 2),
            "code": "auth_failed",
            "status_code": 401,
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": str(exc),
            "latency_ms": round((time.time() - started) * 1000.0, 2),
        }
```

### Response shape

```json
{
  "ok": true,
  "message": "Human-readable status",
  "latency_ms": 245.5,
  "code": "auth_failed",       // optional, on failure
  "status_code": 401           // optional, on failure
}
```

**Do NOT raise** from `validate_connection`. Return `{"ok": false, ...}` on
every failure path. The Store displays the message verbatim to the user.

### What to test

Use the cheapest endpoint that proves the credentials work:

- `/models` (list models) — nearly always free
- `/health` or `/status` if available
- `POST /chat` with a 1-token prompt — costs a few cents but proves everything

**Never test by sending a full chat completion** unless absolutely necessary.
The Test button should be instantaneous and free.

---

## Step 9 — error handling

Every error must map to a `ProviderError` subclass. The supervisor rehydrates
your exception into a canonical error at the supervisor side, so the caller
sees the same error type as if your adapter ran in-process.

### Error taxonomy

| Exception | When | Retryable? |
|-----------|------|------------|
| `ProviderAuthError` | 401, 403, invalid credentials | No |
| `ProviderRateLimitError` | 429 | Yes (with backoff) |
| `ProviderTimeoutError` | Timeout, no response | Yes |
| `ProviderConnectionError` | Network failure | Yes |
| `ProviderUnavailableError` | 5xx, server down | Yes |
| `ProviderInvalidRequestError` | 400, malformed request | No |
| `ProviderContextLengthError` | Prompt too long | No |
| `ProviderUnsupportedFeatureError` | Provider rejects a parameter | No |
| `ProviderContentPolicyError` | Content filtered | No |
| `ProviderQuotaExceededError` | Out of credits | No |

### Mapping pattern

```python
def _map_exception(self, exc: Exception) -> ProviderError:
    status = getattr(exc, "status_code", None)
    if status == 401:
        return ProviderAuthError(str(exc), provider="gigachat")
    if status == 429:
        return ProviderRateLimitError(str(exc), provider="gigachat")
    if status in (500, 502, 503, 504):
        return ProviderUnavailableError(str(exc), provider="gigachat")
    if status == 400:
        return ProviderInvalidRequestError(str(exc), provider="gigachat")
    return ProviderError(str(exc), provider="gigachat")
```

### What NOT to do

**Never** return raw SDK exceptions:

```python
# WRONG
except GigaChatException as exc:
    raise exc  # ← caller sees GigaChatException, not ProviderError
```

**Never** swallow errors:

```python
# WRONG
except GigaChatException:
    return LLMResponse(content="", ...)  # ← caller has no idea it failed
```

**Never** catch `BaseException`:

```python
# WRONG
except BaseException:
    ...
```

This catches `asyncio.CancelledError` and breaks the Stop button.

---

## Step 10 — testing locally

Before packaging, test the adapter in isolation.

### 1. Install dependencies in a local venv

```bash
cd gigachat-package
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
pip install peldrun  # for the LLMResponse / LLMRequest imports
```

### 2. Write a small test script

```python
# test_adapter.py
import asyncio
from peldrun.llm.contracts import ConnectionContext, AdapterProfile
from peldrun.llm.request import (
    LLMRequest,
    GenerationConfig,
    ReasoningConfig,
    ResponseFormat,
    ToolChoiceSpec,
)
from gigachat_peldrun.adapter import GigaChatAdapter

async def main():
    connection = ConnectionContext(
        connection_id="test",
        provider_id="gigachat",
        base_url="https://gigachat.devices.sberbank.ru/api/v1",
        api_key="",
        extra_params={
            "credentials": "your-base64-credentials",
            "scope": "PERS",
            "verify_ssl": True,
        },
    )

    adapter = GigaChatAdapter(context=type("Ctx", (), {
        "connection": connection,
        "profile": GigaChatAdapter.PROFILE,
        "credentials": {},
    })())

    request = LLMRequest(
        provider_id="gigachat",
        model_id="GigaChat",
        connection_id="test",
        messages=[{"role": "user", "content": "Say hello in Russian."}],
        generation=GenerationConfig(temperature=0.7, max_output_tokens=100),
        reasoning=ReasoningConfig(),
        response_format=ResponseFormat(),
        tool_choice=ToolChoiceSpec(),
        connection=connection,
    )

    # Non-streaming
    response = await adapter.generate(request)
    print("Response:", response.content)
    print("Tokens:", response.usage)

    # Streaming
    print("\nStreaming:")
    async for event in adapter.stream(request):
        if event.type.value == "content_delta":
            print(event.content_delta, end="", flush=True)
    print()

    await adapter.close()

asyncio.run(main())
```

### 3. Run it

```bash
python test_adapter.py
```

Expected output:

```
Response: Привет!
Tokens: input_tokens=12 output_tokens=3 total_tokens=15

Streaming:
Привет! Как я могу помочь?
```

### 4. Test error paths

Change the `credentials` to an invalid value. Expected:

```
ProviderAuthError: Authentication failed
```

Change `scope` to an invalid value. Expected:

```
ProviderInvalidRequestError: Invalid scope
```

---

## Step 11 — package and install

### 1. Verify the entry point resolves

```bash
python -c "from gigachat_peldrun.adapter import GigaChatAdapter; print('OK')"
```

### 2. Build the ZIP

```bash
cd gigachat-package
zip -r gigachat-1.0.0.zip \
    manifest.json \
    requirements.txt \
    gigachat_peldrun/
```

**Verify the structure:**

```bash
unzip -l gigachat-1.0.0.zip
```

Expected:

```
Archive:  gigachat-1.0.0.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
     2500  2025-01-01 00:00   manifest.json
       50  2025-01-01 00:00   requirements.txt
        0  2025-01-01 00:00   gigachat_peldrun/__init__.py
     8000  2025-01-01 00:00   gigachat_peldrun/adapter.py
---------                     -------
    10550                     4 files
```

### 3. Install it

In the UI:

1. **Store** → **Providers** → **Upload Package**
2. Select `gigachat-1.0.0.zip`
3. Leave **"Install dependencies in an isolated venv"** checked
4. Click **Install Package**

**What happens:**

```
validate ✓ → conflict check ✓ → extract ✓ → promote ✓ → venv (~5s) → pip install (~30s) → register ✓
```

The upload modal shows the progress. When done, the card appears in the
**Installed** tab.

### 4. Configure a connection

1. Click **Configure**
2. Fill in:
   - **Connection Name**: `Personal`
   - **Authorization Key**: `<your base64 credentials>`
   - **Account Scope**: `Personal (PERS)`
   - **Verify SSL**: ✓
3. Click **Create Connection**

### 5. Test the connection

Click **⚡ Test**. Expected:

```
✓ Connected — 312 ms
```

### 6. Use it in chat

The provider is now available as `gigachat`. Use it in any chat.

---

## Understanding the execution model

### What runs where

| Component | Process | Why |
|-----------|---------|-----|
| Supervisor | Main backend | Manages workers, translates errors |
| Transport | Main backend | JSON-Lines framing over pipes |
| Worker | Subprocess | Runs your adapter in isolation |
| Your adapter | Subprocess | Calls the provider API |
| Provider SDK | Subprocess | Uses the isolated venv |

### The IPC round-trip

```
[Main process]                           [Worker subprocess]
     │                                          │
     │  {"v":1,"type":"generate",...}           │
     ├──────────────────────────────────────────┤
     │                                          │
     │                              adapter.generate(request)
     │                                          │
     │                                     [provider API]
     │                                          │
     │  {"v":1,"type":"generate_result",...}    │
     ├──────────────────────────────────────────┤
     │                                          │
```

Each message is a single line of JSON. The supervisor and worker both validate
against the Pydantic schema in `plugin_host/protocol.py`. A malformed message
is a hard error — no partial acceptance.

### Timeouts

| Timeout | Default | Where configured |
|---------|---------|------------------|
| Worker startup | 30s | `WORKER_STARTUP_TIMEOUT_SECONDS` |
| `LOAD_ADAPTER` acknowledgment | 30s | `WORKER_STARTUP_TIMEOUT_SECONDS` |
| `GENERATE` reply | 600s | `REQUEST_TIMEOUT_SECONDS` |
| Stream stall | 300s | `STREAM_STALL_TIMEOUT_SECONDS` |
| Graceful shutdown | 5s | `SHUTDOWN_TIMEOUT_SECONDS` |
| SIGTERM grace | 3s | `SIGTERM_GRACE_SECONDS` |

If your adapter hangs longer than 600s on a single request, the supervisor
kills it. If it hangs during streaming for more than 300s between chunks, it is
killed too.

### Worker startup

The worker is spawned **lazily** — on the first `generate()` call, not on
install. This is why installing a package does not start a subprocess.

The worker is reused for the rest of the session — the second, third, and
hundredth `generate()` calls all go to the same process. This gives you
connection pooling in the SDK for free.

---

## Common pitfalls

### Pitfall 1 — forgetting to close the SDK client

**Symptom**: after 100+ calls, `httpx` throws `RuntimeError: Event loop is
closed` or `Connection pool is full`.

**Fix**: implement `close()`:

```python
async def close(self) -> None:
    try:
        await self._client.aclose()
    except Exception:
        pass
```

The supervisor calls `close()` on shutdown. If you do not implement it, the
SDK's client leaks connections.

### Pitfall 2 — forgetting to yield `MESSAGE_COMPLETE`

**Symptom**: the chat hangs with no final event. The user sees tokens arriving
but no "Done" state.

**Fix**: ensure `stream()` always yields `MESSAGE_COMPLETE` at the end:

```python
async def stream(self, request):
    try:
        async for chunk in self._client.astream(chat):
            yield LLMStreamEvent(type=StreamEventType.CONTENT_DELTA, ...)
    finally:
        yield LLMStreamEvent(type=StreamEventType.MESSAGE_COMPLETE, finish_reason="stop")
```

The `finally` block ensures `MESSAGE_COMPLETE` fires even on cancellation.

### Pitfall 3 — catching `BaseException`

**Symptom**: the Stop button does nothing.

**Fix**: catch `Exception`, not `BaseException`. `asyncio.CancelledError`
inherits from `BaseException` (since Python 3.8) — you must let it propagate:

```python
# WRONG
except BaseException as exc:
    logger.error("Caught %s", exc)

# RIGHT
except Exception as exc:
    logger.error("Caught %s", exc)
```

### Pitfall 4 — raising instead of yielding

**Symptom**: streaming hangs.

**Fix**: inside `stream()`, do not `raise`. Yield a `PROVIDER_ERROR` event
instead:

```python
# WRONG
async def stream(self, request):
    async for chunk in sdk.stream():
        if error_condition:
            raise ProviderError("...")  # ← will hang the stream

# RIGHT
async def stream(self, request):
    try:
        async for chunk in sdk.stream():
            yield ...
    except Exception as exc:
        yield LLMStreamEvent(
            type=StreamEventType.PROVIDER_ERROR,
            error=self._map_exception(exc).to_dict(),
        )
```

### Pitfall 5 — not popping `_peldrun_plugin`

**Symptom**: `KeyError: '_peldrun_plugin'` or a JSON serialization error.

**Fix**: always remove the descriptor key:

```python
extra = dict(self._connection.extra_params or {})
extra.pop("_peldrun_plugin", None)
```

It is a Pydantic model — not JSON serializable. Passing it to the SDK's
`**kwargs` will raise.

### Pitfall 6 — hardcoded model name

**Symptom**: always uses `"GigaChat"` regardless of what the user selected.

**Fix**: read the model from `request.model_id`:

```python
# WRONG
chat = Chat(model="GigaChat", messages=messages)  # ← ignores user selection

# RIGHT
chat = Chat(model=request.model_id, messages=messages)
```

### Pitfall 7 — blocking the event loop

**Symptom**: the whole PELDRUN UI freezes during your request.

**Fix**: use async APIs everywhere. If the SDK only has sync APIs, wrap in
`asyncio.to_thread()`:

```python
# WRONG
response = self._client.chat(chat)  # ← sync call, blocks the loop

# RIGHT
response = await self._client.achat(chat)  # ← async
```

### Pitfall 8 — hidden dependency

**Symptom**: `ModuleNotFoundError: No module named 'X'` inside the worker.

**Fix**: add every import to `requirements.txt`. If your `adapter.py` imports
`requests`, but `requirements.txt` only lists `gigachat`, the worker will fail
to start.

**Verify**:

```bash
pip install -r requirements.txt
python -c "import gigachat_peldrun.adapter"
```

If this fails locally, it will fail inside the worker too.

### Pitfall 9 — forgetting `__init__.py`

**Symptom**: `ModuleNotFoundError: No module named 'gigachat_peldrun'`.

**Fix**: the module directory must contain `__init__.py`:

```
gigachat_peldrun/
├── __init__.py  ← empty file
└── adapter.py
```

---

## Checklist

Before uploading your package:

### Manifest

- [ ] `schema_version` is `"1.0.0"`
- [ ] `kind` is `"peldrun.llm_provider"`
- [ ] `id` matches `^[a-z0-9][a-z0-9._-]*$`
- [ ] `version` is valid semver
- [ ] `transport` is `"native"`
- [ ] `entry_point` resolves to a real file/class in your ZIP
- [ ] `connection_fields` declares every field the adapter reads
- [ ] `runtime_hints` declares every UI warning (`requires_custom_tls`, ...)
- [ ] `permissions.network` is `"connection_endpoint"`
- [ ] `permissions.workspace_access` is `false`
- [ ] `permissions.shell_execute` is `false`
- [ ] `dependencies` matches `requirements.txt`

### requirements.txt

- [ ] Every import in `adapter.py` is declared
- [ ] Every dependency is version-pinned
- [ ] The pinned versions install cleanly in a fresh venv

### adapter.py

- [ ] Declares `ADAPTER_STYLE = "v1"`
- [ ] Declares `API_CONTRACT = "peldrun.llm.provider.v1"`
- [ ] Declares `PROFILE = AdapterProfile(...)`
- [ ] Declares `provider_id = "<your-id>"`
- [ ] `__init__` reads all fields from `connection.extra_params`
- [ ] `__init__` pops `_peldrun_plugin`
- [ ] `generate()` returns `LLMResponse` with `content`, `finish_reason`, `usage`
- [ ] `stream()` yields `CONTENT_DELTA` for each token
- [ ] `stream()` yields exactly one `MESSAGE_COMPLETE`
- [ ] `stream()` yields `PROVIDER_ERROR` on failure
- [ ] `stream()` catches `Exception`, not `BaseException`
- [ ] `discover_models()` returns `[]` on failure (does not raise)
- [ ] `validate_connection()` returns `{ok: bool, message, latency_ms}` (does not raise)
- [ ] `close()` closes the SDK client
- [ ] Errors map to `ProviderError` subclasses (not raw SDK exceptions)

### Local test

- [ ] `python -c "from <module> import <class>"` works
- [ ] `test_adapter.py` runs and prints a successful response
- [ ] Streaming test shows tokens arriving one by one
- [ ] Error test with invalid credentials shows `ProviderAuthError`
- [ ] `discover_models()` returns at least one model
- [ ] `validate_connection()` returns `{ok: true, ...}`

### Package

- [ ] ZIP contains `manifest.json` at the root
- [ ] ZIP contains `requirements.txt` at the root
- [ ] ZIP contains the adapter module directory with `__init__.py`
- [ ] ZIP is under 100 MB
- [ ] `unzip -l` shows the expected structure

### Install & use

- [ ] Install succeeds
- [ ] Card appears in **Installed** tab
- [ ] Configure modal opens
- [ ] Connection creation succeeds
- [ ] Test button returns "Connected"
- [ ] Chat message receives a response
- [ ] Telemetry shows the invocation with token counts
- [ ] Streaming works (tokens appear one by one)

---

## What's next

- **Need the full manifest schema?** → [Manifest Reference](04-manifest-reference.md)
- **Need the complete adapter contract?** → [Adapter Protocol](06-adapter-protocol.md)
- **Worried about security?** → [Security Model](12-security-model.md)
- **Have a real provider to ship?** → [Examples](13-examples.md) for GigaChat,
  Yandex, AWS Bedrock, OpenRouter, Vertex
- **Something broken?** → [Troubleshooting](14-troubleshooting.md)
```

---
