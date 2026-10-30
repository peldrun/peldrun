
```markdown
# MCP Server Manifest Reference

**Version:** 1.0.0
**Status:** Stable
**Last Updated:** October 2026

> Complete reference for building `manifest.json` files for MCP servers
> running under PELDRUN. Covers every field, its purpose, accepted
> values, defaults, and how the runtime consumes it.

---

## 1. Overview

An MCP server manifest is a JSON file that describes how PELDRUN should
discover, spawn/connect to, authenticate with, and display a Model
Context Protocol server. The runtime reads the manifest to:

- Know how to launch the server (command / URL / transport).
- Determine which credentials to ask for (`env_schema` + `auth_strategy`).
- Cache its tool list persistently and expose per-tool toggles.
- Render a professional store card with icon, badges, hints, and links.
- Enforce trust and safety boundaries in the UI.

**Manifests are discovered from two directories:**

| Directory | Purpose | `is_builtin` |
|---|---|---|
| `backend/omweb/extensions/builtins/*.json` | Shipped with PELDRUN | `true` (auto) |
| `backend/storage/store/extensions/*.json` | User-installed / mutated state | `false` (unless a matching builtin exists) |

**Identity rule:** a manifest is builtin iff a matching `{id}.json`
exists under `builtins/`. The system enforces this regardless of what
the file in `custom/` claims, so toggling a builtin never demotes it to
"custom".

---

## 2. File Naming & Location

| Type | Location | Filename |
|---|---|---|
| Builtin | `backend/omweb/extensions/builtins/` | `{id}.json` |
| Custom | `backend/storage/store/extensions/` | `{id}.json` |
| Bundle | `backend/storage/store/extensions/bundles/{id}/` | `manifest.json` (uploaded as `.zip`) |

The `id` field must match the filename (minus extension).

---

## 3. Field Reference

Fields are grouped by concern. **Required** fields must always be
present; **Recommended** fields improve the store UX; **Optional**
fields enable advanced features.

### 3.1 Core Identity (Required)

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `id` | string | ✅ | — | Unique registry id. Lowercase, `[a-z0-9_]` only. Must match filename. |
| `name` | string | ✅ | = `id` | Human-readable display name shown in store cards and modal headers. |
| `type` | string | ✅ | — | Must be `"mcp_server"` or `"mcp"`. |
| `description` | string | ✅ | — | One-line fallback description. Used when `short_description` is absent. |

### 3.2 Extended Identity (Recommended)

| Field | Type | Default | Description |
|---|---|---|---|
| `registry_id` | string | — | Reverse-DNS identifier (e.g. `"io.peldrun/mcp_github"`). Used for publishing to registries. |
| `short_description` | string | — | ~120 char summary shown in the store list card. |
| `store_description` | string | — | 400-2000 char detailed description shown in the detail drawer ("About" section). |
| `category` | string | `"general"` | Primary category (`development`, `system`, `data`, `communication`, `media`, `productivity`, ...). |
| `tags` | string[] | `[]` | Free-form tags for search and filtering. |

### 3.3 Visual Assets (Recommended)

| Field | Type | Description |
|---|---|---|
| `icon_svg` | string | Inline SVG markup. Preferred over `icon_url` because it inherits `currentColor`. Rendered at 40×40 in detail drawer, 24×24 in card. |
| `icon_url` | string | Fallback URL to an SVG/PNG icon if `icon_svg` is absent. |
| `screenshots` | string[] | URLs to store screenshots. Displayed in the detail drawer gallery. |
| `banner_url` | string | Wide banner for the store front page. |

**SVG safety:** inline SVGs are injected via `dangerouslySetInnerHTML`.
Do not include `<script>`, `<foreignObject>`, or `<image>` tags. Stick to
`<path>`, `<g>`, `<circle>`, `<rect>`, `<title>`, `<desc>`.

### 3.4 Author & Trust (Recommended)

| Field | Type | Description |
|---|---|---|
| `author` | object | `{name, url?, email?}` — displayed in "Made by" section. |
| `publisher_id` | string | Publisher namespace (e.g. `"org.peldrun.official"`). |
| `trust` | enum | `"verified"`, `"community"`, or `"unverified"`. `verified` shows a green shield in UI. |
| `signature` | string | SHA-256 of the manifest body (excluding the signature field itself), prefixed with `"sha256:"`. Verified by the runtime for builtin manifests. |

### 3.5 Licensing & Legal (Recommended)

| Field | Type | Description |
|---|---|---|
| `license` | string | SPDX identifier (e.g. `"MIT"`, `"Apache-2.0"`, `"proprietary"`). |
| `license_url` | string | Link to full license text. |
| `terms_url` | string | Terms of service. |
| `privacy_url` | string | Privacy policy (important for OAuth servers). |

### 3.6 Links (Optional)

| Field | Type | Description |
|---|---|---|
| `homepage` | string | Main product page. |
| `docs_url` | string | Documentation link. |
| `demo_url` | string | Interactive demo (or `null`). |
| `changelog_url` | string | Release notes. |
| `support_url` | string | Support / contact page. |
| `repository` | object | `{url, source}` — e.g. `{url: "https://github.com/...", source: "github"}`. |
| `upstream` | object | Source package info (see §3.6.1). |

#### 3.6.1 `upstream` — Origin of a wrapped package

Used when PELDRUN ships a wrapper around an NPM/PyPI package:

```json
"upstream": {
  "name": "@modelcontextprotocol/server-github",
  "registry": "npm",
  "registry_url": "https://www.npmjs.com/package/@modelcontextprotocol/server-github",
  "version": "0.6.2",
  "license": "MIT"
}
```

### 3.7 Versioning (Recommended)

| Field | Type | Default | Description |
|---|---|---|---|
| `version` | string | `"1.0.0"` | Manifest version (semver). Not the upstream version. |
| `release_date` | string | — | ISO 8601 date (`"2025-01-15"`). |
| `stability` | enum | `"stable"` | `"stable"`, `"beta"`, or `"alpha"`. Affects UI badge. |
| `min_app_version` | string | — | Minimum PELDRUN version required (e.g. `"2.4.0"`). |
| `lifecycle_status` | enum | `"active"` | `"active"`, `"deprecated"`, or `"archived"`. |

### 3.8 Execution Configuration (Required for functionality)

| Field | Type | Required for | Description |
|---|---|---|---|
| `transport` | enum | ✅ | `"stdio"`, `"sse"`, `"http"`, or `"streamable-http"`. Default `"stdio"` if omitted. |
| `command` | string | stdio | Executable + args as one string. E.g. `"npx -y @modelcontextprotocol/server-github"` or `"mcp-server-github"`. |
| `url` | string | sse/http | Base URL of the server (e.g. `"https://api.example.com/mcp/"`). |
| `headers` | object | HTTP only | Static headers sent with every request (e.g. `{"User-Agent": "PELDRUN/2.4"}`). Do NOT put `Authorization` here — use `auth_token` or OAuth. |
| `timeout` | number | — | Override the subsystem default `startup_timeout_seconds` (in seconds). |
| `auth_token` | string | HTTP only | Static bearer token baked into the manifest (rare — prefer OAuth). |
| `runtime` | string | Bundles | `"python"` or `"node"` — tells the bundle installer which dependency manager to use. |
| `entrypoint` | string | Bundles | Relative path to the entry file (e.g. `"server.py"`). Combined with `runtime` to synthesize `command`. |

### 3.9 Status & Flags (Auto-managed or optional)

| Field | Type | Default | Description |
|---|---|---|---|
| `status` | enum | `"ready"` | `"active"` or `"ready"`. Toggling is done via the Store UI, but manifests may declare an initial value. |
| `is_builtin` | boolean | auto | Read-only. Set by the registry based on file location. |
| `is_bundle` | boolean | auto | Read-only. Set by the bundle installer. |
| `bundle_dir` | string | auto | Read-only. Absolute path to the extracted bundle directory. |
| `read_only` | boolean | `false` | When `true`, the store UI hides write-capable controls (used for informational manifests). |

### 3.10 Authentication (Env-based)

| Field | Type | Description |
|---|---|---|
| `auth_strategy` | enum | `"env"`, `"oauth"`, `"apikey"`, `"api_key"`, `"bearer"`, `"token"`, or `"none"`. Determines the modal prompt. |
| `env_schema` | object | JSON Schema for required env vars. See §4. |

**Auth trigger rule:** the runtime treats a server as requiring auth
when *either*:
- `auth_strategy` is one of the recognized auth kinds, **or**
- `env_schema.required` contains a field whose property has
  `"secret": true`.

When triggered and no per-chat decision exists, the runtime emits
`mcp_auth_required` and opens the modal.

### 3.11 Authentication (OAuth)

| Field | Type | Description |
|---|---|---|
| `auth_strategy` | `"oauth"` | Required for OAuth flows. |
| `scopes` | string[] | Requested scopes. If absent, uses `scopes_supported` from AS metadata. |
| `client_metadata_url` | string | CIMD document URL (falls back to PELDRUN's hosted document). |

**OAuth requirements:**
- `transport` must be `"sse"`, `"http"`, or `"streamable-http"`.
- `url` must be present.
- The server must either support RFC 9728 discovery, or match a
  pre-registered hostname in `omweb/extensions/auth_manager.py`.

### 3.12 Settings (Server-level, not per-chat)

| Field | Type | Description |
|---|---|---|
| `settings_schema` | object | JSON Schema for non-secret server settings (e.g. `allowed_directories` for filesystem). |
| `env_schema` | object | JSON Schema for global env (encrypted at rest). |

### 3.13 Display Hints (Optional, for the store card)

| Field | Type | Description |
|---|---|---|
| `capabilities` | object | `{tools: bool, resources: bool, prompts: bool}`. |
| `tool_count` | integer | Displayed count before the actual tool list loads. |
| `badges` | string[] | Short labels (`"official"`, `"verified"`, `"requires-token"`, `"local-only"`, `"writes-data"`). |
| `hints` | object | See §3.13.1. |

#### 3.13.1 `hints` — At-a-glance facts

```json
"hints": {
  "setup_time": "1 minute",
  "requires_account": true,
  "requires_token": true,
  "cost": "free",
  "data_access": "read-write",
  "network_access": true,
  "rate_limit": "5,000 req/hour authenticated"
}
```

| Key | Type | Values |
|---|---|---|
| `setup_time` | string | Human-readable duration. |
| `requires_account` | boolean | Whether a third-party account is needed. |
| `requires_token` | boolean | Whether `env_schema` declares a secret. |
| `cost` | enum | `"free"`, `"freemium"`, `"paid"`. |
| `data_access` | enum | `"read-only"`, `"read-write"`, `"none"`. |
| `network_access` | boolean | `false` for local-only servers (filesystem, memory). |
| `rate_limit` | string | Free-form description. |

---

## 4. `env_schema` — JSON Schema for credentials

`env_schema` describes the environment variables the server needs. It
must be a standard JSON Schema object (`type: "object"`).

### 4.1 Recognized fields

| Property | Type | Purpose |
|---|---|---|
| `type` | `"object"` | Required. |
| `required` | string[] | Fields that must be non-empty. The modal blocks submission if any are missing. |
| `properties` | object | Per-field schema. See §4.2. |

### 4.2 Per-field properties

| Property | Type | Purpose |
|---|---|---|
| `type` | `"string"` | Only `string` is supported for credentials. |
| `title` | string | Display label in the modal. Falls back to humanized key. |
| `description` | string | Help text shown below the input. Use for scope hints. |
| `secret` | boolean | When `true`, the field is masked by default and contributes to the "requires auth" trigger. |
| `default` | string | Pre-fill value (rare for secrets). |

### 4.3 Encryption

Global env (server-level) is encrypted with AAD `"global:{server_id}"`
and stored in `backend/storage/store/extensions/settings/{id}.json`
under `env_enc`.

Per-chat env is encrypted with AAD `"chat:{chat_id}:{server_id}"` and
stored in `backend/storage/chats/{chat_id}/mcp_auth.json`.

**Never write plaintext credentials to any file.**

---

## 5. `settings_schema` — JSON Schema for non-secret settings

Same structure as `env_schema` but for settings that are not secrets
(e.g. `allowed_directories` for the filesystem server).

Values are stored plaintext (no encryption) in the same settings file
under `settings`.

Supported field types: `string`, `integer`, `number`, `boolean`,
`array`, `object`. See `MCPSettingsModal.tsx` for the UI rendering rules.

---

## 6. Complete Examples

### 6.1 Stdio + Env Auth (`mcp_github`-style)

```json
{
  "$schema": "https://peldrun.ai/schemas/mcp-server/v1.json",

  "id": "mcp_github",
  "registry_id": "io.peldrun/mcp_github",
  "name": "MCP GitHub Integration",
  "type": "mcp_server",
  "status": "ready",

  "icon_svg": "<svg role=\"img\" viewBox=\"0 0 24 24\" xmlns=\"http://www.w3.org/2000/svg\"><title>GitHub</title><path fill=\"currentColor\" d=\"M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12\"/></svg>",

  "short_description": "Inspect repos, fetch issues, open PRs, and automate commits directly from your agent.",
  "store_description": "Connects the agent to the GitHub REST API through the official MCP server. Supports repository inspection, file CRUD, branch management, issue and pull-request lifecycles, code search, and commit automation. Requires a Personal Access Token (classic: `repo` scope, or fine-grained: `Contents: Read and Write`). Tokens are encrypted at rest per-chat and never written to logs.",

  "description": "Enables repository inspection, issues retrieval, and commit automation.",
  "category": "development",
  "tags": ["git", "github", "repositories", "issues", "pull-requests"],

  "author": {
    "name": "PELDRUN Team",
    "url": "https://peldrun.ai",
    "email": "support@peldrun.ai"
  },
  "publisher_id": "org.peldrun.official",
  "trust": "verified",

  "repository": {
    "url": "https://github.com/modelcontextprotocol/servers/tree/main/src/github",
    "source": "github"
  },
  "upstream": {
    "name": "@modelcontextprotocol/server-github",
    "registry": "npm",
    "version": "0.6.2",
    "license": "MIT"
  },

  "homepage": "https://peldrun.ai/mcp/github",
  "docs_url": "https://docs.peldrun.ai/mcp/github",
  "license": "proprietary",
  "license_url": "https://peldrun.ai/license",

  "version": "1.0.0",
  "release_date": "2025-01-15",
  "stability": "stable",
  "min_app_version": "2.4.0",

  "is_builtin": true,
  "read_only": false,
  "command": "npx -y @modelcontextprotocol/server-github",
  "transport": "stdio",
  "runtime": "node",
  "auth_strategy": "env",

  "capabilities": { "tools": true, "resources": false, "prompts": false },
  "tool_count": 26,
  "badges": ["official", "verified", "requires-token", "writes-data"],

  "env_schema": {
    "type": "object",
    "required": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    "properties": {
      "GITHUB_PERSONAL_ACCESS_TOKEN": {
        "type": "string",
        "title": "GitHub Personal Access Token",
        "description": "Classic PAT needs 'repo' for write ops, 'public_repo' for read-only. Create at https://github.com/settings/tokens",
        "secret": true
      }
    }
  },

  "hints": {
    "setup_time": "1 minute",
    "requires_account": true,
    "requires_token": true,
    "cost": "free",
    "data_access": "read-write",
    "network_access": true,
    "rate_limit": "5,000 req/hour authenticated"
  }
}
```

### 6.2 Stdio + Settings (`mcp_filesystem`-style)

```json
{
  "id": "mcp_filesystem",
  "name": "MCP Local Filesystem",
  "type": "mcp_server",
  "status": "active",
  "short_description": "Give agents safe, sandboxed access to specific local directories.",
  "description": "Exposes local filesystem directories safely to agents.",
  "category": "system",
  "command": "npx -y @modelcontextprotocol/server-filesystem",
  "transport": "stdio",
  "runtime": "node",
  "auth_strategy": "none",
  "capabilities": { "tools": true, "resources": true, "prompts": false },

  "settings_schema": {
    "type": "object",
    "properties": {
      "allowed_directories": {
        "type": "array",
        "items": { "type": "string" },
        "title": "Allowed Directories",
        "description": "Absolute paths the server may access. Required for the server to start.",
        "default": []
      }
    }
  },

  "hints": {
    "setup_time": "30 seconds",
    "requires_token": false,
    "cost": "free",
    "data_access": "read-write",
    "network_access": false
  }
}
```

### 6.3 HTTP + OAuth (`http_mcp`-style)

```json
{
  "id": "mcp_example",
  "name": "Example MCP Server",
  "type": "mcp_server",
  "transport": "sse",
  "url": "https://api.example.com/mcp/",
  "auth_strategy": "oauth",
  "scopes": ["read", "write"],
  "client_metadata_url": "https://peldrun.ai/client-metadata.json",
  "description": "Example HTTP MCP server with OAuth 2.1."
}
```

### 6.4 Python Bundle

```json
{
  "id": "my_bundle",
  "name": "My Python Bundle",
  "type": "mcp_server",
  "runtime": "python",
  "entrypoint": "server.py",
  "transport": "stdio",
  "description": "A self-contained Python MCP server.",
  "env_schema": {
    "type": "object",
    "required": ["MY_API_KEY"],
    "properties": {
      "MY_API_KEY": { "type": "string", "secret": true }
    }
  }
}
```

---

## 7. JSON Schema (machine-readable)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "PELDRUN MCP Server Manifest",
  "type": "object",
  "required": ["id", "name", "type", "description"],
  "properties": {
    "id": { "type": "string", "pattern": "^[a-z0-9_]+$" },
    "name": { "type": "string", "minLength": 1 },
    "type": { "enum": ["mcp_server", "mcp"] },
    "description": { "type": "string" },
    "short_description": { "type": "string", "maxLength": 200 },
    "store_description": { "type": "string", "maxLength": 4000 },
    "category": { "type": "string" },
    "tags": { "type": "array", "items": { "type": "string" } },
    "status": { "enum": ["active", "ready"] },
    "transport": { "enum": ["stdio", "sse", "http", "streamable-http"] },
    "command": { "type": "string" },
    "url": { "type": "string", "format": "uri" },
    "headers": { "type": "object" },
    "timeout": { "type": "number", "minimum": 1 },
    "runtime": { "enum": ["python", "node"] },
    "entrypoint": { "type": "string" },
    "auth_strategy": {
      "enum": ["env", "oauth", "apikey", "api_key", "bearer", "token", "none"]
    },
    "env_schema": { "type": "object" },
    "settings_schema": { "type": "object" },
    "scopes": { "type": "array", "items": { "type": "string" } },
    "client_metadata_url": { "type": "string", "format": "uri" },
    "capabilities": {
      "type": "object",
      "properties": {
        "tools": { "type": "boolean" },
        "resources": { "type": "boolean" },
        "prompts": { "type": "boolean" }
      }
    },
    "badges": { "type": "array", "items": { "type": "string" } },
    "hints": {
      "type": "object",
      "properties": {
        "setup_time": { "type": "string" },
        "requires_account": { "type": "boolean" },
        "requires_token": { "type": "boolean" },
        "cost": { "enum": ["free", "freemium", "paid"] },
        "data_access": { "enum": ["read-only", "read-write", "none"] },
        "network_access": { "type": "boolean" },
        "rate_limit": { "type": "string" }
      }
    },
    "author": {
      "type": "object",
      "properties": {
        "name": { "type": "string" },
        "url": { "type": "string", "format": "uri" },
        "email": { "type": "string", "format": "email" }
      }
    },
    "trust": { "enum": ["verified", "community", "unverified"] },
    "license": { "type": "string" },
    "version": { "type": "string" },
    "stability": { "enum": ["stable", "beta", "alpha"] }
  }
}
```

---

## 8. Runtime Behavior Reference

### 8.1 Tool Cache Key

The persistent tool cache uses this hash to decide freshness:

```
sha256(command | url | transport | env_schema)
```

Any change to one of these fields invalidates the cache and forces
re-discovery. Changes to `name`, `status`, `description`, `icon_svg`,
`hints`, etc. do **not** invalidate the cache.

### 8.2 Effective Tool Surface (Four Gates)

A tool is only sent to the LLM when all four are true:

1. **Global MCP subsystem** is enabled (`Setup → MCP → Master Enable`).
2. **Server status** is `"active"` (toggled ON in the Store).
3. **Tool `user_enabled`** is `true` (toggled in "Manage Tools").
4. The **tool cache** contains the tool (i.e. discovery succeeded).

If any gate is off, the tool is visible in the modal but marked
"Inactive" and excluded from `AgentSpec.tools`.

### 8.3 Auth Modal Trigger

The modal opens when:
- An MCP tool call is attempted (lazy check in `_mcp_tool_executor`).
- The server requires auth (`auth_strategy` or `env_schema.required` + `secret`).
- No per-chat decision exists (`get_mode(chat_id, server_id) is None`).

Three options are offered:
- **Use Global Settings** → records `use_global` mode.
- **One-time for This Chat** → stores ephemeral credentials in memory only.
- **Authenticate & Save** → OAuth popup OR saved env (encrypted).

### 8.4 Probe & Refresh

- `POST /api/mcp/test` — probe a server, no cache write.
- `POST /api/mcp/servers/{id}/tools/refresh` — force re-discovery; merges with existing cache (preserves `user_enabled`).

Refresh fails fast with:
- `412` if credentials are required but missing globally.
- `504` if the server times out (cold start, missing config).
- `502` for any other server-side error.

---

## 9. Common Patterns

### 9.1 Pre-warm `npx` servers

`npx -y` downloads the package on first use. For slower networks, either:

1. Set `startup_timeout_seconds` to `300` in `Setup → MCP`.
2. Or pre-warm once: `npm cache add @modelcontextprotocol/server-xxx`.

### 9.2 Filesystem servers require directories

`@modelcontextprotocol/server-filesystem` refuses to start without at
least one directory argument. Always declare `settings_schema.allowed_directories`.

### 9.3 OAuth servers must be HTTP

OAuth requires `transport ∈ {sse, http, streamable-http}` and a `url`.
Stdio transports cannot participate in OAuth.

### 9.4 Bundle-specific fields

Bundles set `is_bundle: true` and `bundle_dir` automatically. Do not
hand-write these — the installer overrides them.

---

## 10. Validation

Run the built-in validator:

```powershell
cd D:\AI\peldrun\backend
python -c "import json; m = json.load(open('omweb/extensions/builtins/mcp_github.json')); print('OK:', m['id'])"
```

Or validate through the runtime:

```powershell
Invoke-RestMethod -Uri "http://localhost:3088/api/mcp"
```

Check for the server in the response and verify `is_builtin: true`.

---

## 11. Migration Checklist

When updating an existing manifest:

- [ ] If `command`, `url`, `transport`, or `env_schema` changed → the
      tool cache will auto-invalidate on next discovery. No action needed.
- [ ] If only display fields changed → the cache is preserved. The UI
      reflects the changes immediately after refresh.
- [ ] If `is_builtin` appears wrong → check whether the manifest is
      under `builtins/`. The registry re-derives it on every load.
- [ ] For OAuth servers changing `client_metadata_url` or `scopes` →
      users will need to re-authenticate (existing tokens may lack the
      new scopes).

---

## 12. See Also

- `docs/MCP_AUTH.md` — full OAuth 2.1 and per-chat credential design.
- `backend/omweb/extensions/registry.py` — manifest loading logic.
- `backend/omweb/extensions/mcp_tool_cache.py` — persistent tool cache.
- `backend/omweb/extensions/mcp_subsystem_settings.py` — global settings.
- `frontend/src/app/stores/_components/MCPDetailDrawer.tsx` — UI rendering reference.
```

