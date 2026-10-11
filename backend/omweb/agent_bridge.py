"""
backend/omweb/agent_bridge.py

PELDRUN Universal Agent Bridge — Thin Shell Entry Point.
================================================================

Authoritative runtime router dispatching execution to PELDRUN Core.
All legacy OpenManus execution pathways removed under PR 2.
"""

from __future__ import annotations

# Re-export public symbols so existing callers keep working unchanged.
from .agent_bridge_parts import (  # noqa: F401
    # Public entry points & validation
    run_instrumented,
    run_direct_chat,
    validate_requested_engine,
    # Utility functions
    read_active_toml_config,
    format_smart_error,
    check_lmstudio_model_readiness,
    sanitize_final_result_text,
    # Core engine runners
    _run_peldrun_core_agent,
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
    "validate_requested_engine",
    "read_active_toml_config",
    "format_smart_error",
    "check_lmstudio_model_readiness",
    "sanitize_final_result_text",
    "_run_peldrun_core_agent",
    "human_answers",
    "human_data",
    "active_tasks",
    "current_active_job_id",
    "job_scoped_artifacts",
]
