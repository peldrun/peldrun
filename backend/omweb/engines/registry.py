"""
backend/omweb/engines/registry.py

Central Authoritative Execution Engine Registry.

Maintains registered execution engines, enforces strict lookup policies,
eliminates silent fallback masking, and provides non-blocking concurrent health diagnostics.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from .base import ExecutionEngine


class EngineNotFoundError(KeyError):
    """Raised when a requested execution engine is not registered."""

    def __init__(self, engine_id: str, available_engines: List[str]):
        super().__init__(
            f"Execution engine '{engine_id}' is not registered or unavailable. "
            f"Registered engines: {', '.join(available_engines)}"
        )
        self.engine_id = engine_id
        self.available_engines = available_engines


class EngineRegistry:
    """Registry managing the lifecycle, retrieval, and health diagnostics of ExecutionEngines."""

    def __init__(self) -> None:
        self._engines: Dict[str, ExecutionEngine] = {}
        self._aliases: Dict[str, str] = {}

    def register(self, engine: ExecutionEngine, aliases: Optional[List[str]] = None) -> None:
        """Register an execution engine with its primary identifier and optional aliases."""
        primary_id = engine.engine_id.strip().lower()
        self._engines[primary_id] = engine

        if aliases:
            for alias in aliases:
                norm_alias = alias.strip().lower()
                self._aliases[norm_alias] = primary_id

    def get(self, engine_id: str) -> ExecutionEngine:
        """Resolve and retrieve an engine by ID or alias with strict fail-fast semantics."""
        normalized_id = (engine_id or "").strip().lower()
        resolved_key = self._aliases.get(normalized_id, normalized_id)

        if resolved_key not in self._engines:
            available = self.list_engines()
            raise EngineNotFoundError(normalized_id, available)

        return self._engines[resolved_key]

    def has(self, engine_id: str) -> bool:
        """Check whether an engine or alias exists in the registry."""
        normalized_id = (engine_id or "").strip().lower()
        resolved_key = self._aliases.get(normalized_id, normalized_id)
        return resolved_key in self._engines

    def list_engines(self) -> List[str]:
        """Return a sorted list of primary registered engine IDs."""
        return sorted(list(self._engines.keys()))

    async def health_all(self) -> Dict[str, Dict[str, Any]]:
        """
        Query diagnostic health checks across all registered engines concurrently.
        
        Uses asyncio.gather to prevent slow or unreachable engines from
        blocking diagnostic responses sequentially.
        """
        async def _check_single(eng_id: str, eng: ExecutionEngine) -> Tuple[str, Dict[str, Any]]:
            try:
                res = await eng.health()
                return eng_id, res
            except Exception as exc:
                return eng_id, {
                    "engine_id": eng_id,
                    "available": False,
                    "error": str(exc),
                }

        if not self._engines:
            return {}

        tasks = [
            _check_single(eng_id, eng)
            for eng_id, eng in self._engines.items()
        ]
        completed = await asyncio.gather(*tasks)
        return dict(completed)