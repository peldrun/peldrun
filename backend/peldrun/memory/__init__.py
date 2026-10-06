"""
PELDRUN Core Memory Subsystem.
Exports short-term conversational context buffers, persistent long-term storage models,
and a unified MemoryManager coordinator.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from peldrun.memory.long_term import (
    LongTermMemory,
    LongTermMemoryConfig,
    MemoryEntry,
)
from peldrun.memory.short_term import (
    ShortTermMemory,
    ShortTermMemoryConfig,
)

logger = logging.getLogger("peldrun.memory")


class MemoryManager:
    """
    Unified memory coordinator orchestrating both transient dialogue turns (short-term)
    and persistent project knowledge bases (long-term).
    """

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        system_prompt: Optional[str] = None,
        short_term_config: Optional[ShortTermMemoryConfig] = None,
        long_term_config: Optional[LongTermMemoryConfig] = None,
    ) -> None:
        self.workspace_root = workspace_root
        self._raw_system_prompt = system_prompt or "You are PELDRUN, an autonomous AI engineer."
        self.short_term = ShortTermMemory(
            config=short_term_config,
            system_prompt=self._raw_system_prompt,
        )
        self.long_term = LongTermMemory(
            config=long_term_config,
            workspace_root=workspace_root,
        )
        self._initialized: bool = False

    async def ainitialize(self) -> None:
        """Initialize persistent storage directories and indexes."""
        if self._initialized:
            return

        await self.long_term.ainitialize()
        self._initialized = True
        logger.debug("MemoryManager initialized for workspace: %s", self.workspace_root)

    def set_system_prompt(self, prompt: str) -> None:
        """Update base system instruction and synchronize with short-term buffer."""
        self._raw_system_prompt = prompt
        self.short_term.set_system_message(prompt)

    async def compile_system_prompt(self, query: Optional[str] = None, max_memories: int = 5) -> str:
        """
        Synthesize the effective system prompt by appending relevant long-term memories
        retrieved dynamically based on the current user task query.
        """
        if not self._initialized:
            await self.ainitialize()

        memory_context = await self.long_term.format_context_for_prompt(
            query=query,
            limit=max_memories,
        )

        if memory_context:
            full_prompt = f"{self._raw_system_prompt.strip()}\n\n{memory_context}"
        else:
            full_prompt = self._raw_system_prompt.strip()

        # Update the short-term memory head
        self.short_term.set_system_message(full_prompt)
        return full_prompt

    def export_snapshot(self) -> Dict[str, Any]:
        """Export serialized representation of active conversational state."""
        return {
            "workspace_root": self.workspace_root,
            "system_prompt": self._raw_system_prompt,
            "short_term": self.short_term.export_snapshot(),
            "long_term_count": len(self.long_term),
        }

    def load_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Restore conversational state from snapshot."""
        if "workspace_root" in snapshot:
            self.workspace_root = snapshot["workspace_root"]
        if "system_prompt" in snapshot:
            self._raw_system_prompt = snapshot["system_prompt"]
        if "short_term" in snapshot:
            self.short_term.load_snapshot(snapshot["short_term"])
        logger.info("Restored MemoryManager conversational snapshot.")


__all__ = [
    # Short-term
    "ShortTermMemory",
    "ShortTermMemoryConfig",
    # Long-term
    "LongTermMemory",
    "LongTermMemoryConfig",
    "MemoryEntry",
    # Unified Manager
    "MemoryManager",
]