"""
backend/omweb/agent_bridge_parts/core_engine.py

Compatibility shim wrapping PELDRUN Core native execution.
Hardened under PR 2 (Single Execution Authority):
- Deprecated as an independent execution loop.
- All invocations delegate strictly to PeldrunEngine and Core AgentRunner.
- Retained solely for backward compatibility with legacy bridge callers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from omweb.engines.base import EngineRunContext
from omweb.engines.peldrun_engine import PeldrunEngine

logger = logging.getLogger(__name__)


async def _run_peldrun_core_agent(
    job_id: str,
    prompt: str,
    agent_id: str,
    active_llm: Dict[str, Any],
    model_name: str,
    provider_name: str,
    project_dir: Path,
    chat_id: str,
    manifest: Dict[str, Any],
) -> None:
    """
    Execute an autonomous agent workflow via the canonical PeldrunEngine authority.
    Delegates immediately to PeldrunEngine, guaranteeing single AgentRunner authority.
    """
    logger.info("[LEGACY BRIDGE SHIM] Routing job %s to authoritative PeldrunEngine", job_id)
    context = EngineRunContext(
        job_id=job_id,
        prompt=prompt,
        agent_id=agent_id,
        active_llm=active_llm,
        model_name=model_name,
        provider_name=provider_name,
        project_dir=project_dir,
        chat_id=chat_id,
        manifest=manifest,
    )
    engine = PeldrunEngine()
    await engine.run(context)