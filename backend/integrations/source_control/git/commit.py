from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from ..domain.models import CommitRef
from ..errors import ConflictError, NotFoundError, SourceControlError, ValidationError
from ..security.redaction import redact_text
from .executable import GitExecutable, GitExecutionError
from .path_policy import secure_resolve_path


class GitCommitManager:
    """Thread-safe manager coordinating file staging, committer identity injection, and atomic commits."""

    def __init__(
        self,
        git: Optional[GitExecutable] = None,
        default_author_name: str = "PELDRUN Agent",
        default_author_email: str = "agent@peldrun.local",
    ) -> None:
        self.git = git or GitExecutable()
        self.default_author_name = default_author_name
        self.default_author_email = default_author_email

    def _get_identity_args(
        self, author_name: Optional[str] = None, author_email: Optional[str] = None
    ) -> list[str]:
        """Generates transient CLI configuration arguments to inject author identity."""
        name = author_name or self.default_author_name
        email = author_email or self.default_author_email
        return ["-c", f"user.name={name}", "-c", f"user.email={email}"]

    async def stage_files(
        self, cwd: str | Path, file_paths: Sequence[str | Path]
    ) -> None:
        """Stages specific files after verifying path containment within workspace root.

        Raises:
            ValidationError: If any path escapes workspace root.
            GitExecutionError: If git add fails.
        """
        if not file_paths:
            return

        cwd_path = Path(cwd).resolve()
        relative_paths: list[str] = []

        for p in file_paths:
            resolved = secure_resolve_path(cwd_path, p)
            rel = resolved.relative_to(cwd_path).as_posix()
            relative_paths.append(rel)

        cmd = ["add", "--", *relative_paths]
        await self.git.run(cmd, cwd=str(cwd_path), check=True)

    async def stage_all(self, cwd: str | Path) -> None:
        """Stages all tracked and untracked changes in the working directory."""
        await self.git.run(["add", "-A"], cwd=str(cwd), check=True)

    async def reset_index(self, cwd: str | Path) -> None:
        """Unstages all staged changes without modifying working tree files."""
        await self.git.run(["reset"], cwd=str(cwd), check=True)

    async def create_commit(
        self,
        cwd: str | Path,
        message: str,
        *,
        author_name: Optional[str] = None,
        author_email: Optional[str] = None,
        allow_empty: bool = False,
    ) -> CommitRef:
        """Creates an atomic Git commit with a scrubbed message and transient committer credentials.

        Raises:
            ValidationError: If commit message is empty.
            ConflictError: If working tree index has no changes to commit and allow_empty is False.
        """
        if not message or not message.strip():
            raise ValidationError("Commit message cannot be empty.")

        clean_message = redact_text(message.strip())
        identity_args = self._get_identity_args(author_name, author_email)
        cwd_str = str(cwd)

        cmd = [*identity_args, "commit", "-m", clean_message]
        if allow_empty:
            cmd.append("--allow-empty")

        try:
            await self.git.run(cmd, cwd=cwd_str, check=True)
        except GitExecutionError as exc:
            lower_err = (exc.stderr + exc.stdout).lower()
            if "nothing to commit" in lower_err or "no changes added to commit" in lower_err:
                raise ConflictError(
                    "Nothing to commit: working tree and staging index are clean.",
                    details={"cwd": cwd_str},
                ) from exc
            raise SourceControlError(
                f"Failed to create commit: {exc.stderr.strip() or exc.stdout.strip()}"
            ) from exc

        return await self.get_commit(cwd_str, rev="HEAD")

    async def get_commit(self, cwd: str | Path, rev: str = "HEAD") -> CommitRef:
        """Inspects and returns a typed CommitRef for a specific revision or commit SHA.

        Raises:
            NotFoundError: If revision cannot be resolved.
        """
        cwd_str = str(cwd)
        delimiter = "---PELDRUN_COMMIT_DELIM---"
        # Format: SHA | Author | ISO Date | Parent SHAs \n delimiter \n Message
        cmd = ["log", "-1", f"--format=%H|%an <%ae>|%aI|%P%n{delimiter}%n%B", rev]

        try:
            res = await self.git.run(cmd, cwd=cwd_str, check=True)
        except GitExecutionError as exc:
            raise NotFoundError(f"Commit or revision '{rev}' not found in workspace.") from exc

        raw = res.stdout
        parts = raw.split(delimiter, 1)
        if not parts:
            raise NotFoundError(f"Unable to parse commit output for revision '{rev}'.")

        meta_line = parts[0].strip()
        commit_message = parts[1].strip() if len(parts) > 1 else ""

        meta_tokens = meta_line.split("|")
        if len(meta_tokens) < 4:
            raise SourceControlError(f"Malformed commit metadata returned for '{rev}'.")

        commit_sha = meta_tokens[0].strip()
        author = meta_tokens[1].strip()
        iso_date_str = meta_tokens[2].strip()
        parents_str = meta_tokens[3].strip()

        try:
            created_at = datetime.fromisoformat(iso_date_str)
        except Exception:
            created_at = datetime.utcnow()

        parent_shas = tuple(p for p in parents_str.split() if p)

        return CommitRef(
            commit_sha=commit_sha,
            message=commit_message,
            author=author,
            created_at=created_at,
            parent_shas=parent_shas,
        )

    async def list_recent_commits(
        self, cwd: str | Path, max_count: int = 10, branch: Optional[str] = None
    ) -> Sequence[CommitRef]:
        """Lists recent commits in reverse chronological order."""
        cwd_str = str(cwd)
        cmd = ["rev-list", f"--max-count={max(1, max_count)}"]
        if branch:
            cmd.append(branch)
        else:
            cmd.append("HEAD")

        try:
            res = await self.git.run(cmd, cwd=cwd_str, check=True)
        except GitExecutionError:
            return ()

        commits: list[CommitRef] = []
        for sha in res.stdout.splitlines():
            sha_clean = sha.strip()
            if sha_clean:
                try:
                    c = await self.get_commit(cwd_str, rev=sha_clean)
                    commits.append(c)
                except NotFoundError:
                    continue

        return tuple(commits)
