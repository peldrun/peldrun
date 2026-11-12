# backend/omweb/memory/manager/memory_settings_manager.py
"""
PELDRUN Memory Platform - Persistent Memory Settings Store.

Loads and persists ``MemorySettings`` to a JSON file with atomic
writes. Provides the sole durable configuration interface for the
memory subsystem (P1.7 task 1.7.1).

File location
-------------
Default: ``<storage_root>/memory_settings.json``.
Override: pass ``path=`` explicitly (used by tests).

The file is a straight JSON serialization of
``MemorySettings.model_dump(mode="json")`` -- no custom envelope,
no version field. ``MemorySettings`` itself is designed to be
round-trip safe (see ``peldrun.memory.domain.settings``'s module
docstring), so read/write is a single ``model_validate`` /
``model_dump`` pair.

Design invariants
-----------------

1. **Atomic writes.**
   Every save writes to a temp file in the same directory, fsyncs,
   then ``os.replace()``s the target. A crash mid-write leaves the
   OLD file intact -- the temp file is orphaned but harmless.
   ``os.replace`` is atomic on NTFS and POSIX; a concurrent reader
   always sees a fully-formed file.

2. **Never crash on a broken file.**
   Missing file -> defaults, no warning (first boot).
   Unreadable / malformed JSON / invalid field -> defaults, WARN log.
   Corrupted settings are an operator-visible event, but they must
   NOT block startup or crash a running turn.

3. **No caching.**
   ``load()`` reads from disk every call. The settings file is
   small (a few KB) and the read is one ``to_thread`` hop, so the
   performance cost is negligible. Caching was rejected because:

     * it introduces an invalidation bug surface (a save from one
       caller must invalidate every other cached copy),
     * the acceptance test for P1.7 (#9) requires that a settings
       change mid-run affects only the NEXT run -- impossible to
       reason about cleanly with a shared mutable cache.

   ``MemoryService`` (P1.7 task 1.7.3) holds a SINGLE settings
   instance for the lifetime of one ``retrieve()`` call. Saving
   through this manager writes a NEW instance to disk; the next
   ``retrieve()`` loads it. Old in-flight runs keep their old
   instance by reference.

4. **Concurrent writes serialize on an asyncio.Lock.**
   The lock is per-manager-instance. Production uses a singleton
   (see below), so all writers (API endpoints, auto-reload hooks)
   serialize against each other. Tests that build a fresh manager
   per test get their own lock -- no cross-test interference.

5. **Validation on update.**
   ``update(**kwargs)`` re-validates the merged result through
   ``MemorySettings.model_validate``. This catches type mismatches
   (e.g. ``mode="bogus"``) and Pydantic-level constraints
   (``reserved_for_user_scope > max_tokens``) that
   ``model_copy(update=...)`` would silently skip.

Module-level singleton
----------------------
The ``MemoryService`` (P1.7 task 1.7.3) constructs one manager at
startup and holds it for the process lifetime. The module does
NOT expose a global ``get_settings_manager()`` -- that is the
service's job. This keeps the manager unit-testable in isolation
(constructor takes an explicit path).

Project override delta preservation
-----------------------------------
``MemorySettings.project_overrides`` maps project ids to
``MemorySettings`` instances whose **explicitly-set fields**
override the corresponding global fields. The mechanism relies
on ``model_fields_set`` (Pydantic's record of which fields were
passed to the constructor vs. filled by defaults). Two places
in this manager MUST preserve that invariant:

  1. ``update()`` -- when merging the current settings with the
     caller's kwargs, ``project_overrides`` is excluded from the
     ``model_dump`` and the original override OBJECTS are re-
     injected by identity (rather than their materialized dict
     form). Otherwise ``model_validate`` would reconstruct each
     override with ``model_fields_set`` = all fields.
  2. ``_save_sync()`` -- when writing to disk, each override is
     serialized as a DELTA (only ``model_fields_set`` keys), not
     the fully-materialized form.

If either invariant is broken, an override saved to disk and
reloaded becomes a full snapshot: subsequent changes to the
global settings would NOT propagate to projects that did not
override that specific field. The ``P1.8`` smoke suite's
Test 11 exercises this end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from peldrun.memory.domain import MemorySettings, default_settings


logger = logging.getLogger("omweb.memory.manager.settings")


class MemorySettingsManager:
    """Loads and persists ``MemorySettings`` to a JSON file.

    See the module docstring for the design rationale and the
    file format specification.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        """Construct the manager.

        Parameters
        ----------
        path
            Explicit file path. When ``None`` (production default),
            resolves to ``<storage_root>/memory_settings.json``
            lazily -- the storage root is not resolved at
            construction time so that tests can override it via
            ``PELDRUN_STORAGE_ROOT`` without racing an import.

        Raises
        ------
        TypeError
            When ``path`` is neither ``None`` nor a ``Path``.
        """
        if path is not None and not isinstance(path, Path):
            raise TypeError(
                f"path must be pathlib.Path or None, "
                f"got {type(path).__name__}"
            )
        self._explicit_path: Optional[Path] = (
            Path(path) if path is not None else None
        )
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """Return the effective file path (resolved on first access)."""
        if self._explicit_path is not None:
            return self._explicit_path
        from peldrun.storage_paths import get_storage_root

        return get_storage_root() / "memory_settings.json"

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    async def load(self) -> MemorySettings:
        """Load settings from disk. Never raises.

        Failure modes map to the P1.7 "operator-visible but not
        fatal" rule:

          * missing file   -> ``default_settings()``, INFO log
            (first boot -- not a problem worth a WARNING),
          * unreadable or
            malformed JSON -> ``default_settings()``, WARNING log,
          * invalid fields -> ``default_settings()``, WARNING log.

        A concurrent reader sees either the old or the new file
        atomically (``os.replace`` guarantee) -- never a partial
        JSON document.
        """
        return await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> MemorySettings:
        target = self.path
        if not target.exists():
            logger.info(
                "MemorySettingsManager: no settings file at %s; "
                "using defaults.",
                target,
            )
            return default_settings()

        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "MemorySettingsManager: cannot read %s: %s. Using "
                "defaults.",
                target,
                exc,
            )
            return default_settings()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "MemorySettingsManager: %s is not valid JSON (%s). "
                "Using defaults.",
                target,
                exc,
            )
            return default_settings()

        try:
            return MemorySettings.model_validate(data)
        except Exception as exc:
            logger.warning(
                "MemorySettingsManager: %s contains invalid "
                "settings: %s. Using defaults.",
                target,
                exc,
            )
            return default_settings()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    async def save(self, settings: MemorySettings) -> None:
        """Persist ``settings`` to disk atomically.

        Raises
        ------
        TypeError
            When ``settings`` is not a ``MemorySettings`` instance.
        OSError
            When the write fails at the OS level (permission denied,
            disk full). Atomicity is guaranteed: the target file is
            either fully replaced or left untouched.
        """
        if not isinstance(settings, MemorySettings):
            raise TypeError(
                f"settings must be MemorySettings, "
                f"got {type(settings).__name__}"
            )
        async with self._lock:
            await asyncio.to_thread(self._save_sync, settings)

    def _save_sync(self, settings: MemorySettings) -> None:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)

        payload = settings.model_dump(mode="json")

        # [FIX-DELTA-OVERRIDES] Serialize each project override as
        # a DELTA -- only the fields the user actually configured,
        # not the fully-materialized form.
        #
        # ``model_dump(mode="json")`` on a MemorySettings fully
        # materializes each override into a plain dict with every
        # field populated (defaults included). Writing that to disk
        # would lose the "delta" information: on the next ``load()``,
        # Pydantic reconstructs each override from the full dict,
        # so its ``model_fields_set`` becomes ALL field names.
        # ``MemorySettings.effective_for_project`` then treats every
        # override as a full snapshot (no fields inherit from
        # global) -- breaking the intended delta-merge semantics.
        #
        # Concretely: a project overrode only ``default_limit=3``.
        # Later the operator raises the global
        # ``min_relevance_score`` to 0.5. Without this fix, the
        # project would keep the override's default 0.3 -- the
        # global change silently does not propagate. Test 11 in
        # the P1.8 smoke suite catches exactly this.
        #
        # The fix reads ``model_fields_set`` (which ``update()``
        # preserved via object identity -- see its docstring) and
        # strips the materialized dump to only those keys.
        if (
            payload.get("project_overrides")
            and settings.project_overrides
        ):
            delta_overrides: dict = {}
            for pid, override_obj in settings.project_overrides.items():
                # ``project_overrides`` itself must never appear
                # on an override (nested overrides are forbidden
                # by MemorySettings's own validator, but strip
                # defensively here too).
                delta_fields = (
                    override_obj.model_fields_set
                    - {"project_overrides"}
                )
                full_dump = (
                    payload["project_overrides"].get(pid) or {}
                )
                if delta_fields:
                    delta_overrides[pid] = {
                        k: v
                        for k, v in full_dump.items()
                        if k in delta_fields
                    }
                else:
                    # Edge case: an override was attached with
                    # NO explicitly-set fields (e.g. an empty
                    # MemorySettings created via model_validate({})).
                    # Preserve the "override exists" fact as an
                    # empty dict -- ``without_project_override``
                    # is the right way to remove it, not "PUT an
                    # empty override".
                    delta_overrides[pid] = {}
            payload["project_overrides"] = delta_overrides

        # Atomic write: temp file in the same directory, then
        # os.replace(). ``tempfile.mkstemp`` creates the file with
        # O_EXCL so two concurrent writers can never clobber each
        # other's temp file.
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".memory_settings_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path_str, target)
            logger.info(
                "MemorySettingsManager: persisted settings to %s "
                "(mode=%s, provider=%s).",
                target,
                payload.get("mode"),
                payload.get("provider_id"),
            )
        except Exception:
            # Best-effort cleanup of the temp file. Any failure
            # here must not mask the original exception.
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Update (convenience)
    # ------------------------------------------------------------------

    async def update(self, **kwargs: Any) -> MemorySettings:
        """Load, apply ``kwargs`` as field overrides, validate, save.

        The result is fully re-validated by Pydantic before the
        write, so an invalid override (e.g. ``mode="bogus"`` or a
        ``reserved_for_user_scope`` exceeding ``max_tokens``)
        raises BEFORE any file is touched. This is the primary
        entry point the settings API uses (P1.7 task 1.7.6).

        Returns
        -------
        MemorySettings
            The fully-validated new instance that was just written.

        Raises
        ------
        pydantic.ValidationError
            When the merged data violates a constraint.
        OSError
            When the atomic write fails (see ``save``).
        """
        async with self._lock:
            current = await asyncio.to_thread(self._load_sync)

            # Merge via model_dump + model_validate so partial
            # updates cannot bypass Pydantic constraints.
            # ``model_copy(update=...)`` was rejected because it
            # skips field validation -- a caller could set
            # ``mode="Read_Write"`` (wrong case) and end up with a
            # bare string where an enum is expected.
            #
            # [FIX-DELTA-OVERRIDES] ``model_dump(mode="json")`` on
            # a MemorySettings fully MATERIALIZES each project
            # override into a plain dict (all fields). Re-validating
            # that dump would reconstruct each override with
            # ``model_fields_set`` containing ALL field names -- the
            # "delta" information (which fields the user actually
            # configured vs. inherited from defaults) would be
            # permanently lost. That breaks
            # ``MemorySettings.effective_for_project``'s delta merge
            # (see its docstring and the same fix in ``_save_sync``).
            #
            # The fix: exclude ``project_overrides`` from the dump
            # and re-inject the ORIGINAL override OBJECTS (which
            # carry the correct ``model_fields_set``) when the
            # caller did not supply new ones. Pydantic's
            # ``Dict[str, MemorySettings]`` field accepts
            # already-validated MemorySettings instances by
            # identity without re-validating them, preserving
            # ``model_fields_set`` on each.
            current_overrides = dict(current.project_overrides)
            merged_data = current.model_dump(
                mode="json",
                exclude={"project_overrides"},
            )
            merged_data.update(kwargs)

            if "project_overrides" in kwargs:
                # Caller supplied fresh overrides (from
                # ``with_project_override`` / ``without_project_override``).
                # Those objects were constructed with correct
                # ``model_fields_set``; pass them through verbatim.
                merged_data["project_overrides"] = kwargs[
                    "project_overrides"
                ]
            else:
                # Preserve existing overrides by OBJECT IDENTITY
                # (not by their materialized dict form).
                merged_data["project_overrides"] = current_overrides

            new_settings = MemorySettings.model_validate(merged_data)

            await asyncio.to_thread(self._save_sync, new_settings)
            return new_settings


__all__ = ["MemorySettingsManager"]
