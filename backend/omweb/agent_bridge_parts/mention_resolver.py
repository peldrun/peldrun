"""
backend/omweb/agent_bridge_parts/mention_resolver.py

Resolves user-provided file references and injects their contents as trusted
LLM context, before the agent engine builds its messages.

Two entry points:

1. Structured refs from the frontend composer.
   The user selects a file from the `@` typeahead menu; the frontend sends
   a list of `{relPath, fileName, origin}` objects in `context_refs`.

2. Inline legacy `@filename` tokens in the raw prompt.
   Retained as a defensive fallback for:
     - Users who type `@file.md` manually
     - Prompts pasted from other apps
     - Old chats replayed without structured refs

Security invariants:
- Every resolved path MUST be strictly inside the workspace root.
- Symlink escapes, `..` traversal, and absolute paths are rejected.
- Files larger than `MAX_PER_FILE` are truncated with a clear marker.
- Total injected content is capped at `MAX_TOTAL` to protect the context window.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("omweb.agent_bridge.mention_resolver")


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# Maximum size for a single file's content to be inlined.
MAX_PER_FILE_BYTES = 200 * 1024  # 200 KiB

# Maximum total size of all inlined content per turn.
MAX_TOTAL_BYTES = 1024 * 1024  # 1 MiB

# Regex for legacy `@filename` tokens. Matches `@foo/bar.ext`.
# Does NOT match email addresses because we require a dot-extension and no @
# at the start of the token beyond the initial marker.
_INLINE_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])@([A-Za-z0-9_][A-Za-z0-9_\-./]*\.[A-Za-z0-9]{1,10})"
)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _safe_resolve(workspace_dir: Path, rel: str) -> Optional[Path]:
    """
    Resolve a workspace-relative path and enforce strict containment.

    Handles THREE forms of `rel`:
      1. Bare workspace-relative:     "report.md" or "sub/dir/report.md"
      2. Full storage path:           "storage/chats/<cid>/files/report.md"
      3. Legacy workspace prefix:     "workspace/report.md"

    The full storage path form is what `/api/files` returns in `path`, and
    what the frontend naively forwards. We strip the prefix so that
    `rel` is treated as relative to the current chat's workspace root.

    Returns None if the path is invalid, absolute, or escapes the workspace.
    """
    if not rel or not isinstance(rel, str):
        return None

    cleaned = rel.strip().replace("\\", "/")
    if not cleaned:
        return None

    # Build candidate relative paths to try, in priority order.
    candidates: List[str] = [cleaned]

    # If the path contains "/files/", take everything AFTER it.
    #   "storage/chats/c1/files/sub/report.md" -> "sub/report.md"
    if "/files/" in cleaned:
        candidates.append(cleaned.split("/files/", 1)[1])

    # If the path starts with "workspace/", strip it.
    #   "workspace/report.md" -> "report.md"
    if cleaned.startswith("workspace/"):
        candidates.append(cleaned[len("workspace/"):])

    # Also try just the basename as a last resort for safety.
    # This helps when the file is at the workspace root.
    basename = cleaned.rsplit("/", 1)[-1]
    if basename and basename != cleaned:
        candidates.append(basename)

    workspace_root = workspace_dir.resolve()

    for cand in candidates:
        if not cand:
            continue
        # Reject absolute paths and traversal attempts.
        if cand.startswith("/") or ".." in cand.split("/"):
            continue
        try:
            target = (workspace_root / cand).resolve()
            # Enforce strict containment.
            target.relative_to(workspace_root)
            if target.is_file():
                return target
        except (ValueError, OSError):
            continue

    return None


# ---------------------------------------------------------------------------
# Structured refs (from frontend `context_refs`)
# ---------------------------------------------------------------------------

def resolve_context_refs(
    refs: Optional[List[Dict[str, Any]]],
    workspace_dir: Path,
) -> List[Dict[str, Any]]:
    """
    Validate and normalize a list of structured context references.

    Input shape (from the frontend):
        [{"relPath": "...", "fileName": "...", "origin": "..."}]

    Output shape (canonical, deduped, validated):
        [{"relPath": "...", "fileName": "...", "sizeBytes": 1234}]

    Invalid or non-existent refs are logged and dropped.

    NOTE: `relPath` in the output is preserved as received from the client.
    Downstream consumers (inject_referenced_files) recompute the SHORT
    canonical path relative to the workspace root when building the
    <referenced_files> block, so the agent's tools always receive a path
    they can resolve.
    """
    if not refs:
        return []

    validated: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for raw in refs:
        if not isinstance(raw, dict):
            continue

        # Accept both camelCase (from frontend) and snake_case (defensive).
        rel = (
            raw.get("relPath")
            or raw.get("rel_path")
            or ""
        )
        rel = rel.strip() if isinstance(rel, str) else ""
        if not rel or rel in seen:
            continue

        target = _safe_resolve(workspace_dir, rel)
        if target is None or not target.is_file():
            logger.warning(
                "[MENTION] Structured ref rejected (not found or unsafe): %r", rel
            )
            continue

        seen.add(rel)
        validated.append({
            "relPath": rel,
            "fileName": raw.get("fileName") or target.name,
            "sizeBytes": target.stat().st_size,
        })

    return validated


# ---------------------------------------------------------------------------
# Inline legacy `@filename` tokens
# ---------------------------------------------------------------------------

def resolve_inline_mentions(
    prompt: str,
    workspace_dir: Path,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Extract legacy `@filename` tokens from the prompt, validate them against
    the workspace, and rewrite the prompt to remove the `@` marker.

    Tokens that do not resolve to a real file are LEFT UNCHANGED so the LLM
    can decide what to do (usually it will ask the user).

    Returns:
        (cleaned_prompt, resolved_refs)
    """
    if not prompt or "@" not in prompt:
        return prompt, []

    resolved: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _repl(match: re.Match[str]) -> str:
        rel = match.group(1)
        if rel in seen:
            return f"`{rel}`"

        target = _safe_resolve(workspace_dir, rel)
        if target is None or not target.is_file():
            # Leave the token as-is; not our job to guess.
            return match.group(0)

        seen.add(rel)
        resolved.append({
            "relPath": rel,
            "fileName": target.name,
            "sizeBytes": target.stat().st_size,
        })
        # Rewrite to a backticked filename for consistency with new-style.
        return f"`{rel}`"

    cleaned = _INLINE_MENTION_RE.sub(_repl, prompt)
    return cleaned, resolved


# ---------------------------------------------------------------------------
# Content injection
# ---------------------------------------------------------------------------

def inject_referenced_files(
    prompt: str,
    refs: List[Dict[str, Any]],
    workspace_dir: Path,
    max_per_file: int = MAX_PER_FILE_BYTES,
    max_total: int = MAX_TOTAL_BYTES,
) -> str:
    """
    Prepend a structured block of referenced file contents to the prompt.

    The block is enclosed in `<referenced_files>` tags so the LLM can
    distinguish authoritative workspace content from user instructions
    and from any untrusted external data (e.g. web search results).

    IMPORTANT — path canonicalization:
        The `path` attribute in each <file> element is computed as
        `target.relative_to(workspace_dir)`, i.e. the SHORT canonical path
        that the agent's tools (str_replace_editor, file_ops, ...) expect.
        This is critical because the raw `relPath` coming from the frontend
        may contain a full storage prefix like
        `storage/chats/<cid>/files/seafood_report.md`, which would NOT be
        resolvable by tools that are already rooted at the workspace dir.

    Size limits:
    - Per-file: files larger than `max_per_file` are truncated.
    - Cumulative: once `max_total` bytes have been inlined, remaining files
      are emitted as truncated markers only.

    If no files could be inlined, the prompt is returned unchanged.
    """
    if not refs:
        return prompt

    workspace_root = workspace_dir.resolve()
    blocks: List[str] = []
    total_bytes = 0

    for ref in refs:
        rel = ref.get("relPath", "")
        target = _safe_resolve(workspace_dir, rel)
        if target is None or not target.is_file():
            continue

        # [FIX-4.3] Compute the SHORT canonical path relative to workspace_dir.
        # This is what agent tools expect — not the full storage path.
        try:
            canonical_rel = str(
                target.relative_to(workspace_root)
            ).replace("\\", "/")
        except ValueError:
            # Should never happen because _safe_resolve already enforced
            # containment, but we guard defensively.
            continue

        size = target.stat().st_size

        # Refuse to inline if we've already hit the cumulative cap.
        if total_bytes >= max_total:
            blocks.append(
                f'<file path="{canonical_rel}" truncated="true" '
                f'reason="total_cap_reached" size_bytes="{size}"/>'
            )
            continue

        # Refuse to inline oversized single files.
        if size > max_per_file:
            blocks.append(
                f'<file path="{canonical_rel}" truncated="true" '
                f'reason="file_too_large" size_bytes="{size}" '
                f'max_per_file="{max_per_file}"/>'
            )
            continue

        # Respect the remaining cumulative budget.
        remaining = max_total - total_bytes
        if size > remaining:
            blocks.append(
                f'<file path="{canonical_rel}" truncated="true" '
                f'reason="budget_exhausted" size_bytes="{size}" '
                f'remaining="{remaining}"/>'
            )
            continue

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("[MENTION] Failed to read %r: %s", rel, exc)
            blocks.append(
                f'<file path="{canonical_rel}" error="read_failed" '
                f'size_bytes="{size}"/>'
            )
            continue

        total_bytes += size
        blocks.append(
            f'<file path="{canonical_rel}" size_bytes="{size}">\n'
            f'{content}\n</file>'
        )

    if not blocks:
        return prompt

    directive = (
        "The user referenced the following files in the workspace. "
        "Treat their contents as authoritative context for this turn. "
        "When editing, call `str_replace_editor` with the exact relative "
        "path shown in the `path` attribute — do NOT create new files with "
        "these names, and do NOT include the backticks in the path.\n\n"
        "<referenced_files>\n"
        + "\n".join(blocks)
        + "\n</referenced_files>\n\n"
        "User request:\n"
    )
    return directive + prompt


__all__ = [
    "resolve_context_refs",
    "resolve_inline_mentions",
    "inject_referenced_files",
]
