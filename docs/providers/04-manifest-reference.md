### `docs/providers/04-manifest-reference.md`

```markdown
<!-- docs/providers/04-manifest-reference.md -->
# Manifest Reference

> **Complete reference** for every field in `manifest.json`.
>
> **Reading time**: 15 minutes.
>
> **Who this is for**: Anyone writing or modifying a manifest — either
> `compatible` or `native`.
>
> **Design principle**: The manifest schema uses `extra="forbid"` — a typo in
> a field name is a hard error at install time, not a silent no-op. This is
> deliberate: dropping a security-relevant field by accident is unacceptable.

---

## Table of Contents

- [Top-level structure](#top-level-structure)
- [Identity fields](#identity-fields)
- [Transport fields](#transport-fields)
- [Metadata fields](#metadata-fields)
- [Connection fields](#connection-fields)
- [Runtime hints](#runtime-hints)
- [Feature flags](#feature-flags)
- [Permissions](#permissions)
- [Complete examples](#complete-examples)
- [Validation rules summary](#validation-rules-summary)

---

## Top-level structure

A `manifest.json` has this shape:

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "acme-ai",
  "name": "Acme AI",
  "version": "1.0.0",
  "display_name": "Acme AI",
  "description": "...",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "compatible",
  "base_adapter": "openai_compat",
  "entry_point": null,
  "author": "Acme Inc.",
  "author_url": "https://acme.example.com",
  "homepage": "https://acme.example.com/docs",
  "license": "MIT",
  "icon": "acme-logo",
  "tags": ["acme", "cloud"],
  "connection_fields": [],
  "runtime_hints": {},
  "features": {},
  "permissions": {},
  "runtime": {},
  "dependencies": []
}
```

Every field is documented below.

---

## Identity fields

### `schema_version` — required

**Type**: `string`
**Allowed values**: `"1.0.0"` (currently the only supported version)
**Purpose**: Declares which manifest schema this package uses.

A package declaring a version the current PELDRUN build does not recognize is
rejected with a clear error. This prevents silent forward-compatibility issues
(e.g., a package that uses a field introduced in `1.1.0` failing on a `1.0.x`
backend with a cryptic error).

**Example**:
```json
"schema_version": "1.0.0"
```

**What breaks it**:
- `"1"` — not a valid version string
- `"2.0.0"` — not supported by this build
- Omitting the field — Pydantic ValidationError

---

### `kind` — required

**Type**: `string` (literal)
**Allowed values**: `"peldrun.llm_provider"` (currently the only valid kind)
**Purpose**: Distinguishes provider packages from other future package types
(`peldrun.tool`, `peldrun.skill`, etc.).

**Example**:
```json
"kind": "peldrun.llm_provider"
```

---

### `id` — required

**Type**: `string`
**Pattern**: `^[a-z0-9][a-z0-9._-]*$`
**Purpose**: The stable identifier used throughout the system.

**Why the pattern is strict**: `id` is used as:
- A directory name (`storage/store/providers/<id>/`)
- A registry key (`registry.json.packages[<id>]`)
- A provider registry key (`provider_registry.find(<id>)`)
- A user-facing string in the chat provider selector

Uppercase letters, spaces, and special characters would break one or more of
those uses.

**Examples**:
```json
"id": "acme-ai"           ✅
"id": "acme_ai"           ✅
"id": "acme.ai"           ✅
"id": "acme-1"            ✅
"id": "Acme-AI"           ❌ (uppercase)
"id": "acme ai"           ❌ (space)
"id": "acme/ai"           ❌ (slash)
"id": "_acme"             ❌ (leading underscore)
"id": "1-acme"            ✅ (leading digit is OK)
```

**Stability rule**: Once a user installs a package with `id: "acme-ai"`,
changing the id makes it a **different package**. The old package will remain
installed alongside the new one, with separate connections and separate
registry entries. This is intentional — it prevents accidental data loss.

**Built-in collision**: `id` cannot match a built-in provider id:
- `openai_compat`
- `lmstudio`
- `ollama`
- `openai_native`
- `anthropic`
- `google_genai`
- `ollama_native`
- `lmstudio_native`

The install fails with `PROVIDER_PACKAGE_CONFLICT` if you try.

---

### `name` — required

**Type**: `string`
**Purpose**: Human-readable name, shown wherever `display_name` is not set.

**Example**:
```json
"name": "Acme AI"
```

**No constraints** on length or content. Any UTF-8 string is accepted.

---

### `version` — required

**Type**: `string`
**Pattern**: `^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$` (semver)
**Purpose**: The package version. Used for rollback and idempotency.

**Supported formats**:
```
1.0.0                    ✅ (basic)
1.0.0-beta.1             ✅ (pre-release)
1.0.0+build.42           ✅ (build metadata)
1.0.0-rc.1+build.42      ✅ (both)
1.0                      ❌ (missing patch)
1                        ❌ (not semver)
latest                   ❌ (not semver)
1.0.0.0                  ❌ (too many segments)
```

**Rollback semantics**: When a new version is installed, the previous version
remains in `installed_versions[]`. The user can activate it via the Store
(Details → Versions → Activate) at any time. The active version's files and
venv are preserved forever.

**Idempotency**: Installing the exact same ZIP twice (same version, same
SHA-256) is a no-op. Installing the same version with a different SHA-256
raises `PROVIDER_PACKAGE_CONFLICT`.

---

### `display_name` — optional

**Type**: `string | null`
**Default**: `null` (falls back to `name`)
**Purpose**: A friendlier label for the Store card.

**Use case**: When `name` is a technical identifier (e.g., `"gigachat-sber"`)
but you want the card to show `"GigaChat"`.

**Example**:
```json
"name": "gigachat",
"display_name": "GigaChat"
```

---

### `description` — optional

**Type**: `string`
**Default**: `""` (empty string)
**Purpose**: Shown in the Store card (2-line clamp) and Details drawer (full).

**Recommended**: 1-3 sentences. Explain what the provider is, what makes it
useful, and any setup notes.

**Example**:
```json
"description": "GigaChat is a family of Russian language models from Sber. Requires OAuth authentication and a custom CA bundle."
```

---

## Transport fields

### `api_contract` — required

**Type**: `string`
**Allowed values**: `"peldrun.llm.provider.v1"` (currently the only supported value)
**Purpose**: Declares compatibility with the current PELDRUN adapter API.

**Why a separate field from `schema_version`**: `schema_version` describes
the manifest format. `api_contract` describes the Python API the adapter code
implements. They evolve independently:
- Bumping `schema_version` (e.g., adding a field) does not affect adapter code.
- Bumping `api_contract` (e.g., changing `generate()`'s signature) requires
  adapter code changes.

**Future-proofing**: When the adapter API changes incompatibly, a new value
`"peldrun.llm.provider.v2"` will be introduced. Packages declaring `v1` will
continue to work until they are explicitly removed (with advance notice).

**Example**:
```json
"api_contract": "peldrun.llm.provider.v1"
```

---

### `transport` — required

**Type**: `string` (enum)
**Allowed values**: `"compatible"` | `"native"` | `"custom"`

**`compatible`** — The package ships no code. It reuses a built-in adapter via
`base_adapter`. The runtime creates a `PluginProviderAdapter` that delegates to
the built-in adapter in-process. No subprocess, no venv, no IPC.

**`native`** — The package ships Python code (via `entry_point`) and requires
an isolated venv. The runtime spawns a worker subprocess. Best for providers
with unique wire formats or unusual authentication schemes.

**`custom`** — Same execution path as `native` (worker subprocess, isolated
venv), but the manifest declares `base_adapter` is not usable. Use this when
your provider is not a standard protocol and you need to make it explicit in
the UI.

**Example** (compatible):
```json
"transport": "compatible"
```

**Validation rules by transport**:

| Transport | `entry_point` | `base_adapter` |
|-----------|--------------|----------------|
| `compatible` | **must be `null`** | **required** |
| `native` | **required** | **must be `null`** |
| `custom` | **required** | **must be `null`** |

Omitting both, or providing both, or providing the wrong one, causes a
`manifest.transport: ...` validation error.

---

### `entry_point` — conditionally required

**Type**: `string | null`
**Pattern**: `^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$`
**Required for**: `native`, `custom`
**Forbidden for**: `compatible`

**Purpose**: The `module.path:ClassName` reference to the adapter class inside
the package's Python code.

**Format**:
```
<module_path>:<class_name>
```

- `<module_path>` — dots for package path, no `.py` extension
- `<class_name>` — the class that implements `ProviderAdapter`

**Examples**:
```json
"entry_point": "gigachat_peldrun.adapter:GigaChatAdapter"         ✅
"entry_point": "acme.provider.adapter:AcmeAdapter"                ✅
"entry_point": "adapter:MyProvider"                               ✅ (top-level)
"entry_point": "acme_peldrun.adapter:AcmeProvider"                 ✅
"entry_point": "gigachat_peldrun.adapter"                          ❌ (missing class)
"entry_point": "gigachat_peldrun.adapter:GigaChatAdapter.other"    ❌ (too many colons)
"entry_point": "123module:Class"                                   ❌ (leading digit)
```

**File resolution**: The validator checks that `entry_point` resolves to a real
file in the ZIP:

| Module path | Resolves to |
|-------------|-------------|
| `gigachat_peldrun.adapter` | `gigachat_peldrun/adapter.py` **or** `gigachat_peldrun/adapter/__init__.py` |
| `adapter` | `adapter.py` or `adapter/__init__.py` |
| `src.gigachat.adapter` | `src/gigachat/adapter.py` or `src/gigachat/adapter/__init__.py` |

If the file does not exist, install fails with
`entry_point module '...' does not resolve to any .py file in the archive`.

**At runtime**: The worker imports the module, gets the class via
`getattr(module, class_name)`, and instantiates it. If the class is missing,
`ADAPTER_LOAD_FAILED` is emitted with a clear error.

---

### `base_adapter` — conditionally required

**Type**: `string | null`
**Pattern**: `^[a-z0-9][a-z0-9._-]*$`
**Required for**: `compatible`
**Forbidden for**: `native`, `custom`

**Purpose**: Names the built-in adapter this package delegates to.

**Allowed values**:
- `openai_compat`
- `lmstudio` (legacy)
- `ollama` (legacy)
- `anthropic`
- `google_genai`
- `ollama_native`
- `lmstudio_native`
- `openai_native`

**Example**:
```json
"transport": "compatible",
"base_adapter": "openai_compat"
```

**Delegation behavior**:

1. `PluginProviderAdapter.__init__` sees `transport == "compatible"`.
2. It calls `provider_factory.create(base_adapter, delegate_connection)`.
3. The built-in adapter is instantiated in-process.
4. `PluginProviderAdapter.generate()` calls `delegate.generate()`.

**Connection isolation**: The delegate receives a `ConnectionContext` that
has `_peldrun_plugin` removed from `extra_params`. This prevents the delegate
from recursively treating itself as a plugin.

---

## Metadata fields

All metadata fields are optional. They appear in the Store card and Details
drawer.

### `author` — optional

**Type**: `string | null`
**Example**:
```json
"author": "Acme Inc."
```

### `author_url` — optional

**Type**: `string | null`
**Purpose**: Link to the author's website or GitHub. Shown in the Details
drawer next to the author name.
**Example**:
```json
"author_url": "https://github.com/acme"
```

### `homepage` — optional

**Type**: `string | null`
**Purpose**: Link to the provider's documentation or homepage. Shown as a
clickable link in the Details drawer.
**Example**:
```json
"homepage": "https://acme.example.com/docs"
```

### `license` — optional

**Type**: `string | null`
**Purpose**: SPDX license identifier. Shown in the Details drawer.
**Example**:
```json
"license": "MIT"
```

**Common values**: `MIT`, `Apache-2.0`, `BSD-3-Clause`, `GPL-3.0`, `Proprietary`.

### `icon` — optional

**Type**: `string | null`
**Purpose**: Icon identifier. Currently used only for display in the Details
drawer. Future versions may support custom SVG icons.

**Example**:
```json
"icon": "acme-logo"
```

### `tags` — optional

**Type**: `string[]`
**Default**: `[]`
**Purpose**: Searchable keywords. The Store search matches against these.

**Example**:
```json
"tags": ["acme", "cloud", "openai-compatible", "cheap"]
```

**Best practice**: Use lowercase, single-word tags. The UI displays them as
pills.

---

## Connection fields

### `connection_fields` — optional

**Type**: `ConnectionFieldSpec[]`
**Default**: `[]` (means "use defaults — base_url and api_key only")
**Purpose**: Declares the inputs the user must fill in when creating a
connection for this provider.

The Store's Configure modal renders one input per spec, in declaration order.
Fields declared here are persisted to `models_vault.json` under the
connection's `fields` dict, and passed to the adapter via
`connection.extra_params[<key>]` at request time.

### Default fields

Every package has two **implicit** fields even if `connection_fields` is empty:

| Key | Label | Type | Required |
|-----|-------|------|----------|
| `base_url` | Endpoint URL | `url` | true |
| `api_key` | API Key | `password` | false |

To customize their appearance (e.g., different label or default), declare them
explicitly in `connection_fields`. The declaration **overrides** the implicit
default.

### `ConnectionFieldSpec` fields

Every entry in `connection_fields` supports these properties:

| Property | Type | Required | Default | Purpose |
|----------|------|----------|---------|---------|
| `key` | string | ✅ | — | Identifier used in `models_vault.json` and `extra_params` |
| `label` | string | ✅ | — | User-facing label |
| `type` | enum | — | `"string"` | Input type (see below) |
| `required` | bool | — | `false` | Whether the form enforces a non-empty value |
| `default` | any | — | `null` | Initial value |
| `placeholder` | string | — | `null` | Placeholder text in the input |
| `description` | string | — | `null` | Helper text below the input |
| `options` | array | — | `null` | For `select` type |
| `visible_if` | object | — | `null` | Conditional visibility |
| `accept` | string | — | `null` | For `file` / `secret_file` — comma-separated extensions |
| `min_length` | int | — | `null` | Minimum string length |
| `max_length` | int | — | `null` | Maximum string length |
| `pattern` | string | — | `null` | Regex pattern (client-side validation only) |

### Field types

#### `string`

Free-form text.

```json
{
  "key": "project_id",
  "label": "Project ID",
  "type": "string",
  "required": true,
  "placeholder": "my-gcp-project",
  "description": "Found in the Google Cloud Console."
}
```

Renders as: `<input type="text">`

#### `password`

Masked text. Value is stored in `models_vault.json` in plaintext but **masked
in API responses** as `••••••••`. Submitting the masked value on update
preserves the original.

```json
{
  "key": "api_key",
  "label": "API Key",
  "type": "password",
  "required": true,
  "placeholder": "sk-..."
}
```

Renders as: `<input type="password">` with a "Show" toggle.

#### `url`

Same as `string` but uses `type="url"` (browsers validate the URL format).

```json
{
  "key": "base_url",
  "label": "Endpoint URL",
  "type": "url",
  "required": true,
  "placeholder": "https://api.example.com/v1"
}
```

Renders as: `<input type="url">`

#### `number`

Numeric input.

```json
{
  "key": "timeout_seconds",
  "label": "Timeout",
  "type": "number",
  "default": 60,
  "description": "Seconds before a request is abandoned."
}
```

Renders as: `<input type="number">`

#### `boolean`

Checkbox.

```json
{
  "key": "verify_ssl",
  "label": "Verify SSL Certificates",
  "type": "boolean",
  "default": true,
  "description": "Disable ONLY for development."
}
```

Renders as: `<input type="checkbox">` with "Enabled" / "Disabled" label.

#### `select`

Dropdown. Requires `options`.

```json
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
  ]
}
```

Renders as: `<select>` with the declared options.

**`options` shape**: Array of `{value, label}` dicts. `value` is what gets
stored; `label` is what gets displayed. Both must be strings.

#### `textarea`

Multi-line text input.

```json
{
  "key": "service_account_json",
  "label": "Service Account JSON",
  "type": "textarea",
  "required": true,
  "placeholder": "Paste the JSON key from Google Cloud Console...",
  "description": "The full contents of the service account key file."
}
```

Renders as: `<textarea rows={4}>`

**Warning**: Storing large JSON blobs in `models_vault.json` bloats the file.
For large credentials, prefer `file` type (path to a file on the host).

#### `file`

Path to a file on the PELDRUN backend host. The Store cannot read the user's
local filesystem, so the user must upload the file manually to the backend
(e.g., via SSH or the Files UI) and then enter the path.

```json
{
  "key": "ca_bundle_path",
  "label": "CA Bundle",
  "type": "file",
  "required": true,
  "accept": ".pem,.crt,.cer",
  "placeholder": "/etc/ssl/certs/ca-bundle.pem",
  "description": "Absolute path on the PELDRUN host to a PEM file."
}
```

Renders as: `<input type="text">` with a hint explaining that it is a path.

**Security note**: The path is not validated against traversal at manifest
time. The adapter is responsible for reading it safely. A package should
never accept a file path from an untrusted source without sanitization.

#### `secret_file`

Same as `file` but the value is masked in API responses (`••••••••`).

```json
{
  "key": "mtls_client_cert",
  "label": "Client Certificate",
  "type": "secret_file",
  "required": true,
  "accept": ".pem,.crt",
  "description": "Path to your mTLS client certificate."
}
```

### Conditional visibility

Any field can declare `visible_if` to appear only when another field has a
specific value.

```json
{
  "key": "scope",
  "type": "select",
  "options": [
    {"value": "personal", "label": "Personal"},
    {"value": "b2b", "label": "Business"}
  ]
},
{
  "key": "ca_bundle_path",
  "type": "file",
  "required": true,
  "visible_if": {"scope": "b2b"}
}
```

**Behavior**:
- When `scope == "personal"`, the CA bundle field is hidden and its `required`
  constraint is not enforced.
- When `scope == "b2b"`, the CA bundle field appears with a red asterisk, and
  Save is disabled until it is filled.

**Multiple conditions** (all ANDed):

```json
{
  "visible_if": {
    "scope": "b2b",
    "verify_ssl": true
  }
}
```

**Empty `visible_if`** (or absent) means "always visible".

### Validation rules

Client-side (the form):
- `required` — non-empty string, non-null value
- `pattern` — regex match (browser `pattern` attribute)
- `min_length` / `max_length` — string length

Server-side: **none**. The backend stores whatever the form sends. Adapters
must validate their own fields on use.

**Why no server-side validation**: The manifest cannot express every possible
validation rule (e.g., "the base64 string must decode to valid JSON with a
`project_id` key"). Adding per-provider validators to the backend would
require code changes for every package. Instead, adapters validate on use and
raise `ProviderInvalidRequestError` with a clear message.

---

## Runtime hints

### `runtime_hints` — optional

**Type**: `object`
**Default**: `{}`
**Purpose**: Non-functional declarations that drive UI warnings and docs
links. Hints do NOT enforce anything — the adapter code is the source of truth
for actual behavior. They exist so users understand what to expect before
saving a connection.

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `requires_custom_tls` | bool | false | Show a banner explaining that a custom CA is required |
| `requires_mtls` | bool | false | Show a banner explaining mutual TLS |
| `requires_token_refresh` | bool | false | Show a note about token expiry |
| `token_lifetime_seconds` | int | null | Used to render "tokens expire in N minutes" |
| `supports_env_vars` | bool | false | Adapter can read from environment when fields are empty |
| `docs_url` | string | null | Link to the provider's documentation |
| `custom_headers_from_fields` | object | {} | Map of `{field_key: http_header_name}` |

### Examples

**GigaChat** — requires custom TLS + token refresh:

```json
"runtime_hints": {
  "requires_custom_tls": true,
  "requires_token_refresh": true,
  "token_lifetime_seconds": 1800,
  "docs_url": "https://developers.sber.ru/docs/ru/gigachat/guides/main"
}
```

**OpenRouter** — injects custom headers from form fields:

```json
"runtime_hints": {
  "custom_headers_from_fields": {
    "http_referer": "HTTP-Referer",
    "x_title": "X-Title"
  }
}
```

With this hint, if the user fills in `http_referer: "https://myapp.com"` in
the connection form, PELDRUN will add `HTTP-Referer: https://myapp.com` to
every outgoing request from this connection. This is OpenRouter's ranking
metadata convention.

**YandexGPT** — token expires every 12 hours:

```json
"runtime_hints": {
  "requires_token_refresh": true,
  "token_lifetime_seconds": 43200,
  "docs_url": "https://cloud.yandex.com/docs"
}
```

### What hints do NOT do

- They do NOT validate the user's input
- They do NOT enable or disable fields
- They do NOT affect the wire format
- They do NOT affect capability resolution

They are **purely informational** for the UI.

---

## Feature flags

### `features` — optional

**Type**: `object`
**Default**: all flags `false`
**Purpose**: Declares what the adapter can handle. The
`compute_resolved_capabilities()` function intersects these with the model's
declared capabilities to decide which parameters to include in requests.

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `model_discovery` | bool | false | Adapter can list models via `discover_models()` |
| `native_streaming` | bool | false | Adapter streams tokens in real time |
| `buffered_streaming` | bool | false | Adapter returns full response, runtime simulates chunks |
| `tool_calling` | bool | false | Adapter supports `tools` and `tool_calls` |
| `parallel_tool_calls` | bool | false | Model can call multiple tools in one turn |
| `forced_tool_choice` | bool | false | Adapter honors `tool_choice="required"` |
| `vision` | bool | false | Adapter accepts `image_url` parts |
| `audio` | bool | false | Adapter accepts audio input |
| `structured_output` | bool | false | Adapter supports `response_format` |
| `usage_reporting` | bool | false | Provider returns token counts |
| `reasoning` | bool | false | Adapter supports reasoning parameters |

### Feature intersection rules

The runtime NEVER attempts a capability unless BOTH the model AND the adapter
declare it. For example:

| Model says | Adapter says | Runtime does |
|-----------|-------------|--------------|
| `tools: true` | `tool_calling: false` | **Does not** send tools |
| `tools: false` | `tool_calling: true` | **Does not** send tools |
| `tools: true` | `tool_calling: true` | **Does** send tools |
| `tools: unknown` | `tool_calling: true` | Depends on user's manual override |

**This is intentional**. A conservative adapter that declares
`tool_calling: false` will never send tools, even if the model supports them.
If you want to enable tools, declare the capability and implement it.

### `reasoning` feature

The `reasoning` flag enables the adapter to translate PELDRUN's reasoning
intent into the provider's native parameter. The translation strategy depends
on `reasoning_strategies` in the `AdapterProfile` (see
[Adapter Protocol](06-adapter-protocol.md)), not on the manifest.

If `features.reasoning: false`, the adapter receives no reasoning parameters,
even if the user selected "High" in the UI.

---

## Permissions

### `permissions` — required

**Type**: `object`
**Purpose**: Declares what the adapter's code is allowed to do at runtime.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `network` | `"none"` \| `"connection_endpoint"` | `"connection_endpoint"` | Network scope |
| `secrets` | `string[]` | `[]` | Which secrets the adapter can read |
| `workspace_access` | `bool` | `false` | **Forbidden** — must be false |
| `shell_execute` | `bool` | `false` | **Forbidden** — must be false |

### `network`

**`"connection_endpoint"`** (default) — The adapter can make HTTP requests to
the host and port of the connection's `base_url`. All other URLs are blocked
by the runtime allowlist.

**`"none"`** — The adapter cannot make any network requests. Useful for
providers that operate entirely on local data (none exist today, but the flag
is reserved).

**Not allowed**: `"any"`, `"*"`, or any other value. The runtime has no
"unrestricted network" mode for packages.

### `secrets`

**Allowed values**:
- `"connection.api_key"` — The adapter can read `connection.api_key`

**Forbidden values** (rejected at install):
- `"connection.base_url"` — base_url is not a secret
- `"connection.extra_params.<anything>"` — extra_params are not secrets by
  default; use connection_fields + `type: "password"` instead
- `"*"` — no wildcards
- `"env.*"` — no environment variables
- Any other value — the manifest is rejected

**Why so strict**: An adapter can only read its OWN connection's API key. It
cannot read another connection's key, the PELDRUN config, or any other secret.
This is enforced at process level — the worker only receives its own
`ConnectionContext`.

### `workspace_access`

**Must be `false`.** The install fails if `true`.

Provider adapters do not need to read workspace files. The runtime blocks any
file I/O attempt from a provider package. If your provider needs to read a file
(e.g., a CA bundle), use a `file` connection field instead — the user provides
the path, and the adapter opens it explicitly via `open(path)`, which is not
blocked (the file is a configuration input, not workspace data).

### `shell_execute`

**Must be `false`.** The install fails if `true`.

Provider adapters never spawn processes. If your provider needs to invoke a
CLI tool, you cannot do it via a Provider Package — that requires an Extension
Package (MCP server), which has different permissions.

---

## Complete examples

### Minimal compatible package

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "acme-ai",
  "name": "Acme AI",
  "version": "1.0.0",
  "api_contract": "peldrun.llm.provider.v1",
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
  }
}
```

### Compatible with custom headers (OpenRouter-style)

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "openrouter",
  "name": "OpenRouter",
  "version": "1.0.0",
  "display_name": "OpenRouter",
  "description": "Unified API for 100+ models from every provider. Pay per token, no subscription.",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "compatible",
  "base_adapter": "openai_compat",
  "author": "OpenRouter",
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
      "key": "http_referer",
      "label": "HTTP-Referer (optional)",
      "type": "url",
      "required": false,
      "default": "https://peldrun.app",
      "description": "Shown in OpenRouter's model rankings."
    },
    {
      "key": "x_title",
      "label": "App Title (optional)",
      "type": "string",
      "required": false,
      "default": "PELDRUN"
    }
  ],
  "runtime_hints": {
    "custom_headers_from_fields": {
      "http_referer": "HTTP-Referer",
      "x_title": "X-Title"
    }
  },
  "features": {
    "model_discovery": true,
    "native_streaming": true,
    "tool_calling": true,
    "vision": true,
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

### Full native package (GigaChat)

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
  "dependencies": ["gigachat>=0.2.0,<1.0"],
  "runtime": {
    "type": "python",
    "python": ">=3.12,<3.13"
  },
  "author": "PELDRUN Community",
  "author_url": "https://github.com/peldrun/peldrun-providers",
  "homepage": "https://developers.sber.ru/docs/ru/gigachat/guides/main",
  "license": "MIT",
  "icon": "gigachat",
  "tags": ["gigachat", "sber", "russian", "oauth"],
  "connection_fields": [
    {
      "key": "credentials",
      "label": "Authorization Key",
      "type": "password",
      "required": true,
      "placeholder": "Base64-encoded client_id:client_secret",
      "description": "From the Sber Studio dashboard."
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
      ]
    },
    {
      "key": "ca_bundle_path",
      "label": "Russian Root CA Bundle",
      "type": "file",
      "required": false,
      "accept": ".pem,.crt,.cer",
      "placeholder": "/etc/ssl/certs/russian_trusted_root_ca.pem",
      "visible_if": {"scope": "B2B"}
    },
    {
      "key": "verify_ssl",
      "label": "Verify SSL",
      "type": "boolean",
      "default": true
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

### Full custom package (AWS Bedrock)

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "aws-bedrock",
  "name": "AWS Bedrock",
  "display_name": "AWS Bedrock",
  "version": "1.0.0",
  "description": "AWS Bedrock provides access to foundation models from Anthropic, Meta, Mistral, Cohere, and Amazon, with enterprise-grade security.",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "native",
  "entry_point": "bedrock_peldrun.adapter:BedrockAdapter",
  "dependencies": ["boto3>=1.34,<2.0", "botocore>=1.34,<2.0"],
  "runtime": {
    "type": "python",
    "python": ">=3.12,<3.13"
  },
  "author": "PELDRUN Community",
  "homepage": "https://docs.aws.amazon.com/bedrock",
  "license": "MIT",
  "tags": ["aws", "bedrock", "enterprise", "cloud"],
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
    },
    {
      "key": "model_id",
      "label": "Bedrock Model ID",
      "type": "string",
      "required": true,
      "default": "anthropic.claude-3-5-sonnet-20241022-v2:0"
    }
  ],
  "runtime_hints": {
    "docs_url": "https://docs.aws.amazon.com/bedrock"
  },
  "features": {
    "model_discovery": true,
    "native_streaming": true,
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

---

## Validation rules summary

Quick reference table:

| Field | Type | Required | Constraint |
|-------|------|----------|------------|
| `schema_version` | string | ✅ | Must be `"1.0.0"` |
| `kind` | string | ✅ | Must be `"peldrun.llm_provider"` |
| `id` | string | ✅ | `^[a-z0-9][a-z0-9._-]*$`, no builtin collision |
| `name` | string | ✅ | Any non-empty string |
| `version` | string | ✅ | Valid semver |
| `display_name` | string \| null | — | Any string |
| `description` | string | — | Any string |
| `api_contract` | string | ✅ | Must be `"peldrun.llm.provider.v1"` |
| `transport` | enum | ✅ | `compatible` / `native` / `custom` |
| `entry_point` | string \| null | Conditional | `module:Class` pattern; required for native/custom |
| `base_adapter` | string \| null | Conditional | `^[a-z0-9][a-z0-9._-]*$`; required for compatible |
| `author` | string \| null | — | Any string |
| `author_url` | string \| null | — | Any string |
| `homepage` | string \| null | — | Any string |
| `license` | string \| null | — | Any string |
| `icon` | string \| null | — | Any string |
| `tags` | string[] | — | Each must be non-empty |
| `connection_fields` | ConnectionFieldSpec[] | — | See section above |
| `runtime_hints` | object | — | See section above |
| `features` | object | — | All flags default `false` |
| `permissions` | object | ✅ | `network` + `secrets` + `workspace_access=false` + `shell_execute=false` |
| `runtime` | object | — | `type` = `"python"`, `python` = PEP 440 specifier |
| `dependencies` | string[] | — | PEP 508 specifier strings |

**Unknown fields**: The manifest uses `extra="forbid"`. Any field not in this
table causes a `manifest.<field>: Extra inputs are not permitted` error.

---

## What's next

- **Need to write the adapter code?** → [Adapter Protocol](06-adapter-protocol.md)
- **Need to add custom connection fields?** → [Connection Fields](05-connection-fields.md)
- **Need examples?** → [Examples](13-examples.md)
- **Hitting an error?** → [Troubleshooting](14-troubleshooting.md)
```

---

