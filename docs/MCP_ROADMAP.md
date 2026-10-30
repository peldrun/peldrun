```markdown
# MCP Expansion Roadmap — PELDRUN

This document tracks the engineering roadmap for expanding PELDRUN's
Model Context Protocol (MCP) capabilities. It covers three major
phases (A, B, C) that bring PELDRUN into full compliance with the
MCP 2025-11-25 specification, plus a testing and documentation phase
(D).

**Status:** All phases (A, B, C, D) are **COMPLETE**.
**Spec Compliance:** MCP 2025-11-25 (Tools, Resources, Prompts, Auth).

---

## 0. Executive Summary

Prior to this roadmap, PELDRUN supported 4 MCP server types:
- GitHub (stdio + env vars)
- Filesystem (stdio + settings)
- WordPress (HTTP + Basic Auth)
- Notion (Streamable HTTP + OAuth 2.1)

Three critical gaps remained:
1. **Missing `outputSchema` and `structuredContent`** — modern servers
   returned machine-readable JSON that was silently discarded.
2. **Missing mTLS** — Enterprise servers (Banking, Healthcare,
   Government) could not be connected.
3. **Missing `resources` and `prompts`** — "documentation servers"
   showed "0 tools" and appeared broken to users.

All three gaps have been closed with full backward compatibility.

---

## Phase A: `outputSchema` + `structuredContent`
**Status:** ✅ COMPLETE

### A.1 Objective
Support MCP 2025-11-25 §7.2, which allows a tool to declare an
`outputSchema` and return a `structuredContent` payload alongside
the traditional human-readable `content` blocks.

### A.2 Implementation Details

**Backend:**
- `mcp_pool.get_tools` and `mcp_pool.probe_connection` now capture
  `outputSchema` from the server response and sanitize it via
  `_sanitize_tool_input_schema` (the sanitizer is schema-agnostic,
  so it correctly handles both input and output schemas).
- `mcp_client.call_tool` now reads `structuredContent` and
  serializes it via `json.dumps()` as the primary output. The
  human-readable `content` blocks are appended after the structured
  payload (separated by `\n\n`). Both forms are captured in
  `metadata` for programmatic access.

**Cache:**
- `CACHE_SCHEMA_VERSION` bumped from `2` → `3`.
- `merge_tools` preserves `output_schema` when present.
- Stale caches (version < 3) are automatically re-discovered on the
  next read (via `is_cache_fresh`).

**Frontend:**
- `MCPToolsModal` reads `output_schema` and displays it in a
  labeled section ("Input Schema" / "Output Schema") when the user
  expands the schema view.

### A.3 Backward Compatibility
- Servers without `outputSchema` → `output_schema` is simply absent
  from the cached tool dict. Zero behavior change.
- Servers without `structuredContent` → `call_tool` falls back to
  `content` blocks exactly as before.

### A.4 Files Modified
- `backend/omweb/extensions/mcp_pool.py`
- `backend/omweb/extensions/mcp_tool_cache.py`
- `backend/peldrun/tools/mcp_client.py`
- `frontend/src/app/stores/_modals/MCPToolsModal.tsx`

---

## Phase B: mTLS (Mutual TLS)
**Status:** ✅ COMPLETE

### B.1 Objective
Support Enterprise MCP servers that require client certificates
during the TLS handshake. Prior to this phase, such servers could
not be connected at all — every attempt would hang or fail with an
opaque SSL error.

### B.2 Implementation Details

**Backend — `MCPClient`:**
- `__init__` now accepts `client_cert` (single path or `(cert, key)`
  tuple) and `ca_bundle` (custom trust store).
- `_get_client` passes both to `httpx.AsyncClient(cert=..., verify=...)`.
  A malformed cert path is logged as a warning and skipped — the
  subsequent TLS handshake fails fast with `SSLError`, never hangs.

**Backend — `mcp_pool`:**
- `get_or_create_client` resolves the manifest's `mtls` block against
  the effective env (per-chat or global) at connect time. Paths are
  read from env vars, so a rotated cert takes effect after session
  close without editing the manifest.
- `probe_connection` mirrors the resolution, but **merges**
  `env_override` over global env rather than **replacing** it (the
  test-override semantics differ from production isolation).
- `build_transport` contract is **unchanged** (still returns
  3-tuple) — mTLS is resolved directly inside the two call sites
  to avoid breaking the existing contract.

**Manifest schema:**
- New optional `mtls` block mapping logical roles to env var names:
  ```json
  "mtls": {
    "cert_env": "MTLS_CLIENT_CERT",
    "key_env": "MTLS_CLIENT_KEY",
    "ca_env": "MTLS_CA_BUNDLE"
  }
  ```

### B.3 Backward Compatibility
- Servers without an `mtls` block → `client_cert` and `ca_bundle`
  are `None` → `MCPClient` behaves identically to before.
- **stdio servers ignore mTLS entirely.** The resolution is gated
  on `fallback_url` being non-empty (which only holds for HTTP/SSE).

### B.4 Reference Manifest
`extensions/builtins/test_mtls.json` — a template for Enterprise
server connections. Not functional out of the box (requires a real
mTLS endpoint), but demonstrates the exact shape.

### B.5 Files Modified
- `backend/peldrun/tools/mcp_client.py`
- `backend/omweb/extensions/mcp_pool.py`
- `backend/omweb/extensions/builtins/test_mtls.json` (new file)

---

## Phase C: `resources` + `prompts`
**Status:** ✅ COMPLETE

### C.1 Objective
Support MCP 2025-11-25 §5 and §6 — read-only resources and
parameterized prompt templates. Prior to this phase, a server
exposing 3 resources and 0 tools appeared as "0 tools" in the Store
UI, indistinguishable from a broken server.

### C.2 Implementation Details

**Backend — `MCPClient`:**
- Added four async methods:
  - `list_resources()` → `List[{uri, name, description, mimeType}]`
  - `read_resource(uri)` → `{contents: [...]}` or `None`
  - `list_prompts()` → `List[{name, description, arguments}]`
  - `get_prompt(name, arguments)` → `{description, messages}` or `None`
- All four are **fail-soft**: a server that doesn't support the
  primitive returns `[]` or `None`, never raises.

**Cache:**
- `CACHE_SCHEMA_VERSION` bumped from `3` → `4`.
- Added `get_resources()`, `get_prompts()`,
  `store_discovered_resources()`, `store_discovered_prompts()`.
- Resources and prompts share the cache container with tools (one
  disk read serves all three endpoints).

**`mcp_pool`:**
- Added `get_resources()` and `get_prompts()`, mirroring `get_tools`:
  kill-switch check → cache fast-path → live discovery → cache write.

**Router — `routers/mcp.py`:**
- Added three endpoints:
  - `GET /servers/{sid}/resources` — cache-first, live fallback
  - `GET /servers/{sid}/prompts` — same semantics
  - `POST /servers/{sid}/resources/read` — body `{uri}`; returns
    `{contents: [...]}` on success, **404** when the resource is
    unavailable (distinct from 502 transport errors).

**Frontend — `MCPDetailDrawer`:**
- Converted to three tabs: Tools / Resources / Prompts.
- All three are fetched in parallel via `Promise.all` on drawer open.
- Tab labels show counts: `Tools (12)`, `Resources (3)`, `Prompts (2)`.
- Empty states are explicit ("This server does not expose any
  resources") rather than ambiguous.

### C.3 Backward Compatibility
- Servers without resources/prompts → both endpoints return
  `{resources: [], prompts: []}`. UI shows the tabs but with the
  empty message.
- The Tools tab is selected by default — a tools-only server looks
  identical to before.

### C.4 Files Modified
- `backend/peldrun/tools/mcp_client.py`
- `backend/omweb/extensions/mcp_tool_cache.py`
- `backend/omweb/extensions/mcp_pool.py`
- `backend/omweb/routers/mcp.py`
- `frontend/src/app/stores/_components/MCPDetailDrawer.tsx`

---

## Phase C6: `mcp_read_resource` Tool (Optional / Deferred)
**Status:** ⏭️ DEFERRED

### C6.1 Why Deferred

Adding a synthetic MCP tool that lets the agent read resources was
evaluated and **explicitly deferred**. Rationale:

1. **No active use case.** None of the four servers currently
   integrated (GitHub, Filesystem, WordPress, Notion) expose
   resources — they expose tools only. A `mcp_read_resource` tool
   would be dead code.
2. **Design ambiguity.** A server may expose the same data as both
   a tool and a resource (e.g. Notion's `notion-fetch` vs a
   hypothetical `notion://page/...` resource). Giving the LLM two
   paths to the same data creates confusion and inflates tool
   definition tokens.
3. **Auth duplication.** The auth resolution logic in
   `resolve_tool_runtime._mcp_tool_executor` is ~120 lines. A
   resource reader would need to duplicate it (per-chat mode,
   OAuth extraction, ephemeral mode, SSE event dispatch). This is a
   maintenance liability for zero immediate benefit.

### C6.2 When to Revisit

Reconsider if:
- A documentation server (e.g. Read the Docs MCP) with resources is
  integrated.
- Users explicitly request LLM access to resources in a
  documentation-heavy workflow.
- A refactor of `_mcp_tool_executor` extracts the auth resolution
  into a reusable function, eliminating the duplication concern.

### C6.3 If Implemented (Design Sketch)

- Two synthetic tools per resource-capable server:
  - `mcp__{sid}__list_resources` → returns the URI list
  - `mcp__{sid}__read_resource(uri)` → returns contents
- Both resolved in `resolve_tool_runtime` and registered only when
  the server's cache carries a non-empty `resources` array.
- Auth resolution factored out of `_mcp_tool_executor` into a shared
  helper.

---

## Phase D: Testing & Documentation
**Status:** ✅ COMPLETE

### D.1 Documentation

| File | Purpose |
|---|---|
| `docs/MCP_MANIFEST_REFERENCE.md` | Full manifest schema, all auth strategies, all states, examples, testing guide, cache schema |
| `docs/MCP_AUTH.md` | Auth mechanism deep-dive: storage, encryption, OAuth 2.1, mTLS, per-chat isolation, threat model, endpoint reference |
| `docs/MCP_ROADMAP.md` | This file — what was done, what was deferred, and why |

### D.2 Test Matrix

| Test | Phase | Expected Result |
|---|---|---|
| GitHub tools discovery (no `outputSchema`) | A | `output_schema` absent, no regression |
| Server with `outputSchema` (synthetic) | A | `output_schema` cached, UI shows both schemas |
| `call_tool` on server with `structuredContent` | A | JSON prepended to text |
| Cache version bump 2 → 3 | A | Stale cache invalidated, re-discovered |
| GitHub/Notion/WordPress/Filesystem (no mTLS) | B | No regression |
| mTLS server with valid cert | B | Connection succeeds |
| mTLS server with invalid cert path | B | Fast `SSLError`, no hang |
| Cache version bump 3 → 4 | C | Stale cache invalidated, `resources`/`prompts` populated |
| `/resources` on tool-only server | C | `{resources: [], total: 0, source: "live"}`, no error |
| `/resources/read` on missing URI | C | HTTP 404 with clear message |
| `MCPDetailDrawer` on tool-only server | C | 3 tabs shown, Tools selected, Resources/Prompts empty |
| Backend restart | A/B/C | No startup errors, caches intact |

### D.3 Backward Compatibility Audit

Every phase verified:
- ✅ GitHub (stdio + env)
- ✅ Filesystem (stdio + settings)
- ✅ WordPress (HTTP + Basic Auth)
- ✅ Notion (HTTP + OAuth 2.1)

All four continue to work identically. No manifest changes required
for any existing server.

---

## Deferred / Future Work

| Item | Priority | Notes |
|---|---|---|
| `mcp_read_resource` tool | Low | See Phase C6 — pending use case |
| `prompts/get` UI (materialize a prompt) | Low | Currently only `prompts/list` is surfaced |
| Streaming `resources/read` (large files) | Low | Currently buffers full content |
| Per-tool rate limiting | Medium | Defense against runaway agents |
| Cross-server tool deduplication | Medium | Same tool exposed by multiple servers |
| Automatic OAuth token refresh | Medium | Currently requires manual reconnect near expiry |
| SSE resource subscription | Low | MCP 2025-11-25 supports it; not implemented |

---

## Version History

| Version | Date | Changes |
|---|---|---|
| 2.0 | 2026-10-30 | Completed Phases A, B, C, D. Deferred C6. |
| 1.0 | 2026-07-28 | Initial roadmap drafted (Phases A, B, C plan). |

---

## Related Documentation

- `docs/MCP_MANIFEST_REFERENCE.md` — manifest schema + examples
- `docs/MCP_AUTH.md` — authentication deep-dive
- MCP 2025-11-25 Spec: https://spec.modelcontextprotocol.io/specification/2025-11-25/
```
