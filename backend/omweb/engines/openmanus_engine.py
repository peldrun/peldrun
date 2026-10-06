"""
OpenManus External Legacy Execution Engine Adapter.

Encapsulates execution of the secondary OpenManus engine, providing runtime
instrumentation, backward compatibility, and clean environment isolation.
Guarantees resilient health diagnostics without throwing unhandled ModuleNotFoundError.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Dict

from .base import EngineRunContext, ExecutionEngine


class OpenManusEngine(ExecutionEngine):
    """External optional execution engine adapting OpenManus runtime."""

    engine_id: str = "openmanus"

    async def health(self) -> Dict[str, Any]:
        """Verify presence of OpenManus agent dependencies without global path pollution."""
        available = False
        entrypoint = "missing"

        try:
            # Check root package 'app' first to avoid ModuleNotFoundError when resolving submodules
            app_spec = importlib.util.find_spec("app")
            if app_spec is not None:
                peldrun_spec = importlib.util.find_spec("app.agent.peldrun")
                manus_spec = importlib.util.find_spec("app.agent.manus")
                if peldrun_spec is not None:
                    available = True
                    entrypoint = "app.agent.peldrun"
                elif manus_spec is not None:
                    available = True
                    entrypoint = "app.agent.manus"
        except (ModuleNotFoundError, ImportError, ValueError, AttributeError):
            available = False
            entrypoint = "missing"

        return {
            "engine_id": self.engine_id,
            "available": available,
            "is_legacy": True,
            "entrypoint": entrypoint,
        }

    async def run(self, context: EngineRunContext) -> None:
        """Dispatch task to legacy OpenManus runner.

        Imports the legacy runner lazily at invocation time to avoid circular
        import cycles between engines and agent_bridge subsystems.
        """
        print(
            f"\n[ENGINE OPENMANUS] >>> Executing Job: {context.job_id} via Legacy Engine <<<"
        )

        from omweb.agent_bridge_parts.legacy_engine import (
            _run_legacy_openmanus_agent,
        )

        await _run_legacy_openmanus_agent(
            job_id=context.job_id,
            prompt=context.prompt,
            agent_id=context.agent_id,
            active_llm=context.active_llm,
            model_name=context.model_name,
            provider_name=context.provider_name,
            project_dir=context.project_dir,
            chat_id=context.chat_id,
            manifest=context.manifest,
        )