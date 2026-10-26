# 📘 PELDRUN MCP Integration — Engineering Documentation

```markdown
# docs/MCP_INTEGRATION.md
```

---

# PELDRUN MCP Integration — Engineering Documentation

**Version:** 1.0
**Last Updated:** 2026-10-27
**Audience:** Backend engineers, Frontend engineers, Integration engineers, Future maintainers
**Status:** Stable — production-ready

---

## 1. Executive Summary

PELDRUN integrates external services through the **Model Context Protocol (MCP)** — an open standard that lets a host application discover and invoke tools exposed by remote servers over JSON-RPC 2.0.

The integration is designed around a **single, generic pipeline**:

```
Any MCP server (GitHub, WordPress, Slack, PostgreSQL, ...)
        ↓  (stdio subprocess or HTTP/SSE)
[mcp_session_pool] — persistent connections, cache, lifecycle
        ↓
[resolve_tool_runtime] — schemas + executors
        ↓
[peldrun_engine] — tools materialised into AgentSpec.tools
        ↓
LLM calls tools → mcp_client sends tools/call → server responds
```

**No service-specific code exists.** PELDRUN has no hardcoded knowledge of GitHub, WordPress, or any other service. The only requirement for adding a service is to register its MCP server manifest. This document specifies:

1. How the current system works (components, data flow, storage, caching).
2. How to **add a new MCP service** (the primary extension point).
3. How to troubleshoot and extend the integration.

---

## 2. Architecture Overview

### 2.1 Layered View

```
┌──────────────────────────────────────────────────────────────┐
│ Frontend (React / Next.js)                                    │
│  • /stores → Agents section → AgentMcpModal                    │
│  • /stores → Extensions section → MCPSettingsModal             │
│  • /chat → ActiveToolsModal                                    │
│  • /chat → ToolCallCard                                        │
└──────────────────────────────┬───────────────────────────────┘
                               │ REST API
┌──────────────────────────────▼───────────────────────────────┐
│ Routers (FastAPI)                                              │
│  • omweb/routers/mcp.py         — server lifecycle             │
│  • omweb/routers/store_rtr.py   — extension + agent MCP state  │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ Core Services                                                  │
│  • extensions/registry.py       — manifest registry            │
│  • extensions/settings_manager.py — per-server settings/env    │
│  • extensions/bundle_manager.py — zip install + pip/npm        │
│  • extensions/mcp_pool.py       — session pool + cache         │
│  • agents/registry.py           — per-agent MCP binding        │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ Runtime (PeldrunEngine)                                        │
│  • engines/peldrun_engine.py    — discovery + materialisation  │
│  • adapters/.../resolve_tool_runtime.py — schema + executor    │
│  • adapters/.../web_tool_adapter.py     — Core ToolResult      │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ PELDRUN Core                                                   │
│  • peldrun/tools/mcp_client.py  — JSON-RPC 2.0 client          │
│  • peldrun/tools/transports/stdio.py — subprocess transport    │
└──────────────────────────────┬───────────────────────────────┘
                               │
                      MCP Server (external)
                  (GitHub, WordPress, Slack, ...)
```

### 2.2 Key Design Principles

| Principle | Implementation |
|---|---|
| **Generic, not service-specific** | Zero code branches on `server_id`. The pipeline is data-driven via manifests. |
| **Workspace-isolated** | Every stdio session is keyed by `(server_id, resolved_workspace_root)`. Two chats = two subprocesses. |
| **Cache-first** | Tool discovery is cached per server with a 5-minute TTL and explicit invalidation. |
| **Lazy + parallel discovery** | MCP discovery runs concurrently via `asyncio.gather`, not serially. |
| **Per-agent opt-in** | No agent receives MCP tools unless explicitly bound via `mcp_servers` or `extra_mcp_servers`. |
| **Graceful shutdown** | `lifespan` calls `mcp_session_pool.close_all()` with a 30s timeout. |
| **Defensive schema handling** | Complex JSON schemas (`$ref`, `oneOf`, nested arrays) pass through verbatim instead of being silently flattened. |

---

## 3. Component Reference

### 3.1 `peldrun/tools/mcp_client.py` — JSON-RPC Client

**Purpose:** Core MCP client. Sends `tools/list` and `tools/call` to the server; parses responses.

**Key methods:**

| Method | Purpose |
|---|---|
| `_send_jsonrpc_request(method, params)` | Dispatches a JSON-RPC 2.0 request via the configured transport. |
| `discover_tools(workspace_root)` | Calls `tools/list` and returns `DynamicMCPTool` instances. **Silently swallows errors** — see §13. |
| `call_tool(tool_name, arguments)` | Calls `tools/call` and returns a `ToolResult`. |
| `close()` | Closes transport + HTTP client. |

**Important:** `discover_tools` returns `[]` on any failure. Use `_send_jsonrpc_request("tools/list")` directly when you need strict error handling (which `mcp_pool.probe_connection` and `get_tools` do).

---

### 3.2 `peldrun/tools/transports/stdio.py` — Stdio Transport

**Purpose:** Spawns MCP servers as subprocesses; communicates over stdin/stdout.

**Configuration:**
- `command` — resolved executable (validated by `mcp_pool._build_command_args`)
- `args` — CLI arguments
- `env` — merged with `os.environ`
- `cwd` — **set to workspace_root** (critical for `@modelcontextprotocol/server-filesystem`)

**Lifecycle:**
- `aconnect()` — spawns subprocess (idempotent).
- `asend_request()` — writes JSON line, reads JSON line with timeout.
- `aclose()` — terminates subprocess (SIGTERM → SIGKILL after 5s).

---

### 3.3 `omweb/extensions/mcp_pool.py` — Session Pool

**Purpose:** Central singleton `mcp_session_pool` managing all MCP connections.

**State:**

```python
_clients: Dict[str, MCPClient]     # key: "{server_id}:{workspace_root}"
_locks:   Dict[str, asyncio.Lock]  # per-key lock for creation
_global_lock: asyncio.Lock         # protects _clients/_locks/_closing
_closing: Set[str]                 # keys being torn down (race guard)
_tool_cache: Dict[str, {...}]      # per-server tool discovery cache
_cache_ttl: float                  # 300 seconds (default)
```

**Public API:**

| Method | Purpose | Concurrency |
|---|---|---|
| `get_or_create_client(server_id, manifest, workspace_root)` | Returns pooled client; spawns if missing | Double-checked locking + `_closing` guard |
| `get_tools(server_id, manifest, workspace_root, force_refresh=False)` | **Preferred** — returns cached tool list; probes on miss | Per-server probe lock (deduplicates concurrent calls) |
| `get_cached_tools(server_id)` | **Sync** — returns cache or `None` | Lock-free (`dict.get` is atomic) |
| `invalidate_cache(server_id)` | Async cache eviction | Global lock |
| `invalidate_cache_sync(server_id)` | Sync cache eviction (for `install_bundle`) | Lock-free |
| `probe_connection(manifest, workspace_root)` | One-shot strict diagnostic | Isolated; no cache |
| `close_session(server_id, workspace_root)` | Close one specific key | `_closing` barrier |
| `close_server_sessions(server_id)` | Close **all** workspace-bound sessions | `_closing` barrier |
| `close_all()` | Global shutdown | `_closing` barrier |

**Cache key:** `f"{server_id}:{Path(workspace_root).resolve() if workspace_root else 'default'}"`

**Why the `_closing` set:** Without it, a `close_session` that races with an in-flight `get_or_create_client` could leave an orphaned subprocess. The set marks keys under teardown; `get_or_create_client` raises if its key is in the set.

**Cache TTL:** 5 minutes (`DEFAULT_CACHE_TTL_SECONDS = 300.0`). Configurable per `McpSessionPool` instance, but the singleton uses the default.

---

### 3.4 `omweb/extensions/registry.py` — Manifest Registry

**Purpose:** Discovers and manages MCP server manifests on disk.

**Discovery directories:**
- Built-in: `backend/omweb/extensions/builtins/*.json` (not present in current build, kept for future)
- Custom: `backend/storage/store/extensions/*.json`

**Key methods:**

| Method | Purpose |
|---|---|
| `list_extensions()` | All manifests (built-in + custom) |
| `list_mcp_servers()` | Filters to `type in ("mcp_server", "mcp")` |
| `get_extension(ext_id)` | Raw manifest |
| `get_effective_server(ext_id)` | Manifest + `effective_settings` + `effective_env` |
| `get_effective_servers_bulk(ids)` | Bulk variant (O(N) with 1 disk read) |
| `save_extension(data)` | **Atomic write + cache invalidation** if transport changed |
| `toggle_status(ext_id)` | **Atomic write + cache invalidation** |
| `delete_extension(ext_id)` | Remove custom manifest |
| `get_active_mcp_servers()` | `status == "active"` filter |

**Cache invalidation rule (`_config_changed`):**

Cache is invalidated on save **only if** one of these fields differs from the previous manifest:
- `command`
- `url`
- `transport`
- `env_schema`
- `settings_schema`

Metadata-only saves (name, description, author, tags) do **not** invalidate.

---

### 3.5 `omweb/extensions/settings_manager.py` — Settings & Env

**Purpose:** Stores per-server user settings and env vars.

**Storage:** `backend/storage/store/extensions/settings/{server_id}.json`

**File shape:**

```json
{
  "settings": { "allowed_directories": ["D:\\Projects\\Foo"] },
  "env": { "GITHUB_TOKEN": "ghp_..." }
}
```

**API:**

| Method | Purpose |
|---|---|
| `get_settings(server_id, schema)` | Defaults + saved values merged |
| `save_settings(server_id, values, schema)` | Validates against `settings_schema` (jsonschema) + atomic write |
| `get_env(server_id, schema)` | Same for env |
| `save_env(server_id, values, schema)` | Same for env |
| `get_effective_runtime_config(server_id, manifest)` | Returns `{"settings": {...}, "env": {...}}` |

**Security note:** `env` values are stored **in plaintext**. See §14.

---

### 3.6 `omweb/extensions/bundle_manager.py` — Bundle Installation

**Purpose:** Installs MCP servers packaged as `.zip` archives.

**Flow:**

```
upload → validate zip-slip → extract to bundles/{id}/ → read manifest.json
       → detect runtime (python/node) → install dependencies
       → register in extension_registry → return install report
```

**Runtime detection (`_detect_runtime`):**

1. Explicit `manifest.runtime` field
2. `requirements.txt` present → `python`
3. `package.json` present → `node`
4. Neither → `None` (no installation needed)

**Dependency installation:**

- **Python:** `sys.executable -m pip install -r requirements.txt` (timeout 300s)
- **Node:** `npm install --omit=dev --no-audit --no-fund --prefix <bundle>` (timeout 300s; `shell=True` on Windows)

**Rollback on failure:** Entire bundle directory removed; `McpBundleError(code="DEPENDENCY_INSTALL_FAILED")` with `stderr[-1500:]`.

**Post-install:** `mcp_session_pool.invalidate_cache_sync(bundle_id)`.

**Delete:** `await mcp_session_pool.close_server_sessions(bundle_id)` before filesystem removal.

---

### 3.7 `omweb/adapters/core_adapter_parts/resolve_tool_runtime.py`

**Purpose:** Bridges platform tool IDs to executable adapters.

**`resolve_tool_runtime(tool_id, tool_meta, workspace_root, ...)` — resolution order:**

1. `ask_human` special case
2. **MCP tool** (`mcp__server__tool` or `mcp::server::tool`)
3. `AdapterRegistry` lookup (built-in tools)
4. `get_custom_tool_instance` fallback (custom + bundle tools)

**`resolve_authorized_tools(authorized_tool_ids, ...)` — bulk resolver:**

- Iterates over IDs.
- **MCP server-ID expansion:** If `tid` is a **bare server ID** (e.g. `"filesystem"`), it is expanded into `mcp__filesystem__{tool}` for each tool in `get_cached_tools(tid)`.
- Uses `mcp_session_pool.get_cached_tools(tid)` (sync) to avoid blocking.
- Cache-cold servers are skipped silently (documented in §13).

**`_mcp_tool_executor` — closure captured by `WebToolAdapter`:**

```python
async def _mcp_tool_executor(**kwargs):
    fresh_manifest = extension_registry.get_effective_server(mcp_server_id)
    if not fresh_manifest: raise RuntimeError("no longer registered")
    if fresh_manifest["status"] != "active": raise RuntimeError("not active")
    client = await mcp_session_pool.get_or_create_client(
        server_id=mcp_server_id,
        manifest=fresh_manifest,
        workspace_root=str(ws_path),
    )
    return await client.call_tool(tool_name=mcp_remote_name, arguments=kwargs)
```

**Why re-fetch manifest:** Detects deletion / toggle / config change between registration and invocation.

---

### 3.8 `omweb/adapters/core_adapter_parts/web_tool_adapter.py`

**Purpose:** Wraps any executor callable as a Core `BaseTool`.

**Critical schema handling — `_should_disable_args_schema(parameters)`:**

Returns `True` (→ `args_schema = None`) when the schema contains:
- Top-level `$ref`, `$defs`, `definitions`, `oneOf`, `anyOf`, `allOf`, `not`, `if`/`then`/`else`
- Any property with those keywords
- `array` whose `items` is `array` or `object` (with `properties`)
- `object` with declared `properties`

When disabled, Core uses `self.parameters` verbatim (from `to_openai_tool()` / `to_openai_schema()` / `to_param()`).

**Why:** Pydantic's `create_model` silently drops these constructs (`array → list`, `object → dict`, `$ref → gone`). Disabling `args_schema` preserves the original schema for the LLM.

**Methods:**

- `to_openai_tool()` — returns OpenAI function schema
- `to_openai_schema()` — alias (Core-compatible)
- `to_param()` — alias
- `get_schema()` — returns `_parameters_schema`

---

### 3.9 `omweb/agents/registry.py` — MCP State Per Agent

**Purpose:** Merges manifest MCP config with user overrides.

**`list_agents()` — the hot path** (`GET /api/store/agents`):

```
1. Load built-in manifests
2. Load custom agent manifests (skip agents_state.json + agent_settings.json)
3. In ONE pass:
     _merge_mcp_state_for_agent(agent, saved_settings[agent.id])
       → computes effective_mcp_servers
       → strips historical "mcp" pseudo-tool from `tools`
```

**`get_agent_mcp_state(agent_id)` — the store modal source:**

Returns:

```json
{
  "manifest_mcp_servers":    ["..."],   // from manifest
  "disabled_mcp_servers":    ["..."],   // from agent_settings.json
  "extra_mcp_servers":       ["..."],   // from agent_settings.json
  "effective_mcp_servers":   ["..."]    // = (manifest - disabled) ∪ extra
}
```

**`save_agent_mcp_state(agent_id, disabled, extra)`:**

Writes to `agent_settings.json` atomically (`.tmp` + `os.replace`).

---

### 3.10 `omweb/engines/peldrun_engine.py`

**Purpose:** Orchestrates a single agent run — discovers MCP tools and materialises them.

**Discovery phase (concurrent):**

```python
async def _probe_one(srv):
    try:
        tools = await mcp_session_pool.get_tools(
            server_id=srv["id"], manifest=srv, workspace_root=ws_path_str,
        )
        return srv, list(tools or [])
    except Exception:
        return srv, None

probe_results = await asyncio.gather(
    *[_probe_one(srv) for srv in target_mcp_servers],
)
```

**Materialisation phase:**

For each discovered tool, build `mcp__{server_id}__{tool_name}` and call `resolve_tool_runtime`. **Only add to `requested_tools` after adapter construction succeeds.**

**Delegation awareness:** `_collect_specialist_tool_allowlists` **passes through** any ID starting with `mcp__` / `mcp::` and **expands** the specialist's `effective_mcp_servers` via `get_cached_tools`.

---

## 4. Data Model & Storage

### 4.1 Storage Layout

```
backend/storage/
└── store/
    ├── extensions/
    │   ├── mcp_github.json              # manifest
    │   ├── mcp_filesystem.json          # manifest
    │   ├── settings/
    │   │   ├── mcp_github.json          # {settings, env}
    │   │   └── mcp_filesystem.json
    │   └── bundles/
    │       └── my_bundle/
    │           ├── manifest.json
    │           ├── node_modules/        (after npm install)
    │           └── requirements.txt     (installed via pip)
    └── agents/
        ├── peldrun.json                  # builtin? no — from omweb/agents/builtins/
        ├── custom_taman.json             # custom agent
        ├── agents_state.json             # {agent_id: "active"|"disabled"}
        └── agent_settings.json           # {agent_id: {disabled_mcp_servers, extra_mcp_servers, ...}}
```

### 4.2 Manifest Shape

```json
{
  "id": "mcp_github",
  "name": "MCP GitHub Integration",
  "type": "mcp_server",
  "transport": "stdio",
  "command": "npx -y @modelcontextprotocol/server-github",
  "status": "active",
  "description": "...",
  "env_schema": {
    "type": "object",
    "properties": {
      "GITHUB_TOKEN": {
        "type": "string",
        "title": "GitHub Personal Access Token",
        "secret": true
      }
    },
    "required": ["GITHUB_TOKEN"]
  },
  "settings_schema": {
    "type": "object",
    "properties": {
      "default_branch": { "type": "string", "default": "main" }
    }
  }
}
```

**Supported transports:**

| `transport` | Config keys | Notes |
|---|---|---|
| `stdio` | `command`, `timeout` | Subprocess-based; `cwd = workspace_root` |
| `sse` | `url`, `timeout` | Server-Sent Events; requires `SSETransport` |
| `http` | `url`, `timeout` | Direct JSON-RPC over HTTP |

### 4.3 Per-Agent Override File

`backend/storage/store/agents/agent_settings.json`:

```json
{
  "peldrun": {
    "disabled_mcp_servers": [],
    "extra_mcp_servers": ["mcp_filesystem", "mcp_github"]
  },
  "code_architect": {
    "extra_mcp_servers": ["mcp_filesystem"],
    "disabled_tools": [],
    "extra_tools": []
  }
}
```

**Resolution rule** (identical to `get_agent_mcp_state`):

```
effective = (manifest.mcp_servers - disabled) ∪ extra
```

---

## 5. Runtime Flow

### 5.1 Discovery (per run)

Triggered inside `PeldrunEngine.run()`:

```
1. effective_mcp_servers = manifest.effective_mcp_servers or manifest.mcp_servers
2. Filter to servers with status == "active"
3. asyncio.gather([get_tools(srv) for srv in active_servers])
   ↓
   For each server:
     a. Check _tool_cache[server_id] (TTL 5min)
     b. If cached → return immediately
     c. If miss → get_or_create_client → _send_jsonrpc_request("tools/list")
     d. Cache result
4. For each discovered tool → resolve_tool_runtime → WebToolAdapter
5. Append to adapted_core_tools + requested_tools
```

### 5.2 Per-Agent Binding

1. **Manifest:** `{"mcp_servers": ["mcp_github"]}`
2. **Or user override:** `agent_settings.json → extra_mcp_servers`
3. **Merged at `list_agents()` → `effective_mcp_servers`**

### 5.3 Tool Materialization

```
Remote tool name: "create_or_update_file"
Server ID:        "mcp_github"
Canonical ID:     "mcp__mcp_github__create_or_update_file"
```

The double underscore is used (not `::`) because most LLM providers reject `::` in function names.

### 5.4 Invocation

```
LLM emits tool_call(name="mcp__mcp_github__create_or_update_file", args={...})
    ↓
ToolCallAgent resolves adapter (WebToolAdapter wrapping _mcp_tool_executor)
    ↓
_mcp_tool_executor(**kwargs):
    1. fresh_manifest = extension_registry.get_effective_server("mcp_github")
    2. if not fresh_manifest → RuntimeError
    3. if fresh_manifest["status"] != "active" → RuntimeError
    4. client = await mcp_session_pool.get_or_create_client(...)
    5. return await client.call_tool("create_or_update_file", kwargs)
    ↓
MCPClient.call_tool:
    _send_jsonrpc_request("tools/call", {"name": "create_or_update_file", "arguments": kwargs})
    ↓
StdioTransport: writes JSON line to subprocess stdin, reads JSON line from stdout
    ↓
Returns ToolResult(output=..., is_error=..., ...)
```

### 5.5 Multi-Agent Delegation

When the parent agent invokes `delegate_task`:

1. `MultiAgentCoordinator._resolve_authorized_child_tool_ids` reads `specialist_tools_map[child_agent_id]`.
2. This map is populated in `PeldrunEngine._collect_specialist_tool_allowlists`:
   - Iterates `agent_registry.list_agents()`
   - For each specialist, reads `get_agent_tools_state()` + `effective_mcp_servers`
   - **Expands** each `mcp_servers` entry via `mcp_session_pool.get_cached_tools()` → `mcp__{server}__{tool}`
3. Coordinator passes the resolved list to `resolve_authorized_tools`, which:
   - Handles explicit `mcp__server__tool` IDs
   - Expands bare server IDs (`"filesystem"`) via `get_cached_tools`
   - Skips inactive servers

**Cold-cache caveat:** If the parent has not discovered tools for a server before delegating, the child receives nothing for that server. To mitigate: the parent's own `run()` discovers first, warming the cache. Delegation therefore sees a warm cache in normal operation.

---

## 6. Cache Architecture

### 6.1 Layers

| Layer | Scope | TTL | Invalidation |
|---|---|---|---|
| **`_tool_cache[server_id]`** | Process-wide | 300s | `invalidate_cache` / TTL |
| **`_clients[key]`** | Per `(server_id, workspace_root)` | Until close | `close_session` / `close_server_sessions` |
| **Registry `_builtin_tools`** | Process-wide | Permanent | None (static manifests) |
| **Frontend `AgentMcpModal`** | Per modal open | Until close | Modal `useEffect` |
| **Frontend `ActiveToolsModal`** | Per modal open | Until close | Modal `useEffect` |

### 6.2 Cache Invalidation Channels

| Trigger | Action |
|---|---|
| `POST /api/mcp/servers/{id}/toggle` | `invalidate_cache_sync` + `close_server_sessions` |
| `PUT /api/mcp/servers/{id}/settings` | `close_server_sessions` |
| `POST /api/store/extensions/upload` (existing ID) | `invalidate_cache_sync` (in `save_extension`) + `close_server_sessions` |
| `DELETE /api/store/extensions/{id}` | `close_server_sessions` |
| `install_bundle` (re-upload) | `invalidate_cache_sync` |
| `delete_bundle` | `close_server_sessions` |
| Manual refresh (`ActiveToolsModal`) | Client-side re-fetch (server cache still respected) |

---

## 7. Workspace Isolation

**Rule:** Every stdio session is bound to a specific `workspace_root`.

**Consequences:**

- Two chats → two sessions → two subprocesses per server
- No cross-chat file leakage
- `StdioTransport.cwd` = workspace root (enforced in `_build_command_args`)

**Key derivation:**

```python
def _get_session_key(server_id, workspace_root):
    normalized = str(Path(workspace_root).resolve()) if workspace_root else "default"
    return f"{server_id}:{normalized}"
```

**Implication for `close_session(server_id)` without `workspace_root`:** Only closes the `"server_id:default"` key. To close all bindings for a server, use `close_server_sessions(server_id)` — which is what routers and bundle manager use.

---

## 8. Session Lifecycle & Shutdown

### 8.1 Normal Lifecycle

```
First run() for agent with MCP binding
    → get_or_create_client → StdioTransport.aconnect → subprocess spawned
    → cached under key "{server_id}:{workspace}"

Subsequent runs (same workspace)
    → cached client returned

Different workspace
    → new session created under different key

close_session / close_server_sessions
    → _closing marks key, client popped, subprocess terminated

TTL expiry (tool cache)
    → next get_tools() re-probes; subprocess stays alive
```

### 8.2 Application Shutdown

`omweb/main.py::lifespan`:

```python
yield

# 1. Terminate MCP subprocesses FIRST (before other subsystems)
try:
    from omweb.extensions.mcp_pool import mcp_session_pool
    await asyncio.wait_for(mcp_session_pool.close_all(), timeout=30.0)
except asyncio.TimeoutError:
    logger.warning("McpSessionPool.close_all() exceeded 30s")
except Exception as exc:
    logger.warning("Failed to close McpSessionPool: %s", exc)

# 2. Shutdown ResourceMonitorService
# 3. Close BrowserSessionManager
```

**Why MCP first:** On Windows, lingering Node/Python subprocesses hold file handles on the backend working directory, blocking `uvicorn` from releasing the event loop cleanly.

**Timeout behavior:** If a subprocess ignores SIGTERM and refuses to exit within 5s, `StdioTransport.aclose` sends SIGKILL. If the pool's `close_all` exceeds 30s total, remaining subprocesses are reaped by the OS on process exit.

---

## 9. REST API Reference

### 9.1 Server Lifecycle (`/api/mcp`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/mcp` | All servers + active tools (uses cache) |
| `GET` | `/api/mcp/servers` | Server list |
| `GET` | `/api/mcp/servers/{id}` | Server detail (effective manifest) |
| `POST` | `/api/mcp/servers/{id}/toggle` | Toggle status; closes sessions |
| `DELETE` | `/api/mcp/servers/{id}` | Delete server (or purge bundle) |

### 9.2 Settings (`/api/mcp/servers/{id}`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/mcp/servers/{id}/settings` | Schemas + current values |
| `PUT` | `/api/mcp/servers/{id}/settings` | Validate + save; closes sessions |
| `GET` | `/api/mcp/servers/{id}/tools` | Discover tools (cached) |

### 9.3 Connectivity (`/api/mcp`)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/mcp/test` | One-shot strict probe; no caching |

### 9.4 Bundles (`/api/mcp/bundles`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/mcp/bundles` | List installed bundles |
| `POST` | `/api/mcp/bundles/upload` | Upload `.zip`, extract, install deps |
| `DELETE` | `/api/mcp/bundles/{id}` | Purge bundle + close sessions |

### 9.5 Store Integration (`/api/store`)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/store/extensions/upload` | Upload `.json` manifest or `.zip` bundle |
| `DELETE` | `/api/store/extensions/{id}` | Delete extension or bundle |
| `GET` | `/api/store/agents/{id}/mcp` | Per-agent MCP state |
| `PUT` | `/api/store/agents/{id}/mcp` | Save `disabled_mcp_servers` + `extra_mcp_servers` |

---

## 10. Frontend Integration

### 10.1 Components

| Component | Purpose |
|---|---|
| `AgentMcpModal` | Per-agent MCP binding editor |
| `AgentCard` | Displays `effective_mcp_servers.length` as `N MCP` badge |
| `AgentBuilderModal` | MCP server selection during agent creation |
| `MCPSettingsModal` | Per-server settings + env + test connection |
| `ExtensionCard` | MCP server card in `/stores → Extensions` |
| `ActiveToolsModal` | Chat-side viewer with per-server accordions |
| `ToolCallCard` | Tool call display in chat (with MCP disabled state) |

### 10.2 Data Flow

```
/stores → Agents section
    GET /api/store/agents
        → each agent includes effective_mcp_servers
        → AgentCard renders badge

/stores → Agent → Server button
    AgentMcpModal opens
    GET /api/store/agents/{id}/mcp
        → renders manifest/extra/effective lists
    User selects servers → PUT /api/store/agents/{id}/mcp
    onSaved → reload() → AgentCard refreshes

/stores → Extensions → Configure
    MCPSettingsModal opens
    GET /api/mcp/servers/{id}/settings
        → renders schema
    PUT /api/mcp/servers/{id}/settings
    POST /api/mcp/test (optional)

/chat → ActiveToolsModal (Wrench button)
    GET /api/store/agents/{id}/tools
    GET /api/store/agents/{id}/mcp
    GET /api/mcp
    For each effective server: GET /api/mcp/servers/{id}/tools
    Renders built-in tools + MCP accordions
```

### 10.3 UI States

**`AgentCard` badge:**
```
const effectiveMcpServers = agent.effective_mcp_servers ?? agent.mcp_servers ?? [];
const mcpServersCount = effectiveMcpServers.length;
```

**`ActiveToolsModal` filter counts:**
- `All (N)` — `builtinTools.length + Σ mcpGroups.tools.length`
- `Built-in (N)` — `builtinTools.length`
- `MCP (N)` — `Σ mcpGroups.tools.length`

**Per-server accordion state:** `loaded`, `loading`, `error`, `inactive`, `empty`.

---

## 11. Bundle Lifecycle & Dependency Installation

### 11.1 Accepted Formats

- Single `.json` manifest (advanced users; server must be installed manually)
- `.zip` bundle (recommended; self-contained)

### 11.2 Required Bundle Layout

```
my_bundle.zip
├── manifest.json          (required, at root or inside single subfolder)
├── requirements.txt       (python only)
├── package.json           (node only)
├── server.py / server.js  (entry point)
└── ...                    (any other files)
```

`manifest.json` fields (minimum):

```json
{
  "id": "my_server",
  "name": "My MCP Server",
  "type": "mcp_server",
  "transport": "stdio",
  "command": "python server.py",
  "entrypoint": "server.py",
  "runtime": "python"
}
```

### 11.3 Installation Flow

```
1. Upload → POST /api/mcp/bundles/upload (multipart)
2. Validate zip (zip-slip, size limits, max files)
3. Extract to storage/store/extensions/bundles/{id}/
4. Read manifest.json
5. Detect runtime
6. Install dependencies:
     python → pip install -r requirements.txt
     node   → npm install --omit=dev
     neither → skip
7. Register manifest via extension_registry.save_extension
8. invalidate_cache_sync(bundle_id)
9. Return install_report
```

### 11.4 Failure Handling

**On dependency failure:**
- Delete the entire `{id}/` folder
- Raise `McpBundleError(code="DEPENDENCY_INSTALL_FAILED")`
- `stderr[-1500:]` included in error
- HTTP 400 with structured detail

**On manifest failure:**
- Same rollback
- `code="MISSING_MANIFEST"`, `"INVALID_MANIFEST_JSON"`, `"MISSING_BUNDLE_ID"`

**On runtime mismatch:**
- `python` declared but `python` executable not found → clear error
- `node` declared but `npm` not on PATH → clear error

### 11.5 Deletion

```
DELETE /api/mcp/bundles/{id}
    → await mcp_session_pool.close_server_sessions(id)
    → shutil.rmtree(bundles/{id})
    → settings file removed
    → extension manifest removed
```

---

## 12. Tool Naming Convention

### 12.1 Canonical Format

```
mcp__{server_id}__{remote_tool_name}
```

**Example:** `mcp__mcp_github__create_or_update_file`

**Alternative (parser accepted, not preferred):** `mcp::{server_id}::{tool_name}`

### 12.2 Why Double Underscore?

Most LLM providers (OpenAI, Anthropic, Google) **reject `::`** in function names for OpenAI-compatible tool schemas. `__` is universally accepted.

### 12.3 Parsing

```python
def _parse_mcp_tool_id(tool_id):
    if tool_id.startswith("mcp__"):
        _, server, tool = tool_id.split("__", 2)
        return server, tool
    if tool_id.startswith("mcp::"):
        _, server, tool = tool_id.split("::", 2)
        return server, tool
    return None
```

**Note:** `{remote_tool_name}` may itself contain underscores — that's why we `split("__", 2)`, not `.split("__")`.

### 12.4 Naming Conflicts

If a server's ID already starts with `mcp_` (like `mcp_github`), the resulting tool ID looks doubled: `mcp__mcp_github__...`. **This is not a bug** — it's the canonical format. To avoid it, choose server IDs that don't start with `mcp_` (e.g. `github` instead of `mcp_github`).

**Renaming an existing server:**

1. Edit `backend/storage/store/extensions/{id}.json` → change `"id"`
2. Delete old settings file `backend/storage/store/extensions/settings/{old_id}.json`
3. Create new settings file `{new_id}.json` (or let user re-enter via UI)
4. Rebind via `AgentMcpModal` for each affected agent
5. Restart backend (or wait for cache TTL)

---

## 13. Error Handling & Recovery

### 13.1 Failure Modes

| Failure | Detection | Response |
|---|---|---|
| Server subprocess crashes | `proc.returncode is not None` | Auto-reinit on next `get_or_create_client` |
| `tools/list` fails | `_send_jsonrpc_request` raises | `probe_connection` → `status:"error"` |
| Server not registered | `get_effective_server` returns `None` | `_mcp_tool_executor` raises `RuntimeError` |
| Server inactive | `manifest["status"] != "active"` | Skipped in discovery, raises in invocation |
| Executable not found | `shutil.which` / `Path.is_file` fails | `ValueError` at transport build time |
| Schema too complex | `_should_disable_args_schema` returns `True` | `args_schema = None`; original schema preserved |
| Delegation cache cold | `get_cached_tools` returns `None` | Skipped silently (logged at DEBUG) |
| Dependency install fails | Non-zero exit + timeout | Full rollback + `McpBundleError` |

### 13.2 Silent Swallow Points (documented behavior)

These are **intentional** — they prevent cascading failures:

| Point | Behavior | Rationale |
|---|---|---|
| `MCPClient.discover_tools` | Returns `[]` on error | Backward-compatible; use `_send_jsonrpc_request` directly for strict mode |
| `peldrun_engine._probe_one` | Returns `(srv, None)` on failure | One failing server must not break the run |
| `ActiveToolsModal.loadMcpTools` | Per-server errors → empty list | Other servers still display |
| `resolve_authorized_tools` cache-cold | Logs DEBUG, skips | Best-effort contract |

### 13.3 Recovery Procedures

**Server appears in Store but tools never load:**
1. `Invoke-RestMethod http://127.0.0.1:8088/api/mcp/servers/{id}/tools`
2. If 400/500 → check server logs; likely dependency missing
3. `POST /api/mcp/test` with `{"server_id": "..."}` for detailed error

**Tools load but agent can't call them:**
1. Check `AgentMcpModal` → is the server in `Effective MCP Servers`?
2. Check backend logs for `_mcp_tool_executor` errors
3. Verify server `status == "active"`

**Subprocess accumulates (leak):**
1. `Get-Process node,python` before/after Ctrl+C
2. If leak persists: `close_server_sessions` may have been bypassed — check `_closing` set logic

**Cache serves stale tools:**
1. `POST /api/mcp/servers/{id}/toggle` (twice) — forces `invalidate_cache_sync`
2. Or restart backend (in-memory cache lost)

---

## 14. Security Model

### 14.1 Threat Model

**In scope:**

- Malicious MCP server attempting filesystem escape
- Malicious bundle (zip-slip, arbitrary code execution at install time)
- Cross-tenant data leakage between chats
- Credential exposure via logs / API responses

**Out of scope (documented limitations):**

- Malicious actor with local filesystem access (PELDRUN is not a sandbox)
- Network-level MITM (use TLS for `sse`/`http` transports)

### 14.2 Mitigations

| Threat | Mitigation |
|---|---|
| Zip-slip (path traversal) | `_safe_extract` rejects absolute paths and `..` members |
| Filesystem escape via `cwd` | `StdioTransport.cwd = workspace_root`; `@modelcontextprotocol/server-filesystem` restricted to `allowed_directories` |
| Bundles running arbitrary code at install | pip/npm installs run in bundle dir; no sandboxing (documented limitation) |
| Cross-chat leakage | Session key includes `workspace_root`; subprocess `cwd` isolated |
| Credential leakage | `env` not returned in `GET /api/mcp/servers/{id}`; only schemas returned |
| HTTPS enforcement | Depends on `sse` / `http` server; `auth_token` passed in `Authorization: Bearer` |
| Cache poisoning | `invalidate_cache` called on all config-changing endpoints |

### 14.3 Known Limitations

**Plaintext credentials:** `settings/{server_id}.json` stores `env` values unencrypted. This matches most MCP implementations today. For production:
- Use **Application Passwords** (WordPress), **fine-grained tokens** (GitHub), **short-lived credentials** (AWS STS)
- Ensure `backend/storage/` is protected by filesystem permissions
- Do not expose `backend/storage/` via static file serving (it isn't — only `/storage` for deliverables)

**No process isolation:** Bundles run with the backend's UID. Malicious bundles can read `backend/storage/`. Future work: per-bundle UID or container.

**No schema validation on `tools/call` responses:** A malicious server can return `ToolResult` with any `output`. The LLM may be confused (prompt injection). Mitigation: server trust model (only install servers from trusted sources).

---

## 15. 🔮 Adding New MCP Services (Future)

**This is the primary extension point.** PELDRUN is designed so that adding a new service (WordPress, Slack, Notion, PostgreSQL, AWS, etc.) requires **zero backend code changes**.

### 15.1 Decision Tree

```
Do you want to add a new service (e.g. WordPress)?
│
├─ Is there an existing MCP server for it?
│  │
│  ├─ Yes → Is it on npm?
│  │  └─ Yes → Use Method A (manifest JSON, npx command)
│  │
│  ├─ Yes → Is it a GitHub repo with requirements.txt / package.json?
│  │  └─ Yes → Use Method B (zip bundle)
│  │
│  └─ No → Use Method C (write a small wrapper)
│
└─ See §15.5 for a directory of known MCP servers
```

### 15.2 Method A — Manifest JSON with `npx`

**When:** An official or community MCP server exists on npm.

**Steps:**

1. Create `wordpress.json`:

```json
{
  "id": "wordpress",
  "name": "WordPress Integration",
  "type": "mcp_server",
  "transport": "stdio",
  "command": "npx -y @modelcontextprotocol/server-wordpress",
  "timeout": 30.0,
  "description": "Manage WordPress posts, pages, media, users via WP REST API.",
  "env_schema": {
    "type": "object",
    "properties": {
      "WP_URL": {
        "type": "string",
        "title": "WordPress Site URL",
        "description": "Base URL of the WordPress site (e.g. https://example.com)",
        "default": ""
      },
      "WP_USERNAME": {
        "type": "string",
        "title": "WordPress Username"
      },
      "WP_APP_PASSWORD": {
        "type": "string",
        "title": "Application Password",
        "description": "Generate from WP Admin → Users → Application Passwords",
        "secret": true
      }
    },
    "required": ["WP_URL", "WP_USERNAME", "WP_APP_PASSWORD"]
  },
  "settings_schema": {
    "type": "object",
    "properties": {
      "default_status": {
        "type": "string",
        "title": "Default post status",
        "enum": ["draft", "publish", "private"],
        "default": "draft"
      }
    }
  }
}
```

2. Upload via UI (`/stores → Extensions → Upload MCP`) or `POST /api/store/extensions/upload`.

3. Configure via `MCPSettingsModal`:
   - Enter `WP_URL`, `WP_USERNAME`, `WP_APP_PASSWORD`
   - Test connection (`POST /api/mcp/test`)

4. Bind to agents via `AgentMcpModal`.

**Time:** 5 minutes.

### 15.3 Method B — Zip Bundle

**When:** An MCP server exists as a GitHub repo, but not on npm.

**Steps:**

1. Clone the repo.

2. Ensure it has a `manifest.json` at root:

```json
{
  "id": "custom_service",
  "name": "Custom Service MCP",
  "type": "mcp_server",
  "transport": "stdio",
  "entrypoint": "dist/server.js",
  "runtime": "node",
  "command": "node dist/server.js"
}
```

Or:

```json
{
  "id": "custom_python_service",
  "name": "Custom Python Service",
  "type": "mcp_server",
  "transport": "stdio",
  "entrypoint": "server.py",
  "runtime": "python",
  "command": "python server.py"
}
```

3. Zip the **entire** repo (minus `.git/`, `node_modules/`, `__pycache__/`).

4. `POST /api/mcp/bundles/upload` with the zip.

5. `_detect_runtime` + `_install_python_dependencies` / `_install_node_dependencies` run automatically.

6. Configure via `MCPSettingsModal` if the server has `env_schema`.

**Time:** 10 minutes (if deps install cleanly).

### 15.4 Method C — Wrapper from Scratch

**When:** No existing MCP server, but the service has a documented REST API.

**Example: minimal WordPress MCP wrapper (Node)**

```javascript
// server.js
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

const WP_URL = process.env.WP_URL;
const AUTH = Buffer.from(
  `${process.env.WP_USERNAME}:${process.env.WP_APP_PASSWORD}`
).toString("base64");

const server = new Server(
  { name: "wordpress", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler("tools/list", async () => ({
  tools: [
    {
      name: "list_posts",
      description: "List WordPress posts",
      inputSchema: {
        type: "object",
        properties: {
          per_page: { type: "number", default: 10 },
          status: { type: "string", enum: ["draft", "publish", "private"] },
        },
      },
    },
    {
      name: "create_post",
      description: "Create a WordPress post",
      inputSchema: {
        type: "object",
        properties: {
          title: { type: "string" },
          content: { type: "string" },
          status: { type: "string", default: "draft" },
        },
        required: ["title", "content"],
      },
    },
    // ... more tools
  ],
}));

server.setRequestHandler("tools/call", async (req) => {
  const { name, arguments: args } = req.params;
  if (name === "list_posts") {
    const res = await fetch(
      `${WP_URL}/wp-json/wp/v2/posts?per_page=${args.per_page || 10}`,
      { headers: { Authorization: `Basic ${AUTH}` } }
    );
    const posts = await res.json();
    return {
      content: [{ type: "text", text: JSON.stringify(posts, null, 2) }],
    };
  }
  if (name === "create_post") {
    const res = await fetch(`${WP_URL}/wp-json/wp/v2/posts`, {
      method: "POST",
      headers: { Authorization: `Basic ${AUTH}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        title: args.title,
        content: args.content,
        status: args.status || "draft",
      }),
    });
    const post = await res.json();
    return {
      content: [{ type: "text", text: JSON.stringify(post, null, 2) }],
    };
  }
  throw new Error(`Unknown tool: ${name}`);
});

const transport = new StdioServerTransport();
await server.connect(transport);
```

**Bundle layout:**

```
wordpress-mcp.zip
├── manifest.json          (as in Method B, runtime: node)
├── package.json
├── server.js
└── (no node_modules — installed automatically)
```

**Time:** 2-4 hours (depends on API surface).

### 15.5 Known MCP Servers Directory

**Official (modelcontextprotocol):**
- GitHub, Slack, Google Drive, PostgreSQL, SQLite, MySQL, Puppeteer, Filesystem, Fetch, Git, Memory, Time, etc.
- Reference: https://github.com/modelcontextprotocol/servers

**Community (curated):**
- Reference: https://github.com/punkpeye/awesome-mcp-servers

**By category:**

| Category | Services |
|---|---|
| **Cloud / IaaS** | AWS, GCP, Azure, Cloudflare |
| **Databases** | PostgreSQL, MySQL, SQLite, MongoDB, Redis, Pinecone |
| **Communication** | Slack, Discord, Telegram, Email |
| **Productivity** | Notion, Google Workspace, Airtable, Jira |
| **CMS** | WordPress (community), Contentful, Sanity |
| **DevOps** | GitHub, GitLab, Jira, Docker, Kubernetes |
| **Analytics** | Google Analytics, PostHog |
| **Payment** | Stripe, PayPal |
| **Storage** | S3, Google Drive, Dropbox |
| **Search** | Brave Search, Exa, Tavily |

### 15.6 Best Practices When Adding a Service

**1. Naming:**
- `id`: lowercase, short, no `mcp_` prefix (e.g. `github`, `wordpress`, `slack`)
- `name`: human-readable (e.g. `"MCP GitHub Integration"`)

**2. Env vars:**
- Always provide `env_schema` for authentication
- Use `"secret": true` for tokens/passwords
- Provide `"default"` where sensible
- Add `"description"` explaining where to find the value

**3. Settings:**
- Use `settings_schema` only for non-secret configuration
- Common examples: `default_branch`, `allowed_directories`, `timeout`

**4. Timeout:**
- Set `timeout: 30.0` for servers with tool discovery > 5s
- Set lower for fast servers (`15.0`)

**5. Per-agent binding (avoid tool bloat):**
- **Do NOT** bind 5 servers to one agent
- Each server adds 10-40 tools
- Recommended: 1-2 servers per agent, ≤40 tools total
- Pattern:
  - `code_architect` → `github` only
  - `peldrun` → `filesystem` + `slack`
  - `data_scientist` → `postgres` only

**6. Test in isolation:**
- `POST /api/mcp/test` → check `status: "ok"`
- `GET /api/mcp/servers/{id}/tools` → verify tools list
- Bind to a throwaway agent → test one tool call

**7. Documentation:**
- Add a note in `docs/MCP_SERVICES.md` (recommended practice) describing:
  - Server ID
  - Auth method
  - Primary use cases
  - Known limitations

### 15.7 Anticipated Services for PELDRUN

Priority roadmap (informational, not committed):

| Priority | Service | Reason |
|---|---|---|
| **P0** | Slack | Team communication; commonly requested |
| **P0** | Notion | Documentation and knowledge management |
| **P1** | PostgreSQL | Data science; SQL queries |
| **P1** | Google Drive | File access beyond local workspace |
| **P2** | AWS S3 | Cloud storage |
| **P2** | Stripe | Payment operations for code agents |
| **P3** | WordPress | CMS automation |
| **P3** | Jira | Project management |

Each follows the same pipeline; no code changes required.

---

## 16. Testing Guide

### 16.1 Unit-Level Checks

```powershell
# 1. Verify pool methods exist
python -c "
from omweb.extensions.mcp_pool import mcp_session_pool
for name in ['get_tools', 'get_cached_tools', 'close_server_sessions',
             'invalidate_cache', 'invalidate_cache_sync']:
    assert hasattr(mcp_session_pool, name), f'Missing: {name}'
print('All pool methods present')
"

# 2. Verify schema heuristic
python -c "
from omweb.adapters.core_adapter_parts.web_tool_adapter import _should_disable_args_schema
cases = {
    'simple': {'type': 'object', 'properties': {'q': {'type': 'string'}}},
    'ref': {'type': 'object', 'properties': {'x': {'\$ref': '#/X'}}},
}
assert _should_disable_args_schema(cases['simple']) is False
assert _should_disable_args_schema(cases['ref']) is True
print('Schema heuristic OK')
"

# 3. Verify agent registry merges effective_mcp_servers
python -c "
from omweb.agents.registry import agent_registry
agents = agent_registry.list_agents()
for a in agents:
    assert 'effective_mcp_servers' in a, f'Missing in {a[\"id\"]}'
print(f'All {len(agents)} agents enriched')
"
```

### 16.2 API-Level Checks

```powershell
# 1. Server list
Invoke-RestMethod http://127.0.0.1:8088/api/mcp/servers | ConvertTo-Json -Depth 3

# 2. Tool discovery (uses cache)
Invoke-RestMethod http://127.0.0.1:8088/api/mcp/servers/mcp_github/tools

# 3. Per-agent MCP state
Invoke-RestMethod http://127.0.0.1:8088/api/store/agents/peldrun/mcp

# 4. Cache warm/cold timing
Measure-Command { Invoke-RestMethod http://127.0.0.1:8088/api/mcp } | Select-Object TotalSeconds
Start-Sleep -Seconds 1
Measure-Command { Invoke-RestMethod http://127.0.0.1:8088/api/mcp } | Select-Object TotalSeconds
# Expected: 1st ~0.3-2s, 2nd <0.1s
```

### 16.3 End-to-End Scenario

```
1. /stores → Extensions → Upload MCP → (zip or json)
2. Verify server appears in list
3. Click "Test Connection" → status: ok
4. /stores → Agents → Peldrun → Server button
5. Add server → Save
6. Verify AgentCard shows "1 MCP"
7. /chat → Wrench (ActiveToolsModal)
8. Verify MCP tab shows tools grouped by server
9. Send chat message: "What tools does GitHub expose?"
10. Verify agent responds with tool names
11. Send: "Search for repositories about kubernetes"
12. Verify tool call appears in chat with result
```

### 16.4 Shutdown Test

```powershell
# 1. Start backend
uvicorn omweb.main:app --port 8088

# 2. Trigger discovery (spawns subprocess)
Invoke-RestMethod http://127.0.0.1:8088/api/mcp/servers/mcp_github/tools

# 3. Count processes BEFORE shutdown
Get-Process node,python | Measure-Object

# 4. Ctrl+C uvicorn

# 5. Count processes AFTER shutdown (after 3s)
Start-Sleep -Seconds 3
Get-Process node,python | Measure-Object
# Expected: count returns to baseline (no leaked subprocess)
```

---

## 17. Known Limitations & Future Work

| # | Limitation | Impact | Proposed Fix |
|---|---|---|---|
| 1 | Env vars stored plaintext | Credentials at rest | Keyring / encrypted settings file |
| 2 | No per-bundle UID | Malicious bundle can read backend storage | Container per bundle |
| 3 | Tool bloat (5+ servers = 200+ tools) | LLM confusion, token cost | Server bundles (pre-defined sets) |
| 4 | Cold start ~1-3s | Slow first call per session | Lazy keep-alive / pre-warm |
| 5 | Delegation cold-cache | Child loses MCP tools if parent hasn't probed | Pre-warm on delegate_task |
| 6 | No schema version negotiation | Breaking MCP versions undetected | MCP spec version in manifest |
| 7 | Silent `discover_tools` failures | Opaque tool-missing errors | Strict flag on `MCPClient.discover_tools` |
| 8 | `_config_changed` misses `auth_token` change | Tool cache stale after token update | Include `auth_token` in diff |
| 9 | No SSE `reconnect` | Dropped HTTP/SSE connection = tool failure | Transport-level retry |
| 10 | Windows-specific path handling | Edge cases with UNC paths | Normalize UNC in `_build_command_args` |

---

## 18. Appendix: Example Manifests

### A. Filesystem Server (`@modelcontextprotocol/server-filesystem`)

```json
{
  "id": "filesystem",
  "name": "MCP Local Filesystem",
  "type": "mcp_server",
  "transport": "stdio",
  "command": "npx -y @modelcontextprotocol/server-filesystem",
  "timeout": 30.0,
  "description": "Exposes local filesystem directories safely to agents.",
  "settings_schema": {
    "type": "object",
    "properties": {
      "allowed_directories": {
        "type": "array",
        "title": "Allowed Directories",
        "description": "Absolute paths the server may access. Empty = workspace root only.",
        "items": { "type": "string" },
        "default": []
      }
    }
  }
}
```

### B. GitHub Server (`@modelcontextprotocol/server-github`)

```json
{
  "id": "github",
  "name": "MCP GitHub Integration",
  "type": "mcp_server",
  "transport": "stdio",
  "command": "npx -y @modelcontextprotocol/server-github",
  "timeout": 30.0,
  "description": "Repository inspection, issues, PRs, commits, code search.",
  "env_schema": {
    "type": "object",
    "properties": {
      "GITHUB_PERSONAL_ACCESS_TOKEN": {
        "type": "string",
        "title": "GitHub PAT",
        "description": "Fine-grained token with repo scope",
        "secret": true
      }
    },
    "required": ["GITHUB_PERSONAL_ACCESS_TOKEN"]
  }
}
```

### C. PostgreSQL Server (Python, via zip bundle)

`manifest.json`:

```json
{
  "id": "postgres",
  "name": "MCP PostgreSQL",
  "type": "mcp_server",
  "transport": "stdio",
  "runtime": "python",
  "entrypoint": "server.py",
  "command": "python server.py",
  "timeout": 30.0,
  "description": "Query PostgreSQL databases with parameterized statements.",
  "env_schema": {
    "type": "object",
    "properties": {
      "DATABASE_URL": {
        "type": "string",
        "title": "Database URL",
        "description": "postgresql://user:pass@host:5432/dbname",
        "secret": true
      }
    },
    "required": ["DATABASE_URL"]
  }
}
```

`requirements.txt`:

```
mcp>=1.0.0
psycopg[binary]>=3.1.0
```

Upload the folder as `postgres.zip`. `pip install -r requirements.txt` runs automatically.

---

## 19. Glossary

| Term | Definition |
|---|---|
| **MCP** | Model Context Protocol — an open standard for exposing tools to LLM hosts |
| **Server** | A process (or HTTP endpoint) implementing MCP; exposes tools via JSON-RPC |
| **Tool** | A function exposed by an MCP server (e.g. `create_or_update_file`) |
| **Transport** | Communication layer (stdio subprocess, SSE, or HTTP) |
| **Session** | A persistent connection bound to `(server_id, workspace_root)` |
| **Cache** | In-memory `{server_id: {tools, fetched_at}}` with 5min TTL |
| **Manifest** | JSON descriptor of a server (id, command, transport, schemas) |
| **Bundle** | A `.zip` containing manifest + code + dependency files |
| **Effective servers** | `(manifest.mcp_servers - disabled) ∪ extra` |
| **Materialisation** | Converting a discovered MCP tool into a `WebToolAdapter` |
| **Delegation** | Parent agent calls `delegate_task`; child runs with allowlist of tools |
| **Tool bloat** | Degradation of LLM accuracy when >50 tools are exposed |

---
