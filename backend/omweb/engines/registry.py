"""
Central Authoritative Execution Engine Registry.

Maintains registered execution engines, enforces strict lookup policies,
and eliminates silent fallback masking.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import ExecutionEngine


class EngineNotFoundError(KeyError):
    """Raised when an requested execution engine is not registered."""

    def __init__(self, engine_id: str, available_engines: List[str]):
        super().__init__(
            f"Execution engine '{engine_id}' is not registered or unavailable. "
            f"Registered engines: {', '.join(available_engines)}"
        )
        self.engine_id = engine_id
        self.available_engines = available_engines


class EngineRegistry:
    """Registry managing the lifecycle and retrieval of ExecutionEngine instances."""

    def __init__(self) -> None:
        self._engines: Dict[str, ExecutionEngine] = {}
        self._aliases: Dict[str, str] = {}

    def register(self, engine: ExecutionEngine, aliases: Optional[List[str]] = None) -> None:
        """Register an execution engine with its primary identifier and optional aliases.

        Args:
            engine: The concrete ExecutionEngine instance.
            aliases: Optional list of alternate identifiers resolving to this engine.
        """
        primary_id = engine.engine_id.strip().lower()
        self._engines[primary_id] = engine

        if aliases:
            for alias in aliases:
                norm_alias = alias.strip().lower()
                self._aliases[norm_alias] = primary_id

    def get(self, engine_id: str) -> ExecutionEngine:
        """Resolve and retrieve an engine by ID or alias.

        Enforces authoritative lookup without silent fallbacks.

        Args:
            engine_id: Identifier or registered alias of the engine.

        Returns:
            The resolved ExecutionEngine.

        Raises:
            EngineNotFoundError: If the engine cannot be resolved.
        """
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
        """Query diagnostic health checks across all registered engines concurrently."""
        results: Dict[str, Dict[str, Any]] = {}
        for eng_id, eng in self._engines.items():
            try:
                results[eng_id] = await eng.health()
            except Exception as exc:
                results[eng_id] = {
                    "engine_id": eng_id,
                    "available": False,
                    "error": str(exc),
                }
        return results