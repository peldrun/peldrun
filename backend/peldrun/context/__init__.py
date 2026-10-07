"""
peldrun Context Management Package.

Provides architectural primitives for decoupled context management,
including Sidecar persistence (session.context.json), token budgeting,
structurally lossless trimming, and historical compaction.
"""

from __future__ import annotations

from peldrun.context.budget import (
    TokenBudgetController,
    count_message_tokens,
    count_messages_total,
    count_tokens_heuristic,
)
from peldrun.context.builder import ContextManager, ContextManagerProtocol
from peldrun.context.compaction import (
    compact_sidecar_history,
    should_trigger_compaction,
    trim_structurally_lossless,
)
from peldrun.context.schema import (
    BudgetMetadata,
    BudgetRatios,
    BudgetSections,
    ContextSidecar,
    ConversationMessage,
    CurrentExecution,
    ExecutionStep,
    SummaryState,
)
from peldrun.context.sidecar import (
    build_context_sidecar,
    get_sidecar_path,
    load_context_sidecar,
    save_context_sidecar,
)

__all__ = [
    "BudgetMetadata",
    "BudgetRatios",
    "BudgetSections",
    "ContextManager",
    "ContextManagerProtocol",
    "ContextSidecar",
    "ConversationMessage",
    "CurrentExecution",
    "ExecutionStep",
    "SummaryState",
    "TokenBudgetController",
    "build_context_sidecar",
    "compact_sidecar_history",
    "count_message_tokens",
    "count_messages_total",
    "count_tokens_heuristic",
    "get_sidecar_path",
    "load_context_sidecar",
    "save_context_sidecar",
    "should_trigger_compaction",
    "trim_structurally_lossless",
]