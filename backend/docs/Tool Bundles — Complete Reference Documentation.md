# 📖 Tool Bundles — Complete Reference Documentation

```markdown
# PELDRUN Tool Bundles — Complete Reference

> **Version:** 1.0.0
> **Last updated:** 2026-10-16
> **Audience:** Tool authors, backend engineers, integration developers

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Bundle Format Specification](#3-bundle-format-specification)
4. [Security Model](#4-security-model)
5. [Backend Components](#5-backend-components)
6. [HTTP API Reference](#6-http-api-reference)
7. [Frontend Integration](#7-frontend-integration)
8. [Creating Your First Bundle](#8-creating-your-first-bundle)
9. [Advanced Examples](#9-advanced-examples)
10. [Troubleshooting](#10-troubleshooting)
11. [Testing Reference](#11-testing-reference)
12. [Best Practices](#12-best-practices)
13. [Appendix — Full Manifest Schema](#13-appendix--full-manifest-schema)

---

## 1. Overview

**PELDRUN Tool Bundles** is a content-addressed, version-aware distribution
system for agent tools. Each bundle is a ZIP archive containing a declarative
manifest plus Python source code, installed atomically into an immutable
blob store.

### What problem does it solve?

Before bundles, tools could only be added as **single `.py` files** dropped
into `storage/store/tools/custom/`. That model has hard limitations:

- No support for multi-file tools (helpers, submodules, data files)
- No way to declare pip dependencies
- No version history — replacing a tool destroys the old version
- No rollback if a new version breaks
- No integrity verification
- No structured metadata (author, license, capabilities)

Bundles solve all of the above with a single, well-defined format.

### Key guarantees

| Guarantee | How it works |
|---|---|
| **Atomic install** | Files are written to a staging directory, then `os.replace()` moves them into place |
| **Idempotency** | The content hash determines the blob path — same payload = same blob |
| **Side-by-side versions** | Every version keeps its own immutable blob |
| **Instant rollback** | Switching active version is a single pointer file update |
| **Self-healing** | Registry can be rebuilt from blobs alone |
| **Supply-chain integrity** | Optional SHA-256 checksum + strict validation |
| **Sandboxed execution** | Tools run bounded inside `workspace_root` with path traversal guards |

### When to use bundles vs. legacy single-file tools

| Scenario | Recommended approach |
|---|---|
| Simple one-file tool (< 100 lines) | Legacy `.py` upload |
| Multi-file tool with helpers | **Bundle** |
| Tool needs pip dependencies | **Bundle** |
| Tool needs versioning / rollback | **Bundle** |
| Third-party tool you want to audit | **Bundle** with checksum |
| Quick prototype | Legacy `.py` upload |

---

## 2. Architecture

### 2.1 High-level data flow

```
┌──────────┐   .zip    ┌──────────────┐   validated  ┌───────────────┐
│  User    │──────────>│  HTTP API    │─────────────>│  BundleManager│
│ (browser)│           │ /upload-...  │              │               │
└──────────┘           └──────────────┘              └───────┬───────┘
                                                            │
                              ┌─────────────────────────────┼──────────────┐
                              │                             │              │
                              ▼                             ▼              ▼
                       ┌──────────────┐             ┌──────────────┐ ┌──────────────┐
                       │  Validator   │             │  Extractor   │ │  Storage     │
                       │ (pre-flight) │             │ (safe unzip) │ │ (blob store) │
                       └──────────────┘             └──────────────┘ └──────┬───────┘
                                                                            │
                                                                            ▼
                                                                    ┌──────────────┐
                                                                    │  registry    │
                                                                    │  .json       │
                                                                    └──────────────┘
                                                                            │
                                                                            ▼
                                                                    ┌──────────────┐
                                                                    │  _blobs/     │
                                                                    │  _installs/  │
                                                                    └──────────────┘
```

### 2.2 Directory layout

```
backend/storage/store/tools/
├── registry.json                    ← Source of truth for installed bundles
├── _blobs/                          ← Immutable content-addressed store
│   ├── sha256_a3f9c12e4d5b6a7c/
│   │   ├── manifest.json
│   │   ├── tool.py
│   │   ├── helpers/
│   │   │   └── util.py
│   │   ├── requirements.txt
│   │   └── _bundle_meta.json        ← Internal metadata (hash, install time, ...)
│   └── sha256_b7d4e08c1a5f33d9/
│       └── ...
├── _installs/                       ← Active version pointers
│   ├── word_count/
│   │   └── active.txt               ← Contains "a3f9c12e4d5b6a7c"
│   └── pdf_summarizer/
│       └── active.txt
└── custom/                          ← Legacy single-file tools (unchanged)
    └── my_tool.py
```

### 2.3 Content-addressed storage (CAS)

Every uploaded payload is hashed with SHA-256 (truncated to 16 hex chars).
The hash **is** the identity of the blob:

```
SHA-256(zip_bytes).hexdigest()[:16] = "a3f9c12e4d5b6a7c"
```

**Why this matters:**

- **Idempotency**: uploading the same `.zip` twice never duplicates storage
- **Deduplication**: two versions with byte-identical content share one blob
- **Integrity**: the hash is a cryptographic commitment to the payload
- **Rollback safety**: switching versions never touches the underlying bytes

### 2.4 Atomic install model

Installation is designed so that **either the entire bundle is installed or
nothing changes on disk**. No partial states.

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. validate(zip)              → fail fast on security / structure  │
│ 2. make_temp_dir()            → allocate .tmp_<uuid>/             │
│ 3. safe_extract(zip, temp)    → write files into staging           │
│ 4. parse_manifest(temp)       → validate schema                    │
│ 5. compute_hash(zip)          → identity                           │
│ 6. commit_blob(temp, hash)    → atomic os.replace → _blobs/        │
│ 7. update_registry(hash)      → atomic write registry.json         │
│ 8. set_active_hash(id, hash)  → atomic write _installs/<id>/...    │
└─────────────────────────────────────────────────────────────────────┘
```

**Failure modes:**

- Steps 1–4 fail → staging directory is removed, no side effects
- Step 6 fails → staging directory stays orphaned, cleaned up on next install
- Steps 7–8 fail → blob exists but isn't referenced; `cleanup_orphans()` removes it

### 2.5 Version resolution

When the agent needs a tool instance:

```
ToolRegistry.get_custom_tool_instance(tool_id, workspace_root)
    │
    ├─ 1. In-memory cache (with workspace rebinding)
    │
    ├─ 2. Legacy custom_tools_dir/<id>.py
    │
    ├─ 3. Platform builtins_dir/<id>.py
    │
    └─ 4. BundleManager → get_bundle(id) → load_tool_from_bundle(blob)
                                                │
                                                ├─ sys.path.insert(blob_dir)
                                                ├─ importlib.util.spec_from_file_location
                                                ├─ resolve tool class
                                                ├─ instantiate
                                                ├─ bind workspace_root
                                                └─ sys.path.remove(blob_dir)
```

**Resolution priority:** legacy file > platform builtin > bundle.

This preserves backward compatibility: if you already have `word_count.py`
in `custom/`, adding a `word_count` bundle will not shadow it.

---

## 3. Bundle Format Specification

### 3.1 ZIP structure

```
<tool_id>-<version>.zip
├── manifest.json                  [REQUIRED] — metadata + entry point
├── <entry_point>.py               [REQUIRED] — declared in manifest
├── helpers/                       [OPTIONAL] — additional Python modules
│   ├── __init__.py
│   └── *.py
├── requirements.txt               [OPTIONAL] — pip dependencies
├── README.md                      [OPTIONAL] — recommended
└── LICENSE                        [OPTIONAL]
```

**Rules:**

- Exactly **one** `manifest.json` at the ZIP root (not nested)
- The file named in `entry_point` must exist at that exact path
- No `__pycache__`, `.git`, `.venv`, `node_modules`, or other build artifacts
- No absolute paths, no `..` segments, no symlinks
- File count ≤ **500**; per-file size ≤ **5 MB**; total extracted ≤ **100 MB**
- Compressed `.zip` size ≤ **20 MB**

### 3.2 `manifest.json` — full schema

```json
{
  "schema_version": "1.0.0",
  "id": "word_count",
  "version": "1.0.0",
  "name": "Word Count",
  "description": "Counts words, characters, and lines in a workspace file.",
  "entry_point": "tool.py",
  "class_name": "WordCountTool",
  "safety_level": "safe",
  "capabilities": ["workspace.read", "workspace.scoped"],
  "author": "Your Name",
  "license": "MIT",
  "homepage": "https://github.com/example/word_count",
  "tags": ["text", "analysis", "utility"],
  "runtime": {
    "python_min": "3.11",
    "python_max": null
  },
  "dependencies": {
    "pip": ["pypdf2>=3.0.0"]
  },
  "checksum_sha256": null
}
```

### 3.3 Field reference

| Field | Required | Type | Notes |
|---|---|---|---|
| `schema_version` | ✅ | string | Must match `^1\.\d+\.\d+$` |
| `id` | ✅ | string | `^[a-z][a-z0-9_]{2,63}$` |
| `version` | ✅ | string | Semver: `^MAJOR.MINOR.PATCH(-prerelease)?$` |
| `name` | ✅ | string | 1–120 characters |
| `description` | ✅ | string | 1–500 characters |
| `entry_point` | ✅ | string | Relative `.py` path, no `..` |
| `class_name` | ❌ | string | Python identifier; if omitted, first `SovereignBaseTool` subclass found is used |
| `safety_level` | ❌ | enum | `safe` (default), `read_only`, `dangerous` |
| `capabilities` | ❌ | list | See capability list below |
| `author` | ❌ | string | Free-form |
| `license` | ❌ | string | SPDX identifier (informational) |
| `homepage` | ❌ | string | URL |
| `tags` | ❌ | list | Lowercased, deduplicated, max 32 chars each |
| `runtime.python_min` | ❌ | string | `MAJOR.MINOR` format; default `"3.11"` |
| `runtime.python_max` | ❌ | string\|null | Inclusive; null = open-ended |
| `dependencies.pip` | ❌ | list | PEP 508 requirement strings |
| `checksum_sha256` | ❌ | string | 64-char lowercase hex; verified against actual hash |

### 3.4 Capabilities enum

| Capability | Meaning |
|---|---|
| `workspace.read` | Reads files from `workspace_root` |
| `workspace.write` | Writes files to `workspace_root` |
| `workspace.scoped` | Uses `workspace_root` as its sole working directory |
| `network` | Performs outbound HTTP requests |
| `shell` | Executes subprocesses |
| `system` | Reads/writes outside the workspace (heavily discouraged) |
| `ai.llm` | Calls an LLM provider |

**Rule:** if `safety_level == "dangerous"`, at least one capability is
required. This ensures operators see what the tool actually does before
activation.

### 3.5 Tool class contract

Every bundle's entry file must define a subclass of `SovereignBaseTool`:

```python
from omweb.tools.base import SovereignBaseTool

class MyTool(SovereignBaseTool):
    name: str = "my_tool"
    description: str = "One-line description for the LLM."
    category: str = "custom"
    safety_level: str = "safe"

    # OPTIONAL: declare workspace support
    workspace_root: str = ""

    def set_workspace_root(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root

    # REQUIRED: async execution
    async def execute(self, **kwargs):
        # kwargs contains the arguments the LLM passed to the tool
        return {
            "output": "any string or dict",
            "exit_code": 0,
            "is_error": False,
        }
```

**Return contract:** the `execute()` method must return either a `dict`
(converted to `ToolResult` automatically) or a `ToolResult` instance.

---

## 4. Security Model

### 4.1 Validation layers

The system performs **five independent checks** before any file touches disk:

```
Layer 1 — HTTP input validation
  • Content-Type check
  • Filename extension check
  • Empty payload check

Layer 2 — Zip structure validation
  • Total compressed size ≤ 20 MB
  • File count ≤ 500
  • No symlinks
  • No forbidden directories
  • No forbidden file extensions (.pyc, .so, .dll, ...)

Layer 3 — Path safety validation
  • No absolute paths (checked BEFORE normalization)
  • No Windows drive letters
  • No `..` traversal segments
  • No path deeper than 10 levels
  • No path longer than 200 characters

Layer 4 — Manifest schema validation
  • Pydantic v2 with strict mode
  • ID regex, semver regex, entry-point regex
  • Cross-field consistency (e.g. dangerous → capabilities)

Layer 5 — Extraction containment
  • Re-verify path containment per file at write time
  • Cumulative size cap
  • Per-file size cap
```

### 4.2 Path containment invariant

Every extracted file must satisfy:

```python
(target_dir / normalized_path).resolve().relative_to(target_dir.resolve())
```

This runs **twice**: once in `validator.py` (against the ZIP entries) and
once in `extractor.py` (before each write). Defense in depth.

### 4.3 Zip-bomb heuristic

If the compression ratio exceeds **100×**, a warning is added to the report.
The install proceeds only because of the absolute size caps enforced
elsewhere (20 MB compressed, 100 MB extracted, 5 MB per file).

### 4.4 Python import isolation

When loading a bundle's code:

- The blob directory is prepended to `sys.path` **only** for the duration
  of the import.
- A unique module name is generated per load to avoid `sys.modules` pollution.
- On any failure, both the module entry and the `sys.path` entry are removed.
- Helper modules imported by the entry file remain accessible at runtime
  because Python holds module references in their original namespace.

### 4.5 Supply-chain verification

If the manifest declares `checksum_sha256`, the backend compares it against
`hashlib.sha256(zip_bytes).hexdigest()`. A mismatch aborts the install.

This lets publishers sign their bundles out-of-band and lets consumers
verify authenticity.

---

## 5. Backend Components

### 5.1 Module map

```
omweb/tools/bundles/
├── __init__.py        — public API re-exports
├── errors.py          — exception hierarchy
├── schema.py          — Pydantic models
├── validator.py       — ZIP pre-flight checks
├── extractor.py       — safe extraction
├── storage.py         — content-addressed storage layer
├── manager.py         — orchestration + registry
└── loader.py          — Python module import
```

### 5.2 `BundleManager`

**Responsibilities:**

- Orchestrate the install pipeline
- Maintain `registry.json`
- Handle version bumps, activation, deletion
- Provide health reports

**Public methods:**

```python
class BundleManager:
    def __init__(self, root_dir: Path): ...

    def install_bundle(
        self,
        zip_bytes: bytes,
        source_filename: str = "bundle.zip",
        *,
        install_requirements: bool = True,
        requirements_timeout_seconds: int = 60,
    ) -> BundleInstallResult: ...

    def list_bundles(self) -> List[BundleEntry]: ...
    def get_bundle(self, bundle_id: str) -> Optional[BundleEntry]: ...
    def delete_bundle(self, bundle_id: str, force: bool = False) -> bool: ...
    def activate_version(self, bundle_id: str, version: str) -> bool: ...
    def check_health(self, bundle_id: str) -> Optional[BundleHealthReport]: ...
    def cleanup_orphans(self, older_than_seconds: int = 3600) -> Dict[str, int]: ...
```

### 5.3 `BundleStorage`

Low-level filesystem operations:

```python
class BundleStorage:
    def blob_dir(self, hash_short: str) -> Path: ...
    def make_temp_dir(self) -> Path: ...
    def commit_blob(self, staging_dir, hash_short, meta) -> Path: ...
    def get_active_hash(self, bundle_id: str) -> Optional[str]: ...
    def set_active_hash(self, bundle_id: str, hash_short: str) -> None: ...
    def list_blobs(self) -> List[str]: ...
    def list_orphaned_blobs(self, active_hashes) -> List[str]: ...
    def cleanup_orphan_temp_dirs(self, older_than_seconds=3600) -> int: ...
```

### 5.4 `ToolRegistry` integration

The existing `ToolRegistry` was extended, not replaced:

```python
# At app startup (main.py)
from omweb.routers.store.bundles import get_bundle_manager
from omweb.tools.registry import tool_registry

tool_registry.set_bundle_manager(get_bundle_manager())
```

`list_tools()` now merges three sources:

1. Built-in tools (JSON manifests in `builtins/`)
2. Legacy custom tools (`custom/*.py`, `custom/*.json`)
3. Bundle-installed tools (from `registry.json`)

`get_custom_tool_instance()` searches them in that order, returning the
first match.

### 5.5 `load_tool_from_bundle()`

The core loading function:

```python
def load_tool_from_bundle(
    blob_dir: Path,
    entry_point: str,
    *,
    class_name: Optional[str] = None,
    workspace_root: Optional[str] = None,
    bundle_id: Optional[str] = None,
) -> SovereignBaseTool:
    ...
```

Steps:

1. Verify `blob_dir` exists and is a directory
2. Resolve `entry_point` relative to `blob_dir` with containment check
3. Prepend `blob_dir` to `sys.path`
4. Import the entry file with a unique module name
5. Resolve the tool class (explicit `class_name` or first subclass found)
6. Instantiate the class
7. Bind `workspace_root` (via `set_workspace_root()` or attribute)
8. Remove the `sys.path` entry
9. Return the instance

---

## 6. HTTP API Reference

All endpoints are mounted at `/api/store/tools/`.

### 6.1 `POST /upload-bundle`

Install a bundle from a ZIP archive.

**Request:**

```http
POST /api/store/tools/upload-bundle
Content-Type: multipart/form-data

file: <binary .zip>
install_requirements: true
```

**Response (200):**

```json
{
  "status": "success",
  "bundle": {
    "id": "word_count",
    "version": "1.0.0",
    "hash": "a3f9c12e4d5b6a7c",
    "name": "Word Count",
    "description": "...",
    "size_bytes": 4096,
    "file_count": 3,
    "capabilities": ["workspace.read"],
    "tags": ["text"],
    "install_report": {
      "requirements_installed": false,
      "requirements_skipped": true,
      "duration_ms": 145,
      "steps": [
        {"name": "validate", "status": "ok", "duration_ms": 2, "detail": "3 files"},
        {"name": "extract", "status": "ok", "duration_ms": 5, "detail": "3 files, 4000 bytes"},
        {"name": "manifest", "status": "ok", "duration_ms": 1, "detail": "word_count@1.0.0"},
        {"name": "blob_commit", "status": "ok", "duration_ms": 3, "detail": "hash=a3f9c12e4d5b6a7c"},
        {"name": "register", "status": "ok", "duration_ms": 4, "detail": "version=1.0.0"}
      ],
      "warnings": []
    },
    "warnings": []
  },
  "registry": {"total_bundles": 5}
}
```

**Error response (400):**

```json
{
  "detail": {
    "error": "Zip payload failed validation.",
    "code": "BUNDLE_VALIDATION_FAILED",
    "details": {
      "errors": ["traversal segment in path: '../evil.txt'"]
    }
  }
}
```

**Common error codes:**

| Code | HTTP | Meaning |
|---|---|---|
| `BUNDLE_WRONG_FILE_TYPE` | 400 | Not a `.zip` |
| `BUNDLE_EMPTY_UPLOAD` | 400 | Zero-byte payload |
| `BUNDLE_VALIDATION_FAILED` | 400 | Zip structure or security issue |
| `BUNDLE_MANIFEST_INVALID` | 400 | Malformed `manifest.json` |
| `BUNDLE_SECURITY_VIOLATION` | 400 | Path traversal, symlink, etc. |
| `BUNDLE_STORAGE_FAILED` | 500 | Filesystem error |
| `BUNDLE_LOAD_FAILED` | 500 | Python import failure |

### 6.2 `GET /bundles`

List all installed bundles.

```json
{
  "bundles": [
    {
      "id": "word_count",
      "name": "Word Count",
      "description": "...",
      "active_version": "1.0.0",
      "active_hash": "a3f9c12e4d5b6a7c",
      "installed_versions": [
        {
          "version": "1.0.0",
          "hash": "a3f9c12e4d5b6a7c",
          "installed_at": "2026-10-16T15:00:00Z",
          "size_bytes": 4096,
          "file_count": 3,
          "entry_point": "tool.py",
          "class_name": "WordCountTool"
        }
      ],
      "status": "active",
      "safety_level": "safe",
      "capabilities": ["workspace.read"],
      "tags": ["text"],
      "author": "Your Name",
      "homepage": null
    }
  ],
  "total": 1
}
```

### 6.3 `GET /bundles/{bundle_id}`

Return a single bundle's full detail. Same shape as a list element.

**Errors:**

| Code | HTTP |
|---|---|
| `BUNDLE_NOT_FOUND` | 404 |

### 6.4 `DELETE /bundles/{bundle_id}`

Remove a bundle. By default refuses if more than one version is installed.

**Query parameters:**

| Name | Type | Default | Meaning |
|---|---|---|---|
| `force` | boolean | false | Delete even with multiple versions |

**Response (200):**

```json
{"status": "success", "bundle_id": "word_count"}
```

**Errors:**

| Code | HTTP | Reason |
|---|---|---|
| `BUNDLE_NOT_FOUND` | 404 | Unknown id |
| `BUNDLE_DELETE_REFUSED` | 400 | Multiple versions and `force=false` |

### 6.5 `POST /bundles/{bundle_id}/activate`

Switch the active version (rollback or roll-forward).

**Request:**

```json
{"version": "1.0.0"}
```

**Response (200):**

```json
{
  "status": "success",
  "bundle_id": "word_count",
  "active_version": "1.0.0"
}
```

**Errors:**

| Code | HTTP | Reason |
|---|---|---|
| `BUNDLE_NOT_FOUND` | 404 | Unknown id |
| `BUNDLE_VERSION_NOT_FOUND` | 404 | Version not installed |
| `BUNDLE_STORAGE_FAILED` | 500 | Blob missing on disk |

### 6.6 `GET /bundles/{bundle_id}/health`

Filesystem integrity report.

**Response (200):**

```json
{
  "health": {
    "id": "word_count",
    "version": "1.0.0",
    "hash": "a3f9c12e4d5b6a7c",
    "status": "healthy",
    "checks": [
      {"name": "active_pointer", "passed": true, "message": "pointer=... registry=..."},
      {"name": "blob_present", "passed": true, "message": "hash=..."},
      {"name": "manifest_present", "passed": true, "message": "manifest.json readable"},
      {"name": "entry_point_present", "passed": true, "message": "ok"},
      {"name": "meta_present", "passed": true, "message": "ok"}
    ],
    "checked_at": "2026-10-16T15:05:00Z"
  }
}
```

**Status values:**

| Status | Meaning |
|---|---|
| `healthy` | All checks passed |
| `degraded` | Some checks failed but the bundle may still work |
| `broken` | No checks passed — bundle unusable |

---

## 7. Frontend Integration

### 7.1 Components

```
frontend/src/
├── lib/
│   └── bundleApi.ts                     — typed API client
├── components/store/
│   ├── BundleUploadModal.tsx            — upload with progress
│   ├── BundleCard.tsx                   — display card
│   └── BundleDetailsDrawer.tsx          — side panel with versions + health
└── app/store/
    └── page.tsx                         — integration point
```

### 7.2 User workflow

```
1. Navigate to /store
2. Click "Sovereign Tools" tab
3. Click "Install Bundle (.zip)" in the header
   → BundleUploadModal opens
4. Drag & drop a .zip file (or click to browse)
5. [Optional] Toggle "Auto-install requirements.txt"
6. Click "Install Bundle"
   → Progress bar advances 0 → 100%
   → Success card shows name, version, hash, size
7. Modal auto-closes after 1.5s
8. Bundle appears in "Installed Bundles" section
9. Click the ℹ️ icon on the card
   → BundleDetailsDrawer opens with:
     • Description
     • Capabilities
     • Health report
     • Version history table
     • Activate button per version
10. Click "Activate" on an older version to roll back
```

### 7.3 `bundleApi.ts` — client contract

```typescript
// Upload with real progress
export function uploadBundle(
  file: File,
  options: { installRequirements: boolean },
  onProgress?: (percent: number) => void
): Promise<BundleInstallResult>;

// Standard JSON operations
export function listBundles(): Promise<BundleEntry[]>;
export function getBundle(bundleId: string): Promise<BundleEntry>;
export function deleteBundle(bundleId: string, force?: boolean): Promise<void>;
export function activateVersion(bundleId: string, version: string): Promise<void>;
export function checkBundleHealth(bundleId: string): Promise<BundleHealthReport>;
```

---

## 8. Creating Your First Bundle

### Step 1 — Create the project directory

```
word_count/
├── manifest.json
├── tool.py
└── README.md
```

### Step 2 — Write `manifest.json`

```json
{
  "schema_version": "1.0.0",
  "id": "word_count",
  "version": "1.0.0",
  "name": "Word Count",
  "description": "Counts words, characters, and lines in a workspace file.",
  "entry_point": "tool.py",
  "class_name": "WordCountTool",
  "safety_level": "safe",
  "capabilities": ["workspace.read", "workspace.scoped"],
  "author": "Your Name",
  "license": "MIT",
  "tags": ["text", "analysis"]
}
```

### Step 3 — Write `tool.py`

```python
from pathlib import Path
from omweb.tools.base import SovereignBaseTool


class WordCountTool(SovereignBaseTool):
    name: str = "word_count"
    description: str = "Count words, characters, and lines in a workspace file."
    category: str = "custom"
    safety_level: str = "safe"

    workspace_root: str = ""

    def set_workspace_root(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root

    async def execute(self, path: str = "", **kwargs):
        if not path:
            return {
                "output": "Error: 'path' argument is required.",
                "is_error": True,
                "exit_code": 1,
            }

        root = Path(self.workspace_root or ".").resolve()
        target = (root / path).resolve()

        # Path containment check
        try:
            target.relative_to(root)
        except ValueError:
            return {
                "output": f"Error: '{path}' escapes the workspace.",
                "is_error": True,
                "exit_code": 1,
            }

        if not target.is_file():
            return {
                "output": f"Error: file not found: {path}",
                "is_error": True,
                "exit_code": 1,
            }

        text = target.read_text(encoding="utf-8", errors="replace")
        words = len(text.split())
        chars = len(text)
        lines = text.count("\n") + (1 if text else 0)

        return {
            "output": f"words={words} chars={chars} lines={lines}",
            "is_error": False,
            "exit_code": 0,
            "metadata": {"words": words, "chars": chars, "lines": lines},
        }
```

### Step 4 — (Optional) `README.md`

```markdown
# Word Count

A simple tool that counts words, characters, and lines in a file.

## Arguments
- `path` (string, required): Workspace-relative path to the file.

## Returns
A summary line: `words=N chars=N lines=N`
```

### Step 5 — Build the ZIP

**On Linux/macOS:**

```bash
cd word_count
zip -r ../word_count-1.0.0.zip .
```

**On Windows (PowerShell):**

```powershell
Compress-Archive -Path .\word_count\* -DestinationPath .\word_count-1.0.0.zip
```

> **Important:** compress the **contents** of the directory, not the
> directory itself. `manifest.json` must be at the root of the ZIP.

### Step 6 — Upload

1. Open `http://localhost:3088/store`
2. Go to **Sovereign Tools**
3. Click **Install Bundle (.zip)**
4. Drop `word_count-1.0.0.zip`
5. Watch the progress bar
6. Confirm the bundle card appears

### Step 7 — Enable for an agent

1. Go to **Agents Hub**
2. Edit the target agent (or create a new one)
3. In "Scoped Permitted Tools", select `word_count`
4. Save

### Step 8 — Use it in a chat

```
Count the words in @somefile.md using word_count
```

The agent will call `word_count` with `path="somefile.md"` and return the
result.

---

## 9. Advanced Examples

### 9.1 Bundle with helpers

```
my_bundle/
├── manifest.json
├── tool.py
└── helpers/
    ├── __init__.py
    └── text_utils.py
```

**`tool.py`:**

```python
from helpers.text_utils import normalize_text
from omweb.tools.base import SovereignBaseTool


class MyTool(SovereignBaseTool):
    name: str = "my_tool"
    description: str = "Uses a helper module."
    category: str = "custom"

    async def execute(self, text: str = "", **kwargs):
        cleaned = normalize_text(text)
        return {"output": cleaned, "is_error": False, "exit_code": 0}
```

**`helpers/text_utils.py`:**

```python
def normalize_text(s: str) -> str:
    return " ".join(s.lower().split())
```

The loader automatically prepends `blob_dir` to `sys.path` so
`from helpers.text_utils import ...` resolves correctly.

### 9.2 Bundle with pip dependencies

Add a `requirements.txt`:

```
pypdf2>=3.0.0
requests>=2.31.0
```

**`manifest.json`:**

```json
{
  "schema_version": "1.0.0",
  "id": "pdf_summarizer",
  "version": "1.0.0",
  "name": "PDF Summarizer",
  "description": "Extracts text from PDF files in the workspace.",
  "entry_point": "tool.py",
  "dependencies": {
    "pip": ["pypdf2>=3.0.0"]
  }
}
```

**`tool.py`:**

```python
from pathlib import Path
from pypdf2 import PdfReader
from omweb.tools.base import SovereignBaseTool


class PdfSummarizer(SovereignBaseTool):
    name: str = "pdf_summarizer"
    description: str = "Extracts text from a workspace PDF."
    category: str = "custom"

    workspace_root: str = ""

    def set_workspace_root(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root

    async def execute(self, path: str = "", **kwargs):
        root = Path(self.workspace_root or ".").resolve()
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return {"output": "escape", "is_error": True, "exit_code": 1}

        reader = PdfReader(str(target))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return {"output": text[:4000], "is_error": False, "exit_code": 0}
```

When you upload the bundle with `install_requirements=true`, the backend
runs:

```bash
python -m pip install --disable-pip-version-check --no-input -r requirements.txt
```

with a 60-second timeout.

### 9.3 Versioning and rollback

**Step 1 — Install v1.0.0**

Bump `manifest.json`:

```json
{"id": "word_count", "version": "1.0.0", ...}
```

Upload the ZIP. The registry now shows one version.

**Step 2 — Install v1.1.0**

Change `version` to `"1.1.0"`, modify the tool, rebuild, upload.

Both blobs exist:
- `_blobs/sha256_<hash1>/` (v1.0.0)
- `_blobs/sha256_<hash2>/` (v1.1.0)

`_installs/word_count/active.txt` points to hash2.

**Step 3 — Roll back**

Either:
- Use the UI: click the version chip on the card → select v1.0.0
- Or call the API: `POST /bundles/word_count/activate {"version":"1.0.0"}`

The active pointer flips atomically. The old blob is still on disk.

---

## 10. Troubleshooting

### Bundle rejected: "traversal segment in path"

**Cause:** the ZIP contains a file whose path includes `..`.

**Fix:** rebuild the ZIP without traversing upward. On Windows, avoid
using `Compress-Archive` from a parent directory that includes `..`.

```powershell
# WRONG — includes '..' entries
Compress-Archive -Path ..\word_count\* -DestinationPath out.zip

# CORRECT — runs inside the bundle dir
cd word_count
Compress-Archive -Path * -DestinationPath ..\out.zip
```

### Bundle rejected: "absolute path"

**Cause:** the ZIP contains an absolute path like `/etc/passwd` or `C:\...`.

**Fix:** ensure the archiver stores **relative** paths. Most archivers do
this correctly when you `cd` into the directory first.

### Bundle rejected: "missing required file: manifest.json"

**Cause:** the manifest isn't at the ZIP root.

**Wrong structure:**

```
word_count-1.0.0.zip
└── word_count/
    ├── manifest.json     ← nested, not at root
    └── tool.py
```

**Correct structure:**

```
word_count-1.0.0.zip
├── manifest.json         ← at root
└── tool.py
```

### "entry_point not found in bundle"

The file declared in `manifest.entry_point` doesn't exist inside the ZIP.

Check:
- Exact case-sensitive filename (`tool.py` ≠ `Tool.py`)
- Exact path (`tool.py` ≠ `src/tool.py` unless the file is truly there)

### "No SovereignBaseTool subclass found"

Your entry file doesn't define a class that inherits from
`SovereignBaseTool`, or the class is missing.

**Wrong:**

```python
class MyTool:  # doesn't inherit!
    ...
```

**Correct:**

```python
from omweb.tools.base import SovereignBaseTool

class MyTool(SovereignBaseTool):
    ...
```

### `pip install` timing out

The default timeout is 60 seconds. If your dependencies are large or your
network is slow, upload with `install_requirements=false` first, then
install manually:

```bash
python -m pip install -r backend/storage/store/tools/_blobs/sha256_<hash>/requirements.txt
```

### Bundle shows "degraded" health

Open the details drawer to see which check failed. Common causes:

| Failed check | Meaning | Fix |
|---|---|---|
| `blob_present` | Blob directory was manually deleted | Reinstall the bundle |
| `manifest_present` | `manifest.json` inside the blob is missing | Reinstall |
| `entry_point_present` | Entry file was deleted | Reinstall |
| `active_pointer` | `active.txt` doesn't match registry | Roll back or reinstall |

### Import error at runtime

Example: `ModuleNotFoundError: No module named 'helpers'`

**Cause:** the bundle was installed **before** the helpers directory was
added, or the ZIP structure is wrong.

**Fix:** verify the ZIP contains the helpers:

```bash
unzip -l bundle.zip
```

Rebuild and reinstall if helpers are missing.

---

## 11. Testing Reference

### 11.1 Backend tests

```bash
cd D:\AI\peldrun\backend
```

**Run all bundle tests:**

```bash
python -m pytest tests/test_bundles_phase1.py \
                 tests/test_bundle_validation.py \
                 tests/test_bundle_extraction.py \
                 tests/test_bundle_storage.py \
                 tests/test_bundle_manager.py \
                 tests/test_bundle_loader.py \
                 tests/test_bundle_integration.py \
                 tests/test_bundles_api.py -v
```

**Expected:** 137 tests passing.

**Test coverage by module:**

| Module | Tests | Coverage |
|---|---|---|
| `errors.py` | 3 | Exception hierarchy + serialization |
| `schema.py` | 20 | Every field validator + cross-field rules |
| `validator.py` | 21 | Happy path + 13 rejection scenarios |
| `extractor.py` | 13 | Happy path + security + cleanup |
| `storage.py` | 21 | Hash + blobs + pointers + cleanup |
| `manager.py` | 24 | Full lifecycle + versioning + persistence |
| `loader.py` | 12 | Import + class resolution + sys.path hygiene |
| `integration` | 7 | End-to-end: install → load → execute |
| `HTTP API` | 16 | Every endpoint + error codes |

### 11.2 Test fixtures

Build bundles programmatically in tests:

```python
from tests.fixtures import bundle_factory as bf

# Valid minimal bundle
payload = bf.make_valid_bundle()

# Bundle with helpers
payload = bf.make_bundle_with_helpers()

# Bundle with requirements
payload = bf.make_bundle_with_requirements()

# Malicious bundles (for negative tests)
payload = bf.make_bundle_with_traversal()
payload = bf.make_bundle_with_absolute_path()
payload = bf.make_bundle_with_symlink()
```

### 11.3 Frontend tests

```bash
cd D:\AI\peldrun\frontend
npm run typecheck
```

**Expected:** 0 errors in bundle-related files.

---

## 12. Best Practices

### 12.1 Manifest hygiene

- **Always set `class_name`** — explicit is better than implicit
- **Always declare `capabilities`** — operators see what a tool does
- **Always set `license`** — critical for third-party distribution
- **Use semver correctly** — MAJOR for breaking changes
- **Keep `id` stable** across versions — that's how upgrades work

### 12.2 Tool code

- **Validate all inputs** before touching the filesystem
- **Enforce path containment** in every file operation
- **Return structured errors** — never raise uncaught exceptions
- **Prefer `set_workspace_root()`** over class attribute injection
- **Keep `description` concise** — it's shown to the LLM

### 12.3 Version bumps

| Change | Version bump |
|---|---|
| Bug fix in tool logic | PATCH (1.0.0 → 1.0.1) |
| New argument, backward-compatible | MINOR (1.0.0 → 1.1.0) |
| Removed argument, changed return shape | MAJOR (1.0.0 → 2.0.0) |

### 12.4 Security posture

- **Never bundle `.env`, `secrets.json`, or API keys** — they're read by
  anyone who can inspect the blob
- **Never use `safety_level: dangerous`** unless absolutely required
- **Declare `network` capability** explicitly if making HTTP calls
- **Do not use `shell` capability** for anything other than local
  computation
- **Provide `checksum_sha256`** for public distribution

### 12.5 Publishing

For tools shared across teams:

1. Version the source in a Git repo
2. Add a `Makefile` or script to build the ZIP deterministically:

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   VERSION=$(jq -r .version manifest.json)
   rm -f out.zip
   find . -type f ! -path './out.zip' -print0 \
     | sort -z \
     | zip -X -0 out.zip -@
   echo "Built out.zip for v$VERSION"
   sha256sum out.zip
   ```

3. Publish the SHA-256 alongside the download
4. Consumers verify before uploading

---

## 13. Appendix — Full Manifest Schema

### JSON Schema (draft-07)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "PELDRUN Bundle Manifest",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version", "id", "version", "name",
    "description", "entry_point"
  ],
  "properties": {
    "schema_version": {
      "type": "string",
      "pattern": "^1\\.\\d+\\.\\d+$"
    },
    "id": {
      "type": "string",
      "pattern": "^[a-z][a-z0-9_]{2,63}$"
    },
    "version": {
      "type": "string",
      "pattern": "^\\d+\\.\\d+\\.\\d+(-[a-z0-9.\\-]+)?$"
    },
    "name": {
      "type": "string",
      "minLength": 1,
      "maxLength": 120
    },
    "description": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500
    },
    "entry_point": {
      "type": "string",
      "pattern": "^[A-Za-z0-9_./\\-]+\\.py$",
      "not": {
        "anyOf": [
          {"pattern": "^/"},
          {"pattern": "\\.\\."}
        ]
      }
    },
    "class_name": {
      "type": ["string", "null"],
      "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"
    },
    "safety_level": {
      "type": "string",
      "enum": ["safe", "read_only", "dangerous"],
      "default": "safe"
    },
    "capabilities": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "workspace.read", "workspace.write", "workspace.scoped",
          "network", "shell", "system", "ai.llm"
        ]
      },
      "uniqueItems": true
    },
    "author": {"type": ["string", "null"]},
    "license": {"type": ["string", "null"]},
    "homepage": {"type": ["string", "null"]},
    "tags": {
      "type": "array",
      "items": {"type": "string", "maxLength": 32},
      "uniqueItems": true
    },
    "runtime": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "python_min": {
          "type": "string",
          "pattern": "^\\d+\\.\\d+$",
          "default": "3.11"
        },
        "python_max": {
          "type": ["string", "null"],
          "pattern": "^\\d+\\.\\d+$"
        }
      }
    },
    "dependencies": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "pip": {
          "type": "array",
          "items": {"type": "string"}
        }
      }
    },
    "checksum_sha256": {
      "type": ["string", "null"],
      "pattern": "^[a-f0-9]{64}$"
    }
  },
  "allOf": [
    {
      "if": {
        "properties": {"safety_level": {"const": "dangerous"}}
      },
      "then": {
        "properties": {
          "capabilities": {"minItems": 1}
        }
      }
    }
  ]
}
```

### Python model — quick reference

```python
class BundleManifest(BaseModel):
    schema_version: str
    id: str
    version: str
    name: str
    description: str
    entry_point: str
    class_name: Optional[str] = None
    safety_level: Literal["safe", "read_only", "dangerous"] = "safe"
    capabilities: List[BundleCapability] = []
    author: Optional[str] = None
    license: Optional[str] = None
    homepage: Optional[str] = None
    tags: List[str] = []
    runtime: RuntimeSpec = RuntimeSpec()
    dependencies: DependenciesSpec = DependenciesSpec()
    checksum_sha256: Optional[str] = None
```

### Complete `BundleInstallResult`

```python
class BundleInstallResult(BaseModel):
    id: str
    version: str
    hash: str
    name: str
    description: str
    size_bytes: int
    file_count: int
    capabilities: List[str]
    tags: List[str]
    install_report: InstallReport
    warnings: List[str]
```

### Complete `BundleEntry`

```python
class BundleEntry(BaseModel):
    id: str
    name: str
    description: str
    active_version: str
    active_hash: str
    installed_versions: List[BundleVersionRecord]
    status: Literal["active", "disabled", "broken"]
    safety_level: str
    capabilities: List[str]
    tags: List[str]
    author: Optional[str]
    homepage: Optional[str]
```

---

## End of Document

For issues or questions, open an issue on the project repository or consult
the code under `backend/omweb/tools/bundles/`.
```
