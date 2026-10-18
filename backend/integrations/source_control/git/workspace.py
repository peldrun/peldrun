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
import logging
import shutil
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence
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

        if at_capacity:
            # Release the lock before cleanup so that a slow
            # `rmtree` never blocks other registry operations.
            await self._force_remove_directory(workspace_path)
            raise ValidationError(
                "Workspace registry is at capacity.",
                details={"max_workspaces": self._max_workspaces},
            )

        return materialized

    # --- Lifecycle: read -----------------------------------------------

    def get_workspace(self, workspace_id: str) -> Optional[WorkspaceRef]:
        """
        Return the workspace with the given ID, or `None` if unknown.

        This method never raises for a missing workspace; callers that
        need a strict failure use `require_workspace`.
        """
        if not workspace_id or not isinstance(workspace_id, str):
            return None
        with self._lock:
            return self._workspaces.get(workspace_id)

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

    async def delete_workspace(self, workspace_id: str) -> None:
        """
        Remove a PELDRUN-owned workspace from disk and the registry.

        This operation is destructive and MUST only be called for
        workspaces owned by this manager. The target directory MUST
        resolve inside the workspace root, or the operation is
        refused.

        Raises:
            WorkspaceNotFoundError: If the ID is unknown.
            PathTraversalError: If the resolved path escapes the
                root.
        """
        workspace = self.require_workspace(workspace_id)

        # Validate containment before any destructive action.
        try:
            resolved = self._policy.resolve_within_root(
                workspace.workspace_path, must_exist=False
            )
        except Exception:
            # Refuse to delete anything we cannot prove is inside
            # the root.
            raise

        await self._force_remove_directory(resolved)

        with self._lock:
            self._workspaces.pop(workspace_id, None)

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
        target.parent.mkdir(parents=True, exist_ok=True)

        branch = request.branch_name or workspace.current_branch

        if request.clone_url:
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
        dirty = await self._detect_dirty(target)

        return replace(
            workspace,
            current_branch=branch_now,
            head_sha=head_sha,
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

        A new branch is created from the current HEAD when it does
        not already exist.

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

        # Create the branch from HEAD. Lowercase `-b` refuses to
        # overwrite an existing branch, protecting user commits.
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
        """
        try:
            result = await self._git.run(
                ["rev-parse", "HEAD"],
                cwd=target,
                check_returncode=False,
            )
        except GitExecutionError:
            return fallback
        sha = result.stdout.strip()
        return sha or fallback

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
