"""
PELDRUN Universal Agent Bridge — Modular Package.

Dual-Engine dispatcher package delegating agent execution to native embedded
PELDRUN Core or legacy OpenManus via the omweb.engines abstraction layer.
"""

# ─────────────────────────────────────────────────────────────────────
# Environment variable hardening — MUST run before any heavy import
# (numpy, openblas, matplotlib, Qt) so they pick up single-thread mode.
# ─────────────────────────────────────────────────────────────────────
import os as _os

_os.environ["OPENBLAS_NUM_THREADS"] = "1"
_os.environ["OMP_NUM_THREADS"] = "1"
_os.environ["MKL_NUM_THREADS"] = "1"
_os.environ["NUMEXPR_NUM_THREADS"] = "1"
_os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
_os.environ["MPLBACKEND"] = "Agg"
_os.environ["QT_QPA_PLATFORM"] = "offscreen"

# ─────────────────────────────────────────────────────────────────────
# Public API re-exports — keeps `import agent_bridge` call-sites intact.
# ─────────────────────────────────────────────────────────────────────
from .config_loader import read_active_toml_config
from .core_engine import _run_peldrun_core_agent
from .dispatcher import run_direct_chat, run_instrumented
from .errors import format_smart_error
from .legacy_engine import (
    _run_legacy_openmanus_agent,
    inject_legacy_runtime_llm,
    scope_legacy_mcp_servers,
)
from .lmstudio import check_lmstudio_model_readiness
from .state import (
    active_tasks,
    current_active_job_id,
    human_answers,
    human_data,
    job_scoped_artifacts,
)
from .text_utils import sanitize_final_result_text

__all__ = [
    # Public entry points
    "run_instrumented",
    "run_direct_chat",
    # Utility functions
    "read_active_toml_config",
    "format_smart_error",
    "check_lmstudio_model_readiness",
    "sanitize_final_result_text",
    # Legacy engine runners (kept for internal backward compatibility)
    "_run_peldrun_core_agent",
    "_run_legacy_openmanus_agent",
    "scope_legacy_mcp_servers",
    "inject_legacy_runtime_llm",
    # Global state
    "human_answers",
    "human_data",
    "active_tasks",
    "current_active_job_id",
    "job_scoped_artifacts",
]