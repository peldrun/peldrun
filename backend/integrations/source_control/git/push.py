from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..domain.policies import is_protected_branch
from ..errors import (
    ConflictError,
    PermissionDeniedError,
    RemoteStateChangedError,
    SourceControlError,
    ValidationError,
)
from ..security.redaction import redact_text
from .executable import GitExecutable, GitExecutionError
from .path_policy import validate_branch_name


@dataclass(frozen=True)
class PushResult:
    """Immutable outcome of a successful remote Git push operation."""

    local_branch: str
    remote_branch: str
    commit_sha: str
    remote_name: str
    is_new_branch: bool
    forced: bool


class GitPushManager:
    """Thread-safe manager coordinating secure remote Git push operations with policy and lease enforcement."""

    def __init__(self, git: Optional[GitExecutable] = None) -> None:
        self.git = git or GitExecutable()

    @staticmethod
    def _get_auth_header_args(token: Optional[str]) -> list[str]:
        """Generates transient CLI arguments injecting HTTP auth without persisting credentials."""
        if not token:
            return []
        encoded = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
        return ["-c", f"http.extraHeader=Authorization: Basic {encoded}"]

    async def push_branch(
        self,
        cwd: str | Path,
        local_branch: str,
        remote_branch: Optional[str] = None,
        remote_name: str = "origin",
        *,
        set_upstream: bool = True,
        force_with_lease: bool = False,
        token: Optional[str] = None,
        allow_protected: bool = False,
    ) -> PushResult:
        """Pushes a local branch to the remote repository.

        Raises:
            ValidationError: If branch names are malformed.
            PermissionDeniedError: If targeting a protected branch without explicit permission.
            RemoteStateChangedError: If remote branch was updated concurrently (non-fast-forward).
            ConflictError: If force-with-lease lease failed or push was rejected.
            SourceControlError: If Git push execution fails.
        """
        clean_local = validate_branch_name(local_branch)
        target_remote_branch = validate_branch_name(remote_branch) if remote_branch else clean_local
        cwd_str = str(cwd)

        # 1. Enforce protected branch policy
        if not allow_protected and is_protected_branch(target_remote_branch):
            raise PermissionDeniedError(
                f"Direct push to protected branch '{target_remote_branch}' is strictly prohibited by policy. "
                "Push to a feature branch and open a change request instead.",
                details={"target_branch": target_remote_branch},
            )

        # 2. Verify local branch exists and obtain head commit SHA
        try:
            head_res = await self.git.run(
                ["rev-parse", clean_local], cwd=cwd_str, check=True
            )
            head_sha = head_res.stdout.strip()
        except GitExecutionError as exc:
            raise ValidationError(
                f"Local branch '{clean_local}' does not exist in workspace."
            ) from exc

        # 3. Assemble safe push command
        auth_args = self._get_auth_header_args(token)
        cmd = [*auth_args, "push"]

        if set_upstream:
            cmd.append("-u")

        if force_with_lease:
            cmd.append("--force-with-lease")

        cmd.extend([remote_name, f"{clean_local}:{target_remote_branch}"])

        # 4. Execute push with error analysis
        try:
            res = await self.git.run(cmd, cwd=cwd_str, check=True)
        except GitExecutionError as exc:
            lower_err = (exc.stderr + exc.stdout).lower()
            clean_err = redact_text(exc.stderr.strip() or exc.stdout.strip())

            # Stale base / concurrent push / non-fast-forward
            if "fetch first" in lower_err or "non-fast-forward" in lower_err or "[rejected]" in lower_err:
                if force_with_lease and "stale info" in lower_err:
                    raise RemoteStateChangedError(
                        f"Push with lease failed for '{target_remote_branch}': remote branch was updated concurrently.",
                        expected_sha="lease",
                        actual_sha="unknown",
                        details={"stderr": clean_err},
                    ) from exc
                raise ConflictError(
                    f"Remote branch '{target_remote_branch}' has advanced. Pull or rebase before pushing.",
                    details={"target_branch": target_remote_branch, "stderr": clean_err},
                ) from exc

            # Remote hook or server-side branch protection decline
            if "pre-receive hook declined" in lower_err or "protected branch" in lower_err:
                raise PermissionDeniedError(
                    f"Remote repository rejected push to '{target_remote_branch}' due to branch protection or server hook.",
                    details={"target_branch": target_remote_branch, "stderr": clean_err},
                ) from exc

            # Authentication / Authorization errors
            if "authentication failed" in lower_err or "invalid credentials" in lower_err or "403" in lower_err:
                raise PermissionDeniedError(
                    f"Authentication failed while pushing to '{remote_name}'. Token may be expired or lacking write scope.",
                    details={"remote": remote_name, "stderr": clean_err},
                ) from exc

            # Generic push failure
            raise SourceControlError(
                f"Failed to push branch '{clean_local}' to '{target_remote_branch}': {clean_err}",
                details={"stderr": clean_err},
            ) from exc

        # 5. Parse push outcome
        combined_output = res.stderr + res.stdout
        is_new_branch = "[new branch]" in combined_output or "* [new branch]" in combined_output

        return PushResult(
            local_branch=clean_local,
            remote_branch=target_remote_branch,
            commit_sha=head_sha,
            remote_name=remote_name,
            is_new_branch=is_new_branch,
            forced=force_with_lease,
        )
