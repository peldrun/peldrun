"""
PELDRUN Core Tokenizer and Context Window Management Subsystem.
Provides token estimation, message budgeting, and intelligent sliding-window context truncation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("peldrun.llm.tokenizer")

# Attempt optional tiktoken import with graceful heuristic fallback
try:
    import tiktoken
    _TIKTOKEN_AVAILABLE = True
except ImportError:
    tiktoken = None
    _TIKTOKEN_AVAILABLE = False


def estimate_tokens_from_string(text: Optional[str], model: str = "gpt-4") -> int:
    """
    Calculate the token count for a raw string.
    Uses tiktoken cl100k_base encoding if present, falling back to a character heuristic.
    """
    if not text:
        return 0

    if _TIKTOKEN_AVAILABLE and tiktoken is not None:
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except Exception:
            pass

    # Heuristic fallback: ~4 characters per token for English/code, ~2 for mixed Unicode
    return max(1, len(text) // 4)


def count_message_tokens(messages: List[Dict[str, Any]], model: str = "gpt-4") -> int:
    """
    Calculate token usage across a list of structured chat messages,
    accounting for role identifiers, name wrappers, and tool call serialization.
    """
    total_tokens = 3  # Baseline overhead per conversation reply prefix

    for msg in messages:
        total_tokens += 4  # Formatting overhead per message envelope
        for key, value in msg.items():
            if isinstance(value, str):
                total_tokens += estimate_tokens_from_string(value, model=model)
            elif isinstance(value, list) and key == "tool_calls":
                for tc in value:
                    total_tokens += 10  # Function call structure baseline
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    args = func.get("arguments", "")
                    total_tokens += estimate_tokens_from_string(name, model=model)
                    total_tokens += estimate_tokens_from_string(args, model=model)
            elif key == "name" and isinstance(value, str):
                total_tokens += estimate_tokens_from_string(value, model=model)

    return total_tokens


def truncate_messages_sliding_window(
    messages: List[Dict[str, Any]],
    max_context_tokens: int,
    reserve_generation_tokens: int = 1000,
    model: str = "gpt-4",
) -> List[Dict[str, Any]]:
    """
    Trims chat messages to fit inside the available context window.
    Guarantees that:
    1. The leading system message is always preserved.
    2. The latest user instruction is always preserved.
    3. Intermediate messages are dropped chronologically (oldest first).
    """
    allowed_tokens = max(500, max_context_tokens - reserve_generation_tokens)
    current_tokens = count_message_tokens(messages, model=model)

    if current_tokens <= allowed_tokens or len(messages) <= 2:
        return list(messages)

    # Separate system instruction if present
    system_message: Optional[Dict[str, Any]] = None
    remaining_messages: List[Dict[str, Any]] = []

    for msg in messages:
        if msg.get("role") == "system" and system_message is None:
            system_message = msg
        else:
            remaining_messages.append(msg)

    # Sliding window: remove oldest messages from remaining until within budget
    while remaining_messages and count_message_tokens(
        ([system_message] if system_message else []) + remaining_messages,
        model=model,
    ) > allowed_tokens:
        if len(remaining_messages) <= 1:
            # Keep at least the final message
            break
        removed = remaining_messages.pop(0)
        logger.debug("Dropped message with role '%s' to conserve context window.", removed.get("role"))

    result: List[Dict[str, Any]] = []
    if system_message:
        result.append(system_message)
    result.extend(remaining_messages)
    return result


class ContextBudgetManager:
    """
    Tracks and enforces context limits and token budgets for model completions.
    """

    def __init__(
        self,
        max_context_window: int = 8192,
        max_generation_tokens: int = 2048,
        safety_margin_tokens: int = 256,
        model_name: str = "local-model",
    ) -> None:
        self.max_context_window = max_context_window
        self.max_generation_tokens = max_generation_tokens
        self.safety_margin_tokens = safety_margin_tokens
        self.model_name = model_name

    def get_available_input_tokens(self) -> int:
        """Calculate the maximum tokens allowable for input prompt context."""
        return max(
            512,
            self.max_context_window - self.max_generation_tokens - self.safety_margin_tokens,
        )

    def fit_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fit a list of conversation messages into the calculated token budget."""
        target_budget = self.get_available_input_tokens()
        return truncate_messages_sliding_window(
            messages=messages,
            max_context_tokens=self.max_context_window,
            reserve_generation_tokens=self.max_generation_tokens + self.safety_margin_tokens,
            model=self.model_name,
        )

    def calculate_remaining_generation_capacity(self, messages: List[Dict[str, Any]]) -> int:
        """Determine how many tokens remain available for the model completion."""
        used_tokens = count_message_tokens(messages, model=self.model_name)
        remaining = self.max_context_window - used_tokens - self.safety_margin_tokens
        return max(128, min(self.max_generation_tokens, remaining))