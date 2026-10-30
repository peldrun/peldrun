## 📄 الملف الثالث عشر: `backend/docs/MCP_MANIFEST_REFERENCE.md`
### المرحلة D6 — دليل شامل لتوثيق MCP Manifest + ما تم تنفيذه

هذا الملف **جديد** وسيكون المرجع الأساسي لأي مبرمج يريد:
1. فهم ما تم تنفيذه في المراحل A/B/C.
2. بناء manifest جديد لأي خادم MCP (stdio/http/oauth/mtls/...).
3. معرفة كل الحالات (states) الممكنة للحقول.

**📍 المسار:** `backend/docs/MCP_MANIFEST_REFERENCE.md`

**🔁 أنشئ الملف بالمحتوى التالي:**

````markdown
# MCP Manifest Reference — PELDRUN

Complete reference for building MCP server manifests, including
all authentication strategies, transport types, and lifecycle
states supported by PELDRUN.

**Version:** 2.0 (post A/B/C implementation)
**Spec compliance:** MCP 2025-11-25 + OAuth 2.1 + RFC 9728 + RFC 8707 + mTLS

---

## 1. What's New (Changelog)

### Phase A — `outputSchema` + `structuredContent`

Tools may now declare the shape of their output, and tool calls
may return machine-readable structured data alongside the
human-readable text blocks.

**For manifest authors:**
- No manifest change is required. `outputSchema` is discovered
  automatically from `tools/list` and cached by the backend.

**For agent consumers:**
- `mcp_client.call_tool` now returns `structuredContent` (JSON)
  prepended to `content` (text). The LLM sees both.
- The `metadata` field on `ToolResult` carries
  `has_structured_content: bool` for programmatic checks.

**For UI:**
- `MCPToolsModal` displays `output_schema` under `input_schema`
  with labeled sections ("Input Schema" / "Output Schema").

**Cache:**
- `CACHE_SCHEMA_VERSION` bumped from 2 → 3. Old caches are
  automatically invalidated on first read.

---

### Phase B — mTLS (Mutual TLS)

Enterprise MCP servers that require client certificates (Banking,
Healthcare, Government, internal PKI) can now be configured.

**For manifest authors:**
- Declare `auth_strategy: "mtls"` and an `mtls` block that maps
  logical roles (cert / key / ca) to environment variable names.
- The env schema prompts the user for **paths** (not the cert
  contents themselves — paths are safer and let the operator
  rotate certs without editing the manifest).

**Example:**

```json
{
  "auth_strategy": "mtls",
  "mtls": {
    "cert_env": "MTLS_CLIENT_CERT",
    "key_env": "MTLS_CLIENT_KEY",
    "ca_env": "MTLS_CA_BUNDLE"
  }
}
```

**Runtime behavior:**
- `mcp_pool.get_or_create_client` and `probe_connection` resolve
  the env vars into filesystem paths, pass them to `MCPClient`,
  which forwards them to `httpx.AsyncClient(cert=..., verify=...)`.
- A malformed cert path fails fast with a clear SSLError — never
  hangs.
- **stdio transports ignore mTLS entirely** — a subprocess has no
  TLS layer to participate in.

**Reference manifest:** `extensions/builtins/test_mtls.json`

---

### Phase C — `resources` + `prompts`

Servers that expose read-only resources (documents, files, DB
rows) or reusable prompt templates are now fully supported.

**For manifest authors:**
- No manifest change is required. `resources/list`, `resources/read`,
  `prompts/list`, and `prompts/get` are called automatically.
- A server that returns `[]` for both is treated identically to
  a tools-only server — no error, no UI noise.

**For consumers:**
- `GET /api/mcp/servers/{sid}/resources` — list
- `POST /api/mcp/servers/{sid}/resources/read` — read by URI
- `GET /api/mcp/servers/{sid}/prompts` — list
- `MCPDetailDrawer` now has three tabs: Tools / Resources / Prompts.

**Cache:**
- `CACHE_SCHEMA_VERSION` bumped from 3 → 4. Cache now carries
  `resources` and `prompts` arrays alongside `tools`.

---

## 2. MCP Server States

### 2.1 `status` — lifecycle

| Value | Meaning | Session behavior |
|---|---|---|
| `"active"` | Server is enabled; its tools are exposed to agents | Sessions are created on demand |
| `"ready"` | Server is disabled; user has not activated it yet | No sessions spawned; tools not exposed |

Toggle endpoint: `POST /api/mcp/servers/{sid}/toggle`

**Transition active → ready:** All live sessions are closed and
the tool cache is invalidated. The next activation re-discovers
tools from scratch.

**Transition ready → active:** No immediate action; sessions are
created lazily on the first tool call.

---

### 2.2 `transport` — wire protocol

| Value | When to use | Required fields |
|---|---|---|
| `"stdio"` | Spawn a local subprocess (Node/Python MCP server) | `command` |
| `"http"` | Streamable HTTP (modern MCP servers) | `url` |
| `"sse"` | Server-Sent Events (legacy MCP servers) | `url` |
| `"streamable-http"` | Alias for `"http"` | `url` |

**Decision rule:**
- If the server ships as `npx @scope/package` → `stdio`
- If the server is reached over HTTPS → `http` (preferred) or
  `sse` (legacy)
- If you're unsure, prefer `"http"` — modern servers accept it.

---

### 2.3 `auth_strategy` — authentication method

| Value | Trigger condition | Credential storage |
|---|---|---|
| `"env"` (or absent) | Env vars from `env_schema` | `mcp_settings_manager` (encrypted) |
| `"apikey"`, `"bearer"`, `"token"` | Aliases for `"env"` — same behavior | Same as `"env"` |
| `"oauth"` | OAuth 2.1 flow via `mcp_auth` router | `settings_manager.save_oauth()` (encrypted) |
| `"mtls"` | Client certificate authentication | Env vars resolved to file paths |

**Detection rule (used by `mcp_auth_state.manifest_needs_auth`):**
- If `auth_strategy` is set to any of the above → auth required.
- Otherwise, if `env_schema.required` contains a field with
  `"secret": true` → auth required.
- Otherwise → no auth.

---

### 2.4 `runtime` — dependency installation

| Value | Auto-detection | Effect |
|---|---|---|
| `"python"` | `requirements.txt` present | Runs `pip install -r requirements.txt` on bundle install |
| `"node"` | `package.json` present | Runs `npm install --omit=dev` on bundle install |
| absent / other | — | No dependency installation |

Only relevant for bundles; manifest-declared servers must
provide their own dependencies.

---

### 2.5 `is_builtin` / `is_bundle` — source tracking

| Field | Value | Meaning |
|---|---|---|
| `is_builtin` | `true` | Ships with PELDRUN (`extensions/builtins/*.json`) |
| `is_builtin` | `false` | User-uploaded manifest |
| `is_bundle` | `true` | Installed via ZIP bundle (`extensions/bundles/{id}/`) |
| `is_bundle` | absent / `false` | Single JSON manifest |

**Identity rule:** `is_builtin` is determined by the presence of
`extensions/builtins/{id}.json`, **not** by the persisted value
in the custom-dir copy. This means toggling a builtin server
(status change) does not degrade it to "custom".

---

## 3. Complete Manifest Structure

```json
{
  "id": "unique_server_id",                    // REQUIRED, sanitized [a-zA-Z0-9_-]
  "name": "Human-Readable Name",               // REQUIRED
  "type": "mcp_server",                        // REQUIRED ("mcp" also accepted)
  "short_description": "One-line summary",     // Strongly recommended
  "description": "Full multi-line description", // Shown in Details drawer
  "version": "1.0.0",                          // Optional
  "status": "ready",                           // "active" | "ready" (default "ready")

  // ── Transport ───────────────────────────────────────────────
  "transport": "stdio",                        // "stdio" | "http" | "sse" | "streamable-http"
  "command": "npx -y @scope/package",          // REQUIRED for stdio
  "url": "https://example.com/mcp",            // REQUIRED for http/sse
  "timeout": 30.0,                             // Optional (seconds), fallback to subsystem setting
  "headers": {                                 // Optional static headers (http/sse)
    "X-Custom": "value"
  },

  // ── Authentication ──────────────────────────────────────────
  "auth_strategy": "env",                      // "env" | "oauth" | "mtls" | (aliases)
  "scopes": ["read", "write"],                 // OAuth only — override AS-advertised scopes
  "client_metadata_url": "https://...",        // OAuth only — custom CIMD document

  "mtls": {                                    // REQUIRED only when auth_strategy="mtls"
    "cert_env": "MTLS_CLIENT_CERT",
    "key_env":  "MTLS_CLIENT_KEY",
    "ca_env":   "MTLS_CA_BUNDLE"               // Optional
  },

  "http_basic_auth": {                         // Optional, http/sse only
    "user_env": "BASIC_USER",                  // env var name holding the username
    "pass_env": "BASIC_PASS"                   // env var name holding the password
  },

  // ── Schemas ─────────────────────────────────────────────────
  "settings_schema": {                         // JSON Schema for UI-driven config
    "type": "object",
    "properties": {
      "allowed_directories": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Absolute paths the server may access"
      }
    }
  },
  "env_schema": {                              // JSON Schema for env vars
    "type": "object",
    "required": ["GITHUB_TOKEN"],
    "properties": {
      "GITHUB_TOKEN": {
        "type": "string",
        "title": "GitHub Personal Access Token",
        "description": "Create at https://github.com/settings/tokens",
        "secret": true                         // Hides input + marks as required-for-auth
      }
    }
  },

  // ── Runtime (bundles only) ──────────────────────────────────
  "runtime": "node",                           // "node" | "python"
  "entrypoint": "index.js",                    // Relative to bundle root

  // ── UI metadata (optional but recommended) ──────────────────
  "category": "Developer Tools",
  "tags": ["github", "git", "code"],
  "author": {
    "name": "Author Name",
    "url": "https://github.com/author"
  },
  "homepage": "https://...",
  "docs_url": "https://...",
  "repository": { "url": "https://..." },
  "privacy_url": "https://...",
  "support_url": "https://...",
  "trust": "verified",                         // "verified" | absent
  "icon_svg": "<svg>...</svg>",               // Inline SVG string
  "hints": {
    "setup_time": "2 mins",
    "requires_token": true,
    "network_access": true,
    "cost": "Free"
  }
}
```

---

## 4. Auth Strategy Reference

### 4.1 `env` (or absent) — Environment Variables

**Use when:** The MCP server accepts credentials as process env
vars (stdio) or as HTTP headers derived from env (http).

**Example — GitHub (stdio):**

```json
{
  "id": "mcp_github",
  "name": "GitHub",
  "transport": "stdio",
  "command": "npx -y @modelcontextprotocol/server-github",
  "auth_strategy": "env",
  "env_schema": {
    "type": "object",
    "required": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    "properties": {
      "GITHUB_PERSONAL_ACCESS_TOKEN": {
        "type": "string",
        "title": "Personal Access Token",
        "secret": true
      }
    }
  }
}
```

**Runtime:** `mcp_pool.build_transport` reads `env_schema` +
`mcp_settings_manager.get_env()` and passes the merged values to
`StdioTransport(env=...)`.

---

### 4.2 `oauth` — OAuth 2.1

**Use when:** The server exposes an HTTP endpoint and follows the
MCP OAuth 2.1 discovery spec (RFC 9728 protected resource
metadata + RFC 8414 AS metadata).

**Example — Notion:**

```json
{
  "id": "notion_mcp",
  "name": "Notion",
  "transport": "http",
  "url": "https://mcp.notion.com/mcp",
  "auth_strategy": "oauth"
}
```

**No `env_schema` needed** — tokens are stored via
`mcp_settings_manager.save_oauth()` after the popup flow completes.

**Flow:**
1. `POST /api/mcp/servers/{sid}/oauth/start` → returns `auth_url`.
2. Frontend opens popup.
3. Callback saves tokens with AAD `"global:{sid}:oauth"`.
4. `mcp_pool.build_transport` reads them via `get_oauth()` and
   sets `Authorization: Bearer {token}` on HTTP requests.

---

### 4.3 `mtls` — Mutual TLS

**Use when:** The server requires a client certificate during the
TLS handshake (Enterprise / internal PKI).

**Example:**

```json
{
  "id": "bank_mcp",
  "name": "Bank Internal API",
  "transport": "http",
  "url": "https://internal.bank.example.com/mcp",
  "auth_strategy": "mtls",
  "mtls": {
    "cert_env": "MTLS_CLIENT_CERT",
    "key_env": "MTLS_CLIENT_KEY",
    "ca_env": "MTLS_CA_BUNDLE"
  },
  "env_schema": {
    "type": "object",
    "required": ["MTLS_CLIENT_CERT", "MTLS_CLIENT_KEY"],
    "properties": {
      "MTLS_CLIENT_CERT": {
        "type": "string",
        "title": "Client Certificate Path",
        "description": "Absolute path to the .pem or .crt file",
        "secret": true
      },
      "MTLS_CLIENT_KEY": {
        "type": "string",
        "title": "Client Private Key Path",
        "description": "Absolute path to the .pem or .key file",
        "secret": true
      },
      "MTLS_CA_BUNDLE": {
        "type": "string",
        "title": "Custom CA Bundle (Optional)",
        "description": "Path to a CA bundle if using a private PKI",
        "secret": false
      }
    }
  }
}
```

**Rules:**
- **Only meaningful for `transport: "http"` or `"sse"`.**
  `stdio` servers cannot use mTLS.
- `cert_env` is required. If it resolves to an empty value, mTLS
  is silently skipped (TLS uses the default trust store).
- `key_env` is optional — if empty, `cert_env` must be a combined
  cert+key `.pem`.
- `ca_env` is optional — overrides the system trust store.

---

### 4.4 `http_basic_auth` — HTTP Basic (WordPress, GitLab)

**Use when:** The server accepts HTTP Basic auth and does not use
OAuth. Applied only to `http`/`sse` and only when no Bearer token
is available (Bearer wins).

**Example — WordPress:**

```json
{
  "id": "wordpress_mcp",
  "transport": "http",
  "url": "{WORDPRESS_URL}/wp-json/mcp/mcp-adapter-default-server",
  "auth_strategy": "env",
  "http_basic_auth": {
    "user_env": "WORDPRESS_USER",
    "pass_env": "WORDPRESS_APP_PASSWORD"
  },
  "env_schema": {
    "type": "object",
    "required": ["WORDPRESS_URL", "WORDPRESS_USER", "WORDPRESS_APP_PASSWORD"],
    "properties": {
      "WORDPRESS_URL": { "type": "string", "title": "WordPress URL" },
      "WORDPRESS_USER": { "type": "string", "title": "Username" },
      "WORDPRESS_APP_PASSWORD": {
        "type": "string",
        "title": "Application Password",
        "secret": true
      }
    }
  }
}
```

**Placeholder substitution:** `{WORDPRESS_URL}` in `url` is
replaced with the env value at connect time.

---

## 5. Settings vs. Env Schema

| Aspect | `settings_schema` | `env_schema` |
|---|---|---|
| Storage | `settings_manager` (plaintext JSON) | `settings_manager` (AES-256-GCM encrypted) |
| Shown in UI | Settings tab | Environment Variables tab |
| Use case | Non-sensitive config (paths, flags, limits) | Credentials (tokens, passwords, cert paths) |
| Encrypted at rest | No | Yes |
| Marks server as auth-required | No | Yes, when a `secret: true` field is required |

**Rule of thumb:**
- If the value would be dangerous in a `.env` file committed to
  git → put it in `env_schema` with `secret: true`.
- Everything else → `settings_schema`.

---

## 6. Example Manifests

### 6.1 stdio + env (GitHub pattern)

```json
{
  "id": "mcp_github",
  "name": "GitHub",
  "type": "mcp_server",
  "transport": "stdio",
  "command": "npx -y @modelcontextprotocol/server-github",
  "auth_strategy": "env",
  "status": "ready",
  "category": "Developer Tools",
  "env_schema": {
    "type": "object",
    "required": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    "properties": {
      "GITHUB_PERSONAL_ACCESS_TOKEN": {
        "type": "string",
        "title": "Personal Access Token",
        "secret": true
      }
    }
  }
}
```

### 6.2 stdio + settings (Filesystem pattern)

```json
{
  "id": "mcp_filesystem",
  "name": "Filesystem",
  "type": "mcp_server",
  "transport": "stdio",
  "command": "npx -y @modelcontextprotocol/server-filesystem",
  "settings_schema": {
    "type": "object",
    "properties": {
      "allowed_directories": {
        "type": "array",
        "items": {"type": "string"},
        "title": "Allowed Directories",
        "description": "Absolute paths the server can access"
      }
    }
  }
}
```

### 6.3 HTTP + Basic Auth (WordPress pattern)

See §4.4 above.

### 6.4 HTTP + OAuth (Notion pattern)

See §4.2 above.

### 6.5 HTTP + mTLS (Enterprise pattern)

See §4.3 above.

---

## 7. Testing a Manifest

### 7.1 Lint before upload

```bash
python -c "import json; json.load(open('my_manifest.json'))"
```

### 7.2 Verify registration

```bash
curl -s http://localhost:3088/api/mcp/servers | jq '.servers[].id'
```

The manifest's `id` must appear in the list.

### 7.3 Test connectivity

```bash
curl -s -X POST http://localhost:3088/api/mcp/test \
  -H "Content-Type: application/json" \
  -d '{"server_id":"my_server"}' | jq
```

**Expected on success:**
```json
{
  "status": "ok",
  "message": "Successfully connected... Discovered N tool(s).",
  "tools": [...]
}
```

**Common failures:**

| Symptom | Cause | Fix |
|---|---|---|
| `412` "requires credentials" | Server has `auth_strategy` set but no global env | Open Settings and save credentials |
| `502` "Tool discovery failed" | Transport mismatch / wrong URL | Verify `url` and `transport` |
| Timeout after 120s | `npx -y` is downloading | Run the command manually once, then retry |
| `SSLError` | mTLS cert invalid / path wrong | Verify cert/key/CA paths exist and are readable |

### 7.4 Discover tools

```bash
curl -s -X POST http://localhost:3088/api/mcp/servers/my_server/tools/refresh | jq
```

### 7.5 Inspect cached tools (including `output_schema`)

```bash
curl -s http://localhost:3088/api/mcp/servers/my_server/tools/cached | jq
```

### 7.6 Discover resources / prompts

```bash
curl -s http://localhost:3088/api/mcp/servers/my_server/resources | jq
curl -s http://localhost:3088/api/mcp/servers/my_server/prompts | jq
```

Empty `resources: []` and `prompts: []` are valid — the server
simply doesn't expose those primitives.

---

## 8. Cache Schema (Internal)

Not part of the manifest, but documented here so you know what
the backend stores per server.

**Location:** `backend/storage/mcp/tool_cache/{server_id}.json`

```json
{
  "version": 4,
  "server_id": "my_server",
  "server_config_hash": "<sha256>",
  "fetched_at": 1730000000.0,
  "tools": [
    {
      "name": "search",
      "description": "...",
      "parameters": { "type": "object", "properties": {...} },
      "output_schema": { "type": "object", "properties": {...} },
      "user_enabled": true,
      "first_seen_at": 1730000000.0,
      "last_seen_at": 1730000000.0
    }
  ],
  "resources": [
    { "uri": "file:///doc.md", "name": "doc", "description": "", "mimeType": "text/markdown" }
  ],
  "prompts": [
    {
      "name": "summarize",
      "description": "...",
      "arguments": [{ "name": "text", "description": "...", "required": true }]
    }
  ]
}
```

**`version` history:**
- `1` — initial
- `2` — sanitized `parameters` ($ref-free)
- `3` — added `output_schema`
- `4` — added `resources` and `prompts`

Any cache with `version < 4` is treated as stale on read and
re-discovered automatically on the next call.

---

## 9. Security Invariants

These are enforced by the backend and must not be violated by
manifests:

1. **Credentials never touch disk in plaintext.** Every secret in
   `env_schema` (marked `secret: true`) is encrypted with
   AES-256-GCM before persistence.
2. **AAD binds every ciphertext to its logical context.** A token
   stored for `global:my_server` cannot be replayed as `chat:abc:other_server`.
3. **mTLS certs are referenced by path, not embedded.** The
   manifest never contains the certificate itself.
4. **OAuth tokens use a distinct AAD** (`:oauth` suffix) — cannot
   be confused with env credentials even if extracted from disk.
5. **`http_basic_auth` is a fallback only.** Bearer tokens always
   win over Basic auth when both are present.
6. **stdio ignores mTLS and OAuth tokens.** A subprocess has no
   TLS handshake — forwarding HTTP-layer secrets to it would leak
   them into a process env dump.

---

## 10. Related Documentation

- **OAuth details:** `docs/MCP_AUTH.md`
- **Implementation plan (A/B/C):** `docs/MCP_ROADMAP.md` (if present)
- **Client cert generation:** `docs/MTLS_SETUP.md` (if present)
- **Backend API:** `routers/mcp.py` (endpoint signatures)

---

*Last updated: post A/B/C implementation. All features tested against
GitHub (stdio), Filesystem (stdio), WordPress (HTTP+Basic), Notion
(HTTP+OAuth), and a synthetic mTLS server.*
````

---
