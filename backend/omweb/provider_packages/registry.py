# backend/omweb/provider_packages/registry.py
"""
Persistent registry for installed Provider Packages.

Maintains two files on disk:

    <storage>/store/providers/
    ├── registry.json                   # the global index
    └── <provider_id>/
        ├── versions/<version>/         # extracted package files
        ├── environments/<version>/     # isolated venvs
        └── active.json                 # pointer to the active version

Why two files instead of one
----------------------------

The plan (Section 12) specifies a per-provider ``active.json`` in
addition to the global ``registry.json``. Keeping them separate serves
three concrete purposes:

1. **Fast lookup by the plugin host (phase 2).**
   When the Supervisor launches a worker for provider X, it needs to
   know "which version is active" without loading the entire registry
   index (which can carry metadata for dozens of packages). Reading
   ``<root>/<id>/active.json`` is a single small file open.

2. **Crash resilience.**
   If ``registry.json`` is corrupted by a partially-written write (it
   is written atomically, so this is rare, but the disk layer is the
   disk layer), the active pointers survive. The plugin host can still
   load the correct version of every package.

3. **Deterministic activation.**
   ``set_active_version`` writes ``active.json`` AFTER updating the
   registry. The plugin host can poll ``active.json`` to detect a live
   version switch without rereading the whole index.

The registry cache is invalidated on every write, so subsequent reads
return the latest committed state. Writes are atomic (tmp file +
``os.replace``); a mid-write crash leaves the previous valid file
untouched.

Design invariants
-----------------

1. **Atomic writes only.** ``registry.json`` and ``active.json`` are
   always written via tmp file + ``os.replace``. Readers never observe
   a partially written file.

2. **Idempotent install.** ``install_version`` merges the new version
   record into the existing entry, preserving older versions as
   rollback targets. Re-installing the exact same version replaces its
   record in place — it does not duplicate it.

3. **The user's enable/disable choice survives re-installs.**
   Re-installing an already-disabled provider does NOT re-enable it.
   The manager's caller (the UI) must call ``set_enabled(True)``
   explicitly to re-enable.

4. **Registry failures are loud, pointer failures are soft.**
   A failure to write ``registry.json`` raises
   ``ProviderPackageStorageError`` — the install is aborted. A failure
   to write ``active.json`` also raises (the pointer is critical for
   runtime correctness). A failure to DELETE ``active.json`` is logged
   but does not fail the surrounding ``remove_package`` — an orphaned
   pointer file is harmless (the plugin host ignores pointers to
   unregistered providers).

5. **The cache is trusted, not verified.** External writers (an admin
   editing ``registry.json`` with ``vim``) are supported via
   ``invalidate_cache()``. Under normal operation, the in-memory cache
   is the source of truth between writes.

See also
--------

    * ``backend/omweb/provider_packages/schema.py`` — model shapes
    * ``backend/omweb/provider_packages/errors.py`` — exception types
    * ``backend/omweb/provider_packages/package_manager.py`` — orchestrator
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omweb.provider_packages.errors import (
    ProviderPackageNotFoundError,
    ProviderPackageStorageError,
)
from omweb.provider_packages.schema import (
    ACTIVE_POINTER_FILENAME,
    REGISTRY_FILENAME,
    ProviderPackageEntry,
    ProviderPackageRegistryIndex,
    ProviderPackageVersionRecord,
)

logger = logging.getLogger("omweb.provider_packages.registry")


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with timezone."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Active pointer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivePointer:
    """
    Contents of ``<provider_id>/active.json``.

    The plugin host (phase 2) reads this to locate the venv and package
    files for the currently active version. All fields are strings for
    easy persistence and JSON round-tripping.
    """

    provider_id: str
    version: str
    hash: str
    activated_at: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "version": self.version,
            "hash": self.hash,
            "activated_at": self.activated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActivePointer":
        """
        Build an ``ActivePointer`` from a JSON-decoded dict.

        Missing or malformed fields are coerced to ``""`` so a
        hand-edited file never crashes the plugin host. The caller is
        responsible for deciding whether an empty version is usable
        (the plugin host treats empty as "not active" and refuses to
        launch).
        """
        return cls(
            provider_id=str(data.get("provider_id", "")),
            version=str(data.get("version", "")),
            hash=str(data.get("hash", "")),
            activated_at=str(data.get("activated_at", "")),
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ProviderPackageRegistry:
    """
    Thread-safe persistent registry for installed Provider Packages.

    Every mutating operation holds ``self._lock`` for the full read-
    modify-write cycle. Registry operations are infrequent (install,
    activate, delete) — they are not on the hot path of request
    handling — so holding the lock during disk I/O is acceptable and
    eliminates a whole class of race conditions.

    Reads (``get``, ``list_packages``, ``exists``, ``read_active``)
    also acquire the lock briefly, but only around the cache access
    or the file open — not around any computation the caller might
    want to do with the result.
    """

    def __init__(self, root: Path) -> None:
        """
        Parameters
        ----------
        root
            The provider-packages storage root, typically
            ``<backend>/storage/store/providers/``. Created lazily on
            the first write — the constructor does not touch disk.
        """
        self._root = Path(root).resolve()
        self._registry_file = self._root / REGISTRY_FILENAME
        self._lock = threading.RLock()
        self._cache: Optional[ProviderPackageRegistryIndex] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The storage root this registry writes under."""
        return self._root

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, provider_id: str) -> Optional[ProviderPackageEntry]:
        """Return the entry for ``provider_id``, or ``None`` if not installed."""
        if not provider_id:
            return None
        with self._lock:
            idx = self._load_registry()
            return idx.packages.get(provider_id)

    def get_or_raise(self, provider_id: str) -> ProviderPackageEntry:
        """
        Return the entry for ``provider_id``, or raise
        ``ProviderPackageNotFoundError`` if not installed.
        """
        entry = self.get(provider_id)
        if entry is None:
            raise ProviderPackageNotFoundError(
                f"Provider package '{provider_id}' is not installed.",
                details={"provider_id": provider_id},
            )
        return entry

    def exists(self, provider_id: str) -> bool:
        """Return True iff ``provider_id`` is installed (regardless of enabled state)."""
        return self.get(provider_id) is not None

    def is_enabled(self, provider_id: str) -> bool:
        """Return True iff the entry exists AND is enabled."""
        entry = self.get(provider_id)
        return entry is not None and entry.enabled

    def list_packages(
        self,
        *,
        enabled_only: bool = False,
    ) -> List[ProviderPackageEntry]:
        """
        Return a snapshot list of installed packages.

        Sorted by ``display_name or name`` for stable UI ordering.
        """
        with self._lock:
            idx = self._load_registry()
            entries = list(idx.packages.values())

        if enabled_only:
            entries = [e for e in entries if e.enabled]

        entries.sort(key=lambda e: (e.display_name or e.name or e.id).lower())
        return entries

    def get_version_record(
        self, provider_id: str, version: str
    ) -> Optional[ProviderPackageVersionRecord]:
        """
        Return the version record for ``version``, or ``None`` if not
        installed under that package.
        """
        entry = self.get(provider_id)
        if entry is None:
            return None
        return entry.get_version(version)

    # ------------------------------------------------------------------
    # Writes — install
    # ------------------------------------------------------------------

    def install_version(
        self,
        *,
        provider_id: str,
        version_record: ProviderPackageVersionRecord,
        name: str,
        display_name: Optional[str] = None,
        description: str = "",
        transport: str = "native",
        api_contract: str = "peldrun.llm.provider.v1",
        features: Optional[Dict[str, bool]] = None,
        permissions: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        author: Optional[str] = None,
        author_url: Optional[str] = None,
        homepage: Optional[str] = None,
        connection_fields: Optional[List[Dict[str, Any]]] = None,
        runtime_hints: Optional[Dict[str, Any]] = None,
        source: str = "uploaded",
        set_as_active: bool = True,
    ) -> ProviderPackageEntry:
        """
        Atomically install a version record into the registry.

        Behavior
        --------
        * If the provider is not yet registered, a fresh entry is
          created with ``installed_versions = [version_record]``.
        * If the provider already exists, its scalar fields are updated
          from the parameters above, its ``installed_versions`` list is
          merged (existing versions NOT in the new entry are preserved
          as rollback targets), and the new version record is added or
          replaced.
        * The user's enable/disable choice is preserved across re-installs.
        * When ``set_as_active=True`` (the default) the new version
          becomes the active version and ``active.json`` is written.

        Returns the resulting ``ProviderPackageEntry``.

        Raises
        ------
        ProviderPackageStorageError
            On any disk write failure.
        """
        if not provider_id:
            raise ProviderPackageStorageError(
                "Cannot install a version with an empty provider_id.",
            )

        with self._lock:
            idx = self._load_registry()
            existing = idx.packages.get(provider_id)

            if existing is None:
                entry = ProviderPackageEntry(
                    id=provider_id,
                    name=name,
                    display_name=display_name,
                    description=description,
                    active_version=version_record.version,
                    active_hash=version_record.hash,
                    enabled=True,
                    installed_versions=[version_record],
                    transport=transport,
                    api_contract=api_contract,
                    features=dict(features or {}),
                    permissions=dict(permissions or {}),
                    tags=list(tags or []),
                    author=author,
                    author_url=author_url,
                    homepage=homepage,
                    connection_fields=list(connection_fields or []),
                    runtime_hints=dict(runtime_hints or {}),
                    source=source,
                )
            else:
                # Preserve the user's enable/disable choice. A user who
                # disabled a provider does not expect a re-install to
                # silently flip it back on.
                was_enabled = existing.enabled

                # Replace scalar metadata from the new manifest.
                existing.name = name
                existing.description = description
                if display_name is not None:
                    existing.display_name = display_name
                existing.transport = transport
                existing.api_contract = api_contract
                existing.features = dict(features or {})
                existing.permissions = dict(permissions or {})
                existing.tags = list(tags or [])
                if author is not None:
                    existing.author = author
                if author_url is not None:
                    existing.author_url = author_url
                if homepage is not None:
                    existing.homepage = homepage
                # connection_fields and runtime_hints are REPLACED
                # unconditionally (not conditionally like author)
                # because they define the shape of the Configure form
                # and must always reflect the CURRENT manifest. A
                # rollback to an older version calls install_version
                # for that version, which replaces them too — so the
                # form always matches the active version's manifest.
                existing.connection_fields = list(connection_fields or [])
                existing.runtime_hints = dict(runtime_hints or {})
                existing.source = source

                # Merge installed_versions: replace or append.
                replaced = False
                for i, v in enumerate(existing.installed_versions):
                    if v.version == version_record.version:
                        existing.installed_versions[i] = version_record
                        replaced = True
                        break
                if not replaced:
                    existing.installed_versions.append(version_record)

                # Preserve the user's enable/disable choice.
                existing.enabled = was_enabled

                # Activate the new version.
                existing.active_version = version_record.version
                existing.active_hash = version_record.hash

                entry = existing

            idx.packages[provider_id] = entry
            self._save_registry(idx)

            # Write the active pointer AFTER the registry, so an
            # interrupted install never points at a version the
            # registry does not know about.
            if set_as_active:
                try:
                    self._write_active_pointer(
                        provider_id=provider_id,
                        version=entry.active_version,
                        hash=entry.active_hash,
                    )
                except ProviderPackageStorageError:
                    # The registry update succeeded; the pointer is
                    # missing. Roll back the registry write so the
                    # state is consistent, then re-raise.
                    logger.error(
                        "Failed to write active pointer for %r; "
                        "rolling back registry update.",
                        provider_id,
                    )
                    self._save_registry(self._load_registry_no_cache())
                    raise

            return entry

    # ------------------------------------------------------------------
    # Writes — activate / enable / disable
    # ------------------------------------------------------------------

    def set_active_version(
        self, provider_id: str, version: str
    ) -> ProviderPackageEntry:
        """
        Atomically switch the active version.

        The version MUST already be present in ``installed_versions``.
        Raises ``ProviderPackageNotFoundError`` when either the package
        or the version is unknown.

        The version directory on disk is NOT verified here — that is
        the manager's responsibility (it has the filesystem context to
        check ``versions/<version>/`` exists and the venv is present).
        """
        with self._lock:
            idx = self._load_registry()
            entry = idx.packages.get(provider_id)
            if entry is None:
                raise ProviderPackageNotFoundError(
                    f"Provider package '{provider_id}' is not installed.",
                    details={"provider_id": provider_id},
                )

            record = entry.get_version(version)
            if record is None:
                raise ProviderPackageNotFoundError(
                    f"Version '{version}' of provider '{provider_id}' "
                    f"is not installed. Installed versions: "
                    f"{[v.version for v in entry.installed_versions]}.",
                    details={
                        "provider_id": provider_id,
                        "requested_version": version,
                        "installed_versions": [
                            v.version for v in entry.installed_versions
                        ],
                    },
                )

            entry.active_version = version
            entry.active_hash = record.hash
            self._save_registry(idx)
            self._write_active_pointer(
                provider_id=provider_id,
                version=version,
                hash=record.hash,
            )
            return entry

    def set_enabled(
        self, provider_id: str, enabled: bool
    ) -> ProviderPackageEntry:
        """
        Toggle the runtime-enabled flag for a package.

        The registry file is updated; the active pointer is NOT touched
        (enabling/disabling is orthogonal to which version is active).
        """
        with self._lock:
            idx = self._load_registry()
            entry = idx.packages.get(provider_id)
            if entry is None:
                raise ProviderPackageNotFoundError(
                    f"Provider package '{provider_id}' is not installed.",
                    details={"provider_id": provider_id},
                )
            entry.enabled = bool(enabled)
            self._save_registry(idx)
            return entry

    # ------------------------------------------------------------------
    # Writes — delete
    # ------------------------------------------------------------------

    def remove_package(self, provider_id: str) -> Optional[ProviderPackageEntry]:
        """
        Remove an entire package from the registry.

        Returns the removed entry (useful to the manager, which needs
        to delete the corresponding version directories and venvs), or
        ``None`` if the package was not installed.

        The ``active.json`` pointer file is deleted best-effort. A
        failure to delete the pointer is logged but does NOT propagate
        — an orphaned pointer is harmless (the plugin host ignores
        pointers to unregistered providers).
        """
        with self._lock:
            idx = self._load_registry()
            entry = idx.packages.pop(provider_id, None)
            if entry is None:
                return None
            self._save_registry(idx)
            self._delete_active_pointer(provider_id)
            return entry

    # ------------------------------------------------------------------
    # Cache control
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """
        Drop the in-memory cache.

        Use this when an external process (e.g. an admin editing
        ``registry.json`` with ``vim``) may have modified the file.
        Under normal operation the cache is authoritative between
        writes and does not need invalidation.
        """
        with self._lock:
            self._cache = None

    # ------------------------------------------------------------------
    # Active pointer I/O
    # ------------------------------------------------------------------

    def read_active(self, provider_id: str) -> Optional[ActivePointer]:
        """
        Read the ``active.json`` pointer for ``provider_id``.

        Fallback behavior
        -----------------
        When the pointer file is missing or malformed, this method
        falls back to the registry's own ``active_version`` /
        ``active_hash`` fields (which always agree with the pointer
        after a successful write). This keeps the plugin host robust
        against an accidentally-deleted pointer file.

        Returns ``None`` only when the package is not registered at
        all (there is nothing to fall back to).
        """
        path = self._active_file_for(provider_id)

        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("version"):
                    return ActivePointer.from_dict(raw)
                logger.warning(
                    "active.json for %r is malformed; falling back to "
                    "registry.",
                    provider_id,
                )
            except Exception as exc:
                logger.warning(
                    "Could not read active.json for %r: %s — falling "
                    "back to registry.",
                    provider_id,
                    exc,
                )

        # Fallback: read from registry.
        entry = self.get(provider_id)
        if entry is None:
            return None
        return ActivePointer(
            provider_id=provider_id,
            version=entry.active_version,
            hash=entry.active_hash,
            activated_at="",
        )

    def _active_file_for(self, provider_id: str) -> Path:
        """Return the path of the ``active.json`` pointer for a provider."""
        return self._root / provider_id / ACTIVE_POINTER_FILENAME

    def _write_active_pointer(
        self, *, provider_id: str, version: str, hash: str
    ) -> None:
        """
        Write ``active.json`` atomically.

        Raises ``ProviderPackageStorageError`` on disk failure. The
        caller (``install_version`` / ``set_active_version``) has
        already committed the registry update at this point, so a
        failure here is rolled back by the caller where possible.
        """
        path = self._active_file_for(provider_id)
        payload = ActivePointer(
            provider_id=provider_id,
            version=version,
            hash=hash,
            activated_at=_utc_now_iso(),
        ).to_dict()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(path))
        except OSError as exc:
            raise ProviderPackageStorageError(
                f"Failed to persist active pointer for "
                f"'{provider_id}': {exc}",
                details={"path": str(path)},
            ) from exc

    def _delete_active_pointer(self, provider_id: str) -> None:
        """Delete ``active.json`` best-effort. Never raises."""
        path = self._active_file_for(provider_id)
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            logger.warning(
                "Could not delete active pointer for %r at %s: %s",
                provider_id,
                path,
                exc,
            )

    # ------------------------------------------------------------------
    # Registry file I/O
    # ------------------------------------------------------------------

    def _load_registry(self) -> ProviderPackageRegistryIndex:
        """
        Load the registry, using the in-memory cache when valid.

        Callers MUST hold ``self._lock``. A failed read returns a
        freshly-built empty index — the caller may then write it back
        to recover a corrupted file.
        """
        if self._cache is not None:
            return self._cache

        return self._load_registry_no_cache()

    def _load_registry_no_cache(self) -> ProviderPackageRegistryIndex:
        """
        Read ``registry.json`` from disk without consulting or
        repopulating the cache.

        Used by the rollback path in ``install_version``, which needs
        the actual on-disk state — the cache is being discarded.

        Callers MUST hold ``self._lock``.
        """
        if not self._registry_file.is_file():
            idx = ProviderPackageRegistryIndex()
            self._cache = idx
            return idx

        try:
            raw = self._registry_file.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            idx = ProviderPackageRegistryIndex.model_validate(parsed)
            self._cache = idx
            return idx
        except Exception as exc:
            logger.warning(
                "registry.json at %s could not be loaded (%s). Starting "
                "from an empty index. The previous contents are NOT "
                "deleted from disk — inspect and repair the file before "
                "the next write, or the corruption will be preserved.",
                self._registry_file,
                exc,
            )
            idx = ProviderPackageRegistryIndex()
            self._cache = idx
            return idx

    def _save_registry(self, idx: ProviderPackageRegistryIndex) -> None:
        """
        Atomically persist ``registry.json`` and update the cache.

        Raises ``ProviderPackageStorageError`` on any failure. The
        previous on-disk file remains untouched in that case (the tmp
        file is deleted on failure so the next attempt starts clean).

        Callers MUST hold ``self._lock``.
        """
        idx.updated_at = _utc_now_iso()
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = self._registry_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(idx.model_dump(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(self._registry_file))
            self._cache = idx
        except OSError as exc:
            # Clean up the tmp file so a retry is not poisoned by a
            # stale half-write.
            try:
                tmp.unlink()
            except Exception:
                pass
            raise ProviderPackageStorageError(
                f"Failed to persist registry at "
                f"{self._registry_file}: {exc}",
                details={"path": str(self._registry_file)},
            ) from exc


# ---------------------------------------------------------------------------
# Default root + module-level singleton
# ---------------------------------------------------------------------------


def _default_root() -> Path:
    """
    Compute the default storage root for provider packages.

    Layout: ``<backend>/storage/store/providers/``

    Mirrors the location used by tool bundles
    (``<backend>/storage/store/tools/``) so the storage tree is
    consistent across subsystems.
    """
    # __file__ = backend/omweb/provider_packages/registry.py
    # .parent            = backend/omweb/provider_packages/
    # .parent.parent     = backend/omweb/
    # .parent.parent.parent = backend/
    backend_root = Path(__file__).resolve().parent.parent.parent
    return backend_root / "storage" / "store" / "providers"


#: Process-wide registry singleton. Points at the default storage root.
#: Tests that need isolation should build their own
#: ``ProviderPackageRegistry(root=tmp_path)``.
provider_package_registry = ProviderPackageRegistry(_default_root())


__all__ = [
    "ActivePointer",
    "ProviderPackageRegistry",
    "provider_package_registry",
]
