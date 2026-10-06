"""
PELDRUN Core Long-Term Memory Subsystem.
Provides persistent storage, semantic/lexical keyword retrieval, categorization,
and contextual prompt injection across agent lifecycles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("peldrun.memory.long_term")


class MemoryEntry(BaseModel):
    """Structured representation of a single persisted memory unit."""
    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique identifier")
    content: str = Field(..., description="Textual body of memory, fact, or instruction")
    category: str = Field(default="general", description="Domain category: 'fact', 'preference', 'architecture', etc.")
    tags: List[str] = Field(default_factory=list, description="Keywords or topic markers")
    created_at: float = Field(default_factory=time.time, description="Creation Unix timestamp")
    updated_at: float = Field(default_factory=time.time, description="Last update Unix timestamp")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary auxiliary payload")


class LongTermMemoryConfig(BaseModel):
    """Configuration governing long-term memory persistence and capacity."""
    model_config = ConfigDict(extra="ignore")

    storage_path: Optional[str] = Field(
        default=None,
        description="Filesystem directory where long-term memory JSON is persisted"
    )
    filename: str = Field(
        default="long_term_memory.json",
        description="Target filename for stored memories"
    )
    max_entries: int = Field(
        default=2000,
        ge=10,
        description="Maximum entries stored before oldest/least relevant are trimmed"
    )
    auto_save: bool = Field(
        default=True,
        description="Persist to storage automatically upon insertion or modification"
    )


class LongTermMemory:
    """
    Persistent memory store for agents.
    Provides fast lookup, token overlap ranking, and formatted system prompt exports.
    """

    def __init__(
        self,
        config: Optional[LongTermMemoryConfig] = None,
        workspace_root: Optional[str] = None,
    ) -> None:
        self.config = config or LongTermMemoryConfig()
        if workspace_root and not self.config.storage_path:
            self.config.storage_path = str(Path(workspace_root) / ".peldrun")

        self._entries: Dict[str, MemoryEntry] = {}
        self._lock = asyncio.Lock()
        self._initialized: bool = False

    @property
    def file_path(self) -> Optional[Path]:
        """Resolve the full path to the persistence storage file."""
        if not self.config.storage_path:
            return None
        return Path(self.config.storage_path).resolve() / self.config.filename

    def __len__(self) -> int:
        return len(self._entries)

    async def ainitialize(self) -> None:
        """Bootstrap storage directory and restore stored memories from disk."""
        async with self._lock:
            if self._initialized:
                return

            if self.file_path:
                await asyncio.to_thread(self.file_path.parent.mkdir, parents=True, exist_ok=True)
                if self.file_path.exists():
                    await self._aload_from_disk()

            self._initialized = True
            logger.debug("LongTermMemory initialized with %d entries.", len(self._entries))

    def _sync_read_file(self, path: Path) -> List[Dict[str, Any]]:
        """Synchronously read and parse JSON file."""
        try:
            raw = path.read_text(encoding="utf-8")
            return json.loads(raw) if raw.strip() else []
        except Exception as ex:
            logger.warning("Failed to read memory file '%s': %s", path, ex)
            return []

    def _sync_write_file(self, path: Path, data: List[Dict[str, Any]]) -> None:
        """Synchronously write JSON data to file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    async def _aload_from_disk(self) -> None:
        """Internal asynchronous deserialization from disk."""
        if not self.file_path or not self.file_path.exists():
            return

        raw_list = await asyncio.to_thread(self._sync_read_file, self.file_path)
        for item in raw_list:
            try:
                entry = MemoryEntry(**item)
                self._entries[entry.id] = entry
            except Exception as ex:
                logger.debug("Skipping corrupted memory record: %s", ex)

    async def asave(self) -> None:
        """Persist current memory dictionary to disk asynchronously."""
        if not self.file_path:
            return

        data = [entry.model_dump() for entry in self._entries.values()]
        await asyncio.to_thread(self._sync_write_file, self.file_path, data)
        logger.debug("Persisted %d long-term memories to '%s'.", len(data), self.file_path)

    async def aadd_memory(
        self,
        content: str,
        category: str = "general",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEntry:
        """
        Record a new memory entry and optionally trigger auto-saving.
        """
        if not self._initialized:
            await self.ainitialize()

        clean_content = content.strip()
        if not clean_content:
            raise ValueError("Memory content cannot be empty.")

        entry = MemoryEntry(
            content=clean_content,
            category=category.lower().strip(),
            tags=[t.lower().strip() for t in (tags or [])],
            metadata=metadata or {},
        )

        async with self._lock:
            # Enforce max entries ceiling (drop oldest if exceeded)
            if len(self._entries) >= self.config.max_entries:
                oldest_id = min(self._entries.keys(), key=lambda k: self._entries[k].created_at)
                self._entries.pop(oldest_id, None)

            self._entries[entry.id] = entry

        if self.config.auto_save:
            await self.asave()

        logger.info("Added long-term memory [%s]: %s...", entry.category, entry.content[:40])
        return entry

    async def aget_memory(self, memory_id: str) -> Optional[MemoryEntry]:
        """Retrieve a specific memory by its unique identifier."""
        if not self._initialized:
            await self.ainitialize()
        return self._entries.get(memory_id)

    async def adelete_memory(self, memory_id: str) -> bool:
        """Remove a memory entry by ID and update storage."""
        if not self._initialized:
            await self.ainitialize()

        async with self._lock:
            removed = self._entries.pop(memory_id, None)

        if removed and self.config.auto_save:
            await self.asave()

        return removed is not None

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        """Extract alphanumeric lowercase tokens from text."""
        return set(re.findall(r"\w+", text.lower()))

    async def asearch(
        self,
        query: str,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 5,
    ) -> List[MemoryEntry]:
        """
        Search memory entries using lexical token overlap ranking.
        """
        if not self._initialized:
            await self.ainitialize()

        query_tokens = self._tokenize(query)
        target_tags = set(t.lower() for t in tags) if tags else set()
        candidates: List[tuple[float, MemoryEntry]] = []

        for entry in self._entries.values():
            if category and entry.category != category.lower().strip():
                continue

            if target_tags and not target_tags.intersection(set(entry.tags)):
                continue

            entry_tokens = self._tokenize(entry.content) | set(entry.tags)
            if not query_tokens:
                score = entry.updated_at
            else:
                overlap = len(query_tokens.intersection(entry_tokens))
                if overlap == 0 and not (target_tags and target_tags.intersection(set(entry.tags))):
                    continue
                score = overlap / max(1, len(query_tokens))

            candidates.append((score, entry))

        # Sort descending by score, then by update timestamp
        candidates.sort(key=lambda x: (x[0], x[1].updated_at), reverse=True)
        return [item[1] for item in candidates[:limit]]

    async def format_context_for_prompt(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 5,
    ) -> str:
        """
        Compile matching long-term memories into a structured Markdown string
        formatted specifically for agent context injection.
        """
        if query:
            memories = await self.asearch(query=query, category=category, limit=limit)
        else:
            all_mems = list(self._entries.values())
            all_mems.sort(key=lambda m: m.updated_at, reverse=True)
            memories = all_mems[:limit]

        if not memories:
            return ""

        lines = ["### Long-Term Memory & Project Knowledge:"]
        for idx, mem in enumerate(memories, start=1):
            tag_str = f" [tags: {', '.join(mem.tags)}]" if mem.tags else ""
            lines.append(f"{idx}. [{mem.category.upper()}]{tag_str} {mem.content}")

        return "\n".join(lines).strip()