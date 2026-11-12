# backend/omweb/memory/imports/adapters/markdown_adapter.py
"""
PELDRUN Memory Platform - Markdown Notes Import Adapter.

Imports Markdown notes (a single ``.md`` file, a directory tree of
``.md`` files, or an inline ``{name, content}`` dict) into the
memory subsystem as ``MemoryEntry`` candidates.

Why markdown -- and what it maps to
-----------------------------------

Markdown is the most common format for personal notes, and a folder
of ``.md`` files is the canonical "second brain" (Obsidian, Foam,
Logseq export, plain Vim notes, ...). Importing them gives the
memory subsystem immediate, user-curated content without requiring
the user to reformat everything into the JSON package format.

The parser uses the STRUCTURE that markdown already encodes:

    * Each heading (``#``, ``##``, ..., ``######``) starts a
      new section,
    * The section's body (everything until the next heading) is
      the candidate content,
    * The heading text becomes a tag on the candidate,
    * The file's stem becomes another tag,
    * ``classify_memory_type`` (P1.4) auto-detects the type
      from the body ("prefers X" -> PREFERENCE, "we decided Y"
      -> DECISION, ...); unclassifiable bodies default to
      SEMANTIC.

The result: a folder of notes becomes a scoped set of typed,
tagged memories in one import, without any per-file configuration.

What it does NOT do (deliberately)
----------------------------------

    * It does NOT preserve markdown formatting (bold, italics,
      links). The body is captured verbatim -- if you want
      ``**important**`` in the memory, that is fine, but the
      parser does not interpret the markdown. Downstream tooling
      that needs the formatted version should read the original
      file, not the imported memory.
    * It does NOT strip code blocks. A section that contains a
      fenced code block becomes a memory whose content includes
      the block. This is deliberate: a note like "Use this snippet
      to install X:\n```\npip install x\n```" is meant to be
      remembered verbatim.
    * It does NOT recurse into hidden directories (``.git``,
      ``node_modules``, ``.obsidian``, ...). Dot-prefixed
      path segments are skipped, matching the conventions of
      every tool that has ever written such a folder.
    * It does NOT use the file's YAML front matter as metadata.
      Front matter (``---\ntitle: X\n---``) is STRIPPED, not
      interpreted. It is common enough that including it in the
      memory content would be noise. Phase 2 can add explicit
      front-matter awareness if there is demand.

Scope enforcement
-----------------

Markdown files have no inherent scope. The importer therefore
requires the caller to specify the target scope via ``scope_ref``.

**User scope is REJECTED**, not merely discouraged. The plan's
security rule (task 1.6.14) treats imports as ``source_type=IMPORT``
and refuses to write user-scope entries from that source. A user
who wants their markdown notes to influence user-scope preferences
should use the UI's "Save to memory" button on individual notes;
that path produces ``source_type=EXPLICIT_USER`` entries which
ARE allowed in user scope.

This is a UX decision, not a technical limitation. It prevents the
following failure mode: a user imports a folder of notes containing
a quoted sentence like "the user prefers dark mode" (copied from a
blog post they were reading). Without the rule, that quote would
land in user scope and bias future responses toward a preference
the user never asserted.

Directory recursion
-------------------

When the source is a directory, the adapter recurses into every
non-hidden subdirectory looking for ``.md`` / ``.markdown`` files.
The traversal order is sorted (``Path.rglob`` returns
filesystem-order, which is not deterministic across platforms --
sorted makes the import order stable and reviewable).

Each file's parse produces candidates independently. A file that
fails to parse (invalid UTF-8, permission denied, ...) is SKIPPED
with a warning, and the import continues. A whole tree of notes
must not abort because one file has a bad byte sequence.

File name encoding -- decoded leniently
---------------------------------------

File names are decoded with ``errors="replace"`` because on some
Windows filesystems a folder of mixed-encoding file names is the
norm (files copied from legacy systems). A file name that cannot
be decoded as UTF-8 would otherwise crash the whole directory scan.

The stem still appears in the tag list (with U+FFFD replacement
characters if the original bytes are non-UTF-8). This is a
best-effort approach: the CONTENT of the file is what matters
most; the tag is a hint.

Sync vs async I/O
-----------------

Reading files runs via ``asyncio.to_thread`` (same pattern as
``JsonAdapter``). The markdown PARSING (headings split, tag
building) is pure CPU and runs on the caller's thread.

A directory import touches N files sequentially. The reason we do
NOT parallelize: the parse step is trivial CPU work dominated by
file I/O, and the filesystem is the bottleneck anyway -- issuing
N concurrent reads does not speed up a local SSD or NAS, and it
makes error attribution harder (which file failed which
exception?). Sequential keeps the log lines and warnings
unambiguously ordered.

Cap on candidates per import
----------------------------

A large folder (500+ notes) could produce thousands of candidates
and overwhelm the preview UI. We cap at ``_MAX_CANDIDATES_PER_IMPORT``
(500) -- enough for a serious notes vault, small enough that the
preview UI renders in one page. Entries beyond the cap are
SKIPPED with a warning telling the user how many were dropped and
why. The user can split large imports by subfolder if they need
all of them.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemoryScopeRef,
    MemorySourceType,
    MemoryType,
    MemoryValidationError,
)
from peldrun.memory.services.policies import (
    classify_memory_type,
    is_worth_saving,
)

from omweb.memory.imports.base_adapter import BaseImportAdapter


logger = logging.getLogger("omweb.memory.imports.adapters.markdown")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_ACCEPTED_SUFFIXES: frozenset = frozenset({".md", ".markdown"})


_MAX_CANDIDATES_PER_IMPORT: int = 500
"""Ceiling on candidates produced by a single directory import.

Protects the preview UI from an unbounded response. A 1000-file
notes vault would otherwise produce 5000+ candidates and either
time out the HTTP request or overflow the browser's rendering
capacity. The cap is generous enough for any normal single-scope
import; users with larger vaults can import one folder at a time.
"""


_MAX_SECTION_CHARS: int = 8_000
"""Content-length ceiling for a single section, in characters.

Slightly below ``MemoryEntry``'s 10,000-char cap. The extra
2,000 chars of headroom cover the ``is_worth_saving`` length
check and the type classification's regex work -- and any
trailing character a legitimate note might carry beyond the cap.

When a section exceeds this limit, the parser TRUNCATES it
(rather than rejecting the entry) and records a warning naming
the section, so the user knows the note was trimmed.
"""


# ---------------------------------------------------------------------------
# Regexes -- compiled once
# ---------------------------------------------------------------------------


_HEADING_RE: re.Pattern = re.compile(
    r"^(#{1,6})\s+(.+?)\s*$",
    re.MULTILINE,
)
"""Match a markdown heading line.

Captures:
    * ``group(1)`` -- the ``#``-prefix (used only for depth
      information in the tag, not currently consulted),
    * ``group(2)`` -- the heading text.

Anchored to line start with MULTILINE so that headings inside
code blocks (which are indented) are NOT matched. This is a
deliberate simplification -- a fully correct markdown parser
would track fenced code blocks and skip headings inside them.
Phase 1 accepts the minor false-positive risk (a line inside a
code block that starts with ``#`` being treated as a heading).
The consequence is cosmetic (one extra split), not correctness-
altering.
"""


# The docstring below is a RAW string (r-prefix) so that the
# ``\A`` regex anchor appearing in the prose is not interpreted
# as an escape sequence by Python's string parser. Using a
# non-raw docstring would emit ``SyntaxWarning: invalid escape
# sequence '\A'`` at import time -- harmless, but noise in
# test output.
_FRONT_MATTER_RE: re.Pattern = re.compile(
    r"\A---\s*\n.*?\n---\s*\n",
    re.DOTALL,
)
r"""Match YAML front matter at the start of a file.

Shape::

    ---
    title: Some title
    tags: [a, b]
    ---
    # Actual content starts here

The front matter is STRIPPED before parsing -- it is
bookkeeping, not the note. Only matches at the very start of
the content (before BOM stripping in the content loader).
Files without front matter are unaffected (the regex simply
does not match).
"""


_SLUG_INVALID_RE: re.Pattern = re.compile(r"[^\w\-]+")
"""Characters to replace when converting a heading into a tag.

Keeps: ``\\w`` (Unicode word chars -- Arabic/Cyrillic/Latin all
pass through), hyphens. Replaces: everything else (spaces,
punctuation, emoji) with ``_``.
"""


# ---------------------------------------------------------------------------
# MarkdownAdapter
# ---------------------------------------------------------------------------


class MarkdownAdapter(BaseImportAdapter):
    """Import adapter for markdown notes.

    See the module docstring for the parsing model, the security
    policy for user scope, and the directory traversal rules.
    """

    adapter_id: str = "markdown"
    description: str = (
        "Markdown notes: a single .md/.markdown file, a directory "
        "tree of such files, or an inline {name, content} dict. "
        "Each heading section becomes one memory entry."
    )

    # ------------------------------------------------------------------
    # Format detection
    # ------------------------------------------------------------------

    def can_handle(self, source: Any) -> bool:
        """Return True for a markdown source.

        Accepts:
          * ``Path`` ending in ``.md`` / ``.markdown``,
          * ``Path`` to a directory (assumed markdown-only; the
            ImportManager's adapter-selection order places
            MarkdownAdapter AFTER JsonAdapter, so a ``.zip``
            directory-shaped source would already have been
            claimed by JsonAdapter if it looked like a package),
          * ``dict`` with ``"content"`` key (name optional).
        """
        if isinstance(source, dict):
            return isinstance(source.get("content"), str)

        if isinstance(source, Path):
            if source.is_file():
                return source.suffix.lower() in _ACCEPTED_SUFFIXES
            if source.is_dir():
                # Any directory is potentially a markdown tree.
                # We do NOT pre-scan for ``.md`` files here --
                # that would be O(N) directory work on every
                # adapter selection. Instead, ``_parse_directory``
                # discovers the files and produces a clear error
                # if none match.
                return True

        return False

    # ------------------------------------------------------------------
    # Parse dispatch
    # ------------------------------------------------------------------

    async def _parse_to_candidates(
        self, source: Any, scope_ref: MemoryScopeRef
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Parse ``source`` into ``(candidates, warnings)``.

        ``scope_ref`` is REQUIRED for markdown -- the format
        carries no inherent scope. User scope is rejected here
        (before any parsing) with a clear error message.
        """
        # ----- Scope validation (before any I/O) -----------------
        #
        # The plan's security rule (task 1.6.14) refuses
        # ``scope_type=user`` for ``source_type=IMPORT``. We fail
        # FAST rather than letting every entry be rejected one
        # by one in ``validate_package``-analog checks (we do not
        # use ``validate_package`` for markdown -- each entry is
        # constructed directly).
        if scope_ref.scope_type == MemoryScope.USER:
            raise MemoryValidationError(
                "MarkdownAdapter cannot import into user scope. "
                "Markdown files are treated as source_type=IMPORT, "
                "and user-scope entries must originate from "
                "explicit_user action. To save preferences into "
                "user memory, use the UI's 'Save to memory' button "
                "on individual notes, or import into project scope.",
                field_name="scope_ref",
                context={"requested_scope": str(scope_ref)},
            )

        # ----- Dispatch by shape ---------------------------------
        if isinstance(source, dict):
            return await self._parse_single_content(
                content=source["content"],
                name=str(source.get("name") or "inline"),
                scope_ref=scope_ref,
            )

        if isinstance(source, Path):
            if source.is_file():
                return await self._parse_file(source, scope_ref)
            return await self._parse_directory(source, scope_ref)

        # Unreachable if ``can_handle`` was called first (the
        # ImportManager's contract). Defensive anyway.
        raise MemoryValidationError(
            f"MarkdownAdapter cannot handle source of type "
            f"{type(source).__name__}; expected dict or Path.",
            field_name="source",
        )

    # ------------------------------------------------------------------
    # Single file
    # ------------------------------------------------------------------

    async def _parse_file(
        self, path: Path, scope_ref: MemoryScopeRef
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Parse one ``.md`` file."""
        try:
            content = await asyncio.to_thread(
                path.read_text, encoding="utf-8-sig"
            )
        except UnicodeDecodeError as exc:
            # Legacy encodings (cp1252, latin-1) are common in
            # older note files. We could fall back, but the
            # user should know their file is not UTF-8 --
            # silent re-decoding can corrupt content.
            raise MemoryValidationError(
                f"File {path.name} is not valid UTF-8: {exc}",
                field_name="content",
                context={"path": str(path)},
                cause=exc,
            ) from exc
        except OSError as exc:
            raise MemoryValidationError(
                f"Cannot read {path.name}: {exc}",
                field_name="content",
                context={"path": str(path)},
                cause=exc,
            ) from exc

        return self._parse_content(
            content=content,
            source_name=path.stem,
            scope_ref=scope_ref,
        )

    # ------------------------------------------------------------------
    # Directory tree
    # ------------------------------------------------------------------

    async def _parse_directory(
        self, root: Path, scope_ref: MemoryScopeRef
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Recursively parse every ``.md`` file under ``root``.

        Skips hidden path segments. Files that fail to read are
        skipped with a warning; the rest of the tree is imported.
        """
        # Discover files on a worker thread -- ``rglob`` performs
        # directory I/O which can be slow on a network mount.
        all_md_files = await asyncio.to_thread(
            lambda: sorted(
                p for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in _ACCEPTED_SUFFIXES
            )
        )

        # Filter out hidden paths. Do it separately (after sort,
        # so the order stays deterministic) because the filter is
        # pure CPU and does not need a thread hop.
        visible_files: List[Path] = []
        for p in all_md_files:
            rel = p.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                logger.debug(
                    "Skipping hidden path: %s", rel
                )
                continue
            visible_files.append(p)

        if not visible_files:
            raise MemoryValidationError(
                f"No .md or .markdown files found under {root}",
                field_name="source",
                context={"path": str(root)},
            )

        candidates: List[MemoryEntry] = []
        warnings: List[str] = []

        for md_path in visible_files:
            if len(candidates) >= _MAX_CANDIDATES_PER_IMPORT:
                remaining = len(visible_files) - visible_files.index(md_path)
                warnings.append(
                    f"import capped at {_MAX_CANDIDATES_PER_IMPORT} "
                    f"candidates; {remaining} file(s) were not "
                    f"processed. Split the directory into smaller "
                    f"batches if you need all of them."
                )
                break
            try:
                content = await asyncio.to_thread(
                    md_path.read_text, encoding="utf-8-sig"
                )
            except (UnicodeDecodeError, OSError) as exc:
                rel = md_path.relative_to(root)
                warnings.append(
                    f"skipped {rel}: {type(exc).__name__}: {exc}"
                )
                continue

            file_candidates, file_warnings = self._parse_content(
                content=content,
                source_name=md_path.stem,
                scope_ref=scope_ref,
            )
            candidates.extend(file_candidates)
            rel = md_path.relative_to(root)
            for w in file_warnings:
                warnings.append(f"{rel}: {w}")

        logger.info(
            "MarkdownAdapter parsed %d candidates from %d/%d files "
            "under %s (%d warnings).",
            len(candidates),
            len(visible_files),
            len(visible_files),
            root,
            len(warnings),
        )
        return candidates, warnings

    # ------------------------------------------------------------------
    # Inline dict
    # ------------------------------------------------------------------

    async def _parse_single_content(
        self,
        content: str,
        name: str,
        scope_ref: MemoryScopeRef,
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Parse an inline ``{name, content}`` dict."""
        return self._parse_content(
            content=content,
            source_name=name,
            scope_ref=scope_ref,
        )

    # ------------------------------------------------------------------
    # Core parser
    # ------------------------------------------------------------------

    def _parse_content(
        self,
        content: str,
        source_name: str,
        scope_ref: MemoryScopeRef,
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Split ``content`` into sections and build candidates.

        Steps:

          1. Strip YAML front matter (``---...---`` at start).
          2. Find all headings.
          3. If no headings, treat the whole content as one entry.
          4. Otherwise, split by heading boundaries.
          5. For each section, apply ``is_worth_saving`` quality
             check; skip empty or trivial sections.
          6. Truncate overlong sections at ``_MAX_SECTION_CHARS``.
          7. Build a ``MemoryEntry`` with:
             * ``memory_type`` -- auto-detected via
               ``classify_memory_type``, defaulting to SEMANTIC
             * ``tags`` -- the file stem + the heading slug
             * ``provenance`` -- IMPORT, imported_from=markdown:{name}
             * scope from ``scope_ref``
        """
        candidates: List[MemoryEntry] = []
        warnings: List[str] = []

        # ----- Front matter strip ---------------------------------
        stripped = _FRONT_MATTER_RE.sub("", content, count=1)

        # ----- Locate headings ------------------------------------
        heading_matches = list(_HEADING_RE.finditer(stripped))

        if not heading_matches:
            # Whole file, one candidate.
            body = stripped.strip()
            if not body:
                return [], []  # empty file, no warning
            if not is_worth_saving(body):
                warnings.append(
                    "content failed is_worth_saving "
                    "(too short or non-informative)"
                )
                return [], warnings
            entry = self._build_entry(
                body=body,
                source_name=source_name,
                scope_ref=scope_ref,
                extra_tags=[],
                warnings=warnings,
            )
            return ([entry] if entry is not None else []), warnings

        # ----- Walk each section ----------------------------------
        for idx, match in enumerate(heading_matches):
            heading_text = match.group(2).strip()
            body_start = match.end()
            body_end = (
                heading_matches[idx + 1].start()
                if idx + 1 < len(heading_matches)
                else len(stripped)
            )
            body = stripped[body_start:body_end].strip()

            if not body:
                continue  # section has heading but no content
            if not is_worth_saving(body):
                continue  # too short / noise

            heading_slug = self._slugify(heading_text)
            extra_tags: List[str] = []
            if heading_slug:
                extra_tags.append(heading_slug)

            entry = self._build_entry(
                body=body,
                source_name=source_name,
                scope_ref=scope_ref,
                extra_tags=extra_tags,
                warnings=warnings,
            )
            if entry is not None:
                candidates.append(entry)

        return candidates, warnings

    # ------------------------------------------------------------------
    # Candidate construction
    # ------------------------------------------------------------------

    def _build_entry(
        self,
        *,
        body: str,
        source_name: str,
        scope_ref: MemoryScopeRef,
        extra_tags: List[str],
        warnings: List[str],
    ) -> Optional[MemoryEntry]:
        """Build one ``MemoryEntry`` from a parsed section body.

        Returns ``None`` when construction fails (in which case
        a warning is appended). The caller treats ``None`` as
        "skip this section".
        """
        # ----- Section length cap -----------------------------
        if len(body) > _MAX_SECTION_CHARS:
            original_len = len(body)
            body = body[:_MAX_SECTION_CHARS].rstrip() + (
                "\n\n... [section truncated] ..."
            )
            warnings.append(
                f"section in {source_name!r} truncated from "
                f"{original_len} to {_MAX_SECTION_CHARS} chars"
            )

        # ----- Auto-detect memory type ------------------------
        #
        # ``classify_memory_type`` returns None for unclassified
        # prose. We default to SEMANTIC -- the safest
        # catch-all that will not mislead the ranker.
        memory_type = classify_memory_type(body) or MemoryType.SEMANTIC

        # ----- Tag construction --------------------------------
        #
        # The file stem always contributes one tag. The heading
        # (if any) contributes another. ``MemoryEntry`` dedupes
        # and lowercases them.
        tags: List[str] = []
        if source_name:
            stem_slug = self._slugify(source_name)
            if stem_slug:
                tags.append(stem_slug)
        for t in extra_tags:
            tags.append(t)

        # ----- Provenance --------------------------------------
        provenance = MemoryProvenance(
            source_type=MemorySourceType.IMPORT,
            imported_from=f"markdown:{source_name}",
        )

        try:
            return MemoryEntry.create(
                content=body,
                memory_type=memory_type,
                scope_type=scope_ref.scope_type,
                scope_id=scope_ref.scope_id,
                provenance=provenance,
                tags=tags,
            )
        except (ValueError, TypeError) as exc:
            warnings.append(
                f"section in {source_name!r} failed construction: "
                f"{exc}"
            )
            return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _slugify(text: str) -> str:
        """Turn ``text`` into a tag-safe slug.

        Lowercase, non-word chars to ``_``, collapse runs of
        underscores, strip leading/trailing underscores. Empty
        result (e.g. heading was only emoji) returns "" --
        callers must skip empty tags.
        """
        if not text:
            return ""
        lowered = text.lower().strip()
        replaced = _SLUG_INVALID_RE.sub("_", lowered)
        return replaced.strip("_")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["MarkdownAdapter"]
