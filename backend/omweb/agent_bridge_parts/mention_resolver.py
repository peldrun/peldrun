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
import mimetypes
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

# Maximum raw PDF size that we are willing to extract text from. This
# is intentionally separate from MAX_PER_FILE_BYTES because raw PDF
# size does NOT correlate with extracted text size — a 5 MiB PDF may
# yield 20 KiB of text or vice versa. The goal is only to skip
# pathological cases where loading the PDF into memory would be unsafe.
MAX_PDF_RAW_BYTES = 20 * 1024 * 1024  # 20 MiB

# Regex for legacy `@filename` tokens. Matches `@foo/bar.ext`.
# Does NOT match email addresses because we require a dot-extension and no @
# at the start of the token beyond the initial marker.
_INLINE_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])@([A-Za-z0-9_][A-Za-z0-9_\-./]*\.[A-Za-z0-9]{1,10})"
)


# ---------------------------------------------------------------------------
# File type classification + per-type content extraction
# ---------------------------------------------------------------------------
#
# Before opening a file, we classify it into one of three buckets:
#   * Text-like — read as UTF-8 (source code, markdown, JSON, CSV, ...)
#   * PDF       — extract via pypdf / PyPDF2 / pdfplumber
#   * Binary    — emit a metadata marker, NEVER inline bytes
#
# Extensions are the primary signal. Files without a recognized
# extension fall back to null-byte content sniffing, which is the
# canonical signature of binary content.
#
# The whitelist is deliberately conservative — every additional
# extension in `_TEXT_LIKE_EXTENSIONS` that turns out to be binary
# will silently corrupt the prompt with U+FFFD replacement chars.
#
# The blacklist (`_BINARY_EXTENSIONS`) does not need to be exhaustive:
# it is only a fast path for common binaries. Files without a
# recognized binary extension are still caught by content sniffing.

_TEXT_LIKE_EXTENSIONS: frozenset = frozenset({
    # Plain text / logs / config
    ".txt", ".text", ".log", ".md", ".markdown", ".rst",
    ".ini", ".cfg", ".conf", ".config", ".properties",
    ".toml", ".yaml", ".yml", ".json", ".jsonl", ".ndjson",
    ".env", ".gitignore", ".gitattributes", ".dockerignore",
    ".editorconfig", ".npmrc", ".yarnrc",
    # Web / markup
    ".xml", ".html", ".htm", ".xhtml",
    ".css", ".scss", ".sass", ".less",
    ".svg",
    # Data
    ".csv", ".tsv", ".psv",
    # Programming languages
    ".py", ".pyi", ".pyx",
    ".rb", ".rake", ".gemspec",
    ".php", ".phtml",
    ".go", ".mod", ".sum",
    ".rs",
    ".java", ".kt", ".kts", ".scala", ".groovy",
    ".c", ".h", ".cpp", ".cxx", ".hpp", ".hxx", ".cc", ".hh", ".m", ".mm",
    ".cs", ".fs", ".fsx", ".vb",
    ".swift", ".dart", ".lua", ".pl", ".pm", ".r", ".R",
    # JS / TS ecosystem
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".vue", ".svelte", ".astro",
    # Shell
    ".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh",
    ".ps1", ".psm1", ".psd1", ".bat", ".cmd",
    # Query / schema
    ".sql", ".graphql", ".gql", ".proto",
    # Documents
    ".tex", ".bib", ".rtf",
    # Build / infrastructure
    ".dockerfile", ".tf", ".tfvars", ".hcl", ".nix", ".cmake",
})

# PDF extensions — handled separately because the raw file must be
# text-extracted, not read as bytes.
_PDF_EXTENSIONS: frozenset = frozenset({".pdf"})

# Known binary extensions — fast-path for common formats. Unknown
# extensions are still caught by content sniffing below.
_BINARY_EXTENSIONS: frozenset = frozenset({
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".ico", ".icns", ".heic", ".heif", ".avif", ".jxl",
    # Audio
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".opus", ".aiff",
    # Video
    ".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".m4v", ".mpeg",
    # Archives
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".7z", ".rar", ".xz", ".zst",
    # Office
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".pages", ".numbers", ".key",
    # Executables / libraries
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".app", ".msi", ".deb", ".rpm", ".dmg", ".iso",
    # Bytecode / compiled
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear",
    ".o", ".obj", ".lib", ".a", ".wasm",
    # Databases
    ".db", ".sqlite", ".sqlite3", ".mdb", ".accdb",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Design / media project
    ".psd", ".ai", ".sketch", ".fig", ".xd", ".indd",
    # ML binaries
    ".pkl", ".pickle", ".npy", ".npz", ".h5", ".hdf5", ".onnx", ".pt", ".pth",
})

# Bytes inspected during content sniffing. 8 KiB is enough to catch
# every common binary format's signature.
_BINARY_SNIFF_SIZE = 8192


def _is_likely_binary(path: Path) -> bool:
    """
    Return True when ``path`` should be treated as binary.

    Decision order:
        1. Text-like extension whitelist  → NOT binary (fast path)
        2. Known binary extension         → binary     (fast path)
        3. Unknown extension: sniff the first 8 KiB for a NUL byte,
           which is the canonical marker of binary content.
        4. Unreadable file                → binary     (safe default)
    """
    ext = path.suffix.lower()

    if ext in _TEXT_LIKE_EXTENSIONS:
        return False
    if ext in _BINARY_EXTENSIONS:
        return True

    # Unknown extension — sniff.
    try:
        with open(path, "rb") as f:
            chunk = f.read(_BINARY_SNIFF_SIZE)
    except OSError:
        return True

    return b"\x00" in chunk


def _extract_pdf_text(path: Path) -> Optional[str]:
    """
    Extract plain text from a PDF file.

    Tries, in order:
        1. pypdf    — modern, actively maintained (recommended).
        2. PyPDF2   — legacy but same API.
        3. pdfplumber — slower, better for layout-heavy PDFs.

    Returns None only when NO extractor is installed. Returns an empty
    string when extraction succeeds but the PDF has no extractable
    text (e.g. a scanned image-only document — OCR is out of scope
    for this helper). Partial extraction is best-effort: if some pages
    fail, the successfully extracted pages are returned.
    """
    PdfReader = None
    try:
        from pypdf import PdfReader  # type: ignore[no-redef]
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore[no-redef]
        except ImportError:
            PdfReader = None  # type: ignore[assignment,misc]

    if PdfReader is not None:
        try:
            reader = PdfReader(str(path))
            pages: List[str] = []
            for page in reader.pages:
                try:
                    text = page.extract_text() or ""
                    if text:
                        pages.append(text)
                except Exception as page_err:
                    logger.debug(
                        "PDF page extraction failed in %s: %s", path, page_err
                    )
            return "\n".join(pages)
        except Exception as exc:
            logger.debug(
                "PDF extraction via pypdf/PyPDF2 failed for %s: %s", path, exc
            )

    # pdfplumber fallback
    try:
        import pdfplumber  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        pages = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                try:
                    text = page.extract_text() or ""
                    if text:
                        pages.append(text)
                except Exception:
                    continue
        return "\n".join(pages)
    except Exception as exc:
        logger.debug("PDF extraction via pdfplumber failed for %s: %s", path, exc)
        return None


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
        ext = target.suffix.lower()

        # ------------------------------------------------------------------
        # [FIX-ALL-FILE-TYPES] Classify the file BEFORE reading it.
        # The pre-classification prevents PDFs and binaries from being
        # read as UTF-8, which would otherwise inject thousands of
        # U+FFFD replacement chars into the prompt.
        # ------------------------------------------------------------------
        content_kind = "text"

        if ext in _PDF_EXTENSIONS:
            # ── PDF: extract text via a dedicated library ────────
            if total_bytes >= max_total:
                blocks.append(
                    f'<file path="{canonical_rel}" truncated="true" '
                    f'reason="total_cap_reached" size_bytes="{size}"/>'
                )
                continue

            if size > MAX_PDF_RAW_BYTES:
                blocks.append(
                    f'<file path="{canonical_rel}" truncated="true" '
                    f'reason="pdf_too_large" size_bytes="{size}" '
                    f'max_pdf_raw="{MAX_PDF_RAW_BYTES}"/>'
                )
                continue

            extracted = _extract_pdf_text(target)
            if extracted is None:
                blocks.append(
                    f'<file path="{canonical_rel}" '
                    f'error="pdf_extractor_unavailable" '
                    f'size_bytes="{size}">\n'
                    f'[PDF content not inlined: no PDF text extraction '
                    f'library is installed. Install one with: '
                    f'pip install pypdf]\n'
                    f'</file>'
                )
                continue

            if not extracted.strip():
                # PDF exists but contains no extractable text (scanned
                # image-only document). Note this explicitly so the
                # model does not silently wonder why the file is empty.
                blocks.append(
                    f'<file path="{canonical_rel}" size_bytes="{size}" '
                    f'kind="pdf">\n'
                    f'[PDF contains no extractable text — likely a '
                    f'scanned document or image-only PDF. OCR is not '
                    f'performed automatically.]\n'
                    f'</file>'
                )
                continue

            content = extracted
            content_kind = "pdf"
            content_bytes = len(content.encode("utf-8"))

        elif _is_likely_binary(target):
            # ── Binary: emit a metadata marker only ──────────────
            # Binary files NEVER consume the text budget — the marker
            # is tiny (a few tokens) and the model only needs to know
            # that the file exists, not what its bytes are.
            mime_guess, _ = mimetypes.guess_type(str(target))
            mime_str = mime_guess or "application/octet-stream"
            blocks.append(
                f'<file path="{canonical_rel}" kind="binary" '
                f'mime="{mime_str}" size_bytes="{size}">\n'
                f'[Binary file — content not inlined. If you need '
                f'its contents, invoke an appropriate tool '
                f'(image viewer, PDF reader, archive extractor, '
                f'etc.).]\n'
                f'</file>'
            )
            continue

        else:
            # ── Text-like: original behavior preserved ───────────
            if total_bytes >= max_total:
                blocks.append(
                    f'<file path="{canonical_rel}" truncated="true" '
                    f'reason="total_cap_reached" size_bytes="{size}"/>'
                )
                continue

            if size > max_per_file:
                blocks.append(
                    f'<file path="{canonical_rel}" truncated="true" '
                    f'reason="file_too_large" size_bytes="{size}" '
                    f'max_per_file="{max_per_file}"/>'
                )
                continue

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
            content_kind = "text"
            content_bytes = len(content.encode("utf-8"))

        # ------------------------------------------------------------------
        # Post-read truncation (applies to PDF and text — binary never
        # reaches this point).
        #
        # For PDFs, `content_bytes` may exceed `max_per_file` even when
        # `size` (the raw PDF) is small — a 50 KiB PDF can easily yield
        # 300 KiB of extracted text. We truncate rather than refusing
        # the whole file, because the beginning of a PDF document
        # usually contains the most important content.
        # ------------------------------------------------------------------
        if content_bytes > max_per_file:
            omitted = content_bytes - max_per_file
            content = (
                content[:max_per_file]
                + f"\n\n[... truncated: {omitted} bytes omitted "
                f"to fit per-file limit of {max_per_file} bytes ...]"
            )
            content_bytes = max_per_file

        remaining = max_total - total_bytes
        if content_bytes > remaining:
            omitted = content_bytes - remaining
            content = (
                content[:remaining]
                + f"\n\n[... truncated: {omitted} bytes omitted "
                f"to fit total budget ...]"
            )
            content_bytes = remaining

        total_bytes += content_bytes
        blocks.append(
            f'<file path="{canonical_rel}" size_bytes="{size}" '
            f'kind="{content_kind}">\n'
            f'{content}\n</file>'
        )

    if not blocks:
        return prompt

    directive = (
        "The user referenced the following files in the workspace. "
        "Their contents have ALREADY been inlined below — you can "
        "read them directly from this message.\n\n"
        "IMPORTANT: Do NOT call any file-reading tool "
        "(`str_replace_editor`, `read_file_content`, `file_ops`, "
        "`view`, etc.) for the purpose of reading these files. The "
        "content is already here; announce-and-then-call is "
        "redundant. Only call a file tool when the user asks you to "
        "MODIFY a file — in that case use `str_replace_editor` with "
        "the exact relative path shown in the `path` attribute. Do "
        "NOT create new files with these names, and do NOT include "
        "the backticks in the path.\n\n"
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
