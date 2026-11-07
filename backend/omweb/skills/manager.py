# backend/omweb/skills/manager.py
"""
Skill lifecycle manager: install, delete, activate, and query skills.

Responsibilities:
    * Orchestrate the storage and registry layers.
    * Provide a single, stable API surface for the HTTP router
      (``omweb.routers.skills``) and for the engine's prompt injection
      path (``peldrun_engine``).
    * Keep the two layers in sync: any mutation on disk triggers a
      matching in-memory refresh.
    * Log every lifecycle operation at INFO with enough context to
      diagnose user-facing problems from the server logs alone.

Non-responsibilities (kept in their own modules):
    * Parsing and validation        — ``omweb.skills.manifest``
    * Disk layout                   — ``omweb.skills.storage``
    * In-memory cache + state file  — ``omweb.skills.registry``
    * Content loading               — ``omweb.skills.loader``
    * Agent assignments             — ``omweb.skills.assignments``

Sync guarantees:
    * ``install_skill`` — install on disk first, then refresh registry.
      If refresh fails (unlikely: it just re-runs discovery), the disk
      state is already correct and the next read picks it up.
    * ``delete_skill`` — delete on disk first, then remove from
      registry. Same fail-safe ordering.
    * A partial failure never leaves the registry claiming a skill
      exists when the disk no longer has it.

Orphan cleanup:
    ``cleanup_orphans`` runs:
        * automatically before every ``install_skill`` (cheap, once per
          install, protects against a crashed previous install), and
        * explicitly from the FastAPI lifespan handler on startup.
    It removes stale ``.tmp_*`` and ``.trash_*`` directories left
    behind by a crash.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from omweb.skills.errors import (
    SkillError,
    SkillNotFoundError,
    SkillSecurityError,
    SkillStorageError,
    SkillValidationError,
)
import os

from omweb.skills.loader import load_icon, load_instructions
from omweb.skills.manifest import (
    DEFAULT_INSTRUCTIONS_FILE,
    DEFAULT_MAX_INSTRUCTIONS_BYTES,
    MAX_INSTRUCTIONS_BYTES,
    parse_skill_md,
)
from omweb.skills.registry import (
    SkillRecord,
    SkillRegistry,
    normalize_skill_id,
    skill_registry,
)
from omweb.skills.storage import (
    SkillInstallResult,
    SkillStorage,
)


logger = logging.getLogger("omweb.skills.manager")


# ---------------------------------------------------------------------------
# SkillManager
# ---------------------------------------------------------------------------

class SkillManager:
    """
    Public entry point for the skills subsystem.

    Constructed once at module load with the process-wide
    ``skill_registry`` singleton and a ``SkillStorage`` rooted at
    ``skill_registry.skills_dir``. Tests can pass their own instances
    to isolate state without touching the production singleton.
    """

    def __init__(
        self,
        *,
        registry: Optional[SkillRegistry] = None,
        storage: Optional[SkillStorage] = None,
    ) -> None:
        self.registry: SkillRegistry = registry or skill_registry
        if storage is not None:
            self.storage: SkillStorage = storage
        else:
            self.storage = SkillStorage(self.registry.skills_dir)

    # ── Reads ────────────────────────────────────────────────────────

    def list_skills(
        self,
        *,
        include_disabled: bool = True,
        include_deprecated: bool = True,
    ) -> List[SkillRecord]:
        """
        Return all installed skills, sorted by canonical ID.

        Args:
            include_disabled:   Include skills whose status is disabled.
            include_deprecated: Include skills marked deprecated.
        """
        return self.registry.list_skills(
            include_disabled=include_disabled,
            include_deprecated=include_deprecated,
        )

    def search_skills(
        self,
        query: str,
        *,
        include_disabled: bool = False,
        include_deprecated: bool = False,
        limit: int = 30,
    ) -> List[SkillRecord]:
        """
        Rank installed skills against a free-form query.

        Used by:
            * ``GET /api/skills?search=...`` — the store page filter.
            * The ``/`` typeahead menu — the frontend queries the API
              with the user's typed prefix and ranks the results here.

        Disabled and deprecated skills are excluded by default; callers
        opt in via the include-* flags.
        """
        return self.registry.search_skills(
            query,
            include_disabled=include_disabled,
            include_deprecated=include_deprecated,
            limit=limit,
        )

    def get_skill(self, skill_id: str) -> Optional[SkillRecord]:
        """Resolve a skill by ID (accepts aliases and normalized forms)."""
        return self.registry.get_skill(skill_id)

    def get_skill_or_raise(self, skill_id: str) -> SkillRecord:
        """Resolve a skill or raise ``SkillNotFoundError``."""
        return self.registry.get_skill_or_raise(skill_id)

    def is_enabled(self, skill_id: str) -> bool:
        """True when the skill exists and its status is ``active``."""
        return self.registry.is_enabled(skill_id)

    def get_skill_full(self, skill_id: str) -> Dict[str, Any]:
        """
        Return the full record as a dict for the details drawer.

        Includes the raw ``manifest`` and the ``SKILL.md`` frontmatter.
        Does NOT include the instructions body (potentially up to
        1 MiB) — use ``get_skill_instructions`` for that.
        """
        return self.get_skill_or_raise(skill_id).to_full_dict()

    def get_skill_instructions(self, skill_id: str) -> str:
        """
        Return the skill's instructions text (body, no frontmatter).

        Enforces ``limits.max_instructions_bytes``. Raises
        ``SkillNotFoundError`` when the ID does not resolve, and
        ``SkillLoadError`` when the stored instructions are empty or
        oversized.
        """
        record = self.get_skill_or_raise(skill_id)
        return load_instructions(record)

    def get_skill_icon(self, skill_id: str) -> Optional[Dict[str, Any]]:
        """
        Return the skill's sanitized icon, or ``None`` when the skill
        declares no icon.

        The dict has the shape::

            {"kind": "inline" | "file",
             "content": "<svg>...</svg>",
             "content_type": "image/svg+xml"}

        Raises ``SkillNotFoundError`` when the ID does not resolve.
        ``SkillLoadError`` / ``SkillSecurityError`` from the underlying
        file read propagates to the caller (the router translates them
        to 404 / 400 accordingly).
        """
        record = self.get_skill_or_raise(skill_id)
        return load_icon(record)

    def update_skill_instructions(
        self,
        skill_id: str,
        new_body: str,
    ) -> SkillRecord:
        """
        Replace the SKILL.md body (below the YAML frontmatter) with
        ``new_body``.

        The frontmatter block is preserved verbatim — it is the
        canonical source of ``name`` / ``description`` / any author
        custom keys, and rewriting it would risk silent loss. Only the
        Markdown body after the closing ``---`` is replaced.

        Validation chain (in order):
            1. ``user_editable`` must be true (403 otherwise). Built-in
               skills are always non-editable.
            2. Body must be non-empty.
            3. Body must not start with ``---`` (prevents double-
               frontmatter, the most common copy/paste mistake).
            4. Body size must fit ``limits.max_instructions_bytes``
               (narrowed by the absolute ``MAX_INSTRUCTIONS_BYTES``).
            5. Recomposed file (frontmatter + new body) must re-parse
               cleanly through ``parse_skill_md``.
            6. Atomic write via ``.md.tmp`` + ``os.replace``.

        After the write, the registry is refreshed so the next run of
        ``resolve_skill_injection`` picks up the new body immediately.
        If the refresh itself fails (e.g. a concurrent delete), the
        on-disk change still succeeded; we fall back to mutating the
        in-memory record and log a warning — the caller gets the
        updated record either way.

        Args:
            skill_id:  Canonical ID, alias, or normalized form.
            new_body:  Markdown body WITHOUT frontmatter.

        Raises:
            SkillNotFoundError:   when the ID does not resolve.
            SkillSecurityError:   when ``user_editable`` is false.
            SkillValidationError: on empty body, leading ``---``,
                                  oversized body, or re-parse failure.
            SkillStorageError:    on filesystem read/write failure.
        """
        import re as _re

        # 1. Resolve record + editability gate (before any disk I/O)
        record = self.get_skill_or_raise(skill_id)

        if not record.user_editable:
            raise SkillSecurityError(
                f"Skill '{record.id}' is not user-editable.",
                details={
                    "skill_id": record.id,
                    "reason": "user_editable_false",
                    "is_builtin": record.is_builtin,
                },
            )

        # 2. Basic body validation
        body = (new_body or "").strip()
        if not body:
            raise SkillValidationError(
                "Instructions body cannot be empty.",
                details={
                    "skill_id": record.id,
                    "reason": "empty_body",
                },
            )

        if body.startswith("---"):
            raise SkillValidationError(
                "Instructions must not include a frontmatter block. "
                "The existing frontmatter is preserved automatically.",
                details={
                    "skill_id": record.id,
                    "reason": "frontmatter_in_body",
                },
            )

        # 3. Enforce size cap (manifest may narrow the absolute ceiling)
        declared_cap = (
            (record.manifest.get("limits") or {}).get(
                "max_instructions_bytes"
            )
            if isinstance(record.manifest.get("limits"), dict)
            else None
        )
        # Effective ceiling: min(declared, hard). When the manifest omits
        # the field, DEFAULT_MAX_INSTRUCTIONS_BYTES (256 KiB) applies —
        # matching the JSON Schema default and install-time validation.
        max_bytes = DEFAULT_MAX_INSTRUCTIONS_BYTES
        if isinstance(declared_cap, int) and declared_cap > 0:
            max_bytes = min(declared_cap, MAX_INSTRUCTIONS_BYTES)

        body_bytes = len(body.encode("utf-8"))
        if body_bytes > max_bytes:
            raise SkillValidationError(
                f"Instructions exceed {max_bytes} bytes "
                f"({body_bytes} actual).",
                details={
                    "skill_id": record.id,
                    "reason": "instructions_too_large",
                    "size_bytes": body_bytes,
                    "limit": max_bytes,
                },
            )

        # 4. Read the existing SKILL.md and extract the frontmatter block
        execution_block = record.manifest.get("execution") or {}
        instructions_file = (
            execution_block.get("instructions_file")
            if isinstance(execution_block, dict)
            else None
        ) or DEFAULT_INSTRUCTIONS_FILE

        instructions_path = record.package_dir / instructions_file
        if not instructions_path.is_file():
            raise SkillValidationError(
                f"Instructions file '{instructions_file}' is missing "
                f"on disk.",
                details={
                    "skill_id": record.id,
                    "path": str(instructions_path),
                },
            )

        try:
            original = instructions_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillStorageError(
                f"Failed to read {instructions_file}: {exc}",
                details={
                    "skill_id": record.id,
                    "path": str(instructions_path),
                },
            ) from exc

        # Accept both \n and \r\n line endings. The block ends with a
        # trailing newline so the recomposed file keeps the blank line
        # between frontmatter and body that every PELDRUN skill ships
        # with (see parse_skill_md's delimiter contract).
        match = _re.search(
            r"\A---\r?\n(.*?)\r?\n---\r?\n",
            original,
            _re.DOTALL,
        )
        if not match:
            raise SkillValidationError(
                f"{instructions_file} has no YAML frontmatter block. "
                f"The file may be corrupted; reinstall the skill.",
                details={
                    "skill_id": record.id,
                    "reason": "missing_frontmatter",
                },
            )
        frontmatter_block = match.group(0)  # includes '---\n...\n---\n'

        # 5. Recompose and re-validate the full file BEFORE writing it
        new_content = frontmatter_block + "\n" + body + "\n"

        try:
            parse_skill_md(new_content)
        except Exception as exc:
            raise SkillValidationError(
                f"Recomposed {instructions_file} failed validation: "
                f"{exc}",
                details={
                    "skill_id": record.id,
                    "reason": "recompose_invalid",
                },
            ) from exc

        # 6. Atomic write: temp file + fsync + os.replace
        tmp_path = instructions_path.with_suffix(".md.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(new_content)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # fsync is best-effort on some filesystems.
                    pass
            os.replace(str(tmp_path), str(instructions_path))
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise SkillStorageError(
                f"Failed to write {instructions_file}: {exc}",
                details={
                    "skill_id": record.id,
                    "path": str(instructions_path),
                },
            ) from exc

        # 7. Refresh the registry so the next injection sees the new body
        refreshed: Optional[SkillRecord]
        try:
            refreshed = self.registry.refresh_skill(record.id)
        except Exception as exc:  # noqa: BLE001 — never lose the save
            logger.warning(
                "Registry refresh failed after editing skill %r: %s. "
                "Falling back to in-memory update.",
                record.id,
                exc,
            )
            refreshed = None

        if refreshed is None:
            # Fallback: mutate the in-memory record so the caller sees
            # the new body even without a successful reload. The file
            # on disk is already correct.
            record.instructions = body
            refreshed = record

        logger.info(
            "Skill %r instructions updated (%d bytes, previous %d bytes).",
            record.id,
            body_bytes,
            len(refreshed.instructions.encode("utf-8")),
        )
        return refreshed

    def list_broken_skills(self) -> Dict[str, str]:
        """
        Return ``{package_dirname: error_message}`` for packages that
        failed validation during discovery.

        Exposed on ``GET /api/skills?include_broken=true`` so operators
        can diagnose why an uploaded package was rejected.
        """
        return self.registry.list_broken_skills()

    # ── Install / delete ─────────────────────────────────────────────

    def install_skill(
        self,
        zip_bytes: bytes,
        *,
        source_filename: str = "skill.zip",
    ) -> SkillInstallResult:
        """
        Install a skill package from a zip payload.

        Flow:
            1. Clean up orphan temp / trash directories (cheap, once
               per install, protects against a crashed previous run).
            2. Delegate extraction + commit to ``SkillStorage``.
            3. Refresh the registry so the new skill becomes visible
               immediately.

        Any validation, extraction, or storage failure propagates as a
        ``SkillError`` subclass with structured ``details``.
        """
        self.cleanup_orphans()

        result = self.storage.install_skill(
            zip_bytes,
            source_filename=source_filename,
        )

        try:
            self.registry.refresh_skill(result.skill_id)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "Skill %r was installed but registry refresh failed: %s. "
                "The skill is on disk and will be visible on the next "
                "list request.",
                result.skill_id,
                exc,
            )

        logger.info(
            "Skill install succeeded: id=%r version=%s upgrade=%s "
            "warnings=%d",
            result.skill_id,
            result.version,
            result.was_upgrade,
            len(result.warnings),
        )
        return result

    def delete_skill(self, skill_id: str) -> bool:
        """
        Delete a skill package from disk and remove it from the registry.

        Accepts a resolvable ID (canonical, alias, or normalized form).
        Broken packages (present on disk but never successfully
        discovered) can be deleted by passing the package directory
        name; this method falls back to that when the registry has no
        record.

        Returns ``True`` when a package was removed, ``False`` if the
        directory did not exist (rare race with an external delete).

        Raises:
            SkillNotFoundError: when nothing matches the given ID.
            SkillStorageError:  on a filesystem failure during deletion.
        """
        record = self.registry.get_skill(skill_id)
        if record is not None:
            target_id = record.id
        else:
            # Might be a broken package referenced by directory name.
            # Try both the raw input and the normalized form against
            # the disk so operators can always clean up a corrupt
            # package.
            raw = (skill_id or "").strip()
            normalized = normalize_skill_id(raw)
            candidates: List[str] = []
            for cand in (raw, normalized):
                if cand and cand not in candidates:
                    candidates.append(cand)

            target_id = ""
            for cand in candidates:
                if self.storage.skill_exists(cand):
                    target_id = cand
                    break

            if not target_id:
                raise SkillNotFoundError(
                    skill_id,
                    hint="No installed or broken package matches this ID.",
                )

        deleted = self.storage.delete_skill(target_id)

        # Remove from the in-memory registry. Safe to call even when the
        # skill was never discovered.
        self.registry.remove_skill(target_id)

        # Force a full re-discovery so orphaned aliases and state
        # entries for the deleted skill are cleaned up in one pass.
        self.registry.reload()

        if deleted:
            logger.info("Skill %r deleted successfully.", target_id)
        else:
            logger.warning(
                "Skill %r directory was not found during delete (may "
                "have been removed concurrently).",
                target_id,
            )
        return deleted

    # ── Activation ───────────────────────────────────────────────────

    def toggle_skill_status(self, skill_id: str) -> SkillRecord:
        """Toggle a skill between ``active`` and ``disabled``."""
        record = self.registry.toggle_skill_status(skill_id)
        logger.info(
            "Skill %r toggled to status=%r.", record.id, record.status
        )
        return record

    def set_skill_status(self, skill_id: str, status: str) -> SkillRecord:
        """
        Explicitly set a skill's status.

        ``status`` must be ``"active"`` or ``"disabled"``; anything else
        raises ``ValueError`` (mapped to HTTP 400 by the router).
        """
        record = self.registry.set_skill_status(skill_id, status)
        logger.info(
            "Skill %r status set to %r by operator.", record.id, status
        )
        return record

    # ── Lifecycle ────────────────────────────────────────────────────

    def reload(self) -> None:
        """
        Invalidate the in-memory cache so the next read re-discovers
        everything from disk. Called explicitly by tests and by the
        startup lifespan handler after cleanup.
        """
        self.registry.reload()

    def cleanup_orphans(self) -> Dict[str, int]:
        """
        Remove stale staging / trash directories.

        Called automatically before every install and explicitly from
        the FastAPI lifespan handler on startup. Errors are logged and
        swallowed — a cleanup failure must never block an install.

        Returns a dict with counts for logging / metrics::

            {"removed_temp_dirs": int, "removed_trash_dirs": int}
        """
        removed_temp = 0
        removed_trash = 0

        try:
            removed_temp = self.storage.cleanup_orphan_temp_dirs()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cleanup_orphan_temp_dirs failed: %s", exc
            )

        try:
            removed_trash = self.storage.cleanup_orphan_trash_dirs()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cleanup_orphan_trash_dirs failed: %s", exc
            )

        return {
            "removed_temp_dirs": removed_temp,
            "removed_trash_dirs": removed_trash,
        }


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
#
# Follows the same module-level singleton pattern used by
# ``agent_registry`` (in ``omweb/agents/registry.py``) and
# ``tool_registry`` (in ``omweb/tools/registry.py``). Imported by:
#
#   * ``omweb/routers/skills.py``       — HTTP API
#   * ``omweb/main.py``                 — startup cleanup + router wiring
#   * ``omweb/engines/peldrun_engine``  — prompt injection (Phase P5)
#   * ``omweb/agents/registry.py``      — assignment validation (P4)
#
# Tests should construct ``SkillManager(registry=..., storage=...)``
# with isolated fixtures rather than relying on this singleton.

skill_manager = SkillManager()


__all__ = [
    "SkillManager",
    "skill_manager",
]
