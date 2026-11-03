### `docs/providers/05-connection-fields.md`

```markdown
<!-- docs/providers/05-connection-fields.md -->
# Connection Fields — Complete Reference

> **Complete reference** for `connection_fields[]` in `manifest.json`.
>
> **Reading time**: 12-15 minutes.
>
> **Who this is for**: Anyone designing the configuration form for a
> Provider Package.
>
> **What you'll learn**: Every field property, every input type, how values
> flow from the UI to the adapter's Python code, and the patterns real
> providers use.

---

## Table of Contents

- [What connection fields are](#what-connection-fields-are)
- [Where the values end up](#where-the-values-end-up)
- [The two implicit fields](#the-two-implicit-fields)
- [Field-by-field reference](#field-by-field-reference)
- [Field types](#field-types)
- [Conditional visibility](#conditional-visibility)
- [Reading fields in the adapter](#reading-fields-in-the-adapter)
- [Best practices](#best-practices)
- [Common patterns](#common-patterns)
- [Anti-patterns](#anti-patterns)
- [Complete real-world example](#complete-real-world-example)

---

## What connection fields are

A `connection_fields[]` array declares the inputs the user must fill in when
creating a **connection** for your provider. Each entry in the array becomes
one input in the Configure modal.

**Without connection fields** (empty array), the form shows only:
- `base_url` — the API endpoint URL
- `api_key` — the authentication secret

**With connection fields**, the form shows those two plus whatever else you
declare — `scope`, `region`, `project_id`, `ca_bundle_path`, `deployment_name`,
etc.

### Why they exist

Different providers need different data:

| Provider | Fields needed |
|----------|--------------|
| OpenAI | `base_url`, `api_key` |
| Anthropic | `base_url`, `api_key`, `anthropic_version` |
| Google Gemini | `base_url`, `api_key` |
| Google Vertex AI | `project_id`, `location`, `service_account_json` |
| AWS Bedrock | `region`, `access_key_id`, `secret_access_key`, `session_token`, `model_id` |
| Azure OpenAI | `azure_endpoint`, `deployment_name`, `api_version`, `api_key` |
| GigaChat | `credentials`, `scope`, `ca_bundle_path`, `verify_ssl` |
| YandexGPT | `folder_id`, `iam_token` |
| OpenRouter | `base_url`, `api_key`, `http_referer`, `x_title` |

Connection fields let each package declare its own form — no changes to the
PELDRUN UI are needed.

---

## Where the values end up

Understanding the data flow is essential before writing your first field.
Here is what happens from the user clicking "Create Connection" to the adapter
reading a value.

### The full flow

```
User fills in the form
  ↓
Store UI collects values
  ↓
POST /api/store/providers/{id}/connections
    {
      "name": "Personal",
      "fields": {
        "base_url": "https://api.acme.com/v1",
        "api_key": "sk-acme-...",
        "scope": "PERS"
      },
      "is_active": true
    }
  ↓
Backend saves to models_vault.json:
    {
      "provider_connections": [{
        "id": "conn_abc123",
        "provider_package_id": "acme-ai",
        "name": "Personal",
        "enabled": true,
        "is_active": true,
        "fields": {
          "base_url": "https://api.acme.com/v1",
          "api_key": "sk-acme-...",
          "scope": "PERS"
        }
      }]
    }
  ↓
Later, at request time:
  dispatcher._resolve_provider_and_extras() reads the active connection
  ↓
Builds ConnectionContext:
    ConnectionContext(
      base_url="https://api.acme.com/v1",   ← from fields.base_url
      api_key="sk-acme-...",                  ← from fields.api_key
      extra_params={
        "_peldrun_plugin": {...},
        "scope": "PERS",                      ← all other fields
      },
    )
  ↓
Adapter receives context:
    context.connection.base_url    → "https://api.acme.com/v1"
    context.connection.api_key    → "sk-acme-..."
    context.extra_params["scope"] → "PERS"
```

### The three-field rule

Every value you declare in `connection_fields` follows the **three-field
rule**:

1. **Field declared** in the manifest's `connection_fields[]`
2. **Field rendered** in the Configure modal (one input per spec)
3. **Value stored** in the connection's `fields` dict in `models_vault.json`

### Two special keys

Two keys have special meaning and are **hoisted** out of `extra_params` before
the adapter sees them:

| Key | Hoisted to | Read in adapter as |
|-----|-----------|-------------------|
| `base_url` | `connection.base_url` | `context.connection.base_url` |
| `api_key` | `connection.api_key` | `context.connection.api_key` |

All other fields go into `connection.extra_params`. This is why the adapter
reads them as a dict:

```python
scope = context.extra_params["scope"]
region = context.extra_params["region"]
```

### Reserved key

One key is **reserved** and cannot be declared in `connection_fields`:

- `_peldrun_plugin` — used by the runtime to pass the plugin descriptor

If you declare it, install fails with a validation error. Always pop it before
iterating `extra_params` in your adapter:

```python
extra = dict(self._connection.extra_params or {})
extra.pop("_peldrun_plugin", None)  # ← always remove
```

---

## The two implicit fields

You do not need to declare `base_url` or `api_key` in `connection_fields`. The
Store renders them automatically:

| Implicit field | Type | Required | Description shown |
|---------------|------|----------|-------------------|
| `base_url` | `url` | ✅ | "The base URL of the provider's API. The adapter appends its own path." |
| `api_key` | `password` | ❌ | "Sent as a Bearer token by default. Leave empty for local providers that do not require authentication." |

### When to override them

If your provider needs a **different label, placeholder, or default** for
these keys, declare them explicitly in `connection_fields`. The declaration
overrides the implicit version:

```json
"connection_fields": [
  {
    "key": "base_url",
    "label": "Bedrock Endpoint",
    "type": "url",
    "required": true,
    "default": "https://bedrock-runtime.us-east-1.amazonaws.com",
    "description": "The AWS Bedrock endpoint for your region."
  },
  {
    "key": "api_key",
    "label": "Bedrock API Key (optional)",
    "type": "password",
    "required": false,
    "description": "Leave empty if using SigV4 credential signing."
  },
  {
    "key": "region",
    "label": "AWS Region",
    "type": "select",
    "required": true,
    "default": "us-east-1",
    "options": [...]
  }
]
```

### When to accept the implicit version

For most providers — especially OpenAI-compatible ones — the defaults are
perfectly adequate:

```json
"connection_fields": []
```

This renders exactly two inputs: Endpoint URL and API Key.

---

## Field-by-field reference

Every entry in `connection_fields[]` is a `ConnectionFieldSpec`. Here are all
its properties.

### `key` — required

**Type**: `string`
**Pattern**: No formal constraint, but must be unique within the array
**Purpose**: The identifier used in `models_vault.json` and `extra_params`

**Rules**:
- Must be unique within `connection_fields[]`
- Should be `snake_case` for consistency
- Must be a valid Python identifier (letters, digits, underscores; not starting with a digit) — this is not enforced by the schema, but any key that violates it will not be readable via `context.extra_params[key]` in Python

**Examples**:
```json
"key": "scope"                   ✅
"key": "project_id"              ✅
"key": "api_version"             ✅
"key": "scope-account"           ⚠️ (valid but not Python-accessible via dot notation)
"key": "scope.account"           ⚠️ (same)
"key": "1scope"                  ❌ (starts with digit — not accessible as attribute)
"key": "_peldrun_plugin"         ❌ (reserved)
```

### `label` — required

**Type**: `string`
**Purpose**: The user-facing label shown above the input

**Examples**:
```json
"label": "AWS Region"
"label": "Account Scope"
"label": "Service Account JSON"
```

**Best practices**:
- Keep it under 40 characters
- Use title case
- Do not add a trailing colon (the form adds it automatically)

### `type` — optional

**Type**: `string` (enum)
**Default**: `"string"`
**Allowed values**: see [Field types](#field-types) below
**Purpose**: Determines the HTML input kind

### `required` — optional

**Type**: `boolean`
**Default**: `false`
**Purpose**: Whether the form blocks submission when the field is empty

**Behavior**:
- When `true`, the Save button stays disabled until the field has a non-empty
  value
- A red asterisk appears next to the label
- When the field is hidden via `visible_if`, the requirement is not enforced

### `default` — optional

**Type**: `any` (must match the field type)
**Default**: `null`
**Purpose**: The initial value

**Examples by type**:
```json
"type": "string",  "default": "us-east-1"
"type": "select",  "default": "PERS"
"type": "number",  "default": 60
"type": "boolean", "default": true
```

**Behavior**:
- When `null` (or absent), the field starts empty (or with the first
  `options` value for `select`)
- When set, the field is pre-populated
- The default is applied **once** when the connection is created; subsequent
  edits preserve the user's changes

### `placeholder` — optional

**Type**: `string | null`
**Default**: `null`
**Purpose**: Grey hint text shown when the field is empty

**Examples**:
```json
"placeholder": "sk-..."
"placeholder": "/etc/ssl/certs/ca-bundle.pem"
"placeholder": "https://api.example.com/v1"
"placeholder": "2024-02-01"
```

**Best practices**:
- Show an example value, not a description
- For paths, show a realistic absolute path
- For API keys, show the prefix (`sk-`, `Bearer`, etc.)

### `description` — optional

**Type**: `string | null`
**Default**: `null`
**Purpose**: Helper text shown below the input

**Examples**:
```json
"description": "Found in the Sber Studio dashboard under 'Settings → API Keys'."
"description": "The API version must match YYYY-MM-DD format."
"description": "Disable SSL verification ONLY for development."
```

**Best practices**:
- Explain *where* to find the value, not *what* it is
- Mention format requirements ("must be YYYY-MM-DD", "base64-encoded")
- Keep under 200 characters

### `options` — for `select` type only

**Type**: `Array<{value: string, label: string}> | null`
**Default**: `null`
**Purpose**: The dropdown choices

**Shape**:
```json
"options": [
  {"value": "us-east-1", "label": "US East (N. Virginia)"},
  {"value": "eu-west-1", "label": "EU (Ireland)"}
]
```

**Rules**:
- `value` is what gets stored (in `models_vault.json`)
- `label` is what gets displayed (in the UI)
- Both must be strings
- At least one option should be present
- When absent, the dropdown shows "(No options declared)" and cannot be saved

### `visible_if` — optional

**Type**: `object | null`
**Default**: `null`
**Purpose**: Show the field only when other fields have specific values

**Shape**:
```json
"visible_if": {"field_key_1": "expected_value_1", "field_key_2": 42}
```

**Behavior**: All conditions are ANDed. The field appears only when every
referenced field matches its expected value exactly.

See [Conditional visibility](#conditional-visibility) below for details.

### `accept` — for `file` / `secret_file` types

**Type**: `string | null`
**Default**: `null`
**Purpose**: Comma-separated extensions the file should have. **Client-side
hint only** — not enforced by the backend.

**Examples**:
```json
"accept": ".pem,.crt,.cer"
"accept": ".json"
"accept": ".pem,.crt,.key"
```

### `min_length` / `max_length` — optional

**Type**: `int | null`
**Default**: `null`
**Purpose**: Minimum/maximum string length. Client-side validation only.

**Examples**:
```json
"min_length": 8
"max_length": 200
```

**Note**: These are advisory. A user with the Developer Tools open can bypass
them. The adapter must validate on use if the constraint matters.

### `pattern` — optional

**Type**: `string | null` (regex)
**Default**: `null`
**Purpose**: A regex the value must match. Client-side validation only.

**Examples**:
```json
"pattern": "^\\d{4}-\\d{2}-\\d{2}$"        // YYYY-MM-DD
"pattern": "^sk-[A-Za-z0-9]{32,}$"          // OpenAI-style key
"pattern": "^[a-z]{2}-[a-z]+-\\d$"          // AWS region
```

**Note**: The pattern is applied via the HTML `pattern` attribute, which
implicitly anchors to `^...$`. You do not need to add anchors, but including
them is harmless.

**Escape backslashes**: In JSON, `\d` must be written as `\\d`.

---

## Field types

Nine input types are supported. Each renders as a different HTML element.

### `string` (default)

Single-line text input. Most common type for identifiers and API-specific
values.

```json
{
  "key": "project_id",
  "label": "Project ID",
  "type": "string",
  "required": true,
  "placeholder": "my-gcp-project",
  "description": "Found in the Google Cloud Console header."
}
```

**Renders as**: `<input type="text">`

**Use for**: IDs, model names, API versions, region names (as text),
usernames.

---

### `password`

Same as `string`, but the value is masked. Includes a "Show" toggle.

```json
{
  "key": "api_key",
  "label": "API Key",
  "type": "password",
  "required": true,
  "placeholder": "sk-..."
}
```

**Renders as**: `<input type="password">` with a show/hide button

**Special behavior**:
- **Stored in plaintext** in `models_vault.json` — this is intentional. The
  adapter needs the actual value at request time.
- **Masked in API responses** as `••••••••`
- **Preserved on update** when the user submits the masked value (the
  backend's `save_provider_connection()` merge logic keeps the original)
- **Replaced on update** when the user submits a new value

**Use for**: API keys, OAuth secrets, tokens, passwords.

---

### `url`

Same as `string`, but uses `type="url"` (browsers validate the URL format).

```json
{
  "key": "base_url",
  "label": "Endpoint URL",
  "type": "url",
  "required": true,
  "default": "https://api.example.com/v1",
  "placeholder": "https://api.example.com/v1"
}
```

**Renders as**: `<input type="url">`

**Use for**: Base URLs, metadata endpoints, webhook URLs.

**Note**: The browser validation requires a scheme (`http://` or `https://`).
A bare hostname like `api.example.com` will be rejected by the browser.

---

### `number`

Numeric input. Accepts floats.

```json
{
  "key": "timeout_seconds",
  "label": "Request Timeout",
  "type": "number",
  "default": 60,
  "description": "Seconds before a request is abandoned."
}
```

**Renders as**: `<input type="number">`

**Use for**: Timeouts, retry counts, temperature, port numbers.

---

### `boolean`

Checkbox.

```json
{
  "key": "verify_ssl",
  "label": "Verify SSL Certificates",
  "type": "boolean",
  "default": true,
  "description": "Disable ONLY for development against a local mock server."
}
```

**Renders as**: `<input type="checkbox">` with "Enabled" / "Disabled" label

**Use for**: Feature toggles, SSL verification, strict mode, disable/enable
flags.

---

### `select`

Dropdown with predefined options.

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
  ],
  "description": "Which Sber API scope to authenticate against."
}
```

**Renders as**: `<select>` with the declared options

**Use for**: Enum-like choices — regions, tiers, protocol versions, auth
scopes.

**Rules**:
- At least one option should be declared
- Each option must have both `value` and `label`
- `value` is what gets stored, `label` is what gets shown
- The `default` should match one of the `value`s

**Why `select` over `string`**: Prevents typos. A user typing `"Us-East-1"` in
a `string` field would send an invalid region. A `select` enforces the exact
AWS-provided values.

---

### `textarea`

Multi-line text input.

```json
{
  "key": "service_account_json",
  "label": "Service Account JSON",
  "type": "textarea",
  "required": true,
  "placeholder": "Paste the full JSON key from Google Cloud Console...",
  "description": "The full contents of the service account key file."
}
```

**Renders as**: `<textarea rows={4}>`

**Use for**: Small JSON blobs, PEM certificates, multi-line config strings.

**Warning**: Large content blurs the line between "config" and "data". If the
value is over ~4KB or is a binary-safe artifact, prefer `file` type (path to
a file on the PELDRUN host).

---

### `file`

Path to a file on the **PELDRUN backend host**. The user must upload the file
to the backend manually (via SSH, the Files UI, or by copying it into the
workspace) before entering its path.

```json
{
  "key": "ca_bundle_path",
  "label": "Custom CA Bundle",
  "type": "file",
  "required": true,
  "accept": ".pem,.crt,.cer",
  "placeholder": "/etc/ssl/certs/ca-bundle.pem",
  "description": "Absolute path on the PELDRUN host to a PEM file containing the root CA."
}
```

**Renders as**: `<input type="text">` with a hint that the value is a path

**Why not a real file upload**: The Store UI cannot transfer arbitrary binary
files to the backend without a separate upload endpoint, and PELDRUN already
has one (the Files API). Using a path keeps the connection lightweight and
gives the operator full control over where files live.

**Use for**: TLS certificate bundles, mTLS client certificates, SSH private
keys, service account key files.

**Security note**: The path is stored as a string. The adapter is responsible
for opening it safely. Never construct a path from an untrusted source.

---

### `secret_file`

Same as `file` but the value is masked in API responses (`••••••••`).

```json
{
  "key": "mtls_client_cert",
  "label": "Client Certificate (mTLS)",
  "type": "secret_file",
  "required": true,
  "accept": ".pem,.crt",
  "placeholder": "/etc/ssl/private/client.pem",
  "description": "Path to your mTLS client certificate on the PELDRUN host."
}
```

**Renders as**: `<input type="password">` with a show/hide toggle

**Use for**: Secret material referenced by path — client certificates,
encrypted keys, credential files.

---

## Conditional visibility

`visible_if` hides a field until other fields have specific values.

### Basic example

Only ask for a CA bundle when the user picks B2B:

```json
"connection_fields": [
  {
    "key": "scope",
    "label": "Account Scope",
    "type": "select",
    "required": true,
    "default": "PERS",
    "options": [
      {"value": "PERS", "label": "Personal"},
      {"value": "B2B", "label": "Business"}
    ]
  },
  {
    "key": "ca_bundle_path",
    "label": "CA Bundle",
    "type": "file",
    "required": true,
    "visible_if": {"scope": "B2B"}
  }
]
```

**Behavior**:
- User selects "Personal" → CA bundle field is hidden
- User selects "Business" → CA bundle field appears with a red asterisk
- User switches back → CA bundle field hides again

### Multiple conditions

All conditions must match (ANDed):

```json
{
  "key": "mtls_cert_path",
  "label": "Client Certificate",
  "type": "secret_file",
  "visible_if": {
    "auth_mode": "mtls",
    "enable_tls": true
  }
}
```

The field appears only when `auth_mode == "mtls"` **AND** `enable_tls == true`.

### Boolean conditions

`visible_if` compares against exact values. For booleans:

```json
{
  "visible_if": {"verify_ssl": false}
}
```

The field appears when `verify_ssl` is `false` — i.e., when the user disabled
SSL verification (which usually means "I know I need a custom CA bundle").

### Common patterns

**Progressive disclosure**:
```json
[
  {"key": "auth_mode", "type": "select", "options": [
    {"value": "api_key", "label": "API Key"},
    {"value": "oauth", "label": "OAuth2"},
    {"value": "mtls", "label": "Mutual TLS"}
  ]},
  {"key": "api_key", "type": "password", "visible_if": {"auth_mode": "api_key"}},
  {"key": "client_id", "type": "string", "visible_if": {"auth_mode": "oauth"}},
  {"key": "client_secret", "type": "password", "visible_if": {"auth_mode": "oauth"}},
  {"key": "mtls_cert", "type": "file", "visible_if": {"auth_mode": "mtls"}}
]
```

This renders a clean form: only the fields relevant to the chosen
authentication mode are visible.

**Region-dependent endpoints**:
```json
[
  {"key": "region", "type": "select", "options": [
    {"value": "us", "label": "United States"},
    {"value": "eu", "label": "European Union"}
  ]},
  {"key": "us_data_residency", "type": "boolean", "visible_if": {"region": "us"}},
  {"key": "gdpr_compliance", "type": "boolean", "visible_if": {"region": "eu"}}
]
```

### What happens to hidden fields

**Validation**: A hidden field's `required` constraint is **not** enforced.
The form submits without it.

**Value preservation**: When a field becomes hidden, its previous value remains
in the form state. If the user re-shows it, the value comes back. On save,
hidden fields are still submitted (so switching modes does not lose data).

**Adapter behavior**: The adapter receives whatever is in `extra_params`,
regardless of `visible_if`. The adapter must decide whether to ignore
irrelevant fields.

---

## Reading fields in the adapter

In `native` adapters, connection fields arrive as `extra_params`:

```python
class MyAdapter:
    def __init__(self, context):
        self._connection = context.connection

        # Remove the reserved descriptor key
        extra = dict(self._connection.extra_params or {})
        extra.pop("_peldrun_plugin", None)

        # Read standard fields
        base_url = self._connection.base_url
        api_key = self._connection.api_key

        # Read custom fields (with defaults for safety)
        scope = str(extra.get("scope") or "personal")
        region = str(extra.get("region") or "us-east-1")
        timeout = int(extra.get("timeout") or 60)
        verify_ssl = bool(extra.get("verify_ssl", True))
        ca_bundle_path = extra.get("ca_bundle_path") or None

        # Use them
        self._client = MySDKClient(
            base_url=base_url,
            api_key=api_key,
            scope=scope,
            region=region,
            timeout=timeout,
            verify=verify_ssl,
            ca_bundle=ca_bundle_path,
        )
```

### Handling missing values

Connection fields are user-supplied. Assume they might be missing or empty:

```python
# WRONG — crashes if scope is empty
scope = extra["scope"]

# RIGHT — provides a safe default
scope = str(extra.get("scope") or "personal")
```

### Handling optional fields

For optional fields (e.g., a CA bundle that is only needed in some cases):

```python
ca_bundle_path = extra.get("ca_bundle_path") or None
if ca_bundle_path is not None:
    if not Path(ca_bundle_path).is_file():
        raise ProviderInvalidRequestError(
            f"CA bundle file not found: {ca_bundle_path}",
            provider="my-provider",
        )
    client = MySDKClient(ca_bundle=ca_bundle_path)
else:
    client = MySDKClient()
```

### Validating values

The manifest's `pattern`, `min_length`, and `max_length` are **client-side
only**. The adapter must validate on use:

```python
credentials = extra.get("credentials")
if not credentials:
    raise ProviderAuthError(
        "The 'credentials' field is required.",
        provider="my-provider",
    )
if not credentials.strip():
    raise ProviderAuthError(
        "The 'credentials' field is empty.",
        provider="my-provider",
    )
try:
    # Base64-decode the credentials
    decoded = base64.b64decode(credentials).decode("utf-8")
except Exception:
    raise ProviderInvalidRequestError(
        "The 'credentials' field is not valid base64.",
        provider="my-provider",
    )
```

### Compatible adapters

In `compatible` packages, connection fields are **not read directly** by the
plugin adapter. The built-in base adapter (`openai_compat`, `anthropic`,
etc.) reads `base_url` and `api_key`. Any extra fields are typically ignored
unless the base adapter was designed to look for them.

**OpenRouter example**: The `openai_compat` adapter does not know about
`http_referer` or `x_title`. That is why the manifest declares
`runtime_hints.custom_headers_from_fields` — the runtime injects them at the
HTTP layer, bypassing the adapter:

```json
"runtime_hints": {
  "custom_headers_from_fields": {
    "http_referer": "HTTP-Referer",
    "x_title": "X-Title"
  }
}
```

**GigaChat-style example**: Since `compatible` adapters do not read custom
fields, if you need auth logic that touches custom fields, use `native` or
`custom` transport.

---

## Best practices

### 1. Name fields after what the provider calls them

If the provider's docs say "Account Scope", call it `"Account Scope"` — not
"Authentication Type" or "Mode". The user has the provider's docs open; the
form should match.

```json
"label": "Account Scope"     ✅
"label": "Auth Mode"          ❌ (vague)
```

### 2. Always provide a `description` for non-obvious fields

Users do not know what `credentials` is. Tell them.

```json
{
  "key": "credentials",
  "label": "Authorization Key",
  "type": "password",
  "description": "From the Sber Studio dashboard. This is the 'Authorization key' for your GigaChat app."
}
```

### 3. Use `select` for enums, `string` only for free-form values

| Field | `select` | `string` |
|-------|----------|----------|
| AWS region | ✅ (fixed list) | ❌ (typos) |
| API version | ✅ (dated versions) | ❌ |
| Base URL | ❌ | ✅ (users may have proxies) |
| Model name | ❌ | ✅ (varies) |
| Project ID | ❌ | ✅ (user's own) |
| Account tier | ✅ (fixed) | ❌ |

### 4. Provide sensible defaults

A good default reduces friction:

```json
{
  "key": "region",
  "type": "select",
  "default": "us-east-1"
}
```

The user can accept the default or change it. A field with no default forces
a choice even when the answer is obvious.

### 5. Group related fields with `visible_if`

Do not show the mTLS certificate path unless the user chose mTLS auth:

```json
{"key": "mtls_cert", "visible_if": {"auth_mode": "mtls"}}
```

### 6. Validate aggressively in the adapter

Trust the user's input as far as you trust the network. Assume:

- The field might be empty
- The field might contain whitespace
- The field might contain the masked placeholder (`••••••••`) if your code
  accidentally skips the "preserve on unchanged" logic
- The field might contain an injection attempt

```python
credentials = str(extra.get("credentials") or "").strip()
if not credentials or credentials == "••••••••":
    raise ProviderAuthError("Credentials missing.")
```

### 7. Do not store large values

`models_vault.json` is a single JSON file. A 5MB service account key makes
every read of the file 5MB slower. Use `file` type for large values:

```json
{
  "key": "service_account_path",
  "type": "file",
  "description": "Path to the JSON key on the PELDRUN host."
}
```

### 8. Keep the number of fields under 6

The Configure modal is already crowded. Each additional field is friction.
Combine related settings where possible, or move them to `runtime_hints`.

---

## Common patterns

### OpenAI-compatible provider

```json
"connection_fields": []
```

Two fields (URL + key), both with good defaults. Done.

### Anthropic-style provider

```json
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

### Azure OpenAI

```json
"connection_fields": [
  {
    "key": "base_url",
    "label": "Azure Endpoint",
    "type": "url",
    "required": true,
    "placeholder": "https://YOUR-RESOURCE.openai.azure.com",
    "description": "Your Azure OpenAI endpoint. Found in the Azure portal."
  },
  {
    "key": "api_version",
    "label": "API Version",
    "type": "string",
    "required": true,
    "default": "2024-02-01",
    "pattern": "^\\d{4}-\\d{2}-\\d{2}(-preview)?$"
  },
  {
    "key": "deployment_name",
    "label": "Deployment Name",
    "type": "string",
    "required": true,
    "description": "The name you gave your model deployment in Azure."
  }
]
```

### AWS Bedrock

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
    "description": "Only for temporary STS credentials."
  }
]
```

### GigaChat

```json
"connection_fields": [
  {
    "key": "credentials",
    "label": "Authorization Key",
    "type": "password",
    "required": true,
    "description": "Base64-encoded client_id:client_secret from Sber Studio."
  },
  {
    "key": "scope",
    "label": "Account Scope",
    "type": "select",
    "required": true,
    "default": "PERS",
    "options": [
      {"value": "PERS", "label": "Personal"},
      {"value": "B2B", "label": "Business"},
      {"value": "CORP", "label": "Corporate"}
    ]
  },
  {
    "key": "ca_bundle_path",
    "label": "Russian Root CA Bundle",
    "type": "file",
    "required": true,
    "accept": ".pem,.crt",
    "visible_if": {"scope": "B2B"}
  }
]
```

### Google Vertex AI (OAuth + service account)

```json
"connection_fields": [
  {
    "key": "project_id",
    "label": "GCP Project ID",
    "type": "string",
    "required": true,
    "placeholder": "my-gcp-project",
    "description": "From the Google Cloud Console header."
  },
  {
    "key": "location",
    "label": "Region",
    "type": "select",
    "required": true,
    "default": "us-central1",
    "options": [
      {"value": "us-central1", "label": "US Central (Iowa)"},
      {"value": "europe-west1", "label": "Europe (Belgium)"},
      {"value": "asia-northeast1", "label": "Asia (Tokyo)"}
    ]
  },
  {
    "key": "service_account_path",
    "label": "Service Account Key File",
    "type": "secret_file",
    "required": true,
    "accept": ".json",
    "placeholder": "/etc/peldrun/gcp-sa.json",
    "description": "Absolute path to the JSON key on the PELDRUN host."
  }
]
```

---

## Anti-patterns

### ❌ Duplicating `base_url` and `api_key` unnecessarily

```json
"connection_fields": [
  {"key": "base_url", "label": "Base URL", "type": "url"},
  {"key": "api_key", "label": "API Key", "type": "password"}
]
```

If you have nothing to customize, leave `connection_fields` empty. The
implicit fields handle it.

**Only declare them when you need to change the label, default, or
description.**

### ❌ Declaring fields the adapter ignores

```json
// manifest
"connection_fields": [
  {"key": "custom_option", "type": "string"}
]

// adapter.py — never reads custom_option
```

The user fills in a field that has no effect. Confusing.

### ❌ Using `password` for non-secret fields

```json
{
  "key": "model_name",
  "label": "Model Name",
  "type": "password"  // ← WRONG
}
```

The model name is visible in every request. Masking it hides useful info from
the user.

**Use `password` only for actual secrets.**

### ❌ No default for `select`

```json
{
  "key": "region",
  "type": "select",
  "options": [...]
  // ← no default — form starts with no selection
}
```

The user must open the dropdown and pick something. Add a sensible default:

```json
"default": "us-east-1"
```

### ❌ Relying on `pattern` for security

```json
{
  "key": "api_key",
  "type": "password",
  "pattern": "^sk-[A-Za-z0-9]{32,}$"  // ← client-side only
}
```

The user can bypass this by opening DevTools. The adapter must validate:

```python
api_key = extra.get("api_key", "")
if not api_key.startswith("sk-"):
    raise ProviderInvalidRequestError("Invalid API key format.")
```

### ❌ Storing files as `textarea`

```json
{
  "key": "service_account_json",
  "type": "textarea"  // ← 5KB JSON blob in models_vault.json
}
```

Use `file` and require a path. The vault stays small, and file permissions are
enforced by the OS.

### ❌ Hidden required fields never satisfied

```json
[
  {"key": "mode", "type": "select", "options": [
    {"value": "a", "label": "Option A"},
    {"value": "b", "label": "Option B"}
  ]},
  {"key": "extra", "required": true, "visible_if": {"mode": "c"}}  // ← "c" is not an option
]
```

The `extra` field is required but can never become visible. The user can never
save a valid connection.

**Fix**: Use a value that actually appears in the options.

### ❌ Reusing `_peldrun_plugin` or `_peldrun_*` keys

```json
{
  "key": "_peldrun_base_url",  // ← RESERVED, causes validation error
  "type": "string"
}
```

Any key starting with `_peldrun_` is reserved. Install fails.

---

## Complete real-world example

Here is the full connection configuration for the **GigaChat** package:

```json
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
]
```

**What the user sees**:

1. **Authorization Key** input with description
2. **Account Scope** dropdown, default "Personal (PERS)"
3. **Verify SSL Certificates** checkbox, default checked

**If the user selects "Business (B2B)":** The form now shows the CA bundle
field (with `visible_if: {"scope": "B2B"}`), and that field becomes **required**.

**What the adapter reads**:

```python
class GigaChatAdapter:
    def __init__(self, context):
        self._connection = context.connection
        extra = dict(self._connection.extra_params or {})
        extra.pop("_peldrun_plugin", None)

        # Standard fields
        base_url = self._connection.base_url   # e.g. "https://gigachat.devices.sberbank.ru/api/v1"
        api_key = self._connection.api_key      # may be empty

        # Custom fields
        credentials = extra.get("credentials") or ""       # "dGVzdDp0ZXN0"
        scope = extra.get("scope") or "PERS"                # "PERS" | "B2B" | "CORP"
        ca_bundle_path = extra.get("ca_bundle_path")        # None for PERS, path for B2B
        verify_ssl = bool(extra.get("verify_ssl", True))    # True | False

        # Validate credentials exist
        if not credentials:
            raise ProviderAuthError(
                "GigaChat requires the 'credentials' field.",
                provider="gigachat",
            )

        self._client = GigaChat(
            credentials=credentials,
            scope=scope,
            verify_ssl_certs=verify_ssl,
            ca_bundle_file=ca_bundle_path,
        )
```

**What gets saved in `models_vault.json`**:

```json
{
  "provider_connections": [{
    "id": "conn_6c87efd...",
    "provider_package_id": "gigachat",
    "name": "Personal",
    "enabled": true,
    "is_active": true,
    "fields": {
      "credentials": "dGVzdDp0ZXN0",
      "scope": "PERS",
      "verify_ssl": true
    },
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:00Z"
  }]
}
```

Note that `ca_bundle_path` is missing — the user chose PERS scope, so that
field never appeared in the form, so its value is absent. The adapter handles
this gracefully with `extra.get("ca_bundle_path")` returning `None`.

---

## What's next

- **Need to write the adapter that reads these fields?** → [Adapter Protocol](06-adapter-protocol.md)
- **Need to declare UI warnings?** → [Runtime Hints](07-runtime-hints.md)
- **Need examples of full packages?** → [Examples](13-examples.md)
- **Hitting a validation error?** → [Troubleshooting](14-troubleshooting.md)
```

---
