


```markdown
# MCP Authentication — Architecture & Operations Guide

**Status:** Production
**Last Updated:** October 2026
**Audience:** Backend engineers, frontend engineers, and operators
maintaining PELDRUN's Model Context Protocol integration.

---

## 1. Overview

PELDRUN supports per-chat authentication for MCP servers using
**OAuth 2.1** and **environment-variable credentials**. The system is
designed around three principles:

1. **Encryption at rest.** Every credential — OAuth token or env var —
   is encrypted with AES-256-GCM before it touches the disk.
2. **Per-chat isolation.** The same MCP server can be used with
   different credentials in different chats simultaneously.
3. **Lazy authentication.** Credentials are only requested from the
   user the moment an MCP tool actually needs them, never on every
   message.

---

## 2. Storage Layout

| Path | Purpose | Encryption |
|---|---|---|
| `~/.peldrun/secrets.key` | 32-byte AES-256-GCM master key | File permission 0600 (POSIX); outside project backups |
| `backend/storage/chats/{chat_id}/mcp_auth.json` | Per-chat credential decisions | All secrets (`env_enc`, `oauth_enc`) encrypted |
| `backend/storage/store/extensions/settings/{server_id}.json` | Global (server-wide) credentials | `env_enc` encrypted |

**Why the key lives in `~/.peldrun/` and not in the project:**
The project directory is commonly archived, zipped into bug reports,
or pushed to a remote — the key would travel with it. `~/.peldrun/`
is outside the project tree and is preserved across reinstalls.

**AAD (Additional Authenticated Data):**
Every ciphertext is bound to its context:
- `global:{server_id}` for global env
- `chat:{chat_id}:{server_id}` for per-chat env
- `chat:{chat_id}:{server_id}:oauth` for per-chat OAuth bundle

This prevents a ciphertext from being moved between contexts: a token
copied from chat A into chat B will fail to decrypt because the AAD
no longer matches.

---

## 3. Three Credential Modes

A chat can decide how to supply credentials for each MCP server in
three ways:

### 3.1 `custom` — Persistent per-chat credentials

Stored in `backend/storage/chats/{chat_id}/mcp_auth.json` under
`entries.{server_id}`. Encrypted. Survives restarts and re-runs.

Endpoint: `POST /api/chats/{chat_id}/mcp-auth/{server_id}/token`

### 3.2 `use_global` — Fallback to server-wide settings

Recorded as a marker (no credentials in the file). When this mode is
active, the executor reads from `mcp_settings_manager.get_env(server_id)`
which is the same value managed from the "Extensions & MCP" page in
the Store UI.

Endpoint: `POST /api/chats/{chat_id}/mcp-auth/{server_id}/use-global`

### 3.3 `ephemeral` — One-shot, in-memory only

Never written to disk. Dropped on process restart. Superseded by any
subsequent persistent save.

Endpoint: `POST /api/chats/{chat_id}/mcp-auth/{server_id}/ephemeral`

---

## 4. Lazy Authentication Flow

**Design decision (October 2026):** The backend no longer runs an
auth check before every message. The old pre-flight gate failed the
job on "hi" whenever the agent's manifest included an authenticated
MCP server — even if the user had no intention of using MCP in that
turn.

The correct behaviour is lazy: enforcement happens the moment an MCP
tool is actually invoked.

### Flow diagram

```
User: "hi"
  │
  ▼
POST /api/run → PeldrunEngine.run()
  │
  ├─ Discovery: try every target MCP server (env_override = whatever
  │  the store already has; None if no decision yet)
  │
  ├─ Materialise tool adapters (no auth enforcement here)
  │
  ├─ Agent runs with all tools available
  │
  └─ Agent: "مرحباً!" — no tool call → job completes normally ✓
```

```
User: "list my GitHub repos"
  │
  ▼
POST /api/run → PeldrunEngine.run()
  │
  ├─ Same setup as above
  │
  ├─ Agent decides to call mcp__mcp_github__list_repositories
  │
  ├─ _mcp_tool_executor runs (in resolve_tool_runtime.py):
  │    ├─ Check store.get_mode(chat_id, "mcp_github") → None
  │    ├─ manifest_needs_auth → True
  │    ├─ mark_auth_required(job_id, ...)
  │    ├─ dispatch_event(MCP_AUTH_REQUIRED) → modal opens
  │    ├─ job_manager.fail_job(job_id, "MCP_AUTH_REQUIRED: ...")
  │    ├─ core_executor.cancel(job_id) [best-effort]
  │    └─ raise RuntimeError("MCP_AUTH_REQUIRED: ...")
  │
  ├─ peldrun_engine.py: pop_auth_required(job_id) → skips normal
  │  completion path (the job is already failed server-side)
  │
  ▼
Frontend: modal opens → user picks decision → POST /api/run (retry)
  │
  ▼
Second run: store.get_mode → "use_global"
  │
  ├─ system_prompt gets "[MCP SERVER ACCESS]" hint
  │
  ├─ Agent calls the tool again
  │
  ├─ _mcp_tool_executor sees mode == "use_global" → env_override
  │  is None → mcp_pool falls back to settings_manager
  │
  └─ Tool succeeds → job completes normally ✓
```

### Why this design is important

| Scenario | Old (pre-flight) | New (lazy) |
|---|---|---|
| Chat with MCP server, user says "hi" | ❌ Fails, modal opens | ✅ Works normally |
| Chat with MCP server, user needs MCP | ✅ Modal opens | ✅ Modal opens |
| Chat with no MCP servers | ✅ Works | ✅ Works |
| Chat where auth is broken but user never uses MCP | ❌ Fails | ✅ Works |

---

## 5. OAuth 2.1 Flow

For MCP servers reachable over HTTP (`transport: "sse" | "http"`),
OAuth is supported. The full flow:

### 5.1 Discovery

`GET {mcp_server_url}/.well-known/oauth-protected-resource` (RFC 9728)
→ returns `authorization_servers: [issuer]`
→ then `GET {issuer}/.well-known/oauth-authorization-server`
→ returns `authorization_endpoint`, `token_endpoint`,
  `registration_endpoint`, `client_id_metadata_document_supported`

If discovery fails, `auth_manager.get_metadata()` falls back to a
pre-registered table (`PRE_REGISTERED_CLIENTS`) keyed by hostname.

### 5.2 Client Registration

Three mechanisms, in order of preference:

| Mechanism | When used | Client secret |
|---|---|---|
| **CIMD** | Server advertises `client_id_metadata_document_supported` | No |
| **DCR** | Server exposes `registration_endpoint` | Rarely |
| **Pre-registered** | Hostname matches a known provider (GitHub) | Optional |

**CIMD** uses a URL as `client_id`. The URL points to a JSON document
served by `GET /api/mcp/oauth/client-metadata.json` on the PELDRUN
instance. The document's `client_id` field is always the exact URL
the document was served from — that is the invariant CIMD relies on.

### 5.3 Authorization

`POST /api/chats/{chat_id}/mcp-auth/{server_id}/start` returns
`{auth_url, state}`. The frontend opens `auth_url` in a popup.

Mandatory parameters sent to the AS:
- `code_challenge_method=S256` (PKCE, never `plain`)
- `resource={mcp_server_url}` (RFC 8707)
- `state={random nonce}` (single-use)

### 5.4 Callback

`GET /api/mcp/oauth/callback?code=...&state=...`

- Validates `state` by popping it from `oauth_state_store`
  (single-use; second call fails).
- Exchanges the code with the original `code_verifier` and the
  **verbatim** `redirect_uri` from the start call.
- Saves tokens via `chat_mcp_auth.save_oauth_tokens()`.
- Returns HTML that `postMessage`s the result to `window.opener` and
  closes itself.

Frontend listener must verify:
```js
event.origin === window.location.origin
event.data.source === "peldrun-mcp-oauth"
event.data.chat_id === chatId
event.data.server_id === serverId
```

---

## 6. Public Base URL Resolution

**No port is hardcoded anywhere.** `resolve_public_base_url()` uses
this precedence:

1. `PELDRUN_BASE_URL` env var (explicit override)
2. FastAPI `Request.base_url` (per-request, exact as the client sees)
3. `PELDRUN_PORT` / `PORT` env var
4. `sys.argv` — uvicorn CLI (`--port N` / `--port=N`)
5. `RuntimeError` — never silently fall back to a wrong port

For deployments behind a reverse proxy, always set `PELDRUN_BASE_URL`
to the **public** URL (e.g. `https://peldrun.example.com`), not the
internal one. Otherwise the `redirect_uri` sent to the OAuth provider
will not match what the browser actually sees.

---

## 7. Configuration Reference

### Required environment variables (pre-registered OAuth clients)

| Variable | Purpose | Example |
|---|---|---|
| `PELDRUN_GITHUB_CLIENT_ID` | GitHub OAuth app client id | `Iv1.abc123` |
| `PELDRUN_GITHUB_CLIENT_SECRET` | GitHub OAuth app secret (only if required by the app type) | `••••` |
| `PELDRUN_BASE_URL` | Public URL of the PELDRUN instance | `https://peldrun.example.com` |

Register a GitHub OAuth app at
`https://github.com/settings/developers` with:
- Homepage URL: `{PELDRUN_BASE_URL}`
- Authorization callback URL: `{PELDRUN_BASE_URL}/api/mcp/oauth/callback`

---

## 8. Adding a New MCP Server

### 8.1 Env-based (stdio, non-OAuth)

Add to the server's manifest:

```json
{
  "auth_strategy": "env",
  "env_schema": {
    "type": "object",
    "required": ["MY_API_TOKEN"],
    "properties": {
      "MY_API_TOKEN": {
        "type": "string",
        "title": "My API Token",
        "description": "Create one at https://example.com/tokens",
        "secret": true
      }
    }
  }
}
```

`secret: true` marks the field as sensitive in both the Store UI and
the per-chat auth modal. `required` ensures the modal will not accept
an empty submit.

### 8.2 OAuth-based (HTTP/SSE)

```json
{
  "transport": "sse",
  "url": "https://api.example.com/mcp/",
  "auth_strategy": "oauth",
  "scopes": ["read", "write"],
  "client_metadata_url": "https://example.com/client-metadata.json"
}
```

If the provider does not support CIMD or DCR, add it to
`PRE_REGISTERED_CLIENTS` in `backend/omweb/extensions/auth_manager.py`
with env var names for the client id and secret.

---

## 9. API Reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chats/{chat_id}/mcp-auth/{server_id}/start` | Begin OAuth; returns `{auth_url, state}` |
| `GET` | `/api/mcp/oauth/callback` | OAuth callback (redirect target) |
| `GET` | `/api/mcp/oauth/client-metadata.json` | CIMD document |
| `POST` | `/api/chats/{chat_id}/mcp-auth/{server_id}/token` | Save custom env |
| `POST` | `/api/chats/{chat_id}/mcp-auth/{server_id}/env` | Alias of `/token` |
| `POST` | `/api/chats/{chat_id}/mcp-auth/{server_id}/use-global` | Record `use_global` |
| `POST` | `/api/chats/{chat_id}/mcp-auth/{server_id}/ephemeral` | Save ephemeral env |
| `DELETE` | `/api/chats/{chat_id}/mcp-auth/{server_id}` | Remove credentials |
| `GET` | `/api/chats/{chat_id}/mcp-auth` | List entries (no secrets returned) |

---

## 10. Security Invariants

Every one of these must remain true:

| # | Rule | Where enforced |
|---|---|---|
| S1 | Tokens are never logged | Handlers use `logger.info` with truncated ids only |
| S2 | Ciphertext is bound to its context | `aad=f"chat:{cid}:{sid}"` |
| S3 | Refresh tokens rotate | `refresh()` returns a new token; callers must overwrite |
| S4 | `code_verifier` is never on disk | Stored in `OAuthStateStore` in memory only |
| S5 | Loopback / HTTPS redirect only | `resolve_public_base_url()` refuses wrong-port fallbacks |
| S6 | PKCE S256 is mandatory | `generate_pkce()` returns S256 only |
| S7 | `resource` parameter is always present | `build_authorization_url()` requires it |
| S8 | Discovery fails closed | `get_metadata()` raises when no mechanism matches |
| S9 | Ephemeral credentials never touch disk | In-memory dict in `ChatMcpAuthStore` only |
| S10 | State nonce is single-use | `oauth_state_store.pop()` at callback |

---

## 11. Troubleshooting

### "This MCP server declares no additional configurable settings"
The server's manifest has no `env_schema`. Add one and restart.
Also: an old copy of the manifest may exist in
`backend/storage/store/extensions/{id}.json` (created by any past
toggle action). Delete it to force the registry to re-read the
built-in manifest.

### "PELDRUN_GITHUB_CLIENT_ID missing"
Register a GitHub OAuth app and export the environment variable.
Restart the backend to pick it up.

### "Cannot determine PELDRUN's public base URL"
Set `PELDRUN_BASE_URL` or run behind a proxy that sets it. See §6.

### "Invalid or expired state" on OAuth callback
The state nonce has a 10-minute TTL. If the user took longer than
that to complete the provider's login page, they must start over.

### Modal opens, user authenticates, but agent still says it lacks access
Some models (GigaChat historically) refuse to use MCP tools after
seeing a previous auth error. The engine injects an
`[MCP SERVER ACCESS]` hint into the system prompt when at least one
server has a recorded decision. If the refusal persists:
1. Start a fresh chat (the old conversation may still confuse the model).
2. Confirm `mcp_auth.json` contains `"mode": "use_global"` or a valid
   `env_enc`.

### Duplicate "Authentication Required" blocks in the timeline
This was fixed in October 2026 by not archiving failed-auth turns
into `historyTurns` (`handleStartTask(..., isAuthRetry=true)`).
If it recurs, check `frontend/src/components/chat/chat-container/index.tsx`
around the `if (!isAuthRetry && submittedPrompt && ...)` guard.

---

## 12. Testing Checklist

Run through this before deploying a change to the auth stack:

- [ ] Fresh chat → "hi" → no modal, model responds
- [ ] Fresh chat → "list my repos" → modal opens, no red error box
- [ ] Choose "Use Global" → job retries automatically → succeeds
- [ ] Same chat → "tell me about repo X" → no modal, tool works
- [ ] Fresh chat → "list my repos" → modal opens again (isolation)
- [ ] Choose "Save Credentials" → enter a PAT → job retries → succeeds
- [ ] Restart backend → same chat → prompt → no modal (persistence)
- [ ] Choose "One-time" → enter PAT → succeeds → restart backend →
      prompt → modal opens again (ephemeral was dropped)
- [ ] `grep -r "ghp_" backend/storage/chats/` returns nothing
- [ ] `cat ~/.peldrun/secrets.key | wc -c` returns 32
- [ ] Delete a chat → the whole directory is gone, including
      `mcp_auth.json`
- [ ] OAuth server (HTTP): popup opens, sign-in completes, modal closes,
      tool works
- [ ] OAuth popup closed manually → no zombie popup, "Cancel" works
- [ ] OAuth state reused → second call returns "invalid_state"
```

---
