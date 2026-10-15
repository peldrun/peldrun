from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from ..domain.enums import SourceControlErrorCode
from ..errors import ConflictError, SourceControlError, ValidationError
from .executable import GitExecutable, GitExecutionError
from .path_policy import secure_resolve_path


@dataclass(frozen=True)
class ConflictMarkerSection:
    """Represents a specific block of conflicting lines within a single file."""

    start_line: int
    end_line: int
    our_branch: str
    their_branch: str
    our_content: str
    their_content: str


@dataclass(frozen=True)
class FileConflictDetail:
    """Detailed conflict status and parsed markers for an individual file."""

    path: str
    has_markers: bool
    marker_count: int
    sections: Sequence[ConflictMarkerSection] = field(default_factory=tuple)


@dataclass(frozen=True)
class ConflictReport:
    """Immutable aggregate diagnostic report of all unresolved conflicts in a workspace."""

    has_conflicts: bool
    is_merge_in_progress: bool
    is_rebase_in_progress: bool
    conflicted_files: Sequence[str] = field(default_factory=tuple)
    file_details: Sequence[FileConflictDetail] = field(default_factory=tuple)
    message: str = ""


class GitConflictDetector:
    """Thread-safe inspector responsible for discovering, parsing, and managing Git merge/rebase conflicts."""

    def __init__(self, git: Optional[GitExecutable] = None) -> None:
        self.git = git or GitExecutable()

    @staticmethod
    def _parse_conflict_markers(content: str) -> list[ConflictMarkerSection]:
        """Parses standard Git conflict markers line-by-line to prevent ReDoS."""
        lines = content.splitlines()
        sections: list[ConflictMarkerSection] = []

        in_marker = False
        start_line = 0
        our_label = ""
        our_lines: list[str] = []
        their_lines: list[str] = []
        in_their = False

        for idx, line in enumerate(lines, start=1):
            if line.startswith("<<<<<<<"):
                in_marker = True
                in_their = False
                start_line = idx
                our_label = line[7:].strip()
                our_lines = []
                their_lines = []
            elif in_marker and line.startswith("======="):
                in_their = True
            elif in_marker and line.startswith(">>>>>>>"):
                their_label = line[7:].strip()
                sections.append(
                    ConflictMarkerSection(
                        start_line=start_line,
                        end_line=idx,
                        our_branch=our_label,
                        their_branch=their_label,
                        our_content="\n".join(our_lines),
                        their_content="\n".join(their_lines),
                    )
                )
                in_marker = False
                in_their = False
            elif in_marker:
                if in_their:
                    their_lines.append(line)
                else:
                    our_lines.append(line)

        return sections

    async def get_conflicted_files(self, cwd: str | Path) -> Sequence[str]:
        """Lists paths of all files currently marked as unmerged in the Git index."""
        cwd_str = str(cwd)
        cmd = ["diff", "--name-only", "--diff-filter=U"]
        try:
            res = await self.git.run(cmd, cwd=cwd_str, check=False)
            if not res.success:
                return ()
            return tuple(line.strip() for line in res.stdout.splitlines() if line.strip())
        except Exception:
            return ()

    async def inspect_file_conflicts(
        self, cwd: str | Path, file_path: str | Path
    ) -> FileConflictDetail:
        """Inspects an individual file for physical Git conflict markers."""
        cwd_path = Path(cwd).resolve()
        resolved = secure_resolve_path(cwd_path, file_path)

        if not resolved.exists() or not resolved.is_file():
            return FileConflictDetail(
                path=str(file_path),
                has_markers=False,
                marker_count=0,
                sections=(),
            )

        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as exc:
            raise SourceControlError(
                f"Failed to read file '{file_path}' for conflict inspection: {exc}"
            ) from exc

        sections = self._parse_conflict_markers(content)
        return FileConflictDetail(
            path=str(file_path),
            has_markers=len(sections) > 0,
            marker_count=len(sections),
            sections=tuple(sections),
        )

    async def detect_conflicts(self, cwd: str | Path) -> ConflictReport:
        """Constructs an aggregate diagnostic report of all active conflicts in the workspace."""
        cwd_path = Path(cwd).resolve()
        git_dir = cwd_path / ".git"

        # 1. Inspect filesystem indicators for in-progress operations
        is_merge = False
        is_rebase = False

        if git_dir.exists():
            is_merge = (git_dir / "MERGE_HEAD").exists()
            is_rebase = (git_dir / "rebase-apply").exists() or (git_dir / "rebase-merge").exists()

        # 2. Extract unmerged files from Git
        unmerged_paths = await self.get_conflicted_files(cwd_path)

        # 3. Parse conflict markers for each unmerged file
        details: list[FileConflictDetail] = []
        for path_str in unmerged_paths:
            detail = await self.inspect_file_conflicts(cwd_path, path_str)
            details.append(detail)

        has_conflicts = len(unmerged_paths) > 0 or any(d.has_markers for d in details)

        message = ""
        if has_conflicts:
            message = f"Found {len(unmerged_paths)} unmerged file(s) with active conflicts."
        elif is_merge:
            message = "Merge in progress but no file conflicts detected."
        elif is_rebase:
            message = "Rebase in progress but no file conflicts detected."
        else:
            message = "Workspace working tree is clean with no conflicts."

        return ConflictReport(
            has_conflicts=has_conflicts,
            is_merge_in_progress=is_merge,
            is_rebase_in_progress=is_rebase,
            conflicted_files=unmerged_paths,
            file_details=tuple(details),
            message=message,
        )

    async def has_unresolved_markers(
        self, cwd: str | Path, file_path: str | Path
    ) -> bool:
        """Convenience method checking whether a given file still contains conflict markers."""
        detail = await self.inspect_file_conflicts(cwd, file_path)
        return detail.has_markers

    async def abort_merge(self, cwd: str | Path) -> bool:
        """Aborts an in-progress merge, resetting working tree to pre-merge state."""
        try:
            await self.git.run(["merge", "--abort"], cwd=str(cwd), check=True)
            return True
        except GitExecutionError as exc:
            raise ConflictError(
                f"Failed to abort in-progress merge: {exc.stderr.strip() or exc.stdout.strip()}"
            ) from exc

    async def abort_rebase(self, cwd: str | Path) -> bool:
        """Aborts an in-progress rebase, resetting working tree to original branch head."""
        try:
            await self.git.run(["rebase", "--abort"], cwd=str(cwd), check=True)
            return True
        except GitExecutionError as exc:
            raise ConflictError(
                f"Failed to abort in-progress rebase: {exc.stderr.strip() or exc.stdout.strip()}"
            ) from exc
