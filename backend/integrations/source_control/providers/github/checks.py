from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from ...domain.models import RepositoryRef
from ...errors import NotFoundError, SourceControlError, ValidationError
from .client import GitHubClient

# Resilient domain model imports with fallback definitions
try:
    from ...domain.models import CheckRunRef
except ImportError:
    @dataclass(frozen=True)
    class CheckRunRef:  # type: ignore[no-redef]
        name: str
        status: str
        conclusion: Optional[str] = None
        web_url: Optional[str] = None
        started_at: Optional[datetime] = None
        completed_at: Optional[datetime] = None


try:
    from ...domain.models import CheckSummaryRef
except ImportError:
    @dataclass(frozen=True)
    class CheckSummaryRef:  # type: ignore[no-redef]
        ref: str
        total_count: int
        success_count: int
        failure_count: int
        pending_count: int
        overall_state: str  # "success", "failure", "pending"
        runs: tuple[CheckRunRef, ...] = field(default_factory=tuple)


class GitHubChecksService:
    """Service coordinating commit status inspection and GitHub Actions check runs querying."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client

    @staticmethod
    def _parse_iso_timestamp(timestamp_str: Optional[str]) -> Optional[datetime]:
        """Safely parses ISO 8601 timestamp returned by GitHub API."""
        if not timestamp_str or not isinstance(timestamp_str, str):
            return None
        try:
            return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except Exception:
            return None

    @classmethod
    def parse_check_run(cls, data: Mapping[str, Any]) -> CheckRunRef:
        """Transforms a raw GitHub check-run API object into a typed CheckRunRef."""
        name = str(data.get("name", "unnamed-check"))
        status = str(data.get("status", "completed")).lower()
        conclusion_raw = data.get("conclusion")
        conclusion = str(conclusion_raw).lower() if conclusion_raw else None
        html_url = str(data.get("html_url") or "")

        started_at = cls._parse_iso_timestamp(data.get("started_at"))
        completed_at = cls._parse_iso_timestamp(data.get("completed_at"))

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

    @classmethod
    def parse_commit_status(cls, data: Mapping[str, Any]) -> CheckRunRef:
        """Transforms a classic GitHub commit status payload into a typed CheckRunRef."""
        context = str(data.get("context", "default-status"))
        state = str(data.get("state", "pending")).lower()
        target_url = str(data.get("target_url") or "")
        created_at = cls._parse_iso_timestamp(data.get("created_at"))

        # Map classic status state ("success", "failure", "error", "pending") to run status and conclusion
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

    async def list_check_runs(
        self,
        repo: RepositoryRef,
        ref: str,
        token: Optional[str] = None,
    ) -> Sequence[CheckRunRef]:
        """Queries GitHub Actions check-runs associated with a commit SHA or branch ref.

        Raises:
            ValidationError: If ref is empty.
            NotFoundError: If repository or ref does not exist.
        """
        clean_ref = ref.strip()
        if not clean_ref:
            raise ValidationError("Target ref or commit SHA cannot be empty.")

        endpoint = f"/repos/{repo.namespace}/{repo.name}/commits/{clean_ref}/check-runs"
        params = {"per_page": 100}

        res = await self.client.get(endpoint, params=params, token=token)
        if not isinstance(res.data, dict):
            return ()

        raw_runs = res.data.get("check_runs", [])
        if not isinstance(raw_runs, list):
            return ()

        runs: list[CheckRunRef] = []
        for item in raw_runs:
            if isinstance(item, dict):
                runs.append(self.parse_check_run(item))

        return tuple(runs)

    async def list_commit_statuses(
        self,
        repo: RepositoryRef,
        ref: str,
        token: Optional[str] = None,
    ) -> Sequence[CheckRunRef]:
        """Queries legacy commit statuses associated with a commit SHA or branch ref.

        Raises:
            ValidationError: If ref is empty.
            NotFoundError: If repository or ref does not exist.
        """
        clean_ref = ref.strip()
        if not clean_ref:
            raise ValidationError("Target ref or commit SHA cannot be empty.")

        endpoint = f"/repos/{repo.namespace}/{repo.name}/commits/{clean_ref}/statuses"
        params = {"per_page": 100}

        res = await self.client.get(endpoint, params=params, token=token)
        if not isinstance(res.data, list):
            return ()

        # Deduplicate by context name preserving most recent status
        seen_contexts: set[str] = set()
        statuses: list[CheckRunRef] = []

        for item in res.data:
            if isinstance(item, dict):
                context = str(item.get("context", ""))
                if context and context not in seen_contexts:
                    seen_contexts.add(context)
                    statuses.append(self.parse_commit_status(item))

        return tuple(statuses)

    async def get_check_summary(
        self,
        repo: RepositoryRef,
        ref: str,
        token: Optional[str] = None,
    ) -> CheckSummaryRef:
        """Aggregates all Check Runs and Commit Statuses into an authoritative CheckSummaryRef."""
        clean_ref = ref.strip()
        if not clean_ref:
            raise ValidationError("Target ref or commit SHA cannot be empty.")

        # Query both GitHub Actions check-runs and external commit statuses
        check_runs = await self.list_check_runs(repo, clean_ref, token=token)
        commit_statuses = await self.list_commit_statuses(repo, clean_ref, token=token)

        # Merge runs into an aggregate collection
        combined_runs: list[CheckRunRef] = list(check_runs) + list(commit_statuses)

        total_count = len(combined_runs)
        success_count = 0
        failure_count = 0
        pending_count = 0

        for r in combined_runs:
            if r.status != "completed":
                pending_count += 1
            elif r.conclusion in {"success", "neutral", "skipped"}:
                success_count += 1
            else:
                failure_count += 1

        # Derive overall aggregate state
        if total_count == 0:
            overall_state = "neutral"
        elif failure_count > 0:
            overall_state = "failure"
        elif pending_count > 0:
            overall_state = "pending"
        else:
            overall_state = "success"

        return CheckSummaryRef(
            ref=clean_ref,
            total_count=total_count,
            success_count=success_count,
            failure_count=failure_count,
            pending_count=pending_count,
            overall_state=overall_state,
            runs=tuple(combined_runs),
        )
