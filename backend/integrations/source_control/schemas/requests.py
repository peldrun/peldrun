from __future__ import annotations

from typing import Any, Optional, Sequence
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain.policies import is_protected_branch
from ..git.path_policy import validate_branch_name


class BaseSCRequest(BaseModel):
    """Base schema enforcing strict validation and forbidden extra fields."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
        frozen=True,
    )


class ConnectBeginRequest(BaseSCRequest):
    """Payload to initiate an OAuth 2.0 PKCE authentication handshake with a provider."""

    provider: str = Field(
        default="github",
        description="Target source control provider identifier (e.g. 'github', 'gitlab').",
    )
    redirect_uri: Optional[str] = Field(
        default=None,
        description="Optional explicit redirect URI if different from the default loopback callback.",
    )
    scopes: Optional[Sequence[str]] = Field(
        default=None,
        description="Optional list of specific OAuth scopes requested for this connection.",
    )
    base_url: Optional[str] = Field(
        default=None,
        description="Base instance URL for self-managed enterprise provider installations (e.g. GitLab Self-Managed).",
    )


class ValidateConnectionRequest(BaseSCRequest):
    """Payload to introspect and re-verify an existing provider connection."""

    force_refresh: bool = Field(
        default=False,
        description="Whether to forcefully refresh expired OAuth access tokens via stored refresh tokens.",
    )


class DisconnectRequest(BaseSCRequest):
    """Payload to disconnect a provider and optionally request upstream token revocation."""

    revoke_upstream_token: bool = Field(
        default=True,
        description="Whether to attempt calling provider revocation endpoints prior to deleting local vault credentials.",
    )


class CreateWorkspaceRequest(BaseSCRequest):
    """Payload to initialize an isolated local Git workspace or worktree."""

    repository_id: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Numeric ID or slug ('owner/repo') of the target repository.",
    )
    branch_name: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Branch to checkout or create locally. If omitted, uses default branch.",
    )
    base_sha: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Optional starting commit SHA to anchor the isolated workspace.",
    )
    provider: str = Field(
        default="github",
        description="Target source control provider identifier.",
    )

    @field_validator("branch_name")
    @classmethod
    def validate_branch(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip():
            return validate_branch_name(v)
        return None


class RunChecksRequest(BaseSCRequest):
    """Payload to trigger local verification checks or query remote commit statuses."""

    workspace_id: Optional[str] = Field(
        default=None,
        description="Local workspace ID to inspect.",
    )
    ref: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Remote git revision or commit SHA to inspect check-runs for.",
    )
    provider: str = Field(
        default="github",
        description="Target source control provider identifier.",
    )


class CreateBranchRequest(BaseSCRequest):
    """Payload to create a new branch directly on the remote repository."""

    branch_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Name of the new branch conforming to git ref syntax.",
    )
    from_sha: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Base commit SHA. If omitted, uses default branch HEAD.",
    )

    @field_validator("branch_name")
    @classmethod
    def validate_branch(cls, v: str) -> str:
        clean = validate_branch_name(v)
        if is_protected_branch(clean):
            raise ValueError(f"Branch name '{clean}' is protected and cannot be created directly.")
        return clean


class CreateOperationRequest(BaseSCRequest):
    """Payload to register a new durable source control workflow operation."""

    operation_type: str = Field(
        ...,
        description="Type of operation (e.g. 'push_branch', 'create_change_request').",
    )
    repository_id: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Target repository identifier (numeric ID or slug).",
    )
    target_branch: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Destination branch for the proposed changes.",
    )
    workspace_id: Optional[str] = Field(
        default=None,
        description="Optional isolated local workspace ID bound to this operation.",
    )
    title: Optional[str] = Field(
        default=None,
        max_length=300,
        description="Proposed title for change requests or commit messages.",
    )
    description: Optional[str] = Field(
        default=None,
        max_length=10000,
        description="Proposed description or rationale for changes.",
    )
    provider: str = Field(
        default="github",
        description="Target source control provider identifier.",
    )

    @field_validator("operation_type")
    @classmethod
    def validate_op_type(cls, v: str) -> str:
        clean = v.strip().lower()
        allowed = {"push_branch", "create_change_request", "merge_change_request"}
        if clean not in allowed:
            raise ValueError(f"Invalid operation_type '{v}'. Must be one of: {sorted(allowed)}")
        return clean

    @field_validator("target_branch")
    @classmethod
    def validate_target(cls, v: str) -> str:
        return validate_branch_name(v)


class ApproveOperationRequest(BaseSCRequest):
    """Payload submitted by user to explicitly approve a frozen approval ticket."""

    approval_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Cryptographic approval ticket ID generated by RequestApprovalTool.",
    )
    user_id: Optional[str] = Field(
        default="user",
        description="Identifier of the human user granting approval for audit logging.",
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional review notes or commentary attached to this approval.",
    )


class RejectOperationRequest(BaseSCRequest):
    """Payload submitted by user to reject a proposed operation."""

    approval_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Cryptographic approval ticket ID to reject and invalidate.",
    )
    user_id: Optional[str] = Field(
        default="user",
        description="Identifier of the human user rejecting the proposal.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Explicit mandatory explanation for rejecting the proposed changes.",
    )


class PushBranchRequest(BaseSCRequest):
    """Payload to trigger an authenticated push of an approved branch."""

    approval_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Valid single-use approval ticket ID granting permission to push.",
    )
    workspace_id: str = Field(
        ...,
        min_length=1,
        description="Workspace containing the commits to be pushed.",
    )
    remote_name: str = Field(
        default="origin",
        max_length=50,
        description="Remote name in local git repository.",
    )
    set_upstream: bool = Field(
        default=True,
        description="Whether to configure upstream tracking reference.",
    )


class CreateChangeRequestPayload(BaseSCRequest):
    """Payload to open a new remote pull or merge request."""

    repository_id: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Target repository identifier.",
    )
    source_branch: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Head branch containing the changes.",
    )
    target_branch: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Base branch to merge changes into.",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="Title for the change request.",
    )
    body: Optional[str] = Field(
        default="",
        max_length=20000,
        description="Markdown body describing the change request.",
    )
    draft: bool = Field(
        default=True,
        description="Whether to open the change request as a draft.",
    )
    approval_id: Optional[str] = Field(
        default=None,
        description="Approval ticket ID if policy mandates approval for change request creation.",
    )
    provider: str = Field(
        default="github",
        description="Target source control provider identifier.",
    )

    @field_validator("source_branch", "target_branch")
    @classmethod
    def validate_branches(cls, v: str) -> str:
        return validate_branch_name(v)
