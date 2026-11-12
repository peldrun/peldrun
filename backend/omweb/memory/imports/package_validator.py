# backend/omweb/memory/imports/package_validator.py
"""
PELDRUN Memory Platform - peldrun-memory-package Validator.

Defines and validates the OFFICIAL exchange format for the memory
subsystem: a ``peldrun-memory-package`` is a directory (or zip)
containing exactly:

    manifest.json
        Single JSON object describing the package: format version,
        origin, list of scopes, entry count, and export timestamp.
        Schema is a Pydantic model (``PeldrunMemoryPackageManifest``)
        with ``extra="forbid"`` -- any new field requires a
        ``format_version`` bump.

    memories.jsonl
        One JSON object per line, each representing ONE memory
        entry. The line format is deliberately NARROW: only the
        fields needed to RECONSTRUCT an entry. Fields that can be
        re-derived (``memory_id``, ``content_hash``,
        ``normalized_content``, ``updated_at``, ``last_used_at``)
        are FORBIDDEN in the package -- the validator recomputes
        them at import time.

Why recompute ``content_hash`` instead of trusting the package
--------------------------------------------------------------

A package produced by an older PELDRUN instance (or by a different
application entirely) may carry a ``content_hash`` computed by a
DIFFERENT normalizer version. If the importer preserved that hash,
the dedupe logic on the target instance -- which recomputes the
hash with the CURRENT normalizer -- would never match, and every
re-import would create a duplicate of every entry.

By re-deriving the hash from the entry's raw content through the
canonical ``MemoryEntry.create()`` path, the importer guarantees:

  * the hash always matches what a fresh save of the same content
    would produce on THIS instance (dedupe works), and
  * the ``normalized_content`` reflects the CURRENT normalizer's
    language rules (FTS works with the current tokenizer).

The consequence is that two PELDRUN instances with different
normalizer versions will produce different hashes for the same
content. That is CORRECT: within any single instance, hash
equality == content equivalence, which is the only invariant the
dedupe logic depends on.

Security invariant: user-scope requires explicit_user source
------------------------------------------------------------

Plan task 1.6.14: an imported package MUST NOT be able to write
into ``scope_type=user`` unless every user-scope entry in the
package carries ``source_type=explicit_user``. Rationale:

  * ``user:`` scope entries are, by contract, entries the USER
    explicitly confirmed. Allowing arbitrary imports to write
    there would let a package silently inject preferences the
    user never approved.
  * An entry that was originally ``explicit_user`` in the source
    instance (e.g. "prefers dark mode" that the user typed
    themselves on machine A) has a legitimate claim to be
    ``explicit_user`` on machine B -- the user's original
    intent carries over with the entry.
  * Entries with ``source_type=extraction`` or ``agent_tool``
    do NOT have that claim; they were inferred by the system,
    not asserted by the user. They may be imported into project
    scope but NEVER into user scope.

The validator enforces this rule at parse time by REJECTING any
user-scope entry that violates it. The rejection is per-entry
(the rest of the package still imports) and produces a clear
warning naming the offending line.

Cross-validation invariants
---------------------------

The validator checks the following BEFORE returning any entries:

    * ``manifest.format_version`` is the currently supported
      version (``PACKAGE_FORMAT_VERSION``). A newer version is
      rejected with a clear error -- the importer does not
      attempt best-effort partial imports for future versions.
    * ``manifest.count`` equals the actual line count in
      ``memories.jsonl``. A mismatch means the package was
      truncated during transfer, or hand-edited. Either is
      fatal.

Per-entry validation (non-fatal)
--------------------------------

Individual lines that fail are COUNTED and skipped, with a
warning describing the problem. The import proceeds with the
surviving entries. Only if ALL lines fail does the validator
raise a fatal error -- an import operation that would create
nothing is a sign of a genuine format mismatch, not a transient
line-level issue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemoryScopeRef,
    MemorySourceType,
    MemoryType,
    MemoryValidationError,
)


logger = logging.getLogger("omweb.memory.imports.package_validator")


# ---------------------------------------------------------------------------
# Format version
# ---------------------------------------------------------------------------

PACKAGE_FORMAT_VERSION: int = 1
"""Current ``peldrun-memory-package`` format version.

Bumping this is a BREAKING change: older PELDRUN instances will
refuse to import the new format (they validate for an exact match,
not a range). Producers must NOT bump without coordination -- the
correct path for additive changes is a NARROWER new field within
version 1; a genuinely incompatible change (new file, renamed
required field) requires version 2 with a new validator.

Version history
---------------
    * v1 -- initial format: ``manifest.json`` + ``memories.jsonl``.
"""


_MANIFEST_FILENAME: str = "manifest.json"
"""Canonical filename. ``package_writer`` writes to this name;
``package_validator`` callers should expect it."""

_MEMORIES_FILENAME: str = "memories.jsonl"
"""Canonical filename for the entry lines."""


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------


class PeldrunMemoryPackageManifest(BaseModel):
    """Validated manifest for a ``peldrun-memory-package``.

    All fields are required or have safe defaults. ``extra="forbid"``
    is deliberate: a package carrying an unfamiliar field is
    REJECTED, not silently accepted. This forces every format
    extension to bump ``format_version``, which in turn forces
    every consumer to explicitly support the new version. Without
    this discipline, two instances running slightly different
    PELDRUN versions could silently misinterpret each other's
    packages.

    Fields
    ------
    format_version
        Must equal ``PACKAGE_FORMAT_VERSION``. Validated by the
        caller (``validate_package``); this model just carries
        the value so that a mismatched-version error can name the
        version it found.

    source
        Free-form origin label -- e.g. ``"peldrun-web@1.7.0"``,
        ``"chatgpt-export-2026-03"``, ``"legacy-markdown-notes"``.
        Stored on every imported entry's ``provenance.imported_from``
        so the audit trail always names where a memory came from.
        Non-empty after trimming.

    scopes
        Complete list of ``"<scope_type>:<scope_id>"`` refs that
        the package's entries target. Each must be canonical
        (validated via ``MemoryScopeRef.from_string``). The list
        is INFORMATIONAL -- the importer does not require every
        entry's scope to appear here (an undeclared scope produces
        a warning, not a rejection), because producers with large
        packages may legitimately cap the manifest size. But
        consumers should treat an entry with an undeclared scope
        as suspicious and surface it in the warning list.

    count
        Total number of entries in ``memories.jsonl``. MUST equal
        the actual line count. Validated by ``validate_package``.

    exported_at
        ISO 8601 UTC timestamp of when the package was produced.
        Used for audit and for the "imported on X from package
        exported on Y" note in the write pipeline's provenance.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    format_version: int = Field(
        default=PACKAGE_FORMAT_VERSION,
        ge=1,
        description=(
            "Package format version. Must match the importer's "
            "supported version exactly. Enforced by "
            "``validate_package``, not by a field validator -- the "
            "manifest itself is a valid Pydantic model at ANY "
            "version, so a clear version-mismatch error can be "
            "produced instead of a generic Pydantic error."
        ),
    )
    source: str = Field(
        ...,
        description=(
            "Origin label. Non-empty after trimming. Copied to "
            "every imported entry's ``provenance.imported_from``."
        ),
    )
    scopes: List[str] = Field(
        default_factory=list,
        description=(
            "Canonical '<scope_type>:<scope_id>' refs targeted by "
            "the package. Informational -- validation only warns "
            "on mismatches (see class docstring)."
        ),
    )
    count: int = Field(
        default=0,
        ge=0,
        description=(
            "Total number of entries in memories.jsonl. "
            "Cross-validated against the actual line count."
        ),
    )
    exported_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description=(
            "ISO 8601 UTC timestamp of package production. Used "
            "for audit only."
        ),
    )

    @field_validator("source", mode="after")
    @classmethod
    def _validate_source(cls, v: str) -> str:
        """Trim and require non-empty."""
        if not isinstance(v, str):
            raise ValueError(
                f"source must be a string, got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError("source must not be empty")
        return stripped

    @field_validator("scopes", mode="after")
    @classmethod
    def _validate_scopes(cls, v: List[str]) -> List[str]:
        """Normalize each scope ref via ``MemoryScopeRef.from_string``.

        A non-canonical ref (missing ``:``, colon inside the id, ...)
        raises ``ValueError`` here, which becomes a fatal
        ``MemoryValidationError`` at the outer validator level --
        the manifest is supposed to be well-formed even if individual
        entries are not.
        """
        result: List[str] = []
        for idx, raw in enumerate(v):
            if not isinstance(raw, str):
                raise ValueError(
                    f"scopes[{idx}] must be a string, "
                    f"got {type(raw).__name__}"
                )
            try:
                parsed = MemoryScopeRef.from_string(raw)
            except ValueError as exc:
                raise ValueError(
                    f"scopes[{idx}] is not canonical: {exc}"
                ) from exc
            result.append(str(parsed))
        return result

    @field_validator("exported_at", mode="after")
    @classmethod
    def _validate_iso(cls, v: str) -> str:
        """Require an ISO 8601 parseable timestamp."""
        if not isinstance(v, str) or not v.strip():
            raise ValueError("exported_at must be a non-empty string")
        stripped = v.strip()
        try:
            datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except Exception as exc:
            raise ValueError(
                f"exported_at is not a valid ISO 8601 timestamp "
                f"({stripped!r}): {exc}"
            ) from exc
        return stripped


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass
class PackageValidationResult:
    """Result of validating a complete package.

    Attributes
    ----------
    manifest
        The validated manifest object.
    entries
        Freshly-constructed ``MemoryEntry`` objects, ready to be
        written. Hashes and normalized content are computed by the
        CURRENT normalizer -- NOT copied from the package.
    warnings
        Non-fatal issues encountered during validation. Each warning
        names the line number (1-based) or field it refers to. The
        import proceeds despite warnings.
    rejected_count
        Number of lines that failed validation and were therefore
        NOT included in ``entries``.
    """

    manifest: PeldrunMemoryPackageManifest
    entries: List[MemoryEntry] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rejected_count: int = 0

    @property
    def is_empty(self) -> bool:
        """True when validation produced no usable entries.

        The ONLY legitimate case is a genuinely empty package
        (``manifest.count == 0`` and ``memory_lines == []``) --
        an "import nothing" no-op that must succeed. The caller
        can inspect ``rejected_count`` to distinguish this
        ("empty package") from the fatal "all rejected" case,
        which never returns a result at all (it raises).
        """
        return not self.entries


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def validate_manifest(data: Dict[str, Any]) -> PeldrunMemoryPackageManifest:
    """Parse and structurally validate a manifest dict.

    Raises
    ------
    MemoryValidationError
        When ``data`` is not a dict, or fails any field validator,
        or contains an unexpected field (``extra="forbid"``).
    """
    if not isinstance(data, dict):
        raise MemoryValidationError(
            f"manifest must be a JSON object, got {type(data).__name__}",
            field_name="manifest",
        )
    try:
        return PeldrunMemoryPackageManifest.model_validate(data)
    except Exception as exc:
        raise MemoryValidationError(
            f"Invalid manifest: {exc}",
            field_name="manifest",
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# Per-entry reconstruction
# ---------------------------------------------------------------------------


def _rebuild_entry_from_line(
    raw: Dict[str, Any],
    *,
    manifest_source: str,
) -> Tuple[Optional[MemoryEntry], List[str], bool]:
    """Rebuild a ``MemoryEntry`` from a single ``memories.jsonl`` line.

    Returns
    -------
    (entry, warnings, is_security_rejection)
        ``entry`` is None when the line cannot be reconstructed
        (missing required field, unknown enum value, construction
        failure, security-rule violation). ``warnings`` names the
        specific reason even when ``entry`` is not None (e.g. an
        unparseable ``expires_at`` produces a warning but the
        entry is still returned with ``expires_at=None``).

        ``is_security_rejection`` is True only when the rejection
        was a POLICY decision -- specifically the user-scope +
        non-explicit-user rule from task 1.6.14. That flag
        distinguishes "we refuse this entry on principle" from
        "we cannot even parse this entry". The distinction drives
        the fatal-vs-warning decision in ``validate_package``.

    Important: ``content_hash``, ``normalized_content``,
    ``memory_id``, ``updated_at``, and ``last_used_at`` are
    DElIBERATELY NOT read from ``raw``. They are re-derived:
      * ``memory_id`` gets a fresh UUIDv4 fragment (no collision
        with existing entries on the target instance),
      * ``content_hash`` and ``normalized_content`` come from
        ``MemoryEntry.create()`` -- the current normalizer's
        output,
      * ``updated_at`` becomes "now" (the entry was last modified
        at import time),
      * ``last_used_at`` becomes None (the entry has never been
        used on this instance).

    The only field that IS preserved from the package for audit
    purposes is ``created_at``: it reflects when the memory was
    originally created on the source instance, and preserving it
    keeps the timeline meaningful across a migration. A caller
    who disagrees can override via the import_manager's apply
    step (see ``import_manager.py``).
    """
    warnings: List[str] = []

    if not isinstance(raw, dict):
        return None, [
            f"expected a JSON object, got {type(raw).__name__}"
        ], False

    # ------------------------------------------------------------------
    # 1. Required fields
    # ------------------------------------------------------------------
    try:
        content = raw["content"]
        memory_type_str = raw["memory_type"]
        scope_type_str = raw["scope_type"]
        scope_id = raw["scope_id"]
        source_type_str = raw["source_type"]
    except KeyError as exc:
        return None, [f"missing required field: {exc}"], False

    # ------------------------------------------------------------------
    # 2. Enum coercion (fail-fast with a clear message)
    # ------------------------------------------------------------------
    try:
        memory_type = MemoryType(memory_type_str)
    except (ValueError, TypeError):
        return None, [
            f"unknown memory_type: {memory_type_str!r}; "
            f"expected one of "
            f"{sorted(m.value for m in MemoryType)}"
        ], False

    try:
        scope_type = MemoryScope(scope_type_str)
    except (ValueError, TypeError):
        return None, [
            f"unknown scope_type: {scope_type_str!r}; "
            f"expected one of "
            f"{sorted(s.value for s in MemoryScope)}"
        ], False

    try:
        source_type = MemorySourceType(source_type_str)
    except (ValueError, TypeError):
        return None, [
            f"unknown source_type: {source_type_str!r}; "
            f"expected one of "
            f"{sorted(t.value for t in MemorySourceType)}"
        ], False

    # ------------------------------------------------------------------
    # 3. Security invariant (plan task 1.6.14)
    # ------------------------------------------------------------------
    #
    # User-scope entries MUST originate from ``explicit_user``. An
    # entry that was inferred (``extraction``) or invoked by an
    # agent (``agent_tool``) on the source instance has no claim to
    # write into ``user:`` scope here -- the user never explicitly
    # confirmed it. See the module docstring for the full rationale.
    if (
        scope_type == MemoryScope.USER
        and source_type != MemorySourceType.EXPLICIT_USER
    ):
        return None, [
            f"REJECTED (security): user-scope entry with "
            f"source_type={source_type.value!r}. Only "
            f"'explicit_user' entries may target user scope. "
            f"This prevents packages from injecting unconfirmed "
            f"preferences into the user's global memory."
        ], True  # ← True: policy rejection, NOT format rejection

    # ------------------------------------------------------------------
    # 4. Optional fields with safe defaults
    # ------------------------------------------------------------------
    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        warnings.append(
            f"ignoring non-list tags ({type(tags).__name__})"
        )
        tags = []

    try:
        importance = int(raw.get("importance", 50))
    except (ValueError, TypeError):
        warnings.append("unparseable importance; using 50")
        importance = 50

    try:
        confidence = float(raw.get("confidence", 1.0))
    except (ValueError, TypeError):
        warnings.append("unparseable confidence; using 1.0")
        confidence = 1.0

    is_pinned = bool(raw.get("is_pinned", False))

    # ------------------------------------------------------------------
    # 5. Optional datetimes (defensive parsing)
    # ------------------------------------------------------------------
    expires_at: Optional[datetime] = None
    raw_expires = raw.get("expires_at")
    if raw_expires:
        try:
            expires_at = datetime.fromisoformat(
                str(raw_expires).replace("Z", "+00:00")
            )
        except Exception:
            warnings.append(
                f"unparseable expires_at ({raw_expires!r}); "
                f"treating as non-expiring"
            )
            expires_at = None

    created_at_override: Optional[datetime] = None
    raw_created = raw.get("created_at")
    if raw_created:
        try:
            created_at_override = datetime.fromisoformat(
                str(raw_created).replace("Z", "+00:00")
            )
        except Exception:
            warnings.append(
                f"unparseable created_at ({raw_created!r}); "
                f"using import time"
            )
            created_at_override = None

    # ------------------------------------------------------------------
    # 6. Build the entry
    # ------------------------------------------------------------------
    #
    # ``imported_from`` is ALWAYS set to the manifest's source
    # label, even when the entry's own source_type is not
    # "import". The rationale: "this entry was imported from X"
    # is true regardless of what its original source_type was on
    # the source system. The audit trail captures BOTH facts
    # (source_type = original; imported_from = package origin).
    provenance = MemoryProvenance(
        source_type=source_type,
        imported_from=manifest_source,
    )

    try:
        entry = MemoryEntry.create(
            content=content,
            memory_type=memory_type,
            scope_type=scope_type,
            scope_id=scope_id,
            provenance=provenance,
            importance=importance,
            confidence=confidence,
            tags=tags,
            is_pinned=is_pinned,
            expires_at=expires_at,
        )
    except (ValueError, TypeError) as exc:
        return None, [f"entry construction failed: {exc}"], False

    # Apply the created_at override if the package carried one.
    # ``MemoryEntry.create`` set it to "now"; we want the audit
    # timeline to reflect the original creation time instead.
    if created_at_override is not None:
        entry = entry.model_copy(update={"created_at": created_at_override})

    return entry, warnings, False


# ---------------------------------------------------------------------------
# Package-level validation
# ---------------------------------------------------------------------------


def validate_package(
    manifest_data: Dict[str, Any],
    memory_lines: List[Dict[str, Any]],
    *,
    import_time: Optional[datetime] = None,
) -> PackageValidationResult:
    """Validate a complete ``peldrun-memory-package``.

    Parameters
    ----------
    manifest_data
        Parsed content of ``manifest.json``, as a Python dict.
    memory_lines
        Parsed content of ``memories.jsonl``, as a list of dicts
        (each corresponding to one JSON line). An empty list is a
        fatal error -- a package with zero entries has nothing to
        import.
    import_time
        The "now" reference for entries that lack a ``created_at``
        in the package. Injectable for deterministic tests.
        Defaults to ``datetime.now(timezone.utc)``.

    Returns
    -------
    PackageValidationResult
        Contains validated manifest, rebuilt entries, warnings,
        and rejected-count.

    Raises
    ------
    MemoryValidationError
        On any FATAL error:

          * manifest is not a dict, or fails schema validation,
          * ``format_version`` is not ``PACKAGE_FORMAT_VERSION``,
          * ``manifest.count`` != ``len(memory_lines)``,
          * every entry line was rejected (no usable entries).
    """
    # Resolve "now" once; every entry that needed a default uses
    # the same reference time.
    if import_time is None:
        import_time = datetime.now(timezone.utc)
    elif import_time.tzinfo is None:
        import_time = import_time.replace(tzinfo=timezone.utc)

    # ----- Manifest structure ---------------------------------------
    manifest = validate_manifest(manifest_data)

    # ----- Version check --------------------------------------------
    if manifest.format_version != PACKAGE_FORMAT_VERSION:
        raise MemoryValidationError(
            (
                f"Unsupported package format_version "
                f"{manifest.format_version}. This importer supports "
                f"only version {PACKAGE_FORMAT_VERSION}. If the "
                f"package was produced by a newer PELDRUN, upgrade "
                f"this instance before importing."
            ),
            field_name="format_version",
            context={
                "package_version": manifest.format_version,
                "supported_version": PACKAGE_FORMAT_VERSION,
            },
        )

    # ----- Count cross-check ----------------------------------------
    actual_count = len(memory_lines)
    if manifest.count != actual_count:
        raise MemoryValidationError(
            (
                f"Manifest count ({manifest.count}) does not match "
                f"the actual memories.jsonl line count "
                f"({actual_count}). The package may have been "
                f"truncated during transfer or edited after export."
            ),
            field_name="count",
            context={
                "manifest_count": manifest.count,
                "actual_lines": actual_count,
            },
        )

    # ----- Per-entry validation -------------------------------------
    entries: List[MemoryEntry] = []
    warnings: List[str] = []
    rejected = 0

    # Track whether ANY rejection was a POLICY decision (security)
    # vs a FORMAT failure (unknown enum, missing field, ...). The
    # distinction drives the fatal-vs-warning decision below.
    any_security_rejection: bool = False

    for idx, raw in enumerate(memory_lines):
        entry, line_warnings, is_security = _rebuild_entry_from_line(
            raw,
            manifest_source=manifest.source,
        )
        line_no = idx + 1
        if entry is None:
            rejected += 1
            if is_security:
                any_security_rejection = True
            for w in line_warnings:
                warnings.append(f"line {line_no}: {w}")
        else:
            entries.append(entry)
            for w in line_warnings:
                warnings.append(f"line {line_no}: {w}")

    # ----- Fatal only when ALL rejections were FORMAT failures ------
    #
    # Three distinguishable situations exist when ``not entries``:
    #
    #   1. **Empty package** (``actual_count == 0``): legitimate
    #      no-op. A producer that exported an empty scope set (a
    #      fresh project with no memories yet) sends zero entries,
    #      and importing zero entries is a successful operation.
    #      Return the result so the caller can proceed (nothing to
    #      do) rather than failing.
    #
    #   2. **All rejections were SECURITY rejections**: the package
    #      format is FINE -- the entries are structurally valid --
    #      but they target user scope without an explicit_user
    #      source marker. This is a POLICY refusal, not a parse
    #      failure. The user MUST see the warnings and decide
    #      whether to investigate. Return the result (with
    #      ``rejected_count == actual_count``) so that decision
    #      can be made at the ``import_manager`` layer.
    #
    #   3. **At least one FORMAT rejection** (unknown enum,
    #      missing field, construction failure): the package is
    #      genuinely incompatible with this PELDRUN version, OR
    #      the file was hand-edited into an invalid state. Fail
    #      loudly so the user is forced to investigate rather
    #      than silently importing nothing.
    #
    # The rule: raise ONLY when the package has entries AND NONE
    # of the rejections was security-based. If even ONE security
    # rejection occurred, the caller (import_manager) receives the
    # result and decides what to do.
    if actual_count > 0 and not entries and not any_security_rejection:
        raise MemoryValidationError(
            (
                f"All {actual_count} entries in the package were "
                f"rejected during validation. Nothing to import. "
                f"This usually means the package's memory_type / "
                f"scope_type / source_type values do not match this "
                f"PELDRUN version's vocabulary, OR the package was "
                f"hand-edited into an invalid state. First warning: "
                f"{(warnings[0] if warnings else '(none)')}"
            ),
            field_name="memories",
        )

    # ----- Scope sanity check (warning, not fatal) -------------------
    declared = set(manifest.scopes)
    if declared:
        for entry in entries:
            ref = f"{entry.scope_type.value}:{entry.scope_id}"
            if ref not in declared:
                warnings.append(
                    f"entry scope {ref!r} is not declared in "
                    f"manifest.scopes; importing anyway"
                )

    logger.info(
        "Package validated: source=%r, version=%d, "
        "entries=%d, rejected=%d, warnings=%d.",
        manifest.source,
        manifest.format_version,
        len(entries),
        rejected,
        len(warnings),
    )

    return PackageValidationResult(
        manifest=manifest,
        entries=entries,
        warnings=warnings,
        rejected_count=rejected,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "PACKAGE_FORMAT_VERSION",
    "PeldrunMemoryPackageManifest",
    "PackageValidationResult",
    "validate_manifest",
    "validate_package",
]
