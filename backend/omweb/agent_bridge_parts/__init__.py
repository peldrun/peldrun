"""
backend/omweb/agent_bridge_parts/__init__.py

PELDRUN Universal Agent Bridge — Modular Package.

Dispatcher package delegating agent execution strictly to PELDRUN Core.
Legacy OpenManus runners removed under PR 2.
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
from .dispatcher import (
    run_direct_chat,
    run_instrumented,
    validate_requested_engine,
)
from .errors import format_smart_error
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
    # Public entry points & validation
    "run_instrumented",
    "run_direct_chat",
    "validate_requested_engine",
    # Utility functions
    "read_active_toml_config",
    "format_smart_error",
    "check_lmstudio_model_readiness",
    "sanitize_final_result_text",
    # Core engine runners
    "_run_peldrun_core_agent",
    # Global state
    "human_answers",
    "human_data",
    "active_tasks",
    "current_active_job_id",
    "job_scoped_artifacts",
]
