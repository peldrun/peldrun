from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from ...domain.enums import ProviderId
from ...domain.models import ChangeRequestRef, RepositoryRef
from ...errors import ConflictError, NotFoundError, SourceControlError, ValidationError
from ...git.path_policy import validate_branch_name
from ...security.redaction import redact_text
from .client import GitHubClient


class GitHubPullRequestService:
    """Service coordinating remote GitHub pull request creation, querying, updates, and comments."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client

    @staticmethod
    def parse_change_request_ref(
        data: Mapping[str, Any], repository_id: str
    ) -> ChangeRequestRef:
        """Transforms a raw GitHub pull request API payload into a domain ChangeRequestRef."""
        number = str(data.get("number", ""))
        html_url = str(data.get("html_url", ""))
        raw_state = str(data.get("state", "open")).lower()
        merged_at_raw = data.get("merged_at")
        state = "merged" if merged_at_raw else raw_state

        head_obj = data.get("head")
        base_obj = data.get("base")
        source_branch = head_obj.get("ref", "") if isinstance(head_obj, dict) else ""
        target_branch = base_obj.get("ref", "") if isinstance(base_obj, dict) else ""

        title = str(data.get("title", ""))
        body = str(data.get("body") or "")
        is_draft = bool(data.get("draft", False))

        created_at_raw = data.get("created_at")
        created_at: Optional[datetime] = None
        if created_at_raw and isinstance(created_at_raw, str):
            try:
                created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
            except Exception:
                created_at = None

        metadata = {
            "id": data.get("id"),
            "is_draft": is_draft,
            "body": body,
            "merged_at": merged_at_raw,
            "created_at": created_at_raw,
            "user": data.get("user", {}).get("login") if isinstance(data.get("user"), dict) else None,
        }

        # Dynamically map to ChangeRequestRef fields for forward and backward compatibility
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

    async def list_change_requests(
        self,
        repo: RepositoryRef,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        state: Optional[str] = "open",
        limit: int = 100,
        token: Optional[str] = None,
    ) -> Sequence[ChangeRequestRef]:
        """Lists pull requests for a repository with optional branch and state filters.

        Raises:
            NetworkError: On transport timeouts or network errors.
            SourceControlError: On GitHub REST API failure.
        """
        clamped_limit = max(1, min(limit, 100))
        endpoint = f"/repos/{repo.namespace}/{repo.name}/pulls"

        params: dict[str, Any] = {
            "per_page": clamped_limit,
            "sort": "updated",
            "direction": "desc",
        }

        effective_state = (state or "open").strip().lower()
        if effective_state in {"open", "closed", "all"}:
            params["state"] = effective_state
        else:
            params["state"] = "all"

        if source_branch and source_branch.strip():
            # In GitHub API, head filter can be specified as 'owner:branch' or 'branch'
            clean_head = validate_branch_name(source_branch)
            params["head"] = f"{repo.namespace}:{clean_head}"

        if target_branch and target_branch.strip():
            clean_base = validate_branch_name(target_branch)
            params["base"] = clean_base

        raw_items = await self.client.paginate(
            endpoint,
            params=params,
            token=token,
            max_pages=(clamped_limit + 99) // 100,
        )

        pull_requests: list[ChangeRequestRef] = []
        for item in raw_items:
            if isinstance(item, dict) and "number" in item:
                pull_requests.append(self.parse_change_request_ref(item, repository_id=repo.repository_id))
            if len(pull_requests) >= limit:
                break

        return tuple(pull_requests)

    async def get_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str | int,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        """Retrieves details of a specific pull request by number.

        Raises:
            ValidationError: If pull request number is invalid.
            NotFoundError: If pull request does not exist.
        """
        num_str = str(number_or_iid).strip().lstrip("#")
        if not num_str.isdigit():
            raise ValidationError(f"Invalid pull request number: '{number_or_iid}'.")

        endpoint = f"/repos/{repo.namespace}/{repo.name}/pulls/{num_str}"
        res = await self.client.get(endpoint, token=token)

        if not isinstance(res.data, dict):
            raise NotFoundError(
                f"Pull request #{num_str} not found in repository '{repo.namespace}/{repo.name}'."
            )

        return self.parse_change_request_ref(res.data, repository_id=repo.repository_id)

    async def create_change_request(
        self,
        repo: RepositoryRef,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str = "",
        draft: bool = False,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        """Creates a new pull request on GitHub.

        Raises:
            ValidationError: If branch names or title are invalid.
            ConflictError: If a pull request already exists for the branch or cannot be merged.
            NotFoundError: If the repository or target branch does not exist.
        """
        clean_source = validate_branch_name(source_branch)
        clean_target = validate_branch_name(target_branch)

        clean_title = title.strip()
        if not clean_title:
            raise ValidationError("Pull request title cannot be empty.")

        clean_body = redact_text(body.strip()) if body else ""

        endpoint = f"/repos/{repo.namespace}/{repo.name}/pulls"
        payload = {
            "title": clean_title,
            "body": clean_body,
            "head": clean_source,
            "base": clean_target,
            "draft": draft,
        }

        try:
            res = await self.client.post(endpoint, json_data=payload, token=token)
        except ValidationError as exc:
            lower_msg = str(exc).lower()
            if "already exists" in lower_msg or "pull request already exists" in lower_msg:
                raise ConflictError(
                    f"A pull request already exists for '{clean_source}' into '{clean_target}'.",
                    details={
                        "source_branch": clean_source,
                        "target_branch": clean_target,
                        "repository": f"{repo.namespace}/{repo.name}",
                    },
                ) from exc
            if "no commits between" in lower_msg:
                raise ConflictError(
                    f"Cannot create pull request: no commits between base '{clean_target}' and head '{clean_source}'.",
                    details={"source_branch": clean_source, "target_branch": clean_target},
                ) from exc
            raise

        if not isinstance(res.data, dict):
            raise SourceControlError(
                f"Unexpected response payload creating pull request in '{repo.namespace}/{repo.name}'."
            )

        return self.parse_change_request_ref(res.data, repository_id=repo.repository_id)

    async def update_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str | int,
        *,
        title: Optional[str] = None,
        body: Optional[str] = None,
        state: Optional[str] = None,
        target_branch: Optional[str] = None,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        """Updates pull request metadata or lifecycle state (e.g. open/closed).

        Raises:
            ValidationError: If parameters are malformed.
            NotFoundError: If pull request does not exist.
        """
        num_str = str(number_or_iid).strip().lstrip("#")
        if not num_str.isdigit():
            raise ValidationError(f"Invalid pull request number: '{number_or_iid}'.")

        payload: dict[str, Any] = {}
        if title is not None:
            clean_title = title.strip()
            if not clean_title:
                raise ValidationError("Pull request title cannot be empty.")
            payload["title"] = clean_title

        if body is not None:
            payload["body"] = redact_text(body.strip())

        if state is not None:
            clean_state = state.strip().lower()
            if clean_state not in {"open", "closed"}:
                raise ValidationError("State must be either 'open' or 'closed'.")
            payload["state"] = clean_state

        if target_branch is not None:
            payload["base"] = validate_branch_name(target_branch)

        if not payload:
            return await self.get_change_request(repo, num_str, token=token)

        endpoint = f"/repos/{repo.namespace}/{repo.name}/pulls/{num_str}"
        res = await self.client.patch(endpoint, json_data=payload, token=token)

        if not isinstance(res.data, dict):
            raise SourceControlError(f"Failed to update pull request #{num_str}.")

        return self.parse_change_request_ref(res.data, repository_id=repo.repository_id)

    async def add_comment(
        self,
        repo: RepositoryRef,
        number_or_iid: str | int,
        comment: str,
        token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Adds a comment to an active or closed pull request (via Issues Comment API).

        Raises:
            ValidationError: If comment text is empty.
            NotFoundError: If pull request does not exist.
        """
        num_str = str(number_or_iid).strip().lstrip("#")
        if not num_str.isdigit():
            raise ValidationError(f"Invalid pull request number: '{number_or_iid}'.")

        clean_comment = redact_text(comment.strip())
        if not clean_comment:
            raise ValidationError("Comment body cannot be empty.")

        endpoint = f"/repos/{repo.namespace}/{repo.name}/issues/{num_str}/comments"
        payload = {"body": clean_comment}

        res = await self.client.post(endpoint, json_data=payload, token=token)
        if isinstance(res.data, dict):
            return {
                "id": res.data.get("id"),
                "html_url": res.data.get("html_url"),
                "created_at": res.data.get("created_at"),
            }

        return {}
