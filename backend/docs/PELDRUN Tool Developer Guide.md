Here is the complete, standalone, and comprehensive English documentation. You can save this as `PELDRUN_DEVELOPER_HANDBOOK.md`. It is designed to be a complete, end-to-end guide for any developer, regardless of their prior knowledge of PELDRUN.

---

# 🚀 The PELDRUN Tool Developer Handbook

**Version:** 2.0.0 (Master Edition)
**Target Audience:** Core Engineers, Third-Party Tool Developers, and AI Agents.
**Goal:** A comprehensive, end-to-end guide to building, testing, and publishing tools within the PELDRUN ecosystem.

---

## 📑 Table of Contents
1.  **The PELDRUN Mental Model** (How the AI uses tools)
2.  **The Three Pillars of a Tool** (Manifest, Logic, Adapter)
3.  **Scenario A: Building a Bundle Tool** (Recommended for external developers)
4.  **Scenario B: Building a Built-in Tool** (Core engineering)
5.  **The Manifest Reference** (Complete JSON schema)
6.  **The Settings Architecture** (Zero-Code UI)
7.  **Workspace & File Management** (Security and Sandboxing)
8.  **Network Security (SSRF Protection)**
9.  **The `ToolResult` Contract** (Error handling)
10. **Developer Checklist & Testing**

---

## 1. The PELDRUN Mental Model

Before writing any code, you must understand this fundamental rule: **The LLM (Large Language Model) does not execute code directly.**

The LLM reads a JSON manifest to discover what tools are available, what arguments they require, and when to use them. When the LLM decides to use a tool, it sends a request to the PELDRUN backend, which executes the Python code and returns the result to the LLM.

Therefore, a tool must be split into two distinct parts:
1.  **The Interface (JSON):** Explains to the LLM *how* and *when* to use the tool.
2.  **The Engine (Python):** The actual code that performs the work.

---

## 2. The Three Pillars of a Tool

Every tool in PELDRUN, regardless of its origin, is composed of three layers:

1.  **The Manifest (`manifest.json` or `<id>.json`):** The contract. It defines the tool's identity, its parameters (for the LLM), and its settings (for the User).
2.  **The Logic (`tool.py`):** The execution engine. It inherits from a base class and performs the actual work.
3.  **The Adapter (`*_adapter.py`):** The bridge. It injects the `workspace_root`, the `run_id`, and the `settings` into the Logic layer. *(Note: Bundles do not require a custom adapter).*

---

## 3. Scenario A: Building a Bundle Tool (Recommended)

**Bundles** are `.zip` files distributed via the PELDRUN Store. They are the recommended path for external developers because they support versioning, pip dependencies, and hot-reloading.

### Step 1: Create the Directory Structure
Create a new folder for your tool. It must contain at least a `manifest.json` and a `tool.py`.
```text
my_awesome_tool/
├── manifest.json     (The Interface)
├── tool.py           (The Engine)
└── requirements.txt  (Optional: pip dependencies)
```

### Step 2: Write the `manifest.json`
This file tells the LLM and the PELDRUN UI everything they need to know.
```json
{
  "schema_version": "1.0.0",
  "id": "my_awesome_tool",
  "version": "1.0.0",
  "name": "My Awesome Tool",
  "description": "CRITICAL: The LLM uses this to decide when to use the tool. Be specific. Example: 'Counts words in a text file. Use this when the user asks for word counts.'",
  "entry_point": "tool.py",
  "class_name": "MyAwesomeTool",
  "safety_level": "safe",

  "settings_schema": {
    "type": "object",
    "properties": {
      "ignore_case": {
        "type": "boolean",
        "default": true,
        "title": "Ignore Case",
        "description": "If true, treats 'Word' and 'word' as the same."
      }
    },
    "additionalProperties": false
  },

  "parameters": {
    "type": "object",
    "properties": {
      "filename": {
        "type": "string",
        "description": "The name of the file inside the workspace to analyze."
      }
    },
    "required": ["filename"]
  }
}
```

### Step 3: Write the `tool.py`
A Bundle tool **must** inherit from `SovereignBaseTool` and implement `set_settings()`.

```python
from typing import Any, Dict
from omweb.tools.base import SovereignBaseTool
from pathlib import Path

class MyAwesomeTool(SovereignBaseTool):
    name = "my_awesome_tool"
    description = "Counts words in a file."

    def __init__(self, workspace_root: str = "", **kwargs):
        super().__init__(workspace_root=workspace_root, **kwargs)
        # 1. Set default settings
        self._settings = {"ignore_case": True}

    def set_settings(self, settings: Dict[str, Any]) -> None:
        # 2. PELDRUN injects user settings here
        if isinstance(settings, dict):
            self._settings.update(settings)

    async def execute(self, filename: str, **kwargs) -> Any:
        # 3. Safely read the file from the workspace
        ws_path = Path(self.workspace_root).resolve()
        target_file = (ws_path / filename).resolve()

        # Security: Prevent path traversal
        if not target_file.is_relative_to(ws_path):
            return {"output": "Error: Path escapes workspace.", "is_error": True}

        if not target_file.exists():
            return {"output": f"Error: File {filename} not found.", "is_error": True}

        text = target_file.read_text(encoding="utf-8")

        # Apply settings
        if self._settings.get("ignore_case"):
            text = text.lower()

        count = len(text.split())

        # Return the result
        return {
            "output": f"The file '{filename}' contains {count} words.",
            "is_error": False,
            "exit_code": 0
        }
```

### Step 4: Package and Install
1. Compress the contents of `my_awesome_tool/` into a ZIP file (ensure `manifest.json` is at the root of the ZIP, not nested).
2. Go to the PELDRUN UI -> Store -> "Install Bundle".
3. Upload the ZIP. The tool is now available to the LLM.

---

## 4. Scenario B: Building a Built-in Tool (Core Engineering)

If you are adding a tool directly to the PELDRUN repository, you must separate the Logic (Core) from the Bridge (Adapter).

### Step 1: The Manifest
Add `my_tool.json` to `backend/omweb/tools/builtins/`. The structure is identical to a Bundle manifest, but without `entry_point` and `class_name`.

### Step 2: The Core Logic
Add `my_tool.py` to `backend/peldrun/tools/builtins/`. It inherits from `BaseTool` (not `SovereignBaseTool`) and returns a `ToolResult`.

```python
from peldrun.tools.base import BaseTool, ToolResult

class MyBuiltinTool(BaseTool):
    name = "my_builtin_tool"

    def __init__(self, workspace_root=None, settings=None, **kwargs):
        super().__init__(workspace_root=workspace_root, **kwargs)
        s = settings or {}
        self.my_setting = s.get("my_setting", "default")

    async def _arun(self, input_data: str, **kwargs) -> ToolResult:
        return ToolResult(output="Success", exit_code=0)
```

### Step 3: The Adapter
Add `my_tool_adapter.py` to `backend/omweb/adapters/core_adapter_parts/tools_adapters/`.

```python
from ._contract import AdapterSpec
from peldrun.tools.builtins.my_tool import MyBuiltinTool

def create_my_tool_executor(workspace_dir=None, *, settings=None, **kwargs):
    # Inject workspace and settings
    core = MyBuiltinTool(workspace_root=str(workspace_dir), settings=settings or {})

    async def _execute(input_data: str, **kw):
        return await core.aexecute(input_data=input_data, **kw)
    return _execute

# Define the AdapterSpec so PELDRUN discovers the tool
ADAPTER_SPEC = AdapterSpec(
    tool_names=("my_builtin_tool",),
    factory=create_my_tool_executor,
    requires_context=frozenset({"settings"}) # CRITICAL: Tells PELDRUN to inject settings
)
```

---

## 5. The Manifest Reference (Complete)

| Field | Required | Type | Description |
| :--- | :--- | :--- | :--- |
| `schema_version` | Yes | String | Always `"1.0.0"` for now. |
| `id` | Yes | String | Unique identifier. Lowercase, alphanumeric, underscores. |
| `version` | Yes (Bundle) | String | SemVer (e.g., `"1.0.0"`). |
| `name` | Yes | String | Human-readable name. |
| `description` | Yes | String | **Crucial.** The LLM reads this to decide when to use the tool. |
| `entry_point` | Yes (Bundle) | String | The Python file containing the logic (e.g., `"tool.py"`). |
| `class_name` | Yes (Bundle) | String | The name of the class inside `entry_point`. |
| `safety_level` | Yes | String | `"safe"`, `"read_only"`, or `"dangerous"`. |
| `settings_schema` | No | Object | JSON Schema defining configurable UI fields. |
| `parameters` | Yes | Object | JSON Schema defining arguments the LLM can pass. |

---

## 6. The Settings Architecture (Zero-Code UI)

PELDRUN allows users to configure tools via a dynamic UI.

1. **Declaration:** You define `settings_schema` in the manifest.
2. **UI Generation:** The frontend reads the schema and renders inputs (checkboxes, text fields, dropdowns).
3. **Validation:** When the user saves, the backend validates the data against your schema. If it fails, it returns `400 Bad Request`. Bad data never reaches the disk.
4. **Persistence:** Valid settings are written atomically to `_installs/<id>/settings.json` (Bundles) or `builtin_settings/<id>.json` (Built-ins).
5. **Injection:** When the tool runs, PELDRUN merges defaults with user values and injects them into your tool via `set_settings()` (Bundles) or `__init__` (Built-ins).

### Supported Schema Types
| JSON Schema Type | UI Widget |
| :--- | :--- |
| `string` | Text Input |
| `string` (with `enum`) | Dropdown / Select |
| `integer` / `number` | Number Input (with min/max) |
| `boolean` | Checkbox / Toggle |
| `array` | Textarea (one item per line) |
| `object` | Fieldset (nested fields) |

*Note: If `properties: {}` is empty, the "⚙️ Settings" button is hidden.*

---

## 7. Workspace & File Management (Security)

All tools operate inside an isolated **Workspace Sandbox** (`workspace_root`).

*   **NEVER** write files using absolute paths (e.g., `C:\temp\file.txt`).
*   **ALWAYS** use `Path(self.workspace_root) / "filename"`.
*   **Path Traversal Guard:** Always validate paths before writing.

```python
from pathlib import Path

async def execute(self, filename: str, content: str, **kwargs):
    ws_path = Path(self.workspace_root).resolve()
    target = (ws_path / filename).resolve()

    if not target.is_relative_to(ws_path):
        return {"output": "Security Error: Path escape detected.", "is_error": True}

    target.write_text(content)
    return {
        "output": f"Saved {filename}",
        "artifacts": [filename] # Registers in the UI Artifacts tab
    }
```

---

## 8. Network Security (SSRF Protection)

If your tool makes HTTP requests, you are vulnerable to Server-Side Request Forgery (SSRF). A malicious site could trick your tool into accessing internal metadata endpoints (like `169.254.169.254`).

PELDRUN provides a strict security module in `peldrun.tools.builtins.browser_interactive.security`.

**Usage:**
```python
from peldrun.tools.builtins.browser_interactive.security import (
    is_safe_url,
    make_route_handler
)

# 1. For standard HTTP requests:
if not is_safe_url(target_url):
    raise ValueError("Malicious URL blocked.")

# 2. For Playwright browsers (Layer 2 defense):
await context.route("**/*", make_route_handler())
```

---

## 9. The `ToolResult` Contract

Regardless of the tool type, the return value must be a canonical `ToolResult` (or a dict that maps to it).

```python
{
    "output": "The text or data returned to the LLM.",
    "is_error": False,          # True if the tool failed.
    "exit_code": 0,             # 0 for success, non-zero for failure.
    "artifacts": ["file.png"],  # Files to show in the UI.
    "metadata": {               # Optional telemetry.
        "execution_time_ms": 120
    }
}
```

---

## 10. Developer Checklist & Testing

Before publishing your tool, ensure:
- [ ] `manifest.json` has a clear, detailed `description` for the LLM.
- [ ] `id` is unique and stable (do not change it between versions).
- [ ] `settings_schema` has `"additionalProperties": false`.
- [ ] Every setting field has a `default` value.
- [ ] Bundles: The class implements `set_settings()`.
- [ ] Bundles: The class inherits from `SovereignBaseTool`.
- [ ] All file writes are scoped to `workspace_root`.
- [ ] Any outbound request validates the URL via `is_safe_url()`.
- [ ] If a package has pip dependencies, they are listed in `requirements.txt`.
