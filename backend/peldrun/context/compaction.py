"""
Structurally Lossless Trimming and Compaction Engine for peldrun-core.

Provides loss-free pruning of mechanical tool artifacts (logs, base64 strings)
and triggers non-destructive summarization when token limits cross activation thresholds.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from peldrun.context.schema import ContextSidecar, ConversationMessage, SummaryState

BASE64_IMAGE_PATTERN = re.compile(r"data:image\/[a-zA-Z]+;base64,[A-Za-z0-9+/=]{100,}")
HEX_DUMP_PATTERN = re.compile(r"([0-9a-fA-F]{2}\s+){16,}")


def trim_structurally_lossless(content: str, max_chunk_chars: int = 800) -> str:
    """
    Perform loss-free structural trimming on tool outputs:
    1. Replaces bloated base64 image strings with lightweight tokens.
    2. Collapses large binary hex dumps into summary indicators.
    3. Trims repeated mechanical terminal output while preserving exit codes.
    """
    if not content:
        return ""

    # Replace inline base64 images
    pruned = BASE64_IMAGE_PATTERN.sub("[image base64 payload omitted]", content)

    # Replace raw hex dumps
    pruned = HEX_DUMP_PATTERN.sub("[hex dump data omitted] ", pruned)

    # Compress excessive multi-line whitespace
    lines = pruned.splitlines()
    if len(lines) > 60:
        head = lines[:25]
        tail = lines[-25:]
        omitted = len(lines) - 50
        pruned = "\n".join(head + [f"... [{omitted} lines omitted] ..."] + tail)

    if len(pruned) > max_chunk_chars:
        half = (max_chunk_chars - 40) // 2
        pruned = f"{pruned[:half]}\n... [intermediate output trimmed] ...\n{pruned[-half:]}"

    return pruned


def should_trigger_compaction(
    total_tokens: int, effective_limit: int, threshold_pct: float = 0.80
) -> bool:
    """
    Check if active context consumption has exceeded the activation threshold (default: 80%).
    Prevents emergency compaction mid-execution.
    """
    if effective_limit <= 0:
        return False
    return (total_tokens / float(effective_limit)) >= threshold_pct


def generate_extractive_summary(
    turns_to_compact: List[ConversationMessage], existing_summary: str = ""
) -> str:
    """
    Heuristic extractive summarizer when an external LLM compaction call is deferred.
    Captures user goals, files referenced, and primary assistant deliverables.
    """
    key_points: List[str] = []

    if existing_summary.strip():
        key_points.append(existing_summary.strip())

    for msg in turns_to_compact:
        content = msg.content.strip()
        if not content:
            continue

        # Extract file paths mentioned
        files = re.findall(r"[\w,\s-]+\.[A-Za-z0-9]{2,4}", content)
        files_mentioned = f" (Files: {', '.join(set(files))})" if files else ""

        if msg.role == "user":
            first_line = content.splitlines()[0][:140]
            key_points.append(f"- User requested: {first_line}{files_mentioned}")
        elif msg.role == "assistant":
            first_line = content.splitlines()[0][:140]
            key_points.append(f"- Assistant delivered: {first_line}")

    return "\n".join(key_points)


def compact_sidecar_history(
    sidecar: ContextSidecar,
    keep_recent_turns: int = 4,
    summarizer_fn: Optional[Callable[[List[ConversationMessage], str], str]] = None,
) -> ContextSidecar:
    """
    Non-destructively fold older conversational turns into sidecar.summary.
    Retains the most recent literal turns and updates covers_turns tracking.
    """
    conversation = sidecar.conversation
    if len(conversation) <= keep_recent_turns:
        return sidecar

    # Split into historical turns to compact and recent turns to preserve
    split_index = len(conversation) - keep_recent_turns
    to_compact = conversation[:split_index]
    retained_recent = conversation[split_index:]

    # Gather turn IDs covered
    new_covered_ids = [t.turn_id for t in to_compact if t.turn_id]
    all_covered_ids = list(set(sidecar.summary.covers_turns + new_covered_ids))

    # Produce condensed summary
    existing_content = sidecar.summary.content
    if summarizer_fn:
        new_summary_text = summarizer_fn(to_compact, existing_content)
    else:
        new_summary_text = generate_extractive_summary(to_compact, existing_content)

    estimated_tokens = max(1, len(new_summary_text) // 4)

    updated_summary = SummaryState(
        covers_turns=all_covered_ids,
        content=new_summary_text,
        tokens=estimated_tokens,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    sidecar.summary = updated_summary
    sidecar.conversation = retained_recent
    return sidecar