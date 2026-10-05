"""
PELDRUN Universal Agent Bridge — Thin Shell Entry Point.
================================================================

Dual-Engine runtime router dispatching execution to:
    - PELDRUN Core:  Native event-driven execution using EventEmitter
                     and typed lifecycle contracts.
    - OpenManus (Legacy): Backward-compatible execution using runtime
                          memory instrumentation.

------------------------------------------------------------------------
🗺️  CODE MAP — Where is everything now?
------------------------------------------------------------------------
The original monolithic body of this module has been split into the
sibling package ``agent_bridge_parts/``. Use the table below to jump
directly to the implementation of any piece of logic.

  ┌─────────────────────────────────────────────────────────────────┐
  │ Public API (what the rest of the app imports)                   │
  ├──────────────────────────────┬──────────────────────────────────┤
  │ ``run_instrumented``         │ agent_bridge_parts/dispatcher.py │
  │ ``run_direct_chat``          │ agent_bridge_parts/dispatcher.py │
  └──────────────────────────────┴──────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │ Utility / helper functions                                      │
  ├──────────────────────────────┬──────────────────────────────────┤
  │ ``read_active_toml_config``  │ agent_bridge_parts/config_loader.py │
  │ ``_err_card``                │ agent_bridge_parts/errors.py     │
  │ ``format_smart_error``       │ agent_bridge_parts/errors.py     │
  │ ``check_lmstudio_model_readiness`` │ agent_bridge_parts/lmstudio.py │
  │ ``sanitize_final_result_text``     │ agent_bridge_parts/text_utils.py │
  └──────────────────────────────┴──────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │ Engine runners (the heavy lifting)                              │
  ├──────────────────────────────┬──────────────────────────────────┤
  │ ``_run_peldrun_core_agent``  │ agent_bridge_parts/core_engine.py │
  │ ``scope_legacy_mcp_servers`` │ agent_bridge_parts/legacy_engine.py │
  │ ``inject_legacy_runtime_llm``│ agent_bridge_parts/legacy_engine.py │
  │ ``_run_legacy_openmanus_agent`` │ agent_bridge_parts/legacy_engine.py │
  └──────────────────────────────┴──────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │ Shared module-level state                                       │
  ├──────────────────────────────┬──────────────────────────────────┤
  │ ``human_answers``            │ agent_bridge_parts/state.py      │
  │ ``human_data``               │ agent_bridge_parts/state.py      │
  │ ``active_tasks``             │ agent_bridge_parts/state.py      │
  │ ``current_active_job_id``    │ agent_bridge_parts/state.py      │
  │ ``job_scoped_artifacts``     │ agent_bridge_parts/state.py      │
  └──────────────────────────────┴──────────────────────────────────┘

  Environment-variable hardening and ``inject_engine_to_syspath()``
  now live at the top of ``agent_bridge_parts/__init__.py`` so that
  they run exactly once, before any heavy submodule is imported.

------------------------------------------------------------------------
📌 HOW TO EDIT
------------------------------------------------------------------------
• Want to change error messages?     → agent_bridge_parts/errors.py
• Want to change LM Studio probing?  → agent_bridge_parts/lmstudio.py
• Want to change core engine flow?   → agent_bridge_parts/core_engine.py
• Want to change legacy engine flow? → agent_bridge_parts/legacy_engine.py
• Want to change dispatch/entry?     → agent_bridge_parts/dispatcher.py
• Want to change global state?       → agent_bridge_parts/state.py
• Want to change config resolution?  → agent_bridge_parts/config_loader.py
------------------------------------------------------------------------
"""

from __future__ import annotations

# Re-export everything so existing callers keep working unchanged.
from .agent_bridge_parts import (  # noqa: F401
    # Public entry points
    run_instrumented,
    run_direct_chat,
    # Utility functions
    read_active_toml_config,
    format_smart_error,
    check_lmstudio_model_readiness,
    sanitize_final_result_text,
    # Engine runners
    _run_peldrun_core_agent,
    _run_legacy_openmanus_agent,
    scope_legacy_mcp_servers,
    inject_legacy_runtime_llm,
    # Global state
    human_answers,
    human_data,
    active_tasks,
    current_active_job_id,
    job_scoped_artifacts,
)

__all__ = [
    "run_instrumented",
    "run_direct_chat",
    "read_active_toml_config",
    "format_smart_error",
    "check_lmstudio_model_readiness",
    "sanitize_final_result_text",
    "_run_peldrun_core_agent",
    "_run_legacy_openmanus_agent",
    "scope_legacy_mcp_servers",
    "inject_legacy_runtime_llm",
    "human_answers",
    "human_data",
    "active_tasks",
    "current_active_job_id",
    "job_scoped_artifacts",
]