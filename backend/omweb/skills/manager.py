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
)
from omweb.skills.loader import load_icon, load_instructions
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
