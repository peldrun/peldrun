from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from ..domain.enums import FileChangeType
from ..domain.models import DiffSummary, FileDiff
from ..domain.policies import is_workflow_path
from ..security.approval import compute_diff_sha256
from .executable import GitExecutable


class GitDiffEngine:
    """Thread-safe engine responsible for computing, parsing, and cryptographically fingerprinting Git diffs."""

    def __init__(self, git: Optional[GitExecutable] = None) -> None:
        self.git = git or GitExecutable()

    @staticmethod
    def _parse_change_type(status_code: str) -> FileChangeType:
        """Maps git name-status code (A, M, D, R100, C100) to domain FileChangeType."""
        code = status_code.strip()
        if not code:
            return FileChangeType.MODIFIED

        primary = code[0].upper()
        if primary == "A":
            return FileChangeType.ADDED
        if primary == "D":
            return FileChangeType.DELETED
        if primary == "M":
            return FileChangeType.MODIFIED
        if primary == "R":
            return getattr(FileChangeType, "RENAMED", FileChangeType.MODIFIED)
        if primary == "C":
            return getattr(FileChangeType, "COPIED", FileChangeType.ADDED)

        return FileChangeType.MODIFIED

    async def get_raw_diff(
        self,
        cwd: str | Path,
        base_ref: str,
        target_ref: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> str:
        """Retrieves raw unified patch diff output from Git."""
        cmd = ["diff", "-U3", "-M"]
        if target_ref:
            cmd.append(f"{base_ref}..{target_ref}")
        else:
            cmd.append(base_ref)

        if file_path:
            cmd.extend(["--", file_path])

        res = await self.git.run(cmd, cwd=str(cwd), check=False)
        return res.stdout

    async def compute_diff(
        self,
        cwd: str | Path,
        base_ref: str,
        target_ref: Optional[str] = None,
    ) -> DiffSummary:
        """Calculates file-level metrics, raw patch diff, and cryptographic SHA-256 fingerprint."""
        cwd_str = str(cwd)

        # 1. Resolve current HEAD SHA for tracking
        head_res = await self.git.run(["rev-parse", "HEAD"], cwd=cwd_str, check=False)
        current_head = head_res.stdout.strip() if head_res.success else (target_ref or "")

        # 2. Extract structured file status: name-status with renames (-M)
        name_status_cmd = ["diff", "--name-status", "-M"]
        if target_ref:
            name_status_cmd.append(f"{base_ref}..{target_ref}")
        else:
            name_status_cmd.append(base_ref)

        name_status_res = await self.git.run(name_status_cmd, cwd=cwd_str, check=False)

        # Map path -> (change_type, old_path)
        status_map: dict[str, tuple[FileChangeType, Optional[str]]] = {}
        for line in name_status_res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            parts = line_str.split("\t")
            if not parts:
                continue

            status_code = parts[0]
            change_type = self._parse_change_type(status_code)

            if (status_code.startswith("R") or status_code.startswith("C")) and len(parts) >= 3:
                old_p = parts[1].strip()
                new_p = parts[2].strip()
                status_map[new_p] = (change_type, old_p)
            elif len(parts) >= 2:
                file_p = parts[1].strip()
                status_map[file_p] = (change_type, None)

        # 3. Extract numerical insertions and deletions: numstat with renames (-M)
        numstat_cmd = ["diff", "--numstat", "-M"]
        if target_ref:
            numstat_cmd.append(f"{base_ref}..{target_ref}")
        else:
            numstat_cmd.append(base_ref)

        numstat_res = await self.git.run(numstat_cmd, cwd=cwd_str, check=False)

        file_diffs: list[FileDiff] = []
        total_insertions = 0
        total_deletions = 0

        for line in numstat_res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            parts = line_str.split("\t")
            if len(parts) >= 3:
                ins_str, del_str, path_token = parts[0], parts[1], parts[2]

                # Identify binary file indicator (- - <path>)
                is_binary = ins_str == "-" and del_str == "-"
                ins = int(ins_str) if ins_str.isdigit() else 0
                dels = int(del_str) if del_str.isdigit() else 0

                # In case of rename notation in numstat (e.g. "path/{old => new}.ext")
                # resolve canonical target path from status_map if available
                matched_path = path_token.strip()
                old_path: Optional[str] = None
                change_type = FileChangeType.MODIFIED

                if matched_path in status_map:
                    change_type, old_path = status_map[matched_path]
                else:
                    # Lookup path matching rename pattern or suffix
                    for status_p, (c_type, o_p) in status_map.items():
                        if status_p in matched_path or (o_p and o_p in matched_path):
                            matched_path = status_p
                            change_type = c_type
                            old_path = o_p
                            break

                total_insertions += ins
                total_deletions += dels

                file_diffs.append(
                    FileDiff(
                        path=matched_path,
                        change_type=change_type,
                        insertions=ins,
                        deletions=dels,
                        old_path=old_path,
                        is_binary=is_binary,
                    )
                )

        # 4. Generate unified diff text and compute deterministic SHA-256 fingerprint
        raw_diff = await self.get_raw_diff(cwd_str, base_ref=base_ref, target_ref=target_ref)
        diff_sha256 = compute_diff_sha256(raw_diff)

        return DiffSummary(
            base_sha=base_ref,
            head_sha=current_head,
            total_files=len(file_diffs),
            insertions=total_insertions,
            deletions=total_deletions,
            files=tuple(file_diffs),
            diff_sha256=diff_sha256,
        )

    @staticmethod
    def contains_workflow_changes(diff_summary: DiffSummary) -> bool:
        """Inspects whether any file modified in the diff touches CI/CD automation."""
        return any(is_workflow_path(f.path) for f in diff_summary.files)

    @staticmethod
    def get_workflow_files(diff_summary: DiffSummary) -> Sequence[str]:
        """Returns paths of all files in the diff touching CI/CD automation."""
        return tuple(f.path for f in diff_summary.files if is_workflow_path(f.path))

    @staticmethod
    def contains_binary_files(diff_summary: DiffSummary) -> bool:
        """Inspects whether any modified file in the diff is a binary asset."""
        return any(f.is_binary for f in diff_summary.files)
