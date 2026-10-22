نعم، يمكن ذلك. لكن قبل أن أكتب الوثيقة، يجب أن أكون صريحاً معك في نقطة معمارية جوهرية:

**الـ Built-ins ليست مثل الـ Bundles**. الـ Bundles ملفات ZIP مستقلة تُرفع من الواجهة، بينما الـ Built-ins جزء من **الكود المصدري** للمشروع. لذلك، الوثيقة لن تكون "نسخة طبق الأصل" (لأن البنية مختلفة)، لكنها ستكون **مرجعاً هندسياً كاملاً مخصصاً** لإنشاء الأدوات المدمجة بأعلى المعايير، مع دمج ميزة الإعدادات (Settings) التي بنيناها.

إليك الوثيقة الكاملة باللغة الإنجليزية.

---

# 📖 PELDRUN Built-in Tools — Complete Reference Documentation

> **Version:** 2.0.0
> **Last updated:** 2026-10-22
> **Audience:** Core Backend Engineers, Tool Authors
> **Scope:** Creating, registering, and managing native built-in tools within the PELDRUN monorepo.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture & The 3 Layers](#2-architecture--the-3-layers)
3. [The Manifest (`<tool>.json`)](#3-the-manifest-tooljson)
4. [The Core Logic (`<tool>.py`)](#4-the-core-logic-toolpy)
5. [The Adapter (`<tool>_adapter.py`)](#5-the-adapter-tool_adapterpy)
6. [Settings Injection (Zero-Code UI)](#6-settings-injection-zero-code-ui)
7. [Workspace & File Management](#7-workspace--file-management)
8. [Security (SSRF & Path Traversal)](#8-security-ssrf--path-traversal)
9. [Step-by-Step: Building a New Built-in Tool](#9-step-by-step-building-a-new-built-in-tool)
10. [Testing & Verification](#10-testing--verification)
11. [Best Practices](#11-best-practices)
12. [Appendix: File Map](#12-appendix-file-map)

---

## 1. Overview

**Built-in Tools** are the native capabilities shipped directly with the PELDRUN repository. Unlike Bundles (which are `.zip` files uploaded via the UI), built-in tools are compiled into the application and loaded at startup.

### Why use Built-in Tools?
- They are fundamentally required by the platform (e.g., `bash`, `python_execute`, `http_fetch`).
- They need deep integration with the core runtime (e.g., accessing the LLM provider, emitting specific events).
- They must be updated synchronously with backend API changes.
- They bypass the overhead of ZIP validation and dynamic loading.

### What are the guarantees?
| Guarantee | How it works |
|---|---|
| **Auto-discovery** | `_discover_adapters()` in `executors.py` scans the `tools_adapters/` folder at startup. |
| **Context Injection** | `resolve_tool_runtime` binds `workspace_root`, `run_id`, and `settings` automatically. |
| **Zero-Code Settings UI** | The frontend reads `settings.schema` from the manifest and renders the ⚙️ Modal. |
| **Strict Isolation** | Every execution is bound to a specific `workspace_root` (chat session). |

---

## 2. Architecture & The 3 Layers

A built-in tool is **not** a single file. It is a triad of files living in different parts of the monorepo.

```
┌──────────────────────────────────────────────────────────────┐
│ 1. MANIFEST (Interface)                                      │
│    backend/omweb/tools/builtins/<tool>.json                  │
│    Defines arguments, settings schema, and LLM description.  │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│ 2. ADAPTER (Bridge)                                          │
│    backend/omweb/adapters/.../tools_adapters/<tool>_adapter  │
│    Declares requires_context, binds workspace, injects settings│
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│ 3. CORE LOGIC (Engine)                                       │
│    backend/peldrun/tools/builtins/<tool>.py                  │
│    Inherits BaseTool. The pure execution logic.              │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. The Manifest (`<tool>.json`)

Located in: `backend/omweb/tools/builtins/<tool>.json`

This file is read by the `ToolRegistry` and sent to the frontend. It defines:
1.  **LLM Interface:** What arguments the AI can pass.
2.  **User Interface:** What settings the human can configure.

### Full Manifest Example
```json
{
  "id": "my_builtin_tool",
  "name": "My Builtin Tool",
  "display_name": "My Custom Processor",
  "description": "CRITICAL: The LLM uses this to decide when to use the tool. Be specific.",
  "category": "system",
  "safety_level": "safe",
  "is_builtin": true,
  "is_enabled": true,

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
          "description": "Maximum execution time."
        },
        "api_endpoint": {
          "type": "string",
          "default": "https://api.example.com",
          "description": "Base URL for outbound requests."
        }
      },
      "additionalProperties": false
    }
  },

  "parameters": {
    "type": "object",
    "properties": {
      "input_data": {
        "type": "string",
        "description": "The data to process."
      }
    },
    "required": ["input_data"]
  }
}
```

> **Note:** Built-in manifests use the canonical `settings.schema` key (nested). Bundles use a top-level `settings_schema`. The registry handles both, but built-ins should stick to `settings.schema`.

---

## 4. The Core Logic (`<tool>.py`)

Located in: `backend/peldrun/tools/builtins/<tool>.py`

This is the pure execution engine. It **must** inherit from `BaseTool` (from `peldrun.tools.base`).
It **must not** read from disk or know about the Web layer. It receives everything via its `__init__`.

### The Contract
1.  Accept `workspace_root: str` and `settings: dict` in `__init__`.
2.  Implement `_arun(...)`.
3.  Return a `ToolResult`.

### Example Implementation
```python
# backend/peldrun/tools/builtins/my_builtin_tool.py
from typing import Any, Dict, Optional
from peldrun.tools.base import BaseTool, ToolResult
from pydantic import BaseModel, Field

class MyBuiltinArgs(BaseModel):
    input_data: str = Field(..., description="The data to process.")

class MyBuiltinTool(BaseTool):
    name = "my_builtin_tool"
    description = "My Custom Processor."
    args_schema = MyBuiltinArgs

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
        **kwargs: Any
    ) -> None:
        super().__init__(workspace_root=workspace_root, **kwargs)
        s = settings or {}
        # Defensive parsing: never crash if a setting is malformed.
        try:
            self._timeout = int(s.get("timeout_seconds", 30))
        except (ValueError, TypeError):
            self._timeout = 30
        self._endpoint = str(s.get("api_endpoint", "https://api.example.com"))

    async def _arun(self, input_data: str, **kwargs: Any) -> ToolResult:
        # ... execution logic using self._timeout and self._endpoint ...
        return ToolResult(
            output=f"Processed {input_data} using {self._endpoint}",
            exit_code=0,
            is_error=False
        )
```

---

## 5. The Adapter (`<tool>_adapter.py`)

Located in: `backend/omweb/adapters/core_adapter_parts/tools_adapters/<tool>_adapter.py`

This is the bridge. It receives context from the Web runtime, instantiates the Core tool, and exposes a callable.
**Crucially**, it must declare `requires_context={"settings"}` so the platform knows to fetch and inject user settings.

### Example Implementation
```python
# backend/omweb/adapters/core_adapter_parts/tools_adapters/my_builtin_tool_adapter.py
from typing import Any, Awaitable, Callable, Dict, Optional
from peldrun.tools.builtins.my_builtin_tool import MyBuiltinTool
from peldrun.tools.contract import ToolResult
from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

def create_my_builtin_tool_executor(
    workspace_dir: WorkspacePathType = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    ws_path = _resolve_workspace_path(workspace_dir)

    # 1. Instantiate the Core tool with context
    core_tool = MyBuiltinTool(
        workspace_root=str(ws_path),
        settings=settings or {},
    )

    # 2. Wrap it in an async callable
    async def _execute(input_data: str = "", **call_kwargs: Any) -> ToolResult:
        try:
            res = await core_tool.aexecute(input_data=input_data, **call_kwargs)
            return _ensure_tool_result(res, default_error_code="MY_TOOL_ERROR")
        except Exception as exc:
            return ToolResult(
                output=f"Tool Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "MY_TOOL_EXCEPTION"},
            )

    return _execute

# 3. Register the spec (Auto-discovered by executors.py)
ADAPTER_SPEC = AdapterSpec(
    tool_names=("my_builtin_tool",),
    factory=create_my_builtin_tool_executor,
    requires_context=frozenset({"settings"}), # CRITICAL
)
```

---

## 6. Settings Injection (Zero-Code UI)

When a built-in tool defines a `settings.schema`, PELDRUN automatically:
1.  Renders the ⚙️ button on the tool card in the UI.
2.  Validates user input against the schema.
3.  Saves it atomically to `backend/storage/store/tools/builtin_settings/<tool_id>.json`.
4.  Injects it via the Adapter -> `__init__(settings=...)`.

### Resolution Priority
If a setting is passed explicitly as an LLM argument, it overrides the user setting.
```python
async def _arun(self, input_data: str, timeout_seconds: Optional[int] = None, **kwargs):
    # Explicit argument wins over the user's configured setting
    eff_timeout = timeout_seconds if timeout_seconds is not None else self._timeout
```

---

## 7. Workspace & File Management

All built-in tools operate inside an isolated **Workspace Sandbox** (`workspace_root`).

*   **NEVER** write files using absolute paths (e.g., `C:\temp\file.txt`).
*   **ALWAYS** use `Path(self.workspace_root) / "filename"`.
*   **Path Traversal Guard:** Always validate paths before writing.

```python
from pathlib import Path

async def _arun(self, filename: str, content: str, **kwargs) -> ToolResult:
    ws_path = Path(self.workspace_root).resolve()
    target = (ws_path / filename).resolve()

    # SECURITY: Prevent the LLM from escaping the sandbox
    if not target.is_relative_to(ws_path):
        return ToolResult(
            output="Security Error: Path escapes workspace.",
            exit_code=1,
            is_error=True
        )

    target.write_text(content)

    return ToolResult(
        output=f"Saved {filename}",
        exit_code=0,
        is_error=False,
        artifacts=[filename] # Registers in the UI Artifacts tab
    )
```

---

## 8. Security (SSRF & Path Traversal)

If your built-in tool makes HTTP requests, it is vulnerable to Server-Side Request Forgery (SSRF).

**Always use the provided security module:**
```python
from peldrun.tools.builtins.browser_interactive.security import is_safe_url

async def _arun(self, url: str, **kwargs) -> ToolResult:
    if not is_safe_url(url):
        return ToolResult(
            output="Security Error: Malicious URL blocked.",
            exit_code=1,
            is_error=True
        )
    # ... proceed with request ...
```

---

## 9. Step-by-Step: Building a New Built-in Tool

Let's build a `csv_analyzer` tool from scratch.

### Step 1: Create the Manifest
`backend/omweb/tools/builtins/csv_analyzer.json`
```json
{
  "id": "csv_analyzer",
  "name": "CSV Analyzer",
  "description": "Analyze CSV files.",
  "safety_level": "safe",
  "is_builtin": true,
  "settings": {
    "schema": {
      "type": "object",
      "properties": {
        "max_rows": { "type": "integer", "default": 10000 }
      }
    }
  },
  "parameters": {
    "type": "object",
    "properties": {
      "csv_path": { "type": "string" }
    },
    "required": ["csv_path"]
  }
}
```

### Step 2: Create the Core Logic
`backend/peldrun/tools/builtins/csv_analyzer.py`
```python
from peldrun.tools.base import BaseTool, ToolResult
from pathlib import Path

class CsvAnalyzerTool(BaseTool):
    name = "csv_analyzer"
    description = "Analyze CSV files."

    def __init__(self, workspace_root=None, settings=None, **kwargs):
        super().__init__(workspace_root=workspace_root, **kwargs)
        s = settings or {}
        self._max_rows = int(s.get("max_rows", 10000))

    async def _arun(self, csv_path: str, **kwargs) -> ToolResult:
        ws_root = Path(self.workspace_root).resolve()
        target = (ws_root / csv_path).resolve()

        if not target.is_relative_to(ws_root) or not target.is_file():
            return ToolResult(output="File not found.", exit_code=1, is_error=True)

        with target.open("r", encoding="utf-8") as f:
            lines = [f.readline() for _ in range(self._max_rows)]

        return ToolResult(output=f"Read {len(lines)} rows.", exit_code=0, is_error=False)
```

### Step 3: Create the Adapter
`backend/omweb/adapters/core_adapter_parts/tools_adapters/csv_analyzer_adapter.py`
```python
from peldrun.tools.builtins.csv_analyzer import CsvAnalyzerTool
from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

def create_csv_analyzer_executor(workspace_dir=None, *, settings=None, **kwargs):
    ws_path = _resolve_workspace_path(workspace_dir)
    core_tool = CsvAnalyzerTool(workspace_root=str(ws_path), settings=settings or {})

    async def _execute(csv_path: str = "", **call_kwargs):
        res = await core_tool.aexecute(csv_path=csv_path, **call_kwargs)
        return _ensure_tool_result(res, default_error_code="CSV_ANALYZER_ERROR")

    return _execute

ADAPTER_SPEC = AdapterSpec(
    tool_names=("csv_analyzer",),
    factory=create_csv_analyzer_executor,
    requires_context=frozenset({"settings"}),
)
```

### Step 4: Restart and Test
Restart the backend. The tool is auto-discovered. Use the UI to configure `max_rows`, then ask the agent to analyze a CSV file in the workspace.

---

## 10. Testing & Verification

### 1. Verify Discovery
```bash
curl http://localhost:8088/api/store/tools
```
Look for your `id` in the response.

### 2. Verify Settings UI
```bash
curl http://localhost:8088/api/store/tools/csv_analyzer/settings
```
Should return the schema.

### 3. Verify Injection
Add logging to your tool's `__init__`:
```python
import logging
logger = logging.getLogger(__name__)
logger.info("CsvAnalyzer initialized with max_rows=%d", self._max_rows)
```
Run the tool from chat and check the backend logs.

---

## 11. Best Practices

*   **Descriptions matter:** The LLM will fail to use your tool correctly if the `description` in the manifest is vague.
*   **Never trust arguments:** Always validate paths (`is_relative_to`) and URLs (`is_safe_url`).
*   **Defensive defaults:** Always set a `default` in your `settings.schema`. If a user setting is corrupt on disk, your `__init__` should fall back gracefully (use `try/except`).
*   **Atomic artifacts:** If your tool creates a file, add it to the `artifacts` list in `ToolResult`.
*   **Isolation:** Never use `os.chdir()`. Always pass absolute resolved paths derived from `self.workspace_root`.

---

## 12. Appendix: File Map

```text
backend/
├── omweb/
│   ├── tools/
│   │   └── builtins/
│   │       ├── csv_analyzer.json          ← 1. Manifest
│   │       └── ...
│   ├── adapters/
│   │   └── core_adapter_parts/
│   │       └── tools_adapters/
│   │           └── csv_analyzer_adapter.py ← 2. Adapter
│   └── routers/
│       └── store_rtr.py                    ← Serves settings API
│
└── peldrun/
    └── tools/
        └── builtins/
            └── csv_analyzer.py             ← 3. Core Logic
```

---

## End of Document
