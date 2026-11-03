### `docs/providers/13-examples.md`

```markdown
<!-- docs/providers/13-examples.md -->
# Examples

> **Complete, working Provider Packages** for real providers.
>
> **Reading time**: 25-30 minutes (or less if you only need one example).
>
> **Who this is for**: Anyone building a package — start with the example
> that most resembles your target provider.
>
> **How to use this document**:
> - Pick the example that matches your situation
> - Copy the manifest (and adapter, for native) to a new folder
> - Modify the provider-specific parts
> - Test locally
> - Package and install

---

## Table of Contents

- [Example index](#example-index)
- [Choosing the right example](#choosing-the-right-example)
- [Example 1 — DeepSeek (minimal compatible)](#example-1--deepseek-minimal-compatible)
- [Example 2 — OpenRouter (compatible with custom headers)](#example-2--openrouter-compatible-with-custom-headers)
- [Example 3 — Anthropic via compatible](#example-3--anthropic-via-compatible)
- [Example 4 — Ollama via compatible](#example-4--ollama-via-compatible)
- [Example 5 — GigaChat (full native, OAuth + custom TLS)](#example-5--gigachat-full-native-oauth--custom-tls)
- [Example 6 — YandexGPT (native, IAM token)](#example-6--yandexgpt-native-iam-token)
- [Example 7 — AWS Bedrock (native, SigV4 signing)](#example-7--aws-bedrock-native-sigv4-signing)
- [Example 8 — Azure OpenAI (native, deployment name)](#example-8--azure-openai-native-deployment-name)
- [Comparison table](#comparison-table)

---

## Example index

| # | Provider | Transport | Time to build | Complexity |
|---|----------|-----------|--------------|------------|
| 1 | DeepSeek | compatible | ~5 min | ⭐ |
| 2 | OpenRouter | compatible | ~10 min | ⭐⭐ |
| 3 | Anthropic | compatible (via base) | ~10 min | ⭐⭐ |
| 4 | Ollama | compatible (via base) | ~5 min | ⭐ |
| 5 | **GigaChat** | **native** | **~2 hours** | **⭐⭐⭐⭐⭐** |
| 6 | YandexGPT | native | ~1 hour | ⭐⭐⭐ |
| 7 | AWS Bedrock | native | ~2 hours | ⭐⭐⭐⭐ |
| 8 | Azure OpenAI | native | ~1 hour | ⭐⭐⭐ |

---

## Choosing the right example

### "My provider speaks OpenAI Chat Completions"

**Use**: [Example 1 — DeepSeek](#example-1--deepseek-minimal-compatible)

This is the pattern for the majority of modern cloud providers:
- DeepSeek
- Groq
- Mistral
- xAI (Grok)
- Together AI
- Fireworks AI
- Perplexity
- Anyscale
- vLLM (self-hosted)
- FastChat (self-hosted)
- LiteLLM Proxy
- LocalAI

**Time**: ~5 minutes. No Python code required.

### "My provider requires custom headers on every request"

**Use**: [Example 2 — OpenRouter](#example-2--openrouter-compatible-with-custom-headers)

Providers that need identification headers:
- OpenRouter (HTTP-Referer, X-Title)
- Some proxies (X-API-Version, X-Team-ID)

**Time**: ~10 minutes.

### "My provider speaks Anthropic Messages"

**Use**: [Example 3 — Anthropic](#example-3--anthropic-via-compatible)

Providers that mirror Anthropic's protocol:
- Anthropic (built-in)
- Claude API proxies (AWS Bedrock, GCP Vertex)
- Internal Anthropic-compatible gateways

**Time**: ~10 minutes.

### "My provider speaks Ollama's native protocol"

**Use**: [Example 4 — Ollama](#example-4--ollama-via-compatible)

Same for LM Studio, vLLM (OpenAI-compat), and any local server.

**Time**: ~5 minutes.

### "My provider has OAuth + custom TLS + Russian CA"

**Use**: [Example 5 — GigaChat](#example-5--gigachat-full-native-oauth--custom-tls)

The most complex example. This is the template for:
- GigaChat (Sber)
- YandexGPT (similar auth model)
- Any provider with a two-step OAuth flow + custom trust store

**Time**: ~2 hours.

### "My provider requires temporary IAM tokens"

**Use**: [Example 6 — YandexGPT](#example-6--yandexgpt-native-iam-token)

Providers with short-lived tokens:
- Yandex Cloud (IAM token, 12h)
- AWS STS (session token, 1h)
- Azure AD (access token, 1h)

**Time**: ~1 hour.

### "My provider requires request signing (SigV4)"

**Use**: [Example 7 — AWS Bedrock](#example-7--aws-bedrock-native-sigv4-signing)

Providers that require HMAC-SHA256 request signing:
- AWS Bedrock
- AWS S3
- Other AWS APIs

**Time**: ~2 hours.

### "My provider requires deployment names or versions"

**Use**: [Example 8 — Azure OpenAI](#example-8--azure-openai-native-deployment-name)

Providers with deployment-specific URLs:
- Azure OpenAI
- Azure AI Services
- Some enterprise proxies

**Time**: ~1 hour.

---

## Example 1 — DeepSeek (minimal compatible)

**DeepSeek** is a Chinese LLM provider that speaks the OpenAI Chat Completions
protocol. It requires only a base URL and an API key.

### When to use this pattern

Any provider that:
- Speaks the OpenAI Chat Completions API
- Uses `Authorization: Bearer <key>` authentication
- Does not need custom fields beyond URL and key

### Complete manifest.json

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "deepseek",
  "name": "DeepSeek",
  "display_name": "DeepSeek",
  "version": "1.0.0",
  "description": "DeepSeek offers competitive pricing on frontier-tier models including deepseek-chat and deepseek-reasoner.",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "compatible",
  "base_adapter": "openai_compat",
  "author": "PELDRUN Community",
  "author_url": "https://github.com/peldrun/peldrun-providers",
  "homepage": "https://platform.deepseek.com",
  "license": "MIT",
  "tags": ["deepseek", "cloud", "chinese", "cheap"],
  "connection_fields": [],
  "runtime_hints": {},
  "features": {
    "model_discovery": true,
    "native_streaming": true,
    "buffered_streaming": true,
    "tool_calling": true,
    "parallel_tool_calls": true,
    "forced_tool_choice": false,
    "vision": false,
    "audio": false,
    "structured_output": true,
    "usage_reporting": true,
    "reasoning": true
  },
  "permissions": {
    "network": "connection_endpoint",
    "secrets": ["connection.api_key"],
    "workspace_access": false,
    "shell_execute": false
  }
}
```

### Directory structure

```
deepseek-package/
└── manifest.json
```

### Build the ZIP

```bash
cd deepseek-package
zip deepseek-1.0.0.zip manifest.json
```

### Install

1. Open PELDRUN → **Store** → **Providers** → **Upload Package**
2. Select `deepseek-1.0.0.zip`
3. Click **Install Package**

### Configure

1. Click **Configure** on the DeepSeek card
2. Fill in:
   - **Connection Name**: `Personal`
   - **Endpoint URL**: `https://api.deepseek.com/v1`
   - **API Key**: `sk-...` (from platform.deepseek.com)
3. Click **Create Connection**

### Test

```bash
CONN_ID=$(curl -s http://localhost:8088/api/store/providers/deepseek/connections | jq -r '.connections[0].id')
curl -X POST http://localhost:8088/api/store/providers/deepseek/connections/$CONN_ID/test
```

Expected: `{"ok": true, "latency_ms": 245.5, ...}`

### Use

The provider is now available as `deepseek`. Open a chat, select DeepSeek,
pick a model (`deepseek-chat` or `deepseek-reasoner`), and send a message.

### What you get for free

- Streaming responses
- Tool calling (OpenAI-style)
- Token accounting
- Reasoning extraction (`deepseek-reasoner` returns `reasoning_content`)
- Model discovery
- Health checks
- Multi-connection support

**Total time**: ~5 minutes.

---

## Example 2 — OpenRouter (compatible with custom headers)

**OpenRouter** is an LLM aggregator providing access to 200+ models. It
requires OpenAI-protocol requests PLUS two custom headers used for ranking
identification.

### When to use this pattern

Any provider that:
- Speaks OpenAI Chat Completions
- Requires additional HTTP headers (identification, API version, team ID)

### Complete manifest.json

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "openrouter",
  "name": "OpenRouter",
  "display_name": "OpenRouter",
  "version": "1.0.0",
  "description": "Unified API for 200+ models from OpenAI, Anthropic, Google, Meta, and more. Pay per token, no subscription.",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "compatible",
  "base_adapter": "openai_compat",
  "author": "PELDRUN Community",
  "homepage": "https://openrouter.ai/docs",
  "license": "MIT",
  "tags": ["openrouter", "aggregator", "multi-provider"],
  "connection_fields": [
    {
      "key": "base_url",
      "label": "Base URL",
      "type": "url",
      "required": true,
      "default": "https://openrouter.ai/api/v1"
    },
    {
      "key": "api_key",
      "label": "API Key",
      "type": "password",
      "required": true,
      "placeholder": "sk-or-...",
      "description": "Generate a key at openrouter.ai/keys"
    },
    {
      "key": "http_referer",
      "label": "HTTP-Referer (optional)",
      "type": "url",
      "required": false,
      "default": "https://peldrun.app",
      "description": "Shown in OpenRouter's model rankings. Required for some features."
    },
    {
      "key": "x_title",
      "label": "App Title (optional)",
      "type": "string",
      "required": false,
      "default": "PELDRUN",
      "description": "Displayed as the app name in OpenRouter's leaderboard."
    }
  ],
  "runtime_hints": {
    "custom_headers_from_fields": {
      "http_referer": "HTTP-Referer",
      "x_title": "X-Title"
    },
    "docs_url": "https://openrouter.ai/docs"
  },
  "features": {
    "model_discovery": true,
    "native_streaming": true,
    "buffered_streaming": true,
    "tool_calling": true,
    "parallel_tool_calls": true,
    "forced_tool_choice": false,
    "vision": true,
    "audio": false,
    "structured_output": true,
    "usage_reporting": true,
    "reasoning": true
  },
  "permissions": {
    "network": "connection_endpoint",
    "secrets": ["connection.api_key"],
    "workspace_access": false,
    "shell_execute": false
  }
}
```

### How the custom headers work

The `runtime_hints.custom_headers_from_fields` block tells the runtime:

```json
{
  "http_referer": "HTTP-Referer",   // ← form field key → HTTP header name
  "x_title": "X-Title"
}
```

When a connection with `http_referer = "https://myapp.com"` is used, the
runtime adds `HTTP-Referer: https://myapp.com` to every outgoing request.

**This is the only hint with functional behavior** — see
[Runtime Hints](07-runtime-hints.md#custom_headers_from_fields) for details.

### Install & configure

Same as Example 1, plus filling in the two optional header fields if desired.

### Test & use

Same as Example 1.

**Total time**: ~10 minutes.

---

## Example 3 — Anthropic via compatible

**Anthropic** ships as a built-in adapter in PELDRUN. But you can also create a
`compatible` package that reuses the built-in Anthropic adapter — useful if you
want to add a custom endpoint or additional configuration.

### When to use this pattern

Any provider that:
- Speaks Anthropic Messages protocol
- Uses `x-api-key` + `anthropic-version` header authentication
- Is a proxy or gateway in front of real Anthropic

**Examples**:
- An internal Anthropic-compatible proxy
- AWS Bedrock (via native adapter — different transport)
- Anthropic on Azure (via Azure API Management)
- LiteLLM Proxy (Anthropic endpoint)

### Complete manifest.json

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "anthropic-proxy",
  "name": "Anthropic (Custom Proxy)",
  "display_name": "Anthropic Proxy",
  "version": "1.0.0",
  "description": "Connect to an Anthropic-compatible proxy or gateway (e.g., Azure APIM, LiteLLM, internal deployment).",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "compatible",
  "base_adapter": "anthropic",
  "author": "Your Org",
  "license": "Proprietary",
  "tags": ["anthropic", "claude", "proxy", "enterprise"],
  "connection_fields": [
    {
      "key": "base_url",
      "label": "Proxy Endpoint",
      "type": "url",
      "required": true,
      "placeholder": "https://your-proxy.example.com/v1",
      "description": "The base URL of your Anthropic-compatible proxy."
    },
    {
      "key": "api_key",
      "label": "API Key",
      "type": "password",
      "required": true,
      "description": "The API key configured on your proxy."
    },
    {
      "key": "anthropic_version",
      "label": "Anthropic API Version",
      "type": "string",
      "required": true,
      "default": "2023-06-01",
      "placeholder": "2023-06-01",
      "description": "Sent in the anthropic-version header. Usually 2023-06-01."
    }
  ],
  "runtime_hints": {
    "custom_headers_from_fields": {
      "anthropic_version": "anthropic-version"
    }
  },
  "features": {
    "model_discovery": true,
    "native_streaming": true,
    "buffered_streaming": true,
    "tool_calling": true,
    "vision": true,
    "usage_reporting": true,
    "reasoning": true
  },
  "permissions": {
    "network": "connection_endpoint",
    "secrets": ["connection.api_key"],
    "workspace_access": false,
    "shell_execute": false
  }
}
```

### How it works

1. The `base_adapter: "anthropic"` tells the runtime to delegate to the
   built-in Anthropic adapter.
2. The built-in adapter handles the Anthropic Messages protocol (system
   prompt, tool_use blocks, thinking, etc.).
3. The `custom_headers_from_fields` block adds the `anthropic-version` header
   automatically.
4. The connection's `base_url` overrides the default `https://api.anthropic.com`
   — useful for proxies.

### When to use this vs. the built-in

**Use the built-in Anthropic provider** (in LLM Settings → Cloud Providers) if:
- You are hitting `https://api.anthropic.com` directly
- You do not need custom connection management

**Use this compatible package** if:
- You have a proxy in front of Anthropic
- You want multiple connections (personal, work, etc.)
- You want the Store UI's unified interface

---

## Example 4 — Ollama via compatible

**Ollama** ships as a built-in provider in PELDRUN. This example shows how to
wrap it as a compatible package — useful when you want per-model configuration
without using the built-in LLM Settings UI.

### When to use this pattern

- Running Ollama on a non-default host
- Wanting multiple Ollama servers as separate connections
- Local-first deployment with no cloud dependency

### Complete manifest.json

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "ollama-proxy",
  "name": "Ollama (Custom Host)",
  "display_name": "Ollama",
  "version": "1.0.0",
  "description": "Connect to a remote or non-default Ollama instance. Reuses the built-in Ollama adapter.",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "compatible",
  "base_adapter": "ollama_native",
  "author": "PELDRUN Community",
  "homepage": "https://ollama.com",
  "license": "MIT",
  "tags": ["ollama", "local", "self-hosted"],
  "connection_fields": [
    {
      "key": "base_url",
      "label": "Ollama Server URL",
      "type": "url",
      "required": true,
      "default": "http://localhost:11434",
      "placeholder": "http://192.168.1.100:11434",
      "description": "The URL of your Ollama server. Supports LAN and remote hosts."
    }
  ],
  "runtime_hints": {},
  "features": {
    "model_discovery": true,
    "native_streaming": true,
    "buffered_streaming": true,
    "tool_calling": true,
    "vision": true,
    "usage_reporting": true,
    "reasoning": true
  },
  "permissions": {
    "network": "connection_endpoint",
    "secrets": [],
    "workspace_access": false,
    "shell_execute": false
  }
}
```

### Key observations

- **`secrets: []`** — Ollama does not require authentication on most deployments.
  If you have a reverse proxy requiring auth, add `"secrets": ["connection.api_key"]`.
- **`base_adapter: "ollama_native"`** — Uses the native Ollama adapter (supports
  `/api/chat`, thinking tokens, etc.).
- **No `api_key` field** — The implicit api_key field will be shown but can be
  left empty.

---

## Example 5 — GigaChat (full native, OAuth + custom TLS)

**GigaChat** is a Russian LLM from Sber. It requires:
- OAuth2 authentication (credentials = base64(client_id:client_secret))
- Different scopes (PERS, B2B, CORP)
- Custom TLS certificate (Russian Trusted Root CA, not in certifi)
- Custom protocol (not OpenAI-compatible)

This is the **most complex example** — if you can build this, you can build
anything.

### When to use this pattern

Any provider that requires:
- OAuth2 with client credentials
- Custom TLS certificate (not in certifi)
- Short-lived access tokens
- Custom wire protocol

**Similar providers**:
- Yandex Cloud (similar OAuth model)
- Sber Cloud (parent company of GigaChat)
- Other Russian / CIS enterprise APIs

### Package structure

```
gigachat-package/
├── manifest.json
├── requirements.txt
└── gigachat_peldrun/
    ├── __init__.py
    └── adapter.py
```

### Complete manifest.json

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "gigachat",
  "name": "GigaChat (Sber)",
  "display_name": "GigaChat",
  "version": "1.0.0",
  "description": "GigaChat is a family of Russian language models from Sber. Requires OAuth authentication and a custom CA bundle (Russian Trusted Root CA).",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "native",
  "entry_point": "gigachat_peldrun.adapter:GigaChatAdapter",
  "dependencies": ["gigachat>=0.2.0,<1.0"],
  "runtime": {
    "type": "python",
    "python": ">=3.12,<3.13"
  },
  "author": "PELDRUN Community",
  "author_url": "https://github.com/peldrun/peldrun-providers",
  "homepage": "https://developers.sber.ru/docs/ru/gigachat/guides/main",
  "license": "MIT",
  "tags": ["gigachat", "sber", "russian", "oauth", "enterprise"],
  "connection_fields": [
    {
      "key": "credentials",
      "label": "Authorization Key",
      "type": "password",
      "required": true,
      "placeholder": "Base64-encoded client_id:client_secret",
      "description": "From the Sber Studio dashboard. This is the 'Authorization key' for your GigaChat app. Format: base64(client_id + ':' + client_secret)."
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
      "description": "Which Sber API scope to authenticate against. B2B/CORP require a contract with Sber."
    },
    {
      "key": "ca_bundle_path",
      "label": "Russian Root CA Bundle",
      "type": "file",
      "required": false,
      "accept": ".pem,.crt,.cer",
      "placeholder": "/etc/ssl/certs/russian_trusted_root_ca.pem",
      "visible_if": {"scope": "B2B"},
      "description": "B2B and CORP scopes use a different CA that certifi does not include. Required for those scopes. Download from https://gu-st.ru/content/Other/doc/russiantrustedca.pem"
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

### Complete requirements.txt

```
gigachat>=0.2.0,<1.0
httpx>=0.27,<0.28
```

### Complete adapter.py

```python
# gigachat_peldrun/adapter.py
"""GigaChat adapter for PELDRUN."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from gigachat import GigaChat
from gigachat.exceptions import (
    AuthenticationError as GigaAuthError,
)
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
from peldrun.llm.request_compiler import compile_request


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
    ),
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

        # Read custom connection fields
        extra = dict(self._connection.extra_params or {})
        extra.pop("_peldrun_plugin", None)

        credentials = extra.get("credentials") or self._connection.api_key or ""
        scope = str(extra.get("scope") or "PERS")
        ca_bundle_path = extra.get("ca_bundle_path") or None
        verify_ssl = bool(extra.get("verify_ssl", True))

        if not credentials:
            raise ProviderAuthError(
                "GigaChat requires an 'credentials' field "
                "(base64-encoded client_id:client_secret).",
                provider=self.provider_id,
            )

        # Warn about masked placeholder
        if credentials == "••••••••":
            raise ProviderAuthError(
                "Credentials appear to be masked. Please re-enter them.",
                provider=self.provider_id,
            )

        # Construct the GigaChat client
        self._client = GigaChat(
            credentials=credentials,
            scope=scope,
            verify_ssl_certs=verify_ssl,
            ca_bundle_file=ca_bundle_path,
            model=str(extra.get("model") or "GigaChat"),
            timeout=float(self._connection.timeout or 60.0),
        )

        logger.debug(
            "GigaChatAdapter ready: scope=%s verify_ssl=%s ca_bundle=%s",
            scope, verify_ssl, ca_bundle_path or "<system>",
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
            raise ProviderAuthError(str(exc), provider=self.provider_id) from exc
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
        captured_usage: Optional[TokenUsage] = None
        finish_reason: Optional[str] = None

        try:
            chat = self._build_chat_payload(compiled)
            chat.stream = True

            async for chunk in self._client.astream(chat):
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

                # Finish reason
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                    usage = getattr(chunk, "usage", None)
                    if usage:
                        captured_usage = TokenUsage(
                            input_tokens=getattr(usage, "prompt_tokens", None),
                            output_tokens=getattr(usage, "completion_tokens", None),
                            total_tokens=getattr(usage, "total_tokens", None),
                            source=UsageSource.PROVIDER,
                        )

                    seq += 1
                    yield LLMStreamEvent(
                        type=StreamEventType.MESSAGE_COMPLETE,
                        finish_reason=finish_reason,
                        usage=captured_usage,
                        sequence=seq,
                    )
                    return

        except asyncio.CancelledError:
            try:
                await self._client.aclose()
            except Exception:
                pass
            raise
        except GigaAuthError as exc:
            seq += 1
            yield LLMStreamEvent(
                type=StreamEventType.PROVIDER_ERROR,
                error=ProviderAuthError(
                    str(exc), provider=self.provider_id
                ).to_dict(),
                sequence=seq,
            )
        except GigaChatException as exc:
            seq += 1
            yield LLMStreamEvent(
                type=StreamEventType.PROVIDER_ERROR,
                error=self._map_exception(exc).to_dict(),
                sequence=seq,
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

        results = []
        for model in models.data or []:
            results.append({
                "id": getattr(model, "id_", None) or getattr(model, "id", ""),
                "display_name": getattr(model, "name", None),
                "max_context_length": getattr(model, "context_length", None),
                "type": "llm",
            })
        return results

    # ------------------------------------------------------------------
    # validate_connection()
    # ------------------------------------------------------------------

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
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

    def _build_chat_payload(self, compiled: Any) -> Chat:
        messages: List[Messages] = []

        # System prompt
        if compiled.system_prompt:
            messages.append(
                Messages(
                    role=MessagesRole.SYSTEM,
                    content=compiled.system_prompt,
                )
            )

        # Conversation
        for msg in compiled.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Flatten multimodal content to text (GigaChat SDK v0.2
            # does not support image parts in Chat.messages yet)
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )

            role_map = {
                "user": MessagesRole.USER,
                "assistant": MessagesRole.ASSISTANT,
                "system": MessagesRole.SYSTEM,
            }
            messages.append(
                Messages(
                    role=role_map.get(role, MessagesRole.USER),
                    content=str(content or ""),
                )
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
        if not result.choices:
            return LLMResponse(
                content="",
                finish_reason="stop",
                model=model,
                raw=result,
            )

        choice = result.choices[0]
        content = choice.message.content or ""
        usage = getattr(result, "usage", None)

        return LLMResponse(
            content=content,
            finish_reason=choice.finish_reason,
            usage=TokenUsage(
                input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
                output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
                total_tokens=getattr(usage, "total_tokens", None) if usage else None,
                source=UsageSource.PROVIDER if usage else UsageSource.UNKNOWN,
            ),
            model=model,
            raw=result,
        )

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

### Empty `__init__.py`

```python
# gigachat_peldrun/__init__.py
"""GigaChat provider adapter for PELDRUN."""
```

### Local testing

Before packaging, test the adapter directly:

```python
# test_adapter.py
import asyncio
from peldrun.llm.contracts import ConnectionContext
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
        messages=[{"role": "user", "content": "Привет! Как дела?"}],
        generation=GenerationConfig(temperature=0.7, max_output_tokens=100),
        reasoning=ReasoningConfig(),
        response_format=ResponseFormat(),
        tool_choice=ToolChoiceSpec(),
        connection=connection,
    )

    # Non-streaming test
    response = await adapter.generate(request)
    print("Response:", response.content)
    print("Tokens:", response.usage)

    # Streaming test
    print("\nStreaming:")
    async for event in adapter.stream(request):
        if event.type.value == "content_delta":
            print(event.content_delta, end="", flush=True)
    print()

    await adapter.close()


asyncio.run(main())
```

### Install the CA bundle (Linux)

```bash
# Download the Russian Trusted Root CA
sudo wget https://gu-st.ru/content/Other/doc/russiantrustedca.pem \
  -O /etc/ssl/certs/russian_trusted_root_ca.pem

# Make it readable by PELDRUN
sudo chmod 644 /etc/ssl/certs/russian_trusted_root_ca.pem
```

### Install the CA bundle (Windows)

```powershell
# Download the CA bundle
Invoke-WebRequest -Uri "https://gu-st.ru/content/Other/doc/russiantrustedca.pem" `
  -OutFile "C:\Certs\russian_trusted_root_ca.pem"
```

### Build the ZIP

```bash
cd gigachat-package
zip -r gigachat-1.0.0.zip \
    manifest.json \
    requirements.txt \
    gigachat_peldrun/
```

### Install, configure, test

1. Upload `gigachat-1.0.0.zip` via **Store → Providers → Upload Package**
2. Wait for pip install (~30-60s)
3. Configure the connection:
   - **Connection Name**: `Personal`
   - **Authorization Key**: your base64 credentials
   - **Account Scope**: `Personal (PERS)` (or `Business (B2B)` if you have a contract)
   - **Verify SSL**: ✓ (checked)
4. Test → expected `{"ok": true, ...}`
5. Use in a chat

**Total time**: ~2 hours (mostly writing the adapter).

---

## Example 6 — YandexGPT (native, IAM token)

**YandexGPT** is Yandex Cloud's LLM. It requires:
- A Yandex Cloud folder ID
- A temporary IAM token (expires every 12 hours)
- A custom protocol (not OpenAI-compatible)

### When to use this pattern

Any provider that:
- Requires short-lived tokens (12h, 1h, etc.)
- Requires a folder/project/tenant ID
- Has its own wire protocol

**Similar providers**:
- GigaChat (with different auth flow)
- Azure OpenAI (with Azure AD auth)
- Enterprise SaaS APIs

### Complete manifest.json

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "yandexgpt",
  "name": "YandexGPT",
  "display_name": "YandexGPT",
  "version": "1.0.0",
  "description": "YandexGPT is a family of Russian language models from Yandex Cloud. Requires a folder ID and a temporary IAM token.",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "native",
  "entry_point": "yandex_peldrun.adapter:YandexGPTAdapter",
  "dependencies": ["httpx>=0.27,<0.28"],
  "runtime": {
    "type": "python",
    "python": ">=3.12,<3.13"
  },
  "author": "PELDRUN Community",
  "homepage": "https://cloud.yandex.com/docs/foundation-models",
  "license": "MIT",
  "tags": ["yandex", "yandexgpt", "russian", "enterprise", "iam"],
  "connection_fields": [
    {
      "key": "base_url",
      "label": "API Endpoint",
      "type": "url",
      "required": true,
      "default": "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    },
    {
      "key": "folder_id",
      "label": "Folder ID",
      "type": "string",
      "required": true,
      "placeholder": "b1g...",
      "description": "Your Yandex Cloud folder ID. Found in the Yandex Cloud Console."
    },
    {
      "key": "iam_token",
      "label": "IAM Token",
      "type": "password",
      "required": true,
      "description": "Temporary IAM token. Generate at cloud.yandex.com/en/docs/iam/operations/iam-token/create. Expires in 12 hours."
    }
  ],
  "runtime_hints": {
    "requires_token_refresh": true,
    "token_lifetime_seconds": 43200,
    "docs_url": "https://cloud.yandex.com/docs/foundation-models"
  },
  "features": {
    "model_discovery": false,
    "native_streaming": true,
    "buffered_streaming": true,
    "tool_calling": false,
    "vision": false,
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

### Complete adapter.py

```python
# yandex_peldrun/adapter.py
"""YandexGPT adapter for PELDRUN."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from peldrun.llm.client import (
    LLMResponse,
    TokenUsage,
    UsageSource,
)
from peldrun.llm.contracts import (
    AdapterFeatures,
    AdapterProfile,
    ConnectionContext,
    LLMStreamEvent,
    StreamEventType,
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


logger = logging.getLogger("yandex_peldrun.adapter")


_PROFILE = AdapterProfile(
    provider_id="yandexgpt",
    api_surface="chat_completions",
    features=AdapterFeatures(
        model_discovery=False,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=False,
        vision=False,
        usage_reporting=True,
    ),
)


class YandexGPTAdapter:
    """Native YandexGPT adapter."""

    ADAPTER_STYLE = "v1"
    API_CONTRACT = "peldrun.llm.provider.v1"
    PROFILE = _PROFILE
    provider_id = "yandexgpt"

    def __init__(self, context: Any) -> None:
        self._connection: ConnectionContext = context.connection
        self._profile = context.profile or _PROFILE
        self.profile = self._profile

        extra = dict(self._connection.extra_params or {})
        extra.pop("_peldrun_plugin", None)

        self._base_url = (
            self._connection.base_url
            or "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
        )
        self._folder_id = str(extra.get("folder_id") or "")
        self._iam_token = (
            extra.get("iam_token")
            or self._connection.api_key
            or ""
        )

        if not self._folder_id:
            raise ProviderAuthError(
                "YandexGPT requires a 'folder_id' field.",
                provider=self.provider_id,
            )
        if not self._iam_token:
            raise ProviderAuthError(
                "YandexGPT requires an 'iam_token' field.",
                provider=self.provider_id,
            )

        self._timeout = float(self._connection.timeout or 60.0)
        self._http = httpx.AsyncClient(timeout=self._timeout)

        logger.debug(
            "YandexGPTAdapter ready: folder_id=%s",
            self._folder_id[:8] + "...",
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._iam_token}",
            "x-folder-id": self._folder_id,
        }

    def _build_payload(self, compiled: Any, stream: bool = False) -> Dict[str, Any]:
        messages = []
        for msg in compiled.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )

            # Yandex uses "assistant" not "model", "system" is supported
            messages.append({"role": role, "text": str(content or "")})

        return {
            "modelUri": f"gpt://{self._folder_id}/{compiled.model}/latest",
            "completionOptions": {
                "stream": stream,
                "temperature": compiled.generation.get("temperature", 0.7),
                "maxTokens": str(compiled.generation.get("max_tokens") or 1000),
            },
            "messages": messages,
        }

    # ------------------------------------------------------------------
    # generate()
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        compiled = compile_request(request, self._profile)
        try:
            payload = self._build_payload(compiled, stream=False)
            response = await self._http.post(
                self._base_url,
                headers=self._headers(),
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                str(exc), provider=self.provider_id, phase="request"
            ) from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(
                str(exc), provider=self.provider_id
            ) from exc

        if response.status_code == 401:
            raise ProviderAuthError(
                "Yandex IAM token is invalid or expired.",
                provider=self.provider_id,
            )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                "Yandex rate limit exceeded.",
                provider=self.provider_id,
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"Yandex returned HTTP {response.status_code}.",
                provider=self.provider_id,
            )
        if response.status_code != 200:
            raise ProviderInvalidRequestError(
                f"HTTP {response.status_code}: {response.text[:200]}",
                provider=self.provider_id,
            )

        data = response.json()
        result = data.get("result", {})
        alternatives = result.get("alternatives", [])
        if not alternatives:
            return LLMResponse(
                content="",
                finish_reason="stop",
                model=compiled.model,
                raw=data,
            )

        content = alternatives[0].get("message", {}).get("text", "")
        usage_info = result.get("usage", {})

        return LLMResponse(
            content=content,
            finish_reason=alternatives[0].get("status", "stop").lower(),
            usage=TokenUsage(
                input_tokens=int(usage_info.get("inputTextTokens", 0)) or None,
                output_tokens=int(usage_info.get("completionTokens", 0)) or None,
                total_tokens=int(usage_info.get("totalTokens", 0)) or None,
                source=UsageSource.PROVIDER,
            ),
            model=compiled.model,
            raw=data,
        )

    # ------------------------------------------------------------------
    # stream()
    # ------------------------------------------------------------------

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        compiled = compile_request(request, self._profile)
        payload = self._build_payload(compiled, stream=True)
        seq = 0
        finish_reason = None
        usage: Optional[TokenUsage] = None

        try:
            async with self._http.stream(
                "POST",
                self._base_url,
                headers=self._headers(),
                json=payload,
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise ProviderError(
                        f"Yandex streaming returned HTTP {response.status_code}: {body[:200]}",
                        provider=self.provider_id,
                    )

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    result = chunk.get("result", {})
                    alternatives = result.get("alternatives", [])
                    if not alternatives:
                        continue

                    alt = alternatives[0]
                    msg = alt.get("message", {})
                    text = msg.get("text", "")
                    if text:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.CONTENT_DELTA,
                            content_delta=text,
                            sequence=seq,
                        )

                    status = alt.get("status")
                    if status:
                        finish_reason = status.lower()

                    usage_info = result.get("usage")
                    if usage_info:
                        usage = TokenUsage(
                            input_tokens=int(usage_info.get("inputTextTokens", 0)) or None,
                            output_tokens=int(usage_info.get("completionTokens", 0)) or None,
                            total_tokens=int(usage_info.get("totalTokens", 0)) or None,
                            source=UsageSource.PROVIDER,
                        )

        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except Exception as exc:
            seq += 1
            yield LLMStreamEvent(
                type=StreamEventType.PROVIDER_ERROR,
                error=ProviderError(
                    str(exc), provider=self.provider_id
                ).to_dict(),
                sequence=seq,
            )
            return

        seq += 1
        yield LLMStreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason or "stop",
            usage=usage,
            sequence=seq,
        )

    # ------------------------------------------------------------------
    # discover_models()
    # ------------------------------------------------------------------

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        # YandexGPT does not expose a discovery endpoint.
        # Return the known model families.
        return [
            {"id": "yandexgpt", "display_name": "YandexGPT Pro", "type": "llm"},
            {"id": "yandexgpt-lite", "display_name": "YandexGPT Lite", "type": "llm"},
            {"id": "summarization", "display_name": "Yandex Summarization", "type": "llm"},
        ]

    # ------------------------------------------------------------------
    # validate_connection()
    # ------------------------------------------------------------------

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        started = time.time()
        try:
            # Send a minimal 1-token request
            payload = {
                "modelUri": f"gpt://{self._folder_id}/yandexgpt-lite/latest",
                "completionOptions": {"maxTokens": "1", "stream": False},
                "messages": [{"role": "user", "text": "hi"}],
            }
            response = await self._http.post(
                self._base_url,
                headers=self._headers(),
                json=payload,
            )
            latency_ms = round((time.time() - started) * 1000.0, 2)
            if response.status_code == 200:
                return {
                    "ok": True,
                    "message": "YandexGPT API reachable.",
                    "latency_ms": latency_ms,
                }
            return {
                "ok": False,
                "message": f"HTTP {response.status_code}: {response.text[:200]}",
                "latency_ms": latency_ms,
                "status_code": response.status_code,
            }
        except Exception as exc:
            return {
                "ok": False,
                "message": str(exc),
                "latency_ms": round((time.time() - started) * 1000.0, 2),
            }

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        return None

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception:
            pass
```

### Key observations

- **`requires_token_refresh: true`** — UI warns the user that the IAM token
  expires in 12 hours.
- **`x-folder-id` header** — Yandex requires the folder ID in a header, not
  in the URL.
- **Model URI format** — `gpt://{folder_id}/{model}/latest`. Unusual but
  well-documented.
- **`discover_models()` returns a static list** — Yandex does not expose a
  discovery endpoint. The adapter returns the known model families.

**Total time**: ~1 hour (simpler than GigaChat — no OAuth, no custom TLS).

---

## Example 7 — AWS Bedrock (native, SigV4 signing)

**AWS Bedrock** requires HMAC-SHA256 request signing (SigV4), which is
significantly more complex than a Bearer token.

### When to use this pattern

Any AWS API (Bedrock, S3, Comprehend) — all use SigV4.

### Complete manifest.json (excerpt)

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "aws-bedrock",
  "name": "AWS Bedrock",
  "display_name": "AWS Bedrock",
  "version": "1.0.0",
  "description": "AWS Bedrock provides access to foundation models from Anthropic, Meta, Mistral, and Amazon, with enterprise security.",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "native",
  "entry_point": "bedrock_peldrun.adapter:BedrockAdapter",
  "dependencies": ["boto3>=1.34,<2.0", "botocore>=1.34,<2.0"],
  "connection_fields": [
    {
      "key": "region",
      "label": "AWS Region",
      "type": "select",
      "required": true,
      "default": "us-east-1",
      "options": [
        {"value": "us-east-1", "label": "US East (N. Virginia)"},
        {"value": "us-west-2", "label": "US West (Oregon)"},
        {"value": "eu-west-1", "label": "EU (Ireland)"},
        {"value": "ap-southeast-1", "label": "Asia Pacific (Singapore)"}
      ]
    },
    {
      "key": "access_key_id",
      "label": "AWS Access Key ID",
      "type": "string",
      "required": true,
      "placeholder": "AKIA..."
    },
    {
      "key": "secret_access_key",
      "label": "AWS Secret Access Key",
      "type": "password",
      "required": true
    },
    {
      "key": "session_token",
      "label": "Session Token (optional)",
      "type": "password",
      "required": false,
      "description": "Required only for temporary STS credentials."
    }
  ],
  "runtime_hints": {
    "requires_token_refresh": true,
    "token_lifetime_seconds": 3600
  },
  "permissions": {
    "network": "connection_endpoint",
    "secrets": ["connection.api_key"],
    "workspace_access": false,
    "shell_execute": false
  }
}
```

### Adapter highlights

```python
# bedrock_peldrun/adapter.py (highlights)
import boto3
from botocore.exceptions import ClientError

class BedrockAdapter:
    def __init__(self, context):
        extra = dict(context.connection.extra_params or {})
        extra.pop("_peldrun_plugin", None)

        # boto3 handles SigV4 signing internally
        session_kwargs = {
            "aws_access_key_id": extra.get("access_key_id"),
            "aws_secret_access_key": extra.get("secret_access_key"),
            "region_name": extra.get("region", "us-east-1"),
        }
        if extra.get("session_token"):
            session_kwargs["aws_session_token"] = extra["session_token"]

        session = boto3.Session(**session_kwargs)
        self._client = session.client("bedrock-runtime")

    async def generate(self, request):
        compiled = compile_request(request, self._profile)

        # Use boto3's converse API (unified across all Bedrock models)
        try:
            response = await asyncio.to_thread(
                self._client.converse,
                modelId=compiled.model,
                messages=self._convert_messages(compiled.messages),
                inferenceConfig={
                    "temperature": compiled.generation.get("temperature", 0.7),
                    "maxTokens": compiled.generation.get("max_tokens", 1000),
                },
                system=[{"text": compiled.system_prompt}] if compiled.system_prompt else [],
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "AccessDeniedException":
                raise ProviderAuthError(str(exc), provider=self.provider_id)
            if error_code == "ThrottlingException":
                raise ProviderRateLimitError(str(exc), provider=self.provider_id)
            raise ProviderError(str(exc), provider=self.provider_id)

        output = response.get("output", {})
        message = output.get("message", {})
        content = "".join(
            block.get("text", "") for block in message.get("content", [])
        )
        usage = response.get("usage", {})

        return LLMResponse(
            content=content,
            finish_reason=response.get("stopReason", "stop").lower(),
            usage=TokenUsage(
                input_tokens=usage.get("inputTokens"),
                output_tokens=usage.get("outputTokens"),
                total_tokens=(usage.get("inputTokens", 0) + usage.get("outputTokens", 0)),
                source=UsageSource.PROVIDER,
            ),
            model=compiled.model,
            raw=response,
        )
```

### Why `asyncio.to_thread`

`boto3` is **synchronous**. Wrapping each call in `asyncio.to_thread` keeps
the event loop responsive. Never call `boto3` methods directly from `async def`.

### Key observations

- **Region selection** — Bedrock is region-specific.
- **`session_token`** — For temporary STS credentials.
- **`asyncio.to_thread`** — `boto3` is sync; must not block the event loop.
- **`converse` API** — Works uniformly across all Bedrock models (Claude,
  Llama, Mistral, Titan). Use this instead of model-specific APIs.

**Total time**: ~2 hours (mostly reading boto3 docs).

---

## Example 8 — Azure OpenAI (native, deployment name)

**Azure OpenAI** has a different URL structure and requires a deployment
name instead of a model name in the URL path.

### Complete manifest.json (excerpt)

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "azure-openai",
  "name": "Azure OpenAI",
  "display_name": "Azure OpenAI",
  "version": "1.0.0",
  "description": "Azure OpenAI hosts OpenAI models on Microsoft Azure with enterprise compliance, VNET integration, and regional data residency.",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "native",
  "entry_point": "azure_peldrun.adapter:AzureOpenAIAdapter",
  "dependencies": ["openai>=1.0,<2.0"],
  "connection_fields": [
    {
      "key": "base_url",
      "label": "Azure Endpoint",
      "type": "url",
      "required": true,
      "placeholder": "https://YOUR-RESOURCE.openai.azure.com",
      "description": "Your Azure OpenAI resource endpoint. Found in the Azure portal."
    },
    {
      "key": "api_key",
      "label": "API Key",
      "type": "password",
      "required": true,
      "description": "Key from the Azure portal (Keys and Endpoint blade)."
    },
    {
      "key": "api_version",
      "label": "API Version",
      "type": "string",
      "required": true,
      "default": "2024-02-01",
      "placeholder": "2024-02-01",
      "pattern": "^\\d{4}-\\d{2}-\\d{2}(-preview)?$"
    },
    {
      "key": "deployment_name",
      "label": "Deployment Name",
      "type": "string",
      "required": true,
      "description": "The name of your model deployment in Azure. This replaces the model name in API calls."
    }
  ],
  "permissions": {
    "network": "connection_endpoint",
    "secrets": ["connection.api_key"],
    "workspace_access": false,
    "shell_execute": false
  }
}
```

### Adapter highlights

```python
# azure_peldrun/adapter.py (highlights)
from openai import AsyncAzureOpenAI

class AzureOpenAIAdapter:
    def __init__(self, context):
        extra = dict(context.connection.extra_params or {})
        extra.pop("_peldrun_plugin", None)

        self._deployment_name = str(extra.get("deployment_name") or "")
        if not self._deployment_name:
            raise ProviderAuthError(
                "Azure OpenAI requires a 'deployment_name'.",
                provider="azure-openai",
            )

        self._client = AsyncAzureOpenAI(
            api_key=context.connection.api_key,
            api_version=str(extra.get("api_version") or "2024-02-01"),
            azure_endpoint=context.connection.base_url,
        )

    async def generate(self, request):
        compiled = compile_request(request, self._profile)

        # Use DEPLOYMENT NAME instead of model name
        response = await self._client.chat.completions.create(
            model=self._deployment_name,   # ← not compiled.model!
            messages=compiled.messages,
            temperature=compiled.generation.get("temperature", 0.7),
            max_tokens=compiled.generation.get("max_tokens"),
        )

        choice = response.choices[0]
        return LLMResponse(
            content=choice.message.content or "",
            finish_reason=choice.finish_reason,
            usage=TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                source=UsageSource.PROVIDER,
            ),
            model=compiled.model,
            raw=response,
        )
```

### Key observations

- **`AsyncAzureOpenAI`** — The official OpenAI SDK has a dedicated class for
  Azure. It handles the deployment-name substitution and API version
  automatically.
- **`deployment_name` replaces `model`** — The URL contains the deployment
  name, not the model name. This is the key difference from the standard
  OpenAI API.
- **`api_version` regex** — Enforced client-side by the pattern, but validated
  server-side by Azure.

**Total time**: ~1 hour.

---

## Comparison table

| Provider | Transport | Auth | Custom fields | Complexity |
|----------|-----------|------|---------------|------------|
| DeepSeek | compatible | Bearer key | 0 | ⭐ |
| OpenRouter | compatible | Bearer + 2 headers | 2 | ⭐⭐ |
| Anthropic (proxy) | compatible | x-api-key + version | 1 | ⭐⭐ |
| Ollama (proxy) | compatible | none | 0 | ⭐ |
| **GigaChat** | **native** | **OAuth + custom TLS** | **4** | **⭐⭐⭐⭐⭐** |
| YandexGPT | native | IAM token + folder | 3 | ⭐⭐⭐ |
| AWS Bedrock | native | SigV4 (boto3) | 4 | ⭐⭐⭐⭐ |
| Azure OpenAI | native | API key + version | 4 | ⭐⭐⭐ |

### Picking your starting point

- **Simplest**: DeepSeek or Ollama — copy the manifest, change the id and URL,
  done.
- **Most common**: OpenRouter or Anthropic — shows the custom-headers pattern.
- **Most complex**: GigaChat — shows OAuth, custom TLS, and full adapter code.
- **Learning adapter patterns**: GigaChat (full flow) or AWS Bedrock (SDK
  integration).

---

## What's next

- **Ready to build your own?** → [Quickstart — Compatible](02-quickstart-compatible.md) or [Quickstart — Native](03-quickstart-native.md)
- **Need the manifest reference?** → [Manifest Reference](04-manifest-reference.md)
- **Need to understand adapter contracts?** → [Adapter Protocol](06-adapter-protocol.md)
- **Hitting an error?** → [Troubleshooting](14-troubleshooting.md)
```

---
