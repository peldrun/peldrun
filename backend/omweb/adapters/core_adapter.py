"""
Adapter layer connecting PELDRUN Web Platform constructs to PELDRUN Core Runtime.
Provides real runtime tool bindings for shell, python, editor, resilient web search,
real browser inspection, and interactive human suspension loops.

=======================================================================
  MIGRATION NOTICE  --  This file is now a thin FACADE.
=======================================================================

The former monolithic implementation has been split into a modular
package to make it dramatically easier to navigate and edit individual
pieces of behavior.  This file re-exports every public symbol so that
external code using:

    from omweb.adapters.core_adapter import <anything>

continues to work exactly as before, with zero modifications required.

-----------------------------------------------------------------------
  WHERE EVERY PIECE OF LOGIC NOW LIVES
-----------------------------------------------------------------------

  Package root:
      D:\\AI\\peldrun\\backend\\omweb\\adapters\\core_adapter_parts\\

  ---------------------------------------------------------------------
  1.  terminal_utils.py
      -----------------------------------------------------------------
      * find_safe_bash_executable()
            -> Locates a real Git Bash / MSYS2 bash on Windows, skipping
               the System32 WSL stubs.  Edit this file when bash spawning
               is failing or when adding new candidate install paths.

      * decode_terminal_bytes(raw)
            -> Decodes raw stdout/stderr bytes, handling UTF-16 / UTF-8 /
               legacy Windows code pages.  Edit here to fix garbled output.

  ---------------------------------------------------------------------
  2.  schemas.py
      -----------------------------------------------------------------
      * CANONICAL_TOOL_SCHEMAS
            -> JSON schemas for every built-in tool (bash, shell_exec,
               python_execute, str_replace_editor, web_search,
               browser_use, ask_human).
            -> Edit here when adding a new built-in tool or changing an
               argument the LLM should see.

  ---------------------------------------------------------------------
  3.  executors.py
      -----------------------------------------------------------------
      * class RealToolExecutionFactory
            - create_bash_executor(workspace_root)
            - create_python_executor(workspace_root)
            - create_str_replace_editor_executor(workspace_root)
            - create_web_search_executor(workspace_root)
            - create_browser_executor(workspace_root)
            - create_human_input_executor()
            -> This is the file you edit 90% of the time when you want
               to change what a specific tool actually DOES.

  ---------------------------------------------------------------------
  4.  web_tool_adapter.py
      -----------------------------------------------------------------
      * class WebToolAdapter
            - __init__(name, description, parameters, executor, ...)
            - to_param() / to_openai_schema()
            - execute(**kwargs)
            -> Wraps a schema + executor pair, handles CWD switching and
               sync/async bridging.  Edit here to change how tools are
               invoked (e.g. sandboxing, retries, auditing).

  ---------------------------------------------------------------------
  5.  resolve_tool_runtime.py
      -----------------------------------------------------------------
      * resolve_tool_runtime(tool_id, tool_meta, workspace_root, registry)
            -> Maps a tool id to a concrete executor factory.  Edit here
               when adding a NEW tool id that needs to be bound.

  ---------------------------------------------------------------------
  6.  setup_core_environment.py
      -----------------------------------------------------------------
      * setup_core_environment(llm_provider, registry,
                               requested_tool_ids, workspace_root)
            -> Top-level entry point returning (LLM client, [adapters]).
            -> Edit here to change how the environment is bootstrapped.

=======================================================================
"""

from __future__ import annotations

# --- Re-export the modular package's public surface -----------------------
# This keeps `from omweb.adapters.core_adapter import X` working for every
# symbol that existed in the original monolithic file.
from .core_adapter_parts import (
    find_safe_bash_executable,
    decode_terminal_bytes,
    CANONICAL_TOOL_SCHEMAS,
    RealToolExecutionFactory,
    WebToolAdapter,
    resolve_tool_runtime,
    setup_core_environment,
)

# --- Preserve auxiliary imports that used to be module-level -------------
# (kept so existing code that imports them from here still works)
from peldrun.llm.client import AsyncLLMClient

try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry

try:
    from omweb.agent_bridge import human_answers, human_data, current_active_job_id
except ImportError:
    human_answers: dict = {}
    human_data: dict = {}
    current_active_job_id: dict = {}


__all__ = [
    # Public API (unchanged from the original file)
    "find_safe_bash_executable",
    "decode_terminal_bytes",
    "CANONICAL_TOOL_SCHEMAS",
    "RealToolExecutionFactory",
    "WebToolAdapter",
    "resolve_tool_runtime",
    "setup_core_environment",
    # Re-exported auxiliaries (for backward compatibility)
    "AsyncLLMClient",
    "ToolRegistry",
    "tool_registry",
    "human_answers",
    "human_data",
    "current_active_job_id",
]