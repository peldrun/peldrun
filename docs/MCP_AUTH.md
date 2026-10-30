```markdown
# MCP Authentication & Credential Management — PELDRUN

Complete reference for how PELDRUN authenticates with Model Context
Protocol (MCP) servers, how credentials are stored, and how per-chat
isolation is enforced.

**Version:** 2.0 (post A/B/C implementation)
**Spec compliance:** MCP 2025-11-25, OAuth 2.1 (draft-ietf-oauth-v2-1),
RFC 9700, RFC 8252, RFC 7636 (PKCE), RFC 8707 (Resource Indicators),
RFC 9728 (Protected Resource Metadata), RFC 8414 (AS Metadata),
Client ID Metadata Documents (CIMD), mTLS.

---

## 1. Overview

PELDRUN supports **five** independent authentication mechanisms for
MCP servers, each with its own storage location and threat model:

| # | Mechanism | Where credentials live | When to use |
|---|---|---|---|
| 1 | **Per-chat environment variables** | `{chat_dir}/mcp_auth.json` (encrypted) | User wants credentials scoped to one conversation |
| 2 | **Global environment variables** | `storage/store/extensions/settings/{sid}.json` (encrypted) | User wants one credential set across all chats |
| 3 | **Per-chat OAuth 2.1 tokens** | `{chat_dir}/mcp_auth.json` (encrypted, `:oauth` AAD) | HTTP server, user authorizes from a chat |
| 4 | **Global OAuth 2.1 tokens** | `storage/store/extensions/settings/{sid}.json` (encrypted, `:oauth` AAD) | HTTP server, user authorizes once from Store UI |
| 5 | **mTLS client certificates** | Env var paths → filesystem `.pem` files | Enterprise server with private PKI |

**Orphaned mechanisms (not recommended):**
- **Ephemeral credentials** — in-memory only, dropped on process restart.
  Used when the user selects "just this once". Never written to disk.
- **HTTP Basic auth** — derived from env vars at connection time, not
  stored separately. Falls back to Bearer when both are present.

---

## 2. Authentication Strategy Decision

The backend decides which mechanism applies based on the manifest.
This is enforced by `omweb/extensions/mcp_auth_state.py`:

```python
def manifest_needs_auth(manifest: dict) -> bool:
    strategy = manifest.get("auth_strategy", "").lower().strip()
    if strategy in ("oauth", "env", "apikey", "api_key", "bearer", "token"):
        return True

    env_schema = manifest.get("env_schema")
    if not isinstance(env_schema, dict):
        return False
    required = env_schema.get("required") or []
    properties = env_schema.get("properties") or {}
    for field in required:
        prop = properties.get(field)
        if isinstance(prop, dict) and prop.get("secret"):
            return True
    return False
```

**Decision matrix:**

| `auth_strategy` | `env_schema.required[*].secret` | Result |
|---|---|---|
| `"oauth"` | (any) | OAuth flow required |
| `"env"` / `"apikey"` / etc. | (any) | Env credentials required |
| `"mtls"` | (any) | mTLS paths required |
| absent | at least one required field with `secret: true` | Env credentials required |
| absent | no required secret field | **No auth** — server is public |

A server with **no** auth requirement (filesystem, fetch) never
prompts the user.

---

## 3. Environment Variable Credentials

### 3.1 Storage architecture

Two independent scopes exist:

```
storage/store/extensions/settings/{server_id}.json   ← global
{chat_dir}/mcp_auth.json                             ← per-chat
```

**Global credentials** are used when the per-chat decision is
`use_global` or absent. **Per-chat credentials** override the
global ones when the user explicitly picks `custom`.

### 3.2 Global credentials

**File:** `storage/store/extensions/settings/{server_id}.json`

**Written by:** `McpSettingsManager.save_env()`

**Encrypted with AAD:** `"global:{server_id}"`

**Shape:**

```json
{
  "settings": { ... },
  "env_enc": "<base64 of AES-256-GCM ciphertext>",
  "schema_version": 2
}
```

**Important:** No code path writes plaintext `env` to disk. The
legacy `env` field is only read for migration purposes (see §8).

### 3.3 Per-chat credentials

**File:** `{chat_dir}/mcp_auth.json`

**Written by:** `ChatMcpAuthStore.save_custom()`

**Encrypted with AAD:** `"chat:{chat_id}:{server_id}"`

**Shape:**

```json
{
  "version": 1,
  "entries": {
    "<server_id>": {
      "mode": "custom",
      "env_enc": "<base64>",
      "oauth_enc": "<base64>",
      "created_at": "2026-07-28T12:00:00+00:00",
      "updated_at": "2026-07-28T12:00:00+00:00"
    }
  }
}
```

### 3.4 Credential modes

| Mode | Storage | Persistence | Isolation |
|---|---|---|---|
| `custom` | Encrypted on disk (per-chat file) | Survives restarts | Fully isolated to this chat |
| `use_global` | Not stored (uses global env) | N/A | Shared with all other chats |
| `ephemeral` | In-memory dict only | **Lost on restart** | Fully isolated, transient |

**Mode selection is per (chat, server) pair.** The decision is
recorded in `mcp_auth.json` for `custom` and `use_global`. For
`ephemeral`, no file entry is created — the mode is discovered
purely from memory.

### 3.5 Runtime resolution

When the agent actually invokes an MCP tool, the executor resolves
credentials as follows (`resolve_tool_runtime._mcp_tool_executor`):

```
1. Caller passed env_override explicitly?     → use it directly.
2. Else: look up chat_mcp_auth store:
   a. mode = custom    → decrypt per-chat env + OAuth bundle.
   b. mode = ephemeral → pull from in-memory dict.
   c. mode = use_global → fall through to global env.
   d. mode is None:
      i. If manifest_needs_auth → emit SSE event, fail job with
         MCP_AUTH_REQUIRED, raise. Frontend shows the auth modal.
      ii. Else → proceed without credentials.
3. mcp_pool.build_transport:
   - env_override present → FULLY REPLACES global env (no merge).
   - env_override absent  → reads from mcp_settings_manager.
```

**Critical security invariant:** `env_override` **replaces** the
manifest env; it does not merge. A chat that chose `custom` must
never silently inherit a global variable the user did not select.

---

## 4. OAuth 2.1 Flow

### 4.1 When to use

The server:
- Has `transport` in `{"http", "sse", "streamable-http", ""}`
- Has a non-empty `url`
- Has `auth_strategy: "oauth"` **or** has no `env_schema`

OAuth servers **never** use `env_schema` — their credentials are
tokens, not environment variables.

### 4.2 Discovery (RFC 9728)

`OAuthManager.discover_auth_server(mcp_server_url)` fetches the
protected resource metadata:

```
1. {scheme}://{host}/.well-known/oauth-protected-resource/{path}   ← path-scoped
2. {scheme}://{host}/.well-known/oauth-protected-resource         ← host-scoped
```

Path-scoped is attempted first (per RFC 9728 §3.1); host-scoped is
the fallback. **Both** must fail before discovery is considered
failed.

The `authorization_servers[0]` field from the PRM yields the
issuer. Then AS metadata is fetched from:

```
1. {issuer}/.well-known/oauth-authorization-server  ← RFC 8414
2. {issuer}/.well-known/openid-configuration        ← OIDC fallback
```

**Fallback for legacy providers:** GitHub and a small number of
older providers do not expose PRM at all. In that case,
`PRE_REGISTERED_CLIENTS` supplies the metadata directly:

```python
PRE_REGISTERED_CLIENTS = {
    "github.com": {
        "client_id_env": "PELDRUN_GITHUB_CLIENT_ID",
        "client_secret_env": "PELDRUN_GITHUB_CLIENT_SECRET",
        "authorization_endpoint": "https://github.com/login/oauth/authorize",
        "token_endpoint": "https://github.com/login/oauth/access_token",
        "scopes": ["repo", "read:user"],
        "pkce": True,
    }
}
```

### 4.3 Client registration

Three mechanisms are tried in order (`OAuthManager.resolve_client`):

1. **Pre-registered hostname match**
   - The server's hostname appears in `PRE_REGISTERED_CLIENTS`.
   - `client_id` and `client_secret` come from env vars.
   - **Chosen first** because the operator explicitly registered an
     OAuth app out-of-band.

2. **CIMD (Client ID Metadata Document)**
   - Only if `metadata.client_id_metadata_document_supported` is True.
   - Only if the metadata URL is **publicly reachable** (`https://` and
     not loopback/private IP). CIMD requires the AS to fetch the URL —
     a `localhost` URL would be rejected by the AS.
   - `client_id` is the metadata URL itself.

3. **DCR (Dynamic Client Registration)**
   - Only if `metadata.registration_endpoint` is present.
   - The AS returns a real `client_id` string — works with localhost
     because no fetch is required.
   - Loopback redirect URIs use `application_type: "native"` (RFC 8252
     §7.3), which most servers accept without the HTTPS requirement.

If all three fail → `ValueError` with actionable message.

### 4.4 PKCE (S256, mandatory)

Every flow uses PKCE with S256. No exceptions — OAuth 2.1 forbids
`plain` and forbids omitting PKCE for public clients.

```python
verifier  = base64url(os.urandom(32))           # 43 chars
challenge = base64url(sha256(verifier))         # 43 chars
```

The verifier is stored in `OAuthStateStore` (TTL = 10 min) and
consumed at token exchange time.

### 4.5 State nonce

`state = secrets.token_urlsafe(32)` — 256 bits of entropy. Stored
in `OAuthStateStore` under the nonce itself. The callback does a
**single-use pop** — a replayed state immediately fails.

### 4.6 Resource indicator (RFC 8707)

Every authorization URL and every token exchange request includes:

```
resource={mcp_server_url}
```

This binds the resulting token to the MCP server's audience. A token
issued for one MCP server cannot be replayed against a different
audience.

### 4.7 Redirect URI resolution

`resolve_public_base_url(request)` determines the base URL without
hardcoding ports. Precedence:

```
1. PELDRUN_BASE_URL env var          ← explicit override
2. FastAPI Request.base_url          ← exact host as seen by client
3. PELDRUN_PORT / PORT env var
4. sys.argv --port N                 ← uvicorn CLI
5. RuntimeError                      ← never fall back to a hardcoded port
```

The redirect URI is `{base_url}/api/mcp/oauth/callback`.

**Stored verbatim in the state entry** and replayed verbatim at
token exchange — prevents subtle mismatch when the callback arrives
via a different proxy path than the start call.

### 4.8 Two flow scopes

| Scope | Start endpoint | Storage | Reused by |
|---|---|---|---|
| **Global** | `POST /api/mcp/servers/{sid}/oauth/start` | `mcp_settings_manager.save_oauth()` | Every chat without a per-chat override |
| **Per-chat** | `POST /api/chats/{chat_id}/mcp-auth/{sid}/start` | `chat_mcp_auth.save_oauth_tokens()` | Only that chat |

**Both flows use the same OAuthManager**, the same PKCE generation,
the same state store, the same callback endpoint. The only
difference is where the resulting tokens are persisted.

**Global flow is the recommended one** — most users authorize once
from the Store UI and never revisit. The per-chat flow exists for
cases where the same MCP server needs different credentials per
conversation (e.g. two Notion workspaces).

### 4.9 Token exchange

`POST {token_endpoint}` with form-encoded body:

```
grant_type=authorization_code
code={code}
redirect_uri={redirect_uri}
client_id={client_id}
code_verifier={verifier}
resource={mcp_server_url}
client_secret={client_secret}   ← only for pre-registered confidential clients
```

Headers: `Accept: application/json`.

The AS response must contain `access_token`. Failures surface the
AS-provided `error` / `error_description` in the raised `ValueError`.

### 4.10 Token storage

**Global:**
```json
{
  "access_token": "...",
  "token_type": "Bearer",
  "refresh_token": "...",
  "expires_in": 3600,
  "expires_at": 1730003600,
  "resource": "https://mcp.notion.com/mcp"
}
```

Encrypted with AAD: `"global:{server_id}:oauth"`.

**Per-chat:** identical shape, but encrypted with AAD
`"chat:{chat_id}:{server_id}:oauth"` and stored inside
`mcp_auth.json`.

**`expires_at` is absolute** (Unix timestamp), so refresh logic does
not need to track elapsed time across process restarts.

### 4.11 Callback page

`GET /api/mcp/oauth/callback` returns a self-contained HTML page
that:
1. `postMessage`s a structured result to `window.opener`
   (`origin` is `window.location.origin` — never `*`).
2. Closes the popup after 150ms.
3. Shows a human-readable message for browsers that block
   `window.close()`.

The page loads **zero external resources** — no CDN scripts, no
external fonts, no remote stylesheets. It renders identically
offline.

**Payload shape:**
```json
{
  "source": "peldrun-mcp-oauth",
  "status": "success" | "error",
  "chat_id": "<id>" | null,
  "server_id": "<id>",
  "error_code": "<code>",        // error only
  "error_message": "<message>"   // error only
}
```

### 4.12 Error codes

| `error_code` | Meaning |
|---|---|
| `missing_state` | Provider redirected without `state` — likely an AS bug |
| `invalid_state` | State nonce expired (>10 min) or already used |
| `missing_code` | No `authorization_code` in callback |
| `exchange_failed` | Token endpoint rejected the code |
| `persist_failed` | Backend could not encrypt / store the tokens |
| (AS-provided) | Provider's own `error` field (e.g. `access_denied`) |

---

## 5. OAuth Status and Disconnect

### 5.1 Status endpoint

`GET /api/mcp/servers/{sid}/oauth/status`

```json
{
  "status": "ok",
  "server_id": "notion_mcp",
  "oauth_capable": true,
  "authenticated": true,
  "needs_refresh": false,
  "has_refresh_token": true
}
```

**`needs_refresh`** is `true` when `expires_at` is within 60 seconds
of now. The Store UI uses this to show a "Reconnect" button instead
of "Connect".

**`oauth_capable`** is `false` for stdio servers — they have no URL
to redirect to.

### 5.2 Disconnect

`DELETE /api/mcp/servers/{sid}/oauth`

Removes the global token bundle. **Does not revoke** at the provider —
the user must also remove the integration from the provider's own
settings (e.g. Notion's "Manage workspace clients").

This is intentional: revoking at the provider requires a separate
call to a provider-specific revoke endpoint, which not all servers
expose. Local removal is sufficient to force re-authorization on
the next use.

---

## 6. mTLS (Mutual TLS)

### 6.1 When to use

The server:
- Has `transport` in `{"http", "sse"}`
- Has `auth_strategy: "mtls"`
- Has an `mtls` block in the manifest

**stdio servers cannot use mTLS.** A subprocess has no TLS layer to
participate in. The `mtls` block is silently ignored on stdio.

### 6.2 Manifest shape

```json
{
  "auth_strategy": "mtls",
  "mtls": {
    "cert_env": "MTLS_CLIENT_CERT",
    "key_env":  "MTLS_CLIENT_KEY",
    "ca_env":   "MTLS_CA_BUNDLE"
  },
  "env_schema": {
    "required": ["MTLS_CLIENT_CERT", "MTLS_CLIENT_KEY"],
    "properties": {
      "MTLS_CLIENT_CERT": {
        "type": "string", "secret": true,
        "title": "Client Certificate Path"
      },
      "MTLS_CLIENT_KEY": {
        "type": "string", "secret": true,
        "title": "Client Private Key Path"
      },
      "MTLS_CA_BUNDLE": {
        "type": "string", "secret": false,
        "title": "Custom CA Bundle (Optional)"
      }
    }
  }
}
```

**Credentials are file paths, not the certificate content itself.**
This lets the operator rotate a cert by replacing the file without
editing the manifest, and avoids embedding a private key in a JSON
document that might get shared.

### 6.3 Runtime resolution

Both `mcp_pool.get_or_create_client` and `mcp_pool.probe_connection`
resolve the `mtls` block at connect time:

```python
mtls_block = manifest.get("mtls")
if isinstance(mtls_block, dict) and fallback_url:
    cert_path = env.get(mtls_block["cert_env"], "")
    key_path  = env.get(mtls_block.get("key_env", ""), "")
    ca_path   = env.get(mtls_block.get("ca_env", ""), "")

    if cert_path:
        client_cert = (cert_path, key_path) if key_path else cert_path
    if ca_path:
        ca_bundle = ca_path
```

**Differences between the two paths:**

| | `get_or_create_client` | `probe_connection` |
|---|---|---|
| `env_override` semantics | **Replaces** global env (per-chat isolation) | **Merges** over global env (test override) |
| Why | A chat that chose "custom" must not silently inherit global certs | User testing a freshly-pasted cert expects fallback to global |

### 6.4 httpx wiring

`MCPClient._get_client` passes the resolved values to
`httpx.AsyncClient`:

```python
client_kwargs = {
    "headers": headers,
    "timeout": httpx.Timeout(self.timeout),
    "transport": self._http_transport,
}

if self._client_cert is not None:
    if isinstance(self._client_cert, (tuple, list)) and len(self._client_cert) == 2:
        client_kwargs["cert"] = (str(self._client_cert[0]), str(self._client_cert[1]))
    else:
        client_kwargs["cert"] = str(self._client_cert)

if self._ca_bundle:
    client_kwargs["verify"] = str(self._ca_bundle)

self._client = httpx.AsyncClient(**client_kwargs)
```

`httpx` accepts either a single combined cert+key `.pem` file or a
`(cert, key)` tuple — PELDRUN normalizes to that contract.

### 6.5 Failure modes

| Symptom | Cause |
|---|---|
| `SSLError` immediately | Cert/key path does not exist or is not readable |
| `SSLError` after handshake | Cert rejected by server (expired, wrong CA) |
| `TypeError` on client creation | Malformed cert argument (logged as warning, falls back to no cert) |
| Timeout (60s+) | Server accepted TLS but did not respond to MCP protocol — likely auth passed, protocol failed |

**A malformed cert path never hangs.** The `TypeError` catch in
`_get_client` logs a warning and falls back to no client cert; the
next HTTPS request fails fast with `SSLError`.

---

## 7. HTTP Basic Auth

Used only when:
- `transport` is `"http"` or `"sse"`
- The manifest declares `http_basic_auth`
- **No** Bearer token is available (OAuth and pre-registered
  credentials take precedence)

**Manifest shape:**

```json
{
  "http_basic_auth": {
    "user_env": "WORDPRESS_USER",
    "pass_env": "WORDPRESS_APP_PASSWORD"
  }
}
```

The env var **names** are declared here, and the env var **values**
hold the actual username and password. At connection time,
`mcp_pool.build_transport` reads both and constructs the
`Authorization: Basic {base64(user:pass)}` header.

**Bearer wins.** If both an OAuth token and Basic credentials are
present, Bearer is used. This is the correct precedence — an OAuth
flow is a more specific user decision than static env credentials.

**SSE limitation:** HTTP Basic over SSE is not expressible via the
`auth_token` parameter of `SSETransport` (the transport hardcodes
`"Bearer "`). Only `"http"` transport supports Basic auth in
current builds. WordPress is the only first-party server that uses
Basic, and it is HTTP-only — so this limitation does not affect any
shipped integration.

---

## 8. Secret Storage

All secrets — env variables, OAuth tokens, mTLS is not encrypted
because it stores paths, not secrets — go through the same engine:
`omweb/extensions/secret_storage.py`.

### 8.1 Encryption scheme

| Parameter | Value |
|---|---|
| Algorithm | AES-256-GCM |
| Key length | 256 bits (32 bytes) |
| Nonce length | 96 bits (12 bytes) |
| Tag length | 128 bits (16 bytes) |
| Output encoding | `base64(nonce ‖ ciphertext ‖ tag)` |

### 8.2 Key management

**Location:** `~/.peldrun/secrets.key`

**Outside `backend/storage/`** — deliberate. This means:
- Backups of the project do not carry the encryption key.
- Git archives never contain the key (it lives in the user's home).
- A leaked project dump does **not** compromise credentials.

**First use:** the key is generated with `os.urandom(32)` and
written to disk with permissions `0600` (POSIX; Windows uses ACLs
inherited from the parent directory).

**Subsequent uses:** the file is read and validated (`len == 32`).
A corrupt key raises immediately — no silent fallback to plaintext.

### 8.3 AAD binding

Every ciphertext is bound to its logical context via AAD
(Additional Authenticated Data):

| Ciphertext purpose | AAD |
|---|---|
| Global env | `"global:{server_id}"` |
| Global OAuth | `"global:{server_id}:oauth"` |
| Per-chat env | `"chat:{chat_id}:{server_id}"` |
| Per-chat OAuth | `"chat:{chat_id}:{server_id}:oauth"` |

**Consequence:** a ciphertext copied from
`global:notion_mcp` and pasted into `chat:abc:notion_mcp` will **fail
to decrypt**. GCM verifies the tag against the AAD; a mismatch
raises `ValueError`.

**Distinct `:oauth` suffix** — the two fields (`env_enc` and
`oauth_enc`) cannot be swapped at rest, even if the attacker has
write access to the settings file.

### 8.4 Decryption failures

A failed decryption **never silently returns empty data.** Every
call site:

- Logs a warning with the specific context.
- Returns `None` (for OAuth) or empty dict (for env).
- The engine interprets `None` / empty as "no credentials" and
  re-prompts the user.

This is the correct behavior for a secret store: failing closed is
safer than silently serving stale or garbage data.

---

## 9. Legacy Plaintext Migration

Older PELDRUN versions stored env in plaintext:

```json
{
  "settings": { ... },
  "env": { "GITHUB_TOKEN": "ghp_..." }   ← plaintext
}
```

`McpSettingsManager.__init__` calls `migrate_plaintext_files()` on
every process start. This:

1. Scans `storage/store/extensions/settings/*.json`.
2. Skips files that already carry a non-empty `env_enc`.
3. For files with plaintext `env`, routes them through
   `_write_server_payload` — the normal writer, which encrypts and
   atomically replaces.
4. Logs each migrated file.

**Idempotent.** Running on every start is safe. A migration failure
on one file is logged and skipped — it does not block the rest of
the sweep or prevent the app from starting.

**Legacy read behavior:** if `env_enc` is present but decryption
fails, the reader **returns empty env**, not the plaintext `env`.
This is intentional: a failed decrypt indicates tampering, wrong
key, or corruption. Serving plaintext would mask the failure.

---

## 10. Per-Chat Isolation Invariants

These are enforced by `resolve_tool_runtime._mcp_tool_executor`:

1. **Full replacement, not merge.** `env_override` (when present)
   fully replaces the global env. A chat that chose `custom` sees
   only its own credentials.
2. **AAD is chat-scoped.** A token encrypted for `chat:abc:github`
   cannot be decrypted for `chat:def:github`.
3. **Ephemeral is truly ephemeral.** `save_ephemeral()` stores in
   an in-memory dict guarded by an `asyncio.Lock`. It is **never**
   written to disk. A process restart loses it.
4. **A successful persistent save supersedes ephemeral.**
   `save_custom()` and `save_oauth_tokens()` call
   `_clear_ephemeral()` after writing, so an old ephemeral value
   cannot shadow a fresh persistent one.
5. **Session keys include a credential hash.** `mcp_pool` computes
   `sha256(json.dumps(env_override, sort_keys=True))[:12]` and
   appends it to the session key. A token refresh produces a new
   key → a fresh subprocess → no stale session with an expired
   token.

---

## 11. Lazy Auth Resolution

Historically, PELDRUN prompted for MCP credentials on **every
message**, even when the message did not need any MCP tool. That is
now fixed.

**Current behavior:**

1. The engine does **not** pre-check MCP auth.
2. When the agent actually invokes an MCP tool:
   - `_mcp_tool_executor` looks up the credential mode.
   - If `mode is None` and `manifest_needs_auth(manifest)` is True:
     - `mcp_auth_state.mark_auth_required(job_id, ...)` records the
       requirement.
     - An `MCP_AUTH_REQUIRED` SSE event is dispatched to the frontend.
     - The job is failed with a clear error code.
     - `core_executor.cancel(job_id)` best-effort stops the run.
     - The tool call raises `RuntimeError` with an actionable message.

3. If `mode is None` and `manifest_needs_auth` is False:
   - Proceed without credentials. The tool call may succeed or fail
     depending on the server, but the user is **never** bothered
     with a modal for a server that doesn't require auth.

**Result:** "hi" works without opening any modal. "fetch this
GitHub issue" triggers the auth flow for GitHub only.

---

## 12. Failure Mode Reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `MCP_AUTH_REQUIRED` error in a chat | Server needs credentials, none saved for this chat | Click the auth modal (or open Settings) |
| OAuth "Invalid client_id" | CIMD rejected on localhost | Falls back to DCR automatically — should not surface |
| `redirect_uri` mismatch | Server has a stale registered URI | Re-run `oauth/start` to trigger a fresh registration |
| Token exchange returns `invalid_grant` | PKCE verifier mismatch or expired code | State TTL is 10 min — start the flow again |
| `SSLError` on mTLS server | Cert/key path wrong or cert expired | Verify paths in Settings |
| Basic auth returns 401 | Wrong username or app password | Re-save credentials; ensure `http_basic_auth` env names match |
| Decryption warning in logs | Key file changed, or ciphertext tampered | Credentials become "no credentials" → re-prompt. If key was lost, **all** secrets must be re-entered |
| `migrate_plaintext_files` warning | Corrupt JSON in one settings file | Inspect the named file; the rest of the sweep continues |

---

## 13. Security Summary

**Threat model:** an attacker who gains read access to the
`backend/storage/` directory, or write access to a settings file,
**must not** be able to:
- Read plaintext credentials.
- Swap a ciphertext from one context to another.
- Replay a token against a different chat or server.
- Recover credentials from a project backup.

**Mitigations (all enforced in code):**

| Threat | Mitigation | Where |
|---|---|---|
| Plaintext at rest | AES-256-GCM with `~/.peldrun/secrets.key` outside `storage/` | `secret_storage.py` |
| Cross-context replay | AAD includes server_id, chat_id, and field name | All encrypt/decrypt call sites |
| Cross-field swap | Distinct `:oauth` AAD suffix | `chat_mcp_auth.py`, `settings_manager.py` |
| Stale session after refresh | Cred hash in session key | `mcp_pool.get_or_create_session` |
| Silent corruption | Decryption failure yields `None`, never plaintext | All readers |
| Orphaned key on rollback | Key file survives storage wipe; must be re-generated manually | `_load_or_create_key` |

**Not covered by PELDRUN (operator's responsibility):**
- OS-level file permissions on `~/.peldrun/secrets.key` beyond
  `0600` (Windows ACLs are inherited).
- Backups of `~/.peldrun/` — this **does** contain the key.
- Physical access to the running machine.

---

## 14. Endpoint Reference

### 14.1 Per-chat credential management

```
POST   /api/chats/{chat_id}/mcp-auth/{server_id}/start    → begin OAuth
POST   /api/chats/{chat_id}/mcp-auth/{server_id}/token    → save custom env
POST   /api/chats/{chat_id}/mcp-auth/{server_id}/env      → alias of /token
POST   /api/chats/{chat_id}/mcp-auth/{server_id}/use-global → record choice
POST   /api/chats/{chat_id}/mcp-auth/{server_id}/ephemeral → in-memory only
DELETE /api/chats/{chat_id}/mcp-auth/{server_id}          → remove entry
GET    /api/chats/{chat_id}/mcp-auth                      → list entries (no secrets)
```

### 14.2 Global OAuth management

```
POST   /api/mcp/servers/{server_id}/oauth/start    → begin OAuth (global scope)
GET    /api/mcp/servers/{server_id}/oauth/status   → connection state
DELETE /api/mcp/servers/{server_id}/oauth          → remove tokens
```

### 14.3 Callback and CIMD

```
GET    /api/mcp/oauth/callback                     → OAuth popup handler
GET    /api/mcp/oauth/client-metadata.json         → CIMD document
```

### 14.4 Global env (via Settings modal)

```
GET    /api/mcp/servers/{server_id}/settings       → current env (decrypted, for edit)
PUT    /api/mcp/servers/{server_id}/settings       → save new env
```

**Note:** `GET /settings` returns the **decrypted** env so the UI
can display it. This is intentional — the user needs to see what is
currently stored to edit it. The response never leaves the
authenticated session, and the endpoint is not cached.

---

## 15. Related Documentation

- **Manifest reference:** `docs/MCP_MANIFEST_REFERENCE.md`
- **Implementation:** `omweb/extensions/secret_storage.py`,
  `omweb/extensions/chat_mcp_auth.py`,
  `omweb/extensions/auth_manager.py`,
  `omweb/routers/mcp_auth.py`
- **OAuth 2.1 spec:** draft-ietf-oauth-v2-1
- **PKCE:** RFC 7636
- **Resource indicators:** RFC 8707
- **Protected resource metadata:** RFC 9728
- **Native app redirects:** RFC 8252
- **MCP 2025-11-25 authorization:** spec.modelcontextprotocol.io

---

*Last updated: post A/B/C implementation. Tested against GitHub
(pre-registered OAuth), Notion (discovery + DCR), WordPress (HTTP
Basic), and a synthetic mTLS server.*
```
