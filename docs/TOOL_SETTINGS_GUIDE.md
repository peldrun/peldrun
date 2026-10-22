# Tool Settings — Developer Guide

**Version**: 1.0.0
**Audience**: Backend and frontend engineers adding or modifying tools
**Prerequisites**: Familiarity with Python type hints, Pydantic v2, JSON Schema

---

## Table of Contents

1. [Overview](#1-overview)
2. [The Settings Pipeline](#2-the-settings-pipeline)
3. [Supported Settings Schema Types](#3-supported-settings-schema-types)
4. [Scenario A — Built-in Tool](#4-scenario-a--built-in-tool)
5. [Scenario B — Custom Single-File Tool](#5-scenario-b--custom-single-file-tool)
6. [Scenario C — Bundle Tool](#6-scenario-c--bundle-tool)
7. [Backend Adapter Changes](#7-backend-adapter-changes)
8. [Frontend Integration](#8-frontend-integration)
9. [Environment Probes (Optional)](#9-environment-probes-optional)
10. [Best Practices](#10-best-practices)
11. [Troubleshooting](#11-troubleshooting)
12. [Reference: File Map](#12-reference-file-map)

---

## 1. Overview

PELDRUN's settings system lets end users configure tools **without writing code**.
It is **schema-driven**: each tool declares what fields it accepts, and the
platform handles validation, persistence, UI rendering, and injection.

### Design principles

| Principle | Guarantee |
|---|---|
| **Separation** | Tool code (`manifest.json`, `.py`) is read-only. User values live in a separate directory. |
| **Validation** | Every `PUT` runs `jsonschema.validate()` before writing. Bad values never reach disk. |
| **Atomicity** | Writes use `os.replace()`; a crashed write can never leave a half-written file. |
| **Dependency injection** | Tools never read settings from disk. The platform reads them and passes them via `context["settings"]`. |
| **Opt-in** | Only tools that declare a non-empty `settings.schema` are configurable. Everything else is untouched. |

### What the system gives you for free

- A "⚙️ Settings" button on every configurable tool's card.
- A dynamic settings modal that renders inputs from your schema.
- Client-side and server-side validation.
- Persistence across restarts and bundle upgrades.
- An "Environment" check button (opt-in — see [§9](#9-environment-probes-optional)).

---

## 2. The Settings Pipeline

```
┌──────────────────────────────────────────────────────────────────────┐
│ 1. AUTHORING                                                         │
│    Developer declares settings.schema in the tool's manifest.       │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 2. DISCOVERY                                                         │
│    GET /api/store/tools            → list_tools() returns schema    │
│    GET /api/store/tools/{id}/settings → schema + merged values      │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 3. USER EDIT                                                         │
│    ⚙️ button → ToolSettingsModal → form → PUT /tools/{id}/settings   │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 4. VALIDATION + PERSISTENCE                                          │
│    ToolSettingsManager.save_settings():                              │
│      a. jsonschema.validate(values, schema)                          │
│      b. atomic write to the tool's storage path                      │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼ (at run start)
┌──────────────────────────────────────────────────────────────────────┐
│ 5. INJECTION                                                         │
│    resolve_tool_runtime:                                             │
│      context["settings"] = registry.get_tool_settings(tool_id)       │
│    → AdapterSpec.matches_context() filters to requires_context keys  │
│    → create_<tool>_executor(..., settings=...)                       │
│    → <CoreTool>(..., settings=...)                                   │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼ (every tool call)
┌──────────────────────────────────────────────────────────────────────┐
│ 6. EXECUTION                                                         │
│    _arun(argument=None) → self._settings_<key>                       │
│    _arun(argument=value) → explicit value wins (user override)       │
└──────────────────────────────────────────────────────────────────────┘
```

### Storage locations

| Tool type | Settings file |
|---|---|
| Built-in | `backend/storage/store/tools/builtin_settings/<tool_id>.json` |
| Bundle | `backend/storage/store/tools/_installs/<bundle_id>/settings.json` |
| Custom single-file | `backend/storage/store/tools/builtin_settings/<tool_id>.json` (same as builtin) |

**Bundles keep their settings across upgrades.** `BundleManager.install_bundle()`
never touches `_installs/<id>/settings.json` — only `delete_bundle()` removes it.
This is a documented contract; see `BundleStorage.set_active_hash()` and
`BundleStorage.delete_install()` docstrings.

---

## 3. Supported Settings Schema Types

The frontend renders each JSON Schema `type` with a dedicated widget. All types
below are fully supported.

| `type` | Extra fields | UI Widget | Example |
|---|---|---|---|
| `string` | `default`, `description`, `title` | text input | `"default": "hello"` |
| `string` + `enum` | `enum`, `enumNames` | `<select>` dropdown | `"enum": ["a","b"]` |
| `integer` | `minimum`, `maximum`, `default` | number input (step=1) | `"minimum": 1, "maximum": 100` |
| `number` | `minimum`, `maximum`, `default` | number input (any step) | `"minimum": 0.0` |
| `boolean` | `default` | checkbox | `"default": true` |
| `object` | `properties` (recursive) | `<fieldset>` group | `"properties": {"width": {...}}` |
| `array` | `items` (`type` used for coercion) | textarea (one item/line) | `"items": {"type": "string"}` |

### Schema-level keywords

| Keyword | Behavior |
|---|---|
| `type: "object"` (top-level) | Required. Container for `properties`. |
| `properties: {...}` | If empty or absent, **the tool is NOT configurable**. No ⚙️ button appears, and `GET /settings` returns 404. |
| `additionalProperties: false` | Recommended. Rejects unknown keys. |
| `required: [...]` | Enforced by `jsonschema.validate`. Any missing required key → 400. |

### Minimum viable schema

```json
{
  "type": "object",
  "properties": {
    "verbose": { "type": "boolean", "default": false }
  },
  "additionalProperties": false
}
```

### Empty schema (deliberately non-configurable)

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

Use this for tools like `source_control` and `web_search` that have no
per-instance configuration.

---

## 4. Scenario A — Built-in Tool

Built-in tools ship with PELDRUN. They consist of:

1. A JSON manifest under `backend/omweb/tools/builtins/<tool_id>.json`.
2. A Python class under `backend/peldrun/tools/builtins/<tool_id>.py`.
3. An adapter under `backend/omweb/adapters/core_adapter_parts/tools_adapters/<tool_id>_adapter.py`.

### Step 1 — Declare the schema in the manifest

```json
{
  "id": "my_builtin",
  "name": "My Builtin Tool",
  "description": "Does something useful.",

  "settings": {
    "enabled": true,
    "schema": {
      "type": "object",
      "properties": {
        "timeout_seconds": {
          "type": "integer",
          "minimum": 1,
          "maximum": 300,
          "default": 30,
          "title": "Timeout (seconds)",
          "description": "Maximum execution time before aborting."
        },
        "api_endpoint": {
          "type": "string",
          "default": "https://api.example.com",
          "description": "Base URL for outbound requests."
        },
        "log_level": {
          "type": "string",
          "enum": ["debug", "info", "warning", "error"],
          "default": "info",
          "title": "Log Level"
        },
        "retries": {
          "type": "integer",
          "minimum": 0,
          "maximum": 10,
          "default": 3
        }
      },
      "additionalProperties": false
    },
    "defaults": {},
    "required": []
  },

  "parameters": {
    "type": "object",
    "properties": {
      "input": { "type": "string" }
    },
    "required": ["input"]
  }
}
```

> **Important**: The schema lives under `settings.schema` for built-in tools,
> **not** under a top-level `settings_schema`. The registry checks both
> conventions, but `settings.schema` is the canonical form for built-in manifests.

### Step 2 — Read settings in the Core tool

```python
# backend/peldrun/tools/builtins/my_builtin.py
from __future__ import annotations
import os
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
from peldrun.tools.base import BaseTool, ToolResult


class MyBuiltinArgs(BaseModel):
    input: str = Field(..., description="Input payload.")
    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Override the configured timeout. Omit to use settings.",
    )


class MyBuiltinTool(BaseTool):
    name = "my_builtin"
    description = "Does something useful."
    args_schema = MyBuiltinArgs

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(workspace_root=workspace_root, **kwargs)

        s: Dict[str, Any] = settings or {}

        def _as_int(v, d):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d

        self._settings_timeout: int = _as_int(s.get("timeout_seconds"), 30)
        self._settings_endpoint: str = str(
            s.get("api_endpoint", "https://api.example.com")
        )
        self._settings_log_level: str = str(s.get("log_level", "info"))
        self._settings_retries: int = _as_int(s.get("retries"), 3)

    async def _arun(
        self,
        input: str,
        timeout_seconds: Optional[int] = None,
        **kwargs: Any,
    ) -> ToolResult:
        # Resolution priority: explicit argument > settings > default.
        eff_timeout = (
            int(timeout_seconds)
            if timeout_seconds is not None
            else self._settings_timeout
        )
        # ... do work using self._settings_endpoint, self._settings_retries ...
        return ToolResult(output=f"Handled {input} with timeout={eff_timeout}")
```

**Rules of the `__init__` contract:**

1. Accept `settings: Optional[Dict[str, Any]] = None` as a keyword argument.
2. Read all values via `settings.get(key, default)`.
3. Coerce / validate defensively; a malformed settings file must not crash the tool.
4. Store resolved values under `self._settings_<key>` so `_arun` can reference them.
5. Do not read from disk. The platform has already done that.

### Step 3 — Adapter changes

The adapter must (a) accept `settings`, (b) forward it to the Core tool, and
(c) declare `requires_context={"settings"}` so `resolve_tool_runtime` forwards it.

```python
# backend/omweb/adapters/core_adapter_parts/tools_adapters/my_builtin_adapter.py
from typing import Any, Awaitable, Callable, Dict, Optional
from peldrun.tools.builtins.my_builtin import MyBuiltinTool
from peldrun.tools.contract import ToolResult
from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path


def create_my_builtin_executor(
    workspace_dir: WorkspacePathType = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    ws_path = _resolve_workspace_path(workspace_dir)
    core_tool = MyBuiltinTool(
        workspace_root=str(ws_path),
        settings=settings or {},
    )

    async def _execute(input: str = "", **call_kwargs: Any) -> ToolResult:
        try:
            res = await core_tool.aexecute(input=input, **call_kwargs)
            return _ensure_tool_result(res, default_error_code="MY_BUILTIN_ERROR")
        except Exception as exc:
            return ToolResult(
                output=f"My Builtin Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "MY_BUILTIN_EXCEPTION"},
            )

    return _execute


ADAPTER_SPEC = AdapterSpec(
    tool_names=("my_builtin",),
    factory=create_my_builtin_executor,
    # This is the M7.7 contract. Without it, settings never arrive.
    requires_context=frozenset({"settings"}),
)
```

That's it. The frontend discovers the schema automatically and renders the
settings button on the tool card.

---

## 5. Scenario B — Custom Single-File Tool

Custom tools are uploaded by users via `POST /api/store/tools/upload`. They can
be either:

- A **`.json` manifest** with a matching class somewhere (legacy path), **or**
- A **`.py` script** containing a `SovereignBaseTool` subclass or an `execute` / `run` / `handler` function.

### For a `.json` manifest upload

The schema goes in the same place as a built-in:

```json
{
  "id": "my_custom_json",
  "name": "My Custom JSON Tool",
  "settings": {
    "schema": {
      "type": "object",
      "properties": {
        "greeting": { "type": "string", "default": "hello" }
      },
      "additionalProperties": false
    }
  },
  "parameters": { "type": "object", "properties": {}, "required": [] }
}
```

Since there is no companion `.py`, the tool will execute via whatever handler is
registered by the manifest's `entry_point` (if any). Settings **are** persisted
and **are** returned by `get_tool_settings()`, but a corresponding core tool
must read them at execution time. If your manifest is purely declarative, you
may not need to consume settings at all — the frontend still renders them for
future use.

### For a `.py` script upload

The registry auto-wraps any module-level `execute`, `run`, or `handler`
function into a `DynamicTool` instance. **However**, the dynamic wrapper does
**not** currently expose a settings-aware `__init__`. To consume settings from
an uploaded `.py` tool you must:

1. Define a class that subclasses `SovereignBaseTool` with an
   `__init__(self, workspace_root=None, settings=None, **kwargs)` signature.
2. Provide a `settings.schema` in a sibling `.json` manifest with the same `id`.

Example:

```python
# my_custom_json.py — uploaded alongside my_custom_json.json
from peldrun.tools.base import SovereignBaseTool, ToolResult

class MyCustomTool(SovereignBaseTool):
    name = "my_custom_json"
    description = "Custom tool with settings."

    def __init__(self, workspace_root=None, settings=None, **kwargs):
        super().__init__(workspace_root=workspace_root, **kwargs)
        s = settings or {}
        self._greeting = str(s.get("greeting", "hello"))

    async def execute(self, **kwargs):
        return ToolResult(output=f"{self._greeting}, world!")
```

The registry's `load_custom_tool_class` discovers `SovereignBaseTool` subclasses
automatically and instantiates them **without** settings (the dynamic loader
does not currently pass `settings`). **Consequence**: settings saved for a
custom `.py` tool are persisted and visible in the UI, but are **not injected**
into the instance.

> **Recommended practice**: if you need settings consumption, package the tool
> as a **bundle** (Scenario C). Bundles support `set_settings()` out of the box
> and are the intended path for settings-aware custom tools.

---

## 6. Scenario C — Bundle Tool

**Bundles are the recommended path for distributable, settings-aware tools.**
A bundle is a `.zip` file containing at least a `manifest.json` and an entry
point Python file.

### Bundle structure

```
my_bundle.zip
├── manifest.json
├── tool.py
└── (optional) requirements.txt
```

### `manifest.json`

```json
{
  "schema_version": "1.0.0",
  "id": "pdf_summarizer",
  "version": "1.0.0",
  "name": "PDF Summarizer",
  "description": "Summarize PDFs with configurable limits.",
  "entry_point": "tool.py",
  "class_name": "PdfSummarizerTool",
  "safety_level": "read_only",

  "settings_schema": {
    "type": "object",
    "properties": {
      "max_summary_length": {
        "type": "integer",
        "minimum": 100,
        "maximum": 5000,
        "default": 1000,
        "title": "Max Summary Length"
      },
      "include_metadata": {
        "type": "boolean",
        "default": false
      },
      "style": {
        "type": "string",
        "enum": ["bullet", "paragraph", "abstract"],
        "default": "paragraph"
      }
    },
    "additionalProperties": false
  },

  "parameters": {
    "type": "object",
    "properties": {
      "pdf_path": { "type": "string" }
    },
    "required": ["pdf_path"]
  }
}
```

> **Note**: Bundles use the **top-level** `settings_schema` key
> (not `settings.schema`). The registry's `_extract_schema_from_manifest`
> checks both conventions, but for bundles the top-level form is canonical.

### `tool.py`

The class MUST implement `set_settings(self, settings: dict)` so the platform
can inject user values. This method is called:

- **On first load** (in `ToolRegistry._load_from_bundle`).
- **On every cache hit** (in `ToolRegistry.get_custom_tool_instance`), so
  updates take effect without restarting the process.

```python
# tool.py
from typing import Any, Dict, Optional
from peldrun.tools.base import BaseTool, ToolResult


class PdfSummarizerTool(BaseTool):
    name = "pdf_summarizer"
    description = "Summarize a PDF."

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(workspace_root=workspace_root, **kwargs)
        # Apply defaults, then settings, so the tool is usable even
        # before set_settings() is called.
        self._settings: Dict[str, Any] = {
            "max_summary_length": 1000,
            "include_metadata": False,
            "style": "paragraph",
        }
        if settings:
            self._settings.update(settings)

    def set_settings(self, settings: Dict[str, Any]) -> None:
        """
        Required for settings to be applied at runtime. Called by the
        registry on every tool resolution, so updates are picked up
        without a restart.
        """
        if not isinstance(settings, dict):
            return
        self._settings.update(settings)

    async def _arun(self, pdf_path: str, **kwargs: Any) -> ToolResult:
        max_len = int(self._settings.get("max_summary_length", 1000))
        include_meta = bool(self._settings.get("include_metadata", False))
        style = str(self._settings.get("style", "paragraph"))

        # ... read pdf, summarize to max_len chars, optionally include metadata ...
        return ToolResult(output=f"Summary ({style}, {max_len} chars)")
```

### Packaging the bundle

The bundle is installed via `POST /api/store/tools/upload-bundle` (multipart
form with `file=<bundle.zip>`). The frontend's "Install Bundle (.zip)" button
does this automatically.

**Verification after install**:

```powershell
# 1. The schema is exposed to the frontend
Invoke-WebRequest -Uri "http://localhost:8088/api/store/tools/pdf_summarizer/settings" -Method GET | ConvertFrom-Json

# 2. Set a value
$body = '{"max_summary_length": 500, "style": "bullet"}'
Invoke-WebRequest -Uri "http://localhost:8088/api/store/tools/pdf_summarizer/settings" -Method PUT -Body $body -ContentType "application/json"

# 3. Confirm persistence
Get-Content "backend\storage\store\tools\_installs\pdf_summarizer\settings.json"
# → {"max_summary_length": 500, "style": "bullet"}

# 4. Upgrade the bundle to v1.1.0 — settings must survive
#    (upload a new zip with the same id, bumped version)
# 5. Re-read settings — they should still be the ones you set.
```

### Critical bundle rules

| Rule | Why |
|---|---|
| `set_settings()` must be defined | Otherwise the registry cannot inject settings on cache hit. |
| `__init__(settings=None)` must be accepted | The registry passes settings on first load. |
| Schema lives at top-level `settings_schema` | Both conventions work, but this is canonical for bundles. |
| Do not write to `_installs/<id>/settings.json` directly | Concurrent updates will corrupt state. Always go through the HTTP API. |
| `additionalProperties: false` in the schema | Prevents accidental pollution if the manifest changes. |

---

## 7. Backend Adapter Changes

Every tool type (built-in, custom, bundle) that wants user settings to reach
execution must:

### 7.1 — Forward `settings` through the adapter factory

```python
def create_<tool>_executor(
    workspace_dir: WorkspacePathType = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    ws_path = _resolve_workspace_path(workspace_dir)
    core_tool = <CoreTool>(workspace_root=str(ws_path), settings=settings or {})
    ...
```

### 7.2 — Declare the requirement in `AdapterSpec`

```python
ADAPTER_SPEC = AdapterSpec(
    tool_names=("<tool_id>",),
    factory=create_<tool>_executor,
    requires_context=frozenset({"settings"}),
)
```

`AdapterSpec.matches_context()` filters the context dict to only the keys
declared in `requires_context`. Adapters that omit `"settings"` will **never**
receive settings, even if the tool declares a schema.

### 7.3 — For tools that need more than settings

If the adapter needs both `settings` and something else (e.g. `run_id`),
include both:

```python
requires_context=frozenset({"run_id", "settings"}),
```

### 7.4 — Settings resolution timing

`resolve_tool_runtime()` calls `registry.get_tool_settings(tool_id)` **once per
run start**. Values are read from disk at that moment and frozen for the
session. Consequences:

| Change | Effect on existing sessions |
|---|---|
| Save settings, then start **new** chat | ✅ New values applied |
| Save settings, then use **existing** chat | ⚠️ Old values still applied |
| Bundle: save settings, then call tool | ✅ New values applied (via `set_settings` on cache hit) |

This is intentional — mid-session consistency is preferred over live reload.

---

## 8. Frontend Integration

**No frontend work is required** for a tool with a valid `settings.schema`.
The following happens automatically:

1. `list_tools()` returns `settings.schema` (built-in / custom JSON) or
   `settings_schema` (bundles).
2. `ToolCard.tsx` detects a non-empty `properties` map and renders the ⚙️
   button.
3. Clicking ⚙️ opens `ToolSettingsModal.tsx`, which:
   - `GET`s `/tools/{id}/settings`,
   - renders an input for each property using the schema type,
   - `PUT`s on save,
   - displays validation errors inline.

The modal supports:

| Feature | Implementation |
|---|---|
| Nested `object` | Recursive `<fieldset>` with sub-fields |
| `enum` | `<select>` with `enumNames` labels if provided |
| `boolean` | Checkbox |
| `integer` / `number` | `<input type="number">` with `min`/`max`/`step` |
| `array` | `<textarea>` (one item per line) |
| Escape key | Closes the modal (except while saving) |
| Body scroll lock | Prevents page scroll while open |

### Detecting "no settings" on the frontend

The ⚙️ button is hidden when:

```typescript
const schema = meta.settings?.schema || meta.settings_schema;
const hasConfigurableSettings = !!(
  schema?.properties && Object.keys(schema.properties).length > 0
);
```

Empty `properties: {}` → no button. This is how `source_control` and
`web_search` (both zero-config) opt out.

### Adding a new settings control type

If you introduce a new widget (e.g. a range slider for a bounded integer), edit
`ToolSettingsModal.tsx::FieldRow` and add a new branch before the generic
`integer`/`number` renderer. Every tool using that schema type will pick it up
automatically.

---

## 9. Environment Probes (Optional)

Some tools need to verify their runtime environment (e.g. a browser tool needs
Chromium installed). The settings system supports this via a "Test Environment"
button in the modal.

### Adding a probe

1. Create `backend/peldrun/tools/builtins/<tool>/environment.py` with an
   `async def probe_<tool>_environment(**context) -> Dict[str, Any]`.
2. Add a branch in `store_rtr.py::test_tool_environment`:

```python
if tool_id == "<tool_id>":
    from peldrun.tools.builtins.<tool>.environment import probe_<tool>_environment
    report = await probe_<tool>_environment(...)
    return {"status": "ok", **report}
```

3. The report MUST conform to:

```python
{
    "ready": bool,
    "checks": [
        {
            "name": str,        # stable identifier
            "label": str,       # human-readable title
            "passed": bool,
            "message": str,     # current state (installed / found / writable)
            "hint": str,        # OPTIONAL: shown when passed=False
            "note": str,        # OPTIONAL: gray italic note
        }
    ]
}
```

### Design constraints

| Constraint | Rationale |
|---|---|
| Probe must be **side-effect free** | Never launch Chromium to test the install; check `p.chromium.executable_path` instead. |
| Probe must not mutate settings | It's a read-only operation from the UI's perspective. |
| Report MUST include `passed` for every check | The UI uses this to color icons green/red. |
| `hint` is shown only when `passed=False` | Don't waste space on successes. |

### Reference implementation

See `backend/peldrun/tools/builtins/browser_interactive/environment.py` for a
complete example with three checks (Playwright package, Chromium binary,
screenshot directory writability + containment).

---

## 10. Best Practices

### Schema design

| Do | Don't |
|---|---|
| Give every field a `default` | Leave a field defaultless — the modal will show empty |
| Add `title` and `description` | Rely on the raw key name |
| Bound every `integer` / `number` with `minimum` / `maximum` | Accept any integer — validators can't catch typos |
| Use `enum` for discrete choices | Use `type: "string"` and hope users spell correctly |
| Set `additionalProperties: false` | Allow unknown keys — future schema changes silently break |
| Split large objects into named fields | Use one giant `"json": {...}` blob |

### `__init__` contract

- Accept `settings: Optional[Dict[str, Any]] = None`.
- Coerce defensively: `try: int(value) except: default`.
- Do not raise on malformed settings. Log a warning and use the default.
- Store resolved values under `self._settings_<key>` (explicit, greppable).

### Adapter contract

- Signature: `factory(workspace_dir, *, settings=None, **kwargs)`.
- Forward `settings` to the core tool constructor.
- Declare `requires_context=frozenset({"settings"})`.
- Do not substitute factory-level defaults for `settings` values (see
  `bash_adapter.py` — `timeout=None` is preserved so the core tool can fall
  back to settings).

### Versioning

- When adding a new field, add it **with a default** so existing user settings
  remain valid.
- When **removing** a field, keep `additionalProperties: true` briefly so old
  files don't fail validation. Then migrate.
- When **renaming** a field, provide both names for one release, then drop the
  old one.

### Testing checklist

Before shipping a settings-aware tool, verify:

- [ ] `GET /tools/{id}/settings` returns the schema (not 404).
- [ ] `PUT /tools/{id}/settings` with a valid payload → 200 + persisted file.
- [ ] `PUT /tools/{id}/settings` with an out-of-range value → 400, no write.
- [ ] Tool uses the setting at execution (`_arun` resolves from `self._settings_*`).
- [ ] Explicit arguments override settings (test: pass a value, confirm it wins).
- [ ] `requires_context={"settings"}` is declared in `ADAPTER_SPEC`.
- [ ] Bundle only: `set_settings()` is defined and exercised on cache hit.
- [ ] `ToolSettingsModal` renders all field types without errors.
- [ ] `npx tsc --noEmit` passes.

---

## 11. Troubleshooting

### ⚙️ button does not appear

| Cause | Check |
|---|---|
| Schema has empty `properties` | `{"type": "object", "properties": {}}` — no button by design |
| Schema lives in wrong place | Built-in: `settings.schema`; bundle: top-level `settings_schema` |
| `settings.enabled: false` | Disables the schema entirely |
| Frontend cache | Hard-reload (`Ctrl+Shift+R`) |

### `GET /tools/{id}/settings` returns 404

- The tool has no schema, or the schema has empty `properties`.
- Server is running with stale code (restart needed).
- Wrong `tool_id` (check `GET /tools` for the exact id).

### `PUT /tools/{id}/settings` returns 400

- Value violates the schema (`minimum`, `maximum`, `enum`, `type`).
- Missing a required field (`required: [...]`).
- Extra field when `additionalProperties: false`.

The 400 body contains a human-readable error from `jsonschema.validate`.

### Settings are saved but not applied at runtime

| Cause | Fix |
|---|---|
| Adapter missing `requires_context={"settings"}` | Add it |
| Factory does not accept `settings` kwarg | Add `*, settings=None, **kwargs` |
| Core tool `__init__` ignores `settings` | Add parsing |
| `_arun` uses a hardcoded default | Replace with `self._settings_<key>` |
| Existing session | Start a new chat |
| Bundle missing `set_settings()` | Add the method |

### Settings disappear after upgrade

- Should **not** happen for bundles: `set_active_hash` never touches
  `settings.json`.
- If it does, check `BundleStorage.set_active_hash` — a code change may have
  introduced a `shutil.rmtree(install_dir)`.

### `Test Environment` button does not appear

- `ToolSettingsModal` renders the button unconditionally, but only shows a
  meaningful report when the endpoint implements checks for that tool.
- For non-browser tools, it returns `{"checks": [{"name": "no_environment_checks", ...}]}`.
- If the button is missing entirely, the modal is stale in memory — reload.

### `probe_browser_environment` reports Chromium missing, but the browser works

- Playwright sometimes uses a **global** install at
  `~/.cache/ms-playwright/` and a **vendored** copy elsewhere.
- Check `p.chromium.executable_path` directly. If the path exists, the browser
  will work. The probe simply reflects the path Playwright expects.

---

## 12. Reference: File Map

```
backend/
├── omweb/
│   ├── tools/
│   │   ├── settings_manager.py           ← Central manager (schema + disk)
│   │   ├── registry.py                   ← ToolRegistry: get/save/schema
│   │   ├── bundles/
│   │   │   ├── manager.py                ← BundleManager: install/upgrade
│   │   │   └── storage.py                ← BundleStorage: atomic I/O
│   │   └── builtins/
│   │       ├── bash.json                 ← Built-in manifests (settings.schema)
│   │       ├── http_fetch.json
│   │       └── ...
│   ├── adapters/core_adapter_parts/
│   │   ├── resolve_tool_runtime.py       ← Injects settings into context
│   │   └── tools_adapters/
│   │       ├── _contract.py              ← AdapterSpec (requires_context)
│   │       ├── bash_adapter.py
│   │       ├── http_fetch_adapter.py
│   │       └── ..._adapter.py
│   └── routers/
│       └── store_rtr.py                  ← GET/PUT /settings, /test-environment
│
└── peldrun/
    └── tools/
        └── builtins/
            ├── shell_exec.py             ← Core Tool (reads settings)
            ├── str_replace_editor.py
            ├── file_ops.py
            ├── isolated_shell.py
            ├── human_input.py
            ├── http_fetch.py
            └── browser_interactive/
                ├── tool.py               ← Core Tool
                ├── session.py            ← BrowserSessionManager
                └── environment.py        ← Probe (Test Environment button)


frontend/
└── src/
    └── app/
        └── stores/
            ├── _components/
            │   └── ToolCard.tsx          ← Renders ⚙️ when schema is non-empty
            ├── _sections/
            │   └── ToolsSection.tsx      ← Wires ToolCard ↔ ToolSettingsModal
            └── _modals/
                └── ToolSettingsModal.tsx ← Dynamic form + Test Environment
```

---

## Appendix A — Complete Example: End-to-End

**Goal**: Add a `csv_analyzer` tool that takes a `max_rows` setting.

### 1. Manifest — `backend/omweb/tools/builtins/csv_analyzer.json`

```json
{
  "id": "csv_analyzer",
  "name": "CSV Analyzer",
  "description": "Analyze CSV files with configurable row limits.",
  "category": "data",
  "safety_level": "safe",
  "is_builtin": true,
  "settings": {
    "schema": {
      "type": "object",
      "properties": {
        "max_rows": {
          "type": "integer",
          "minimum": 10,
          "maximum": 1000000,
          "default": 10000,
          "title": "Max Rows",
          "description": "Maximum rows to process at once."
        },
        "delimiter": {
          "type": "string",
          "enum": [",", ";", "\t", "|"],
          "default": ",",
          "title": "Delimiter"
        }
      },
      "additionalProperties": false
    }
  },
  "parameters": {
    "type": "object",
    "properties": {
      "csv_path": { "type": "string", "description": "Path to the CSV file." }
    },
    "required": ["csv_path"]
  }
}
```

### 2. Core Tool — `backend/peldrun/tools/builtins/csv_analyzer.py`

```python
from __future__ import annotations
import csv
from pathlib import Path
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
from peldrun.tools.base import BaseTool, ToolResult


class CsvAnalyzerArgs(BaseModel):
    csv_path: str = Field(..., description="Path to the CSV file.")


class CsvAnalyzerTool(BaseTool):
    name = "csv_analyzer"
    description = "Analyze CSV files."
    args_schema = CsvAnalyzerArgs

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(workspace_root=workspace_root, **kwargs)
        s = settings or {}
        try:
            self._settings_max_rows = int(s.get("max_rows", 10000))
        except (TypeError, ValueError):
            self._settings_max_rows = 10000
        self._settings_delimiter = str(s.get("delimiter", ","))

    async def _arun(self, csv_path: str, **kwargs: Any) -> ToolResult:
        path = Path(self.workspace_root or ".") / csv_path
        if not path.is_file():
            return ToolResult(
                output=f"File not found: {csv_path}",
                exit_code=1,
                is_error=True,
            )
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter=self._settings_delimiter)
            rows = []
            for i, row in enumerate(reader):
                if i >= self._settings_max_rows:
                    break
                rows.append(row)
        return ToolResult(
            output=f"Read {len(rows)} rows (limit {self._settings_max_rows}).",
            exit_code=0,
            is_error=False,
        )
```

### 3. Adapter — `backend/omweb/adapters/core_adapter_parts/tools_adapters/csv_analyzer_adapter.py`

```python
from typing import Any, Awaitable, Callable, Dict, Optional
from peldrun.tools.builtins.csv_analyzer import CsvAnalyzerTool
from peldrun.tools.contract import ToolResult
from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path


def create_csv_analyzer_executor(
    workspace_dir: WorkspacePathType = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    ws_path = _resolve_workspace_path(workspace_dir)
    core_tool = CsvAnalyzerTool(
        workspace_root=str(ws_path),
        settings=settings or {},
    )

    async def _execute(csv_path: str = "", **call_kwargs: Any) -> ToolResult:
        try:
            res = await core_tool.aexecute(csv_path=csv_path, **call_kwargs)
            return _ensure_tool_result(res, default_error_code="CSV_ANALYZER_ERROR")
        except Exception as exc:
            return ToolResult(
                output=f"CSV Analyzer Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "CSV_ANALYZER_EXCEPTION"},
            )

    return _execute


ADAPTER_SPEC = AdapterSpec(
    tool_names=("csv_analyzer",),
    factory=create_csv_analyzer_executor,
    requires_context=frozenset({"settings"}),
)
```

### 4. Done

Restart the backend. The `csv_analyzer` tool appears in the Tools list with a
⚙️ button. Saving `max_rows=100` and running the tool will cap processing at
100 rows.

---

## Appendix B — Schema Cheat Sheet

```jsonc
{
  "type": "object",
  "properties": {
    "string_field": {
      "type": "string",
      "default": "text",
      "title": "Human Label",
      "description": "Help text shown under the input."
    },
    "enum_field": {
      "type": "string",
      "enum": ["a", "b", "c"],
      "enumNames": ["Option A", "Option B", "Option C"],
      "default": "a"
    },
    "integer_field": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100,
      "default": 10
    },
    "number_field": {
      "type": "number",
      "minimum": 0.0,
      "maximum": 1.0,
      "default": 0.5
    },
    "boolean_field": {
      "type": "boolean",
      "default": false
    },
    "nested_object": {
      "type": "object",
      "properties": {
        "width":  { "type": "integer", "default": 800 },
        "height": { "type": "integer", "default": 600 }
      },
      "additionalProperties": false
    },
    "array_field": {
      "type": "array",
      "items": { "type": "string" },
      "description": "One item per line in the textarea."
    }
  },
  "required": ["string_field"],
  "additionalProperties": false
}
```

