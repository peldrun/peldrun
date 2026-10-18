from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from ...domain.enums import FileChangeType, ProviderId
from ...domain.models import (
    BranchRef,
    ChangeRequestRef,
    CommitRef,
    DiffSummary,
    FileDiff,
    ProviderAccount,
    RepositoryRef,
)
from ...security.redaction import redact_text, redact_url

# Resilient domain model imports with fallback definitions for checks
try:
    from ...domain.models import CheckRunRef, CheckSummaryRef
except ImportError:
    @dataclass(frozen=True)
    class CheckRunRef:  # type: ignore[no-redef]
        name: str
        status: str
        conclusion: Optional[str] = None
        web_url: Optional[str] = None
        started_at: Optional[datetime] = None
        completed_at: Optional[datetime] = None

    @dataclass(frozen=True)
    class CheckSummaryRef:  # type: ignore[no-redef]
        ref: str
        total_count: int
        success_count: int
        failure_count: int
        pending_count: int
        overall_state: str
        runs: tuple[CheckRunRef, ...] = field(default_factory=tuple)


def parse_iso_timestamp(timestamp_str: Optional[str]) -> Optional[datetime]:
    """Safely parses ISO 8601 timestamp string returned by GitHub into a datetime object."""
    if not timestamp_str or not isinstance(timestamp_str, str):
        return None
    try:
        return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except Exception:
        return None


class GitHubDataMapper:
    """Stateless transformation engine converting raw GitHub REST payloads into neutral domain models."""

    @staticmethod
    def parse_account(
        data: Mapping[str, Any], web_base_url: str = "https://github.com"
    ) -> ProviderAccount:
        """Transforms a raw GitHub authenticated user payload into a ProviderAccount."""
        account_id = str(data.get("id", ""))
        username = str(data.get("login") or f"user_{account_id}")

        return ProviderAccount(
            provider=ProviderId.GITHUB,
            account_id=account_id,
            username=username,
            base_url=web_base_url,
            connected=True,
        )

    @staticmethod
    def parse_repository(data: Mapping[str, Any]) -> RepositoryRef:
        """Transforms a raw GitHub repository API payload into a domain RepositoryRef."""
        repo_id = str(data.get("id", ""))
        full_name = str(data.get("full_name", ""))

        if "/" in full_name:
            namespace, name = full_name.split("/", 1)
        else:
            owner_obj = data.get("owner")
            namespace = owner_obj.get("login", "") if isinstance(owner_obj, dict) else ""
            name = str(data.get("name", ""))

        owner_id = ""
        owner_data = data.get("owner")
        if isinstance(owner_data, dict):
            owner_id = str(owner_data.get("id", ""))

        is_private = bool(data.get("private", False))
        visibility = str(data.get("visibility", "private" if is_private else "public"))

        metadata = {
            "full_name": full_name,
            "description": data.get("description"),
            "visibility": visibility,
            "fork": bool(data.get("fork", False)),
            "archived": bool(data.get("archived", False)),
            "stargazers_count": int(data.get("stargazers_count", 0)),
            "open_issues_count": int(data.get("open_issues_count", 0)),
        }

        return RepositoryRef(
            provider=ProviderId.GITHUB,
            account_id=owner_id,
            repository_id=repo_id,
            namespace=namespace,
            name=name,
            clone_url=redact_url(str(data.get("clone_url", ""))),
            web_url=redact_url(str(data.get("html_url", ""))),
            default_branch=str(data.get("default_branch") or "main"),
            is_private=is_private,
            metadata=metadata,
        )

    @staticmethod
    def parse_branch(
        data: Mapping[str, Any], default_branch: Optional[str] = None
    ) -> BranchRef:
        """Transforms a raw GitHub branch API payload into a domain BranchRef."""
        name = str(data.get("name", ""))
        commit_data = data.get("commit")

        head_sha: Optional[str] = None
        if isinstance(commit_data, dict):
            head_sha = commit_data.get("sha")
        elif isinstance(commit_data, str):
            head_sha = commit_data

        is_default = bool(default_branch and name == default_branch)
        is_protected = bool(data.get("protected", False))

        return BranchRef(
            name=name,
            head_sha=head_sha,
            is_default=is_default,
            is_protected=is_protected,
        )

    @staticmethod
    def parse_commit(data: Mapping[str, Any]) -> CommitRef:
        """Transforms a raw GitHub commit payload into a domain CommitRef."""
        commit_sha = str(data.get("sha", ""))
        commit_obj = data.get("commit") if isinstance(data.get("commit"), dict) else data

        author_name = "Unknown"
        author_email = ""
        # Default to a timezone-aware UTC "now" so that consumers do
        # not receive a naive datetime when the payload omits the
        # author date.
        created_at = datetime.now(timezone.utc)

        if isinstance(commit_obj, dict):
            raw_msg = str(commit_obj.get("message") or "")
            clean_msg = redact_text(raw_msg.strip())

            author_data = commit_obj.get("author")
            if isinstance(author_data, dict):
                author_name = str(author_data.get("name") or author_name)
                author_email = str(author_data.get("email") or "")
                date_str = author_data.get("date")
                parsed_dt = parse_iso_timestamp(date_str)
                if parsed_dt:
                    created_at = parsed_dt
        else:
            clean_msg = ""

        author_formatted = f"{author_name} <{author_email}>" if author_email else author_name

        parents_raw = data.get("parents", [])
        parent_shas: list[str] = []
        if isinstance(parents_raw, list):
            for p in parents_raw:
                if isinstance(p, dict) and "sha" in p:
                    parent_shas.append(str(p["sha"]))
                elif isinstance(p, str):
                    parent_shas.append(p)

        return CommitRef(
            commit_sha=commit_sha,
            message=clean_msg,
            author=author_formatted,
            created_at=created_at,
            parent_shas=tuple(parent_shas),
        )

    @staticmethod
    def parse_change_request(
        data: Mapping[str, Any], repository_id: str
    ) -> ChangeRequestRef:
        """Transforms a raw GitHub pull request payload into a domain ChangeRequestRef."""
        number = str(data.get("number", ""))
        html_url = redact_url(str(data.get("html_url", "")))
        raw_state = str(data.get("state", "open")).lower()
        merged_at_raw = data.get("merged_at")
        state = "merged" if merged_at_raw else raw_state

        head_obj = data.get("head")
        base_obj = data.get("base")
        source_branch = head_obj.get("ref", "") if isinstance(head_obj, dict) else ""
        target_branch = base_obj.get("ref", "") if isinstance(base_obj, dict) else ""

        title = redact_text(str(data.get("title", "")))
        body = redact_text(str(data.get("body") or ""))
        is_draft = bool(data.get("draft", False))
        created_at = parse_iso_timestamp(data.get("created_at"))

        metadata = {
            "id": data.get("id"),
            "is_draft": is_draft,
            "body": body,
            "merged_at": merged_at_raw,
            "created_at": data.get("created_at"),
            "user": data.get("user", {}).get("login") if isinstance(data.get("user"), dict) else None,
        }

        model_fields = {f.name for f in dataclasses.fields(ChangeRequestRef)}
        kwargs: dict[str, Any] = {
            "provider": ProviderId.GITHUB,
            "repository_id": repository_id,
            "number_or_iid": number,
            "web_url": html_url,
            "state": state,
            "source_branch": source_branch,
            "target_branch": target_branch,
        }

        if "title" in model_fields:
            kwargs["title"] = title
        if "body" in model_fields:
            kwargs["body"] = body
        if "is_draft" in model_fields:
            kwargs["is_draft"] = is_draft
        if "created_at" in model_fields:
            kwargs["created_at"] = created_at
        if "metadata" in model_fields:
            kwargs["metadata"] = metadata

        return ChangeRequestRef(**kwargs)

    @staticmethod
    def parse_check_run(data: Mapping[str, Any]) -> CheckRunRef:
        """Transforms a raw GitHub check-run API object into a typed CheckRunRef.

        NOTE: GitHub exposes both ``html_url`` and ``details_url`` on a
        check run. The ``details_url`` field is the authoritative
        link to the CI build logs, while ``html_url`` points to the
        GitHub-hosted summary. This mapper uses ``html_url`` for
        historical reasons; callers that need the build-log URL
        should prefer ``checks.GitHubChecksService``, which uses
        ``details_url``.
        """
        name = str(data.get("name", "unnamed-check"))
        status = str(data.get("status", "completed")).lower()
        conclusion_raw = data.get("conclusion")
        conclusion = str(conclusion_raw).lower() if conclusion_raw else None
        html_url = redact_url(str(data.get("html_url") or ""))

        started_at = parse_iso_timestamp(data.get("started_at"))
        completed_at = parse_iso_timestamp(data.get("completed_at"))

        model_fields = {f.name for f in dataclasses.fields(CheckRunRef)}
        kwargs: dict[str, Any] = {
            "name": name,
            "status": status,
            "conclusion": conclusion,
        }
        if "web_url" in model_fields:
            kwargs["web_url"] = html_url
        if "started_at" in model_fields:
            kwargs["started_at"] = started_at
        if "completed_at" in model_fields:
            kwargs["completed_at"] = completed_at

        return CheckRunRef(**kwargs)

    @staticmethod
    def parse_commit_status(data: Mapping[str, Any]) -> CheckRunRef:
        """Transforms a classic GitHub commit status payload into a typed CheckRunRef."""
        context = str(data.get("context", "default-status"))
        state = str(data.get("state", "pending")).lower()
        target_url = redact_url(str(data.get("target_url") or ""))
        created_at = parse_iso_timestamp(data.get("created_at"))

        if state in {"success", "failure", "error"}:
            status = "completed"
            conclusion = "success" if state == "success" else "failure"
        else:
            status = "in_progress"
            conclusion = None

        model_fields = {f.name for f in dataclasses.fields(CheckRunRef)}
        kwargs: dict[str, Any] = {
            "name": context,
            "status": status,
            "conclusion": conclusion,
        }
        if "web_url" in model_fields:
            kwargs["web_url"] = target_url
        if "started_at" in model_fields:
            kwargs["started_at"] = created_at
        if "completed_at" in model_fields:
            kwargs["completed_at"] = created_at if status == "completed" else None

        return CheckRunRef(**kwargs)

    @staticmethod
    def parse_check_summary(
        ref: str, runs: Sequence[CheckRunRef]
    ) -> CheckSummaryRef:
        """Aggregates a sequence of CheckRunRef objects into a CheckSummaryRef."""
        total_count = len(runs)
        success_count = 0
        failure_count = 0
        pending_count = 0

        for r in runs:
            if r.status != "completed":
                pending_count += 1
            elif r.conclusion in {"success", "neutral", "skipped"}:
                success_count += 1
            else:
                failure_count += 1

        if total_count == 0:
            overall_state = "neutral"
        elif failure_count > 0:
            overall_state = "failure"
        elif pending_count > 0:
            overall_state = "pending"
        else:
            overall_state = "success"

        return CheckSummaryRef(
            ref=ref,
            total_count=total_count,
            success_count=success_count,
            failure_count=failure_count,
            pending_count=pending_count,
            overall_state=overall_state,
            runs=tuple(runs),
        )

    @staticmethod
    def parse_file_diff(data: Mapping[str, Any]) -> FileDiff:
        """Transforms a file entry from GitHub commits or compare API into a FileDiff."""
        filename = str(data.get("filename", ""))
        status_str = str(data.get("status", "modified")).lower()
        additions = int(data.get("additions", 0))
        deletions = int(data.get("deletions", 0))
        previous_filename = data.get("previous_filename")

        # Map GitHub file statuses ("added", "removed", "modified", "renamed", "copied")
        if status_str == "added":
            change_type = FileChangeType.ADDED
        elif status_str == "removed":
            change_type = FileChangeType.DELETED
        elif status_str == "renamed":
            change_type = getattr(FileChangeType, "RENAMED", FileChangeType.MODIFIED)
        elif status_str == "copied":
            change_type = getattr(FileChangeType, "COPIED", FileChangeType.ADDED)
        else:
            change_type = FileChangeType.MODIFIED

        model_fields = {f.name for f in dataclasses.fields(FileDiff)}
        kwargs: dict[str, Any] = {
            "path": filename,
            "change_type": change_type,
            "insertions": additions,
            "deletions": deletions,
        }
        if "old_path" in model_fields and previous_filename:
            kwargs["old_path"] = str(previous_filename)
        if "is_binary" in model_fields:
            kwargs["is_binary"] = False

        return FileDiff(**kwargs)

    @classmethod
    def parse_comparison_diff(
        cls, data: Mapping[str, Any], diff_sha256: str = ""
    ) -> DiffSummary:
        """Transforms a GitHub compare API payload into a domain DiffSummary."""
        base_commit = data.get("base_commit", {})
        base_sha = str(base_commit.get("sha", "")) if isinstance(base_commit, dict) else ""

        merge_base_commit = data.get("merge_base_commit", {})
        if not base_sha and isinstance(merge_base_commit, dict):
            base_sha = str(merge_base_commit.get("sha", ""))

        head_res = data.get("commits", [])
        head_sha = ""
        if isinstance(head_res, list) and head_res:
            last_c = head_res[-1]
            if isinstance(last_c, dict) and "sha" in last_c:
                head_sha = str(last_c["sha"])

        files_raw = data.get("files", [])
        file_diffs: list[FileDiff] = []
        total_insertions = 0
        total_deletions = 0

        if isinstance(files_raw, list):
            for f in files_raw:
                if isinstance(f, dict):
                    fd = cls.parse_file_diff(f)
                    file_diffs.append(fd)
                    total_insertions += fd.insertions
                    total_deletions += fd.deletions

        return DiffSummary(
            base_sha=base_sha,
            head_sha=head_sha,
            total_files=len(file_diffs),
            insertions=total_insertions,
            deletions=total_deletions,
            files=tuple(file_diffs),
            diff_sha256=diff_sha256,
        )
