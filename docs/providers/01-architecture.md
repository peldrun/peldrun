### `docs/providers/01-architecture.md`

```markdown
<!-- docs/providers/01-architecture.md -->
# Architecture

> **Prerequisites**: Read the [README](README.md) first for key concepts.
>
> **Reading time**: 15-20 minutes.
>
> **What you'll learn**: How the Provider Packages system is structured,
> which component is responsible for what, and how a request travels from
> the user clicking "Install" to the LLM receiving a completion request.

---

## Table of Contents

- [Overview](#overview)
- [High-level architecture](#high-level-architecture)
- [The seven core components](#the-seven-core-components)
- [The two execution paths](#the-two-execution-paths)
- [Data flow — end to end](#data-flow--end-to-end)
- [Storage layout](#storage-layout)
- [Plugin Host deep dive](#plugin-host-deep-dive)
- [Provider registry and connections](#provider-registry-and-connections)
- [Lifecycle state machines](#lifecycle-state-machines)
- [What is NOT this system's responsibility](#what-is-not-this-systems-responsibility)
- [Version history](#version-history)

---

## Overview

The Provider Packages system exists for **one purpose**: allow a new LLM
provider to be added to PELDRUN without changing any PELDRUN source code.

It sits at the intersection of four PELDRUN subsystems:

- **OMWeb** — the FastAPI backend that serves the UI
- **PELDRUN Core** — the agent runtime and provider registry
- **Storage** — the on-disk JSON files that hold all state
- **The Store UI** — the React frontend where users install and configure

The system adds:

- A **new storage location** (`<backend>/storage/store/providers/`)
- A **new registry section** in `models_vault.json` (`provider_connections`)
- A **new REST API** (`/api/store/providers/*`)
- A **new UI section** in the Store (`/stores` → Providers)
- A **new runtime host** (`plugin_host`) that runs package code in isolation

Everything else — the runtime dispatcher, the accounting system, the pricing
engine, the chat UI — is unchanged. The Provider Packages system plugs into
existing extension points without modifying them.

---

## High-level architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              USER (Browser)                             │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    │ HTTP
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  FRONTEND (Next.js)                                                     │
│                                                                         │
│  /stores/_sections/ProvidersSection.tsx                                 │
│  /stores/_modals/ProviderConfigureModal.tsx   ← Connection forms        │
│  /stores/_modals/ProviderUploadModal.tsx      ← ZIP upload              │
│  /lib/providerApi.ts                          ← Typed API client        │
│                                                                         │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    │ /api/store/providers/*
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  BACKEND (FastAPI)                                                      │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ routers/store/providers.py                                        │  │
│  │   • GET    /                    (list installed packages)         │  │
│  │   • GET    /catalog             (list installable packages)       │  │
│  │   • POST   /upload              (upload a ZIP)                    │  │
│  │   • GET    /{id}/connections    (list connections)                │  │
│  │   • POST   /{id}/connections    (create connection)               │  │
│  │   • POST   /{id}/connections/{cid}/activate                       │  │
│  │   • POST   /{id}/connections/{cid}/test                           │  │
│  │   • ... (12 more endpoints)                                       │  │
│  └─────────────────────┬─────────────────────────────────────────────┘  │
│                        │                                                │
│                        ▼                                                │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ omweb/provider_packages/                                          │  │
│  │                                                                   │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐        │  │
│  │  │  schema.py   │  │  validator   │  │  package_manager  │        │  │
│  │  │  (Pydantic)  │  │  (ZIP safety)│  │  (orchestrator)   │        │  │
│  │  └──────────────┘  └──────────────┘  └───────────────────┘        │  │
│  │                                                                   │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐        │  │
│  │  │  registry.py │  │  catalog.py  │  │  permissions.py   │        │  │
│  │  │  (persist)   │  │  (index)     │  │  (declaration)    │        │  │
│  │  └──────────────┘  └──────────────┘  └───────────────────┘        │  │
│  │                                                                   │  │
│  │  ┌──────────────────────────────────────────────────────────────┐ │  │
│  │  │  dependency_installer.py  (isolated venv per version)        │ │  │
│  │  └──────────────────────────────────────────────────────────────┘ │  │
│  └─────────────────────┬─────────────────────────────────────────────┘  │
│                        │                                                │
│                        ▼                                                │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ peldrun/llm/plugin_host/                                          │  │
│  │   • supervisor.py  (manages a worker subprocess)                  │  │
│  │   • worker.py      (loads the adapter, serves IPC requests)       │  │
│  │   • protocol.py    (JSON-Lines message schema)                    │  │
│  │   • transport.py   (stdin/stdout pipes)                           │  │
│  │   • permissions.py (runtime enforcer, network allowlist)          │  │
│  └─────────────────────┬─────────────────────────────────────────────┘  │
│                        │                                                │
└────────────────────────┼────────────────────────────────────────────────┘
                         │
                         │ subprocess (one per native package)
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  WORKER SUBPROCESS (isolated Python venv)                               │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  user's adapter code                                              │  │
│  │    • class AcmeProvider:                                          │  │
│  │        async def generate(request) -> LLMResponse                 │  │
│  │        def stream(request) -> AsyncIterator[LLMStreamEvent]       │  │
│  │        async def discover_models(connection) -> list              │  │
│  └─────────────────────┬─────────────────────────────────────────────┘  │
│                        │                                                │
│                        ▼ (HTTPS, subject to allowlist)                  │
└─────────────────────────────────────────────────────────────────────────┘
                         │
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  PROVIDER API (e.g., api.anthropic.com, api.openai.com, gigachat.dev)   │
└─────────────────────────────────────────────────────────────────────────┘
```

### What the diagram shows

The diagram has **five layers**:

1. **User** — interacts with the Store UI
2. **Frontend** — renders the Providers section, sends REST calls
3. **Backend** — handles upload, installation, connection management
4. **Plugin Host** — manages isolated worker subprocesses
5. **Provider** — the external LLM API

**Only `native` and `custom` transports use the worker subprocess.** A
`compatible` transport bypasses the plugin_host entirely and delegates to a
built-in adapter that runs in-process. This is why the vertical arrow from
plugin_host to "WORKER SUBPROCESS" is labeled "one per native package".

---

## The seven core components

### 1. Schema (`backend/omweb/provider_packages/schema.py`)

**Responsibility**: Define the shape of every data structure in the system.

**Key models**:

| Model | Purpose |
|-------|---------|
| `ProviderManifest` | The `manifest.json` inside a package |
| `ConnectionFieldSpec` | One input in the connection form |
| `RuntimeHints` | UI warnings (TLS, token refresh, docs link) |
| `PackagePermissions` | What the adapter is allowed to do |
| `PackageFeatureFlags` | Streaming, tools, vision, reasoning, ... |
| `ProviderPackageEntry` | A row in `registry.json` |
| `ProviderPackageInstallResult` | Response from POST /upload |
| `ProviderPackageCatalogItem` | An item in the store catalog |

**Design invariant**: The manifest uses `extra="forbid"` — a typo in a field
name is a hard error, not a silent drop. The registry entries use
`extra="allow"` — future field additions never break old data.

### 2. Validator (`backend/omweb/provider_packages/package_validator.py`)

**Responsibility**: Perform every safety check on an uploaded ZIP **before**
any file touches disk.

**Checks**:

- Compressed size limit (100 MB)
- Uncompressed size limit (500 MB)
- Compression ratio limit (100:1) — archive bomb defense
- File count limit (2000)
- Path safety: no `..`, no absolute paths, no backslashes, no NUL bytes
- Symlink rejection
- Manifest presence and Pydantic validation
- Entry-point module presence (for `native` / `custom`)

**Returns**: A `ValidationReport` dataclass. The caller (`package_manager`)
decides what exception to raise based on the errors found.

### 3. Package Manager (`backend/omweb/provider_packages/package_manager.py`)

**Responsibility**: Orchestrate the full install pipeline and every
lifecycle operation.

**Install pipeline** (6 steps, atomic):

1. **validate** — call `package_validator.validate_zip()`
2. **conflict check** — reject builtin IDs, idempotent re-installs
3. **extract** — `safe_extract` into a staging directory
4. **promote** — atomic move to `versions/<version>/`
5. **environment** — create isolated venv (skipped for `compatible`)
6. **register** — persist to `registry.json` + `active.json`

If any step from 4 onward fails, the manager deletes the partial state.

**Lifecycle operations**:

- `install_bundle(zip_bytes)` — full install
- `activate_version(id, version)` — rollback / roll-forward
- `uninstall(id)` — remove everything
- `enable(id)` / `disable(id)` — runtime toggle
- `check_health(id)` — filesystem diagnostics
- `get_active_adapter_descriptor(id)` — runtime metadata

### 4. Registry (`backend/omweb/provider_packages/registry.py`)

**Responsibility**: Persist `registry.json` and `active.json` atomically with
thread-safe reads and writes.

**Two files**:

- `registry.json` — global index of all installed packages
- `<id>/active.json` — per-package pointer to the active version

**Atomic writes**: `tmp_file → os.replace`. A crash mid-write leaves the
previous file untouched.

**Cache**: In-memory, invalidated on every write. External editors can call
`invalidate_cache()` to force a re-read.

### 5. Catalog (`backend/omweb/provider_packages/catalog.py`)

**Responsibility**: Serve the list of installable packages from
`builtin_catalog.json` (bundled) plus an optional custom catalog.

**Installability rules**:

- `compatible` — **always installable** (no ZIP needed, generates a manifest
  from the catalog item)
- `native` / `custom` — installable only if both `download_url` and a valid
  `checksum_sha256` are present

**Download safety**: HTTP(S) only, hard timeout, size cap enforced mid-stream,
SHA-256 verification before install.

### 6. Dispatcher Integration (`backend/omweb/agent_bridge_parts/dispatcher.py`)

**Responsibility**: Resolve which provider_id and extra_params to use for an
LLM request.

**The key function**: `_resolve_provider_and_extras(active_llm, connection_id)`.

**Logic**:

```
1. Check active_llm["provider"] and ["provider_name"] as candidates.
2. For each candidate:
   a. Look up in provider_registry.
   b. If found AND source == "external" AND class_path contains "custom_plugin":
      i. Build descriptor dict from provider_package_manager.
      ii. Load the ACTIVE connection for this package.
      iii. Build extra_params = {_peldrun_plugin: descriptor, <custom fields>}.
      iv. Return (provider_id, extra_params).
3. If nothing matches: return ("openai_compat", {})
```

**The last step is important**: if no plugin package matches, the dispatcher
falls back to the legacy `openai_compat` adapter. This preserves pre-M9
behavior for every user who has not installed any packages.

### 7. Plugin Host (`backend/peldrun/llm/plugin_host/`)

**Responsibility**: Execute `native` / `custom` adapters in isolation.

**Components**:

| File | Role |
|------|------|
| `supervisor.py` | Spawns and manages a worker subprocess |
| `worker.py` | Runs inside the subprocess, loads the adapter, serves IPC |
| `protocol.py` | JSON-Lines message schema (18 message types) |
| `transport.py` | stdin/stdout pipes (JSON-Lines framing) |
| `permissions.py` | Runtime enforcer (network allowlist, secret isolation) |

**When is it used?** Only for `native` / `custom` transports. Compatible
packages delegate to a built-in adapter in-process, no subprocess.

**Lifecycle**: Lazily spawned on the first request. Reused for the rest of the
session. Killed on `close()` or process shutdown.

---

## The two execution paths

This is the most important architectural distinction to understand.

### Path A — Compatible transport

```
┌──────────────────────────────────────────────────────────────┐
│  Request arrives at dispatcher                               │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  _resolve_provider_and_extras()                              │
│    → returns ("acme-ai", {_peldrun_plugin: {...}})           │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  provider_factory.create("acme-ai", connection)              │
│    → sees ADAPTER_STYLE = "v1"                               │
│    → instantiates PluginProviderAdapter(context=...)         │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  PluginProviderAdapter.__init__                              │
│    → sees descriptor.transport == "compatible"               │
│    → calls _setup_compatible_delegate()                      │
│    → creates built-in adapter (openai_compat) as delegate    │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  adapter.generate(request)                                   │
│    → delegates to openai_compat in-process                   │
│    → openai_compat uses AsyncLLMClient (AsyncOpenAI)         │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
              ┌──────────┐
              │ Provider │
              └──────────┘
```

**Cost**: ~0 (no subprocess, no IPC, no serialization)

**Use cases**:
- Any provider that speaks OpenAI Chat Completions
- Any provider that speaks Anthropic Messages (via `base_adapter: "anthropic"`)
- Ollama and LM Studio (via their native adapters as `base_adapter`)

### Path B — Native / custom transport

```
┌──────────────────────────────────────────────────────────────┐
│  Request arrives at dispatcher                               │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  _resolve_provider_and_extras()                              │
│    → returns ("gigachat", {_peldrun_plugin: {...}, scope, ...}) │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  provider_factory.create("gigachat", connection)             │
│    → instantiates PluginProviderAdapter(context=...)         │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  PluginProviderAdapter.__init__                              │
│    → sees descriptor.transport == "native"                   │
│    → stores supervisor config, defers spawning               │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  adapter.generate(request) — first call                      │
│    → _ensure_started() spawns WorkerSupervisor               │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  WorkerSupervisor.start()                                    │
│    → asyncio.create_subprocess_exec(venv_python, -m worker) │
│    → waits for READY                                         │
│    → sends LOAD_ADAPTER                                      │
│    → waits for ADAPTER_LOADED                                │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  Worker subprocess (isolated venv)                           │
│    → capture_stdout_for_protocol()                           │
│    → import adapter                                          │
│    → install_httpx_guard(enforcer)                           │
│    → instantiate adapter                                     │
│    → wait for GENERATE                                       │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  WorkerSupervisor.generate(request)                          │
│    → sends GENERATE over IPC (JSON line)                    │
│    → waits for GENERATE_RESULT                               │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  Worker handler: handle_generate()                           │
│    → enforcer.require(PERM_LLM_GENERATE)                     │
│    → adapter.generate(request)                               │
│    → serialize LLMResponse → GenerateResultPayload           │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
              ┌──────────┐
              │ Provider │
              └──────────┘
```

**Cost**: ~1-3s (first call, spawn cost) + ~50ms per request (IPC overhead)

**Use cases**:
- GigaChat (needs OAuth flow + custom TLS)
- AWS Bedrock (needs SigV4 signing)
- YandexGPT (needs IAM token + folder_id)
- Any provider whose SDK pulls in heavy dependencies that would pollute the
  main environment

---

## Data flow — end to end

### Flow 1 — Install a package

```
User clicks "Upload Package"
  ↓
ProviderUploadModal reads File
  ↓
POST /api/store/providers/upload (multipart/form-data)
  ↓
router: upload_provider_package()
  ↓
provider_package_manager.install_bundle(zip_bytes)
  ↓
  ├── validate_zip() → ValidationReport
  ├── _check_conflicts() → reject if builtin or hash mismatch
  ├── safe_extract() → staging directory
  ├── os.replace(staging, versions/<version>/)
  ├── DependencyInstaller.create_environment() (native only)
  └── registry.install_version() → writes registry.json
  ↓
Returns ProviderPackageInstallResult
  ↓
Frontend calls reload()
  ↓
GET /api/store/providers → updated list
  ↓
Card appears in "Installed" tab with "Not configured" badge
```

### Flow 2 — Configure a connection

```
User clicks "Configure" on a card
  ↓
useProviderActions.handleOpenConfigure(provider_id)
  ├── setConfigureProvider(provider)
  ├── setConnections([])
  └── handleLoadConnections(provider_id)
        ↓
        GET /api/store/providers/{id}/connections
        ↓
        Returns masked connections
  ↓
ProviderConfigureModal opens in List mode
  ↓
If 0 connections: auto-opens in Create mode
  ↓
User fills form (base_url, api_key, custom fields)
  ↓
handleCreateConnection(provider_id, payload)
  ↓
POST /api/store/providers/{id}/connections
  ↓
router: create_connection()
  ↓
config_rtr.save_provider_connection(connection)
  ↓
Writes to models_vault.json → provider_connections[]
  ↓
Returns saved connection
  ↓
Frontend refreshes list → shows the new row
```

### Flow 3 — Use a plugin provider in chat

```
User sends a chat message
  ↓
run_direct_chat(job_id, prompt, llm_override)
  ↓
active_llm = {provider: "gigachat", model: "GigaChat-Pro", ...}
  ↓
_resolve_provider_and_extras(active_llm, job_id)
  ↓
  ├── provider_registry.find("gigachat") → registration found
  ├── registration.source == "external"
  ├── registration.class_path == "peldrun.llm.providers.custom_plugin:PluginProviderAdapter"
  ├── _build_plugin_descriptor_dict("gigachat") → descriptor
  ├── get_active_connection_for_package("gigachat") → connection with credentials
  └── returns ("gigachat", {
        _peldrun_plugin: descriptor,
        scope: "PERS",
        ca_bundle: "/etc/ssl/russian_trusted_root_ca.crt",
        _peldrun_base_url: "https://gigachat.devices.sberbank.ru/api/v1",
        _peldrun_api_key: "<oauth token>",
      })
  ↓
connection = ConnectionContext(
    provider_id="gigachat",
    base_url="https://gigachat.devices.sberbank.ru/api/v1",
    api_key="<oauth token>",
    extra_params={_peldrun_plugin: ..., scope: "PERS", ca_bundle: ...},
)
  ↓
adapter = provider_factory.create("gigachat", connection)
  ↓
PluginProviderAdapter._ensure_started() spawns worker
  ↓
WorkerSupervisor.generate(request) → IPC → worker → adapter.generate()
  ↓
Response comes back through the same chain
  ↓
SSE chunks stream to the frontend
  ↓
Final result renders in the chat
```

---

## Storage layout

### Complete tree

```
<backend>/
├── data/
│   └── models_vault.json                ← ALL user connections (existing)
│       ├── cloud_vault: []              ← LLM Settings (built-in providers)
│       ├── custom_endpoints: []         ← LLM Settings (custom URLs)
│       ├── provider_connections: []     ← NEW: Store → Providers connections
│       ├── lmstudio_vault: {}           ← LLM Settings
│       ├── ollama_vault: {}             ← LLM Settings
│       └── scanned_models: []           ← metadata cache
│
├── storage/
│   ├── models_metadata.json             ← per-model capability facts
│   ├── models_budget.json               ← per-model context/temperature
│   │
│   └── store/
│       └── providers/                   ← NEW (M9)
│           ├── registry.json            ← Global index of all packages
│           │
│           ├── gigachat/
│           │   ├── active.json          ← {"version": "1.0.0", "hash": "..."}
│           │   ├── versions/
│           │   │   └── 1.0.0/
│           │   │       ├── manifest.json
│           │   │       ├── _peldrun_meta.json
│           │   │       ├── requirements.txt
│           │   │       └── gigachat_peldrun/
│           │   │           └── adapter.py
│           │   └── environments/
│           │       └── 1.0.0/           ← Isolated venv
│           │           ├── bin/python
│           │           └── lib/python3.x/site-packages/
│           │
│           └── acme-compatible/
│               ├── active.json
│               └── versions/
│                   └── 1.0.0/
│                       ├── manifest.json
│                       └── _peldrun_meta.json
│
├── omweb/
│   └── provider_packages/                ← NEW (M9 admin layer)
│       ├── __init__.py
│       ├── schema.py
│       ├── errors.py
│       ├── package_validator.py
│       ├── dependency_installer.py
│       ├── permissions.py
│       ├── registry.py
│       ├── package_manager.py
│       ├── catalog.py
│       └── builtin_catalog.json
│
└── peldrun/
    └── llm/
        └── plugin_host/                  ← NEW (M9 runtime layer)
            ├── __init__.py
            ├── protocol.py
            ├── transport.py
            ├── worker.py
            ├── supervisor.py
            └── permissions.py
```

### Key invariants

1. **`models_vault.json` schema is extended, not changed.** The new
   `provider_connections` array is a separate section. Existing keys
   (`cloud_vault`, `custom_endpoints`, ...) are untouched.

2. **Package storage is fully isolated.** A crash or corruption in
   `storage/store/providers/` cannot break `models_vault.json` or
   `models_metadata.json`.

3. **Each version has its own venv.** Rolling back to version 1.0.0 restores
   the exact environment that was validated at install time.

4. **`_peldrun_meta.json` is PELDRUN-owned.** It is written by the manager
   after extraction and never shipped by the package author. It records the
   SHA-256 of the original ZIP, the install timestamp, and the resolved entry
   point.

---

## Plugin Host deep dive

### Why a separate process per package?

Three reasons:

**1. Dependency isolation**

Each `native` package can require its own version of `httpx`, `pydantic`,
`anthropic`, `boto3`, etc. Without isolation, installing two packages that
require incompatible versions would break both.

**2. Fault isolation**

A crashing adapter (segfault, OOM, infinite loop) kills only the worker. The
main backend process and every other provider remain unaffected.

**3. Permission enforcement**

The worker receives only its own connection's credentials. It has no access to
other connections' secrets, the workspace filesystem, or the metadata vault.
This is a hard boundary at the process level.

### IPC protocol

The worker communicates with the supervisor over **JSON Lines on
stdin/stdout**. Each message is one JSON object, followed by `\n`.

**Envelope shape**:

```json
{
  "v": 1,
  "id": "req_abc123",
  "type": "generate",
  "payload": { ... }
}
```

**Message types** (18 total):

| Direction | Type | Purpose |
|-----------|------|---------|
| S→W | `load_adapter` | Import and instantiate the adapter |
| S→W | `generate` | Non-streaming completion |
| S→W | `stream_start` | Streaming completion |
| S→W | `stream_cancel` | Cancel an active stream |
| S→W | `discover_models` | List available models |
| S→W | `validate_connection` | Probe the connection |
| S→W | `get_model_metadata` | Fetch per-model metadata |
| S→W | `shutdown` | Graceful shutdown |
| W→S | `ready` | Worker is ready (sent at startup) |
| W→S | `adapter_loaded` | Adapter imported successfully |
| W→S | `generate_result` | Non-streaming result |
| W→S | `stream_started` | Stream assigned a stream_id |
| W→S | `stream_event` | One streaming event |
| W→S | `stream_end` | Stream terminated |
| W→S | `models_result` | Model list |
| W→S | `connection_result` | Validation result |
| W→S | `metadata_result` | Per-model metadata |
| W→S | `error` | Generic error reply |
| W→S | `log` | Unsolicited log message |

**Correlation**: every request carries a unique `id`. The worker echoes it in
its reply. The supervisor maintains a `_pending_replies` dict keyed by that
id.

### Worker startup sequence

This ordering is **load-bearing** — changing it introduces protocol corruption
or permission bypasses:

```
1. capture_stdout_for_protocol()
   → redirects sys.stdout to sys.stderr
   → returns ORIGINAL binary stdout for protocol use

2. Normal imports (safe now that stdout is redirected)

3. Send READY message with protocol_version + python_version + pid

4. On LOAD_ADAPTER:
   a. Build RuntimePermissionEnforcer (validates permissions)
   b. install_httpx_guard(enforcer)
      ← MUST be before adapter import
      ← patches httpx.AsyncClient to wrap every request in AllowlistAsyncTransport
   c. Insert package_dir into sys.path
   d. import adapter module
   e. instantiate adapter class
   f. Reply ADAPTER_LOADED

5. Serve requests until SHUTDOWN
```

**Why step 4b comes before 4d**: if the adapter (or its SDK) creates an
`httpx.AsyncClient` at module import time, that client must be wrapped with the
allowlist. If we imported the adapter first, the client would be created
without a guard, and the adapter could reach any URL.

### Network allowlist

The `AllowlistAsyncTransport` wraps every outbound HTTP request and checks:

1. `network.connect` permission is granted
2. URL scheme is `http` or `https`
3. URL is not in the hardcoded denylist
   (`169.254.169.254`, `metadata.google.internal`, ...)
4. URL's `(scheme, host, port)` matches the connection's `base_url`

If any check fails, the request raises `URLNotAllowedError` before any bytes
leave the process. The adapter sees a normal error; the provider never receives
the request.

**Limitations**:

- Only wraps `httpx.AsyncClient`. An adapter that uses `requests`, raw sockets,
  or `aiohttp` bypasses the allowlist.
- Does not prevent the adapter from opening a socket directly via `socket`.
- OS-level sandboxing (seccomp, AppContainer) is a future enhancement.

**These limitations are documented in the source.** Real security requires
OS-level isolation. The allowlist is a defense-in-depth measure that catches
accidental SSRF and misbehaving SDKs, not a malicious package.

### Error translation

Every error raised by the adapter becomes a structured `ErrorPayload`:

```json
{
  "code": "rate_limited",
  "message": "Rate limit exceeded",
  "retryable": true,
  "details": { "retry_after_seconds": 5 }
}
```

The supervisor rehydrates it into a canonical `ProviderError` subclass. The
caller (dispatcher, executor) sees the exact same error types as if the adapter
ran in-process. **The IPC boundary is invisible.**

---

## Provider registry and connections

### Registry state model

`registry.json` tracks every installed package. Here is the complete lifecycle
of one package:

```
[NOT INSTALLED]
     ↓  install_bundle()
[INSTALLED + ENABLED + ACTIVE VERSION 1.0.0]
     ↓  disable()
[DISABLED]
     ↓  enable()
[ENABLED]
     ↓  install_bundle() for version 1.1.0
[ENABLED + INSTALLED_VERSIONS=[1.0.0, 1.1.0] + ACTIVE VERSION 1.1.0]
     ↓  activate_version("1.0.0")
[ENABLED + ACTIVE VERSION 1.0.0]  ← ROLLBACK
     ↓  uninstall()
[NOT INSTALLED]
```

### Connection state model

`provider_connections` in `models_vault.json` tracks every saved connection.
Each connection has independent `enabled` and `is_active` flags:

```
[CONNECTION CREATED: enabled=true, is_active=false]
     ↓  activate()
[enabled=true, is_active=true]  ← used by runtime
     ↓  disable()
[enabled=false, is_active=true]  ← NOT used by runtime
     ↓  enable()
[enabled=true, is_active=true]  ← used by runtime again
     ↓  create second connection + activate it
[first: enabled=true, is_active=false]
[second: enabled=true, is_active=true]  ← used by runtime
```

**Invariant**: at most one connection per package has `is_active=true`.

**Runtime rule**: `get_active_connection_for_package()` returns a connection
only if BOTH `is_active=true` AND `enabled=true`. An active-but-disabled
connection is treated as "no active connection" — the dispatcher falls back to
`openai_compat` with a clear warning.

---

## What is NOT this system's responsibility

To avoid scope creep, here is an explicit list of what Provider Packages
**do not** do:

| Concern | Responsible subsystem |
|---------|----------------------|
| LLM request compilation (capability intersection) | `peldrun.llm.request_compiler` |
| Model capability resolution | `peldrun.llm.capability_resolver` |
| Token accounting and cost tracking | `peldrun.llm.accounting` |
| Chat history and context management | `omweb.chat_storage_engine` |
| Agent loop and tool execution | `peldrun.agents.tool_call_agent` |
| Prompt formatting and system directives | `peldrun.context.builder` |
| Pricing lookup | `peldrun.runtime.pricing_store` |
| Budget enforcement | `peldrun.runtime.executor` |
| Streaming SSE events to the UI | `omweb.sse_events` |
| LLM Settings UI (built-in providers) | `omweb.routers.config_rtr` |

The Provider Packages system is a **plugin loader**, not a rewrite. It plugs
into the existing extension points without changing them.

---

## Version history

### M9 — Provider Package administrative layer

**Delivered**:

- `omweb/provider_packages/` package (10 files)
- Manifest schema with `extra="forbid"` validation
- ZIP validator with archive-bomb and Zip-Slip defenses
- Isolated venv installer per version
- Registry with atomic writes and rollback support
- Catalog with built-in preview items and checksum verification
- `plugin_host` with worker subprocess, IPC protocol, and runtime permissions
- REST API at `/api/store/providers/*`

### M10 — Store UI integration

**Delivered**:

- New "Providers" section in the Store sidebar
- Installed tab (grid of provider cards)
- Catalog tab (preview + install)
- Configure modal (dynamic connection form)
- Details drawer (versions, features, permissions, manifest, health)
- Upload modal (ZIP + install dependencies toggle)
- Full connection lifecycle (create / update / delete / enable / disable /
  activate / test)
- Multiple connections per package with exclusive active selection

### Future work

Not scheduled, but explicitly planned:

| Feature | Reason |
|---------|--------|
| OS-level sandboxing | The current model relies on the allowlist; real isolation needs seccomp or AppContainer |
| Signed packages | Current checksum verification requires a checksum in the catalog. Signed bundles allow third-party distribution without a central checksum source. |
| Package marketplace | A hosted index with version history, ratings, and reviews |
| Migration on schema bump | When `peldrun.llm.provider.v1` evolves to v2 |
| Streaming from native adapters | The IPC protocol supports streaming, but not all native adapters implement it yet |

---

## Next steps

- **Want to build a package?** → [Quickstart — Compatible](02-quickstart-compatible.md)
- **Need to write an adapter?** → [Adapter Protocol](06-adapter-protocol.md)
- **Curious about security?** → [Security Model](12-security-model.md)
- **Hitting an error?** → [Troubleshooting](14-troubleshooting.md)
```

---
