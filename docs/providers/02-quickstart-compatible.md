### `docs/providers/02-quickstart-compatible.md`

```markdown
<!-- docs/providers/02-quickstart-compatible.md -->
# Quickstart — Compatible Provider Package

> **Goal**: Build, install, and use a working Provider Package in **5 minutes**,
> without writing a single line of Python.
>
> **Prerequisites**:
> - PELDRUN is running locally
> - You have access to the PELDRUN UI
> - You know the base URL and API key of the provider you want to add
>
> **What you'll build**: A `compatible` package that teaches PELDRUN to talk
> to a hypothetical "Acme AI" service that speaks the OpenAI Chat Completions
> protocol.
>
> **Time**: ~5 minutes.

---

## Table of Contents

- [When to use the compatible transport](#when-to-use-the-compatible-transport)
- [The complete walkthrough](#the-complete-walkthrough)
  - [Step 1 — Create the manifest.json](#step-1--create-the-manifestjson)
  - [Step 2 — Package it as a ZIP](#step-2--package-it-as-a-zip)
  - [Step 3 — Install it](#step-3--install-it)
  - [Step 4 — Add a connection](#step-4--add-a-connection)
  - [Step 5 — Test it](#step-5--test-it)
  - [Step 6 — Use it in chat](#step-6--use-it-in-chat)
- [Adding custom fields (OAuth, region, etc.)](#adding-custom-fields)
- [Preventing user mistakes with visible_if](#preventing-user-mistakes-with-visible_if)
- [Ship it with the built-in catalog](#ship-it-with-the-built-in-catalog)
- [Troubleshooting](#troubleshooting)
- [Checklist](#checklist)

---

## When to use the compatible transport

**Use `compatible`** when the provider you want to add speaks a protocol that
PELDRUN already has a built-in adapter for.

### Compatible base adapters

| Base adapter | Protocol | Typical providers |
|--------------|----------|-------------------|
| `openai_compat` | OpenAI Chat Completions | OpenAI, DeepSeek, Groq, Mistral, Together AI, Fireworks, xAI (Grok), OpenRouter, Perplexity, most "OpenAI-compatible" services |
| `anthropic` | Anthropic Messages | Anthropic Claude, and any provider that mirrors its protocol |
| `google_genai` | Google Generate Content | Google Gemini, Vertex AI |
| `ollama_native` | Ollama `/api/chat` | Ollama (local) |
| `lmstudio_native` | LM Studio `/api/v0` | LM Studio (local) |

If your provider fits one of these, use `compatible`. It requires **zero
code** — just a `manifest.json` file.

If your provider has its own authentication scheme (OAuth, SigV4, mTLS) or its
own wire format, use [`native`](03-quickstart-native.md) instead.

---

## The complete walkthrough

### Step 1 — Create the manifest.json

Create a new folder for your package:

```bash
mkdir acme-ai-package
cd acme-ai-package
```

Inside, create a file named `manifest.json` with this content:

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "acme-ai",
  "name": "Acme AI",
  "display_name": "Acme AI",
  "version": "1.0.0",
  "description": "Acme AI provides fast, affordable LLM inference via an OpenAI-compatible API.",

  "api_contract": "peldrun.llm.provider.v1",
  "transport": "compatible",
  "base_adapter": "openai_compat",

  "author": "Acme Inc.",
  "author_url": "https://acme.example.com",
  "homepage": "https://acme.example.com/docs",
  "license": "MIT",
  "tags": ["acme", "cloud", "openai-compatible"],

  "connection_fields": [],

  "runtime_hints": {},

  "features": {
    "model_discovery": true,
    "native_streaming": true,
    "buffered_streaming": true,
    "tool_calling": true,
    "parallel_tool_calls": true,
    "forced_tool_choice": true,
    "vision": false,
    "audio": false,
    "structured_output": true,
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

### Understanding the fields

Let's break down every field — you'll need to know what each one does when you
customize the manifest for your provider.

**Identity fields** (all required):

| Field | Value | Notes |
|-------|-------|-------|
| `schema_version` | `"1.0.0"` | Currently the only supported version |
| `kind` | `"peldrun.llm_provider"` | Fixed — this is the only valid kind |
| `id` | `"acme-ai"` | **Stable identifier**. Must match `^[a-z0-9][a-z0-9._-]*$`. Once a user has installed this package, changing `id` makes it a different package. |
| `name` | `"Acme AI"` | Human-readable name |
| `version` | `"1.0.0"` | **Semver** — must match `^\d+\.\d+\.\d+`. Bump this when you change the manifest. |

**Transport fields**:

| Field | Value | Notes |
|-------|-------|-------|
| `api_contract` | `"peldrun.llm.provider.v1"` | Fixed — declares compatibility with the current PELDRUN API |
| `transport` | `"compatible"` | Uses the "no code" path |
| `base_adapter` | `"openai_compat"` | Which built-in adapter to delegate to |

**Metadata** (all optional but recommended):

| Field | Notes |
|-------|-------|
| `display_name` | Shown in the Store card. Falls back to `name`. |
| `description` | One or two sentences. Shown in the Store card. |
| `author`, `author_url`, `homepage`, `license` | Shown in the Details drawer |
| `tags` | Searchable in the Store |

**Connection fields**:

| Field | Notes |
|-------|-------|
| `connection_fields` | Empty array `[]` means "just base_url + api_key". See [Adding custom fields](#adding-custom-fields) to add more. |

**Runtime hints**:

| Field | Notes |
|-------|-------|
| `runtime_hints` | Empty object `{}` means "no warnings". See [Runtime Hints](07-runtime-hints.md) for TLS, token refresh, docs links. |

**Features** (all optional, default `false`):

These describe what the MODEL can do — but they are declarations for the UI.
The actual capability is resolved per-model via the capability resolver
(which may return `unknown` if no metadata exists). See
[Manifest Reference](04-manifest-reference.md#features) for the full list.

**Permissions** (required):

| Field | Value | Notes |
|-------|-------|-------|
| `network` | `"connection_endpoint"` | The adapter can reach the configured endpoint only |
| `secrets` | `["connection.api_key"]` | The adapter can read its own API key |
| `workspace_access` | `false` | **Forbidden** — must be false |
| `shell_execute` | `false` | **Forbidden** — must be false |

---

### Step 2 — Package it as a ZIP

The ZIP must contain `manifest.json` at the root. No subfolder wrapping.

```bash
zip acme-ai-1.0.0.zip manifest.json
```

**Directory structure:**

```
acme-ai-1.0.0.zip
└── manifest.json
```

**Verify the ZIP:**

```bash
unzip -l acme-ai-1.0.0.zip
```

Expected output:

```
Archive:  acme-ai-1.0.0.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
      1234  2025-01-01 00:00   manifest.json
---------                     -------
      1234                     1 file
```

### ⚠️ Common mistakes

**Wrong**: manifest inside a subfolder

```
acme-ai-1.0.0.zip
└── acme-ai/
    └── manifest.json        ← REJECTED
```

The validator supports a **single top-level folder** (it will strip the
prefix automatically), but a nested folder 2 levels deep is rejected because it
is ambiguous.

**Wrong**: manifest not at the root

```
acme-ai-1.0.0.zip
├── README.md
├── src/
└── manifest.json            ← OK — at the root
```

This is fine — extra files are allowed as long as `manifest.json` is at the
root.

**Wrong**: multiple manifests

```
acme-ai-1.0.0.zip
├── manifest.json
└── v2/
    └── manifest.json        ← REJECTED — ambiguity attack prevention
```

The validator rejects any archive with multiple candidate manifests.

---

### Step 3 — Install it

Open the PELDRUN UI:

1. Click **Store** in the sidebar
2. Click **Providers**
3. Click **Upload Package** in the top-right
4. Select `acme-ai-1.0.0.zip`
5. Leave **"Install dependencies in an isolated venv"** checked (for
   `compatible` packages, this has no effect — no venv is created — but
   leaving it on is harmless)
6. Click **Install Package**

**What happens next:**

The upload modal closes. The card appears in the **Installed** tab with:

- Name: **Acme AI**
- Version badge: `v1.0.0`
- Transport badge: **Compatible** (green)
- Status badge: **"Not configured"** (amber)
- Action buttons: **Configure** (primary/blue), **Details**, **Delete**

### Under the hood

Behind the scenes, the following happened:

1. `package_validator.validate_zip()` checked the ZIP for:
   - Compressed size (1234 bytes) — OK
   - Uncompressed size — OK
   - File count (1) — OK
   - Path safety — OK
   - Manifest Pydantic validation — OK

2. `package_manager.install_bundle()` then:
   - Detected the package is `compatible` → **skipped venv creation**
   - Extracted the manifest to
     `storage/store/providers/acme-ai/versions/1.0.0/manifest.json`
   - Wrote `_peldrun_meta.json` with the SHA-256
   - Registered in `registry.json`
   - Wrote `active.json` pointing to version 1.0.0

3. The Store card loaded via `GET /api/store/providers` — which now includes
   the new package with `has_active_connection: false`.

---

### Step 4 — Add a connection

The card has an **"Not configured"** badge — this is the expected state after
install. A package is useless until the user provides credentials.

1. Click **Configure** on the Acme AI card
2. The Configure modal opens in **Create** mode automatically (because there
   are no connections yet)
3. Fill in the form:
   - **Connection Name**: `Personal`
   - **Endpoint URL**: `https://api.acme.example.com/v1`
   - **API Key**: `sk-acme-...` (get this from your Acme AI dashboard)
   - **Set as active connection**: ✓ (checked by default)
4. Click **Create Connection**

**What happens next:**

The form closes and you return to the connection list, which now shows:

- **Personal** row with:
  - Badge: **Active** (green with star)
  - Test button (⚡)
  - Activate button (hidden — already active)
  - Disable button
  - Edit button (→)
  - Delete button (trash)

And the Acme AI card's status badge has changed from **"Not configured"**
(amber) to **"Personal"** (green with checkmark).

### What was saved

The connection was persisted to `models_vault.json`:

```json
{
  "provider_connections": [
    {
      "id": "conn_6c87efd...",
      "provider_package_id": "acme-ai",
      "name": "Personal",
      "enabled": true,
      "is_active": true,
      "fields": {
        "base_url": "https://api.acme.example.com/v1",
        "api_key": "sk-acme-..."
      },
      "created_at": "2025-01-01T00:00:00Z",
      "updated_at": "2025-01-01T00:00:00Z"
    }
  ]
}
```

The next time the user opens the Configure modal, the form will show
`base_url` and `api_key` (masked as `••••••••`) — the user can edit one field
without re-entering the other.

---

### Step 5 — Test it

In the connection list, click the **⚡ Test** button on the "Personal" row.

**What happens:**

The modal calls `POST /api/store/providers/acme-ai/connections/{cid}/test`.
The backend:

1. Reads the connection's stored fields
2. Builds a `ConnectionContext` with those fields
3. Calls `provider_factory.create("acme-ai", connection)` → returns a
   `PluginProviderAdapter`
4. The adapter sees `transport == "compatible"` → delegates to the built-in
   `openai_compat` adapter
5. Calls `adapter.validate_connection()` → `openai_compat` sends
   `GET {base_url}/models` with `Authorization: Bearer {api_key}`
6. Returns `{ok, message, latency_ms}`

**Expected result** (success):

```
✓ Connected — 245 ms
```

**If the test fails**, the row shows the exact error:

```
✗ Probe failed — HTTP 401: Invalid API key
```

Fix the field in the edit form and try again.

### What test does NOT do

- It does **not** send a chat completion
- It does **not** consume any tokens
- It does **not** validate the model name
- It **does** confirm the base URL and API key are correct

---

### Step 6 — Use it in chat

Now for the fun part — actually using the package.

**1. Navigate to Settings → LLM Settings:**

Actually, you don't need to. Provider Packages are automatically available as
providers. To use Acme AI:

**2. Open any chat:**

- Click **Chats** in the sidebar
- Open an existing chat or create a new one
- Open the model picker (usually in the chat header or the composer)

**3. Select Acme AI:**

- The model picker shows "Acme AI" as a provider (because it is registered
  in `provider_registry` with `source == "external"`)
- Select it
- Select a model (e.g., `acme-large-v2`)
- Send a message

**4. Under the hood:**

```
Your message
  ↓
dispatcher.run_direct_chat()
  ↓
_resolve_provider_and_extras(active_llm={"provider": "acme-ai", ...})
  ↓
  Returns ("acme-ai", {
    _peldrun_plugin: {transport: "compatible", base_adapter: "openai_compat", ...},
    _peldrun_base_url: "https://api.acme.example.com/v1",
    _peldrun_api_key: "sk-acme-...",
  })
  ↓
ConnectionContext(
  provider_id="acme-ai",
  base_url="https://api.acme.example.com/v1",
  api_key="sk-acme-...",
  extra_params={_peldrun_plugin: ...},
)
  ↓
provider_factory.create("acme-ai", connection)
  ↓
PluginProviderAdapter → _setup_compatible_delegate()
  ↓
openai_compat adapter (in-process, no subprocess)
  ↓
AsyncOpenAI → POST https://api.acme.example.com/v1/chat/completions
  ↓
Streaming response → SSE events → frontend
  ↓
Full response renders in chat
```

**5. Verify tokens are counted:**

Open **Telemetry** or **Usage** in the sidebar. The invocation you just made
should appear with:

- Provider: `Acme AI`
- Model: `acme-large-v2`
- Input tokens: `<some number>`
- Output tokens: `<some number>`
- Cost: `<some amount>`

The accounting system works identically for built-in and plugin providers —
`PluginProviderAdapter` returns a normal `LLMResponse` with a populated
`TokenUsage`, and the dispatcher's `default_tracker.complete()` records it.

**6. Try streaming:**

Send a longer prompt that produces a multi-token response. You should see:

- Tokens arriving one by one
- Reasoning deltas (if the provider supports them)
- Tool calls (if the model decides to invoke one and the agent exposes tools)
- Final response

Everything that works for built-in providers works for plugin providers —
because they share the same `ProviderAdapter` contract.

---

## Adding custom fields

The `connection_fields: []` above means "just base_url + api_key". Most
providers need more.

### Example — Provider with an OAuth scope

```json
"connection_fields": [
  {
    "key": "scope",
    "label": "Account Scope",
    "type": "select",
    "required": true,
    "default": "personal",
    "options": [
      {"value": "personal", "label": "Personal Account"},
      {"value": "business", "label": "Business Account"}
    ],
    "description": "Which Acme AI account to bill for this connection."
  }
]
```

### Example — Provider with a region selector

```json
"connection_fields": [
  {
    "key": "region",
    "label": "AWS Region",
    "type": "select",
    "required": true,
    "default": "us-east-1",
    "options": [
      {"value": "us-east-1", "label": "US East (N. Virginia)"},
      {"value": "eu-west-1", "label": "EU (Ireland)"},
      {"value": "ap-southeast-1", "label": "Asia Pacific (Singapore)"}
    ]
  }
]
```

### Example — Provider with a custom TLS certificate

```json
"connection_fields": [
  {
    "key": "ca_bundle_path",
    "label": "CA Bundle Path",
    "type": "file",
    "required": true,
    "accept": ".pem,.crt,.cer",
    "placeholder": "/etc/ssl/certs/ca-bundle.pem",
    "description": "Absolute path on the PELDRUN backend host to a PEM file containing the root CA."
  }
]
```

And add the corresponding runtime hint:

```json
"runtime_hints": {
  "requires_custom_tls": true
}
```

This will show a warning banner in the Configure modal so the user understands
why they need to provide a CA bundle.

### Example — Provider with versioned API

```json
"connection_fields": [
  {
    "key": "api_version",
    "label": "API Version",
    "type": "string",
    "required": true,
    "default": "2024-02-01",
    "placeholder": "2024-02-01",
    "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
    "description": "Azure-style dated API version. Must match YYYY-MM-DD."
  }
]
```

### Full reference

See [Connection Fields](05-connection-fields.md) for every field type,
validation rule, and conditional visibility option.

---

## Preventing user mistakes with visible_if

Some fields only make sense in certain configurations. `visible_if` lets you
hide a field until the user makes a choice that requires it.

**Example**: Only ask for a CA bundle when the user chooses "B2B" scope:

```json
"connection_fields": [
  {
    "key": "scope",
    "label": "Account Scope",
    "type": "select",
    "required": true,
    "default": "personal",
    "options": [
      {"value": "personal", "label": "Personal"},
      {"value": "b2b", "label": "Business (requires custom CA)"}
    ]
  },
  {
    "key": "ca_bundle_path",
    "label": "CA Bundle Path",
    "type": "file",
    "required": true,
    "accept": ".pem,.crt",
    "visible_if": {"scope": "b2b"}
  }
]
```

**Behavior in the UI:**

1. User selects "Personal" → only the scope field is shown
2. User changes to "Business" → the CA bundle field appears (with the
   `required: true` asterisk)
3. User changes back to "Personal" → the CA bundle field hides again

**Validation**: When the CA bundle field is hidden, its `required` constraint
is not enforced. When it becomes visible, it becomes required.

**`visible_if` shape**:

```json
"visible_if": {
  "<field_key_1>": <expected_value_1>,
  "<field_key_2>": <expected_value_2>
}
```

All conditions are ANDed. An empty `visible_if` (or absent) means "always
visible".

---

## Ship it with the built-in catalog

If you want your package to be discoverable without a manual upload, add it to
`backend/omweb/provider_packages/builtin_catalog.json`.

### For compatible packages (recommended)

Compatible packages do not need a `download_url` or `checksum_sha256`. They
are **always installable** because the backend generates the manifest from the
catalog item directly.

```json
{
  "id": "acme-ai",
  "name": "Acme AI",
  "display_name": "Acme AI",
  "description": "Acme AI provides fast, affordable LLM inference via an OpenAI-compatible API.",
  "version": "1.0.0",
  "transport": "compatible",
  "base_adapter": "openai_compat",
  "features": {
    "native_streaming": true,
    "tool_calling": true,
    "usage_reporting": true
  },
  "permissions": {
    "network": "connection_endpoint",
    "secrets": ["connection.api_key"],
    "workspace_access": false,
    "shell_execute": false
  },
  "connection_fields": [],
  "download_url": null,
  "checksum_sha256": null,
  "author": "Acme Inc.",
  "homepage": "https://acme.example.com/docs",
  "tags": ["acme", "cloud", "openai-compatible"]
}
```

After adding to the catalog and restarting the backend (or calling
`POST /api/store/providers/catalog/refresh`), users will see "Acme AI" in the
**Catalog** tab and can install it with one click — no ZIP upload required.

### For native / custom packages

Native and custom packages ship code, so they require a real ZIP:

1. Build the ZIP
2. Compute its SHA-256:
   ```bash
   sha256sum acme-ai-1.0.0.zip
   ```
3. Upload it to a URL your users can reach
4. Add to the catalog with both `download_url` and `checksum_sha256`:

```json
{
  "id": "acme-native",
  "name": "Acme AI (Native)",
  "version": "1.0.0",
  "transport": "native",
  "download_url": "https://acme.example.com/packages/acme-native-1.0.0.zip",
  "checksum_sha256": "a1b2c3d4e5f6...",
  ...
}
```

---

## Troubleshooting

### "Upload failed: manifest.json is missing"

Your ZIP does not contain `manifest.json` at the root or inside a single
top-level folder. Check with `unzip -l acme-ai-1.0.0.zip`.

### "Upload failed: version 'latest' must be semver"

Change `version` to `1.0.0`, `1.0.0-beta.1`, or any valid semver. `latest`,
`1.0`, and `dev` are rejected.

### "Upload failed: provider 'id' must match ^[a-z0-9][a-z0-9._-]*$"

Your `id` contains uppercase letters, spaces, or special characters. Change
`"Acme-AI"` to `"acme-ai"`.

### "Upload failed: Compatible packages must declare 'base_adapter'"

You set `transport: "compatible"` but did not declare `base_adapter`. Add
`"base_adapter": "openai_compat"` (or another built-in adapter).

### "Upload failed: builtin collision"

You chose an `id` that matches a built-in adapter (`openai_compat`,
`anthropic`, `google_genai`, `ollama_native`, `lmstudio_native`). Choose a
different `id`.

### "Configure modal shows empty form"

The package has no `connection_fields` declared. This is correct — the form
auto-generates base_url + api_key. If you want more fields, add them to
`connection_fields`.

### "Test failed: HTTP 401"

Your API key is wrong. Open the connection, edit the API key, save, retry.

### "Test failed: HTTP 404"

Your base URL is wrong. Common mistakes:

- Missing `/v1` suffix: `https://api.acme.com` → `https://api.acme.com/v1`
- Wrong path: `https://api.acme.com/chat` → `https://api.acme.com/v1`
- Trailing slash: `https://api.acme.com/v1/` → `https://api.acme.com/v1`

### "Chat failed: Provider returned HTTP 400"

The model name is wrong. Check what model IDs the provider accepts — usually
shown in their documentation. The provider's error body should contain more
detail; open **Browser DevTools → Network** to inspect the raw response.

### "Tokens are not being counted"

The provider did not return a `usage` object in its response. This is normal
for some providers. The accounting system records the invocation with
`usage_source: "unknown"` and `cost_status: "unknown"` — the invocation still
appears in the telemetry, just without token counts.

---

## Checklist

Before you ship your package, verify:

- [ ] `manifest.json` is valid JSON (use `jq . manifest.json` to check)
- [ ] `id` matches `^[a-z0-9][a-z0-9._-]*$`
- [ ] `version` is valid semver (e.g., `1.0.0`)
- [ ] `transport` is `"compatible"`
- [ ] `base_adapter` is one of: `openai_compat`, `anthropic`, `google_genai`,
      `ollama_native`, `lmstudio_native`
- [ ] `connection_fields` is `[]` (or contains valid field specs)
- [ ] `permissions.workspace_access` is `false`
- [ ] `permissions.shell_execute` is `false`
- [ ] ZIP contains `manifest.json` at the root
- [ ] ZIP is under 100 MB
- [ ] You can install the package in a test PELDRUN instance
- [ ] You can create a connection and pass the test
- [ ] You can send a chat message and receive a response

---

## What's next

- **Your provider has custom auth?** → Add fields via
  [Connection Fields](05-connection-fields.md)
- **Your provider needs code?** → [Quickstart — Native](03-quickstart-native.md)
- **Want the full manifest schema?** → [Manifest Reference](04-manifest-reference.md)
- **Need help?** → [Troubleshooting](14-troubleshooting.md)
```

---

