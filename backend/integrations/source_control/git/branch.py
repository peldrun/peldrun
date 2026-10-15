from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from ..domain.models import BranchRef
from ..errors import ConflictError, NotFoundError, SourceControlError
from .executable import GitExecutable, GitExecutionError
from .path_policy import validate_branch_name


class GitBranchManager:
    """Thread-safe manager coordinating local Git branch creation, checkout, listing, and safe deletion."""

    def __init__(self, git: Optional[GitExecutable] = None) -> None:
        self.git = git or GitExecutable()

    async def get_current_branch(self, cwd: str | Path) -> str:
        """Retrieves the active branch name currently checked out in the repository."""
        res = await self.git.run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(cwd), check=True)
        branch = res.stdout.strip()
        if branch == "HEAD":
            # Detached HEAD state
            sha_res = await self.git.run(["rev-parse", "HEAD"], cwd=str(cwd), check=True)
            return f"(detached at {sha_res.stdout.strip()[:8]})"
        return branch

    async def branch_exists(self, cwd: str | Path, branch_name: str) -> bool:
        """Checks whether a local branch exists."""
        clean_name = validate_branch_name(branch_name)
        res = await self.git.run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{clean_name}"],
            cwd=str(cwd),
            check=False,
        )
        return res.returncode == 0

    async def list_branches(
        self, cwd: str | Path, include_remote: bool = False
    ) -> Sequence[BranchRef]:
        """Lists local branches, and optionally remote-tracking branches, as immutable BranchRef objects."""
        cwd_str = str(cwd)
        cmd = ["branch", "--list", "--format=%(refname:short)|%(objectname)|%(HEAD)"]
        if include_remote:
            cmd.insert(2, "-a")

        res = await self.git.run(cmd, cwd=cwd_str, check=True)

        branches: list[BranchRef] = []
        for line in res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue

            parts = line_str.split("|")
            if len(parts) >= 2:
                raw_name = parts[0].strip()
                head_sha = parts[1].strip() or None
                is_head = parts[2].strip() == "*" if len(parts) >= 3 else False

                # Filter out symbolic HEAD pointers like origin/HEAD -> origin/main
                if "->" in raw_name:
                    continue

                branches.append(
                    BranchRef(
                        name=raw_name,
                        head_sha=head_sha,
                        is_default=is_head,
                    )
                )

        return tuple(branches)

    async def create_branch(
        self,
        cwd: str | Path,
        branch_name: str,
        start_point: Optional[str] = None,
        checkout: bool = False,
    ) -> BranchRef:
        """Creates a new branch from an optional start point (commit SHA or branch) and optionally checks it out.

        Raises:
            ValidationError: If branch_name violates git naming rules.
            ConflictError: If branch already exists locally.
            GitExecutionError: If start_point cannot be resolved.
        """
        clean_name = validate_branch_name(branch_name)
        cwd_str = str(cwd)

        if await self.branch_exists(cwd_str, clean_name):
            raise ConflictError(f"Branch '{clean_name}' already exists in workspace.")

        cmd = ["checkout", "-b", clean_name] if checkout else ["branch", clean_name]
        if start_point:
            cmd.append(start_point)

        await self.git.run(cmd, cwd=cwd_str, check=True)

        # Retrieve newly created branch head commit SHA
        sha_res = await self.git.run(["rev-parse", clean_name], cwd=cwd_str, check=True)
        head_sha = sha_res.stdout.strip()

        return BranchRef(
            name=clean_name,
            head_sha=head_sha,
            is_default=checkout,
        )

    async def checkout_branch(self, cwd: str | Path, branch_name: str) -> BranchRef:
        """Switches the working directory to an existing local branch.

        Raises:
            NotFoundError: If the branch does not exist.
            ConflictError: If working tree contains uncommitted changes conflicting with checkout.
        """
        clean_name = validate_branch_name(branch_name)
        cwd_str = str(cwd)

        if not await self.branch_exists(cwd_str, clean_name):
            raise NotFoundError(f"Local branch '{clean_name}' not found.")

        try:
            await self.git.run(["checkout", clean_name], cwd=cwd_str, check=True)
        except GitExecutionError as exc:
            if "local changes to the following files would be overwritten" in exc.stderr.lower():
                raise ConflictError(
                    f"Cannot checkout '{clean_name}': local uncommitted changes would be overwritten.",
                    details={"branch": clean_name, "stderr": exc.stderr},
                ) from exc
            raise

        sha_res = await self.git.run(["rev-parse", "HEAD"], cwd=cwd_str, check=True)
        head_sha = sha_res.stdout.strip()

        return BranchRef(name=clean_name, head_sha=head_sha, is_default=True)

    async def delete_branch(
        self, cwd: str | Path, branch_name: str, force: bool = False
    ) -> bool:
        """Safely deletes a local branch.

        Raises:
            ValidationError: If branch_name is malformed.
            NotFoundError: If branch does not exist.
            ConflictError: If trying to delete current branch or an unmerged branch without force=True.
        """
        clean_name = validate_branch_name(branch_name)
        cwd_str = str(cwd)

        if not await self.branch_exists(cwd_str, clean_name):
            raise NotFoundError(f"Local branch '{clean_name}' does not exist.")

        current = await self.get_current_branch(cwd_str)
        if current == clean_name:
            raise ConflictError(
                f"Cannot delete branch '{clean_name}' because it is currently checked out."
            )

        flag = "-D" if force else "-d"
        try:
            await self.git.run(["branch", flag, clean_name], cwd=cwd_str, check=True)
            return True
        except GitExecutionError as exc:
            if "not fully merged" in exc.stderr.lower():
                raise ConflictError(
                    f"Branch '{clean_name}' is not fully merged. Use force=True to delete.",
                    details={"branch": clean_name},
                ) from exc
            raise SourceControlError(
                f"Failed to delete branch '{clean_name}': {exc.stderr.strip()}"
            ) from exc
