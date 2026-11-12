# backend/omweb/memory/imports/adapters/json_adapter.py
"""
PELDRUN Memory Platform - JSON / Zip Import Adapter.

The OFFICIAL adapter for the ``peldrun-memory-package`` format
produced by ``package_writer.py``. Handles four source shapes:

    * ``dict``   -- pre-parsed in-memory
                    ``{"manifest": {...}, "memories": [...]}``.
                    The fast path for tests and for callers that
                    have already unzipped the archive.

    * ``Path`` (directory) -- a directory containing
                    ``manifest.json`` + ``memories.jsonl`` at its
                    root. The natural shape after the user unzips
                    a package locally.

    * ``Path`` (.zip) -- a zip archive of the above. The most
                    common upload shape from the UI.

    * ``bytes`` (zip) -- raw archive bytes. Used by the HTTP
                    import endpoint when the file is streamed
                    directly from the request body.

Each shape is normalized to ``(manifest_data, memory_lines)`` and
then handed to ``validate_package`` from
``package_validator.py``. That validator does the ACTUAL schema
validation (version, count cross-check, security rule, per-entry
field validation) -- this adapter is a THIN shell around it. The
only logic here is source-shape detection and byte-to-object
conversion.

Scope handling -- PRESERVED, not coerced
----------------------------------------

The package's entries carry their ORIGINAL scopes
(``scope_type``/``scope_id`` on every line). The adapter
PRESERVES those scopes; entries are imported into the exact
scope they had when exported. This is the correct behavior for
the common cases:

    * "Export project A, re-import into project A" --
      round-trip.

    * "Export project A, import into project B on a different
      instance" -- the user is restoring a backup of project A's
      memories into a differently-named project. Preserving the
      scope lets them review the imported entries, then migrate
      the ones that matter to project B via the UI's "save as"
      flow.

    * "Export user:default + project:A, import into user:default"
      -- the user:default entries land where they belong; the
      project:A entries land in project:A on the target instance
      (whether it existed before the import or not). If the user
      wants those project entries reassigned, they can promote
      or delete them post-import.

The alternative -- coercing every entry into a single target
``scope_ref`` -- was rejected because it LOSES information:
"which scope did this memory originally belong to?" is
load-bearing for the audit trail that every other subsystem
maintains. The user's post-import moves are explicit, so they
belong to the user, not to the adapter.

**Known Phase 1 limitation**: the ``scope_ref`` parameter passed
to ``preview`` / ``apply`` drives the dedupe pool query. When a
package contains entries with multiple distinct scopes, the pool
contains entries only from the FIRST scope's slice of
``scope_ref``. Cross-scope dedupe is therefore INCOMPLETE for
multi-scope packages. Two workarounds exist:

  1. Export per-scope (one package per scope) -- the recommended
     path; produces clean, complete dedupe per package.

  2. Accept the limitation -- the import itself is correct
     (each entry still lands in its own scope with its own
     content), only the pre-import "would create / would skip"
     COUNTS may be slightly off. The user's final state is
     accurate, the preview is not.

P2 will extend the base class to support per-candidate dedupe
(one pool query per distinct scope) so this limitation
disappears. Until then, the preview's warning list carries an
explicit note when a multi-scope package is detected.

Sync vs async I/O
-----------------

Reading files is I/O and runs via ``asyncio.to_thread``. Building
the ``MemoryEntry`` objects from the parsed JSON is pure CPU and
runs on the caller's thread. This mirrors the split used by
``package_writer`` and ``package_validator``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryScopeRef,
    MemoryValidationError,
)

from omweb.memory.imports.base_adapter import BaseImportAdapter
from omweb.memory.imports.package_validator import (
    _MANIFEST_FILENAME,
    _MEMORIES_FILENAME,
    validate_package,
)


logger = logging.getLogger("omweb.memory.imports.adapters.json")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_ZIP_MAGIC: bytes = b"PK"
"""First two bytes of every zip archive. Used by ``can_handle``
to detect raw-bytes sources without trying to open them (a full
parse would be wasteful when called from
``ImportManager._select_adapter``, which checks every registered
adapter in sequence)."""


_MAX_ARCHIVE_SIZE: int = 100 * 1024 * 1024
"""Refuse to decompress archives larger than this.

An import archive is expected to be small (JSON + JSONL). 100MB
is generous enough for a 100k-entry memory database and small
enough to reject a decompression bomb. This is a DoS guard --
not a normal-operation limit."""


# ---------------------------------------------------------------------------
# JsonAdapter
# ---------------------------------------------------------------------------


class JsonAdapter(BaseImportAdapter):
    """Import adapter for ``peldrun-memory-package`` sources.

    See the module docstring for the full rationale and the
    scope-handling policy.
    """

    adapter_id: str = "json"
    description: str = (
        "peldrun-memory-package (manifest.json + memories.jsonl) "
        "as a directory, a zip archive, raw zip bytes, or a "
        "pre-parsed dict."
    )

    # ------------------------------------------------------------------
    # Format detection
    # ------------------------------------------------------------------

    def can_handle(self, source: Any) -> bool:
        """Return True for a peldrun-memory-package source.

        Cheap, synchronous shape detection only -- see the
        module docstring for the four supported shapes and the
        rationale for each.
        """
        if isinstance(source, dict):
            # Pre-parsed package dict. Two key sets are
            # acceptable: "manifest" + "memories" (canonical)
            # or "manifest" + "lines" (alias used by callers
            # who prefer "lines" to describe the JSONL content).
            if "manifest" not in source:
                return False
            return (
                "memories" in source
                or "lines" in source
                or "memory_lines" in source
            )

        if isinstance(source, Path):
            if source.is_dir():
                # Directory package: manifest.json must exist at
                # the root. The memories file is validated during
                # parse, so we do NOT check for it here -- a
                # missing file produces a clear error at parse
                # time, which is more actionable than "adapter
                # does not handle this" at selection time.
                return (source / _MANIFEST_FILENAME).is_file()
            if source.is_file() and source.suffix.lower() == ".zip":
                return True
            return False

        if isinstance(source, bytes):
            # Raw zip bytes: detected by the magic prefix. We do
            # not attempt to open the archive here -- the parse
            # step will produce a clear "not a valid zip" error
            # if the magic prefix lies (e.g. a plain text file
            # that happens to start with "PK").
            return source[:2] == _ZIP_MAGIC

        return False

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    async def _parse_to_candidates(
        self, source: Any, scope_ref: MemoryScopeRef
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Parse ``source`` and return ``(candidates, warnings)``.

        Note: ``scope_ref`` is accepted per the abstract signature
        but INTENTIONALLY NOT USED. The package's entries carry
        their own scopes (from the exported instance), and
        preserving them is the correct behavior for round-trip
        restores. See the module docstring for the full
        rationale on scope preservation.

        Delegates the actual validation to ``validate_package``,
        which:
          * enforces the ``format_version`` check,
          * cross-checks the manifest's ``count`` against the
            actual line count,
          * re-derives every entry's content hash with the
            current normalizer (see ``package_validator``'s
            module docstring for why this is load-bearing),
          * applies the security rule (task 1.6.14) for
            user-scope entries,
          * collects per-entry warnings.

        This method's only extra job is a multi-scope detection
        that adds an informational warning to the returned list
        -- the import still proceeds, the user is informed.
        """
        manifest_data, memory_lines = await self._load_package(source)

        # ``validate_package`` is the authority on "fatal vs
        # warning". We propagate its exceptions unchanged so the
        # caller (ImportManager) can wrap them into an HTTP 400
        # with the exact error message.
        result = validate_package(manifest_data, memory_lines)

        warnings: List[str] = list(result.warnings)

        # ----- Multi-scope advisory --------------------------------
        #
        # The base class's dedupe pool is fetched from the
        # ``scope_ref`` parameter. When a package carries entries
        # in MORE than one distinct scope, that pool is only
        # correct for entries matching ``scope_ref``. Entries in
        # other scopes may be deduped against an empty (or
        # partial) pool.
        #
        # The import itself is still correct -- each entry lands
        # in its own scope with its own content. Only the preview
        # counts are approximate. We surface this as a WARNING
        # (not an error) so the user can decide whether to
        # proceed or to split the import per scope.
        distinct_scopes = {
            f"{e.scope_type.value}:{e.scope_id}"
            for e in result.entries
        }
        if len(distinct_scopes) > 1:
            warnings.append(
                f"Package contains entries in {len(distinct_scopes)} "
                f"distinct scopes ({sorted(distinct_scopes)}). "
                f"Cross-scope duplicate detection may miss entries "
                f"outside the import's target scope. The import "
                f"itself is correct -- each entry lands in its own "
                f"original scope. For complete dedupe accuracy, "
                f"export per-scope."
            )
            logger.info(
                "Multi-scope package detected: %d scopes (%s). "
                "Dedupe pool may miss cross-scope duplicates.",
                len(distinct_scopes),
                sorted(distinct_scopes),
            )

        logger.info(
            "JsonAdapter parsed %d candidates (source=%s, "
            "warnings=%d, rejected=%d).",
            len(result.entries),
            self._source_label(source),
            len(warnings),
            result.rejected_count,
        )

        return result.entries, warnings

    # ------------------------------------------------------------------
    # Source-shape dispatch
    # ------------------------------------------------------------------

    async def _load_package(
        self, source: Any
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Normalize ``source`` to ``(manifest_data, memory_lines)``.

        Dispatches by source shape. Every branch produces the
        SAME tuple shape, which is what ``validate_package``
        expects.
        """
        if isinstance(source, dict):
            return self._from_dict(source)

        if isinstance(source, Path):
            if source.is_dir():
                return await self._from_directory(source)
            return await self._from_zip_path(source)

        if isinstance(source, bytes):
            return await self._from_zip_bytes(source)

        # Unreachable if ``can_handle`` was called first (the
        # ImportManager's contract). Defensive anyway.
        raise MemoryValidationError(
            f"JsonAdapter cannot handle source of type "
            f"{type(source).__name__}; expected dict, Path, or "
            f"bytes.",
            field_name="source",
        )

    # ------------------------- dict ------------------------------

    def _from_dict(
        self, source: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Extract ``(manifest, memory_lines)`` from a pre-parsed dict.

        Two acceptable key layouts (both documented on
        ``can_handle``): ``memories`` or ``lines`` /
        ``memory_lines``. The first key that exists wins, in
        that order.
        """
        manifest_data = source.get("manifest")
        if not isinstance(manifest_data, dict):
            raise MemoryValidationError(
                f"source['manifest'] must be a dict, got "
                f"{type(manifest_data).__name__}",
                field_name="manifest",
            )

        memory_lines: Any = (
            source.get("memories")
            if "memories" in source
            else source.get("lines")
            if "lines" in source
            else source.get("memory_lines")
        )
        if not isinstance(memory_lines, list):
            raise MemoryValidationError(
                f"source must carry a 'memories' list, got "
                f"{type(memory_lines).__name__}",
                field_name="memories",
            )

        # Shallow-copy so a downstream mutator (there are none
        # today, but this is defensive against future caller
        # bugs) cannot accidentally modify the caller's dict.
        return dict(manifest_data), list(memory_lines)

    # ------------------------- directory ------------------------

    async def _from_directory(
        self, path: Path
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Read ``manifest.json`` + ``memories.jsonl`` from a directory."""
        manifest_path = path / _MANIFEST_FILENAME
        memories_path = path / _MEMORIES_FILENAME

        if not manifest_path.is_file():
            raise MemoryValidationError(
                f"Directory package is missing {_MANIFEST_FILENAME}: "
                f"{path}",
                field_name="manifest",
            )
        if not memories_path.is_file():
            raise MemoryValidationError(
                f"Directory package is missing {_MEMORIES_FILENAME}: "
                f"{path}",
                field_name="memories",
            )

        manifest_data = await asyncio.to_thread(
            self._read_json_file, manifest_path
        )
        memory_lines = await asyncio.to_thread(
            self._read_jsonl_file, memories_path
        )
        return manifest_data, memory_lines

    # ------------------------- zip path -------------------------

    async def _from_zip_path(
        self, path: Path
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Open ``path`` as a zip archive and extract the package."""
        # Guard against huge archives BEFORE opening.
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise MemoryValidationError(
                f"Cannot stat zip archive {path}: {exc}",
                field_name="source",
                cause=exc,
            ) from exc

        if size > _MAX_ARCHIVE_SIZE:
            raise MemoryValidationError(
                f"Zip archive is too large ({size} bytes > "
                f"{_MAX_ARCHIVE_SIZE} bytes). Refusing to "
                f"decompress a potential DoS payload.",
                field_name="source",
                context={"path": str(path), "size": size},
            )

        try:
            return await asyncio.to_thread(
                self._read_zip_sync, path
            )
        except MemoryValidationError:
            raise
        except zipfile.BadZipFile as exc:
            raise MemoryValidationError(
                f"File is not a valid zip archive: {path} ({exc})",
                field_name="source",
                cause=exc,
            ) from exc

    # ------------------------- zip bytes ------------------------

    async def _from_zip_bytes(
        self, data: bytes
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Open raw zip bytes in memory and extract the package."""
        if len(data) > _MAX_ARCHIVE_SIZE:
            raise MemoryValidationError(
                f"Zip bytes too large ({len(data)} bytes > "
                f"{_MAX_ARCHIVE_SIZE} bytes). Refusing to "
                f"decompress a potential DoS payload.",
                field_name="source",
                context={"size": len(data)},
            )

        try:
            return await asyncio.to_thread(
                self._read_zip_bytes_sync, data
            )
        except MemoryValidationError:
            raise
        except zipfile.BadZipFile as exc:
            raise MemoryValidationError(
                f"Bytes are not a valid zip archive: {exc}",
                field_name="source",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Synchronous I/O helpers (run via asyncio.to_thread)
    # ------------------------------------------------------------------

    @staticmethod
    def _read_json_file(path: Path) -> Dict[str, Any]:
        """Read and parse a JSON file (with UTF-8 BOM tolerance).

        The BOM (U+FEFF) is legal at the start of a UTF-8 JSON
        document but ``json.loads`` on a raw text buffer rejects
        it. Reading with ``utf-8-sig`` transparently strips it.
        """
        raw = path.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemoryValidationError(
                f"Invalid JSON in {path}: {exc}",
                field_name="manifest",
                cause=exc,
            ) from exc
        if not isinstance(data, dict):
            raise MemoryValidationError(
                f"Expected a JSON object in {path}, got "
                f"{type(data).__name__}",
                field_name="manifest",
            )
        return data

    @staticmethod
    def _read_jsonl_file(path: Path) -> List[Dict[str, Any]]:
        """Read a JSONL file, one object per non-blank line.

        Blank lines (including trailing-newline artifacts) are
        skipped silently -- the JSONL convention tolerates them.
        Malformed lines produce a ``MemoryValidationError`` --
        we do not attempt partial line recovery here because
        ``validate_package`` will produce a better error at the
        cross-check level (count mismatch, if lines are silently
        dropped, is a much harder bug to diagnose than a clear
        "line 17 is not valid JSON").
        """
        raw = path.read_text(encoding="utf-8-sig")
        lines: List[Dict[str, Any]] = []
        for idx, line in enumerate(raw.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise MemoryValidationError(
                    f"Invalid JSON on line {idx + 1} of {path.name}: "
                    f"{exc}",
                    field_name="memories",
                    context={"line": idx + 1, "path": str(path)},
                    cause=exc,
                ) from exc
            if not isinstance(obj, dict):
                raise MemoryValidationError(
                    f"Line {idx + 1} of {path.name} is not a JSON "
                    f"object (got {type(obj).__name__})",
                    field_name="memories",
                    context={"line": idx + 1},
                )
            lines.append(obj)
        return lines

    def _read_zip_sync(
        self, path: Path
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Read a package from a zip file (sync -- caller offloads)."""
        with zipfile.ZipFile(path, "r") as zf:
            return self._extract_package_from_zipfile(zf, source_label=str(path))

    def _read_zip_bytes_sync(
        self, data: bytes
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Read a package from zip bytes (sync -- caller offloads)."""
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            return self._extract_package_from_zipfile(
                zf, source_label="<bytes>"
            )

    def _extract_package_from_zipfile(
        self,
        zf: zipfile.ZipFile,
        *,
        source_label: str,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Extract manifest + memories from an open ``ZipFile``.

        Only reads the TWO expected member files. Any additional
        members in the archive are IGNORED -- producers may add
        README files, license files, or future extensions that
        this version does not understand. Ignoring extras is
        forward-compatible; rejecting them would force version
        bumps for purely cosmetic additions.
        """
        members = zf.namelist()
        if _MANIFEST_FILENAME not in members:
            raise MemoryValidationError(
                f"Zip archive is missing {_MANIFEST_FILENAME} "
                f"(source={source_label}, members={members[:10]})",
                field_name="manifest",
            )
        if _MEMORIES_FILENAME not in members:
            raise MemoryValidationError(
                f"Zip archive is missing {_MEMORIES_FILENAME} "
                f"(source={source_label})",
                field_name="memories",
            )

        try:
            manifest_bytes = zf.read(_MANIFEST_FILENAME)
        except KeyError as exc:  # pragma: no cover -- guard for race
            raise MemoryValidationError(
                f"Could not read {_MANIFEST_FILENAME} from archive: {exc}",
                field_name="manifest",
                cause=exc,
            ) from exc

        try:
            memories_bytes = zf.read(_MEMORIES_FILENAME)
        except KeyError as exc:  # pragma: no cover
            raise MemoryValidationError(
                f"Could not read {_MEMORIES_FILENAME} from archive: {exc}",
                field_name="memories",
                cause=exc,
            ) from exc

        try:
            manifest_data = json.loads(
                manifest_bytes.decode("utf-8-sig")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MemoryValidationError(
                f"Invalid {_MANIFEST_FILENAME} in archive: {exc}",
                field_name="manifest",
                cause=exc,
            ) from exc
        if not isinstance(manifest_data, dict):
            raise MemoryValidationError(
                f"{_MANIFEST_FILENAME} is not a JSON object",
                field_name="manifest",
            )

        try:
            memories_text = memories_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise MemoryValidationError(
                f"Invalid UTF-8 in {_MEMORIES_FILENAME}: {exc}",
                field_name="memories",
                cause=exc,
            ) from exc

        memory_lines: List[Dict[str, Any]] = []
        for idx, line in enumerate(memories_text.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise MemoryValidationError(
                    f"Invalid JSON on line {idx + 1} of "
                    f"{_MEMORIES_FILENAME} in archive: {exc}",
                    field_name="memories",
                    context={"line": idx + 1},
                    cause=exc,
                ) from exc
            if not isinstance(obj, dict):
                raise MemoryValidationError(
                    f"Line {idx + 1} of {_MEMORIES_FILENAME} is not "
                    f"a JSON object",
                    field_name="memories",
                    context={"line": idx + 1},
                )
            memory_lines.append(obj)

        return manifest_data, memory_lines

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @staticmethod
    def _source_label(source: Any) -> str:
        """Return a short label for logging."""
        if isinstance(source, dict):
            return "<dict>"
        if isinstance(source, Path):
            return str(source)
        if isinstance(source, bytes):
            return f"<bytes len={len(source)}>"
        return f"<{type(source).__name__}>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["JsonAdapter"]
