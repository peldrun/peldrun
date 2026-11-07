# backend/omweb/skills/loader.py
"""
Skill content loader.

Responsibilities:
    * Provide safe, cached access to a skill's instructions text, its
      optional ``references/`` files, and its icon (inline or file).
    * Enforce containment: no reference file may resolve outside the
      skill's own package directory.
    * Enforce size limits on every read path so a malicious or corrupted
      package cannot exhaust memory or blow the prompt budget.
    * Produce the canonical ``[SKILL: id@version]`` block that the
      engine injects into the system prompt — one place, one format.

Relationship with ``registry.py``:
    The registry already loads instructions once at discovery time and
    stores them on ``SkillRecord.instructions``. ``loader`` does NOT
    re-read that file for the common case — it reads from the record
    and only touches the disk for:
        * reference files (lazily loaded on demand), and
        * icon files (when the manifest declares a relative path).

    This split keeps the engine's hot path in memory while still
    allowing on-demand expansion of reference material without
    loading every reference of every skill on every run.

Security invariants:
    * No path outside ``SkillRecord.package_dir`` is ever read.
    * Symlinks are rejected.
    * File size caps apply to every file read.
    * Every SVG is sanitized a second time at load (the manifest layer
      already sanitized inline SVGs at install time; the file-based
      ``icon`` path is sanitized here for the first time).
    * Instructions that would exceed the declared
      ``limits.max_instructions_bytes`` are rejected with a clear
      ``SkillLoadError`` — the engine catches it and continues the run
      without the affected skill.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
import re
from typing import Any, Dict, Optional, Set

from omweb.skills.errors import (
    SkillLoadError,
    SkillSecurityError,
)
from omweb.skills.manifest import (
    DEFAULT_MAX_INSTRUCTIONS_BYTES,
    MAX_INSTRUCTIONS_BYTES,
    sanitize_svg,
)
from omweb.skills.registry import SkillRecord


logger = logging.getLogger("omweb.skills.loader")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cap for any single reference file. Deliberately larger than the
# instructions cap because reference material can legitimately include
# long API specs / RFC excerpts.
MAX_REFERENCE_BYTES = 512 * 1024  # 512 KiB

# Total bytes allowed across all references loaded for one skill in a
# single call. Prevents a pathological package with 10 × 500 KiB files
# from ballooning the prompt.
MAX_TOTAL_REFERENCES_BYTES = 2 * 1024 * 1024  # 2 MiB

# File extensions allowed inside ``references/``. Executables, archives,
# scripts, and image files are all excluded — references are text-first.
_ALLOWED_REFERENCE_SUFFIXES: Set[str] = {
    ".md", ".markdown", ".txt", ".rst",
    ".json", ".jsonl", ".yaml", ".yml", ".toml",
    ".xml", ".html", ".csv", ".tsv",
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".sh", ".sql", ".c", ".h", ".cpp", ".hpp",
    ".java", ".go", ".rs", ".rb", ".php",
    ".css", ".scss",
    ".svg",
}

# Hard cap on the depth of any file path inside ``references/``. The
# manifest already enforces a single-level ``references_dir``, so the
# remaining depth budget is for content organization *inside* that
# directory (e.g. ``references/api/auth.md``).
MAX_REFERENCE_RELATIVE_DEPTH = 4

_SAFE_REFNAME_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_contained(path: Path, root: Path) -> bool:
    """
    Return True when ``path`` resolves to a location inside ``root``.

    Both arguments are resolved. A symlink that points outside ``root``
    is treated as NOT contained, so callers reject it.
    """
    try:
        resolved = path.resolve(strict=False)
        root_resolved = root.resolve(strict=False)
        resolved.relative_to(root_resolved)
        return True
    except (ValueError, OSError):
        return False


def _safe_relpath(raw: str) -> str:
    """
    Validate a user-supplied relative path (for reference lookup).

    Rejects:
        * empty strings
        * absolute paths (Unix-style ``/`` or Windows-style ``C:\\``)
        * any path segment equal to ``..``
        * paths with more than ``MAX_REFERENCE_RELATIVE_DEPTH`` segments
        * paths containing characters outside ``[A-Za-z0-9_.\\-/]``

    Returns the normalized, forward-slash form. Raises
    ``SkillSecurityError`` on any violation.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise SkillSecurityError(
            "Reference path is empty.",
            details={"reason": "reference_path_empty"},
        )

    cleaned = raw.strip().replace("\\", "/")

    if cleaned.startswith("/") or (len(cleaned) >= 2 and cleaned[1] == ":"):
        raise SkillSecurityError(
            f"Reference path must be relative: {raw!r}",
            details={
                "reason": "reference_path_absolute",
                "value": raw[:200],
            },
        )

    if not _SAFE_REFNAME_RE.match(cleaned):
        raise SkillSecurityError(
            f"Reference path contains disallowed characters: {raw!r}",
            details={
                "reason": "reference_path_invalid_chars",
                "value": raw[:200],
            },
        )

    segments = [seg for seg in cleaned.split("/") if seg and seg != "."]
    if any(seg == ".." for seg in segments):
        raise SkillSecurityError(
            f"Reference path contains traversal segments: {raw!r}",
            details={
                "reason": "reference_path_traversal",
                "value": raw[:200],
            },
        )
    if len(segments) > MAX_REFERENCE_RELATIVE_DEPTH:
        raise SkillSecurityError(
            f"Reference path is too deep ({len(segments)} segments, max "
            f"{MAX_REFERENCE_RELATIVE_DEPTH}): {raw!r}",
            details={
                "reason": "reference_path_too_deep",
                "value": raw[:200],
                "depth": len(segments),
            },
        )

    return "/".join(segments)


def _resolve_references_root(record: SkillRecord) -> Optional[Path]:
    """
    Return the resolved ``references/`` root for a skill, or ``None``
    when the skill declares no references directory.

    The manifest's ``execution.references_dir`` is expected to be a
    single-segment name at the package root (schema-enforced). We
    re-verify containment here as defense-in-depth.
    """
    execution = record.manifest.get("execution") or {}
    if not isinstance(execution, dict):
        return None

    ref_dir = execution.get("references_dir")
    if not isinstance(ref_dir, str) or not ref_dir.strip():
        return None

    candidate = record.package_dir / ref_dir.strip()
    if not _is_contained(candidate, record.package_dir):
        raise SkillSecurityError(
            "references_dir escapes the skill package.",
            details={
                "reason": "references_dir_escape",
                "value": ref_dir,
                "skill_id": record.id,
            },
        )
    if not candidate.is_dir():
        return None
    return candidate


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_instructions(record: SkillRecord) -> str:
    """
    Return the skill's instructions text (the SKILL.md body without
    frontmatter), enforcing ``limits.max_instructions_bytes``.

    The registry already validated this at discovery time, but we
    re-check here because (a) the record may have been constructed by
    a test harness with an overridden instructions field, and (b) a
    second enforcement at the engine boundary is cheap.

    Raises:
        SkillLoadError: when instructions are empty or exceed the cap.
    """
    text = record.instructions or ""
    if not text.strip():
        raise SkillLoadError(
            f"Skill '{record.id}' has empty instructions.",
            details={
                "skill_id": record.id,
                "reason": "instructions_empty",
            },
        )

    declared_cap = (
        (record.manifest.get("limits") or {}).get("max_instructions_bytes")
        if isinstance(record.manifest.get("limits"), dict)
        else None
    )
    # Effective ceiling: min(declared, hard). When the manifest omits
    # the field, DEFAULT_MAX_INSTRUCTIONS_BYTES (256 KiB) applies —
    # matching the JSON Schema default and the install-time cap.
    max_bytes = DEFAULT_MAX_INSTRUCTIONS_BYTES
    if isinstance(declared_cap, int) and declared_cap > 0:
        max_bytes = min(declared_cap, MAX_INSTRUCTIONS_BYTES)

    actual_bytes = len(text.encode("utf-8"))
    if actual_bytes > max_bytes:
        raise SkillLoadError(
            f"Skill '{record.id}' instructions exceed {max_bytes} bytes "
            f"({actual_bytes} actual).",
            details={
                "skill_id": record.id,
                "reason": "instructions_too_large",
                "size_bytes": actual_bytes,
                "limit": max_bytes,
            },
        )
    return text


def load_reference(
    record: SkillRecord,
    rel_path: str,
    *,
    max_bytes: int = MAX_REFERENCE_BYTES,
) -> str:
    """
    Read a single reference file from the skill's ``references/`` dir.

    Args:
        record:    The skill record.
        rel_path:  Relative path inside ``references/``
                   (e.g. ``"api/auth.md"`` or ``"style_guide.md"``).
        max_bytes: Per-file byte cap (defaults to 512 KiB).

    Returns:
        The file's text content.

    Raises:
        SkillLoadError:    when the skill has no references directory,
                           the file is missing, or the file is empty.
        SkillSecurityError: on traversal, absolute paths, disallowed
                           extensions, symlinks, or containment escape.
    """
    ref_root = _resolve_references_root(record)
    if ref_root is None:
        raise SkillLoadError(
            f"Skill '{record.id}' has no references directory.",
            details={
                "skill_id": record.id,
                "reason": "no_references_dir",
            },
        )

    safe_rel = _safe_relpath(rel_path)

    # Extension whitelist
    suffix = Path(safe_rel).suffix.lower()
    if suffix not in _ALLOWED_REFERENCE_SUFFIXES:
        raise SkillSecurityError(
            f"Reference file extension {suffix!r} is not allowed.",
            details={
                "reason": "reference_extension_forbidden",
                "extension": suffix,
                "skill_id": record.id,
            },
        )

    candidate = ref_root / safe_rel

    # Symlink rejection: if the file exists and is a symlink, refuse.
    try:
        if candidate.is_symlink():
            raise SkillSecurityError(
                "Reference file must not be a symlink.",
                details={
                    "reason": "reference_is_symlink",
                    "path": safe_rel,
                    "skill_id": record.id,
                },
            )
    except OSError:
        pass

    # Containment re-check after symlink resolution
    if not _is_contained(candidate, ref_root):
        raise SkillSecurityError(
            "Reference path escapes the references directory.",
            details={
                "reason": "reference_path_escape",
                "path": safe_rel,
                "skill_id": record.id,
            },
        )

    if not candidate.is_file():
        raise SkillLoadError(
            f"Reference file not found: {safe_rel!r}.",
            details={
                "skill_id": record.id,
                "path": safe_rel,
                "reason": "reference_missing",
            },
        )

    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise SkillLoadError(
            f"Failed to stat reference file {safe_rel!r}: {exc}",
            details={"skill_id": record.id, "path": safe_rel},
        ) from exc

    if size > max_bytes:
        raise SkillLoadError(
            f"Reference file {safe_rel!r} exceeds {max_bytes} bytes "
            f"({size} actual).",
            details={
                "skill_id": record.id,
                "path": safe_rel,
                "reason": "reference_too_large",
                "size_bytes": size,
                "limit": max_bytes,
            },
        )

    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SkillLoadError(
            f"Failed to read reference file {safe_rel!r}: {exc}",
            details={"skill_id": record.id, "path": safe_rel},
        ) from exc

    if not text.strip():
        raise SkillLoadError(
            f"Reference file {safe_rel!r} is empty.",
            details={
                "skill_id": record.id,
                "path": safe_rel,
                "reason": "reference_empty",
            },
        )
    return text


def load_icon(record: SkillRecord) -> Optional[Dict[str, Any]]:
    """
    Return the skill's icon as a structured dict:

        {
            "kind": "inline" | "file",
            "content": "<svg>...</svg>",   # sanitized
            "content_type": "image/svg+xml",
        }

    Or ``None`` when the skill declares no icon.

    Resolution order:
        1. ``manifest.icon_svg``  (already sanitized at install time,
                                    re-sanitized here as defense-in-depth)
        2. ``manifest.icon``      (relative SVG file inside the package,
                                    read + sanitized now)
        3. None                   (frontend falls back to Lucide)

    Raises:
        SkillSecurityError: when the manifest-declared icon file
            escapes the package, is a symlink, exceeds the SVG cap,
            or fails sanitization.
    """
    # 1. Inline SVG (already sanitized at discovery)
    if record.icon_svg:
        try:
            resanitized = sanitize_svg(
                record.icon_svg, skill_id=record.id
            )
        except SkillSecurityError:
            # The install-time SVG was sanitized; if re-sanitization
            # fails now, something mutated the record. Fail closed.
            raise
        return {
            "kind": "inline",
            "content": resanitized,
            "content_type": "image/svg+xml",
        }

    # 2. File-based icon
    raw_rel = record.icon_path
    if not isinstance(raw_rel, str) or not raw_rel.strip():
        return None

    cleaned = raw_rel.strip().replace("\\", "/").lstrip("/")

    if ".." in cleaned.split("/"):
        raise SkillSecurityError(
            "Manifest 'icon' path contains traversal segments.",
            details={
                "reason": "icon_path_traversal",
                "value": raw_rel,
                "skill_id": record.id,
            },
        )

    if not cleaned.lower().endswith(".svg"):
        raise SkillSecurityError(
            "Manifest 'icon' must reference an .svg file.",
            details={
                "reason": "icon_not_svg",
                "value": raw_rel,
                "skill_id": record.id,
            },
        )

    candidate = record.package_dir / cleaned

    try:
        if candidate.is_symlink():
            raise SkillSecurityError(
                "Icon file must not be a symlink.",
                details={
                    "reason": "icon_is_symlink",
                    "value": raw_rel,
                    "skill_id": record.id,
                },
            )
    except OSError:
        pass

    if not _is_contained(candidate, record.package_dir):
        raise SkillSecurityError(
            "Icon path escapes the skill package.",
            details={
                "reason": "icon_path_escape",
                "value": raw_rel,
                "skill_id": record.id,
            },
        )

    if not candidate.is_file():
        raise SkillLoadError(
            f"Icon file not found: {raw_rel!r}.",
            details={
                "skill_id": record.id,
                "path": raw_rel,
                "reason": "icon_missing",
            },
        )

    try:
        raw_bytes = candidate.read_bytes()
    except OSError as exc:
        raise SkillLoadError(
            f"Failed to read icon file {raw_rel!r}: {exc}",
            details={"skill_id": record.id, "path": raw_rel},
        ) from exc

    if len(raw_bytes) > 32 * 1024:
        raise SkillSecurityError(
            "Icon file exceeds the 32 KiB cap.",
            details={
                "reason": "icon_too_large",
                "size_bytes": len(raw_bytes),
                "skill_id": record.id,
            },
        )

    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillSecurityError(
            f"Icon file is not valid UTF-8: {exc}",
            details={
                "reason": "icon_not_utf8",
                "skill_id": record.id,
            },
        ) from exc

    sanitized = sanitize_svg(raw_text, skill_id=record.id)

    return {
        "kind": "file",
        "content": sanitized,
        "content_type": "image/svg+xml",
    }


def get_injection_block(
    record: SkillRecord,
    *,
    include_metadata_line: bool = True,
) -> str:
    """
    Return the canonical text block the engine injects into the system
    prompt for a single skill.

    Format (stable, easy to parse by both humans and the model):

        --- BEGIN SKILL: code_review@1.0.0 ---
        [Metadata: name="Code Review", tags=[review, audit]]
        <instructions body>
        --- END SKILL: code_review@1.0.0 ---

    The metadata line is optional (``include_metadata_line=False``
    produces just the delimiters around the body). It is emitted by
    default because the model can use the display name and tags to
    decide whether the skill applies to the current task, without
    re-parsing the whole body.

    This function does NOT touch disk — it uses
    ``record.instructions``, which was loaded at discovery time.
    Callers are expected to wrap the return value in
    ``try/except SkillLoadError`` and skip the skill on failure, so a
    single broken skill cannot take down the engine.
    """
    body = load_instructions(record)

    header = f"--- BEGIN SKILL: {record.id}@{record.version} ---"
    footer = f"--- END SKILL: {record.id}@{record.version} ---"

    if include_metadata_line:
        tags = record.manifest.get("tags") or []
        tag_list = ", ".join(str(t) for t in tags if isinstance(t, str))
        meta = f'[Metadata: name="{record.name}"'
        if tag_list:
            meta += f", tags=[{tag_list}]"
        if record.is_deprecated:
            meta += ", deprecated=true"
        meta += "]"
        return f"{header}\n{meta}\n{body}\n{footer}"

    return f"{header}\n{body}\n{footer}"


__all__ = [
    "MAX_REFERENCE_BYTES",
    "MAX_TOTAL_REFERENCES_BYTES",
    "load_instructions",
    "load_reference",
    "load_icon",
    "get_injection_block",
]
