"""
PELDRUN Universal Agent Bridge — Modular Package.

This package is a modular refactor of the original ``agent_bridge.py``
monolithic file. Code has been split into focused submodules to make
navigation and editing easier, WITHOUT any behavioral change.

Layout map
----------
state.py             → Global shared runtime dictionaries.
config_loader.py     → Reads config.toml from the active engine.
errors.py            → Smart error card formatting utilities.
lmstudio.py          → LM Studio readiness probe.
text_utils.py        → Final result sanitization helpers.
core_engine.py       → PELDRUN Core engine runner.
legacy_engine.py     → OpenManus legacy engine runner + helpers.
dispatcher.py        → Public entry points (run_instrumented, run_direct_chat).

Public API
----------
run_instrumented  : Dispatch an agent job (core or legacy engine).
run_direct_chat   : Dispatch a plain chat completion job.
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
# Engine sys.path injection — MUST happen before any `peldrun.*` import
# is attempted at runtime. Safe to call at package import time.
# ─────────────────────────────────────────────────────────────────────
from omweb.engine_resolver import inject_engine_to_syspath as _inject_engine
_inject_engine()

# ─────────────────────────────────────────────────────────────────────
# Public API re-exports — keeps ``import agent_bridge`` call-sites intact.
# ─────────────────────────────────────────────────────────────────────
from .dispatcher import run_instrumented, run_direct_chat
from .config_loader import read_active_toml_config
from .errors import format_smart_error
from .lmstudio import check_lmstudio_model_readiness
from .text_utils import sanitize_final_result_text
from .core_engine import _run_peldrun_core_agent
from .legacy_engine import (
    _run_legacy_openmanus_agent,
    scope_legacy_mcp_servers,
    inject_legacy_runtime_llm,
)
from .state import (
    human_answers,
    human_data,
    active_tasks,
    current_active_job_id,
    job_scoped_artifacts,
)

__all__ = [
    # Public entry points
    "run_instrumented",
    "run_direct_chat",
    # Utility functions
    "read_active_toml_config",
    "format_smart_error",
    "check_lmstudio_model_readiness",
    "sanitize_final_result_text",
    # Engine runners (kept for backward-compat; treat as internal)
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