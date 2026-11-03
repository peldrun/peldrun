### `docs/providers/07-runtime-hints.md`

```markdown
<!-- docs/providers/07-runtime-hints.md -->
# Runtime Hints — Complete Reference

> **Complete reference** for `runtime_hints` in `manifest.json`.
>
> **Reading time**: 10-12 minutes.
>
> **Who this is for**: Package authors who want to warn users about special
> requirements before they save a connection.
>
> **What you'll learn**: What runtime hints are, what they do (and do not do),
> the seven available hints, and when to use each one.

---

## Table of Contents

- [What runtime hints are](#what-runtime-hints-are)
- [What runtime hints are NOT](#what-runtime-hints-are-not)
- [Where hints appear in the UI](#where-hints-appear-in-the-ui)
- [Hint reference](#hint-reference)
  - [requires_custom_tls](#requires_custom_tls)
  - [requires_mtls](#requires_mtls)
  - [requires_token_refresh](#requires_token_refresh)
  - [token_lifetime_seconds](#token_lifetime_seconds)
  - [supports_env_vars](#supports_env_vars)
  - [docs_url](#docs_url)
  - [custom_headers_from_fields](#custom_headers_from_fields)
- [Real-world examples](#real-world-examples)
- [Best practices](#best-practices)
- [Anti-patterns](#anti-patterns)
- [Complete reference table](#complete-reference-table)

---

## What runtime hints are

`runtime_hints` is an **optional object** in `manifest.json` that declares
**non-functional, UI-only information** about your provider. Its purpose is to
warn users about special requirements *before* they save a connection.

### The problem they solve

Consider GigaChat. It requires a custom CA bundle because certifi (the default
Python TLS trust store) does not include the Russian Trusted Root CA. If we did
not warn the user, they would:

1. Install the GigaChat package
2. Fill in credentials
3. Get a cryptic SSL error on every chat
4. Spend 30 minutes searching GitHub issues
5. Finally discover they need a CA bundle
6. Realize the field exists in the form

With a runtime hint:

1. Install the GigaChat package
2. Open Configure → see a red banner: *"This provider requires a custom TLS
   certificate. Provide the CA bundle path below."*
3. Fill in the CA bundle path (because the form shows it)
4. Success

Runtime hints close the gap between "the adapter needs X" and "the user knows
they need X".

### Where they live

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "gigachat",
  "...": "...",
  "runtime_hints": {
    "requires_custom_tls": true,
    "requires_token_refresh": true,
    "token_lifetime_seconds": 1800,
    "docs_url": "https://developers.sber.ru/docs/ru/gigachat"
  },
  "permissions": { "...": "..." }
}
```

The `runtime_hints` object sits alongside `connection_fields`, `features`, and
`permissions` at the top level.

---

## What runtime hints are NOT

To avoid scope creep, here is what runtime hints **cannot** do:

| What you might expect | What actually happens |
|----------------------|----------------------|
| Enforce that the user provides a CA bundle | ❌ Only a warning banner appears; the adapter must raise `ProviderAuthError` if the field is missing |
| Automatically refresh expired tokens | ❌ Only a warning banner appears; the adapter must handle token refresh in code |
| Add headers to outgoing requests | ⚠️ Only `custom_headers_from_fields` does this (see below) — the rest are purely advisory |
| Change the wire format | ❌ Never. The adapter owns the wire format completely |
| Enable or disable features | ❌ Never. That's what `features` and `permissions` are for |
| Validate connection field values | ❌ Never. Client-side validation is via `pattern` / `required`; server-side validation is the adapter's job |
| Affect capability resolution | ❌ Never. Hints are UI metadata only |

**Golden rule**: If you want a behavior, implement it in the adapter. If you
want a warning, declare a hint.

### Why this design?

Three reasons:

1. **Hints cannot break the runtime.** A malformed hint cannot cause a chat
   to fail. If the UI ignores a hint (e.g., a future field is added and an
   older UI does not recognize it), the connection still works — just without
   the warning.

2. **Adapters remain the source of truth.** The adapter is the only code that
   sees the actual connection data. If a hint says "requires custom TLS" but
   the adapter accepts plain HTTP, the hint is wrong and the adapter is
   correct. This prevents "ghost" behaviors that exist only in the manifest.

3. **The system is forward-compatible.** A future PELDRUN version may add a
   hint the current version does not understand. Older UIs ignore it, newer
   UIs display it. No migration needed.

---

## Where hints appear in the UI

Different hints surface in different places. Here is a map:

| Hint | Where it appears |
|------|------------------|
| `requires_custom_tls` | Configure modal → warning banner at the top |
| `requires_mtls` | Configure modal → warning banner at the top |
| `requires_token_refresh` | Configure modal → info banner |
| `token_lifetime_seconds` | Configure modal → part of the info banner ("tokens expire every 30 min") |
| `supports_env_vars` | Details drawer → "Supports environment variables" badge |
| `docs_url` | Configure modal → small "Docs" link |
| `custom_headers_from_fields` | Not shown in UI — silently applied to outgoing requests |

### The Configure modal with hints

Here is what the top of the Configure modal looks like when
`requires_custom_tls: true`:

```
┌─────────────────────────────────────────────────────────────┐
│  ⚠  This provider requires a custom TLS certificate         │
│     The package declares that certifi does not include the   │
│     root CA this provider uses. Provide the CA bundle file   │
│     path in the connection fields.                          │
│                                                     [Docs →]│
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Authorization Key                                          │
│  [••••••••••••••••••••••••••••]                             │
│                                                             │
│  Account Scope                                              │
│  [Business (B2B)              ▼]                            │
│                                                             │
│  Russian Root CA Bundle                                     │
│  [/etc/ssl/certs/ca.pem                                  ]  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

The banner is amber-colored, with a shield icon, and the "Docs" link comes from
`docs_url`.

---

## Hint reference

### `requires_custom_tls`

**Type**: `boolean`
**Default**: `false`

**Purpose**: Declares that the provider's TLS certificates are signed by a CA
that is not in the system trust store (certifi). The user must provide a
custom CA bundle.

**When to use it**:

- The provider is a Russian service (GigaChat, Yandex) that uses the Russian
  Trusted Root CA, which certifi does not include.
- The provider uses a self-signed or internal CA (on-prem deployments).
- The provider uses a newer root CA that was added to the system after the
  installed certifi version was released.

**What the UI does**:
- Displays an amber warning banner at the top of the Configure modal.
- Includes the `docs_url` link if `docs_url` is set.

**What the adapter must do**: Nothing special. The adapter is responsible for
opening the CA bundle file (via `ca_bundle_path` or similar). The hint is only
for the UI.

**Example**:

```json
"runtime_hints": {
  "requires_custom_tls": true
}
```

**Paired with connection field**:

```json
"connection_fields": [
  {
    "key": "ca_bundle_path",
    "label": "CA Bundle Path",
    "type": "file",
    "required": true,
    "accept": ".pem,.crt,.cer",
    "description": "Absolute path on the PELDRUN host to a PEM file containing the root CA."
  }
]
```

**Adapter pattern**:

```python
ca_bundle_path = extra.get("ca_bundle_path")
if not ca_bundle_path:
    raise ProviderAuthError(
        "GigaChat requires a CA bundle path for TLS.",
        provider="gigachat",
    )
self._client = MyClient(
    ca_bundle_file=ca_bundle_path,
    verify_ssl=True,
)
```

### `requires_mtls`

**Type**: `boolean`
**Default**: `false`

**Purpose**: Declares that the provider requires mutual TLS (client
certificates). The user must provide a client certificate, a private key, and
optionally a CA bundle for verifying the server.

**When to use it**:

- The provider is an enterprise on-prem deployment that uses mTLS.
- The provider's documentation explicitly requires client certificates for API
  access.

**What the UI does**:
- Displays a warning banner: *"This provider requires mutual TLS. Provide your
  client certificate and private key below."*

**What the adapter must do**:

```python
client_cert_path = extra.get("client_cert_path")
client_key_path = extra.get("client_key_path")

if not client_cert_path or not client_key_path:
    raise ProviderAuthError(
        "Mutual TLS requires both a client certificate and a private key.",
        provider="my-provider",
    )

self._client = MyClient(
    cert=(client_cert_path, client_key_path),
    verify=True,
)
```

**Example**:

```json
"runtime_hints": {
  "requires_mtls": true
}
```

**With fields**:

```json
"connection_fields": [
  {
    "key": "client_cert_path",
    "label": "Client Certificate",
    "type": "secret_file",
    "required": true,
    "accept": ".pem,.crt"
  },
  {
    "key": "client_key_path",
    "label": "Client Private Key",
    "type": "secret_file",
    "required": true,
    "accept": ".key,.pem"
  }
]
```

**Note**: `requires_mtls` is less commonly used than `requires_custom_tls`.
Most providers use standard TLS.

### `requires_token_refresh`

**Type**: `boolean`
**Default**: `false`

**Purpose**: Declares that the provider's access tokens have a limited lifetime
and must be refreshed. The user needs to know that their credentials will
expire — otherwise they will be confused when chats suddenly fail after a few
hours.

**When to use it**:

- AWS STS temporary credentials (`session_token`)
- Yandex IAM tokens (expire after 12 hours)
- Any provider that uses short-lived access tokens

**What the UI does**:
- Displays an info banner explaining the token lifetime.
- Combined with `token_lifetime_seconds`, shows a concrete time.

**What the adapter must do**: Nothing automatically. The adapter must handle
token refresh (or the user must refresh manually). Two common patterns:

**Pattern A — Adapter refreshes automatically**:

```python
async def _ensure_valid_token(self) -> str:
    if self._token_expires_at and time.time() > self._token_expires_at - 60:
        await self._refresh_token()
    return self._access_token
```

**Pattern B — User provides a fresh token each time**:

```python
# Yandex pattern: IAM tokens are short-lived, user provides a new one
iam_token = extra.get("iam_token")
if not iam_token:
    raise ProviderAuthError(
        "Yandex requires a fresh IAM token. Generate one from the Yandex Cloud Console.",
        provider="yandex",
    )
```

**Example**:

```json
"runtime_hints": {
  "requires_token_refresh": true,
  "token_lifetime_seconds": 43200
}
```

### `token_lifetime_seconds`

**Type**: `integer | null`
**Default**: `null`

**Purpose**: Specifies how long tokens last, in seconds. Used only when
`requires_token_refresh` is true, to render a specific time in the UI.

**Common values**:

| Provider | Lifetime |
|----------|----------|
| AWS STS session tokens | 3600 (1 hour, default) |
| Yandex IAM tokens | 43200 (12 hours) |
| GigaChat OAuth tokens | 1800 (30 minutes) |
| Azure AD tokens | 3600 (1 hour) |

**What the UI does**:
- Combines with `requires_token_refresh` to render: *"Tokens expire after 30
  minutes. You will need to regenerate them periodically."*

**If absent**: The UI shows a generic *"Tokens may expire"* message without a
specific duration.

**Example**:

```json
"runtime_hints": {
  "requires_token_refresh": true,
  "token_lifetime_seconds": 1800
}
```

### `supports_env_vars`

**Type**: `boolean`
**Default**: `false`

**Purpose**: Declares that the adapter can read connection values from
environment variables when a field is left empty. Useful for CI/CD deployments
where secrets are injected by the orchestrator.

**When to use it**:

- The adapter reads `os.environ.get("MY_PROVIDER_API_KEY")` as a fallback.
- The package is designed for Docker / Kubernetes deployment.

**What the UI does**:
- Shows a small badge in the Details drawer: *"Supports environment
  variables"*.
- No visible change to the Configure modal.

**What the adapter must do**:

```python
api_key = self._connection.api_key
if not api_key:
    api_key = os.environ.get("MY_PROVIDER_API_KEY") or ""

if not api_key:
    raise ProviderAuthError(
        "API key is required. Set the 'api_key' connection field or the MY_PROVIDER_API_KEY environment variable.",
        provider="my-provider",
    )
```

**Example**:

```json
"runtime_hints": {
  "supports_env_vars": true
}
```

**Warning**: The environment variable is read **inside the worker subprocess**,
not in the main process. The worker inherits the main process's environment
(because `create_subprocess_exec` passes it by default). So the env var must be
set when PELDRUN's main process starts.

### `docs_url`

**Type**: `string | null`
**Default**: `null`

**Purpose**: A link to the provider's official documentation. Shown as a
"Docs" link in the Configure modal banner (when a warning is displayed) and in
the Details drawer.

**When to use it**: Whenever the provider has public documentation. Always
useful, especially for complex setup flows.

**What the UI does**:
- In the Configure modal, appears next to warnings as "Docs →"
- In the Details drawer, appears in the "Homepage" or "Documentation" section

**Example**:

```json
"runtime_hints": {
  "docs_url": "https://developers.sber.ru/docs/ru/gigachat/guides/main"
}
```

**Best practice**: Link to the specific setup page, not just the homepage. The
user is already in the middle of a setup flow — take them straight to the
relevant docs.

```json
// GOOD
"docs_url": "https://docs.example.com/api/authentication"

// OKAY
"docs_url": "https://docs.example.com"

// BAD
"docs_url": "https://www.example.com"  // ← marketing homepage, no help
```

### `custom_headers_from_fields`

**Type**: `object` (map of `{field_key: header_name}`)
**Default**: `{}`

**Purpose**: Declares that certain connection fields should be copied into
HTTP headers on every request. This is the **only** hint that has functional
behavior — it actually injects headers.

**When to use it**:

- The provider requires custom headers for identification/ranking
  (OpenRouter's `HTTP-Referer` / `X-Title`).
- The provider uses a header-based API key instead of `Authorization: Bearer`.
- The provider requires a version header (e.g., `anthropic-version`).

**How it works**: When the runtime builds a request for this connection, it
copies the value of the declared field into the header. If the field is empty,
the header is not sent.

**Example (OpenRouter)**:

```json
"runtime_hints": {
  "custom_headers_from_fields": {
    "http_referer": "HTTP-Referer",
    "x_title": "X-Title"
  }
},
"connection_fields": [
  {
    "key": "http_referer",
    "label": "HTTP-Referer",
    "type": "url",
    "required": false,
    "default": "https://peldrun.app",
    "description": "Shown in OpenRouter's model rankings."
  },
  {
    "key": "x_title",
    "label": "App Title",
    "type": "string",
    "required": false,
    "default": "PELDRUN"
  }
]
```

**Behavior**:

```
User fills:
  http_referer = "https://myapp.com"
  x_title      = "MyApp"

Runtime sends:
  POST https://openrouter.ai/api/v1/chat/completions
  HTTP-Referer: https://myapp.com
  X-Title: MyApp
  Authorization: Bearer sk-or-...
```

**Example (Anthropic-style version header)**:

```json
"runtime_hints": {
  "custom_headers_from_fields": {
    "anthropic_version": "anthropic-version"
  }
},
"connection_fields": [
  {
    "key": "anthropic_version",
    "label": "Anthropic API Version",
    "type": "string",
    "required": true,
    "default": "2023-06-01"
  }
]
```

**Behavior**:

```
User fills:
  anthropic_version = "2023-06-01"

Runtime sends:
  POST https://api.anthropic.com/v1/messages
  anthropic-version: 2023-06-01
  x-api-key: sk-ant-...
```

**Rules**:

1. **Field must exist** — the `field_key` in `custom_headers_from_fields` must
   match a field declared in `connection_fields`. If not, the mapping is
   silently ignored (no error at install).

2. **Field must have a value** — if the field is empty, the header is not
   added. This is intentional: a missing optional field should not send an
   empty header.

3. **Value must be a string** — if the field value is not a string (e.g., a
   number), it is converted via `str()`.

4. **Header name is case-insensitive** — HTTP header names are traditionally
   case-insensitive. The runtime uses the name as declared.

5. **Cannot override standard headers** — `Authorization`, `Content-Type`, and
   `Content-Length` are reserved. Attempting to map a field to one of these
   will be silently ignored.

**Common use cases**:

| Provider | Fields mapped |
|----------|---------------|
| OpenRouter | `http_referer → HTTP-Referer`, `x_title → X-Title` |
| Anthropic | `anthropic_version → anthropic-version` |
| Some proxies | `team_id → X-Team-ID`, `api_version → API-Version` |

---

## Real-world examples

### GigaChat (Sber)

Requires OAuth + custom Russian TLS + short-lived tokens.

```json
"runtime_hints": {
  "requires_custom_tls": true,
  "requires_token_refresh": true,
  "token_lifetime_seconds": 1800,
  "docs_url": "https://developers.sber.ru/docs/ru/gigachat/guides/main"
}
```

**UI behavior**:
- Warning banner: *"This provider requires a custom TLS certificate. Provide
  the CA bundle path below."*
- Info banner: *"Tokens expire after 30 minutes."*
- Docs link to Sber's documentation.

### OpenRouter

Requires custom headers for ranking. No other special requirements.

```json
"runtime_hints": {
  "custom_headers_from_fields": {
    "http_referer": "HTTP-Referer",
    "x_title": "X-Title"
  },
  "docs_url": "https://openrouter.ai/docs"
}
```

**UI behavior**:
- Docs link in the Configure modal.
- The `http_referer` and `x_title` fields appear in the form (declared in
  `connection_fields`).
- Every request is silently stamped with `HTTP-Referer` and `X-Title`
  headers.

### YandexGPT

Requires a fresh IAM token (expires every 12 hours).

```json
"runtime_hints": {
  "requires_token_refresh": true,
  "token_lifetime_seconds": 43200,
  "docs_url": "https://cloud.yandex.com/docs"
}
```

**UI behavior**:
- Info banner: *"Tokens expire after 12 hours. Regenerate your IAM token from
  the Yandex Cloud Console before it expires."*

### AWS Bedrock (STS credentials)

Optionally uses temporary STS credentials with a session token.

```json
"runtime_hints": {
  "requires_token_refresh": true,
  "token_lifetime_seconds": 3600,
  "docs_url": "https://docs.aws.amazon.com/bedrock"
}
```

**Note**: Bedrock supports both long-term IAM credentials (no refresh needed)
and temporary STS credentials (1-hour lifetime). The hint is advisory — the
user can ignore it if they are using long-term credentials.

### On-prem enterprise provider with mTLS

```json
"runtime_hints": {
  "requires_mtls": true,
  "docs_url": "https://internal-docs.example.com/mtls-setup"
}
```

**UI behavior**:
- Warning banner: *"This provider requires mutual TLS. Provide your client
  certificate and private key below."*
- Docs link to the internal setup guide.

### Containerized deployment with env vars

```json
"runtime_hints": {
  "supports_env_vars": true,
  "docs_url": "https://github.com/acme/peldrun-provider#environment"
}
```

**UI behavior**:
- Small badge in Details drawer: *"Supports environment variables"*.
- No visible change in the Configure modal.
- Users who deploy via Docker / K8s can leave the API key field empty and
  set `ACME_API_KEY` in the container environment instead.

---

## Best practices

### 1. Only declare hints that describe reality

If `requires_custom_tls` is true but your adapter accepts plain HTTP, the hint
is lying. If `requires_token_refresh` is true but your adapter refreshes
tokens automatically and the user never sees an error, the hint is misleading.

**Rule**: Every hint must describe a real user-facing consequence.

### 2. Pair hints with the fields they imply

`requires_custom_tls` without a `ca_bundle_path` field is confusing — the user
sees "requires custom TLS" but has nowhere to provide the certificate.

**Pattern**:

```json
"runtime_hints": {"requires_custom_tls": true},
"connection_fields": [
  {"key": "ca_bundle_path", "label": "CA Bundle", "type": "file", "required": true}
]
```

### 3. Include `docs_url` whenever you can

The user is in a setup flow. A "Docs" link is the most helpful UI element you
can add. It costs nothing.

### 4. Be specific with `token_lifetime_seconds`

If you have the number, use it. "Tokens expire after 30 minutes" is far more
helpful than "Tokens may expire".

If you do not have the number, omit it rather than guess. A wrong number is
worse than no number.

### 5. Do not use hints for enforcement

Do not declare `requires_custom_tls: true` and then hope the runtime blocks
requests without a CA bundle. The hint does not block anything. The adapter
must raise `ProviderAuthError` if the field is missing.

### 6. Reserved header names

Do not attempt to map a field to `Authorization`, `Content-Type`, or
`Content-Length`. These are reserved and the mapping is ignored.

### 7. Case sensitivity for `custom_headers_from_fields`

Header names are traditionally case-insensitive, but the HTTP/2 specification
requires lowercase. The runtime uses the name as declared in the manifest. To
avoid confusion, use the canonical capitalization (e.g., `HTTP-Referer`, not
`http-referer`).

---

## Anti-patterns

### ❌ `requires_custom_tls` without a CA bundle field

```json
"runtime_hints": {
  "requires_custom_tls": true
}
// No ca_bundle_path field declared
```

**Problem**: The user sees "requires custom TLS" but has nowhere to provide
the certificate.

**Fix**: Add a `ca_bundle_path` field.

### ❌ `requires_token_refresh` with automatic refresh

```json
"runtime_hints": {
  "requires_token_refresh": true
}
// Adapter refreshes tokens automatically every 30 minutes
```

**Problem**: The user sees "tokens expire after 30 minutes" and thinks they
need to regenerate manually. They might refresh unnecessarily.

**Fix**: Only declare the hint if the user genuinely needs to refresh.

### ❌ `docs_url` pointing to a marketing homepage

```json
"runtime_hints": {
  "docs_url": "https://www.acme.com"  // ← marketing homepage
}
```

**Problem**: The user clicks "Docs" expecting setup instructions and lands on
a sales page.

**Fix**: Link to the specific API / authentication docs:

```json
"docs_url": "https://docs.acme.com/api/authentication"
```

### ❌ Using `custom_headers_from_fields` as a security mechanism

```json
"runtime_hints": {
  "custom_headers_from_fields": {
    "api_key": "Authorization"  // ← WRONG: Authorization is reserved
  }
}
```

**Problem**: The mapping is silently ignored (reserved header). The user's API
key never gets sent. All requests fail with 401.

**Fix**: The API key field is already handled by `ConnectionContext.api_key`
and sent as `Authorization: Bearer ...` by the base adapter. Do not try to
override it.

### ❌ Hints that lie

```json
"runtime_hints": {
  "requires_custom_tls": true
}
// But the adapter accepts plain HTTP
```

**Problem**: The user sees a warning about TLS, but it does not matter. Either
the hint or the adapter behavior is wrong.

**Fix**: Make the hint match reality. Remove it if not needed.

### ❌ `supports_env_vars` without env var fallback

```json
"runtime_hints": {
  "supports_env_vars": true
}
```

```python
# adapter.py
api_key = self._connection.api_key  # ← no env var fallback
```

**Problem**: The UI shows "Supports environment variables" but the adapter
does not actually read them.

**Fix**: Implement the fallback:

```python
api_key = self._connection.api_key or os.environ.get("MY_PROVIDER_API_KEY") or ""
```

### ❌ Header mapping to a nonexistent field

```json
"runtime_hints": {
  "custom_headers_from_fields": {
    "nonexistent_field": "X-Custom"  // ← no such field
  }
}
```

**Problem**: The mapping is silently ignored. No error at install, but the
header is never sent.

**Fix**: Ensure every field key in `custom_headers_from_fields` is declared in
`connection_fields`.

### ❌ Using hints to gate features

```json
"runtime_hints": {
  "supports_streaming": true  // ← does not exist
}
```

**Problem**: There is no `supports_streaming` hint. Streaming is controlled by
`features.native_streaming` in the manifest, not by `runtime_hints`.

**Fix**: Use `features.native_streaming: true`.

---

## Complete reference table

| Hint | Type | Default | UI effect | Functional? |
|------|------|---------|-----------|-------------|
| `requires_custom_tls` | bool | `false` | Warning banner in Configure modal | ❌ No |
| `requires_mtls` | bool | `false` | Warning banner in Configure modal | ❌ No |
| `requires_token_refresh` | bool | `false` | Info banner in Configure modal | ❌ No |
| `token_lifetime_seconds` | int \| null | `null` | Refines the token-refresh message | ❌ No |
| `supports_env_vars` | bool | `false` | Badge in Details drawer | ❌ No |
| `docs_url` | string \| null | `null` | "Docs" link in Configure modal | ❌ No |
| `custom_headers_from_fields` | dict | `{}` | **None** (headers applied silently) | ✅ Yes |

### Quick decision guide

| Situation | Use |
|-----------|-----|
| Provider's CA is not in certifi | `requires_custom_tls: true` + `ca_bundle_path` field |
| Provider requires client certs | `requires_mtls: true` + `client_cert_path` / `client_key_path` fields |
| Tokens expire every N seconds | `requires_token_refresh: true` + `token_lifetime_seconds: N` |
| Adapter reads env vars as fallback | `supports_env_vars: true` |
| Provider has public docs | `docs_url: "https://..."` (always) |
| Provider needs custom request headers | `custom_headers_from_fields: {...}` + matching fields |

---

## What's next

- **Need to declare connection fields?** → [Connection Fields](05-connection-fields.md)
- **Need to handle auth in the adapter?** → [Adapter Protocol](06-adapter-protocol.md)
- **Need the full manifest reference?** → [Manifest Reference](04-manifest-reference.md)
- **Need real-world examples?** → [Examples](13-examples.md)
```

---
