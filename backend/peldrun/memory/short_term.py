"""
PELDRUN Core Short-Term Memory Subsystem.
Manages in-session dialogue turns, contextual sliding windows, token budget tracking,
and snapshot exports for agent execution loops.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from peldrun.llm.tokenizer import count_message_tokens, truncate_messages_sliding_window

logger = logging.getLogger("peldrun.memory.short_term")


class ShortTermMemoryConfig(BaseModel):
    """Configuration governing short-term memory capacity and pruning policies."""
    model_config = ConfigDict(extra="ignore")

    max_messages: int = Field(
        default=100,
        ge=1,
        description="Maximum number of historical messages retained before pruning"
    )
    max_context_tokens: int = Field(
        default=8192,
        ge=512,
        description="Maximum allowable context window ceiling in tokens"
    )
    reserve_generation_tokens: int = Field(
        default=1024,
        ge=128,
        description="Tokens reserved exclusively for LLM completion output"
    )
    model_name: str = Field(
        default="gpt-4",
        description="Target model identifier used for tokenizer calculations"
    )
    auto_prune: bool = Field(
        default=True,
        description="Automatically enforce sliding window when budget is exceeded"
    )


class ShortTermMemory:
    """
    In-memory transient dialogue buffer.
    Maintains ordered message history, tracks token consumption,
    and guarantees essential system and user instructions persist.
    """

    def __init__(
        self,
        config: Optional[ShortTermMemoryConfig] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        self.config = config or ShortTermMemoryConfig()
        self._messages: List[Dict[str, Any]] = []

        if system_prompt:
            self.set_system_message(system_prompt)

    @property
    def messages(self) -> List[Dict[str, Any]]:
        """Return a shallow copy of active messages."""
        return list(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def set_system_message(self, content: str) -> None:
        """
        Insert or replace the root system instruction at the beginning of memory.
        """
        system_msg = {"role": "system", "content": content}
        if self._messages and self._messages[0].get("role") == "system":
            self._messages[0] = system_msg
        else:
            self._messages.insert(0, system_msg)
        logger.debug("Configured root system message in short-term memory.")

    def get_system_message(self) -> Optional[Dict[str, Any]]:
        """Retrieve the primary system message if present."""
        if self._messages and self._messages[0].get("role") == "system":
            return dict(self._messages[0])
        return None

    def add_message(
        self,
        role: str,
        content: Optional[str] = None,
        name: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Append a structured message turn to short-term memory.
        Automatically checks and prunes history if auto_prune is enabled.
        """
        msg: Dict[str, Any] = {"role": role}

        if content is not None:
            msg["content"] = content
        if name is not None:
            msg["name"] = name
        if tool_calls is not None:
            msg["tool_calls"] = tool_calls
        if tool_call_id is not None:
            msg["tool_call_id"] = tool_call_id
        if metadata:
            msg["metadata"] = metadata

        self._messages.append(msg)

        if self.config.auto_prune:
            self._enforce_limits()

        return msg

    def add_messages(self, messages: List[Dict[str, Any]]) -> None:
        """Batch append multiple messages to memory."""
        for msg in messages:
            self._messages.append(dict(msg))

        if self.config.auto_prune:
            self._enforce_limits()

    def get_latest_user_message(self) -> Optional[Dict[str, Any]]:
        """Return the most recent message submitted by the user."""
        for msg in reversed(self._messages):
            if msg.get("role") == "user":
                return dict(msg)
        return None

    def get_budgeted_messages(self) -> List[Dict[str, Any]]:
        """
        Return a trimmed copy of messages guaranteed to fit within the configured token budget.
        """
        return truncate_messages_sliding_window(
            messages=self._messages,
            max_context_tokens=self.config.max_context_tokens,
            reserve_generation_tokens=self.config.reserve_generation_tokens,
            model=self.config.model_name,
        )

    def count_tokens(self) -> int:
        """Calculate total tokens consumed across all stored messages."""
        return count_message_tokens(self._messages, model=self.config.model_name)

    def _enforce_limits(self) -> None:
        """Prune messages if message count or context capacity exceeds limits."""
        # 1. Enforce max messages count limit
        if len(self._messages) > self.config.max_messages:
            excess = len(self._messages) - self.config.max_messages
            # Preserve system message if first
            if self._messages and self._messages[0].get("role") == "system":
                del self._messages[1 : 1 + excess]
            else:
                del self._messages[:excess]
            logger.debug("Pruned %d messages exceeding count limit.", excess)

        # 2. Enforce token budget ceiling
        current_tokens = self.count_tokens()
        limit = self.config.max_context_tokens - self.config.reserve_generation_tokens
        if current_tokens > limit:
            self._messages = truncate_messages_sliding_window(
                messages=self._messages,
                max_context_tokens=self.config.max_context_tokens,
                reserve_generation_tokens=self.config.reserve_generation_tokens,
                model=self.config.model_name,
            )

    def summarize_old_messages(self, summary_text: str, keep_last_n: int = 4) -> None:
        """
        Condense historical messages into a single summarized context message,
        retaining the root system prompt and the latest N interaction turns.
        """
        if len(self._messages) <= keep_last_n + 1:
            return

        system_msg = self.get_system_message()
        recent_turns = self._messages[-keep_last_n:] if keep_last_n > 0 else []

        summary_msg = {
            "role": "system",
            "content": f"[Context Summary of earlier conversation]:\n{summary_text.strip()}",
            "metadata": {"is_summary": True},
        }

        new_messages: List[Dict[str, Any]] = []
        if system_msg:
            new_messages.append(system_msg)
        new_messages.append(summary_msg)
        new_messages.extend(recent_turns)

        self._messages = new_messages
        logger.info("Summarized memory history. Retained system prompt and last %d turns.", keep_last_n)

    def clear(self, keep_system_message: bool = True) -> None:
        """Reset memory contents, optionally preserving the system prompt."""
        system_msg = self.get_system_message() if keep_system_message else None
        self._messages.clear()
        if system_msg:
            self._messages.append(system_msg)
        logger.debug("Short-term memory cleared.")

    def export_snapshot(self) -> Dict[str, Any]:
        """Serialize current short-term memory state into a JSON-compatible dict."""
        return {
            "config": self.config.model_dump(),
            "messages": copy.deepcopy(self._messages),
            "token_count": self.count_tokens(),
        }

    def load_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Restore memory state from a previously exported snapshot."""
        if "config" in snapshot:
            self.config = ShortTermMemoryConfig(**snapshot["config"])
        if "messages" in snapshot:
            self._messages = copy.deepcopy(snapshot["messages"])
        logger.info("Restored memory snapshot with %d messages.", len(self._messages))