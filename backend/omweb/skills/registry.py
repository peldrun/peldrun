# backend/omweb/skills/registry.py
"""
Skill registry: on-disk discovery, in-memory cache, and state persistence.

Responsibilities:
    * Discover installed skill packages under
      ``<backend>/storage/store/skills/`` and (optionally) a built-in
      directory at ``omweb/skills/builtins/``.
    * Parse each package via ``omweb.skills.manifest.load_skill_package``
      and cache the result.
    * Persist activation state (active / disabled) in
      ``skills_state.json`` alongside the user-installed packages.
    * Resolve IDs and aliases: exact match, alias map, then
      ``normalize_skill_id`` fallback.
    * Expose the single source of truth consumed by:
        - ``omweb.skills.loader``       (instructions)
        - ``omweb.skills.resolver``     (/ menu)
        - ``omweb.skills.assignments``  (agent binding)
        - ``omweb.skills.manager``      (install / delete lifecycle)
        - ``omweb.routers.skills``      (HTTP API)

Design invariants:
    * Discovery is LAZY. The first read triggers a full scan; every
      subsequent read hits the in-memory cache.
    * A single skill failure (bad manifest, missing SKILL.md, unsafe
      SVG) NEVER aborts discovery. The failing package is recorded in
      ``_broken_skills`` with a diagnostic string and skipped. This is
      the same fail-open behavior used by ``agents/registry.py`` and
      ``tools/registry.py``, and it prevents a corrupted package from
      bricking the entire Skills subsystem.
    * State writes are atomic (``tmp`` file + ``os.replace``). A crash
      mid-write cannot corrupt ``skills_state.json``.
    * All ID comparisons are normalization-aware. ``/code-review`` and
      ``/code_review`` resolve to the same canonical skill.
    * The registry does NOT touch the filesystem outside the two roots
      it owns (``skills_dir`` and ``builtins_dir``). Package deletion
      is the manager's job, not the registry's.

See ``omweb/skills/manifest.py`` for parsing and validation rules.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from omweb.skills.errors import (
    SkillManifestError,
    SkillNotFoundError,
    SkillSecurityError,
    SkillStorageError,
    SkillValidationError,
)
from omweb.skills.manifest import (
    MANIFEST_FILENAME,
    ParsedSkillPackage,
    load_skill_package,
)


logger = logging.getLogger("omweb.skills.registry")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILLS_STATE_FILENAME = "skills_state.json"
STATE_SCHEMA_VERSION = 1
VALID_STATUSES = frozenset({"active", "disabled"})


# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------

def normalize_skill_id(raw: str) -> str:
    """
    Normalize a raw skill ID to PELDRUN's canonical form.

    Rules (applied in order):
        1. Lowercase and strip whitespace.
        2. Replace hyphens and dots with underscores.
        3. Remove any character outside ``[a-z0-9_]``.
        4. Collapse consecutive underscores.
        5. Strip leading / trailing underscores.
        6. Empty result -> ``"skill"``.
        7. Truncate to 64 characters.

    Digits are allowed at the start — the schema accepts
    ``^[a-z0-9][a-z0-9_]{2,63}$``, so ``123-skill`` normalizes to
    ``123_skill`` and keeps its identity. No prefix is injected.

    Examples:
        ``"code-review"``      -> ``"code_review"``
        ``"My.Skill"``         -> ``"my_skill"``
        ``"test planning v2"`` -> ``"test_planning_v2"``
        ``"123-skill"``        -> ``"123_skill"``
        ``"123-skill-v2"``     -> ``"123_skill_v2"``
        ``""``                 -> ``"skill"``
    """
    if not isinstance(raw, str):
        return "skill"

    s = raw.strip().lower()
    s = s.replace("-", "_").replace(".", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")

    if not s:
        return "skill"
    return s[:64]


# ---------------------------------------------------------------------------
# In-memory skill record
# ---------------------------------------------------------------------------

@dataclass
class SkillRecord:
    """
    In-memory representation of one installed skill.

    Instances are mutable only through their ``status`` field, which the
    registry toggles in-place. Every other field is populated once at
    discovery time and remains stable until ``reload()`` or
    ``refresh_skill()``.
    """

    id: str
    version: str
    name: str
    description: str
    manifest: Dict[str, Any]
    instructions: str
    frontmatter: Dict[str, Any]
    icon_svg: Optional[str]
    icon_path: Optional[str]
    package_dir: Path
    status: str = "active"
    is_builtin: bool = False

    # ── Derived booleans ──────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """True when the skill is active."""
        return self.status == "active"

    @property
    def is_deprecated(self) -> bool:
        """True when either ``stability == 'deprecated'`` or the flag is set."""
        if self.manifest.get("deprecated") is True:
            return True
        return self.manifest.get("stability") == "deprecated"

    @property
    def chat_invocable(self) -> bool:
        """Whether the skill can be invoked via ``/<id>`` from chat."""
        if self.is_deprecated:
            return False
        caps = self.manifest.get("capabilities") or {}
        return bool(caps.get("chat_invocable", True))

    @property
    def agent_assignable(self) -> bool:
        """Whether the skill can be assigned to an agent."""
        if self.is_deprecated:
            return False
        caps = self.manifest.get("capabilities") or {}
        return bool(caps.get("agent_assignable", True))

    @property
    def workflow_node(self) -> bool:
        """Reserved: declaration of future Workflow eligibility."""
        caps = self.manifest.get("capabilities") or {}
        return bool(caps.get("workflow_node", False))

    @property
    def user_editable(self) -> bool:
        """
        Whether the operator can edit SKILL.md through the Store UI.

        Rules:
            * Built-in skills are ALWAYS read-only, regardless of the
              manifest's ``user_editable`` flag. They ship with PELDRUN
              and must not diverge from the shipped version.
            * Otherwise, ``user_editable`` defaults to True when the
              field is absent (backward compatibility with pre-2026
              skill packages that predate the field).
        """
        if self.is_builtin:
            return False
        return bool(self.manifest.get("user_editable", True))

    # ── Public serialization ──────────────────────────────────────────

    def to_summary_dict(self) -> Dict[str, Any]:
        """
        Return the public summary used by ``GET /api/skills`` and the
        ``/`` menu. Deliberately excludes ``icon_svg`` (potential 32 KiB
        per skill): the frontend fetches the icon via
        ``GET /api/skills/{id}/icon.svg``.
        """
        author = self.manifest.get("author") or {}
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "display_name": self.manifest.get("display_name") or self.name,
            "description": self.description,
            "short_description": (
                self.manifest.get("short_description") or self.description
            ),
            "status": self.status,
            "enabled": self.enabled,
            "is_builtin": self.is_builtin,
            "user_editable": self.user_editable,
            "is_deprecated": self.is_deprecated,
            "chat_invocable": self.chat_invocable,
            "agent_assignable": self.agent_assignable,
            "workflow_node": self.workflow_node,
            "has_icon_svg": bool(self.icon_svg),
            "has_icon_path": bool(self.icon_path),
            "icon_path": self.icon_path,
            "tags": list(self.manifest.get("tags") or []),
            "author": dict(author) if isinstance(author, dict) else {},
            "publisher_id": self.manifest.get("publisher_id"),
            "trust": self.manifest.get("trust", "unverified"),
            "license": self.manifest.get("license"),
            "homepage": self.manifest.get("homepage"),
            "docs_url": self.manifest.get("docs_url"),
            "release_date": self.manifest.get("release_date"),
            "stability": self.manifest.get("stability", "stable"),
            "capabilities": dict(self.manifest.get("capabilities") or {}),
            "execution": dict(self.manifest.get("execution") or {}),
            "package_dir": str(self.package_dir),
        }

    def to_full_dict(self) -> Dict[str, Any]:
        """
        Return the full record for the details drawer. Includes the raw
        manifest and SKILL.md frontmatter. ``icon_svg`` is still
        excluded — use ``GET /api/skills/{id}/icon.svg`` to fetch it.
        """
        out = self.to_summary_dict()
        out["manifest"] = dict(self.manifest)
        out["frontmatter"] = dict(self.frontmatter)
        return out


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------

class SkillRegistry:
    """Authoritative on-disk registry for installed skills."""

    def __init__(
        self,
        *,
        skills_dir: Optional[Path] = None,
        builtins_dir: Optional[Path] = None,
        state_file: Optional[Path] = None,
    ) -> None:
        """
        Construct a registry instance. Paths are resolved from ``__file__``
        unless explicitly overridden — this makes the registry trivially
        testable with a temporary directory.
        """
        backend_dir = Path(__file__).resolve().parent.parent.parent
        default_storage = backend_dir / "storage"

        self.skills_dir: Path = (
            Path(skills_dir)
            if skills_dir is not None
            else default_storage / "store" / "skills"
        )
        self.builtins_dir: Path = (
            Path(builtins_dir)
            if builtins_dir is not None
            else Path(__file__).resolve().parent / "builtins"
        )
        self.state_file: Path = (
            Path(state_file)
            if state_file is not None
            else self.skills_dir / SKILLS_STATE_FILENAME
        )

        # The user-facing skills directory MUST exist. Builtins is
        # optional (Phase 1 ships none) so we only create it when a
        # caller explicitly provides a path.
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        # Runtime caches
        self._records: Dict[str, SkillRecord] = {}
        self._alias_map: Dict[str, str] = {}
        self._broken_skills: Dict[str, str] = {}
        self._discovered: bool = False

        # Reentrant lock protects discovery and state writes. RLock is
        # required because helper methods chain (e.g. ``toggle_skill_status``
        # acquires the lock then calls ``get_skill`` which may trigger
        # discovery on first use).
        self._lock = threading.RLock()

    # ── Discovery ─────────────────────────────────────────────────────

    def _ensure_discovered(self) -> None:
        """Trigger discovery on first read. Idempotent and thread-safe."""
        if self._discovered:
            return
        self._discover()

    def _discover(self) -> None:
        """Scan both on-disk roots and rebuild the in-memory registry."""
        with self._lock:
            if self._discovered:
                return

            self._records.clear()
            self._alias_map.clear()
            self._broken_skills.clear()

            loaded_states = self._load_states()
            seen_ids: Set[str] = set()

            # Process built-ins first so they are authoritative when a
            # user-installed skill with the same ID tries to override
            # them. User-installed skills are processed second; a
            # collision is logged and the user package is marked broken
            # (not silently overriding the built-in).
            for root, is_builtin in (
                (self.builtins_dir, True),
                (self.skills_dir, False),
            ):
                if not root.is_dir():
                    continue
                for package_dir in sorted(root.iterdir()):
                    if not package_dir.is_dir():
                        continue
                    if package_dir.name.startswith("."):
                        continue
                    if not (package_dir / MANIFEST_FILENAME).is_file():
                        continue
                    self._load_one_package(
                        package_dir,
                        is_builtin=is_builtin,
                        seen_ids=seen_ids,
                        loaded_states=loaded_states,
                    )

            self._discovered = True
            logger.info(
                "Skill registry discovered %d skill(s) "
                "(broken: %d, aliases: %d).",
                len(self._records),
                len(self._broken_skills),
                len(self._alias_map),
            )

            # Prune orphaned state entries for skills that no longer
            # exist on disk. Runs inside the discovery lock.
            self._prune_orphaned_states_locked(loaded_states)

    def _load_one_package(
        self,
        package_dir: Path,
        *,
        is_builtin: bool,
        seen_ids: Set[str],
        loaded_states: Dict[str, str],
    ) -> None:
        """
        Parse a single package directory and register the resulting
        record. Failures are captured in ``_broken_skills`` rather than
        propagated — see the module docstring for the rationale.
        """
        dirname = package_dir.name
        try:
            parsed: ParsedSkillPackage = load_skill_package(package_dir)
        except (SkillManifestError, SkillValidationError, SkillSecurityError) as exc:
            self._broken_skills[dirname] = str(exc)
            logger.warning(
                "Skill package %r rejected during discovery: %s",
                dirname,
                exc,
            )
            return
        except Exception as exc:
            self._broken_skills[dirname] = f"Unexpected error: {exc}"
            logger.exception(
                "Unexpected error while loading skill package %r: %s",
                dirname,
                exc,
            )
            return

        skill_id = parsed.manifest["id"]

        if skill_id in seen_ids:
            self._broken_skills[dirname] = (
                f"Duplicate skill ID {skill_id!r}; another package has "
                "already claimed it."
            )
            logger.warning(
                "Skipping duplicate skill ID %r from package %r.",
                skill_id,
                dirname,
            )
            return

        if skill_id != dirname:
            logger.warning(
                "Skill package %r declares a different ID (%r). The "
                "declared ID is authoritative.",
                dirname,
                skill_id,
            )

        status = loaded_states.get(skill_id, "active")
        if status not in VALID_STATUSES:
            logger.warning(
                "Invalid persisted status %r for skill %r; resetting to "
                "'active'.",
                status,
                skill_id,
            )
            status = "active"

        record = SkillRecord(
            id=skill_id,
            version=str(parsed.manifest.get("version", "0.0.0")),
            name=(
                parsed.manifest.get("display_name")
                or parsed.manifest.get("name")
                or skill_id
            ),
            description=str(parsed.manifest.get("description") or ""),
            manifest=dict(parsed.manifest),
            instructions=parsed.instructions,
            frontmatter=dict(parsed.frontmatter),
            icon_svg=parsed.sanitized_icon_svg,
            icon_path=parsed.manifest.get("icon"),
            package_dir=package_dir,
            status=status,
            is_builtin=is_builtin,
        )

        self._records[skill_id] = record
        seen_ids.add(skill_id)

        # Register normalized directory name as an alias when it differs
        # from the canonical ID. Handles cases where an author names the
        # directory ``code-review`` but writes ``code_review`` in the
        # manifest.
        normalized_dir = normalize_skill_id(dirname)
        if normalized_dir != skill_id and normalized_dir not in self._alias_map:
            self._alias_map[normalized_dir] = skill_id

    def _prune_orphaned_states_locked(
        self,
        loaded_states: Dict[str, str],
    ) -> None:
        """
        Remove persisted state entries for skills that no longer exist
        on disk. Must be called from within the discovery lock.
        """
        current_ids = set(self._records.keys())
        orphans = set(loaded_states.keys()) - current_ids
        if not orphans:
            return
        cleaned = {
            k: v for k, v in loaded_states.items() if k in current_ids
        }
        try:
            self._save_states(cleaned)
            logger.info(
                "Pruned %d orphaned skill state entries: %s",
                len(orphans),
                sorted(orphans),
            )
        except Exception as exc:
            logger.warning(
                "Failed to prune orphaned skill state entries: %s", exc
            )

    # ── State persistence ─────────────────────────────────────────────

    def _load_states(self) -> Dict[str, str]:
        """Read the state map from disk. Returns ``{}`` on any error."""
        if not self.state_file.exists():
            return {}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Failed to read %s: %s. Treating as empty.",
                self.state_file,
                exc,
            )
            return {}

        if not isinstance(raw, dict):
            return {}

        states = raw.get("states")
        if not isinstance(states, dict):
            return {}

        return {
            str(k): str(v)
            for k, v in states.items()
            if isinstance(k, str) and v in VALID_STATUSES
        }

    def _save_states(self, states: Dict[str, str]) -> None:
        """
        Atomically persist the full state map.

        Writes ``tmp`` file first, fsyncs, then ``os.replace`` — a crash
        mid-write cannot corrupt ``skills_state.json``.
        """
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "states": dict(states),
        }

        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_file.with_suffix(".json.tmp")

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # fsync is best-effort; some filesystems disallow it.
                    pass
            os.replace(str(tmp_path), str(self.state_file))
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise SkillStorageError(
                f"Failed to persist {self.state_file.name}: {exc}",
                details={"path": str(self.state_file)},
            ) from exc

    def _persist_all_states(self) -> None:
        """
        Persist the state of every in-memory record.

        This is the only write path. Because the file is REPLACED, any
        stale entries from previous runs (e.g. a skill that was removed
        from disk without cleanup) are dropped automatically.
        """
        states = {rid: rec.status for rid, rec in self._records.items()}
        self._save_states(states)

    # ── Public read API ───────────────────────────────────────────────

    def list_skills(
        self,
        *,
        include_disabled: bool = True,
        include_deprecated: bool = True,
    ) -> List[SkillRecord]:
        """
        Return all discovered skills, sorted by canonical ID.

        Args:
            include_disabled:  Include ``status == "disabled"`` skills.
            include_deprecated: Include skills marked deprecated.
        """
        self._ensure_discovered()
        out: List[SkillRecord] = []
        for record in self._records.values():
            if not include_disabled and not record.enabled:
                continue
            if not include_deprecated and record.is_deprecated:
                continue
            out.append(record)
        out.sort(key=lambda r: r.id)
        return out

    def get_skill(self, skill_id: str) -> Optional[SkillRecord]:
        """
        Resolve a skill by exact ID, alias, or normalized form.

        Returns ``None`` when the ID does not match any installed skill.
        """
        self._ensure_discovered()
        if not isinstance(skill_id, str):
            return None
        raw = skill_id.strip()
        if not raw:
            return None

        # 1. Exact match
        record = self._records.get(raw)
        if record is not None:
            return record

        # 2. Alias map
        canonical = self._alias_map.get(raw)
        if canonical is not None:
            record = self._records.get(canonical)
            if record is not None:
                return record

        # 3. Normalize the raw input and retry
        normalized = normalize_skill_id(raw)
        return self._records.get(normalized)

    def get_skill_or_raise(self, skill_id: str) -> SkillRecord:
        """Resolve a skill or raise :class:`SkillNotFoundError`."""
        record = self.get_skill(skill_id)
        if record is None:
            raise SkillNotFoundError(skill_id)
        return record

    def is_enabled(self, skill_id: str) -> bool:
        """True when the skill exists and its status is ``active``."""
        record = self.get_skill(skill_id)
        return record is not None and record.enabled

    def resolve_alias(self, raw_id: str) -> str:
        """
        Map an alias or raw input to a canonical skill ID. Returns the
        input unchanged when no resolution is possible.
        """
        self._ensure_discovered()
        if not isinstance(raw_id, str):
            return raw_id
        raw = raw_id.strip()
        if not raw:
            return raw
        if raw in self._records:
            return raw
        if raw in self._alias_map:
            return self._alias_map[raw]
        normalized = normalize_skill_id(raw)
        if normalized in self._records:
            return normalized
        return raw


    def list_broken_skills(self) -> Dict[str, str]:
        """Return ``{package_dirname: error_message}`` for broken skills."""
        self._ensure_discovered()
        return dict(self._broken_skills)

    # ── Search ────────────────────────────────────────────────────────
    #
    # Powers two call sites:
    #   * ``GET /api/skills?search=...`` — the store page filter.
    #   * The ``/`` typeahead menu in the chat composer — when the user
    #     types ``/<query>``, the frontend queries this method (via the
    #     API) to rank matching skills.
    #
    # Scoring model (higher = better):
    #   +1000  exact canonical-ID match
    #   +500   ID starts-with
    #   +200   ID contains
    #   +800   exact name/display_name match
    #   +400   name starts-with
    #   +150   name contains
    #   +300   exact tag match
    #   +100   tag contains
    #   +50    description contains
    #
    # Multiple fields can score on the same record — a skill whose ID is
    # ``code_review`` and whose name is ``Code Review`` will score higher
    # than one that only matches in the description. Ties are broken by
    # canonical ID so the ordering is deterministic across calls.
    #
    # Disabled and deprecated skills are excluded by default; the caller
    # opts in via the ``include_*`` flags (used by the store page when
    # the operator wants to see everything).

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

        An empty ``query`` returns the full list (respecting the two
        include-* filters) ordered by canonical ID. This lets the API
        endpoint serve both "list all" and "search" from one code path.

        Args:
            query: Free-form user input from the ``/`` menu or the
                store search box. Matched case-insensitively against
                ID, name, display_name, tags, and description.
            include_disabled: Include skills whose status is ``disabled``.
            include_deprecated: Include skills marked deprecated.
            limit: Maximum number of results. Capped at 100 for safety.

        Returns:
            A list of ``SkillRecord`` ordered by descending relevance.
        """
        self._ensure_discovered()

        safe_limit = max(1, min(int(limit or 30), 100))
        raw_query = (query or "").strip()

        # Empty query -> deterministic alphabetical listing.
        if not raw_query:
            return self.list_skills(
                include_disabled=include_disabled,
                include_deprecated=include_deprecated,
            )[:safe_limit]

        query_lower = raw_query.lower()
        query_normalized = normalize_skill_id(raw_query)

        scored: List[Tuple[int, SkillRecord]] = []
        for record in self._records.values():
            if not include_disabled and not record.enabled:
                continue
            if not include_deprecated and record.is_deprecated:
                continue

            score = self._score_match(
                record, query_lower, query_normalized
            )
            if score > 0:
                scored.append((score, record))

        # Descending score, then ascending ID for a stable tie-break.
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [record for _, record in scored[:safe_limit]]

    @staticmethod
    def _score_match(
        record: SkillRecord,
        query_lower: str,
        query_normalized: str,
    ) -> int:
        """
        Compute a relevance score for one skill against a query.

        Called only from ``search_skills``; kept as a staticmethod so
        it can be unit-tested without instantiating a registry.
        """
        if not query_lower:
            return 0

        skill_id = record.id.lower()
        name_lower = (record.name or "").lower()
        display_name = str(
            record.manifest.get("display_name") or ""
        ).lower()
        short_description = str(
            record.manifest.get("short_description") or ""
        ).lower()
        description_lower = (record.description or "").lower()
        tags_lower = [
            str(tag).lower()
            for tag in (record.manifest.get("tags") or [])
            if isinstance(tag, str)
        ]

        score = 0

        # ── ID match (highest weight) ─────────────────────────────
        if (
            query_normalized == skill_id
            or query_lower == skill_id
        ):
            score += 1000
        elif (
            skill_id.startswith(query_normalized)
            or skill_id.startswith(query_lower)
        ):
            score += 500
        elif (
            query_normalized and query_normalized in skill_id
        ) or query_lower in skill_id:
            score += 200

        # ── Name / display_name match ────────────────────────────
        candidate_names = (name_lower, display_name)
        for candidate in candidate_names:
            if not candidate:
                continue
            if query_lower == candidate:
                score += 800
                break
            if candidate.startswith(query_lower):
                score += 400
                break
            if query_lower in candidate:
                score += 150
                break

        # ── Tag match ────────────────────────────────────────────
        for tag in tags_lower:
            if query_lower == tag:
                score += 300
                break
            if query_lower in tag:
                score += 100
                break

        # ── Description match (lowest weight) ────────────────────
        if short_description and query_lower in short_description:
            score += 75
        if description_lower and query_lower in description_lower:
            score += 50

        return score

    # ── Mutation API ──────────────────────────────────────────────────

    def toggle_skill_status(self, skill_id: str) -> SkillRecord:
        """Toggle a skill between ``active`` and ``disabled``."""
        record = self.get_skill_or_raise(skill_id)
        with self._lock:
            record.status = (
                "disabled" if record.status == "active" else "active"
            )
            self._persist_all_states()
            logger.info(
                "Skill %r status -> %s", record.id, record.status
            )
        return record

    def set_skill_status(self, skill_id: str, status: str) -> SkillRecord:
        """Explicitly set a skill's status. ``status`` must be valid."""
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid skill status {status!r}; expected one of "
                f"{sorted(VALID_STATUSES)}."
            )
        record = self.get_skill_or_raise(skill_id)
        with self._lock:
            record.status = status
            self._persist_all_states()
            logger.info(
                "Skill %r status set to %s", record.id, status
            )
        return record

    def reload(self) -> None:
        """
        Invalidate the in-memory cache so the next read re-discovers
        every skill from disk. Called by ``manager`` after install,
        delete, or upgrade operations.
        """
        with self._lock:
            self._discovered = False
            self._records.clear()
            self._alias_map.clear()
            self._broken_skills.clear()

    def refresh_skill(self, skill_id: str) -> Optional[SkillRecord]:
        """
        Refresh a single skill by triggering a full reload and re-reading.

        Implemented as a full reload on purpose: the current registry is
        small (tens of skills at most), and a full reload removes any
        risk of stale aliases or orphaned state entries. If profiling
        ever shows this matters, replace with a targeted re-parse.
        """
        self.reload()
        return self.get_skill(skill_id)

    def remove_skill(self, skill_id: str) -> bool:
        """
        Remove a skill from the in-memory registry.

        Does NOT delete package files — call ``manager.delete_skill()``
        for that. This method exists so the manager can drop the record
        immediately after deleting the on-disk package without waiting
        for the next discovery pass.
        """
        record = self.get_skill(skill_id)
        if record is None:
            return False
        with self._lock:
            self._records.pop(record.id, None)
            stale_aliases = [
                alias
                for alias, canonical in self._alias_map.items()
                if canonical == record.id
            ]
            for alias in stale_aliases:
                self._alias_map.pop(alias, None)
        return True


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

skill_registry = SkillRegistry()


__all__ = [
    "SKILLS_STATE_FILENAME",
    "VALID_STATUSES",
    "normalize_skill_id",
    "SkillRecord",
    "SkillRegistry",
    "skill_registry",
]
