from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from ..domain.enums import FileChangeType
from .executable import GitExecutable


@dataclass(frozen=True)
class FileStatusEntry:
    """Represents the parsed status of an individual file within the git working tree."""

    path: str
    change_type: FileChangeType
    is_staged: bool
    orig_path: Optional[str] = None


@dataclass(frozen=True)
class WorkingTreeStatus:
    """Immutable aggregate snapshot of the local repository working tree and tracking branch state."""

    is_clean: bool
    has_conflicts: bool
    branch_name: Optional[str] = None
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    staged_files: Sequence[FileStatusEntry] = field(default_factory=tuple)
    unstaged_files: Sequence[FileStatusEntry] = field(default_factory=tuple)
    untracked_files: Sequence[str] = field(default_factory=tuple)
    conflicted_files: Sequence[str] = field(default_factory=tuple)

    @property
    def total_changed_files(self) -> int:
        """Returns the total number of distinct modified, untracked, and conflicted files."""
        unique_paths = {entry.path for entry in self.staged_files}
        unique_paths.update(entry.path for entry in self.unstaged_files)
        unique_paths.update(self.untracked_files)
        unique_paths.update(self.conflicted_files)
        return len(unique_paths)


class GitStatusInspector:
    """Thread-safe inspector parsing porcelain v2 git status outputs into typed domain snapshots."""

    def __init__(self, git: Optional[GitExecutable] = None) -> None:
        self.git = git or GitExecutable()

    @staticmethod
    def _to_change_type(code: str) -> FileChangeType:
        """Maps single-character git status indicator to FileChangeType enum."""
        mapping = {
            "M": FileChangeType.MODIFIED,
            "A": FileChangeType.ADDED,
            "D": FileChangeType.DELETED,
        }
        # Support extended enum variants dynamically if present
        if hasattr(FileChangeType, "RENAMED"):
            mapping["R"] = FileChangeType.RENAMED  # type: ignore[attr-defined]
        else:
            mapping["R"] = FileChangeType.MODIFIED

        if hasattr(FileChangeType, "COPIED"):
            mapping["C"] = FileChangeType.COPIED  # type: ignore[attr-defined]
        else:
            mapping["C"] = FileChangeType.ADDED

        if hasattr(FileChangeType, "CONFLICTED"):
            mapping["U"] = FileChangeType.CONFLICTED  # type: ignore[attr-defined]
        else:
            mapping["U"] = FileChangeType.MODIFIED

        return mapping.get(code, FileChangeType.MODIFIED)

    async def inspect(self, cwd: str | Path) -> WorkingTreeStatus:
        """Executes git status --porcelain=v2 and constructs an immutable WorkingTreeStatus snapshot."""
        cmd = ["status", "--porcelain=v2", "--branch", "-uall"]
        res = await self.git.run(cmd, cwd=str(cwd), check=True)

        branch_name: Optional[str] = None
        upstream: Optional[str] = None
        ahead = 0
        behind = 0

        staged_list: list[FileStatusEntry] = []
        unstaged_list: list[FileStatusEntry] = []
        untracked_list: list[str] = []
        conflicted_list: list[str] = []

        for line in res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue

            # Branch metadata lines
            if line_str.startswith("# branch.head "):
                val = line_str[len("# branch.head "):].strip()
                branch_name = val if val != "(detached)" else None
            elif line_str.startswith("# branch.upstream "):
                upstream = line_str[len("# branch.upstream "):].strip()
            elif line_str.startswith("# branch.ab "):
                # Format: # branch.ab +<ahead> -<behind>
                tokens = line_str[len("# branch.ab "):].strip().split()
                if len(tokens) >= 2:
                    if tokens[0].startswith("+"):
                        ahead = int(tokens[0][1:]) if tokens[0][1:].isdigit() else 0
                    if tokens[1].startswith("-"):
                        behind = int(tokens[1][1:]) if tokens[1][1:].isdigit() else 0

            # Untracked entries: ? <path>
            elif line_str.startswith("? "):
                untracked_list.append(line_str[2:].strip())

            # Unmerged / Conflicted entries: u <XY> ... <path>
            elif line_str.startswith("u "):
                tokens = line_str.split(" ")
                if len(tokens) >= 11:
                    conflicted_list.append(tokens[10].strip())

            # Ordinary modified entries: 1 <XY> ... <path>
            elif line_str.startswith("1 "):
                tokens = line_str.split(" ", 8)
                if len(tokens) >= 9:
                    xy = tokens[1]
                    path = tokens[8].strip()
                    x_code, y_code = xy[0], xy[1]

                    if x_code != ".":
                        staged_list.append(
                            FileStatusEntry(
                                path=path,
                                change_type=self._to_change_type(x_code),
                                is_staged=True,
                            )
                        )
                    if y_code != ".":
                        unstaged_list.append(
                            FileStatusEntry(
                                path=path,
                                change_type=self._to_change_type(y_code),
                                is_staged=False,
                            )
                        )

            # Renamed or copied entries: 2 <XY> ... <path>\t<orig_path>
            elif line_str.startswith("2 "):
                tokens = line_str.split(" ", 9)
                if len(tokens) >= 10:
                    xy = tokens[1]
                    paths_token = tokens[9]
                    path_parts = paths_token.split("\t")
                    path = path_parts[0].strip()
                    orig_path = path_parts[1].strip() if len(path_parts) > 1 else None

                    x_code, y_code = xy[0], xy[1]

                    if x_code != ".":
                        staged_list.append(
                            FileStatusEntry(
                                path=path,
                                orig_path=orig_path,
                                change_type=self._to_change_type(x_code),
                                is_staged=True,
                            )
                        )
                    if y_code != ".":
                        unstaged_list.append(
                            FileStatusEntry(
                                path=path,
                                orig_path=orig_path,
                                change_type=self._to_change_type(y_code),
                                is_staged=False,
                            )
                        )

        is_clean = not (staged_list or unstaged_list or untracked_list or conflicted_list)
        has_conflicts = len(conflicted_list) > 0

        return WorkingTreeStatus(
            is_clean=is_clean,
            has_conflicts=has_conflicts,
            branch_name=branch_name,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            staged_files=tuple(staged_list),
            unstaged_files=tuple(unstaged_list),
            untracked_files=tuple(untracked_list),
            conflicted_files=tuple(conflicted_list),
        )
