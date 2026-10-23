# backend/integrations/source_control/git/workspace.py
"""
Local Git workspace manager for the Source Control Integration
subsystem.

A workspace is a PELDRUN-owned directory under the configured
workspace root. It is bound to exactly one repository and one base
commit. It is either a full clone or a Git worktree.

The manager provides the lifecycle operations used by the application
layer:

    - `create_workspace`     : allocate and materialize a workspace.
    - `get_workspace`        : fetch a workspace by ID (or None).
    - `refresh_workspace_status` : recompute dirty / branch / head.
    - `delete_workspace`     : remove a PELDRUN-owned workspace.
    - `list_workspaces`      : enumerate known workspaces.

Design rules:
    - Every filesystem operation is validated through the path policy
      before touching the disk.
    - The manager never invokes `git clean -fdx` or `git reset
      --hard`. User changes are preserved unless the user explicitly
      requests a destructive cleanup via a separate operation.
    - Failed materialization MUST leave no partial directories on
      disk. The manager removes the workspace directory on error.
    - The manager is thread-safe. The in-memory registry is guarded by
      a reentrant lock.
    - The manager does NOT authenticate to the remote by default.
      Callers that need to clone a private repository MUST pass an
      `auth_token`. When a token is supplied, it is embedded in the
      clone URL ONLY for the duration of the clone/fetch, and the
      `.git/config` origin URL is sanitized immediately afterwards.
      The token is NEVER persisted in `.git/config`, NEVER logged,
      and NEVER returned in any diagnostic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
)
from urllib.parse import quote, urlsplit, urlunsplit

from ..domain.enums import ProviderId
from ..domain.models import (
    RepositoryRef,
    WorkspaceRef,
    WorkspaceStatus,
)
from ..errors import (
    ValidationError,
    WorkspaceDirtyError,
    WorkspaceNotFoundError,
)
from .executable import (
    GitExecutable,
    GitExecutionError,
    get_default_git_executable,
)
from .path_policy import (
    WorkspacePathPolicy,
    get_default_path_policy,
    make_workspace_id,
    sanitize_repository_name,
)

__all__ = [
    "GitWorkspaceManager",
    "RepositoryResolver",
    "DEFAULT_MAX_WORKSPACES",
    "get_default_workspace_manager",
    "set_default_workspace_manager",
    "reset_default_workspace_manager",
]


logger = logging.getLogger(__name__)


#: Maximum number of workspaces tracked in the in-memory registry.
#: Prevents unbounded growth when callers forget to delete
#: workspaces.
DEFAULT_MAX_WORKSPACES: int = 100

#: Directory name of the per-workspace marker that allows the
#: manager to recover a workspace after a process restart, even when
#: the global registry file is lost or corrupted. Added to
#: `.git/info/exclude` so it never pollutes `git status`.
WORKSPACE_MARKER_DIR: str = ".peldrun"

#: File name of the per-workspace marker inside `WORKSPACE_MARKER_DIR`.
WORKSPACE_MARKER_NAME: str = "workspace.json"


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


#: Signature of a repository resolver callback.
#:
#: The resolver receives a repository ID (a string) and returns the
#: corresponding `RepositoryRef`, or `None` if the ID is unknown. The
#: manager calls the resolver when `create_workspace` is invoked with
#: a bare `repository_id` and no `repo`.
RepositoryResolver = Callable[[str], Awaitable[Optional[RepositoryRef]]]


# ---------------------------------------------------------------------------
# Internal request shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CreateRequest:
    """
    Normalized workspace creation request.

    Produced by `_normalize_request` after accepting both the test
    signature (`repository_id` + `branch_name`) and the internal
    signature (`repo` + `source_branch`).

    `auth_token` is a short-lived credential used only for the
    clone/fetch step. It MUST NOT be persisted in `.git/config`,
    logged, or returned in any diagnostic.
    """

    repository_id: str
    provider: Optional[ProviderId]
    clone_url: Optional[str]
    base_sha: Optional[str]
    branch_name: Optional[str]
    use_worktree: bool
    depth: Optional[int]
    reuse_existing_branch: bool
    metadata: Dict[str, object] = field(default_factory=dict)
    auth_token: Optional[str] = None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class GitWorkspaceManager:
    """
    Thread-safe manager for local Git workspaces.

    The manager holds an in-memory registry of live workspaces keyed
    by `workspace_id`. It does not persist this registry to disk; the
    durable operation journal is responsible for persistence.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
        repository_resolver: Optional[RepositoryResolver] = None,
        max_workspaces: int = DEFAULT_MAX_WORKSPACES,
        registry_path: Optional[Path] = None,
    ) -> None:
        if max_workspaces <= 0:
            raise ValidationError(
                "max_workspaces must be a positive integer.",
                details={"max_workspaces": max_workspaces},
            )
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()
        self._resolver = repository_resolver
        self._max_workspaces = int(max_workspaces)

        self._workspaces: Dict[str, WorkspaceRef] = {}
        self._lock = threading.RLock()

        # Durable registry so workspaces survive a process restart.
        # The marker file inside each workspace is the authoritative
        # source of truth; the registry is a fast-lookup cache.
        self._registry_path: Path = (
            Path(registry_path)
            if registry_path is not None
            else self._default_registry_path()
        )
        self._load_registry()

    # --- Registry persistence -------------------------------------------

    def _default_registry_path(self) -> Path:
        """
        Return the default registry file location:
        `<backend>/data/source_control/workspaces.json`.

        The module lives at `backend/integrations/source_control/git/workspace.py`,
        so four `parent` hops reach the `backend/` directory.
        """
        try:
            backend_root = (
                Path(__file__).resolve().parent.parent.parent.parent
            )
            return (
                backend_root
                / "data"
                / "source_control"
                / "workspaces.json"
            )
        except Exception:  # noqa: BLE001
            return (
                Path.cwd()
                / ".peldrun"
                / "workspaces"
                / "registry.json"
            )

    def _load_registry(self) -> None:
        """
        Load the persisted registry and silently prune entries whose
        `workspace_path` no longer exists (e.g., after the owning
        chat was deleted). Pruning is persisted so stale entries do
        not accumulate across restarts.
        """
        path = self._registry_path
        if not path.exists():
            return
        try:
            # `utf-8-sig` tolerates a BOM prefix, which Windows
            # PowerShell 5.1's `Set-Content -Encoding UTF8` adds
            # by default. A plain `utf-8` read on a BOM-prefixed
            # file raises `JSONDecodeError` and, prior to this
            # fix, silently emptied the registry on every restart.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to load workspace registry %s: %s",
                path,
                type(exc).__name__,
            )
            return
        if not isinstance(data, dict):
            return
        items = data.get("workspaces", [])
        if not isinstance(items, list):
            return

        loaded = 0
        pruned = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            ws = self._deserialize_workspace(item)
            if ws is None:
                pruned += 1
                continue
            if not Path(ws.workspace_path).exists():
                pruned += 1
                continue
            self._workspaces[ws.workspace_id] = ws
            loaded += 1

        if loaded or pruned:
            logger.info(
                "Workspace registry %s: loaded=%d pruned=%d",
                path,
                loaded,
                pruned,
            )
        if pruned:
            self._save_registry()

    def _save_registry(self) -> None:
        """
        Persist the current registry atomically.

        MUST be called with `self._lock` held (all call sites comply)
        so that `_workspaces` is stable during serialization.
        """
        try:
            self._registry_path.parent.mkdir(
                parents=True, exist_ok=True
            )
            payload = {
                "schema_version": 1,
                "updated_at": _utc_now().isoformat(),
                "workspaces": [
                    self._serialize_workspace(ws)
                    for ws in self._workspaces.values()
                ],
            }
            tmp = self._registry_path.with_suffix(
                self._registry_path.suffix + ".tmp"
            )
            tmp.write_text(
                json.dumps(
                    payload,
                    indent=2,
                    sort_keys=True,
                    default=str,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._registry_path)
        except OSError as exc:
            logger.warning(
                "Failed to save workspace registry %s: %s",
                self._registry_path,
                type(exc).__name__,
            )

    @staticmethod
    def _serialize_workspace(ws: WorkspaceRef) -> Dict[str, Any]:
        """Serialize a `WorkspaceRef` to a JSON-safe dict."""

        def _v(value: Any, default: str = "") -> str:
            if value is None:
                return default
            return (
                value.value if hasattr(value, "value") else str(value)
            )

        return {
            "workspace_id": ws.workspace_id,
            "repository_id": ws.repository_id,
            "provider": _v(ws.provider, "github"),
            "workspace_path": ws.workspace_path,
            "current_branch": ws.current_branch,
            "base_sha": ws.base_sha,
            "head_sha": ws.head_sha,
            "is_worktree": bool(ws.is_worktree),
            "is_clean": bool(ws.is_clean),
            "status": _v(ws.status, "ready"),
            "created_at": (
                ws.created_at.isoformat() if ws.created_at else None
            ),
            "updated_at": (
                ws.updated_at.isoformat() if ws.updated_at else None
            ),
            "metadata": dict(ws.metadata or {}),
        }

    @staticmethod
    def _deserialize_workspace(
        data: Mapping[str, Any],
    ) -> Optional[WorkspaceRef]:
        """Rebuild a `WorkspaceRef` from a JSON dict, or None."""
        try:
            try:
                provider = ProviderId(
                    str(data.get("provider", "github"))
                )
            except ValueError:
                provider = ProviderId.GITHUB
            try:
                ws_status = WorkspaceStatus(
                    str(data.get("status", "ready"))
                )
            except ValueError:
                ws_status = WorkspaceStatus.READY

            created_raw = data.get("created_at")
            updated_raw = data.get("updated_at")
            return WorkspaceRef(
                workspace_id=str(data["workspace_id"]),
                repository_id=str(data.get("repository_id", "")),
                provider=provider,
                workspace_path=str(data["workspace_path"]),
                current_branch=str(
                    data.get("current_branch", "main")
                ),
                base_sha=str(data.get("base_sha", "")),
                head_sha=str(data.get("head_sha", "")),
                is_worktree=bool(data.get("is_worktree", False)),
                is_clean=bool(data.get("is_clean", True)),
                status=ws_status,
                created_at=(
                    datetime.fromisoformat(created_raw)
                    if created_raw
                    else _utc_now()
                ),
                updated_at=(
                    datetime.fromisoformat(updated_raw)
                    if updated_raw
                    else _utc_now()
                ),
                metadata=dict(data.get("metadata") or {}),
            )
        except (KeyError, ValueError, TypeError):
            return None

    async def cleanup_orphans(self) -> Dict[str, Any]:
        """
        Prune registry entries whose workspace is no longer usable.

        A workspace is pruned when any of the following is true:

            1. Its `workspace_path` does not exist on disk
               (canonical orphan: chat or project was deleted).
            2. The path exists but is not a Git repository (no
               `.git` directory) AND no `.peldrun/workspace.json`
               marker is present. This catches "partial deletes"
               where `shutil.rmtree(ignore_errors=True)` was used
               and silently left a directory shell behind.

        Companion of the chat storage engine's
        `sweep_orphaned_storage()`: that method handles the
        physical storage tree, this one handles the source-control
        registry. The two are intentionally decoupled so this
        method never needs to know how the chat storage engine
        performed the deletion.
        """
        removed: List[str] = []
        pruned_reasons: Dict[str, int] = {
            "path_missing": 0,
            "broken_workspace": 0,
        }

        with self._lock:
            for ws_id, ws in list(self._workspaces.items()):
                ws_path = Path(ws.workspace_path)

                # Case 1: physical directory gone.
                if not ws_path.exists():
                    del self._workspaces[ws_id]
                    removed.append(ws_id)
                    pruned_reasons["path_missing"] += 1
                    continue

                # Case 2: directory exists but it is no longer a
                # valid workspace. Detect via the two independent
                # indicators the manager itself writes:
                #   - a `.git` directory (set up by materialization)
                #   - a `.peldrun/workspace.json` marker
                # If neither is present, the directory is a
                # post-delete remnant and must be pruned.
                git_dir = ws_path / ".git"
                has_git = git_dir.exists() and git_dir.is_dir()
                has_marker = self._read_workspace_marker(
                    ws_path
                ) is not None
                if not has_git and not has_marker:
                    del self._workspaces[ws_id]
                    removed.append(ws_id)
                    pruned_reasons["broken_workspace"] += 1

            if removed:
                self._save_registry()
            remaining = len(self._workspaces)

        return {
            "removed_count": len(removed),
            "removed_ids": removed,
            "remaining_count": remaining,
            "registry_path": str(self._registry_path),
            "pruned_reasons": pruned_reasons,
        }

    # --- Workspace marker (.peldrun/workspace.json) ---------------------

    @classmethod
    def _marker_path(cls, workspace_path: Path) -> Path:
        return (
            workspace_path
            / WORKSPACE_MARKER_DIR
            / WORKSPACE_MARKER_NAME
        )

    def _write_workspace_marker(self, ws: WorkspaceRef) -> None:
        """
        Write the per-workspace marker and add its directory to
        `.git/info/exclude` so it never appears in `git status`.

        Best-effort: write failures are logged and swallowed.
        """
        try:
            ws_path = Path(ws.workspace_path)
            marker = self._marker_path(ws_path)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    self._serialize_workspace(ws),
                    indent=2,
                    sort_keys=True,
                    default=str,
                ),
                encoding="utf-8",
            )

            exclude_path = ws_path / ".git" / "info" / "exclude"
            if exclude_path.parent.exists():
                existing = ""
                if exclude_path.exists():
                    try:
                        existing = exclude_path.read_text(
                            encoding="utf-8"
                        )
                    except OSError:
                        existing = ""
                marker_rel = f"/{WORKSPACE_MARKER_DIR}/"
                if marker_rel not in existing:
                    with open(
                        exclude_path, "a", encoding="utf-8"
                    ) as f:
                        if existing and not existing.endswith("\n"):
                            f.write("\n")
                        f.write(
                            f"# PELDRUN workspace marker\n{marker_rel}\n"
                        )
        except OSError as exc:
            logger.warning(
                "Failed to write workspace marker for %s: %s",
                ws.workspace_id,
                type(exc).__name__,
            )

    def _read_workspace_marker(
        self, workspace_path: Path
    ) -> Optional[Dict[str, Any]]:
        """
        Return the parsed marker dict, or None when absent/invalid.

        Reads with `utf-8-sig` so that a marker written by a tool
        that prepends a BOM (notably Windows PowerShell 5.1's
        `Set-Content -Encoding UTF8`, which silently adds one)
        still parses correctly. A plain `utf-8` read on a
        BOM-prefixed file raises `JSONDecodeError`, which used to
        silently disable workspace discovery after a restart.
        """
        marker = self._marker_path(workspace_path)
        if not marker.exists():
            return None
        try:
            data = json.loads(
                marker.read_text(encoding="utf-8-sig")
            )
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _delete_workspace_marker(self, workspace_path: Path) -> None:
        """Remove the marker file so re-scans cannot re-discover it."""
        marker = self._marker_path(workspace_path)
        try:
            if marker.exists():
                marker.unlink()
        except OSError:
            pass

    # --- Internal path helpers ------------------------------------------

    def _storage_roots(self) -> List[Path]:
        """
        Return the storage roots to scan for workspace markers.

        Multiple candidates are checked, in priority order:

            1. `<project_root>/storage` — canonical location for
               chat and project storage. This is where the chat
               storage engine actually writes workspaces, because
               `PROJECT_ROOT` in `storage.py` resolves to the
               directory that contains `backend/`.
            2. `<backend>/storage` — legacy location. Kept for
               backward compatibility with older deployments where
               the storage tree lived inside the backend directory.

        Both are resolved and de-duplicated so a machine with a
        symlinked layout does not scan the same tree twice. Missing
        roots are skipped silently; an empty list means the storage
        tree is unavailable (typical in isolated unit tests).
        """
        roots: List[Path] = []
        seen: set = set()
        try:
            backend_root = (
                Path(__file__).resolve().parent.parent.parent.parent
            )
            candidates = [
                backend_root.parent / "storage",
                backend_root / "storage",
            ]
            for candidate in candidates:
                try:
                    resolved = candidate.resolve()
                except Exception:  # noqa: BLE001
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                if resolved.exists():
                    roots.append(resolved)
        except Exception:  # noqa: BLE001
            pass
        return roots

    def _remove_by_path(self, workspace_path: Path) -> List[str]:
        """
        Drop any registry entries that resolve to the same physical
        directory. Prevents duplicate workspace IDs on the same path
        when a chat is re-executed (which reuses the existing `.git`
        but generates a new `workspace_id`).
        """
        removed: List[str] = []
        try:
            normalized = str(Path(workspace_path).resolve())
        except Exception:  # noqa: BLE001
            normalized = str(workspace_path)

        with self._lock:
            for ws_id, ws in list(self._workspaces.items()):
                try:
                    ws_norm = str(Path(ws.workspace_path).resolve())
                except Exception:  # noqa: BLE001
                    ws_norm = ws.workspace_path
                if ws_norm == normalized:
                    del self._workspaces[ws_id]
                    removed.append(ws_id)
            if removed:
                self._save_registry()
        return removed

    def _build_workspace_ref_from_path(
        self, workspace_id: str, target_dir: Path
    ) -> WorkspaceRef:
        """
        Build a `WorkspaceRef` by inspecting a legacy workspace
        directory laid out under `_policy.root` (no marker file).
        """
        branch_name = "main"
        head_sha = ""
        head_file = target_dir / ".git" / "HEAD"
        if head_file.exists():
            try:
                head_content = head_file.read_text(
                    encoding="utf-8"
                ).strip()
                if head_content.startswith("ref: refs/heads/"):
                    branch_name = head_content[
                        len("ref: refs/heads/") :
                    ]
                    ref_file = (
                        target_dir
                        / ".git"
                        / "refs"
                        / "heads"
                        / branch_name
                    )
                    if ref_file.exists():
                        head_sha = ref_file.read_text(
                            encoding="utf-8"
                        ).strip()
                elif len(head_content) == 40:
                    head_sha = head_content
            except OSError:
                pass

        repository_id = (
            target_dir.name.split("__")[0]
            if "__" in target_dir.name
            else "unknown"
        )

        return WorkspaceRef(
            workspace_id=workspace_id,
            repository_id=repository_id,
            provider=ProviderId.GITHUB,
            workspace_path=str(target_dir),
            current_branch=branch_name,
            base_sha=head_sha,
            head_sha=head_sha,
            is_worktree=False,
            is_clean=True,
            status=WorkspaceStatus.READY,
            created_at=_utc_now(),
            updated_at=_utc_now(),
            metadata={"recovered_from_disk": True},
        )

    # --- Properties -----------------------------------------------------

    @property
    def workspace_root(self) -> Path:
        """Return the absolute workspace root directory."""
        return self._policy.root

    @property
    def policy(self) -> WorkspacePathPolicy:
        """Return the path policy used by this manager."""
        return self._policy

    # --- Registration of a resolver ------------------------------------

    def set_repository_resolver(
        self, resolver: Optional[RepositoryResolver]
    ) -> None:
        """
        Set or clear the repository resolver callback.

        The resolver is invoked when `create_workspace` receives a
        bare `repository_id` and needs a full `RepositoryRef` to
        determine the clone URL and provider.
        """
        if resolver is not None and not callable(resolver):
            raise ValidationError("resolver must be callable or None.")
        with self._lock:
            self._resolver = resolver

    # --- Lifecycle: create ---------------------------------------------

    async def create_workspace(
        self,
        repository_id: Optional[str] = None,
        *,
        repo: Optional[RepositoryRef] = None,
        provider: Optional[ProviderId] = None,
        base_sha: Optional[str] = None,
        branch_name: Optional[str] = None,
        source_branch: Optional[str] = None,
        clone_url: Optional[str] = None,
        use_worktree: bool = True,
        depth: Optional[int] = None,
        reuse_existing_branch: bool = False,
        metadata: Optional[Dict[str, object]] = None,
        auth_token: Optional[str] = None,
        target_path: Optional[Path] = None, # <-- ADD THIS
    ) -> WorkspaceRef:
        """
        Create a new workspace.

        Two calling conventions are supported for backward
        compatibility:

            1. Test / simple signature::

                   await create_workspace(
                       repository_id="repo_101",
                       base_sha="a1b2...",
                       branch_name="feature/task-alpha",
                   )

            2. Internal signature::

                   await create_workspace(
                       repo=RepositoryRef(...),
                       source_branch="main",
                   )

        Exactly one of `repository_id` or `repo` MUST be provided.
        `branch_name` and `source_branch` are synonyms; when both are
        provided, they MUST be equal.

        Args:
            auth_token: Optional short-lived credential used only for
                the clone/fetch step. It is embedded in the clone URL
                for the duration of the network operation and removed
                from `.git/config` immediately afterwards. It is
                NEVER persisted, logged, or returned.

        Returns:
            The `WorkspaceRef` for the newly created workspace.

        Raises:
            ValidationError: If the inputs are inconsistent, or the
                registry is at capacity.
            WorkspaceNotFoundError: If the repository cannot be
                resolved.
            PathTraversalError: If the workspace path escapes the
                root.
        """
        request = await self._normalize_request(
            repository_id=repository_id,
            repo=repo,
            provider=provider,
            base_sha=base_sha,
            branch_name=branch_name,
            source_branch=source_branch,
            clone_url=clone_url,
            use_worktree=use_worktree,
            depth=depth,
            reuse_existing_branch=reuse_existing_branch,
            metadata=metadata,
            auth_token=auth_token,
        )

        with self._lock:
            if len(self._workspaces) >= self._max_workspaces:
                raise ValidationError(
                    "Workspace registry is at capacity.",
                    details={"max_workspaces": self._max_workspaces},
                )

        workspace_id = make_workspace_id(
            provider_hint=(
                request.provider.value if request.provider else None
            ),
            repository_hint=request.repository_id,
        )

        if target_path:
            # Use the provided target path (e.g., chat session files directory)
            workspace_path = target_path
            workspace_path.mkdir(parents=True, exist_ok=True)
        else:
            workspace_path = self._derive_workspace_path(
                workspace_id=workspace_id,
                repository_id=request.repository_id,
            )
            # Ensure the workspace root exists.
            self._policy.root.mkdir(parents=True, exist_ok=True)

        workspace = WorkspaceRef(
            workspace_id=workspace_id,
            repository_id=request.repository_id,
            provider=request.provider or ProviderId.GITHUB,
            workspace_path=str(workspace_path),
            current_branch=request.branch_name or "main",
            base_sha=request.base_sha or "",
            head_sha="",
            is_worktree=request.use_worktree,
            is_clean=True,
            status=WorkspaceStatus.CREATING,
            created_at=_utc_now(),
            updated_at=_utc_now(),
            metadata=dict(request.metadata),
        )

        try:
            materialized = await self._materialize(
                workspace=workspace, request=request
            )
        except Exception:
            # Best-effort cleanup of a partially materialized
            # workspace. Never propagate the cleanup failure; the
            # original exception is more informative.
            await self._force_remove_directory(workspace_path)
            raise

        # Evict any stale entry that points to the same physical
        # directory so that re-running a task does not accumulate
        # duplicate workspace IDs on the same path.
        self._remove_by_path(workspace_path)

        # The capacity check MUST run again here: the early check
        # above fast-fails when the registry is already full, but
        # between that check and this insert the long-running
        # `_materialize` call may have given other coroutines the
        # chance to fill the remaining slots. Holding the lock
        # across the whole materialization would serialize every
        # workspace creation, and releasing it after materialize
        # without a re-check would allow the registry to exceed
        # `_max_workspaces` under concurrency.
        with self._lock:
            at_capacity = (
                len(self._workspaces) >= self._max_workspaces
            )
            if not at_capacity:
                self._workspaces[workspace_id] = materialized
                self._save_registry()

        if at_capacity:
            # Release the lock before cleanup so that a slow
            # `rmtree` never blocks other registry operations.
            await self._force_remove_directory(workspace_path)
            raise ValidationError(
                "Workspace registry is at capacity.",
                details={"max_workspaces": self._max_workspaces},
            )

        # Persist the on-disk marker AFTER registration so that the
        # marker content is the canonical, latest state.
        self._write_workspace_marker(materialized)

        return materialized

    # --- Lifecycle: read -----------------------------------------------

    def get_workspace(self, workspace_id: str) -> Optional[WorkspaceRef]:
        """
        Return the workspace with the given ID, or `None` if unknown.

        Resolution after a restart:
            1. Cached entry whose directory still exists -> return.
            2. Cached entry whose directory was deleted (chat purged)
               -> drop it and continue.
            3. Not cached -> scan `.peldrun/workspace.json` markers
               under `storage/chats` and `storage/projects`.
        """
        if not workspace_id or not isinstance(workspace_id, str):
            return None
        with self._lock:
            ws = self._workspaces.get(workspace_id)
            if ws is not None:
                if Path(ws.workspace_path).exists():
                    return ws
                # Stale entry: physical directory is gone.
                del self._workspaces[workspace_id]
                self._save_registry()

            # Attempt discovery from disk.
            discovered = self._discover_workspace_on_disk(
                workspace_id
            )
            if discovered is not None:
                self._workspaces[workspace_id] = discovered
                self._save_registry()
                self._write_workspace_marker(discovered)
                return discovered
            return None

    def register_workspace(self, workspace: WorkspaceRef) -> None:
        """Register or restore an existing workspace reference."""
        if not workspace or not isinstance(workspace, WorkspaceRef):
            return
        with self._lock:
            self._workspaces[workspace.workspace_id] = workspace
            self._save_registry()
        self._write_workspace_marker(workspace)

    def update_base_sha(self, workspace_id: str, new_sha: str) -> None:
        """
        Update the workspace's base_sha and head_sha after a successful push.

        This ensures the NEXT diff compares against the just-pushed
        commit, rather than the original base_sha from when the
        workspace was created. Without this, a second push would
        re-include the commits from the first push in its diff.
        """
        with self._lock:
            ws = self._workspaces.get(workspace_id)
            if ws is not None:
                updated = replace(
                    ws,
                    base_sha=new_sha,
                    head_sha=new_sha,
                    is_clean=True,
                    updated_at=_utc_now(),
                )
                self._workspaces[workspace_id] = updated
                self._save_registry()
                self._write_workspace_marker(updated)

    def _discover_workspace_on_disk(
        self, workspace_id: str
    ) -> Optional[WorkspaceRef]:
        """
        Locate an existing workspace by scanning disk.

        Search order:
            1. `_policy.root` — legacy name-based discovery for
               workspaces created without a custom `target_path`.
            2. `storage/chats/<chat>/files/.peldrun/workspace.json`.
            3. `storage/projects/<proj>/chats/<chat>/files/.peldrun/workspace.json`.
        """

        # 1) Legacy name-based scan under the policy root.
        if self._policy.root.exists():
            suffix = f"__{workspace_id}"
            try:
                for child in self._policy.root.iterdir():
                    if not child.is_dir():
                        continue
                    matches_name = (
                        child.name == workspace_id
                        or child.name.endswith(suffix)
                    )
                    if (
                        matches_name
                        and (child / ".git").exists()
                    ):
                        return self._build_workspace_ref_from_path(
                            workspace_id, child
                        )
            except OSError:
                pass

        # 2) + 3) Marker-driven scan under the storage tree.
        for root in self._storage_roots():
            chats_dir = root / "chats"
            if chats_dir.exists():
                try:
                    for chat_dir in chats_dir.iterdir():
                        if not chat_dir.is_dir():
                            continue
                        files_dir = chat_dir / "files"
                        if not files_dir.exists():
                            continue
                        marker = self._read_workspace_marker(
                            files_dir
                        )
                        if (
                            marker
                            and marker.get("workspace_id")
                            == workspace_id
                        ):
                            recovered = self._deserialize_workspace(
                                marker
                            )
                            if recovered is not None:
                                # Trust the actual path over the
                                # marker content so a tampered
                                # marker cannot redirect workspace
                                # operations elsewhere.
                                return replace(
                                    recovered,
                                    workspace_path=str(
                                        files_dir.resolve()
                                    ),
                                )
                except OSError:
                    pass

            projects_dir = root / "projects"
            if projects_dir.exists():
                try:
                    for proj_dir in projects_dir.iterdir():
                        if not proj_dir.is_dir():
                            continue
                        p_chats = proj_dir / "chats"
                        if not p_chats.exists():
                            continue
                        for chat_dir in p_chats.iterdir():
                            if not chat_dir.is_dir():
                                continue
                            files_dir = chat_dir / "files"
                            if not files_dir.exists():
                                continue
                            marker = (
                                self._read_workspace_marker(files_dir)
                            )
                            if (
                                marker
                                and marker.get("workspace_id")
                                == workspace_id
                            ):
                                recovered = (
                                    self._deserialize_workspace(marker)
                                )
                                if recovered is not None:
                                    return replace(
                                        recovered,
                                        workspace_path=str(
                                            files_dir.resolve()
                                        ),
                                    )
                except OSError:
                    pass

        return None

    def require_workspace(self, workspace_id: str) -> WorkspaceRef:
        """
        Return the workspace with the given ID, or raise
        `WorkspaceNotFoundError`.
        """
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(
                f"Workspace '{workspace_id}' is not registered.",
                details={"workspace_id": workspace_id},
            )
        return workspace

    def list_workspaces(self) -> Sequence[WorkspaceRef]:
        """
        Return a snapshot of all registered workspaces, oldest first.
        """
        with self._lock:
            items = list(self._workspaces.values())
        items.sort(key=lambda ws: ws.created_at or _utc_now())
        return tuple(items)

    # --- Lifecycle: refresh --------------------------------------------

    async def refresh_workspace_status(
        self, workspace_id: str
    ) -> WorkspaceRef:
        """
        Recompute the status of a workspace and return the updated
        reference.

        The following fields are recomputed:
            - `is_clean` (via `git status --porcelain`)
            - `current_branch`
            - `head_sha`
            - `status` (READY, DIRTY, or CONFLICTED)
            - `updated_at`

        Raises:
            WorkspaceNotFoundError: If the ID is unknown.
        """
        workspace = self.require_workspace(workspace_id)
        path = Path(workspace.workspace_path)

        if not path.exists():
            updated = workspace.with_status(
                WorkspaceStatus.FAILED,
                metadata={**dict(workspace.metadata), "missing_on_disk": True},
            )
            with self._lock:
                self._workspaces[workspace_id] = updated
            return updated

        dirty = await self._detect_dirty(path)
        conflicted = await self._detect_conflicts(path)
        branch = await self._current_branch(path, fallback=workspace.current_branch)
        head_sha = await self._head_sha(path, fallback=workspace.head_sha)

        if conflicted:
            status = WorkspaceStatus.CONFLICTED
        elif dirty:
            status = WorkspaceStatus.DIRTY
        else:
            status = WorkspaceStatus.READY

        updated = replace(
            workspace,
            is_clean=not dirty and not conflicted,
            current_branch=branch,
            head_sha=head_sha,
            status=status,
            updated_at=_utc_now(),
        )
        with self._lock:
            self._workspaces[workspace_id] = updated
        return updated

    async def is_dirty(self, workspace_id: str) -> bool:
        """
        Convenience wrapper: return True if the workspace has
        uncommitted changes.
        """
        workspace = await self.refresh_workspace_status(workspace_id)
        return not workspace.is_clean

    # --- Lifecycle: delete ---------------------------------------------

    async def delete_workspace(
        self,
        workspace_id: str,
        *,
        force_delete_files: bool = False,
    ) -> None:
        """
        Remove a workspace from PELDRUN tracking.

        By default this is a non-destructive unregister: the entry
        is removed from the in-memory and on-disk registries, the
        `.peldrun/workspace.json` marker is deleted (so future
        scans cannot re-discover it), and the physical directory is
        left untouched.

        Pass `force_delete_files=True` to also remove the directory
        tree. This is only honored when the workspace path resolves
        inside `_policy.root`; chat-scoped workspaces under
        `storage/chats/<chat>/files` are intentionally never
        deleted here because their lifecycle is owned by the chat
        storage engine.

        Raises:
            WorkspaceNotFoundError: If the ID is unknown.
        """
        workspace = self.require_workspace(workspace_id)

        if force_delete_files:
            try:
                resolved = self._policy.resolve_within_root(
                    workspace.workspace_path, must_exist=False
                )
                await self._force_remove_directory(resolved)
            except Exception as exc:  # noqa: BLE001
                # Path is outside the policy root — expected for
                # chat-scoped workspaces. Log and continue with
                # the unregister path only.
                logger.info(
                    "delete_workspace: refusing to delete files for "
                    "%s outside the workspace root (%s). "
                    "Unregistering only.",
                    workspace_id,
                    type(exc).__name__,
                )

        # Remove the marker first so a subsequent disk scan cannot
        # re-register the workspace.
        self._delete_workspace_marker(
            Path(workspace.workspace_path)
        )

        with self._lock:
            self._workspaces.pop(workspace_id, None)
            self._save_registry()

    # Alias retained for backward compatibility with older callers.
    async def cleanup_workspace(self, workspace_id: str) -> None:
        """Deprecated alias for `delete_workspace`."""
        await self.delete_workspace(workspace_id)

    # --- Registry maintenance ------------------------------------------

    def clear(self) -> None:
        """
        Forget every registered workspace without touching disk.

        Intended for tests. Production code SHOULD delete workspaces
        explicitly via `delete_workspace`.
        """
        with self._lock:
            self._workspaces.clear()
            self._save_registry()

    def __len__(self) -> int:
        with self._lock:
            return len(self._workspaces)

    def __contains__(self, workspace_id: object) -> bool:
        if not isinstance(workspace_id, str):
            return False
        with self._lock:
            return workspace_id in self._workspaces

    # --- Internal: request normalization -------------------------------

    async def _normalize_request(
        self,
        *,
        repository_id: Optional[str],
        repo: Optional[RepositoryRef],
        provider: Optional[ProviderId],
        base_sha: Optional[str],
        branch_name: Optional[str],
        source_branch: Optional[str],
        clone_url: Optional[str],
        use_worktree: bool,
        depth: Optional[int],
        reuse_existing_branch: bool,
        metadata: Optional[Dict[str, object]],
        auth_token: Optional[str] = None,
    ) -> _CreateRequest:
        """
        Normalize a caller-supplied request into a `_CreateRequest`.

        Accepts both the test signature and the internal signature.
        Raises `ValidationError` for inconsistent inputs.

        `auth_token`, when provided, is carried into the resulting
        `_CreateRequest` and used only for the clone/fetch step.
        """
        if repo is None and not repository_id:
            raise ValidationError(
                "create_workspace requires either `repository_id` or "
                "`repo`."
            )
        if repo is not None and repository_id:
            if repo.repository_id != repository_id:
                raise ValidationError(
                    "`repository_id` and `repo.repository_id` disagree.",
                    details={
                        "repository_id": repository_id,
                        "repo.repository_id": repo.repository_id,
                    },
                )

        # `branch_name` and `source_branch` are synonyms.
        if branch_name and source_branch and branch_name != source_branch:
            raise ValidationError(
                "`branch_name` and `source_branch` must match when both "
                "are provided.",
                details={
                    "branch_name": branch_name,
                    "source_branch": source_branch,
                },
            )
        resolved_branch = branch_name or source_branch

        # If we were given a repository ID but no repo, ask the
        # resolver (if any) to fill in the details.
        if repo is None and self._resolver is not None:
            try:
                repo = await self._resolver(repository_id or "")
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Repository resolver failed for %r: %s",
                    repository_id,
                    type(exc).__name__,
                )
                repo = None

        # Derive the fields.
        resolved_repository_id = (
            repo.repository_id if repo else (repository_id or "")
        )
        resolved_provider = (
            repo.provider if repo else provider
        )
        resolved_clone_url = clone_url or (repo.clone_url if repo else None)

        if not resolved_repository_id:
            raise ValidationError(
                "A non-empty `repository_id` is required."
            )

        if depth is not None and depth <= 0:
            raise ValidationError(
                "`depth` must be a positive integer.",
                details={"depth": depth},
            )

        return _CreateRequest(
            repository_id=resolved_repository_id,
            provider=resolved_provider,
            clone_url=resolved_clone_url,
            base_sha=base_sha,
            branch_name=resolved_branch,
            use_worktree=bool(use_worktree),
            depth=depth,
            reuse_existing_branch=bool(reuse_existing_branch),
            metadata=dict(metadata or {}),
            auth_token=auth_token,
        )

    # --- Internal: workspace path --------------------------------------

    def _derive_workspace_path(
        self, *, workspace_id: str, repository_id: str
    ) -> Path:
        """
        Compute the absolute path of a new workspace.

        The path is `<root>/<safe-repo-name>__<workspace-id>`. The
        safe repo name is derived from `repository_id` using
        `sanitize_repository_name` and truncated as needed.

        The result is validated against the path policy so that any
        attempt to escape the root is rejected before the directory
        is created.
        """
        safe_name = sanitize_repository_name(
            namespace=None, name=repository_id, max_length=60
        )
        relative = f"{safe_name}__{workspace_id}"
        candidate = self._policy.root / relative
        # Validate before creation. `must_exist=False` allows paths
        # that do not exist yet.
        return self._policy.resolve_within_root(candidate, must_exist=False)

    # --- Internal: materialization -------------------------------------

    async def _materialize(
        self,
        *,
        workspace: WorkspaceRef,
        request: _CreateRequest,
    ) -> WorkspaceRef:
        """
        Create the on-disk workspace.

        Two modes are supported:

            - Clone mode (`use_worktree=False`): the manager performs
              `git clone <url> <target>`. The workspace is fully
              independent of any other on-disk repository.

            - Worktree mode (`use_worktree=True`): the manager
              performs `git init` + `git remote add origin <url>` in
              the target directory. This is a simplified form of
              worktree: it produces a self-contained repository
              without depending on a bare cache. Future revisions
              MAY share a bare cache across workspaces.

        When no `clone_url` is available (e.g. in tests), the manager
        initializes an empty repository, creates a single empty
        commit, and renames the branch to the requested name. This
        produces a real Git workspace that tests can inspect.
        """
        target = Path(workspace.workspace_path)
        target.mkdir(parents=True, exist_ok=True)

        branch = request.branch_name or workspace.current_branch

        if request.clone_url:
            git_dir = target / ".git"
            if git_dir.exists():
                # A Git repository already exists in the target
                # directory. Cloning into it would fail ("destination
                # path already exists"), and re-initializing would
                # fail on the "origin" remote already being present.
                # Reuse the existing repository as-is; callers that
                # need to refresh the remote state should do so
                # through a dedicated fetch/reset operation.
                pass
            elif any(target.iterdir()):
                # Non-empty directory without `.git`: `git clone`
                # refuses to operate on such a directory. Use the
                # init+fetch path instead, which preserves the
                # existing files and still wires up the remote.
                await self._clone(
                    clone_url=request.clone_url,
                    target=target,
                    depth=request.depth,
                    use_worktree=True,
                    auth_token=request.auth_token,
                )
            else:
                # Empty directory: both clone and init+fetch work.
                # Honor the caller's preference.
                await self._clone(
                    clone_url=request.clone_url,
                    target=target,
                    depth=request.depth,
                    use_worktree=request.use_worktree,
                    auth_token=request.auth_token,
                )
        else:
            await self._init_empty_repo(target=target)

        # Determine the branch to check out.
        if branch and not request.reuse_existing_branch:
            await self._ensure_branch(target=target, branch=branch)

        # Optionally check out the requested base SHA.
        if request.base_sha:
            await self._checkout_sha(target=target, sha=request.base_sha)

        branch_now = await self._current_branch(
            target, fallback=branch or "main"
        )
        head_sha = await self._head_sha(target, fallback="")

        # A born HEAD is required for every downstream operation:
        # `git diff` needs it, the approval flow needs it, and the
        # push pipeline refuses to push an unborn branch. When
        # neither the remote nor the local repository produced a
        # commit (for example a fresh repository created with `init`
        # on a machine with no network, or `init`+`fetch` against a
        # remote with no reachable objects), bootstrap an empty
        # initial commit so that HEAD exists. This mirrors the same
        # convention `_init_empty_repo` already uses; it is applied
        # here only when the earlier steps could not produce a born
        # HEAD.
        #
        # The previous implementation stored a synthetic `"0"*40`
        # placeholder as `base_sha` when HEAD was unborn. That value
        # is not a valid commit and caused `git diff <sha>` to fail
        # with "fatal: bad revision"; the agent received a cryptic
        # error message and could not recover. Bootstrapping an
        # empty commit removes the ambiguity: either HEAD is born
        # (and a real SHA-1 is stored) or the operation fails
        # loudly.
        if not head_sha:
            await self._git.run(
                [
                    "-c", "user.name=PELDRUN",
                    "-c", "user.email=peldrun@localhost",
                    "commit", "--allow-empty",
                    "-m", "Initial commit",
                ],
                cwd=target,
                check_returncode=False,
            )
            head_sha = await self._head_sha(target, fallback="")

        dirty = await self._detect_dirty(target)

        # Prefer an explicitly requested base SHA; otherwise anchor
        # the workspace to the born HEAD. When neither is available
        # the workspace is in an unrecoverable state and we surface
        # that explicitly rather than fabricating a SHA.
        if request.base_sha:
            effective_base_sha = request.base_sha
        elif head_sha:
            effective_base_sha = head_sha
        else:
            raise ValidationError(
                "Workspace could not be initialized: the repository "
                "has no commits and no reachable remote HEAD.",
                details={
                    "workspace_id": workspace.workspace_id,
                    "workspace_path": str(target),
                },
            )

        return replace(
            workspace,
            current_branch=branch_now,
            head_sha=head_sha,
            base_sha=effective_base_sha,
            is_clean=not dirty,
            status=(
                WorkspaceStatus.READY
                if not dirty
                else WorkspaceStatus.DIRTY
            ),
            updated_at=_utc_now(),
        )

    async def _clone(
        self,
        *,
        clone_url: str,
        target: Path,
        depth: Optional[int],
        use_worktree: bool,
        auth_token: Optional[str] = None,
    ) -> None:
        """
        Materialize `target` from `clone_url`.

        When `auth_token` is provided, it is embedded in the URL used
        for the clone/fetch operation and removed from `.git/config`
        immediately afterwards. When not provided, `clone_url` MUST
        be credential-free.

        Security contract:
            - The authenticated URL is used ONLY for the network
              operation (clone or fetch).
            - After the operation completes (success or failure),
              `git remote set-url origin <clean_url>` replaces the
              authenticated URL with the credential-free form in
              `.git/config`.
            - The cleanup runs in a `finally` block so a failed
              clone still sanitizes any partial `.git/config`.
            - If the cleanup itself fails, a warning is logged and
              the caller is advised to rotate the token.
        """
        # The clean URL is what we will persist. The working URL is
        # what we actually use for the network operation.
        clean_url = _strip_url_credentials(clone_url)
        if auth_token:
            working_url = _inject_token_in_url(clean_url, auth_token)
        else:
            working_url = clean_url

        try:
            if use_worktree:
                # Worktree-style materialization: initialize a local
                # repository, add the remote, then fetch.
                await self._git.run(["init", "-q", str(target)])
                await self._git.run(
                    ["remote", "add", "origin", working_url], cwd=target
                )
                fetch_args: List[str] = ["fetch", "--prune", "origin"]
                if depth:
                    fetch_args.extend(["--depth", str(depth)])
                await self._git.run(
                    fetch_args, cwd=target, timeout_seconds=300
                )
                # Ensure the repository has a HEAD. If it does not,
                # this step is a no-op; the caller will set up the
                # branch.
                await self._git.run(
                    ["remote", "set-head", "origin", "-a"],
                    cwd=target,
                    check_returncode=False,
                )
            else:
                clone_args: List[str] = ["clone", "--origin", "origin"]
                if depth:
                    clone_args.extend(["--depth", str(depth)])
                clone_args.extend([working_url, str(target)])
                await self._git.run(clone_args, timeout_seconds=600)
        finally:
            # Sanitize `.git/config` regardless of whether the clone
            # succeeded. A partially initialized repository may still
            # contain the authenticated URL.
            await self._sanitize_remote_url(
                target=target,
                clean_url=clean_url,
                auth_token=auth_token,
            )

    async def _sanitize_remote_url(
        self,
        *,
        target: Path,
        clean_url: str,
        auth_token: Optional[str],
    ) -> None:
        """
        Replace the origin remote URL with the credential-free form.

        Runs `git remote set-url origin <clean_url>` when a token was
        used. If the operation fails, logs a warning that names the
        target; the caller is expected to continue (the workspace is
        functional) but should consider the token compromised and
        rotate it.

        This method is idempotent and safe to call when no token was
        used or when the target directory does not exist yet.
        """
        if not auth_token or not clean_url:
            return
        if not target.exists():
            return
        try:
            await self._git.run(
                ["remote", "set-url", "origin", clean_url],
                cwd=target,
                check_returncode=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to sanitize origin remote URL in %s: %s. "
                "The clone credential may still be present in "
                ".git/config and SHOULD be rotated.",
                target,
                type(exc).__name__,
            )

    async def _init_empty_repo(self, *, target: Path) -> None:
        """
        Initialize an empty repository with a single empty commit.

        Used when no `clone_url` is provided. Tests rely on this to
        exercise the workspace lifecycle without network access.
        """
        await self._git.run(["init", "-q", str(target)])
        await self._git.run(
            [
                "-c",
                "user.name=PELDRUN",
                "-c",
                "user.email=peldrun@localhost",
                "commit",
                "--allow-empty",
                "-m",
                "Initial empty commit",
            ],
            cwd=target,
        )

    async def _ensure_branch(self, *, target: Path, branch: str) -> None:
        """
        Ensure the workspace is on `branch`, creating it if needed.

        A new branch is created from the best available start point:

            1. ``origin/<branch>`` when the remote tracks that branch
               (the standard case after an `init`+`fetch` cycle).
            2. ``origin/HEAD`` when the remote's default branch is
               known but the requested branch does not exist yet.
            3. The current ``HEAD`` when it is already born (for
               example after a plain `git clone`).

        Creating the branch from a bare ``git checkout -b <name>`` on
        a repository whose HEAD is still unborn (no commits, no
        remote HEAD) produces an orphan branch with no history. In
        that state ``git diff`` fails with "fatal: ambiguous argument
        'HEAD'", and the approval flow surfaces an error that the
        agent cannot interpret. Resolving an explicit start point
        below prevents that state whenever the remote is reachable.

        IMPORTANT: this method uses ``git checkout -b`` (lowercase),
        NOT ``git checkout -B`` (uppercase). The uppercase form
        force-RESETS an existing branch to the current HEAD, which
        would discard any commits already on that branch — a direct
        violation of the manager's "never discard user changes"
        design rule. The lowercase form refuses to overwrite an
        existing branch, so a stale or reused workspace whose branch
        already exists surfaces a clear error instead of silently
        losing commits.
        """
        # Try to check the branch out first.
        result = await self._git.run(
            ["checkout", branch],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode == 0:
            return

        # Branch does not exist locally. Resolve a start point.
        start_point: Optional[str] = None

        # 1. origin/<branch> (the exact remote branch, if present).
        remote_branch_ref = await self._git.run(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=target,
            check_returncode=False,
        )
        if (
            remote_branch_ref.returncode == 0
            and remote_branch_ref.stdout.strip()
        ):
            start_point = f"origin/{branch}"
        else:
            # 2. origin/HEAD (the remote's default branch).
            remote_head_ref = await self._git.run(
                ["rev-parse", "--verify", "--quiet", "refs/remotes/origin/HEAD"],
                cwd=target,
                check_returncode=False,
            )
            if (
                remote_head_ref.returncode == 0
                and remote_head_ref.stdout.strip()
            ):
                start_point = "origin/HEAD"
            else:
                # 3. Current HEAD, when it is already born.
                local_head_ref = await self._git.run(
                    ["rev-parse", "--verify", "--quiet", "HEAD"],
                    cwd=target,
                    check_returncode=False,
                )
                if (
                    local_head_ref.returncode == 0
                    and local_head_ref.stdout.strip()
                ):
                    start_point = "HEAD"
                # else: no valid start point; the caller
                # (`_materialize`) is responsible for bootstrapping
                # an initial commit so that HEAD becomes born.

        if start_point is not None:
            await self._git.run(
                ["checkout", "-b", branch, start_point],
                cwd=target,
            )
        else:
            await self._git.run(
                ["checkout", "-b", branch],
                cwd=target,
            )

    async def _checkout_sha(self, *, target: Path, sha: str) -> None:
        """
        Attempt to check out a specific SHA.

        Failures are logged and ignored: the workspace remains on its
        current branch. The caller is responsible for verifying the
        HEAD after the operation.
        """
        result = await self._git.run(
            ["checkout", sha],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            logger.debug(
                "Could not check out base SHA %r; staying on the "
                "current branch.",
                sha,
            )

    # --- Internal: state detection -------------------------------------

    async def _detect_dirty(self, target: Path) -> bool:
        """
        Return True if the repository has uncommitted changes.

        Uses `git status --porcelain`, which emits one line per
        changed path. Any non-empty output means the workspace is
        dirty.
        """
        try:
            result = await self._git.run(
                ["status", "--porcelain"],
                cwd=target,
                check_returncode=False,
            )
        except GitExecutionError:
            # If the directory is not a git repository, treat it as
            # dirty to be safe: the caller will reject the operation.
            return True
        return bool(result.stdout.strip())

    async def _detect_conflicts(self, target: Path) -> bool:
        """
        Return True if the repository has unresolved merge conflicts.

        Uses `git ls-files -u`, which lists unmerged index entries.
        """
        try:
            result = await self._git.run(
                ["ls-files", "-u"],
                cwd=target,
                check_returncode=False,
            )
        except GitExecutionError:
            return False
        return bool(result.stdout.strip())

    async def _current_branch(
        self, target: Path, *, fallback: str
    ) -> str:
        """
        Return the current branch name, or `fallback` when HEAD is
        detached or the repository is empty.
        """
        try:
            result = await self._git.run(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                cwd=target,
                check_returncode=False,
            )
        except GitExecutionError:
            return fallback
        name = result.stdout.strip()
        if not name or name == "HEAD":
            return fallback
        return name

    async def _head_sha(
        self, target: Path, *, fallback: str
    ) -> str:
        """
        Return the current HEAD SHA, or `fallback` when unavailable.

        Returns `fallback` when any of the following is true:

            - The `git rev-parse` subprocess exits with a non-zero
              status.
            - The captured stdout is empty.
            - The captured stdout is not a valid 40- or 64-character
              hexadecimal commit SHA.

        The SHA-format check is critical and MUST NOT be removed.
        On a repository with an unborn HEAD (no commits yet),
        `git rev-parse HEAD` exits with status 128 and echoes the
        literal string `"HEAD"` to stdout, alongside a fatal error
        on stderr. Without this validation, that string is treated
        as a valid commit SHA, propagates into the workspace's
        `base_sha`, and breaks every downstream `git diff <base_sha>`
        call: "HEAD" is a movable reference, so once `request_approval`
        commits the staged changes, `git diff HEAD` compares the
        just-committed tree to itself and returns an empty diff —
        which surfaces to the agent as "No changes detected",
        causing an infinite retry loop that only the operator can
        stop. Returning `fallback` here lets `_materialize` proceed
        to its empty-commit bootstrap path, which produces a real
        SHA that the downstream code can diff against.
        """
        try:
            result = await self._git.run(
                ["rev-parse", "HEAD"],
                cwd=target,
                check_returncode=False,
            )
        except GitExecutionError:
            return fallback

        # Reject any non-zero exit. `check_returncode=False` above
        # exists only to suppress the exception on a normal
        # "unborn HEAD" call; the caller still needs the exit code.
        if getattr(result, "returncode", 0) != 0:
            return fallback

        sha = result.stdout.strip()
        if not sha:
            return fallback

        # Validate the SHA shape: 40 hex (SHA-1) or 64 hex
        # (SHA-256). Anything else — "HEAD", "refs/heads/main",
        # an abbreviated ref, a tag name, etc. — is a symbolic
        # reference, NOT a commit SHA, and MUST NOT be stored as
        # `base_sha` or `head_sha`.
        lowered = sha.lower()
        if len(lowered) not in (40, 64):
            return fallback
        if any(c not in "0123456789abcdef" for c in lowered):
            return fallback

        return sha

    # --- Internal: cleanup ---------------------------------------------

    async def _force_remove_directory(self, path: Path) -> None:
        """
        Remove a workspace directory tree.

        This method deliberately avoids running Git commands. It
        only removes the directory tree, and only after confirming
        the target is inside the workspace root.
        """
        if not path.exists():
            return
        # Containment check again, in case the caller bypassed the
        # earlier validation.
        try:
            self._policy.resolve_within_root(path, must_exist=False)
        except Exception:
            logger.warning(
                "Refusing to remove a directory outside the "
                "workspace root: %s",
                path,
            )
            return

        try:
            # `shutil.rmtree` may fail on Windows when files are
            # read-only. The `ignore_errors=True` fallback ensures
            # cleanup never blocks the caller.
            await asyncio.to_thread(
                shutil.rmtree, path, True
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to remove workspace directory %s: %s",
                path,
                type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Characters that indicate embedded credentials in a URL.
_URL_CREDS_PATTERN = r"://[^/@]+@"


def _strip_url_credentials(url: str) -> str:
    """
    Return `url` with any embedded `user:password@` portion removed.

    PELDRUN never persists credentials in `.git/config`. When a URL
    contains credentials, they are stripped before use; the caller is
    expected to supply authentication via a credential helper if
    needed.
    """
    if not url:
        return url
    import re

    return re.sub(_URL_CREDS_PATTERN, "://", url)


def _inject_token_in_url(clone_url: str, token: str) -> str:
    """
    Return `clone_url` with the token embedded for authentication.

    The form `https://x-access-token:TOKEN@host/path` is accepted by
    GitHub, GitLab, and Bitbucket Cloud for HTTPS clone. SSH URLs
    (git@...) cannot carry a token this way and are returned
    unchanged; the operation will then fail with a clear auth error,
    which is the correct behavior.

    The token is URL-encoded only if it contains characters that
    would break the URL structure. Tokens issued by the three
    supported providers are URL-safe, so encoding is a no-op in
    practice.
    """
    if not clone_url or not token:
        return clone_url

    parsed = urlsplit(clone_url)
    if parsed.scheme.lower() not in ("http", "https"):
        return clone_url
    if not parsed.hostname:
        return clone_url

    # Percent-encode the token so that reserved URL characters it
    # might contain (`@`, `:`, `/`, `?`, `#`, space, ...) cannot
    # corrupt the URL structure. Tokens issued by GitHub, GitLab,
    # and Bitbucket are URL-safe in practice, but a custom or
    # legacy credential could contain a reserved character and
    # silently break the clone. `quote(..., safe="")` keeps the
    # RFC 3986 unreserved set (A-Z a-z 0-9 - _ . ~) and
    # percent-encodes everything else.
    encoded_token = quote(token, safe="")
    netloc = f"x-access-token:{encoded_token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"

    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


# ---------------------------------------------------------------------------
# Default workspace manager singleton
# ---------------------------------------------------------------------------
#
# The agent tools and the HTTP facade reach for a process-wide default
# `GitWorkspaceManager`. It uses the default `GitExecutable` and the
# default `WorkspacePathPolicy`, which are themselves process-wide
# singletons. Tests can replace the manager via
# `set_default_workspace_manager` or discard it via
# `reset_default_workspace_manager`.

_DEFAULT_WORKSPACE_MANAGER: Optional[GitWorkspaceManager] = None
_WORKSPACE_MANAGER_LOCK = threading.RLock()


def get_default_workspace_manager() -> GitWorkspaceManager:
    """
    Return the process-wide default `GitWorkspaceManager`, creating it
    on first use.

    The manager uses the process-wide default `GitExecutable` and
    `WorkspacePathPolicy`. Tests that need an isolated workspace root
    SHOULD construct a manager explicitly with their own policy.
    """
    global _DEFAULT_WORKSPACE_MANAGER
    with _WORKSPACE_MANAGER_LOCK:
        if _DEFAULT_WORKSPACE_MANAGER is None:
            _DEFAULT_WORKSPACE_MANAGER = GitWorkspaceManager()
        return _DEFAULT_WORKSPACE_MANAGER


def set_default_workspace_manager(manager: GitWorkspaceManager) -> None:
    """
    Replace the process-wide default workspace manager.

    Intended for application startup and for tests.
    """
    if not isinstance(manager, GitWorkspaceManager):
        raise TypeError(
            "set_default_workspace_manager expects a "
            "GitWorkspaceManager instance."
        )
    global _DEFAULT_WORKSPACE_MANAGER
    with _WORKSPACE_MANAGER_LOCK:
        _DEFAULT_WORKSPACE_MANAGER = manager


def reset_default_workspace_manager() -> GitWorkspaceManager:
    """
    Replace the default manager with a fresh one.

    Used by tests to guarantee isolation between test cases. The new
    manager uses the current default path policy and Git executable.
    """
    global _DEFAULT_WORKSPACE_MANAGER
    with _WORKSPACE_MANAGER_LOCK:
        _DEFAULT_WORKSPACE_MANAGER = GitWorkspaceManager()
        return _DEFAULT_WORKSPACE_MANAGER
