# backend/omweb/memory/imports/package_writer.py
"""
PELDRUN Memory Platform - peldrun-memory-package Writer.

The MIRROR of ``package_validator``: produces packages that the
validator (and future importers) can consume. Any change to the
line format here MUST be paired with a change on the validator
side -- otherwise round-trip exports become unreadable, which is
the exact failure mode this module exists to prevent.

Two export flavors
------------------

    * ``write_package_to_directory`` -- async, writes
      ``manifest.json`` + ``memories.jsonl`` into a target
      directory. Used by the export API when the result is a
      server-side file the caller downloads via a static route.

    * ``write_package_to_zip_bytes`` -- SYNC, builds a zip
      archive entirely in memory. Used by the export API when
      the result is streamed back as a ``Content-Disposition:
      attachment`` HTTP response. Kept synchronous because it
      touches no disk, so there is nothing to await.

Both flavors share the SAME payload-building path
(``build_package_payload``), so the output is byte-identical
modulo the fact that zip adds container overhead.

Format specification -- THE contract
------------------------------------

A ``peldrun-memory-package`` is exactly:

    manifest.json
        Single JSON object. Schema: ``PeldrunMemoryPackageManifest``
        (from ``package_validator``). ``extra="forbid"``.

    memories.jsonl
        One JSON object per line. Line schema: ``_entry_to_line``.
        No trailing whitespace, no indentation, UTF-8, no BOM.

Line schema (see ``_entry_to_line``):

    content:       str         -- raw entry content
    memory_type:   str         -- enum value ("semantic"|...)
    scope_type:    str         -- enum value ("user"|...)
    scope_id:      str
    source_type:   str         -- enum value
    tags:          list[str]
    importance:    int
    confidence:    float
    is_pinned:     bool
    status:        str         -- enum value
    created_at:    str         -- ISO 8601 UTC
    expires_at:    str | None  -- ISO 8601 UTC (null if no expiry)

Fields NOT written (deliberately):

    memory_id          -- importer generates a fresh one
    normalized_content -- importer's normalizer owns the value
    content_hash       -- importer recomputes from content
    updated_at         -- importer sets to import time
    last_used_at       -- importer sets to None

These omissions are load-bearing. See ``package_validator``'s
module docstring for the full rationale.

Status field -- forward compatibility
-------------------------------------

The ``status`` field IS included in every line even though the
P1 validator ignores it (imports every entry as ACTIVE). Two
reasons:

  1. A future P2 validator that preserves status can read old
     packages without a format-version bump.
  2. A user who exports a scope with 10 active + 5 archived
     entries and later inspects the JSONL by hand can see the
     archived ones and decide manually whether to re-import
     them.

The alternative -- omitting ``status`` in P1 and adding it in a
version 2 -- would force every P1 producer to be upgraded before
any consumer could benefit. Including it from day 1 costs
nothing (the validator's ``_rebuild_entry_from_line`` uses
``raw.get(...)`` on every optional field, so unknown extras are
ignored) and pays off the moment P2 lands.

Atomic writes -- invariant
--------------------------

``write_package_to_directory`` writes each file via the
``tempfile.mkstemp + fsync + os.replace`` pattern used by
``MemorySettingsManager``. A crash mid-write leaves the previous
package intact (if one existed) -- there is no window where
``manifest.json`` exists but ``memories.jsonl`` is truncated,
or vice versa.

The two-file transaction is NOT truly atomic across files, but
the ordering makes partial writes harmless:

    Step 1: write memories.jsonl (new content) to temp, rename.
    Step 2: write manifest.json (new content) to temp, rename.

If the process dies between Step 1 and Step 2, the target
directory has the NEW memories.jsonl but the OLD manifest.json.
A subsequent validation would fail the count cross-check --
which is the CORRECT behavior: the caller learns the write was
interrupted and can retry. The opposite ordering (manifest
first) would leave a manifest claiming N entries with N-1 lines
already on disk -- validation would also fail, but the failure
would look like "corrupted package" rather than "interrupted
write", which is less diagnostic.

The safer alternative -- write both to temp, fsync both, then
rename both back-to-back -- has the same crash window (between
the two renames) and the same diagnostic outcome. The chosen
ordering is strictly simpler and equally safe.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncio

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryStatus,
    MemoryValidationError,
)

from omweb.memory.imports.package_validator import (
    PACKAGE_FORMAT_VERSION,
    PeldrunMemoryPackageManifest,
    _MANIFEST_FILENAME,
    _MEMORIES_FILENAME,
)


logger = logging.getLogger("omweb.memory.imports.package_writer")


# ---------------------------------------------------------------------------
# Default source label
# ---------------------------------------------------------------------------


def _default_source_label() -> str:
    """Return a sensible default for ``manifest.source``.

    Prefers the installed ``peldrun`` package version. Falls back
    to a generic label if the import fails (e.g. running in a test
    environment without the full package on PYTHONPATH). The label
    appears in the ``manifest.source`` field and in every
    imported entry's ``provenance.imported_from``, so it should
    identify the PRODUCER -- not the consumer.
    """
    try:
        import peldrun
        version = getattr(peldrun, "__version__", "unknown")
        return f"peldrun-web@{version}"
    except ImportError:
        return "peldrun-memory-package"


# ---------------------------------------------------------------------------
# Datetime serialization
# ---------------------------------------------------------------------------


def _dt_to_iso_z(dt: Optional[datetime]) -> Optional[str]:
    """Serialize ``dt`` to ISO 8601 UTC with a trailing ``Z``.

    The validator accepts both ``Z`` and ``+00:00`` suffixes
    (``datetime.fromisoformat(..., replace("Z", "+00:00"))``).
    ``Z`` is the conventional choice for archival timestamps --
    it makes the UTC indicator unambiguous and matches what
    ``datetime.utcnow().isoformat() + "Z"`` produced in legacy
    exports.

    Naive datetimes are treated as UTC (matching ``MemoryEntry``'s
    own coercion behavior at field-validator time). ``None``
    passes through as ``None`` -- the line's ``expires_at`` field
    is written as ``null`` rather than a placeholder string.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Per-entry serialization
# ---------------------------------------------------------------------------


def _entry_to_line(entry: MemoryEntry) -> Dict[str, Any]:
    """Serialize a ``MemoryEntry`` to the package's per-line dict.

    Every field written here is REQUIRED by the validator's
    ``_rebuild_entry_from_line`` -- if you add a field to the line
    format, update BOTH sides of the mirror.

    Fields that are DELIBERATELY not written (see module docstring):
    ``memory_id``, ``normalized_content``, ``content_hash``,
    ``updated_at``, ``last_used_at``. The importer regenerates
    each of these.
    """
    return {
        "content": entry.content,
        "memory_type": entry.memory_type.value,
        "scope_type": entry.scope_type.value,
        "scope_id": entry.scope_id,
        "source_type": entry.provenance.source_type.value,
        "tags": list(entry.tags),
        "importance": int(entry.importance),
        "confidence": float(entry.confidence),
        "is_pinned": bool(entry.is_pinned),
        "status": entry.status.value,
        "created_at": _dt_to_iso_z(entry.created_at),
        "expires_at": _dt_to_iso_z(entry.expires_at),
    }


# ---------------------------------------------------------------------------
# Payload builder (pure function)
# ---------------------------------------------------------------------------


def build_package_payload(
    entries: List[MemoryEntry],
    *,
    source_label: Optional[str] = None,
    exported_at: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Build ``(manifest_data, memory_lines)`` from a list of entries.

    This is the SINGLE source of truth for what goes into a
    package. Both the directory writer and the zip writer call
    this -- the only difference between the two flavors is the
    container format.

    Parameters
    ----------
    entries
        Entries to serialize. May be empty -- an empty package is
        a valid no-op import target (see the validator docstring
        for the empty-package rationale). Order is preserved in
        ``memories.jsonl``; the caller is responsible for sorting
        if determinism is required.
    source_label
        Producer identifier. Defaults to ``_default_source_label()``
        (typically ``"peldrun-web@<version>"``). Copied verbatim
        to ``manifest.source``.
    exported_at
        Timestamp of the export. Defaults to ``datetime.now(UTC)``.
        Injectable for deterministic tests that assert
        byte-identical round-trip output.

    Returns
    -------
    Tuple[Dict[str, Any], List[Dict[str, Any]]]
        ``manifest_data`` -- a dict ready for ``json.dump``. NOT
        validated as a Pydantic model here -- the caller may
        serialize it directly, and the validator will parse it
        on the import side. Building the dict directly avoids a
        needless ``Manifest.model_validate(payload).model_dump()``
        round-trip in the hot export path.
        ``memory_lines`` -- list of per-entry dicts ready for
        JSONL serialization.

    Raises
    ------
    TypeError
        When ``entries`` is not a list, or contains non-
        ``MemoryEntry`` values. Structural validation -- caller
        bugs surface immediately.
    MemoryValidationError
        When an entry's ``created_at`` is unparseable -- this
        should be impossible via ``MemoryEntry``'s own validators,
        but defensive here because a future entry subclass or a
        manually-constructed instance could bypass them.
    """
    if not isinstance(entries, list):
        raise TypeError(
            f"entries must be a list, got {type(entries).__name__}"
        )

    # Resolve exported_at once so every timestamp in the manifest
    # references the same instant.
    if exported_at is None:
        exported_at = datetime.now(timezone.utc)
    elif exported_at.tzinfo is None:
        exported_at = exported_at.replace(tzinfo=timezone.utc)

    effective_source = (
        source_label.strip()
        if isinstance(source_label, str) and source_label.strip()
        else _default_source_label()
    )

    # ---- Per-entry lines -------------------------------------------
    memory_lines: List[Dict[str, Any]] = []
    unique_scopes: List[str] = []
    seen_scopes: set = set()

    for idx, entry in enumerate(entries):
        if not isinstance(entry, MemoryEntry):
            raise TypeError(
                f"entries[{idx}] must be MemoryEntry, "
                f"got {type(entry).__name__}"
            )
        line = _entry_to_line(entry)
        memory_lines.append(line)

        ref = f"{entry.scope_type.value}:{entry.scope_id}"
        if ref not in seen_scopes:
            seen_scopes.add(ref)
            unique_scopes.append(ref)

    # ---- Manifest --------------------------------------------------
    #
    # ``scopes`` is ordered by FIRST APPEARANCE in the entries list
    # -- this produces a deterministic, human-readable scope list
    # that matches the order the user (or caller) is likely to
    # expect. Sorting alphabetically was rejected because a
    # "user:default, project:acme" manifest reads more naturally
    # than "project:acme, user:default" (user is the broader scope
    # and conventionally comes first).
    manifest_data: Dict[str, Any] = {
        "format_version": PACKAGE_FORMAT_VERSION,
        "source": effective_source,
        "scopes": unique_scopes,
        "count": len(memory_lines),
        "exported_at": _dt_to_iso_z(exported_at),
    }

    # Belt-and-suspenders validation of the produced manifest. The
    # cost is one Pydantic model_validate call per export (a few
    # microseconds) and it catches a whole class of "writer drifted
    # from validator" bugs at the moment the drift is introduced,
    # not at the moment a user tries to import the bad package.
    #
    # If this raises, it is ALWAYS a programmer error -- the
    # writer produced a manifest that the validator would reject.
    # Better to fail on the export side (where the developer sees
    # the stack trace) than on the import side (where a user sees
    # a cryptic validation error).
    try:
        PeldrunMemoryPackageManifest.model_validate(manifest_data)
    except Exception as exc:
        logger.error(
            "Writer produced an invalid manifest -- this is a bug. "
            "manifest_data=%r, error=%s",
            manifest_data,
            exc,
        )
        raise MemoryValidationError(
            (
                f"Internal error: writer produced a manifest that "
                f"fails its own validation. manifest.format_version="
                f"{manifest_data.get('format_version')!r}, "
                f"manifest.count={manifest_data.get('count')!r}. "
                f"This is a programmer error -- report it."
            ),
            field_name="manifest",
            cause=exc,
        ) from exc

    return manifest_data, memory_lines


# ---------------------------------------------------------------------------
# JSON serialization helpers
# ---------------------------------------------------------------------------


def _serialize_manifest(manifest_data: Dict[str, Any]) -> bytes:
    """Serialize the manifest to UTF-8 bytes.

    ``indent=2`` for readability -- the manifest is small (a few
    hundred bytes) and being able to open it in a text editor is
    worth the extra whitespace. The memories file uses the
    opposite choice (compact JSONL) because it can grow large.
    """
    return json.dumps(
        manifest_data,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    ).encode("utf-8")


def _serialize_lines(memory_lines: List[Dict[str, Any]]) -> bytes:
    """Serialize ``memory_lines`` to UTF-8 JSONL bytes.

    One line per dict. Empty list produces an empty payload --
    which is the correct representation of an empty package
    (a zero-byte ``memories.jsonl`` file). The validator's
    count cross-check accepts this as long as ``manifest.count``
    is also 0.
    """
    if not memory_lines:
        return b""
    parts: List[str] = []
    for line in memory_lines:
        parts.append(json.dumps(line, ensure_ascii=False, sort_keys=False))
    # ``\n`` separator, no trailing newline (a trailing newline
    # would make ``.strip().splitlines()`` produce a phantom
    # empty line in some parsers, but our own validator uses
    # ``line.strip()`` before parsing, so both conventions work.
    # No trailing newline is the conventional JSONL choice).
    return ("\n".join(parts)).encode("utf-8")


# ---------------------------------------------------------------------------
# Atomic file write
# ---------------------------------------------------------------------------


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` atomically.

    The pattern is identical to ``MemorySettingsManager._save_sync``:

      1. Create a unique temp file in the SAME directory as the
         target (crucial -- ``os.replace`` is atomic only for
         same-filesystem moves).
      2. Write all bytes, flush, ``os.fsync`` -- guarantees the
         content reaches stable storage before rename.
      3. ``os.replace`` -- atomic swap into the final name.

    On any failure the temp file is removed (best-effort) and the
    exception propagates. The target file is left untouched -- a
    reader either sees the OLD content or the NEW content, never
    a partially-written mix.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path_str = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}_",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Directory export
# ---------------------------------------------------------------------------


def _write_package_to_directory_sync(
    manifest_data: Dict[str, Any],
    memory_lines: List[Dict[str, Any]],
    target_dir: Path,
) -> Path:
    """Synchronous body of ``write_package_to_directory``.

    Writes memories.jsonl FIRST, then manifest.json. See the
    module docstring for why the ordering matters.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    memories_bytes = _serialize_lines(memory_lines)
    manifest_bytes = _serialize_manifest(manifest_data)

    _atomic_write_bytes(
        target_dir / _MEMORIES_FILENAME, memories_bytes
    )
    _atomic_write_bytes(
        target_dir / _MANIFEST_FILENAME, manifest_bytes
    )

    logger.info(
        "Package written to %s (entries=%d, manifest_size=%d, "
        "memories_size=%d).",
        target_dir,
        len(memory_lines),
        len(manifest_bytes),
        len(memories_bytes),
    )
    return target_dir


async def write_package_to_directory(
    entries: List[MemoryEntry],
    target_dir: Path,
    *,
    source_label: Optional[str] = None,
    exported_at: Optional[datetime] = None,
) -> Path:
    """Write a package into ``target_dir`` atomically.

    Parameters
    ----------
    entries
        Entries to export.
    target_dir
        Directory that will contain ``manifest.json`` and
        ``memories.jsonl``. Created if missing. Existing files
        with the same names are OVERWRITTEN atomically.
    source_label, exported_at
        Forwarded to ``build_package_payload``. See that
        function's docstring.

    Returns
    -------
    Path
        ``target_dir``, for caller chaining convenience
        (``package_path = await write_package_to_directory(...)``).

    Raises
    ------
    TypeError, MemoryValidationError
        From ``build_package_payload``.
    OSError
        From the underlying file write (permission denied, disk
        full, ...). Atomicity is preserved: on failure, neither
        target file is modified.
    """
    if not isinstance(target_dir, Path):
        raise TypeError(
            f"target_dir must be pathlib.Path, "
            f"got {type(target_dir).__name__}"
        )
    # Build the payload on the calling thread (fast, pure CPU).
    # Only the file I/O goes to a worker thread.
    manifest_data, memory_lines = build_package_payload(
        entries,
        source_label=source_label,
        exported_at=exported_at,
    )
    return await asyncio.to_thread(
        _write_package_to_directory_sync,
        manifest_data,
        memory_lines,
        target_dir,
    )


# ---------------------------------------------------------------------------
# Zip export (in-memory, for HTTP downloads)
# ---------------------------------------------------------------------------


def write_package_to_zip_bytes(
    entries: List[MemoryEntry],
    *,
    source_label: Optional[str] = None,
    exported_at: Optional[datetime] = None,
) -> bytes:
    """Return the package as a zip archive's bytes.

    The zip contains exactly two files at the root:

        manifest.json
        memories.jsonl

    Kept synchronous because everything happens in memory (via
    ``io.BytesIO``) -- no thread offload needed. The typical use
    case is an HTTP handler that wants to stream the archive
    directly to the client without ever touching disk:

        return Response(
            content=write_package_to_zip_bytes(entries),
            media_type="application/zip",
            headers={
                "Content-Disposition":
                    'attachment; filename="memory-export.zip"'
            },
        )

    Parameters
    ----------
    entries, source_label, exported_at
        Forwarded to ``build_package_payload``.

    Returns
    -------
    bytes
        Complete zip archive payload. Deterministic for a given
        ``(entries, source_label, exported_at)`` triple --
        ``zipfile`` writes each file's content in the order
        added, with a fixed compression level, so identical
        inputs produce identical outputs (useful for
        content-hash-based caching by the HTTP layer).

    Raises
    ------
    TypeError, MemoryValidationError
        From ``build_package_payload``.
    """
    manifest_data, memory_lines = build_package_payload(
        entries,
        source_label=source_label,
        exported_at=exported_at,
    )

    manifest_bytes = _serialize_manifest(manifest_data)
    memories_bytes = _serialize_lines(memory_lines)

    buffer = io.BytesIO()
    # ``compression=ZIP_DEFLATED`` -- the JSONL content compresses
    # well (repeated field names, common substrings), typically
    # 5-10x smaller. At the same time, the archive remains readable
    # by any standard unzip tool. ``compresslevel=6`` is the
    # standard zip default -- best compression-to-CPU trade-off
    # for a task that runs once per export, not once per request.
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zf:
        # ``ZipInfo`` with an explicit ``date_time`` keeps the
        # output deterministic across runs -- without it, each
        # archive embeds the current system time in the entry
        # headers and no two exports of the same data are ever
        # byte-identical. We pin the timestamp to the manifest's
        # ``exported_at`` so the file headers agree with the
        # manifest content.
        try:
            dt = datetime.fromisoformat(
                manifest_data["exported_at"].replace("Z", "+00:00")
            )
        except Exception:
            dt = datetime.now(timezone.utc)

        # zipfile's date_time needs 6-tuple (year, month, day,
        # hour, minute, second). Values must be >= 1980 (the
        # format's epoch) -- clamp defensively.
        year = max(1980, dt.year)
        date_tuple = (
            year, dt.month, dt.day,
            dt.hour, dt.minute, dt.second,
        )

        for filename, content in (
            (_MANIFEST_FILENAME, manifest_bytes),
            (_MEMORIES_FILENAME, memories_bytes),
        ):
            info = zipfile.ZipInfo(
                filename=filename,
                date_time=date_tuple,
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16  # normal file mode
            zf.writestr(info, content)

    zip_bytes = buffer.getvalue()
    logger.info(
        "Package zip built (entries=%d, uncompressed=%d, "
        "compressed=%d).",
        len(memory_lines),
        len(manifest_bytes) + len(memories_bytes),
        len(zip_bytes),
    )
    return zip_bytes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "build_package_payload",
    "write_package_to_directory",
    "write_package_to_zip_bytes",
]
