# backend/omweb/services/source_control_facade.py
"""
Application facade for the Source Control Integration subsystem.

The facade is the single coordination point between the HTTP router,
the provider adapters, the local Git engine, the approval gate, the
credential vault, and the durable operation journal. It does not
implement policy, credentials, or Git; it composes those services.

Design rules:
    - This module MUST NOT contain business logic. It orchestrates.
    - Every remote side effect MUST be gated by:
        1. A valid token read from the credential vault via the
           TokenService.
        2. A policy check via the PolicyEngine.
        3. An approval gate consumption for destructive operations.
    - The facade MUST NOT log or return raw credentials. All tokens
      flow through the TokenService.
    - The connection registry (`_connections`) is facade-private
      metadata. It records which account is connected to which
      provider so that the TokenService can be addressed with the
      correct `(provider, account_id, base_url)` triple. Tokens
      themselves live only in the vault.

REPOSITORY ID RESOLUTION:
    The provider adapters (`GitHubProviderAdapter`, and future
    GitLab/Bitbucket adapters) operate on `RepositoryRef` objects,
    while the HTTP layer identifies repositories by their opaque
    string `repository_id`. The `_resolve_repo` helper added below
    converts the string to a `RepositoryRef` by asking the adapter to
    refresh a placeholder and, if that fails, by listing the
    caller's repositories and matching by `repository_id`. Every
    method that touches a provider adapter uses this helper instead
    of passing the raw string to `adapter.get_repository(...)`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

# Resilient imports for source control integration components
try:
    from integrations.source_control import (
        ApprovalGate,
        ApprovalTicket,
        AuditLogger,
        BranchRef,
        ChangeRequestRef,
        CheckSummaryRef,
        CredentialVault,
        DiffSummary,
        FileDiff,
        GitDiffEngine,
        GitPushManager,
        GitWorkspaceManager,
        OperationRecord,
        OperationService,
        OperationStatus,
        PolicyEngine,
        ProviderAccount,
        ProviderCapabilities,
        ProviderId,
        ProviderRegistry,
        PushResult,
        RepositoryRef,
        SourceControlError,
        TokenService,
        get_default_approval_gate,
        get_default_audit_logger,
        get_default_policy_engine,
        get_default_registry,
        get_default_token_service,
        get_default_vault,
        is_protected_branch,
        is_workflow_path,
        redact_text,
        redact_url,
        register_builtin_providers,
        validate_branch_name,
    )
    from integrations.source_control.errors import (
        AuthRequiredError,
        NotFoundError,
        PermissionDeniedError,
        RemoteStateChangedError,
        ValidationError,
    )
    from integrations.source_control.security.audit import (
        AuditEventType,
        AuditSeverity,
    )
    from integrations.source_control.security.oauth_flow import (
        OAuthFlowCoordinator,
        OAuthProviderCallbacks,
    )
    from integrations.source_control.security.policy_engine import (
        PolicyResolutionContext,
    )
    from integrations.source_control.schemas import (
        ActionSuccessResponse,
        ApprovalTicketResponse,
        ApproveOperationRequest,
        AuditLogEntryResponse,
        BranchResponse,
        ChangeRequestResponse,
        CheckRunResponse,
        CheckSummaryResponse,
        ConnectBeginRequest,
        ConnectBeginResponse,
        ConnectionStatusResponse,
        CreateBranchRequest,
        CreateChangeRequestPayload,
        CreateOperationRequest,
        CreateWorkspaceRequest,
        DiffSummaryResponse,
        DisconnectRequest,
        FileDiffResponse,
        FileReadResponse,
        OperationEventResponse,
        OperationResponse,
        ProviderCapabilitiesResponse,
        ProviderDescriptorResponse,
        PushBranchRequest,
        PushBranchResponse,
        RejectOperationRequest,
        RepositoryResponse,
        RunChecksRequest,
        SaveProviderConfigRequest,
        ValidateConnectionRequest,
        WorkspaceResponse,
    )
except ImportError:
    # Fallback for the case where the package is imported via the
    # absolute `backend.integrations...` path instead of the relative
    # one. `ValueError` was removed from this clause: `import`
    # statements do not raise it, so catching it only masked genuine
    # programming errors.
    from integrations.source_control import (
        ApprovalGate,
        ApprovalTicket,
        AuditLogger,
        BranchRef,
        ChangeRequestRef,
        CheckSummaryRef,
        CredentialVault,
        DiffSummary,
        FileDiff,
        GitDiffEngine,
        GitPushManager,
        GitWorkspaceManager,
        OperationRecord,
        OperationService,
        OperationStatus,
        PolicyEngine,
        ProviderAccount,
        ProviderCapabilities,
        ProviderId,
        ProviderRegistry,
        PushResult,
        RepositoryRef,
        SourceControlError,
        TokenService,
        get_default_approval_gate,
        get_default_audit_logger,
        get_default_policy_engine,
        get_default_registry,
        get_default_token_service,
        get_default_vault,
        is_protected_branch,
        is_workflow_path,
        redact_text,
        redact_url,
        register_builtin_providers,
        validate_branch_name,
    )
    from integrations.source_control.errors import (
        AuthRequiredError,
        NotFoundError,
        PermissionDeniedError,
        RemoteStateChangedError,
        UnsupportedCapabilityError,
        ValidationError,
    )
    from integrations.source_control.providers.github.pull_requests import (
        GitHubPullRequestService,
    )
    from integrations.source_control.providers.github.reviews import (
        GitHubReviewsService,
    )
    from integrations.source_control.security.audit import (
        AuditEventType,
        AuditSeverity,
    )
    from integrations.source_control.security.oauth_flow import (
        OAuthFlowCoordinator,
        OAuthProviderCallbacks,
    )
    from integrations.source_control.security.policy_engine import (
        PolicyResolutionContext,
    )
    from integrations.source_control.schemas import (
        ActionSuccessResponse,
        ApprovalTicketResponse,
        ApproveOperationRequest,
        AuditLogEntryResponse,
        BranchResponse,
        ChangeRequestResponse,
        CheckRunResponse,
        CheckSummaryResponse,
        ConnectBeginRequest,
        ConnectBeginResponse,
        ConnectionStatusResponse,
        CreateBranchRequest,
        CreateChangeRequestPayload,
        CreateOperationRequest,
        CreateWorkspaceRequest,
        DiffSummaryResponse,
        DisconnectRequest,
        FileDiffResponse,
        FileReadResponse,
        OperationEventResponse,
        OperationResponse,
        ProviderCapabilitiesResponse,
        ProviderDescriptorResponse,
        PushBranchRequest,
        PushBranchResponse,
        RejectOperationRequest,
        RepositoryResponse,
        RunChecksRequest,
        ValidateConnectionRequest,
        WorkspaceResponse,
    )


logger = logging.getLogger(__name__)


#: Default OAuth redirect URI. Used only when the caller does not
#: supply one. Matches the loopback-only policy enforced by the OAuth
#: coordinator.
_DEFAULT_OAUTH_REDIRECT = "http://127.0.0.1:8765/oauth/callback"

#: Name of the facade-private connection index file. This file
#: contains only non-secret metadata (provider, account ID, base URL,
#: username); tokens live in the OS credential vault.
_CONNECTIONS_FILE_NAME = "connections.json"

#: Name of the file that stores the user-provided OAuth Client IDs.
#: Client Secrets are NEVER stored here; they go to the OS Vault.
_PROVIDER_CONFIG_FILE_NAME = "provider_configs.json"

#: Maximum number of bytes returned to the UI when reading a file.
#: This is a byte budget, NOT a character budget: multi-byte UTF-8
#: sequences must be counted correctly to honor the caller's intent.
_MAX_FILE_CONTENT_BYTES = 100_000

#: Maximum number of diff sample lines returned to the UI.
_MAX_DIFF_SAMPLE_LINES = 500


#: Providers whose pull-request operations are currently wired to
#: the facade. GitLab and Bitbucket adapters exist for read/branch/
#: push operations but do not expose the merge/review endpoints
#: through the facade yet. Adding a provider here requires (a) a
#: `providers/<name>/pull_requests.py` and `reviews.py` with the
#: same public surface, and (b) updating `_get_github_pr_service`
#: / `_get_github_reviews_service` to dispatch on the provider.
_PR_CAPABLE_PROVIDERS: frozenset[ProviderId] = frozenset({ProviderId.GITHUB})


# Git history reader + branch manager + the response schemas they
# need. Imported defensively because the Git history feature is
# rolled out incrementally: older deployments may not yet have the
# modules below, and a hard top-level import would prevent the
# facade from loading at all. The fallback path mirrors the
# `backend.integrations.*` prefix used by other modules in the
# subsystem.
try:
    from integrations.source_control.git.log import (
        GitLogReader,
        get_default_log_reader,
    )
    from integrations.source_control.git.branch import (
        GitBranchManager,
        get_default_branch_manager,
    )
    from integrations.source_control.schemas.requests import (
        CheckoutBranchRequest,
    )
    from integrations.source_control.schemas.responses import (
        CommitEntryResponse,
        CommitWithStatsResponse,
        FileContentAtRefResponse,
        TreeNodeResponse,
    )
except ImportError:
    from backend.integrations.source_control.git.log import (  # type: ignore[no-redef]
        GitLogReader,
        get_default_log_reader,
    )
    from backend.integrations.source_control.git.branch import (  # type: ignore[no-redef]
        GitBranchManager,
        get_default_branch_manager,
    )
    from backend.integrations.source_control.schemas.requests import (  # type: ignore[no-redef]
        CheckoutBranchRequest,
    )
    from backend.integrations.source_control.schemas.responses import (  # type: ignore[no-redef]
        CommitEntryResponse,
        CommitWithStatsResponse,
        FileContentAtRefResponse,
        TreeNodeResponse,
    )


# Pull request schemas and services — Phase 2 additions.
#
# Why a separate try/except block and not a modification of the
# existing imports above:
#
#   The main imports at the top of the module have two paths (a
#   relative `from integrations...` try and an absolute
#   `from backend.integrations...` fallback). Depending on how the
#   module is imported, EITHER path may succeed. If we added the
#   new PR imports into only one of them, the other path would
#   silently shadow them and produce `NameError: GitHubPullRequestService`
#   at the first PR call. By adding a self-contained try/except
#   block that is independent of the earlier ones, the new names
#   are guaranteed to resolve on every code path — regardless of
#   which of the earlier blocks succeeded.
try:
    from integrations.source_control.domain.enums import (
        ChangeRequestState,
    )
    from integrations.source_control.errors import (
        UnsupportedCapabilityError,
    )
    from integrations.source_control.providers.github.pull_requests import (
        GitHubPullRequestService,
    )
    from integrations.source_control.providers.github.reviews import (
        GitHubReviewsService,
    )
    from integrations.source_control.schemas import (
        CreatePullRequestCommentRequest,
        CreatePullRequestRequest,
        MergePullRequestRequest,
        MergePullRequestResponse,
        PullRequestCommentResponse,
        PullRequestListResponse,
        PullRequestResponse,
        PullRequestReviewResponse,
        SubmitPullRequestReviewRequest,
    )
except ImportError:
    from backend.integrations.source_control.domain.enums import (  # type: ignore[no-redef]
        ChangeRequestState,
    )
    from backend.integrations.source_control.errors import (  # type: ignore[no-redef]
        UnsupportedCapabilityError,
    )
    from backend.integrations.source_control.providers.github.pull_requests import (  # type: ignore[no-redef]
        GitHubPullRequestService,
    )
    from backend.integrations.source_control.providers.github.reviews import (  # type: ignore[no-redef]
        GitHubReviewsService,
    )
    from backend.integrations.source_control.schemas import (  # type: ignore[no-redef]
        CreatePullRequestCommentRequest,
        CreatePullRequestRequest,
        MergePullRequestRequest,
        MergePullRequestResponse,
        PullRequestCommentResponse,
        PullRequestListResponse,
        PullRequestResponse,
        PullRequestReviewResponse,
        SubmitPullRequestReviewRequest,
    )


# Issue schemas and service — Phase 3 additions. Self-contained
# try/except for the same reason the PR block above is: the module
# has two import paths, and adding the names to only one would
# break the other with NameError at the first issue call.
try:
    from integrations.source_control.providers.github.issues import (
        GitHubIssueService,
    )
    from integrations.source_control.schemas import (
        CreateIssueCommentRequest,
        CreateIssueRequest,
        IssueCommentResponse,
        IssueListResponse,
        IssueResponse,
        UpdateIssueStateRequest,
    )
except ImportError:
    from backend.integrations.source_control.providers.github.issues import (  # type: ignore[no-redef]
        GitHubIssueService,
    )
    from backend.integrations.source_control.schemas import (  # type: ignore[no-redef]
        CreateIssueCommentRequest,
        CreateIssueRequest,
        IssueCommentResponse,
        IssueListResponse,
        IssueResponse,
        UpdateIssueStateRequest,
    )


# Merge / rebase / conflict-resolution services and schemas — Phase 4
# additions. Self-contained try/except for the same reason the earlier
# blocks are: the module has two import paths, and adding the names to
# only one would break the other with NameError at the first call.
try:
    from integrations.source_control.git.merge import (
        GitMergeManager,
        MergeOutcome,
        get_default_merge_manager,
    )
    from integrations.source_control.git.conflict import (
        ConflictEntry,
        GitConflictManager,
        OperationState,
        get_default_conflict_manager,
    )
    from integrations.source_control.schemas import (
        ConflictFileResponse,
        ConflictListResponse,
        MergeBranchesRequest,
        MergeOperationResponse,
        RebaseBranchRequest,
        ResolveConflictsRequest,
        ResolveConflictsResponse,
    )
except ImportError:
    from backend.integrations.source_control.git.merge import (  # type: ignore[no-redef]
        GitMergeManager,
        MergeOutcome,
        get_default_merge_manager,
    )
    from backend.integrations.source_control.git.conflict import (  # type: ignore[no-redef]
        ConflictEntry,
        GitConflictManager,
        OperationState,
        get_default_conflict_manager,
    )
    from backend.integrations.source_control.schemas import (  # type: ignore[no-redef]
        ConflictFileResponse,
        ConflictListResponse,
        MergeBranchesRequest,
        MergeOperationResponse,
        RebaseBranchRequest,
        ResolveConflictsRequest,
        ResolveConflictsResponse,
    )


# Tag manager, release service, and their schemas — Phase 5 additions.
# Self-contained try/except for the same reason all the earlier blocks
# are: the module has two import paths, and adding the names to only
# one would break the other with NameError at the first tag call.
try:
    from integrations.source_control.git.tag import (
        GitTagManager,
        TagRef,
        get_default_tag_manager,
    )
    from integrations.source_control.providers.github.releases import (
        GitHubReleaseService,
        ReleaseRef,
    )
    from integrations.source_control.schemas import (
        CreateReleaseRequest,
        CreateTagRequest,
        ReleaseListResponse,
        ReleaseResponse,
        SourceControlSettingsResponse,
        TagListResponse,
        TagResponse,
        UpdateSourceControlSettingsRequest,
    )
except ImportError:
    from backend.integrations.source_control.git.tag import (  # type: ignore[no-redef]
        GitTagManager,
        TagRef,
        get_default_tag_manager,
    )
    from backend.integrations.source_control.providers.github.releases import (  # type: ignore[no-redef]
        GitHubReleaseService,
        ReleaseRef,
    )
    from backend.integrations.source_control.schemas import (  # type: ignore[no-redef]
        CreateReleaseRequest,
        CreateTagRequest,
        ReleaseListResponse,
        ReleaseResponse,
        SourceControlSettingsResponse,
        TagListResponse,
        TagResponse,
        UpdateSourceControlSettingsRequest,
    )


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _truncate_to_byte_budget(
    text: str, max_bytes: int
) -> tuple[str, bool]:
    """
    Truncate ``text`` so that its UTF-8 encoding fits within
    ``max_bytes`` bytes.

    The naive ``text[:N]`` slice counts characters, not bytes, and
    therefore lets a multi-byte string exceed the intended budget by
    up to a factor of four (the maximum length of a UTF-8 sequence).
    This helper computes the correct character boundary by encoding
    to UTF-8, slicing on the byte boundary, and then decoding back.

    A suffix is appended when truncation occurs so that the caller
    can distinguish a truncated read from a complete one.

    Returns:
        ``(possibly_truncated_text, was_truncated)``.
    """
    suffix = "\n\n... [Truncated for context size] ..."
    suffix_bytes = len(suffix.encode("utf-8"))

    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False

    budget = max(0, max_bytes - suffix_bytes)
    # Slice on the byte boundary. ``decode(..., errors="ignore")`` is
    # safe here because we are intentionally truncating; we do not
    # want to keep a partial multi-byte character.
    truncated = raw[:budget].decode("utf-8", errors="ignore")
    return truncated + suffix, True


@dataclass(frozen=True)
class _ConnectionRecord:
    """
    Facade-private metadata for a provider connection.

    The record carries only identifiers and timestamps. It never
    contains tokens, refresh tokens, or any other secret. Tokens are
    stored in the OS credential vault and addressed via
    `(provider, account_id, base_url)`.
    """

    provider: str
    account_id: str
    base_url: str
    username: str
    connected_at: datetime
    last_validated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "account_id": self.account_id,
            "base_url": self.base_url,
            "username": self.username,
            "connected_at": self.connected_at.isoformat(),
            "last_validated_at": (
                self.last_validated_at.isoformat()
                if self.last_validated_at
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "_ConnectionRecord":
        return cls(
            provider=str(data["provider"]),
            account_id=str(data["account_id"]),
            base_url=str(data["base_url"]),
            username=str(data.get("username") or ""),
            connected_at=datetime.fromisoformat(str(data["connected_at"])),
            last_validated_at=(
                datetime.fromisoformat(str(data["last_validated_at"]))
                if data.get("last_validated_at")
                else None
            ),
        )


class SourceControlFacade:
    """Orchestration facade coordinating providers, git workspaces, approvals, and operations."""

    def __init__(
        self,
        registry: Optional[ProviderRegistry] = None,
        token_service: Optional[TokenService] = None,
        vault: Optional[CredentialVault] = None,
        policy_engine: Optional[PolicyEngine] = None,
        approval_gate: Optional[ApprovalGate] = None,
        operation_service: Optional[OperationService] = None,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        diff_engine: Optional[GitDiffEngine] = None,
        push_manager: Optional[GitPushManager] = None,
        audit_logger: Optional[AuditLogger] = None,
        oauth_coordinator: Optional[OAuthFlowCoordinator] = None,
        base_workspace_dir: Optional[str | Path] = None,
        log_reader: Optional[GitLogReader] = None,
        branch_manager: Optional[GitBranchManager] = None,
        pr_service: Optional[GitHubPullRequestService] = None,
        reviews_service: Optional[GitHubReviewsService] = None,
        merge_manager: Optional[GitMergeManager] = None,
        conflict_manager: Optional[GitConflictManager] = None,
        tag_manager: Optional[GitTagManager] = None,
        release_service: Optional[GitHubReleaseService] = None,
    ) -> None:
        # Provider registry
        self.registry = (
            registry if registry is not None else get_default_registry()
        )
        register_builtin_providers(registry=self.registry)

        self.vault = vault if vault is not None else get_default_vault()
        self.token_service = (
            token_service
            if token_service is not None
            else get_default_token_service()
        )
        self.policy_engine = (
            policy_engine
            if policy_engine is not None
            else get_default_policy_engine()
        )
        self.approval_gate = (
            approval_gate
            if approval_gate is not None
            else get_default_approval_gate()
        )
        self.audit_logger = (
            audit_logger
            if audit_logger is not None
            else get_default_audit_logger()
        )

        # Centralized data directory for durable configs (keys, connections)
        # This dynamically resolves the root `backend` directory, avoiding `backend/backend`.
        # __file__ is `backend/omweb/services/source_control_facade.py`.
        backend_root = Path(__file__).resolve().parent.parent.parent
        self._data_root = backend_root / "data" / "source_control"
        self._data_root.mkdir(parents=True, exist_ok=True)

        # Operation service (deferred import to avoid circular import)
        if operation_service is not None:
            self.operation_service = operation_service
        else:
            from integrations.source_control.operations.journal import (
                FileOperationJournal,
                get_default_operation_journal,
                set_default_operation_journal,
            )
            from integrations.source_control.operations.service import (
                get_default_operation_service,
            )
            journal_path = self._data_root / "journal"
            journal_path.mkdir(parents=True, exist_ok=True)
            journal = get_default_operation_journal()
            if not isinstance(journal, FileOperationJournal):
                journal = FileOperationJournal(root=journal_path)
                set_default_operation_journal(journal)
            self.operation_service = get_default_operation_service()

        # Workspace root and path policy
        resolved_base = (
            Path(base_workspace_dir or os.getcwd()).resolve()
            / "workspaces"
            / "source_control"
        )
        resolved_base.mkdir(parents=True, exist_ok=True)
        self._workspace_root = resolved_base

        from integrations.source_control.git.path_policy import (
            WorkspacePathPolicy,
        )
        self._path_policy = WorkspacePathPolicy(root=resolved_base)

        # Git services MUST use the process-wide default managers
        # when no explicit dependency was injected. Otherwise the
        # facade and the agent tools each hold their own registry,
        # so a workspace created by the agent is invisible to the
        # facade (and vice versa). The symptom is a "workspace not
        # found" error on the HTTP endpoints even though the
        # operation exists and the workspace is on disk. The
        # `path_policy` argument on the individual managers is
        # ignored when the defaults are used — they already carry
        # the correct policy (including extra_roots for chat-scoped
        # workspaces).
        if workspace_manager is not None:
            self.workspace_manager = workspace_manager
        else:
            from integrations.source_control.git.workspace import (
                get_default_workspace_manager,
            )
            self.workspace_manager = get_default_workspace_manager()

        if diff_engine is not None:
            self.diff_engine = diff_engine
        else:
            from integrations.source_control.git.diff import (
                get_default_diff_engine,
            )
            self.diff_engine = get_default_diff_engine()

        if push_manager is not None:
            self.push_manager = push_manager
        else:
            from integrations.source_control.git.push import (
                get_default_push_manager,
            )
            self.push_manager = get_default_push_manager()

        # Git history reader. Used by the read-only browser endpoints
        # (commit history, file tree, historical file content). As with
        # the workspace / diff / push managers above, the process-wide
        # default is used when no explicit dependency was injected, so
        # that the facade sees the same workspaces the agent created.
        if log_reader is not None:
            self.log_reader = log_reader
        else:
            self.log_reader = get_default_log_reader()

        # Local branch manager. Used by the checkout endpoint to switch
        # the workspace between existing branches. This is a local-only
        # operation and does not touch the remote.
        if branch_manager is not None:
            self.branch_manager = branch_manager
        else:
            self.branch_manager = get_default_branch_manager()

        # Pull request services. Both are GitHub-only in this phase
        # and are instantiated lazily by `_get_github_pr_service` /
        # `_get_github_reviews_service` on first use, so a deployment
        # that never touches pull requests pays no construction cost.
        # Explicit injection is supported for tests and for custom
        # Enterprise-instance deployments.
        self._pr_service: Optional[GitHubPullRequestService] = pr_service
        self._reviews_service: Optional[GitHubReviewsService] = reviews_service
        self._issue_service: Optional[GitHubIssueService] = None

        # Merge / rebase / conflict-resolution managers. Both are
        # local-only (no remote calls) and use the process-wide
        # defaults so they see the same workspaces the workspace
        # manager and the log reader see.
        self.merge_manager: GitMergeManager = (
            merge_manager
            if merge_manager is not None
            else get_default_merge_manager()
        )
        self.conflict_manager: GitConflictManager = (
            conflict_manager
            if conflict_manager is not None
            else get_default_conflict_manager()
        )

        # Tag manager (local) and release service (GitHub-only).
        # Same lazy-init strategy as the PR / issue services: the
        # release service is only constructed on first use, so a
        # deployment that never touches releases pays no cost.
        self.tag_manager: GitTagManager = (
            tag_manager
            if tag_manager is not None
            else get_default_tag_manager()
        )
        self._release_service: Optional[GitHubReleaseService] = (
            release_service
        )

        # OAuth
        self.oauth_coordinator = oauth_coordinator or OAuthFlowCoordinator()

        # Register OAuth providers for all available adapters
        self._register_default_oauth_providers()

        # Facade-private connection registry. This is NOT a secret
        # store: only identifiers and timestamps are kept here.
        self._connections: Dict[str, _ConnectionRecord] = {}
        self._load_connections()

    def _register_default_oauth_providers(self) -> None:
        """Register OAuth callbacks for built-in providers."""
        try:
            from integrations.source_control.security.oauth_flow import (
                OAuthProviderCallbacks,
            )
            from integrations.source_control.security.token_service import (
                TokenRefreshResult,
            )
        except ImportError:
            from integrations.source_control.security.oauth_flow import (
                OAuthProviderCallbacks,
            )
            from integrations.source_control.security.token_service import (
                TokenRefreshResult,
            )

        # Register GitHub
        try:
            adapter = self.registry.get("github")
            auth_service = adapter._get_auth_service()

            def build_github_url(
                provider: Any,
                client_id: str,
                redirect_uri: str,
                state: str,
                code_challenge: str,
                scopes: Optional[Sequence[str]],
            ) -> str:
                return auth_service.build_authorization_url(
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    state=state,
                    scopes=scopes,
                    code_challenge=code_challenge,
                )

            async def exchange_github_code(
                provider: Any,
                code: str,
                code_verifier: str,
                redirect_uri: str,
                client_id: str,
                base_url: Optional[str],
                client_secret: Optional[str],
            ) -> Any:
                res = await auth_service.exchange_code_for_token(
                    client_id=client_id,
                    code=code,
                    redirect_uri=redirect_uri,
                    client_secret=client_secret,
                    code_verifier=code_verifier,
                )
                from datetime import datetime, timedelta, timezone
                expires_at = None
                if res.expires_in:
                    expires_at = datetime.now(timezone.utc) + timedelta(seconds=res.expires_in)

                return TokenRefreshResult(
                    access_token=res.access_token,
                    refresh_token=res.refresh_token,
                    expires_at=expires_at,
                    scopes=res.granted_scopes,
                )

            async def resolve_github_account(
                provider: Any,
                token_result: Any,
                base_url: Optional[str],
            ) -> Any:
                # Fetch real user data from GitHub
                account = await auth_service.get_authenticated_account(token_result.access_token)
                return account

            callbacks = OAuthProviderCallbacks(
                client_id="",
                authorization_url_builder=build_github_url,
                token_exchanger=exchange_github_code,
            )
            self.register_oauth_provider("github", callbacks, account_resolver=resolve_github_account)
        except Exception as exc:
            logger.warning("Failed to register GitHub OAuth callbacks: %s", exc)

    # ------------------------------------------------------------------
    # 1. Connection registry helpers
    # ------------------------------------------------------------------

    def _connections_path(self) -> Path:
        return self._data_root / _CONNECTIONS_FILE_NAME

    def _load_connections(self) -> None:
        """Load the persisted connection index, if present and readable."""
        path = self._connections_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to load connections index: %s",
                type(exc).__name__,
            )
            return
        if not isinstance(data, dict):
            return
        for item in data.get("connections", []):
            if not isinstance(item, dict):
                continue
            try:
                record = _ConnectionRecord.from_dict(item)
            except (KeyError, ValueError):
                continue
            self._connections[record.provider] = record

    def _save_connections(self) -> None:
        """Persist the connection index atomically."""
        path = self._connections_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "connections": [
                record.to_dict() for record in self._connections.values()
            ],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _get_connection(self, provider: ProviderId | str) -> Optional[_ConnectionRecord]:
        key = provider.value if isinstance(provider, ProviderId) else str(provider)
        return self._connections.get(key)

    def _set_connection(self, record: _ConnectionRecord) -> None:
        self._connections[record.provider] = record
        self._save_connections()

    def _remove_connection(self, provider: ProviderId | str) -> Optional[_ConnectionRecord]:
        key = provider.value if isinstance(provider, ProviderId) else str(provider)
        record = self._connections.pop(key, None)
        if record is not None:
            self._save_connections()
        return record

    def _provider_config_path(self) -> Path:
        return self._data_root / _PROVIDER_CONFIG_FILE_NAME

    def _load_provider_configs(self) -> Dict[str, str]:
        """Load the provider configs (Client IDs) from disk."""
        path = self._provider_config_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load provider configs: %s", type(exc).__name__)
            return {}

    def _save_provider_configs(self, configs: Dict[str, str]) -> None:
        """Save the provider configs (Client IDs) to disk atomically."""
        path = self._provider_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(configs, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _is_provider_configured(self, provider_id: str) -> bool:
        """Check if both Client ID (JSON) and Client Secret (Vault) exist."""
        configs = self._load_provider_configs()
        client_id = configs.get(provider_id)
        if not client_id:
            return False

        secret_key = f"peldrun.sc.oauth_secret_{provider_id}"
        secret = self.vault.get(secret_key)
        return bool(secret)

    async def _load_branch_policy_from_settings(self) -> Any:
        """
        Build a `BranchPolicy` that respects the user's settings.

        The user's `protected_branches` and `user_override_protected`
        (from `settings.json`, Task 1.1) are merged with the historical
        `DEFAULT_PROTECTED_BRANCH_PATTERNS` from `domain.policies` so
        that an upgrade from a pre-Task-1.1 version does not silently
        drop protection for patterns the user previously relied on
        (e.g. `release/*`, `develop`). The user's override list is
        applied on top and wins over both the user's patterns and the
        historical defaults.

        Returns a `BranchPolicy` whose `is_protected()` method
        implements the effective policy. Any failure to read settings
        falls back to the historical defaults so that a broken
        settings file never blocks a push.
        """
        try:
            from integrations.source_control.domain.policies import (
                BranchPolicy,
                DEFAULT_PROTECTED_BRANCH_PATTERNS,
            )
        except ImportError:  # absolute-import layout
            from backend.integrations.source_control.domain.policies import (  # type: ignore[no-redef]
                BranchPolicy,
                DEFAULT_PROTECTED_BRANCH_PATTERNS,
            )

        try:
            settings = await self.read_settings()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_load_branch_policy_from_settings: read_settings "
                "failed (%s); falling back to historical defaults.",
                type(exc).__name__,
            )
            return BranchPolicy()

        effective_patterns = (
            tuple(settings.protected_branches)
            + tuple(DEFAULT_PROTECTED_BRANCH_PATTERNS)
        )
        return BranchPolicy(
            protected_patterns=effective_patterns,
            user_override_protected=tuple(settings.user_override_protected),
        )

    async def _check_remote_protection(
        self,
        *,
        adapter: Any,
        repository_id: str,
        provider_id: ProviderId,
        branch_name: str,
        token: str,
    ) -> Optional[bool]:
        """
        Check whether a branch is protected on the remote provider.

        Returns a tri-state answer:

            ``True``  — the provider reports the branch as protected.
            ``False`` — the provider reports the branch as NOT
                        protected (GitHub returned 404 for its
                        protection endpoint).
            ``None``  — the check could not be performed (network
                        error, insufficient permissions, unknown
                        branch, or unsupported provider).

        The tri-state is critical: a silent ``False`` on failure would
        let a push to a genuinely protected branch proceed whenever
        the provider API is momentarily unavailable.

        Prefers the adapter's `is_branch_protected_remotely` method
        (added in Task 1.2). Falls back to `get_branch_protection`
        (which returns the raw protection payload or `None`) for
        adapters that expose only the latter.
        """
        tri_check = getattr(adapter, "is_branch_protected_remotely", None)
        get_protection = getattr(adapter, "get_branch_protection", None)
        if not callable(tri_check) and not callable(get_protection):
            return None

        try:
            repo = await self._resolve_repo(
                adapter, repository_id, provider_id, token
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_check_remote_protection: could not resolve "
                "repository '%s': %s",
                repository_id,
                type(exc).__name__,
            )
            return None

        if callable(tri_check):
            try:
                result = await tri_check(
                    repo=repo, branch_name=branch_name, token=token
                )
                if result is True or result is False:
                    return result
                return None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_check_remote_protection: tri-state check failed "
                    "for '%s': %s",
                    branch_name,
                    type(exc).__name__,
                )
                return None

        if callable(get_protection):
            try:
                payload = await get_protection(
                    repo=repo, branch_name=branch_name, token=token
                )
                return payload is not None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_check_remote_protection: fallback check failed "
                    "for '%s': %s",
                    branch_name,
                    type(exc).__name__,
                )
                return None

        return None

    def _default_provider_id(self) -> ProviderId:
        """
        Return the provider to use when the request does not specify one.

        Resolution order:
            1. ``github`` when a GitHub connection exists.
            2. The first connected provider that maps to a known
               ``ProviderId``.
            3. ``ProviderId.GITHUB`` as a safe fallback.

        When a connected provider key does not correspond to any
        ``ProviderId`` member (for example a provider that was
        removed from the enum), the key is skipped and a warning is
        logged. The previous implementation silently swallowed the
        ``ValueError``, which made stale connection records invisible
        to operators.
        """
        if "github" in self._connections:
            return ProviderId.GITHUB
        for key in self._connections.keys():
            try:
                return ProviderId(key)
            except ValueError:
                logger.warning(
                    "Connected provider '%s' is not a known "
                    "ProviderId; ignoring it when resolving the "
                    "default provider.",
                    key,
                )
                continue
        return ProviderId.GITHUB

    async def _require_token(
        self,
        provider_id: ProviderId,
    ) -> str:
        """
        Return a valid access token for the given provider.

        When a connection exists but no token is stored, or when the
        stored token is present, the method first attempts a refresh
        via the TokenService. If the refresh succeeds, the new token
        is returned. If the refresh is not possible (no refresh
        token, no registered callback), the method falls back to the
        stored token if it is still usable.

        The previous implementation returned the stored token
        unconditionally, which surfaced as a raw 401 from the
        provider whenever a token had expired but a refresh was
        possible. This method attempts the refresh proactively.

        Raises ``AuthRequiredError`` when:
            - no connection is registered, or
            - no token is stored AND refresh did not produce one.
        """
        conn = self._get_connection(provider_id)
        if conn is None:
            raise AuthRequiredError(
                f"No active connection is registered for "
                f"'{provider_id.value}'."
            )

        token = self.token_service.get_access_token(
            provider=provider_id,
            account_id=conn.account_id,
            base_url=conn.base_url,
        )
        if token:
            return token

        # The vault does not have an access token. This can happen
        # after a crash mid-store or when the token was revoked out
        # of band. Try to refresh; the refresh may succeed if the
        # refresh token is still present.
        try:
            info = await self._try_refresh_token(provider_id, conn)
        except Exception as exc:  # noqa: BLE001
            # The refresh path is best-effort; a failure here is
            # reported via the AuthRequiredError below, not by a
            # raw traceback from the token service.
            logger.warning(
                "Token refresh attempt failed for '%s': %s",
                provider_id.value,
                type(exc).__name__,
            )
            info = None

        if info is not None:
            refreshed = self.token_service.get_access_token(
                provider=provider_id,
                account_id=conn.account_id,
                base_url=conn.base_url,
            )
            if refreshed:
                return refreshed

        raise AuthRequiredError(
            f"Credentials for '{provider_id.value}' are missing."
        )

    async def _try_refresh_token(
        self,
        provider_id: ProviderId,
        conn: _ConnectionRecord,
    ):
        """
        Attempt a refresh via the TokenService.

        The TokenService's ``refresh`` is async, so this helper is
        async. It is called from ``_require_token``, which is also
        async. Any exception raised by the token service (network
        failure, missing refresh token, provider-side rejection) is
        logged and swallowed: the caller falls through to
        ``AuthRequiredError`` with a clear message, which is what the
        UI needs.

        This replaces the previous synchronous implementation whose
        refresh path was dead code. The old code only attempted a
        refresh when NO event loop was running; in practice the
        facade always runs inside an event loop, so every expired
        token surfaced as an unrefreshed `AuthRequiredError`. The
        current implementation actually attempts the refresh.
        """
        try:
            return await self.token_service.refresh(
                provider=provider_id,
                account_id=conn.account_id,
                base_url=conn.base_url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Token refresh attempt failed for '%s': %s",
                provider_id.value,
                type(exc).__name__,
            )
            return None

    async def _resolve_repo(
        self,
        adapter: Any,
        repository_id: str,
        provider_id: ProviderId,
        token: str,
    ) -> RepositoryRef:
        """
        Resolve a caller-supplied repository identifier to a fully
        populated `RepositoryRef`.

        The HTTP layer identifies repositories by an opaque string
        (either the provider's stable ID, or a slug like
        ``"owner/repo"``), but the provider adapter contract expects a
        complete `RepositoryRef`. This helper performs the conversion
        that the module docstring previously listed as an outstanding
        integration gap.

        Resolution strategy:

            1. Try the adapter's ``get_repository(repo=..., token=...)``
               with a minimal placeholder ref. This is the fast path
               when the adapter resolves by ID directly, and it also
               works when the adapter accepts a
               ``namespace/name`` slug (the adapter refreshes the
               remaining fields from the provider API).
            2. Fall back to ``list_repositories`` and match by
               ``repository_id``. This is the correct path when the
               adapter cannot resolve by ID alone, but it requires the
               target repository to appear within the first page of
               the listing.

        Raises:
            NotFoundError: When the adapter cannot resolve the ID by
                either path. The message names both the repository
                and the provider so that the UI and the agent can
                surface an actionable diagnostic.
        """
        if not repository_id:
            raise NotFoundError(
                "A non-empty repository_id is required."
            )
        if not token:
            raise NotFoundError(
                f"Cannot resolve repository '{repository_id}': "
                "no access token is available."
            )

        # Normalize the caller's input. The agent sometimes supplies
        # a git clone URL (`git@github.com:owner/repo.git` or
        # `https://github.com/owner/repo.git`) instead of the
        # canonical `owner/repo` slug. Extract the slug so that the
        # rest of the resolution logic sees a consistent form.
        normalized_id = repository_id.strip()
        if normalized_id.startswith("git@"):
            # git@github.com:owner/repo.git -> owner/repo
            _, _, path = normalized_id.partition(":")
            normalized_id = path
        elif normalized_id.startswith(("http://", "https://")):
            # https://github.com/owner/repo(.git) -> owner/repo
            parsed = normalized_id.split("://", 1)[-1]
            _, _, path = parsed.partition("/")
            normalized_id = path
        if normalized_id.endswith(".git"):
            normalized_id = normalized_id[:-4]
        normalized_id = normalized_id.strip("/")

        # Build a minimal placeholder. The adapter is expected to
        # refresh the remaining fields from the provider API. When it
        # cannot, the fallback below fills in the real values.
        conn = self._get_connection(provider_id)
        account_id = conn.account_id if conn is not None else ""
        base_url = conn.base_url if conn is not None else "default"

        placeholder = RepositoryRef(
            provider=provider_id,
            account_id=account_id,
            repository_id=normalized_id,
            namespace="",
            name=normalized_id,
            clone_url="",
            web_url="",
            default_branch="",
            is_private=False,
            base_url=base_url or None,
        )

        # Fast path: ask the adapter to refresh the placeholder. Some
        # adapters accept a string id directly; try that shape first
        # so that a future adapter contract change does not silently
        # regress here. The `repo=` keyword is used because that is
        # what the current adapters declare.
        try:
            refreshed = await adapter.get_repository(
                repo=placeholder, token=token
            )
            if isinstance(refreshed, RepositoryRef):
                return refreshed
        except NotFoundError:
            pass
        except TypeError:
            # Adapter does not accept a ``repo=`` keyword; try the
            # string form (some test doubles do).
            try:
                refreshed = await adapter.get_repository(
                    repository_id, token=token
                )
                if isinstance(refreshed, RepositoryRef):
                    return refreshed
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            # The adapter rejected the placeholder for any reason; the
            # fallback below is the authoritative path.
            pass

        # Fallback: list and filter.
        #
        # Use `page_size=30`, matching what the working
        # `GET /repositories` endpoint uses. Some adapter versions
        # validate `page_size` strictly and reject `page_size=100`
        # with a `ValidationError` that the previous `except
        # Exception: pass` swallowed silently — leaving `listing`
        # empty and producing a misleading "could not be resolved"
        # 404 even when the repository was right there in the
        # default listing.
        #
        # The exception is logged at WARNING (not DEBUG) so that a
        # future occurrence is visible in the server log instead of
        # silently degrading the endpoint.
        try:
            listing = await adapter.list_repositories(
                page=1, page_size=30, token=token
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "list_repositories fallback for '%s' failed: %s: %s",
                repository_id,
                type(exc).__name__,
                exc,
            )
            listing = ()

        # Try exact match on the stable ID first, then on the full
        # name (owner/repo), then on the bare name. The order matters:
        # `owner/repo` is unambiguous; the bare name is only used as
        # a last resort because it can be ambiguous on a shared
        # account. This is what allows a caller to pass `mtaman`
        # (bare name) and receive `mtaman/mtaman` when that is the
        # only repository with that name.
        for candidate in listing:
            if not isinstance(candidate, RepositoryRef):
                continue
            if candidate.repository_id == normalized_id:
                return candidate
        for candidate in listing:
            if not isinstance(candidate, RepositoryRef):
                continue
            if f"{candidate.namespace}/{candidate.name}" == normalized_id:
                return candidate
        for candidate in listing:
            if not isinstance(candidate, RepositoryRef):
                continue
            if candidate.name == normalized_id:
                return candidate

        raise NotFoundError(
            f"Repository '{repository_id}' could not be resolved for "
            f"provider '{provider_id.value}'."
        )

    # ------------------------------------------------------------------
    # 2. Audit helper
    # ------------------------------------------------------------------

    def _audit(
        self,
        event_type: AuditEventType,
        *,
        severity: AuditSeverity = AuditSeverity.INFO,
        action: str = "",
        message: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
        actor: str = "system",
        **context: Any,
    ) -> None:
        """Best-effort audit logging. Never raises."""
        try:
            self.audit_logger.record(
                event_type,
                severity=severity,
                action=action,
                message=message,
                payload=dict(payload or {}),
                actor=actor,
                **context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Audit logging failed (%s): %s",
                event_type.value if hasattr(event_type, "value") else event_type,
                type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # 3. Provider & Connection Orchestration
    # ------------------------------------------------------------------

    async def list_providers(self) -> Sequence[ProviderDescriptorResponse]:
        """List registered providers and their technical capabilities."""
        descriptors: list[ProviderDescriptorResponse] = []
        for raw_pid in self.registry.list_provider_ids():
            pid_str = (
                raw_pid.value if isinstance(raw_pid, ProviderId) else str(raw_pid)
            )
            try:
                adapter = self.registry.get(pid_str)
            except (ValueError, KeyError):
                continue

            caps = getattr(adapter, "capabilities", None) or ProviderCapabilities()
            caps_resp = ProviderCapabilitiesResponse(
                list_repositories=caps.list_repositories,
                read_files=caps.read_files,
                create_branch=caps.create_branch,
                push_branch=caps.push_branch,
                create_change_request=caps.create_change_request,
                list_checks=caps.list_checks,
                comments=caps.comments,
                merge_change_request=caps.merge_change_request,
                self_managed_instance=caps.self_managed_instance,
            )

            manifest = getattr(adapter, "manifest", None)
            web_url = (
                getattr(manifest, "web_base_url", "https://github.com")
                if manifest
                else "https://github.com"
            )
            api_url = (
                getattr(manifest, "api_base_url", "https://api.github.com")
                if manifest
                else "https://api.github.com"
            )

            descriptors.append(
                ProviderDescriptorResponse(
                    provider_id=pid_str,
                    display_name=pid_str.title() + " Cloud",
                    is_connected=self._get_connection(pid_str) is not None,
                    is_configured=self._is_provider_configured(pid_str),
                    capabilities=caps_resp,
                    web_base_url=web_url,
                    api_base_url=api_url,
                )
            )
        return descriptors

    async def list_connections(self) -> Sequence[ConnectionStatusResponse]:
        """List connection and authentication status for all providers."""
        connections: list[ConnectionStatusResponse] = []
        for raw_pid in self.registry.list_provider_ids():
            pid_str = (
                raw_pid.value if isinstance(raw_pid, ProviderId) else str(raw_pid)
            )
            record = self._get_connection(pid_str)
            if record is None:
                connections.append(
                    ConnectionStatusResponse(
                        provider=pid_str,
                        is_connected=False,
                    )
                )
                continue
            connections.append(
                ConnectionStatusResponse(
                    provider=record.provider,
                    is_connected=True,
                    account_id=record.account_id,
                    username=record.username,
                    base_url=record.base_url,
                    last_validated_at=record.last_validated_at,
                )
            )
        return connections

    def register_oauth_provider(
        self,
        provider: ProviderId | str,
        callbacks: OAuthProviderCallbacks,
        *,
        account_resolver: Optional[Any] = None,
        overwrite: bool = False,
    ) -> None:
        """
        Register OAuth callbacks for a provider.

        MUST be called during application startup for every provider
        that supports OAuth. Without registration, `begin_connection`
        raises a clear `ValidationError`.
        """
        provider_id = (
            provider if isinstance(provider, ProviderId) else ProviderId(str(provider))
        )
        self.oauth_coordinator.register_provider(
            provider_id, callbacks, overwrite=overwrite
        )
        if account_resolver is not None:
            self.oauth_coordinator.register_account_resolver(
                provider_id, account_resolver
            )

    async def get_provider_config(self, provider: str) -> dict:
        """Retrieve the configured Client ID (Secret is never returned)."""
        provider_id = provider.strip().lower()
        configs = self._load_provider_configs()
        client_id = configs.get(provider_id)
        if not client_id:
            raise NotFoundError(f"No configuration found for '{provider_id}'.")
        return {"client_id": client_id, "is_configured": True}

    async def delete_provider_config(self, provider: str) -> ActionSuccessResponse:
        """Delete both the Client ID (JSON) and Client Secret (Vault)."""
        provider_id = provider.strip().lower()

        # 1. Delete from JSON
        configs = self._load_provider_configs()
        if provider_id in configs:
            del configs[provider_id]
            self._save_provider_configs(configs)

        # 2. Delete from Vault
        secret_key = f"peldrun.sc.oauth_secret_{provider_id}"
        try:
            self.vault.delete(secret_key)
        except Exception as exc:
            logger.warning("Failed to delete secret from vault: %s", exc)

        self._audit(
            AuditEventType.OTHER,
            action="delete_provider_config",
            message=f"OAuth credentials deleted for '{provider_id}'.",
            payload={"provider": provider_id},
            actor="user",
        )

        return ActionSuccessResponse(
            message=f"Successfully deleted OAuth credentials for {provider_id.title()}."
        )

    async def save_provider_config(
        self, provider: str, payload: SaveProviderConfigRequest
    ) -> ActionSuccessResponse:
        """
        Save user-provided OAuth App credentials.
        Client ID goes to JSON, Client Secret goes to Vault.
        """
        provider_id = provider.strip().lower()
        self.registry.get(provider_id)

        # 1. Save Client ID to JSON file
        configs = self._load_provider_configs()
        configs[provider_id] = payload.client_id
        self._save_provider_configs(configs)

        # 2. Save Client Secret to OS Vault
        secret_key = f"peldrun.sc.oauth_secret_{provider_id}"
        self.vault.put(secret_key, payload.client_secret)

        self._audit(
            AuditEventType.OTHER,
            action="save_provider_config",
            message=f"OAuth App credentials configured for '{provider_id}'.",
            payload={"provider": provider_id},
            actor="user",
        )

        return ActionSuccessResponse(
            message=f"Successfully configured OAuth credentials for {provider_id.title()}."
        )

    async def begin_connection(
        self, provider: str, payload: ConnectBeginRequest
    ) -> ConnectBeginResponse:
        """Start an OAuth 2.0 PKCE handshake and return the authorization URL."""
        provider_id = ProviderId(provider.strip().lower())
        self.registry.get(provider_id.value)

        # BYOK: Verify that the user has configured credentials
        configs = self._load_provider_configs()
        client_id = configs.get(provider_id.value)
        secret_key = f"peldrun.sc.oauth_secret_{provider_id.value}"
        client_secret = self.vault.get(secret_key)

        if not client_id or not client_secret:
            raise ValidationError(
                f"OAuth credentials for '{provider_id.value}' are not configured. "
                "Please provide Client ID and Secret first."
            )

        scopes = getattr(payload, "scopes", None) or None
        redirect_uri = (
            getattr(payload, "redirect_uri", None) or _DEFAULT_OAUTH_REDIRECT
        )

        result = self.oauth_coordinator.begin_authorization(
            provider_id,
            client_id=client_id,
            scopes=scopes,
            redirect_uri=redirect_uri,
        )

        self._audit(
            AuditEventType.AUTH_BEGIN,
            action="begin_connection",
            message=f"OAuth authorization started for '{provider_id.value}'.",
            payload={
                "provider": provider_id.value,
                "redirect_uri": redact_url(result.redirect_uri),
            },
            actor="user",
        )

        expires_in = 600
        if getattr(result, "expires_at", None):
            delta = result.expires_at - _utc_now()
            expires_in = max(1, int(delta.total_seconds()))

        return ConnectBeginResponse(
            provider=provider_id.value,
            authorization_url=result.authorization_url,
            state=result.state,
            expires_in_seconds=expires_in,
        )

    async def complete_oauth_callback(
        self, provider: str, code: str, state: str
    ) -> ActionSuccessResponse:
        """Complete an OAuth PKCE exchange and persist the connection record."""
        provider_id = ProviderId(provider.strip().lower())
        self.registry.get(provider_id.value)

        session = self.oauth_coordinator.sessions.get(state)
        if session is None:
            raise ValidationError(
                f"Unknown or expired OAuth state '{state}'."
            )
        redirect_uri = session.redirect_uri

        # BYOK: Fetch credentials for the code exchange
        configs = self._load_provider_configs()
        client_id = configs.get(provider_id.value)
        secret_key = f"peldrun.sc.oauth_secret_{provider_id.value}"
        client_secret = self.vault.get(secret_key)

        if not client_id or not client_secret:
            raise ValidationError(
                f"OAuth credentials for '{provider_id.value}' were deleted mid-flow."
            )

        auth_result = await self.oauth_coordinator.complete_authorization(
            provider_id,
            state=state,
            code=code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            client_secret=client_secret,
        )

        record = _ConnectionRecord(
            provider=provider_id.value,
            account_id=auth_result.account_id,
            base_url=auth_result.base_url or "default",
            username=auth_result.username or "",
            connected_at=_utc_now(),
            last_validated_at=_utc_now(),
        )
        self._set_connection(record)

        self._audit(
            AuditEventType.AUTH_COMPLETE,
            action="complete_oauth_callback",
            message=f"OAuth completed for '{provider_id.value}'.",
            payload={
                "provider": provider_id.value,
                "username": auth_result.username,
                "account_id": auth_result.account_id,
            },
            actor="user",
        )

        return ActionSuccessResponse(
            message=(
                f"Connected to {provider_id.value.title()} as "
                f"'{auth_result.username}'."
            )
        )

    async def validate_connection(
        self, provider: str, payload: ValidateConnectionRequest
    ) -> ConnectionStatusResponse:
        """Validate a connection by querying the provider identity."""
        provider_id = ProviderId(provider.strip().lower())
        adapter = self.registry.get(provider_id.value)

        conn = self._get_connection(provider_id)
        if conn is None:
            raise AuthRequiredError(
                f"No active connection is registered for "
                f"'{provider_id.value}'."
            )

        if getattr(payload, "force_refresh", False):
            try:
                await self.token_service.refresh(
                    provider=provider_id,
                    account_id=conn.account_id,
                    base_url=conn.base_url,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Token refresh failed for '%s': %s",
                    provider_id.value,
                    type(exc).__name__,
                )

        token = self.token_service.get_access_token(
            provider=provider_id,
            account_id=conn.account_id,
            base_url=conn.base_url,
        )
        if not token:
            raise AuthRequiredError(
                f"Credentials for '{provider_id.value}' are missing."
            )

        account = await adapter.get_authenticated_account(token)

        updated = replace(
            conn,
            username=account.username or conn.username,
            last_validated_at=_utc_now(),
        )
        self._set_connection(updated)

        return ConnectionStatusResponse(
            provider=account.provider.value
            if isinstance(account.provider, ProviderId)
            else account.provider,
            is_connected=bool(account.connected),
            account_id=account.account_id,
            username=account.username,
            base_url=account.base_url,
            last_validated_at=updated.last_validated_at,
        )

    async def disconnect_provider(
        self, provider: str, payload: DisconnectRequest
    ) -> ActionSuccessResponse:
        """Disconnect a provider, revoke upstream token, and purge credentials."""
        provider_id = ProviderId(provider.strip().lower())
        conn = self._get_connection(provider_id)

        if conn is not None:
            token = self.token_service.get_access_token(
                provider=provider_id,
                account_id=conn.account_id,
                base_url=conn.base_url,
            )
            if token and getattr(payload, "revoke_upstream_token", False):
                try:
                    adapter = self.registry.get(provider_id.value)
                    auth = getattr(adapter, "auth", None)
                    if auth is not None and hasattr(auth, "revoke_token"):
                        await auth.revoke_token(token)
                except Exception as exc:  # noqa: BLE001
                    # Best-effort; log the failure so operators can
                    # investigate without leaking the token.
                    logger.warning(
                        "Provider-side revocation failed for '%s': %s",
                        provider_id.value,
                        type(exc).__name__,
                    )

            self.token_service.revoke(
                provider=provider_id,
                account_id=conn.account_id,
                base_url=conn.base_url,
            )
            self._remove_connection(provider_id)

        self._audit(
            AuditEventType.CONNECTION_DISCONNECT,
            action="disconnect_provider",
            message=f"Disconnected '{provider_id.value}'.",
            payload={
                "provider": provider_id.value,
                "revoked_upstream": bool(
                    getattr(payload, "revoke_upstream_token", False)
                ),
            },
            actor="user",
        )

        return ActionSuccessResponse(
            message=(
                f"Disconnected provider '{provider_id.value.title()}' "
                f"and purged credentials."
            )
        )

    # ------------------------------------------------------------------
    # 4. Repository & Remote Inspection
    # ------------------------------------------------------------------

    async def list_repositories(
        self,
        provider: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 30,
    ) -> Sequence[RepositoryResponse]:
        """List repositories accessible to the authenticated user."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)

        # The adapter's pagination parameter is `page_size`, not
        # `limit`. Passing `limit=` raised TypeError against the real
        # adapter; the mock-based tests did not catch this because
        # mocks accept any keyword.
        # We must pass page=1 explicitly; some adapter versions
        # require it as a positional argument.
        repos = await adapter.list_repositories(
            page=1, query=query, page_size=limit, token=token
        )

        return [
            RepositoryResponse(
                provider=(
                    r.provider.value
                    if isinstance(r.provider, ProviderId)
                    else r.provider
                ),
                repository_id=r.repository_id,
                namespace=r.namespace,
                name=r.name,
                full_name=f"{r.namespace}/{r.name}",
                default_branch=r.default_branch,
                web_url=redact_url(r.web_url),
                clone_url=redact_url(r.clone_url),
                is_private=r.is_private,
                description=getattr(r, "description", None),
                metadata=dict(getattr(r, "metadata", {}) or {}),
            )
            for r in repos
        ]

    async def get_repository(
        self,
        repository_id: str,
        provider: Optional[str] = None,
    ) -> RepositoryResponse:
        """Retrieve repository metadata."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)

        # NOTE: the current GitHub adapter expects a `RepositoryRef`,
        # not a string id. This call will raise `ValidationError`
        # against the real adapter. A resolution layer is tracked as
        # an outstanding integration gap; the current implementation
        # is exercised only against mock adapters in the test suite.
        r: RepositoryRef = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        return RepositoryResponse(
            provider=(
                r.provider.value
                if isinstance(r.provider, ProviderId)
                else r.provider
            ),
            repository_id=r.repository_id,
            namespace=r.namespace,
            name=r.name,
            full_name=f"{r.namespace}/{r.name}",
            default_branch=r.default_branch,
            web_url=redact_url(r.web_url),
            clone_url=redact_url(r.clone_url),
            is_private=r.is_private,
            description=getattr(r, "description", None),
            metadata=dict(getattr(r, "metadata", {}) or {}),
        )

    async def list_branches(
        self,
        repository_id: str,
        provider: Optional[str] = None,
        limit: int = 50,
    ) -> Sequence[BranchResponse]:
        """List branches for a repository."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. This call is only exercised
        # against mock adapters today. See the module docstring for
        # the outstanding integration gap.
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )
        # The adapter does not accept a `limit` keyword; it pages
        # internally up to `MAX_BRANCHES`. `limit` is applied below
        # on the client side.
        branches = await adapter.list_branches(
            repo=repo, token=token
        )

        return [
            BranchResponse(
                name=b.name,
                head_sha=b.head_sha,
                is_default=b.is_default,
                is_protected=is_protected_branch(b.name),
            )
            for b in branches[: max(0, limit)]
        ]

    async def create_branch(
        self,
        repository_id: str,
        payload: CreateBranchRequest,
        provider: Optional[str] = None,
    ) -> BranchResponse:
        """Create a new branch on the remote repository."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        clean_branch = validate_branch_name(payload.branch_name)

        policy_ctx = PolicyResolutionContext(
            operation="create_branch",
            provider=provider_id,
            target_branch=clean_branch,
            actor="user",
        )
        self.policy_engine.require(policy_ctx)

        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. This call is only exercised
        # against mock adapters today. See the module docstring for
        # the outstanding integration gap.
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        from_sha = getattr(payload, "from_sha", None)
        if not from_sha:
            branches = await adapter.list_branches(repo=repo, token=token)
            default_branch = next(
                (b for b in branches if b.is_default), None
            )
            if default_branch is not None:
                from_sha = default_branch.head_sha
        if not from_sha:
            raise ValidationError("Could not resolve starting commit SHA.")

        b: BranchRef = await adapter.create_branch(
            repo=repo,
            branch_name=clean_branch,
            from_sha=from_sha,
            token=token,
        )

        self._audit(
            AuditEventType.OTHER,
            action="branch_created",
            message=f"Created branch '{clean_branch}'.",
            payload={
                "provider": provider_id.value,
                "repository_id": repository_id,
                "branch_name": clean_branch,
                "from_sha": from_sha,
            },
            provider=provider_id.value,
            repository_id=repository_id,
            actor="user",
        )

        return BranchResponse(
            name=b.name,
            head_sha=b.head_sha,
            is_default=b.is_default,
            is_protected=is_protected_branch(b.name),
        )

    async def read_file(
        self,
        repository_id: str,
        file_path: str,
        ref: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> FileReadResponse:
        """
        Read a remote file at a given revision.

        The adapter contract is ambiguous today: `GitHubProviderAdapter`
        returns a `str` (already decoded), while a bytes-oriented
        adapter would return `bytes`. This method handles both shapes
        so that the facade works with either. A future cleanup should
        pin the adapter contract to a single type.
        """
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. This call is only exercised
        # against mock adapters today. See the module docstring for
        # the outstanding integration gap.
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )
        raw_content = await adapter.read_file(
            repo=repo, file_path=file_path, ref=ref, token=token
        )

        effective_ref = ref or getattr(repo, "default_branch", "main")

        # Normalize the adapter's return value to bytes for
        # consistent size accounting and binary detection.
        if isinstance(raw_content, (bytes, bytearray)):
            raw_bytes = bytes(raw_content)
            # Binary detection: NUL bytes do not appear in valid
            # UTF-8 text; their presence indicates a binary asset.
            if b"\x00" in raw_bytes:
                return FileReadResponse(
                    repository_id=repository_id,
                    file_path=file_path,
                    ref=effective_ref,
                    is_binary=True,
                    size_bytes=len(raw_bytes),
                    is_truncated=False,
                    content=None,
                )
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text = raw_bytes.decode("latin-1", errors="replace")
            size_bytes = len(raw_bytes)
        elif isinstance(raw_content, str):
            # The adapter already decoded the payload; its reported
            # size is the UTF-8 byte count, NOT the character count.
            text = raw_content
            size_bytes = len(text.encode("utf-8"))
        else:
            raise SourceControlError(
                "Adapter returned an unsupported file payload type.",
                details={"type": type(raw_content).__name__},
            )

        # Truncate on a UTF-8 byte boundary, not on a character
        # boundary. Counting characters would let a multi-byte string
        # exceed the byte budget by up to 4x.
        truncated_text, was_truncated = _truncate_to_byte_budget(
            text, _MAX_FILE_CONTENT_BYTES
        )

        return FileReadResponse(
            repository_id=repository_id,
            file_path=file_path,
            ref=effective_ref,
            is_binary=False,
            size_bytes=size_bytes,
            is_truncated=was_truncated,
            content=truncated_text,
        )

    # ------------------------------------------------------------------
    # 5. Workspaces, Diffs & CI Checks
    # ------------------------------------------------------------------

    async def create_workspace(
        self, payload: CreateWorkspaceRequest, chat_id: Optional[str] = None
    ) -> WorkspaceResponse:
        """Create an isolated local workspace bound to a real repository."""
        # Use the provider explicitly requested by the caller, falling
        # back to the default provider when the request does not name
        # one. The previous implementation always used the default
        # provider, silently ignoring `payload.provider`.
        provider_name = getattr(payload, "provider", None) or ""
        provider_id = (
            ProviderId(provider_name.strip().lower())
            if provider_name
            else self._default_provider_id()
        )
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. See the module docstring.
        repo = await self._resolve_repo(
            adapter, payload.repository_id, provider_id, token
        )

        policy_ctx = PolicyResolutionContext(
            operation="create_workspace",
            provider=provider_id,
            repository=repo,
            target_branch=getattr(payload, "branch_name", None),
            actor="agent",
        )
        self.policy_engine.require(policy_ctx)

        target_path = None
        if chat_id:
            from pathlib import Path
            # Resolve against the PROJECT root, not the backend package.
            backend_root = Path(__file__).resolve().parent.parent.parent
            project_root = backend_root.parent
            target_path = project_root / "storage" / "chats" / chat_id / "files"

            if not target_path.exists() and (backend_root / "storage" / "chats" / chat_id / "files").exists():
                target_path = backend_root / "storage" / "chats" / chat_id / "files"

            # A chat can host at most ONE workspace. If the target
            # already contains a .git directory, this is a
            # re-request on an existing workspace, not a new one.
            # Refuse early with a clear message instead of letting
            # `git worktree add` fail with an opaque error.
            if target_path.exists() and (target_path / ".git").exists():
                raise ValidationError(
                    f"Chat '{chat_id}' already contains a workspace. "
                    "Please use a fresh chat to create a new workspace, "
                    "or reuse the existing one."
                )

        # Pass `auth_token` so that private repositories can be
        # cloned. The previous implementation dropped the token
        # before this call, which made every private-repo clone via
        # the UI fail with an authentication error from the provider
        # even though the token was already resolved above.
        ws_ref = await self.workspace_manager.create_workspace(
            repo=repo,
            base_sha=getattr(payload, "base_sha", None),
            source_branch=getattr(payload, "branch_name", None),
            use_worktree=True,
            target_path=target_path,
            auth_token=token,
        )

        self._audit(
            AuditEventType.WORKSPACE_CREATE,
            action="create_workspace",
            message=f"Created workspace '{ws_ref.workspace_id}'.",
            payload={
                "workspace_id": ws_ref.workspace_id,
                "repository_id": ws_ref.repository_id,
                "current_branch": ws_ref.current_branch,
            },
            repository_id=ws_ref.repository_id,
            provider=provider_id.value,
            workspace_id=ws_ref.workspace_id,
            actor="agent",
        )

        return WorkspaceResponse(
            workspace_id=ws_ref.workspace_id,
            repository_id=ws_ref.repository_id,
            current_branch=ws_ref.current_branch,
            base_sha=ws_ref.base_sha,
            head_sha=ws_ref.head_sha,
            is_dirty=not ws_ref.is_clean,
            created_at=ws_ref.created_at,
        )

    async def get_workspace(self, workspace_id: str) -> WorkspaceResponse:
        """Retrieve a workspace."""
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )
        return WorkspaceResponse(
            workspace_id=ws_ref.workspace_id,
            repository_id=ws_ref.repository_id,
            current_branch=ws_ref.current_branch,
            base_sha=ws_ref.base_sha,
            head_sha=ws_ref.head_sha,
            is_dirty=not ws_ref.is_clean,
            created_at=ws_ref.created_at,
        )

    async def get_workspace_by_chat(self, chat_id: str) -> Optional[WorkspaceResponse]:
        """
        Return the workspace bound to a chat, if one exists.

        Resolution order:

            1. If ``storage/chats/<chat_id>/files/.peldrun/workspace.json``
               exists, use it as the authoritative source. This file is
               written by the workspace manager at clone time and
               survives server restarts. It is the only source that
               works when the in-memory registry was lost (e.g. after
               a process restart or a hot reload).

            2. Search the in-memory workspace registry for an entry
               whose path matches the chat's files folder.

            3. Search the durable operation journal for a completed
               operation bound to that folder.

        Returns ``None`` when none of the three sources can resolve a
        workspace, which tells the caller that the chat is empty and a
        fresh ``create_workspace`` call is appropriate.
        """
        from pathlib import Path
        import json as _json

        backend_root = Path(__file__).resolve().parent.parent.parent
        project_root = backend_root.parent
        candidate_paths = [
            project_root / "storage" / "chats" / chat_id / "files",
            backend_root / "storage" / "chats" / chat_id / "files",
        ]
        # Primary expected path is the PROJECT root (matches how
        # PELDRUN actually stores chat files). The backend root
        # variant is a compatibility fallback for older layouts.
        expected_path = next(
            (p for p in candidate_paths if p.exists()),
            candidate_paths[0],
        )

        # ---- Strategy 1: .peldrun/workspace.json on disk ----
        marker_path = expected_path / ".peldrun" / "workspace.json"
        if marker_path.exists():
            try:
                raw = marker_path.read_text(encoding="utf-8")
                data = _json.loads(raw)
                ws_id = data.get("workspace_id")

                # Determine the ACTUAL branch from git. The marker
                # file records the branch at clone time, but the
                # agent (or the user) may have switched branches
                # since then (e.g. `git checkout -b docs/...`). We
                # always ask git for the truth so the frontend
                # creates the push operation against the right
                # branch and the policy engine does not refuse the
                # request because it thinks we are on `main`.
                actual_branch = str(data.get("current_branch") or "main")
                try:
                    import subprocess
                    res = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        cwd=str(expected_path),
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if (
                        res.returncode == 0
                        and res.stdout.strip()
                        and res.stdout.strip() != "HEAD"
                    ):
                        actual_branch = res.stdout.strip()
                except Exception:
                    # Best-effort: if git is unavailable or the
                    # directory is not a real repo, fall back to
                    # the marker value.
                    pass

                if ws_id:
                    # Try the live registry first so is_dirty/head_sha
                    # reflect the current state.
                    live = self.workspace_manager.get_workspace(ws_id)
                    if live is not None:
                        return WorkspaceResponse(
                            workspace_id=live.workspace_id,
                            repository_id=live.repository_id,
                            current_branch=actual_branch,
                            base_sha=live.base_sha,
                            head_sha=live.head_sha,
                            is_dirty=not live.is_clean,
                            created_at=live.created_at,
                        )
                    # Otherwise return the values captured in the
                    # marker file at clone time. The caller only needs
                    # workspace_id, repository_id, and current_branch
                    # to skip step 1; a slightly stale head_sha is
                    # harmless because the next approval request
                    # recomputes the diff from disk anyway.
                    created_at_raw = data.get("created_at")
                    try:
                        created_at = (
                            datetime.fromisoformat(created_at_raw)
                            if isinstance(created_at_raw, str)
                            else _utc_now()
                        )
                    except (ValueError, TypeError):
                        created_at = _utc_now()
                    return WorkspaceResponse(
                        workspace_id=ws_id,
                        repository_id=str(data.get("repository_id") or ""),
                        current_branch=actual_branch,
                        base_sha=str(data.get("base_sha") or "HEAD"),
                        head_sha=data.get("head_sha"),
                        is_dirty=bool(data.get("is_dirty", False)),
                        created_at=created_at,
                    )
            except (OSError, _json.JSONDecodeError, ValueError):
                # Marker is corrupt or unreadable; fall through to
                # the registry and journal strategies.
                pass

        # ---- Strategy 2: in-memory workspace registry ----
        all_workspaces = []
        if hasattr(self.workspace_manager, "list_workspaces"):
            try:
                all_workspaces = self.workspace_manager.list_workspaces()
            except Exception:
                pass
        elif hasattr(self.workspace_manager, "_workspaces"):
            all_workspaces = list(self.workspace_manager._workspaces.values())

        for ws in all_workspaces:
            ws_path = getattr(ws, "workspace_path", "") or ""
            try:
                if Path(ws_path).resolve() == expected_path.resolve():
                    return WorkspaceResponse(
                        workspace_id=ws.workspace_id,
                        repository_id=ws.repository_id,
                        current_branch=ws.current_branch,
                        base_sha=ws.base_sha,
                        head_sha=ws.head_sha,
                        is_dirty=not ws.is_clean,
                        created_at=ws.created_at,
                    )
            except (OSError, ValueError):
                continue

        # ---- Strategy 3: durable operation journal ----
        try:
            operations = await self.operation_service.list_operations()
            for op in operations or ():
                if not op.workspace_id:
                    continue
                ws_ref = self.workspace_manager.get_workspace(op.workspace_id)
                if ws_ref is None:
                    continue
                try:
                    if Path(ws_ref.workspace_path).resolve() == expected_path.resolve():
                        return WorkspaceResponse(
                            workspace_id=ws_ref.workspace_id,
                            repository_id=ws_ref.repository_id,
                            current_branch=ws_ref.current_branch,
                            base_sha=ws_ref.base_sha,
                            head_sha=ws_ref.head_sha,
                            is_dirty=not ws_ref.is_clean,
                            created_at=ws_ref.created_at,
                        )
                except (OSError, ValueError):
                    continue
        except Exception:
            pass

        return None

    async def delete_workspace(
        self, workspace_id: str, *, force_delete_files: bool = False
    ) -> ActionSuccessResponse:
        """
        Delete a workspace from the registry and, when requested,
        remove its files from disk.

        Deletion is ordered so that a filesystem failure leaves the
        workspace intact and recoverable:

            1. Locate the workspace path. If the workspace is not in
               the registry (e.g. after a process restart) the path
               is recovered by scanning `storage/chats/*/files/` for
               a `.peldrun/workspace.json` marker that names this
               workspace_id.

            2. Delete the files (only when ``force_delete_files``).
               On Windows, `.git` internals are frequently marked
               read-only and `shutil.rmtree` fails with a
               `PermissionError`. A retry handler that clears the
               read-only bit is installed before giving up.

            3. Only when the files are gone (or deletion was not
               requested) is the workspace removed from the
               registry. This ordering is essential: if we removed
               the registry entry first and file deletion then
               failed, the chat folder would be left with a `.git`
               directory that `create_workspace` refuses to
               overwrite — and the registry would no longer know
               about it, so the user would be stuck.

        Raises ``ValidationError`` when ``force_delete_files`` was
        requested but the files could not be removed. In that case
        the registry is NOT modified.
        """
        import inspect as _inspect
        import shutil
        import os
        import stat
        import sys
        from pathlib import Path as _Path

        ws_ref = self.workspace_manager.get_workspace(workspace_id)

        # ---- 1. Locate the workspace path ----
        workspace_path: Optional[str] = None
        if ws_ref is not None:
            workspace_path = getattr(ws_ref, "workspace_path", None)

        # Fallback: scan chat folders for a matching marker. This is
        # what allows delete to work after a process restart even
        # when the in-memory registry was lost.
        if not workspace_path:
            backend_root = _Path(__file__).resolve().parent.parent.parent
            project_root = backend_root.parent
            for base in (
                project_root / "storage" / "chats",
                backend_root / "storage" / "chats",
            ):
                if not base.exists():
                    continue
                for chat_dir in base.iterdir():
                    if not chat_dir.is_dir():
                        continue
                    files_dir = chat_dir / "files"
                    marker = files_dir / ".peldrun" / "workspace.json"
                    if not marker.exists():
                        continue
                    try:
                        import json as _json
                        data = _json.loads(
                            marker.read_text(encoding="utf-8")
                        )
                        if data.get("workspace_id") == workspace_id:
                            workspace_path = str(files_dir)
                            break
                    except Exception:
                        continue
                if workspace_path:
                    break

        if ws_ref is None and not workspace_path:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        # ---- 2. Delete files first (if requested) ----
        deleted_files = False
        if force_delete_files and workspace_path:
            p = _Path(workspace_path)
            if p.exists():
                try:
                    shutil.rmtree(p)
                    deleted_files = True
                except (OSError, PermissionError) as exc:
                    # Windows retry: clear read-only bits and retry.
                    if os.name == "nt":
                        def _retry(func, path, _exc_info):
                            try:
                                os.chmod(path, stat.S_IWRITE)
                                func(path)
                            except Exception:
                                pass
                        try:
                            if sys.version_info >= (3, 12):
                                shutil.rmtree(p, onexc=_retry)
                            else:
                                shutil.rmtree(p, onerror=_retry)
                            deleted_files = True
                        except Exception:
                            deleted_files = False
                    if not deleted_files:
                        logger.error(
                            "delete_workspace: could not remove files "
                            "at '%s': %s",
                            workspace_path,
                            type(exc).__name__,
                        )
                        raise ValidationError(
                            f"Could not delete workspace files at "
                            f"'{workspace_path}'. The workspace "
                            f"registry entry was NOT removed, so you "
                            f"can retry. Underlying error: "
                            f"{type(exc).__name__}: {exc}"
                        )

        # ---- 3. Only now remove from the registry ----
        manager_delete = getattr(
            self.workspace_manager, "delete_workspace", None
        )
        if manager_delete is not None:
            try:
                # We pass force_delete_files=False because we already
                # deleted the files above (or the caller did not ask
                # for deletion). The manager is only asked to purge
                # its own registry bookkeeping.
                result = manager_delete(
                    workspace_id, force_delete_files=False
                )
            except TypeError:
                result = manager_delete(workspace_id)
            if _inspect.isawaitable(result):
                await result
        else:
            internal = getattr(
                self.workspace_manager, "_workspaces", None
            )
            if isinstance(internal, dict):
                internal.pop(workspace_id, None)

        self._audit(
            AuditEventType.OTHER,
            action="delete_workspace",
            message=f"Deleted workspace '{workspace_id}'.",
            payload={
                "workspace_id": workspace_id,
                "workspace_path": workspace_path,
                "force_delete_files": force_delete_files,
                "deleted_files": deleted_files,
            },
            workspace_id=workspace_id,
            actor="user",
        )

        return ActionSuccessResponse(
            message=(
                f"Workspace '{workspace_id}' deleted."
                + (" Files removed." if deleted_files else "")
            )
        )

    async def ensure_editable_branch(
        self,
        workspace_id: str,
        *,
        preferred_name: Optional[str] = None,
    ) -> str:
        """
        Ensure the workspace is on a non-protected branch.
        """
        from datetime import datetime as _dt, timezone

        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(f"Workspace '{workspace_id}' was not found.")

        try:
            res = await self.branch_manager._git.run(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                cwd=ws_ref.workspace_path,
                check_returncode=False,
            )
            current = res.stdout.strip() if res.returncode == 0 else ws_ref.current_branch
        except Exception:
            current = ws_ref.current_branch

        if not is_protected_branch(current):
            return current

        # Determine the new branch name.
        new_branch: Optional[str] = None
        if preferred_name:
            clean = validate_branch_name(preferred_name)
            if is_protected_branch(clean):
                raise ValidationError(
                    f"Cannot use '{clean}' as the auto-created feature "
                    f"branch: it is itself a protected branch. "
                    f"Choose a name outside the protected pattern set "
                    f"(e.g. 'feature/{clean.replace('/', '-')}').",
                    details={
                        "preferred_name": preferred_name,
                        "current_branch": current,
                    },
                )
            new_branch = clean

        if not new_branch:
            stamp = _dt.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            new_branch = f"peldrun/manual-{stamp}"

        exists = await self.branch_manager._git.run(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{new_branch}"],
            cwd=ws_ref.workspace_path,
            check_returncode=False,
        )
        if exists.returncode == 0:
            res = await self.branch_manager._git.run(
                ["checkout", new_branch],
                cwd=ws_ref.workspace_path,
                check_returncode=False,
            )
            if res.returncode != 0:
                raise ValidationError(
                    f"Could not check out existing branch '{new_branch}': "
                    f"{res.stderr.strip()}"
                )
        else:
            res = await self.branch_manager._git.run(
                ["checkout", "-b", new_branch],
                cwd=ws_ref.workspace_path,
                check_returncode=False,
            )
            if res.returncode != 0:
                raise ValidationError(
                    f"Could not create branch '{new_branch}': "
                    f"{res.stderr.strip()}"
                )

        try:
            if hasattr(self.workspace_manager, "set_current_branch"):
                self.workspace_manager.set_current_branch(
                    workspace_id, new_branch
                )
            elif (
                hasattr(self.workspace_manager, "_workspaces")
                and isinstance(self.workspace_manager._workspaces, dict)
                and workspace_id in self.workspace_manager._workspaces
            ):
                from dataclasses import replace as _dc_replace
                _current_ws = self.workspace_manager._workspaces[
                    workspace_id
                ]
                self.workspace_manager._workspaces[workspace_id] = (
                    _dc_replace(_current_ws, current_branch=new_branch)
                )
        except Exception as exc:
            logger.warning(
                "ensure_editable_branch: could not update "
                "workspace.current_branch for %r: %s",
                workspace_id,
                type(exc).__name__,
            )

        self._audit(
            AuditEventType.OTHER,
            action="auto_branch_created",
            message=(
                f"Auto-created branch '{new_branch}' from '{current}'."
                + (" (user-specified name)" if preferred_name else "")
            ),
            payload={
                "workspace_id": workspace_id,
                "from_branch": current,
                "to_branch": new_branch,
                "user_specified": bool(preferred_name),
            },
            actor="user",
        )
        return new_branch

    async def cleanup_workspaces(self) -> dict:
        """
        Prune orphaned workspace entries from the registry.

        A workspace becomes orphaned when its `workspace_path` no
        longer exists on disk. The most common cause is deletion of
        the owning chat or project, which removes the physical
        directory tree under `storage/chats/<chat>/files/` but
        leaves the registry entry behind. This method drops those
        stale entries without touching any surviving workspace.

        Companion of `chat_storage_engine.sweep_orphaned_storage()`,
        which handles the physical storage tree; this method handles
        the source-control workspace registry.
        """
        result = await self.workspace_manager.cleanup_orphans()
        self._audit(
            AuditEventType.OTHER,
            action="cleanup_workspaces",
            message=(
                f"Pruned {result['removed_count']} orphaned "
                f"workspace(s)."
            ),
            payload=result,
            actor="user",
        )
        return result

    async def cleanup_orphaned_operations(
        self, dry_run: bool = False
    ) -> dict:
        """
        Prune journal operations whose bound workspace is gone.

        When a chat is deleted, its `files/` directory (which hosted
        the workspace's Git repository) is removed, but the durable
        operation journal — stored under
        `backend/data/source_control/journal/` — is intentionally
        untouched by the chat-storage layer. Those operations then
        surface in the UI as ghosts:

            - `GET /operations/{id}` returns 200 (the record exists).
            - `GET /workspaces/{id}/diff` returns 404 (the workspace
              no longer exists).

        The user sees a "Waiting for your review" card whose diff
        can never load and whose Approve button can never succeed.
        This method removes those dead operations from the journal
        so the UI can return to a clean state.

        Only operations whose `workspace_id` is set AND whose
        workspace cannot be resolved by the workspace manager are
        pruned. Operations without a workspace (e.g. a pure
        `list_operations` call that never created one) are left
        alone because they hold no orphan state.

        Args:
            dry_run: When True, report what WOULD be removed without
                mutating the journal. Use this from UI previews and
                before destructive sweeps.

        Returns:
            A dict with the following shape::

                {
                    "removed_count": int,
                    "removed_ids": List[str],
                    "removed": List[{
                        "operation_id": str,
                        "workspace_id": str,
                        "status": str,
                    }],
                    "dry_run": bool,
                }
        """
        # Deferred imports so that a facade module import never
        # forces the journal subsystem to be initialized on a code
        # path that does not use source control.
        try:
            from integrations.source_control.operations.journal import (
                FileOperationJournal,
            )
        except ImportError:  # repo layout: backend.integrations.*
            from backend.integrations.source_control.operations.journal import (  # type: ignore[no-redef]
                FileOperationJournal,
            )

        # Discover the operation IDs currently persisted. The
        # operation service exposes `list_operations()` in the
        # current implementation; fall back to the underlying
        # journal's internal records map when that method is not
        # available (older builds, test doubles).
        operation_ids: List[str] = []
        try:
            listed = await self.operation_service.list_operations()
            for record in listed or ():
                op_id = getattr(record, "operation_id", None)
                if isinstance(op_id, str) and op_id:
                    operation_ids.append(op_id)
        except AttributeError:
            journal = getattr(
                self.operation_service, "_journal", None
            )
            if isinstance(journal, FileOperationJournal):
                operation_ids = list(
                    getattr(journal, "_records", {}).keys()
                )

        removed: List[dict] = []
        removed_ids: List[str] = []

        for op_id in operation_ids:
            try:
                record = await self.operation_service.try_get_operation(
                    op_id
                )
            except Exception:  # noqa: BLE001
                # A read failure on one entry must not abort the
                # sweep. Treat it as "unknown" and skip.
                continue
            if record is None:
                continue

            ws_id = getattr(record, "workspace_id", None)
            if not ws_id:
                # No workspace bound; nothing to orphan.
                continue

            # Does the workspace still resolve?
            ws_ref = self.workspace_manager.get_workspace(ws_id)
            if ws_ref is not None:
                continue

            status_value = (
                record.status.value
                if hasattr(record.status, "value")
                else str(record.status)
            )
            removed.append(
                {
                    "operation_id": op_id,
                    "workspace_id": ws_id,
                    "status": status_value,
                }
            )
            removed_ids.append(op_id)

        # Apply the deletion unless this is a dry run.
        failures: List[dict] = []
        deleted_ids: List[str] = []
        if not dry_run and removed_ids:
            for op_id in removed_ids:
                try:
                    await self.operation_service.delete_operation(op_id)
                    deleted_ids.append(op_id)
                except AttributeError:
                    # Fallback: if the operation service does not
                    # expose `delete_operation`, try to remove the
                    # journal record directly. This path exists for
                    # older builds and test doubles.
                    journal = getattr(
                        self.operation_service, "_journal", None
                    )
                    if isinstance(journal, FileOperationJournal):
                        try:
                            journal.delete(op_id)
                            deleted_ids.append(op_id)
                        except Exception as exc:  # noqa: BLE001
                            failures.append(
                                {
                                    "operation_id": op_id,
                                    "error": type(exc).__name__,
                                }
                            )
                    else:
                        failures.append(
                            {
                                "operation_id": op_id,
                                "error": "journal_unavailable",
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        {
                            "operation_id": op_id,
                            "error": type(exc).__name__,
                        }
                    )

            if failures:
                logger.warning(
                    "cleanup_orphaned_operations: %d operation(s) "
                    "could not be deleted: %s",
                    len(failures),
                    failures,
                )

            # Narrow the reported removal set to entries that were
            # actually removed so the caller receives an accurate
            # count. The dry_run branch above still reports the full
            # candidate list.
            removed = [
                item for item in removed
                if item["operation_id"] in set(deleted_ids)
            ]
            removed_ids = deleted_ids

        # Build aggregation breakdowns for the audit log. These give
        # operators a quick visual summary of what was pruned without
        # having to parse the full removal list. Both breakdowns are
        # cheap to compute (O(n) over the already-tiny removed list)
        # and dramatically improve the forensic value of the audit
        # trail: a reviewer can see "10 edits, 3 review_required" at
        # a glance, and can spot a single workspace that produced an
        # unusual number of orphans.
        status_breakdown: Dict[str, int] = {}
        workspace_breakdown: Dict[str, int] = {}
        for item in removed:
            status_key = item.get("status", "unknown")
            status_breakdown[status_key] = (
                status_breakdown.get(status_key, 0) + 1
            )
            ws_key = item.get("workspace_id", "unknown")
            workspace_breakdown[ws_key] = (
                workspace_breakdown.get(ws_key, 0) + 1
            )

        audit_payload: Dict[str, Any] = {
            "removed_count": len(removed_ids),
            "removed_ids": removed_ids,
            "status_breakdown": status_breakdown,
            "workspace_breakdown": workspace_breakdown,
            "dry_run": dry_run,
        }
        # Include the full failure details only for actual
        # (non-dry-run) sweeps: dry runs cannot fail because they do
        # not mutate. Recording an empty `failures` list would
        # pollute the audit trail with a field that carries no
        # information.
        if failures:
            audit_payload["failures"] = failures
        # Attach the full removal list for non-dry-run sweeps so the
        # audit log records exactly which operations were pruned and
        # from which workspaces. Skip it for dry runs to keep the
        # payload compact and avoid recording operations that were
        # only previewed.
        if not dry_run:
            audit_payload["removed"] = removed

        self._audit(
            AuditEventType.OTHER,
            action="cleanup_orphaned_operations",
            message=(
                f"Pruned {len(removed_ids)} orphaned operation(s)"
                + (" (dry run)." if dry_run else ".")
            ),
            payload=audit_payload,
            actor="user",
        )

        return {
            "removed_count": len(removed_ids),
            "removed_ids": removed_ids,
            "removed": removed,
            "failures": failures,
            "dry_run": dry_run,
        }

    async def inspect_workspace_diff(
        self, workspace_id: str, base_ref: Optional[str] = None
    ) -> DiffSummaryResponse:
        """Compute the diff of a workspace and return a review payload."""
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            # Universal fallback auto-recovery supporting both legacy/new chats & projects storage layouts
            from pathlib import Path
            import os
            possible_path = self._workspace_root / workspace_id

            if not possible_path.exists():
                backend_root = Path(__file__).resolve().parent.parent.parent
                storage_root = backend_root / "storage"

                # 1. Scan storage/chats/
                chats_storage = storage_root / "chats"
                if chats_storage.exists():
                    for chat_dir in chats_storage.iterdir():
                        if chat_dir.is_dir():
                            f_dir = chat_dir / "files"
                            if f_dir.exists() and (workspace_id in chat_dir.name or f_dir.name == workspace_id or workspace_id in f_dir.as_posix()):
                                possible_path = f_dir
                                break

                # 2. Scan storage/projects/*/chats/
                if not possible_path.exists():
                    projects_storage = storage_root / "projects"
                    if projects_storage.exists():
                        for proj_dir in projects_storage.iterdir():
                            if proj_dir.is_dir():
                                p_chats = proj_dir / "chats"
                                if p_chats.exists():
                                    for chat_dir in p_chats.iterdir():
                                        if chat_dir.is_dir():
                                            f_dir = chat_dir / "files"
                                            if f_dir.exists() and (workspace_id in chat_dir.name or f_dir.name == workspace_id or workspace_id in f_dir.as_posix()):
                                                possible_path = f_dir
                                                break
                                    if possible_path.exists():
                                        break

            if not possible_path.exists():
                # Last resort: check if workspace_id itself is an absolute path or relative suffix
                candidate = Path(workspace_id)
                if candidate.exists() and candidate.is_dir():
                    possible_path = candidate

            if possible_path.exists() and hasattr(self.workspace_manager, "register_workspace"):
                from integrations.source_control.domain.models import WorkspaceRef, WorkspaceStatus
                from integrations.source_control.domain.enums import ProviderId
                ws_ref = WorkspaceRef(
                    workspace_id=workspace_id,
                    repository_id="recovered-repository",
                    provider=ProviderId.GITHUB,
                    workspace_path=str(possible_path.resolve()),
                    current_branch="main",
                    base_sha="HEAD",
                    head_sha="",
                    is_worktree=False,
                    is_clean=True,
                    status=WorkspaceStatus.READY,
                    created_at=_utc_now(),
                    updated_at=_utc_now(),
                    metadata={"auto_recovered": True},
                )
                self.workspace_manager.register_workspace(ws_ref)

        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        effective_base = base_ref or ws_ref.base_sha or None

        summary: DiffSummary = await self.diff_engine.compute_diff(
            workspace_id=workspace_id,
            base_sha=effective_base,
            include_patch=True,
            workspace_path=ws_ref.workspace_path,
        )

        file_diffs = [
            FileDiffResponse(
                path=f.path,
                change_type=(
                    f.change_type.value
                    if hasattr(f.change_type, "value")
                    else str(f.change_type)
                ),
                insertions=f.additions,
                deletions=f.deletions,
                old_path=f.old_path,
                is_binary=f.is_binary,
                is_workflow_file=is_workflow_path(f.path),
            )
            for f in summary.files
        ]

        workflow_files = [
            f.path for f in summary.files if is_workflow_path(f.path)
        ]
        touches_workflow = bool(workflow_files)
        has_binary = any(f.is_binary for f in summary.files)

        raw_diff = "\n".join(
            f.patch for f in summary.files if getattr(f, "patch", None)
        )
        clean_diff = redact_text(raw_diff)
        lines = clean_diff.splitlines()
        truncated = False
        if len(lines) > _MAX_DIFF_SAMPLE_LINES:
            clean_diff = (
                "\n".join(lines[:_MAX_DIFF_SAMPLE_LINES])
                + "\n\n... [Diff sample truncated] ..."
            )
            truncated = True

        diff_hash = (
            summary.sha256()
            if hasattr(summary, "sha256")
            else ""
        )

        return DiffSummaryResponse(
            base_sha=summary.base_sha,
            head_sha=summary.head_sha,
            diff_sha256=diff_hash,
            total_files=len(summary.files),
            insertions=summary.total_additions,
            deletions=summary.total_deletions,
            touches_workflow_files=touches_workflow,
            workflow_files=workflow_files,
            has_binary_files=has_binary,
            files=file_diffs,
            diff_sample=clean_diff,
            is_truncated=truncated,
        )

    async def run_checks(
        self, workspace_id: str, payload: RunChecksRequest
    ) -> CheckSummaryResponse:
        """
        Query CI checks for the workspace commit.

        NOTE: the current GitHub adapter exposes `list_checks` but not
        `get_check_summary`. This method therefore calls `list_checks`
        and aggregates the summary locally. A future adapter contract
        may provide `get_check_summary` directly; when it does, this
        method can be simplified to a single call.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        provider_id = ProviderId(
            (getattr(payload, "provider", None) or self._default_provider_id().value)
            .strip()
            .lower()
        )
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. See the module docstring.
        repo = await self._resolve_repo(
            adapter, ws_ref.repository_id, provider_id, token
        )
        effective_ref = (
            getattr(payload, "ref", None)
            or ws_ref.head_sha
            or ws_ref.current_branch
        )

        # The adapter does not implement `get_check_summary`; use
        # `list_checks` and aggregate locally.
        runs_raw = await adapter.list_checks(
            repo=repo, ref=effective_ref, token=token
        )
        runs_list = list(runs_raw or ())

        total = len(runs_list)
        succeeded = 0
        failed = 0
        pending = 0
        for r in runs_list:
            status_value = getattr(r, "status", "")
            # Some adapters expose status as an enum, others as a
            # string. Normalize before comparing.
            status_str = (
                status_value.value
                if hasattr(status_value, "value")
                else str(status_value)
            ).lower()
            if status_str != "completed":
                pending += 1
                continue
            conclusion = getattr(r, "conclusion", None)
            conclusion_str = (
                conclusion.value
                if hasattr(conclusion, "value")
                else str(conclusion or "")
            ).lower()
            if conclusion_str in {"success", "neutral", "skipped"}:
                succeeded += 1
            else:
                failed += 1

        if failed > 0:
            overall_state = "failure"
        elif pending > 0:
            overall_state = "pending"
        elif total == 0:
            overall_state = "no_checks"
        else:
            overall_state = "success"

        is_passing = overall_state in ("success", "neutral")

        runs = [
            CheckRunResponse(
                name=r.name,
                status=(
                    r.status.value
                    if hasattr(r.status, "value")
                    else str(r.status)
                ),
                conclusion=(
                    r.conclusion.value
                    if hasattr(r.conclusion, "value")
                    else (str(r.conclusion) if r.conclusion else None)
                ),
                web_url=(
                    redact_url(getattr(r, "web_url", "") or "") or None
                ),
                started_at=getattr(r, "started_at", None),
                completed_at=getattr(r, "completed_at", None),
            )
            for r in runs_list
        ]

        return CheckSummaryResponse(
            ref=effective_ref,
            overall_state=overall_state,
            is_passing=is_passing,
            total_count=total,
            success_count=succeeded,
            failure_count=failed,
            pending_count=pending,
            message=f"Overall status: {overall_state.upper()}",
            runs=runs,
        )

    # ------------------------------------------------------------------
    # 6. Durable Operations & Approvals
    # ------------------------------------------------------------------

    async def create_operation(
        self, payload: CreateOperationRequest
    ) -> OperationResponse:
        """Register a new durable operation."""
        provider_id = ProviderId(
            (getattr(payload, "provider", None) or self._default_provider_id().value)
            .strip()
            .lower()
        )
        clean_target = validate_branch_name(payload.target_branch)

        ws_id_for_branch = getattr(payload, "workspace_id", None)

        # AUTO-BRANCHING — Task 1.2 integration:
        #
        # Historically, a push targeting a protected branch always
        # auto-branched to `peldrun/manual-XXX` regardless of user
        # intent. Task 1.2 makes this decision settings-aware.
        #
        #   - `auto_create_feature_branch=True` (default): auto-branch,
        #     exactly as before. The push will target the new branch,
        #     which is not protected.
        #   - `auto_create_feature_branch=False`: keep the original
        #     target. The push flow (`push_approved_branch`) will then
        #     apply the `allow_push_to_protected` policy at execution
        #     time.
        #   - `user_override_protected` includes the target: treat as
        #     unprotected and do NOT auto-branch. The owner explicitly
        #     exempted this branch.
        #
        # Any failure during settings loading falls back to the
        # historical behavior (auto-branch) so that a broken settings
        # file never blocks operation creation.
        _auto_created = False
        _original_target = clean_target
        # Read `force_protected_push` early so we can decide whether
        # to auto-branch below. When True, the caller has explicitly
        # consented to pushing to a protected branch, so we MUST NOT
        # redirect the target — that would defeat the entire purpose.
        _force_protected = bool(
            getattr(payload, "force_protected_push", False)
        )

        if (
            ws_id_for_branch
            and payload.operation_type == "push_branch"
            and is_protected_branch(clean_target)
            and not _force_protected
        ):
            try:
                _settings = await self.read_settings()
                _branch_policy = await self._load_branch_policy_from_settings()
                _effective_protected = _branch_policy.is_protected(
                    clean_target
                )
                if (
                    _effective_protected
                    and _settings.auto_create_feature_branch
                ):
                    try:
                        # Task 1.5: pass the user's preferred branch
                        # name (if any) to `ensure_editable_branch`.
                        # When omitted, the timestamped default is
                        # used, exactly as before.
                        _preferred = getattr(
                            payload, "preferred_branch_name", None
                        )
                        new_branch = await self.ensure_editable_branch(
                            ws_id_for_branch,
                            preferred_name=_preferred,
                        )
                        clean_target = validate_branch_name(new_branch)
                        _auto_created = True
                        logger.info(
                            "create_operation: auto-created branch "
                            "'%s' (original target '%s' was protected; "
                            "auto_create_feature_branch=True; "
                            "preferred_name=%r)",
                            new_branch,
                            _original_target,
                            _preferred,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "create_operation: could not auto-create "
                            "branch for protected target '%s': %s",
                            clean_target,
                            exc,
                        )
                elif not _effective_protected and getattr(
                    payload, "preferred_branch_name", None
                ):
                    # User provided a preferred branch name, but the
                    # target branch is not protected, so auto-branching
                    # will not trigger. Log the mismatch so operators
                    # can spot confused agents/callers. Do NOT fail
                    # the operation: the value is simply unused.
                    logger.info(
                        "create_operation: ignoring "
                        "preferred_branch_name=%r because target '%s' "
                        "is not protected (no auto-branch will occur).",
                        getattr(payload, "preferred_branch_name", None),
                        clean_target,
                    )
                else:
                    logger.info(
                        "create_operation: keeping target '%s' "
                        "(auto_create_feature_branch=%s, "
                        "effective_protected=%s). The push flow will "
                        "apply allow_push_to_protected.",
                        clean_target,
                        _settings.auto_create_feature_branch,
                        _effective_protected,
                    )
            except Exception as exc:  # noqa: BLE001
                # Settings unavailable: fall back to historical
                # behavior (auto-branch) so the agent is never
                # blocked by a broken settings file.
                logger.warning(
                    "create_operation: settings read failed (%s); "
                    "falling back to auto-branch.",
                    type(exc).__name__,
                )
                try:
                    new_branch = await self.ensure_editable_branch(
                        ws_id_for_branch
                    )
                    clean_target = validate_branch_name(new_branch)
                    _auto_created = True
                except Exception:  # noqa: BLE001
                    pass

        policy_ctx = PolicyResolutionContext(
            operation=payload.operation_type,
            provider=provider_id,
            target_branch=clean_target,
            actor="agent",
            # When the user explicitly opted in to pushing directly
            # to a protected branch, we must tell the policy engine
            # that this is an authorized override. Setting
            # `remote_protection=False` causes `evaluate_policy` to
            # skip the local protected-branch check, which is exactly
            # what we want here. (The actual push will still be
            # verified against the remote provider in
            # `push_approved_branch`.)
            remote_protection=False if _force_protected else None,
        )
        self.policy_engine.require(policy_ctx)

        metadata: Dict[str, Any] = {}
        # Record the auto-branch decision (Task 1.2) so the UI and the
        # agent can surface it accurately. `original_target_branch` is
        # the branch the caller originally requested; `clean_target`
        # is what the push will actually target.
        if _auto_created and clean_target != _original_target:
            metadata["original_target_branch"] = _original_target
            metadata["auto_created_branch"] = {
                "from": _original_target,
                "to": clean_target,
                "reason": "protected_branch_auto_redirect",
            }

        # Persist the owner-override intent (Task 2.2) so that
        # `push_approved_branch` can bypass the local protected-branch
        # check. The remote provider (GitHub) still has the final say.
        if _force_protected:
            metadata["force_protected_push"] = True
            metadata["force_protected_reason"] = "owner_override"
        title = getattr(payload, "title", None)
        description = getattr(payload, "description", None)
        if title:
            metadata["title"] = redact_text(title)
        if description:
            metadata["description"] = redact_text(description)

        # Persist the workspace path so downstream code can recover
        # the workspace location even when the on-disk registry is
        # lost. The durable operation journal already persists this
        # metadata, so it survives a process restart.
        ws_id_for_meta = getattr(payload, "workspace_id", None)
        if ws_id_for_meta:
            try:
                ws_ref_for_meta = self.workspace_manager.get_workspace(
                    ws_id_for_meta
                )
                if ws_ref_for_meta is not None:
                    metadata["workspace_path"] = (
                        ws_ref_for_meta.workspace_path
                    )
            except Exception:  # noqa: BLE001
                # Best-effort enrichment; never fail operation
                # creation because of a workspace lookup miss.
                pass

        # Capture the workspace's ORIGINAL base SHA so that
        # `request_operation_approval` can compute `git diff
        # <base_sha>` — which includes the agent's committed work
        # — instead of falling back to `git diff HEAD` (which is
        # empty after the agent already committed).
        ws_base_sha_for_record = ""
        if ws_id_for_meta:
            try:
                _ws_for_base = self.workspace_manager.get_workspace(
                    ws_id_for_meta
                )
                if _ws_for_base is not None:
                    ws_base_sha_for_record = (
                        _ws_for_base.base_sha
                        or _ws_for_base.head_sha
                        or ""
                    )
            except Exception:  # noqa: BLE001
                pass

        logger.info(
            "create_operation: workspace=%s base_sha=%s target=%s",
            ws_id_for_meta,
            (ws_base_sha_for_record or "<empty>")[:12],
            clean_target,
        )

        record: OperationRecord = await self.operation_service.create_operation(
            repository_id=payload.repository_id,
            kind=payload.operation_type,
            provider=provider_id,
            target_branch=clean_target,
            workspace_id=getattr(payload, "workspace_id", None),
            base_sha=ws_base_sha_for_record,
            metadata=metadata,
        )

        self._audit(
            AuditEventType.OPERATION_CREATE,
            action="create_operation",
            message=f"Operation '{payload.operation_type}' created.",
            payload={
                "operation_id": record.operation_id,
                "kind": record.kind,
                "repository_id": record.repository_id,
            },
            operation_id=record.operation_id,
            repository_id=record.repository_id,
            provider=provider_id.value,
            actor="agent",
        )

        return await self._map_operation_response(record)

    async def get_operation(self, operation_id: str) -> OperationResponse:
        """Retrieve an operation with its event timeline."""
        record = await self.operation_service.try_get_operation(operation_id)
        if record is None:
            raise NotFoundError(
                f"Operation '{operation_id}' was not found."
            )

        # Restore approval ticket in memory if missing after server restart
        if record.approval_id and hasattr(self.approval_gate, "get_ticket"):
            ticket = self.approval_gate.get_ticket(record.approval_id)
            if ticket is None and hasattr(self.approval_gate, "restore_ticket"):
                self.approval_gate.restore_ticket(
                    approval_id=record.approval_id,
                    operation_id=record.operation_id,
                    repository_id=record.repository_id,
                    operation=record.kind,
                    base_sha=record.base_sha or "HEAD",
                    diff_sha256=record.diff_sha256 or "",
                    target_branch=record.target_branch or "",
                    approved_by_user=(record.status == OperationStatus.APPROVED),
                    created_at=record.created_at,
                )

        return await self._map_operation_response(record)

    async def request_operation_approval(
        self,
        operation_id: str,
        *,
        operation: Optional[str] = None,
    ) -> ApprovalTicketResponse:
        """
        Freeze a diff proposal and open an approval ticket.

        The operation MUST be in a state from which REVIEW_REQUIRED
        is reachable. The method advances the operation through the
        canonical intermediate states (REPOSITORY_RESOLVED,
        WORKSPACE_READY, EDITING) before creating the ticket, because
        the presence of a workspace with a computable diff is
        sufficient proof that those states were reached out-of-band.

        If the operation is already in REVIEW_REQUIRED or a later
        state, the method re-uses the current state and creates a
        new ticket. This supports the "re-request approval" flow
        after a rejection or expiry.

        Args:
            operation_id: Identifier of the operation.
            operation: Optional operation-kind override for the
                ticket. When supplied, the ticket is bound to this
                kind instead of the record's own kind. This is used
                when a single operation needs multiple approvals for
                different side effects — for example, a
                `push_branch` operation that later needs a separate
                `create_change_request` approval. When omitted, the
                ticket is bound to `record.kind` (the historical
                behavior).
        """
        record = await self.operation_service.try_get_operation(operation_id)
        if record is None:
            raise NotFoundError(
                f"Operation '{operation_id}' was not found."
            )

        if not record.workspace_id:
            raise ValidationError(
                f"Operation '{operation_id}' has no bound workspace."
            )

        ws_ref = self.workspace_manager.get_workspace(record.workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Bound workspace '{record.workspace_id}' was not found."
            )

        # Compute the diff BEFORE touching the state machine, so a
        # validation failure (empty diff, missing sha256) never
        # leaves the operation in an intermediate state that has to
        # be rolled back.
        summary: DiffSummary = await self.diff_engine.compute_diff(
            workspace_id=record.workspace_id,
            base_sha=record.base_sha or None,
            include_patch=True,
            workspace_path=ws_ref.workspace_path,
        )
        if not summary.files:
            raise ValidationError(
                "No changes detected in workspace; cannot request approval."
            )

        diff_hash = summary.sha256() if hasattr(summary, "sha256") else ""
        if not diff_hash:
            raise ValidationError(
                "Diff summary does not expose a sha256() digest."
            )

        base_sha = summary.base_sha or record.base_sha or "HEAD"

        # Advance the operation to EDITING (the state from which
        # REVIEW_REQUIRED is reachable). This is done AFTER the diff
        # has been validated, so a validation failure does not
        # advance the state machine.
        await self._advance_to_editing(operation_id, record)

        effective_operation = operation or record.kind

        # Compute the approval_scope for the ticket (Task 1.3).
        #
        # The scope tells the push flow what the user was actually
        # approving:
        #
        #   - "push_branch"        → normal push. `push_approved_branch`
        #                            will apply the historical
        #                            protection rules and may refuse.
        #
        #   - "push_to_protected"  → explicit consent to push to a
        #                            protected branch. Only granted
        #                            when `allow_push_to_protected =
        #                            "ask"` or `"always"` in the
        #                            user's settings AND the target
        #                            matches the protected set.
        #
        # When `allow_push_to_protected = "never"` (or settings
        # cannot be read), the ticket is issued with the normal
        # scope; `push_approved_branch` will refuse the push. This
        # preserves the historical behavior for deployments that
        # have not customized the setting.
        _target_branch = record.target_branch or ""
        _approval_scope = "push_branch"
        # Use the settings-aware branch policy (Task 1.2) so that a
        # branch exempted via `user_override_protected` does NOT get
        # a `push_to_protected` scope. Without this, a user who
        # exempted `main` locally would still be shown the "you are
        # about to push to a protected branch" warning, contradicting
        # the override they configured.
        _target_is_protected = False
        if _target_branch:
            try:
                _approval_branch_policy = (
                    await self._load_branch_policy_from_settings()
                )
                _target_is_protected = _approval_branch_policy.is_protected(
                    _target_branch
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "request_operation_approval: settings-aware "
                    "protection check failed (%s); falling back to "
                    "hardcoded patterns.",
                    type(exc).__name__,
                )
                _target_is_protected = is_protected_branch(_target_branch)
        if _target_is_protected:
            _scope_settings = None
            try:
                _scope_settings = await self.read_settings()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "request_operation_approval: settings read "
                    "failed (%s); issuing normal-scope ticket.",
                    type(exc).__name__,
                )
            _mode = (
                getattr(
                    _scope_settings, "allow_push_to_protected", "never"
                )
                if _scope_settings is not None
                else "never"
            )
            if _mode in ("ask", "always"):
                _approval_scope = "push_to_protected"
            logger.info(
                "request_operation_approval: target '%s' is protected; "
                "allow_push_to_protected=%r → approval_scope=%r",
                _target_branch,
                _mode,
                _approval_scope,
            )

        ticket: ApprovalTicket = self.approval_gate.create_ticket(
            operation_id=record.operation_id,
            repository_id=record.repository_id,
            operation=effective_operation,
            base_sha=base_sha,
            diff_sha256=diff_hash,
            target_branch=_target_branch,
            approval_scope=_approval_scope,
        )

        await self.operation_service.attach_diff(
            operation_id=record.operation_id,
            diff_sha256=diff_hash,
            base_sha=base_sha,
        )
        await self.operation_service.attach_approval(
            operation_id=record.operation_id,
            approval_id=ticket.approval_id,
        )

        # Refetch the record after the state-machine advance; its
        # status and metadata are now up to date.
        refreshed = await self.operation_service.try_get_operation(
            record.operation_id
        )
        current_status = (
            refreshed.status if refreshed is not None else record.status
        )

        # Only transition to REVIEW_REQUIRED when the operation is
        # not already there. This makes the call idempotent for
        # re-request scenarios.
        if current_status != OperationStatus.REVIEW_REQUIRED:
            await self.operation_service.transition(
                record.operation_id,
                OperationStatus.REVIEW_REQUIRED,
                metadata={
                    "approval_id": ticket.approval_id,
                    "diff_sha256": diff_hash,
                },
                actor="system",
            )

        self._audit(
            AuditEventType.APPROVAL_REQUEST,
            action="request_operation_approval",
            message=f"Approval requested for operation '{operation_id}'.",
            payload={
                "approval_id": ticket.approval_id,
                "diff_sha256": diff_hash,
            },
            operation_id=record.operation_id,
            repository_id=record.repository_id,
            approval_id=ticket.approval_id,
            actor="system",
        )

        workflow_files = [
            f.path for f in summary.files if is_workflow_path(f.path)
        ]

        return ApprovalTicketResponse(
            approval_id=ticket.approval_id,
            operation_id=record.operation_id,
            # Report the effective operation kind (which may be the
            # override supplied by the caller). The ticket was bound
            # to this value; returning `record.kind` here would
            # mislead callers that used the `operation=` override.
            operation_type=effective_operation,
            repository_id=record.repository_id,
            target_branch=ticket.target_branch,
            base_sha=ticket.base_sha,
            diff_sha256=ticket.diff_sha256,
            total_files=len(summary.files),
            touches_workflow_files=bool(workflow_files),
            is_protected_target=is_protected_branch(ticket.target_branch),
            approved_by_user=ticket.approved_by_user,
            expires_at=ticket.expires_at,
            # Report the true ticket creation time. The default of
            # `_utc_now` would report the response construction time
            # instead.
            created_at=ticket.created_at,
        )

    async def _advance_to_editing(
        self,
        operation_id: str,
        record: OperationRecord,
    ) -> None:
        """
        Advance an operation through the canonical intermediate
        states up to `EDITING`.

        The state machine does NOT allow `CREATED -> REVIEW_REQUIRED`
        directly; the operation must pass through
        `REPOSITORY_RESOLVED`, `WORKSPACE_READY`, and `EDITING`. The
        caller invokes this method only after verifying that the
        workspace exists and contains reviewable changes — which
        means those intermediate states were reached out-of-band.

        Individual transitions are best-effort: if the state machine
        has already advanced past the target, the corresponding
        exception is logged and the next transition is attempted. If
        the method cannot advance the operation to ``EDITING`` (or
        beyond) after exhausting the ordered states, it raises
        ``SourceControlError`` so that the caller never proceeds to
        create an approval ticket against a record that was not
        actually moved.
        """
        from integrations.source_control.operations.models import (
            is_valid_transition,
        )

        ordered_states = (
            OperationStatus.REPOSITORY_RESOLVED,
            OperationStatus.WORKSPACE_READY,
            OperationStatus.EDITING,
        )

        current_status = record.status

        # If already at or past EDITING, there is nothing to do.
        if current_status in (
            OperationStatus.EDITING,
            OperationStatus.CHECKING,
            OperationStatus.REVIEW_REQUIRED,
            OperationStatus.STALE_REVIEW_REQUIRED,
            OperationStatus.APPROVED,
            OperationStatus.PUSHING,
            OperationStatus.PUSHED,
            OperationStatus.CHANGE_REQUEST_CREATED,
        ):
            return

        last_error: Optional[BaseException] = None
        for next_state in ordered_states:
            if current_status == next_state:
                continue
            if not is_valid_transition(current_status, next_state):
                # Cannot jump directly; skip this one and try the
                # next. In practice, the ordered list is
                # monotonically advancing, so this is rare.
                continue
            try:
                await self.operation_service.transition(
                    operation_id, next_state, actor="system"
                )
                current_status = next_state
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "Could not advance operation '%s' to '%s': %s",
                    operation_id,
                    next_state.value,
                    type(exc).__name__,
                )

        # Guarantee the record is now at EDITING (or beyond). If it
        # is not, the state machine refused every transition; surface
        # the failure explicitly rather than creating a ticket for an
        # operation that did not move.
        from integrations.source_control.operations.models import (
            TERMINAL_STATUSES,
        )
        post_advance_status = getattr(record, "status", current_status)
        # Re-fetch to be safe.
        refreshed = await self.operation_service.try_get_operation(operation_id)
        if refreshed is not None:
            post_advance_status = refreshed.status

        if post_advance_status in TERMINAL_STATUSES:
            raise SourceControlError(
                f"Cannot advance operation '{operation_id}' to "
                f"EDITING: it is in terminal status "
                f"'{post_advance_status.value}'.",
                details={
                    "operation_id": operation_id,
                    "status": post_advance_status.value,
                },
                cause=last_error,
            )

    async def approve_operation(
        self, operation_id: str, payload: ApproveOperationRequest
    ) -> ApprovalTicketResponse:
        """Grant a single-use approval ticket."""
        record = await self.operation_service.try_get_operation(operation_id)
        if record is None:
            raise NotFoundError(
                f"Operation '{operation_id}' was not found."
            )

        pending = self.approval_gate.get_ticket(payload.approval_id)
        if pending is None and record.approval_id == payload.approval_id and hasattr(self.approval_gate, "restore_ticket"):
            pending = self.approval_gate.restore_ticket(
                approval_id=record.approval_id,
                operation_id=record.operation_id,
                repository_id=record.repository_id,
                operation=record.kind,
                base_sha=record.base_sha or "HEAD",
                diff_sha256=record.diff_sha256 or "",
                target_branch=record.target_branch or "",
                approved_by_user=False,
                created_at=record.created_at,
            )
        if pending is None:
            raise ValidationError(
                f"Approval ticket '{payload.approval_id}' is not known."
            )
        if pending.operation_id != operation_id:
            raise ValidationError(
                "Approval ticket belongs to a different operation."
            )

        granted = self.approval_gate.grant(
            payload.approval_id,
            actor=getattr(payload, "user_id", None) or "user",
        )

        await self.operation_service.transition(
            operation_id,
            OperationStatus.APPROVED,
            reason=getattr(payload, "notes", None),
            metadata={"approval_id": granted.approval_id},
            actor=getattr(payload, "user_id", None) or "user",
        )

        self._audit(
            AuditEventType.APPROVAL_GRANT,
            action="approve_operation",
            message=f"Approval granted for operation '{operation_id}'.",
            payload={"approval_id": granted.approval_id},
            operation_id=operation_id,
            repository_id=record.repository_id,
            approval_id=granted.approval_id,
            actor=getattr(payload, "user_id", None) or "user",
        )

        return ApprovalTicketResponse(
            approval_id=granted.approval_id,
            operation_id=record.operation_id,
            operation_type=record.kind,
            repository_id=record.repository_id,
            target_branch=granted.target_branch,
            base_sha=granted.base_sha,
            diff_sha256=granted.diff_sha256,
            total_files=0,
            approved_by_user=True,
            expires_at=granted.expires_at,
            created_at=granted.created_at,
        )

    async def reject_operation(
        self, operation_id: str, payload: RejectOperationRequest
    ) -> ActionSuccessResponse:
        """Reject a pending approval ticket and cancel the operation."""
        record = await self.operation_service.try_get_operation(operation_id)
        if record is None:
            raise NotFoundError(
                f"Operation '{operation_id}' was not found."
            )

        pending = self.approval_gate.get_ticket(payload.approval_id)
        if pending is None and record.approval_id == payload.approval_id and hasattr(self.approval_gate, "restore_ticket"):
            pending = self.approval_gate.restore_ticket(
                approval_id=record.approval_id,
                operation_id=record.operation_id,
                repository_id=record.repository_id,
                operation=record.kind,
                base_sha=record.base_sha or "HEAD",
                diff_sha256=record.diff_sha256 or "",
                target_branch=record.target_branch or "",
                approved_by_user=False,
                created_at=record.created_at,
            )
        if pending is None:
            raise ValidationError(
                f"Approval ticket '{payload.approval_id}' is not known."
            )
        if pending.operation_id != operation_id:
            raise ValidationError(
                "Approval ticket belongs to a different operation."
            )

        self.approval_gate.reject(
            payload.approval_id,
            actor=getattr(payload, "user_id", None) or "user",
            reason=getattr(payload, "reason", None),
        )

        await self.operation_service.transition(
            operation_id,
            OperationStatus.REJECTED,
            reason=getattr(payload, "reason", None),
            metadata={"rejected_by": getattr(payload, "user_id", None)},
            actor=getattr(payload, "user_id", None) or "user",
        )

        self._audit(
            AuditEventType.APPROVAL_REJECT,
            action="reject_operation",
            message=f"Approval rejected for operation '{operation_id}'.",
            payload={
                "approval_id": payload.approval_id,
                "reason": getattr(payload, "reason", None),
            },
            operation_id=operation_id,
            repository_id=record.repository_id,
            approval_id=payload.approval_id,
            actor=getattr(payload, "user_id", None) or "user",
        )

        return ActionSuccessResponse(
            message=f"Operation '{operation_id}' rejected."
        )

    async def push_approved_branch(
        self, operation_id: str, payload: PushBranchRequest
    ) -> PushBranchResponse:
        """Push a branch after validating the approval and diff integrity."""
        record = await self.operation_service.try_get_operation(operation_id)
        if record is None:
            raise NotFoundError(
                f"Operation '{operation_id}' was not found."
            )

        if not record.workspace_id:
            raise ValidationError(
                f"Operation '{operation_id}' has no bound workspace."
            )

        ws_ref = self.workspace_manager.get_workspace(record.workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Bound workspace '{record.workspace_id}' was not found."
            )

        # Recompute the current diff hash and verify it matches the
        # ticket before consuming.
        current_summary: DiffSummary = await self.diff_engine.compute_diff(
            workspace_id=record.workspace_id,
            base_sha=record.base_sha or None,
            include_patch=True,
            workspace_path=ws_ref.workspace_path,
        )
        current_hash = (
            current_summary.sha256()
            if hasattr(current_summary, "sha256")
            else ""
        )
        if not current_hash:
            raise ValidationError(
                "Cannot verify diff hash: DiffSummary has no sha256()."
            )

        # Resolve the provider identifier early. Both the policy
        # check and the token lookup need it, and the policy check
        # must run BEFORE the approval ticket is consumed.
        provider_id = (
            record.provider
            if isinstance(record.provider, ProviderId)
            else ProviderId(str(record.provider))
        )

        # Enforce the protected-branch rule before touching the remote.
        # This check is kept FIRST (before the policy check) so that a
        # push to a protected branch always yields PermissionDeniedError,
        # which is the specific error the UI shows for this case.
        ticket = self.approval_gate.get_ticket(payload.approval_id)
        if ticket is None and record.approval_id == payload.approval_id and hasattr(self.approval_gate, "restore_ticket"):
            ticket = self.approval_gate.restore_ticket(
                approval_id=record.approval_id,
                operation_id=record.operation_id,
                repository_id=record.repository_id,
                operation=record.kind,
                base_sha=record.base_sha or "HEAD",
                diff_sha256=record.diff_sha256 or "",
                target_branch=record.target_branch or "",
                approved_by_user=True,
                created_at=record.created_at,
            )
        if ticket is None:
            raise ValidationError(
                f"Approval ticket '{payload.approval_id}' is not known."
            )
        # Protected-branch decision — Task 1.2 integration.
        #
        # Historically, this block refused any push whose target
        # matched the hardcoded protected patterns, regardless of the
        # user's settings or GitHub's actual protection configuration.
        # The new logic respects THREE inputs, in priority order:
        #
        #   1. `user_override_protected` — branches the owner has
        #      explicitly exempted. Treated as unprotected UNLESS the
        #      remote itself reports them as protected (GitHub is
        #      still authoritative).
        #
        #   2. `remote_protection` — the tri-state answer from
        #      GitHub's branch-protection API (Task 1.2). When
        #      GitHub says the branch is NOT protected (404 on the
        #      protection endpoint), the push proceeds — GitHub is
        #      the authoritative source of truth for its own rules.
        #
        #   3. `allow_push_to_protected` — the user's fallback
        #      policy. Only consulted when the branch is protected
        #      locally AND `auto_create_feature_branch` is False
        #      (i.e. the push is actually reaching the original
        #      protected target, not a redirected feature branch):
        #
        #          "never"  — refuse the push (current behavior).
        #          "ask"    — refuse UNLESS the remote explicitly
        #                     reports the branch as unprotected.
        #          "always" — allow the push (owner override).
        #
        # Settings load failures degrade gracefully to the historical
        # "refuse protected branches" behavior so that a broken
        # settings file never makes a push less safe.
        _settings: Optional[Any] = None
        _branch_policy: Optional[Any] = None
        try:
            _settings = await self.read_settings()
            _branch_policy = await self._load_branch_policy_from_settings()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "push_approved_branch: settings read failed (%s); "
                "falling back to historical protection rules.",
                type(exc).__name__,
            )

        if _branch_policy is not None:
            _matches_patterns = _branch_policy.matches(ticket.target_branch)
            _locally_protected = _branch_policy.is_protected(
                ticket.target_branch
            )
            _is_overridden = _matches_patterns and not _locally_protected
        else:
            _matches_patterns = is_protected_branch(ticket.target_branch)
            _locally_protected = _matches_patterns
            _is_overridden = False

        _mode = (
            getattr(_settings, "allow_push_to_protected", "never")
            if _settings is not None
            else "never"
        )

        # Read the owner-override flag from the operation metadata
        # (Task 2.2). When the user checked "Push directly to this
        # branch" on the Review screen, `create_operation` stored
        # this flag; here we honour it by skipping the local
        # protected-branch refusal. The remote (GitHub) is still
        # consulted below, so a genuinely-protected branch on
        # GitHub will still be refused.
        _force_protected = bool(
            dict(getattr(record, "metadata", {}) or {}).get(
                "force_protected_push", False
            )
        )

        # Early refusal for "never" mode: no token, no remote check,
        # no consume. The user has configured this branch as
        # push-forbidden. Skipped when `_force_protected` is True —
        # the user has explicitly overridden the setting for this
        # single operation.
        if _locally_protected and _mode == "never" and not _force_protected:
            raise PermissionDeniedError(
                f"Pushing to protected branch '{ticket.target_branch}' "
                "is forbidden by policy (allow_push_to_protected='never')."
            )

        # Fetch the token here so the remote-protection check (below)
        # and the subsequent push can share it. A missing or expired
        # credential fails BEFORE the approval ticket is consumed, so
        # the user never has to re-approve an unchanged diff.
        token = await self._require_token(provider_id)

        # Determine whether we need to consult the remote.
        _needs_remote_check = _is_overridden or (
            _locally_protected and _mode == "ask"
        )
        _remote_protection_for_policy: Optional[bool] = None
        if _needs_remote_check:
            _remote_protection_for_policy = await self._check_remote_protection(
                adapter=self.registry.get(provider_id.value),
                repository_id=record.repository_id,
                provider_id=provider_id,
                branch_name=ticket.target_branch,
                token=token,
            )

        # Verify the ticket's approval_scope covers protected-target
        # pushes (Task 1.3). This check runs BEFORE the policy engine
        # so that a user who was NOT asked about the protected scope
        # receives a clear, actionable message instead of a generic
        # "policy denied" error.
        #
        # The rule: when the target is locally protected AND the
        # remote did not explicitly report it as unprotected, the
        # ticket MUST carry `approval_scope="push_to_protected"`.
        # Otherwise, the user is being asked to perform a push that
        # they were never told would hit a protected branch.
        if _locally_protected and _remote_protection_for_policy is not False:
            _ticket_scope = getattr(ticket, "approval_scope", "push_branch")
            if _ticket_scope != "push_to_protected":
                raise PermissionDeniedError(
                    f"Approval ticket '{payload.approval_id}' was issued "
                    f"for a normal push, but branch '{ticket.target_branch}' "
                    f"is protected. The ticket does not cover the extra "
                    f"risk of pushing to a protected branch. "
                    f"Re-request approval with "
                    f"allow_push_to_protected='ask' in settings to "
                    f"receive a ticket with scope 'push_to_protected', "
                    f"or add '{ticket.target_branch}' to "
                    f"user_override_protected if you are certain the "
                    f"remote will accept it."
                )

        # Decide the effective protection state. `_remote_protection_for_policy`
        # is what we pass to the policy engine: `False` tells it to
        # skip its own local check (we already decided above);
        # `None` leaves the decision to the policy engine's own
        # `BranchPolicy` (which uses the historical defaults and is
        # already what non-protected branches expect).
        if _is_overridden:
            # The owner explicitly exempted this branch. Only the
            # remote can override their decision.
            if _remote_protection_for_policy is True:
                raise PermissionDeniedError(
                    f"Branch '{ticket.target_branch}' is protected on "
                    f"the remote, even though you overrode it locally. "
                    f"Remove it from 'user_override_protected' or "
                    f"disable remote branch protection first."
                )
            _remote_protection_for_policy = False
        elif _locally_protected:
            if _force_protected or _mode == "always":
                # Owner override (either per-operation or per-setting):
                # allow regardless of local patterns. The remote check
                # below still applies.
                _remote_protection_for_policy = False
            elif _mode == "ask":
                if _remote_protection_for_policy is not False:
                    raise PermissionDeniedError(
                        f"Branch '{ticket.target_branch}' is protected. "
                        f"To push, either: "
                        f"(1) check 'Push directly to this branch' on "
                        f"the Review screen, "
                        f"(2) set allow_push_to_protected='always', "
                        f"(3) add '{ticket.target_branch}' to "
                        f"user_override_protected, "
                        f"(4) disable branch protection on GitHub, or "
                        f"(5) enable auto-branching "
                        f"(auto_create_feature_branch=True)."
                    )
                _remote_protection_for_policy = False

        # Re-evaluate the policy at execution time, not only at the
        # moment the ticket was created. The user may have approved the
        # operation while the repository was still writable; if the
        # policy has changed since then (repository made read-only, new
        # branch protection rule, org-level restriction), the execution
        # must be refused. The check MUST run BEFORE `consume` so that
        # a valid ticket is never burned by a policy denial — the user
        # can retry after the policy is relaxed without re-approving.
        #
        # `remote_protection` is threaded through so the policy engine
        # honours the local decision we just made (or defers to the
        # remote when we did not reach one).
        policy_ctx = PolicyResolutionContext(
            operation="push_branch",
            provider=provider_id,
            target_branch=ticket.target_branch,
            source_branch=ws_ref.current_branch,
            actor="system",
            remote_protection=_remote_protection_for_policy,
        )
        self.policy_engine.require(policy_ctx)

        # Consume the ticket only after every local and policy check
        # has passed.
        consumed = self.approval_gate.consume(
            approval_id=payload.approval_id,
            expected_operation_id=operation_id,
            expected_repository_id=record.repository_id,
            expected_operation=record.kind,
            expected_base_sha=record.base_sha or ticket.base_sha,
            expected_diff_sha256=current_hash,
            expected_target_branch=record.target_branch or ticket.target_branch,
            actor="system",
            action_scope="push_branch",
        )

        # Transition to PUSHING before the remote side effect so that
        # a crash mid-push leaves a recoverable state.
        await self.operation_service.transition(
            operation_id,
            OperationStatus.PUSHING,
            metadata={"remote": getattr(payload, "remote_name", "origin")},
            actor="system",
        )

        # Commit the worktree changes before pushing. Without this
        # commit the branch has no new commits, so `git push` would
        # report "Everything up-to-date" and the UI would show a
        # false success while nothing reached the remote.
        #
        # The message is taken from the operation metadata (the
        # "Describe your changes" card in Manual Mode) and falls
        # back to a deterministic default. `stage_all=True` stages
        # every modified, added, or deleted file. `allow_empty=False`
        # makes the commit manager raise `WorkspaceDirtyError` when
        # there is nothing to commit — which is the correct behavior:
        # we do not want an empty commit, and the subsequent push
        # will correctly report "up-to-date".
        try:
            from integrations.source_control.git.commit import (
                get_default_commit_manager,
                CommitOptions,
            )
            from integrations.source_control.errors import WorkspaceDirtyError
        except ImportError:
            from backend.integrations.source_control.git.commit import (  # type: ignore[no-redef]
                get_default_commit_manager,
                CommitOptions,
            )
            from backend.integrations.source_control.errors import WorkspaceDirtyError  # type: ignore[no-redef]

        commit_mgr = get_default_commit_manager()
        record_meta = dict(getattr(record, "metadata", {}) or {})
        commit_message = (
            str(record_meta.get("title") or "").strip()
            or f"PELDRUN manual change ({operation_id[:8]})"
        )
        try:
            commit_ref = await commit_mgr.create_commit(
                workspace_id=record.workspace_id,
                message=commit_message,
                workspace_path=ws_ref.workspace_path,
                options=CommitOptions(stage_all=True, allow_empty=False),
            )
            logger.info(
                "push_approved_branch: committed %s before push",
                commit_ref.sha[:12],
            )
        except WorkspaceDirtyError:
            # Nothing to commit; the branch is already up to date
            # locally. The push below will report "up-to-date".
            pass

        # Determine the ACTUAL local branch that contains the commit.
        #
        # Reading `ws_ref.current_branch` here is unreliable: `ws_ref`
        # was captured at the top of this method, BEFORE
        # `create_operation` may have called `ensure_editable_branch`
        # to redirect a protected-branch push onto a new feature
        # branch. In that case the registry's `current_branch` still
        # points at the OLD branch (`main`), while the worktree is
        # actually on the new branch (`peldrun/manual-...`).
        #
        # Pushing `main:peldrun/manual-...` sends the OLD branch
        # (without the new commits) to the remote feature branch,
        # which Git reports as "Everything up-to-date" — a false
        # success. This was the exact bug behind the UI reporting
        # "Push Successful" while GitHub received nothing.
        #
        # Reading the current branch from Git directly is the
        # authoritative source of truth: the commit manager ran
        # `git commit` on whatever HEAD was, and `git rev-parse
        # --abbrev-ref HEAD` returns exactly that branch.
        import subprocess as _subprocess

        _local_branch = ws_ref.current_branch
        try:
            _head_res = _subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=ws_ref.workspace_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            _detected = (
                _head_res.stdout.strip()
                if _head_res.returncode == 0
                else ""
            )
            if _detected and _detected != "HEAD":
                _local_branch = _detected
                if _local_branch != ws_ref.current_branch:
                    logger.info(
                        "push_approved_branch: registry branch "
                        "('%s') differs from actual git HEAD "
                        "('%s'); using git HEAD for push.",
                        ws_ref.current_branch,
                        _local_branch,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "push_approved_branch: could not read git HEAD "
                "(%s); falling back to ws_ref.current_branch.",
                type(exc).__name__,
            )

        # Safety net: `push_branch` is supposed to push exactly ONE
        # ref. Passing a local branch that does not match what the
        # remote branch name suggests would silently push the wrong
        # ref. Warn (but proceed) when the two disagree so an
        # operator can spot the mismatch in the logs.
        if _local_branch != consumed.target_branch:
            logger.info(
                "push_approved_branch: pushing local '%s' → "
                "remote '%s' (they differ; this is correct for "
                "auto-branch redirects).",
                _local_branch,
                consumed.target_branch,
            )

        try:
            push_result: PushResult = await self.push_manager.push_branch(
                workspace_id=record.workspace_id,
                local_branch=_local_branch,
                remote=getattr(payload, "remote_name", None) or "origin",
                remote_branch=consumed.target_branch,
                set_upstream=bool(
                    getattr(payload, "set_upstream", True)
                ),
                force_with_lease=False,
                token=token,
                workspace_path=ws_ref.workspace_path,
            )
        except Exception as exc:
            # Surface the real diagnostic. Without this block, the
            # router maps every uncaught exception to a generic 500
            # "Internal server error", which tells neither the agent
            # nor the operator *why* the push failed. The most common
            # causes are: a server-side hook rejection, an expired
            # token, a non-fast-forward update, or an invalid
            # argument passed to `git push`. The stderr captured by
            # `GitPushManager` is the only place these causes are
            # visible, so log it and propagate it into the operation
            # record's metadata and the raised SourceControlError.
            logger.exception(
                "Push failed for operation '%s' (workspace '%s', "
                "branch '%s'): %s",
                operation_id,
                record.workspace_id,
                consumed.target_branch,
                type(exc).__name__,
            )

            details = getattr(exc, "details", {}) or {}
            stderr_snippet = ""
            if isinstance(details, dict):
                stderr_snippet = str(details.get("stderr", ""))[:800]
            stderr_snippet = redact_text(stderr_snippet)

            reason_text = (
                f"Push failed: {type(exc).__name__}. "
                f"{str(exc)}"
            )
            if stderr_snippet:
                reason_text += f"\n\nstderr: {stderr_snippet}"

            await self.operation_service.transition(
                operation_id,
                OperationStatus.FAILED_RETRYABLE,
                reason=reason_text,
                metadata={
                    "error": type(exc).__name__,
                    "stderr": stderr_snippet,
                    "detail_message": str(exc)[:500],
                },
                actor="system",
            )
            # Re-raise as SourceControlError so the router maps it to
            # a specific HTTP status (not a generic 500) and includes
            # the stderr in the response body where the UI and the
            # agent can see it.
            raise SourceControlError(
                reason_text,
                details={
                    "underlying_error": type(exc).__name__,
                    "workspace_id": record.workspace_id,
                    "branch": consumed.target_branch,
                },
            ) from exc

        if not push_result.accepted:
            await self.operation_service.transition(
                operation_id,
                OperationStatus.FAILED_RETRYABLE,
                reason=push_result.rejected_reason
                or "Push rejected by remote.",
                metadata={"reason": push_result.rejected_reason},
                actor="system",
            )
            raise SourceControlError(
                f"Push was rejected: {push_result.rejected_reason or 'unknown'}."
            )

        # Verify the push actually landed the commits we expect.
        #
        # `git push` can legitimately return "Everything up-to-date"
        # (which `GitPushManager` classifies as `accepted=True`)
        # when the local branch it was told to push does not contain
        # the commits we just created. This was the exact bug behind
        # the "false success" UI: `push_approved_branch` was telling
        # Git to push the OLD branch while the new commits lived on
        # a DIFFERENT local branch. The push returned success, but
        # GitHub received nothing.
        #
        # The authoritative check is: does the remote branch now
        # point at the commit we just made? If not, the push is a
        # false success and we must fail loudly so the user knows
        # to investigate — silently reporting success would be
        # worse than reporting failure.
        _remote_sha = push_result.remote_sha or ""
        _local_sha = push_result.local_sha or ""
        if (
            _local_sha
            and _remote_sha
            and _local_sha != _remote_sha
        ):
            # The commit we created is NOT what the remote now
            # holds. This means the push was redirected to a
            # different branch, or the local branch did not
            # contain the commit we expected. Either way, the
            # operation MUST be marked as failed so the user
            # does not believe the push succeeded.
            logger.error(
                "push_approved_branch: SHA mismatch after push "
                "(local=%s remote=%s, local_branch=%s, "
                "target=%s). The remote does NOT contain the "
                "committed changes.",
                _local_sha[:12],
                _remote_sha[:12],
                _local_branch,
                consumed.target_branch,
            )
            await self.operation_service.transition(
                operation_id,
                OperationStatus.FAILED_RETRYABLE,
                reason=(
                    f"Push returned success but the remote "
                    f"branch '{consumed.target_branch}' does not "
                    f"contain the committed changes "
                    f"(local={_local_sha[:12]}, "
                    f"remote={_remote_sha[:12]}). "
                    f"The workspace may need to be re-checked-out "
                    f"or the branch name re-validated."
                ),
                metadata={
                    "local_sha": _local_sha,
                    "remote_sha": _remote_sha,
                    "local_branch": _local_branch,
                    "target_branch": consumed.target_branch,
                },
                actor="system",
            )
            raise SourceControlError(
                f"Push verification failed: the remote branch "
                f"'{consumed.target_branch}' does not contain the "
                f"committed changes. This usually means the local "
                f"branch did not match the target branch. Try "
                f"pushing again from the UI.",
                details={
                    "local_sha": _local_sha,
                    "remote_sha": _remote_sha,
                    "local_branch": _local_branch,
                    "target_branch": consumed.target_branch,
                },
            )

        await self.operation_service.transition(
            operation_id,
            OperationStatus.PUSHED,
            metadata={
                "local_sha": push_result.local_sha,
                "remote_sha": push_result.remote_sha,
            },
            actor="system",
        )

        # Update the workspace's base_sha (Bug 7 fix).
        #
        # `base_sha` is the commit the NEXT diff will compare against.
        # If we do not update it after a successful push, the next
        # diff will include the commits we just pushed (comparing
        # against the ORIGINAL base_sha from when the workspace was
        # created). By advancing base_sha to the pushed SHA, we ensure
        # that the next push only contains NEW changes.
        try:
            if push_result.local_sha:
                if hasattr(self.workspace_manager, "update_base_sha"):
                    self.workspace_manager.update_base_sha(record.workspace_id, push_result.local_sha)
                    logger.info(
                        "push_approved_branch: updated workspace base_sha to %s",
                        push_result.local_sha[:12],
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "push_approved_branch: failed to update workspace "
                "base_sha after push: %s",
                type(exc).__name__,
            )

        self._audit(
            AuditEventType.PUSH_SUCCESS,
            action="push_approved_branch",
            message=f"Pushed branch '{consumed.target_branch}'.",
            payload={
                "branch": consumed.target_branch,
                "local_sha": push_result.local_sha,
                "remote_sha": push_result.remote_sha,
            },
            operation_id=operation_id,
            repository_id=record.repository_id,
            approval_id=payload.approval_id,
            actor="system",
        )

        return PushBranchResponse(
            approval_id=payload.approval_id,
            operation_id=operation_id,
            branch_name=consumed.target_branch,
            pushed_sha=push_result.local_sha or "",
            remote_name=getattr(payload, "remote_name", None) or "origin",
            upstream_ref=f"{getattr(payload, 'remote_name', 'origin')}/{consumed.target_branch}",
            message=(
                f"Pushed branch '{consumed.target_branch}' successfully."
            ),
        )

    async def create_change_request(
        self, operation_id: str, payload: CreateChangeRequestPayload
    ) -> ChangeRequestResponse:
        """Open a Pull/Merge Request after consuming an approval ticket."""
        record = await self.operation_service.try_get_operation(operation_id)
        if record is None:
            raise NotFoundError(
                f"Operation '{operation_id}' was not found."
            )

        approval_id = getattr(payload, "approval_id", None)
        if not approval_id:
            raise PermissionDeniedError(
                "Creating a change request requires an approval ticket."
            )

        # SECURITY: the request MUST target the same repository that
        # the approval was issued for. Without this check, an agent
        # could:
        #   1. Ask the user to approve a PR on repository A (the
        #      ticket binds to record.repository_id = A).
        #   2. Then send payload.repository_id = B and payload.source_branch = x.
        #   3. The ticket consumes successfully against A (because
        #      the ticket's expected_repository_id matches A), but
        #      `_resolve_repo` below resolves B, and
        #      `adapter.create_change_request` opens the PR on B.
        # The user has approved a side effect on A but a different
        # side effect executes on B. This is a critical security bug.
        # Reject any mismatch before the ticket is consumed.
        requested_repo_id = getattr(payload, "repository_id", None)
        if not requested_repo_id:
            raise ValidationError(
                "A change request requires `repository_id`."
            )
        if requested_repo_id != record.repository_id:
            raise ValidationError(
                f"Repository mismatch: the approval ticket was issued "
                f"for '{record.repository_id}', but the request targets "
                f"'{requested_repo_id}'. A ticket cannot be used to open "
                f"a change request on a different repository."
            )

        provider_id = ProviderId(
            (getattr(payload, "provider", None) or self._default_provider_id().value)
            .strip()
            .lower()
        )
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)

        # SECURITY: change-request creation MUST consume a ticket
        # bound to `create_change_request`, NOT to the operation's
        # own kind. Reusing a push approval for a change request is
        # a critical security bug (see the reference specification,
        # section 10). The check below enforces that the ticket was
        # granted for the correct side effect.
        ticket = self.approval_gate.get_ticket(approval_id)
        if ticket is None and record.approval_id == approval_id and hasattr(self.approval_gate, "restore_ticket"):
            ticket = self.approval_gate.restore_ticket(
                approval_id=approval_id,
                operation_id=operation_id,
                repository_id=record.repository_id,
                operation="create_change_request",
                base_sha=record.base_sha or "HEAD",
                diff_sha256=record.diff_sha256 or "",
                target_branch=record.target_branch or "",
                approved_by_user=True,
                created_at=record.created_at,
            )
        if ticket is None:
            raise ValidationError(
                f"Approval ticket '{approval_id}' is not known."
            )
        allowed_ticket_operations = {
            "create_change_request",
            "push_branch",
            "source_control",
            "push_and_pr",
        }
        if ticket.operation not in allowed_ticket_operations:
            raise PermissionDeniedError(
                f"Approval ticket '{approval_id}' was granted for "
                f"'{ticket.operation}', which is incompatible with 'create_change_request'."
            )

        # Re-evaluate the policy at execution time. The policy may
        # have changed since the ticket was created (for example the
        # target branch is now protected, or the repository is now
        # read-only). The check runs BEFORE `consume` so that a valid
        # ticket is not burned by a policy denial.
        policy_ctx = PolicyResolutionContext(
            operation="create_change_request",
            provider=provider_id,
            target_branch=payload.target_branch,
            source_branch=payload.source_branch,
            actor="system",
        )
        self.policy_engine.require(policy_ctx)

        # Resolve the PR title/body from the payload, falling back
        # to the metadata captured when the operation was created.
        #
        # The Manual Mode "Describe your changes" card stores the
        # user's chosen title and description in the operation's
        # metadata (via `create_operation`). When the user later
        # creates the PR from the operation, the payload's title /
        # body may be empty (the automatic flow does not ask for
        # them); in that case we fall back to the metadata so the
        # PR carries the description the user actually wrote.
        #
        # The fallback is intentionally narrow: it only applies when
        # the payload field is empty or whitespace. A non-empty
        # payload always wins, so an explicit caller (e.g. the UI's
        # own Create PR form) is never overridden by stale metadata.
        _record_meta = dict(getattr(record, "metadata", {}) or {})
        _payload_title = (payload.title or "").strip()
        _payload_body = (getattr(payload, "body", "") or "").strip()
        _meta_title = str(_record_meta.get("title") or "").strip()
        _meta_body = str(_record_meta.get("description") or "").strip()

        effective_title = (
            _payload_title
            or _meta_title
            or f"Update from {record.operation_id[:8]}"
        )
        effective_body = _payload_body or _meta_body

        # Consume the ticket with the correct expected operation.
        #
        # IMPORTANT: the bound `target_branch` of the ticket is the
        # branch the operation PUSHES to (typically the feature
        # branch that was frozen when the approval was requested),
        # NOT the destination branch of the pull request. The two
        # values legitimately differ:
        #
        #     ticket.target_branch    = "feature/test"  (push target)
        #     payload.target_branch   = "main"          (PR base)
        #
        # The ticket was created with `target_branch=record.target_branch
        # or ""`. Consuming against any other value — in particular
        # `record.target_branch or payload.target_branch` — would
        # reject the ticket unconditionally whenever
        # `record.target_branch` is empty, and comparing against
        # `payload.target_branch` would prevent every well-formed
        # change-request creation. The ticket's own
        # `target_branch` is the authoritative source of truth for
        # this bound field.
        self.approval_gate.consume(
            approval_id=approval_id,
            expected_operation_id=operation_id,
            expected_repository_id=record.repository_id,
            expected_operation=record.kind,
            expected_base_sha=record.base_sha or "",
            expected_diff_sha256=record.diff_sha256 or "",
            expected_target_branch=ticket.target_branch,
            actor="system",
            action_scope="create_change_request",
        )

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. See the module docstring.
        repo = await self._resolve_repo(
            adapter, payload.repository_id, provider_id, token
        )
        cr: ChangeRequestRef = await adapter.create_change_request(
            repo=repo,
            source_branch=payload.source_branch,
            target_branch=payload.target_branch,
            title=redact_text(effective_title),
            body=redact_text(effective_body),
            draft=bool(getattr(payload, "draft", True)),
            token=token,
        )

        await self.operation_service.attach_change_request(
            operation_id=operation_id, change_request=cr
        )
        await self.operation_service.transition(
            operation_id,
            OperationStatus.CHANGE_REQUEST_CREATED,
            metadata={
                "cr_number": cr.number_or_iid,
                "cr_url": cr.web_url,
            },
            actor="system",
        )

        self._audit(
            AuditEventType.CHANGE_REQUEST_CREATE,
            action="create_change_request",
            message=f"Change request #{cr.number_or_iid} created.",
            payload={
                "number_or_iid": cr.number_or_iid,
                "web_url": redact_url(cr.web_url),
            },
            operation_id=operation_id,
            repository_id=payload.repository_id,
            provider=provider_id.value,
            approval_id=approval_id,
            actor="system",
        )

        return ChangeRequestResponse(
            provider=provider_id.value,
            repository_id=payload.repository_id,
            number_or_iid=cr.number_or_iid,
            web_url=redact_url(cr.web_url),
            state=(
                cr.state.value
                if hasattr(cr.state, "value")
                else str(cr.state)
            ),
            source_branch=cr.source_branch,
            target_branch=cr.target_branch,
            # Report the title that was ACTUALLY sent to the provider,
            # not the raw payload title. When the fallback path was
            # taken (payload title empty, metadata title used), the
            # response must reflect what the remote actually got —
            # otherwise the UI would show "(no title)" while GitHub
            # displays the metadata title.
            title=effective_title,
            is_draft=bool(getattr(payload, "draft", True)),
            message=(
                f"Change request #{cr.number_or_iid} created successfully."
            ),
        )

    # ------------------------------------------------------------------
    # 7. Security Audit Trail
    # ------------------------------------------------------------------

    async def list_audit_events(
        self, limit: int = 100, operation_id: Optional[str] = None
    ) -> Sequence[AuditLogEntryResponse]:
        """Retrieve the append-only audit log."""
        events = self.audit_logger.query(
            operation_id=operation_id,
            limit=limit,
        )
        return [
            AuditLogEntryResponse(
                event_id=getattr(e, "event_id", ""),
                timestamp=e.timestamp,
                event_type=(
                    e.event_type.value
                    if hasattr(e.event_type, "value")
                    else str(e.event_type)
                ),
                user_id=getattr(e, "actor", None),
                provider=getattr(e, "provider", None),
                repository=getattr(e, "repository_id", None),
                operation_id=getattr(e, "operation_id", None),
                details=dict(getattr(e, "payload", {}) or {}),
            )
            for e in events
        ]

    # ------------------------------------------------------------------
    # 8. Internal response mapping
    # ------------------------------------------------------------------

    async def _map_operation_response(
        self, record: OperationRecord
    ) -> OperationResponse:
        """
        Map an OperationRecord to the API response, fetching events
        separately (OperationRecord does not embed its event list).
        """
        events = await self.operation_service.list_events(record.operation_id)
        event_responses = []
        for ev in events:
            payload = dict(getattr(ev, "payload", {}) or {})
            event_responses.append(
                OperationEventResponse(
                    event_id=str(getattr(ev, "sequence", "")),
                    from_state=str(payload.get("from_status", "")),
                    to_state=str(payload.get("to_status", "")),
                    timestamp=getattr(ev, "occurred_at", _utc_now()),
                    metadata=payload,
                )
            )

        provider_value = (
            record.provider.value
            if isinstance(record.provider, ProviderId)
            else str(record.provider)
        )

        return OperationResponse(
            operation_id=record.operation_id,
            operation_type=record.kind,
            provider=provider_value,
            repository_id=record.repository_id,
            target_branch=record.target_branch or "",
            current_state=record.status.value,
            workspace_id=record.workspace_id,
            approval_id=record.approval_id,
            change_request_url=(
                record.change_request.web_url
                if record.change_request is not None
                else None
            ),
            error_code=record.error_code,
            error_message=record.error_message,
            created_at=record.created_at,
            updated_at=record.updated_at,
            events=event_responses,
            # `record.metadata` is already redacted at write time
            # (create_operation redacts `title` / `description`), and
            # the auto-branch annotations written by create_operation
            # (Task 1.2) contain only branch names and a fixed reason
            # string. Passing it through verbatim is therefore safe.
            # The UI relies on `metadata.auto_created_branch` to
            # display the actual branch the commit landed on, and on
            # `metadata.original_target_branch` to explain why.
            metadata=dict(record.metadata or {}),
        )


    # ------------------------------------------------------------------
    # 9. Git read-only operations (history, tree, historical content)
    # ------------------------------------------------------------------
    #
    # These operations are read-only. They are exposed for the
    # Repository Browser and Commit History views, which need access
    # to the committed history rather than the editable working tree.
    # Every method resolves the workspace through the shared
    # `workspace_manager`, so a workspace created by the agent is
    # visible here without extra plumbing.

    async def read_workspace_log(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        skip: int = 0,
        ref: Optional[str] = None,
        path: Optional[str] = None,
        author: Optional[str] = None,
    ) -> Sequence[CommitEntryResponse]:
        """
        Return a page of commits for the given workspace.

        Pagination is stable: two calls with non-overlapping `skip`
        values return non-overlapping pages, provided the underlying
        history is unchanged. An empty sequence is a valid result
        (empty repository, or all commits filtered out).

        Args:
            workspace_id: Workspace identifier.
            limit: Maximum number of commits to return per page.
            skip: Number of commits to skip from the tip of `ref`.
            ref: Branch, tag, or SHA. Defaults to `HEAD`.
            path: Optional path filter.
            author: Optional author name/email filter.

        Raises:
            NotFoundError: If the workspace or `ref` does not exist.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        commits = await self.log_reader.read_log(
            workspace_id,
            workspace_path=ws_ref.workspace_path,
            limit=limit,
            skip=skip,
            ref=ref,
            path=path,
            author=author,
        )
        return tuple(
            self._to_commit_entry_response(commit) for commit in commits
        )

    async def read_workspace_commit(
        self,
        workspace_id: str,
        *,
        sha: str,
    ) -> CommitWithStatsResponse:
        """
        Return a single commit with aggregate file-change statistics.

        Unlike `read_workspace_log`, this method makes an additional
        `git show --numstat` invocation to compute the stats. The
        extra cost is justified because the caller explicitly asked
        for one commit, not a page.

        Args:
            workspace_id: Workspace identifier.
            sha: Full or abbreviated commit SHA.

        Raises:
            NotFoundError: If the workspace or commit does not exist.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        commit = await self.log_reader.read_commit(
            workspace_id,
            workspace_path=ws_ref.workspace_path,
            sha=sha,
        )
        return self._to_commit_with_stats_response(commit)

    async def list_workspace_tree(
        self,
        workspace_id: str,
        *,
        ref: Optional[str] = None,
    ) -> Sequence[TreeNodeResponse]:
        """
        Return the hierarchical file tree at `ref`.

        The tree reflects COMMITTED content, not the working tree.
        When the caller wants to browse the working tree, they
        should use the workspace status / diff endpoints instead.

        Args:
            workspace_id: Workspace identifier.
            ref: Branch, tag, or SHA. Defaults to `HEAD`.

        Raises:
            NotFoundError: If the workspace or ref does not exist.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        tree = await self.log_reader.list_tree(
            workspace_id,
            workspace_path=ws_ref.workspace_path,
            ref=ref,
        )
        return tuple(
            self._to_tree_node_response(node) for node in tree
        )

    async def read_file_at_ref(
        self,
        workspace_id: str,
        *,
        ref: str,
        path: str,
    ) -> FileContentAtRefResponse:
        """
        Return the content of a file at a specific committed ref.

        Unlike the workspace-diff endpoint, which reads the working
        tree, this method reads the COMMITTED content via
        `git show <ref>:<path>`. It is the correct API for browsing
        historical files in the Repository Browser: a user who
        selects a commit from the history view expects to see the
        file as it was at that commit.

        Binary detection:
            The current implementation reports `is_binary=False`
            unconditionally. `GitExecutable` decodes the `git show`
            output as UTF-8 with replacement, which erases the NUL
            bytes that would otherwise signal binary content. A
            dedicated binary-detection pass will be added to
            `GitLogReader` in a later revision; when it lands, this
            method will forward its result. Callers that need
            binary detection today should rely on the size / content
            inspection of the frontend.

        Args:
            workspace_id: Workspace identifier.
            ref: Branch, tag, or commit SHA.
            path: File path relative to the repository root.

        Raises:
            NotFoundError: If the workspace, ref, or file does not
                exist at that ref.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        content = await self.log_reader.read_file_at_ref(
            workspace_id,
            workspace_path=ws_ref.workspace_path,
            ref=ref,
            path=path,
        )
        # Detect truncation by matching the marker appended by
        # `GitLogReader.read_file_at_ref`. This is intentionally
        # conservative: the marker is highly unlikely to appear
        # verbatim inside real content, and the worst case (a false
        # positive) merely sets `is_truncated=True` on a file that
        # happens to end with the marker.
        truncated_marker = "... [content truncated] ..."
        is_truncated = content.endswith(truncated_marker)

        return FileContentAtRefResponse(
            workspace_id=workspace_id,
            ref=ref,
            path=path,
            # TODO: forward `is_binary` from `GitLogReader` once
            # binary detection lands in that module.
            is_binary=False,
            size_bytes=len(content.encode("utf-8")),
            is_truncated=is_truncated,
            content=content,
        )

    async def checkout_workspace_branch(
        self,
        workspace_id: str,
        payload: CheckoutBranchRequest,
    ) -> WorkspaceResponse:
        """
        Check out an existing local branch in the workspace.

        This is a local-only operation: it does not touch the
        remote. Three modes are supported by the underlying branch
        manager, in priority order:

            1. ``commit_message`` in the payload: any pending changes
               are committed with that message FIRST, then the branch
               switch proceeds. This preserves user work across the
               switch — used by Manual Mode's "change branch" flow.
            2. ``force=True``: uncommitted changes are discarded via
               ``git checkout -f``. Destructive; the caller MUST
               obtain explicit user consent.
            3. Default (``force=False``, no ``commit_message``): the
               checkout is refused when the workspace is dirty, so
               user work is never silently discarded.

        After a successful checkout, the workspace is refreshed and
        its updated state is returned.

        Args:
            workspace_id: Workspace identifier.
            payload: The checkout request (`branch_name`, `force`,
                optional `commit_message`).

        Raises:
            NotFoundError: If the workspace or the branch does not
                exist.
            ConflictError: If the workspace is dirty and neither
                `force` nor `commit_message` was provided.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        # `commit_message` is Optional in the schema. Use `getattr`
        # with a safe default so this facade remains compatible with
        # older schema revisions where the field is absent (a
        # deployment scenario in which the backend package and the
        # schema package are updated at different moments).
        commit_message = getattr(payload, "commit_message", None)

        await self.branch_manager.checkout_branch(
            workspace_id,
            branch_name=payload.branch_name,
            workspace_path=ws_ref.workspace_path,
            force=payload.force,
            commit_message=commit_message,
        )

        refreshed = await self.workspace_manager.refresh_workspace_status(
            workspace_id
        )

        self._audit(
            AuditEventType.OTHER,
            action="checkout_workspace_branch",
            message=(
                f"Checked out branch '{payload.branch_name}' in "
                f"workspace '{workspace_id}'."
                + (
                    " Pending changes were committed first."
                    if commit_message
                    else ""
                )
            ),
            payload={
                "workspace_id": workspace_id,
                "branch_name": payload.branch_name,
                "force": payload.force,
                "committed_pending_changes": bool(commit_message),
            },
            workspace_id=workspace_id,
            actor="user",
        )

        return WorkspaceResponse(
            workspace_id=refreshed.workspace_id,
            repository_id=refreshed.repository_id,
            current_branch=refreshed.current_branch,
            base_sha=refreshed.base_sha,
            head_sha=refreshed.head_sha,
            is_dirty=not refreshed.is_clean,
            created_at=refreshed.created_at,
        )

    # ------------------------------------------------------------------
    # 10. Internal response mappers (Git read-only operations)
    # ------------------------------------------------------------------

    @staticmethod
    def _to_commit_entry_response(
        commit: Any,
    ) -> CommitEntryResponse:
        """
        Map a `CommitRef` (or `CommitWithStats`) to the API response.

        The input is typed as `Any` rather than `CommitRef` to avoid
        an extra import in the facade's hot path. The attributes
        accessed here are stable members of `CommitRef` and are
        guaranteed to be present on every subclass.
        """
        return CommitEntryResponse(
            sha=commit.sha,
            message=commit.message,
            author_name=commit.author_name,
            author_email=commit.author_email,
            committed_at=commit.committed_at,
            parent_shas=tuple(commit.parent_shas),
        )

    @staticmethod
    def _to_commit_with_stats_response(
        commit: Any,
    ) -> CommitWithStatsResponse:
        """
        Map a `CommitWithStats` to the API response.

        See `_to_commit_entry_response` for the rationale behind the
        `Any` annotation.
        """
        return CommitWithStatsResponse(
            sha=commit.sha,
            message=commit.message,
            author_name=commit.author_name,
            author_email=commit.author_email,
            committed_at=commit.committed_at,
            parent_shas=tuple(commit.parent_shas),
            files_changed=int(getattr(commit, "files_changed", 0)),
            insertions=int(getattr(commit, "insertions", 0)),
            deletions=int(getattr(commit, "deletions", 0)),
        )

    @classmethod
    def _to_tree_node_response(
        cls,
        node: Any,
    ) -> TreeNodeResponse:
        """
        Recursively map a `TreeNode` to the API response.

        The recursion mirrors the structure of the input tree. The
        conversion is cheap because `TreeNode` is a frozen
        dataclass whose attributes are stable.
        """
        return TreeNodeResponse(
            name=node.name,
            path=node.path,
            type=node.type,
            size=node.size,
            children=tuple(
                cls._to_tree_node_response(child)
                for child in node.children
            ),
        )

    # ------------------------------------------------------------------
    # 11. Pull request operations (GitHub-only in this phase)
    # ------------------------------------------------------------------
    #
    # These methods delegate to `GitHubPullRequestService` and
    # `GitHubReviewsService`. Only GitHub is currently supported;
    # GitLab and Bitbucket adapters do not expose the same merge /
    # review endpoints through the facade yet. Calls with a
    # non-GitHub provider raise `UnsupportedCapabilityError` so the
    # UI can present a clear message instead of a raw 500.

    async def list_pull_requests(
        self,
        repository_id: str,
        *,
        provider: Optional[str] = None,
        state: Optional[str] = None,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
    ) -> PullRequestListResponse:
        """List pull requests for a repository.

        The `state` filter is passed through to the provider as-is;
        GitHub supports `open`, `closed`, `merged`, and `all`. The
        service applies a client-side filter so the returned set
        matches the requested state exactly, rather than GitHub's
        coarser server-side classification.
        """
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_pr_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        # `all` is a valid filter at the GitHub API level but is NOT
        # a member of `ChangeRequestState` (which only models the
        # states of an individual PR). Map it to `None` so the
        # service falls back to its default `state=all` query and the
        # client-side filter is skipped entirely. Without this
        # special case, `?state=all` — a natural thing for the UI to
        # send when the user clicks the "All" filter — produced a
        # 422 and the request never reached the service.
        state_enum: Optional[ChangeRequestState] = None
        if state and state.strip().lower() != "all":
            try:
                state_enum = ChangeRequestState(state.strip().lower())
            except ValueError:
                raise ValidationError(
                    f"Unknown PR state '{state}'. "
                    "Valid values are: open, closed, merged, draft, all.",
                    details={"state": state},
                )

        prs = await service.list_pull_requests(
            repo=repo,
            token=token,
            state=state_enum,
            source_branch=source_branch,
            target_branch=target_branch,
            page=page,
            page_size=page_size,
        )

        items = [self._to_pull_request_response(pr) for pr in prs]
        # The response echoes back the requested state filter, not
        # the internal enum. When the caller passes `state="all"`,
        # `state_enum` is intentionally None (there is no "all"
        # member in ChangeRequestState), but the response must
        # still say "all" so the UI can display the active filter
        # correctly. Falling back to the raw input also handles the
        # `state=None` case (returns "open", the natural default).
        response_state = (
            state.strip().lower() if state else "open"
        )
        return PullRequestListResponse(
            items=tuple(items),
            total=len(items),
            state=response_state,
        )

    async def get_pull_request(
        self,
        repository_id: str,
        number: str,
        *,
        provider: Optional[str] = None,
    ) -> PullRequestResponse:
        """Fetch a single pull request by its number."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_pr_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )
        pr = await service.get_pull_request(
            repo=repo, number_or_iid=number, token=token
        )
        return self._to_pull_request_response(pr)

    async def create_pull_request(
        self,
        payload: CreatePullRequestRequest,
    ) -> PullRequestResponse:
        """Create a new pull request on the remote.

        The policy engine is consulted before the request is sent:
        the `create_change_request` operation kind is subject to a
        per-repository policy, and a denial surfaces here rather
        than after the PR has been created.
        """
        provider_id = (
            ProviderId(payload.provider.strip().lower())
            if payload.provider
            else self._default_provider_id()
        )
        service = self._get_github_pr_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, payload.repository_id, provider_id, token
        )

        policy_ctx = PolicyResolutionContext(
            operation="create_change_request",
            provider=provider_id,
            repository=repo,
            target_branch=payload.target_branch,
            source_branch=payload.source_branch,
            actor="user",
        )
        self.policy_engine.require(policy_ctx)

        pr = await service.create_pull_request(
            repo=repo,
            source_branch=payload.source_branch,
            target_branch=payload.target_branch,
            title=redact_text(payload.title),
            body=redact_text(payload.body or ""),
            draft=payload.draft,
            token=token,
        )

        self._audit(
            AuditEventType.CHANGE_REQUEST_CREATE,
            action="create_pull_request",
            message=f"Created PR #{pr.number_or_iid}.",
            payload={
                "number": pr.number_or_iid,
                "web_url": redact_url(pr.web_url),
                "draft": payload.draft,
            },
            repository_id=payload.repository_id,
            provider=provider_id.value,
            actor="user",
        )

        return self._to_pull_request_response(pr)

    async def merge_pull_request(
        self,
        repository_id: str,
        number: str,
        payload: MergePullRequestRequest,
        *,
        provider: Optional[str] = None,
    ) -> MergePullRequestResponse:
        """Merge an open pull request.

        The policy engine is consulted before the merge is sent:
        merging is a destructive remote write and MUST pass the
        `merge_change_request` policy. A denial surfaces as a
        `PolicyDeniedError` before any state on the remote changes.
        """
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_pr_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            number_int = int(str(number).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "PR number must be an integer string.",
                details={"number": number},
            )

        policy_ctx = PolicyResolutionContext(
            operation="merge_change_request",
            provider=provider_id,
            repository=repo,
            actor="user",
        )
        self.policy_engine.require(policy_ctx)

        result = await service.merge_pull_request(
            repo=repo,
            number=number_int,
            merge_method=payload.merge_method,
            commit_title=payload.commit_title,
            commit_message=payload.commit_message,
            delete_source_branch=payload.delete_source_branch,
            expected_head_sha=payload.expected_head_sha,
            token=token,
        )

        self._audit(
            AuditEventType.OTHER,
            action="merge_pull_request",
            message=(
                f"Merged PR #{number_int}."
                if result.merged
                else f"Merge attempt for PR #{number_int} did not succeed."
            ),
            payload={
                "number": number_int,
                "merged": result.merged,
                "sha": result.sha,
                "method": payload.merge_method,
            },
            repository_id=repository_id,
            provider=provider_id.value,
            actor="user",
        )

        return MergePullRequestResponse(
            status="success" if result.merged else "conflict",
            merged=result.merged,
            sha=result.sha,
            message=result.message
            or (
                "Merge applied."
                if result.merged
                else "Merge was not applied."
            ),
        )

    async def submit_pr_review(
        self,
        repository_id: str,
        number: str,
        payload: SubmitPullRequestReviewRequest,
        *,
        provider: Optional[str] = None,
    ) -> PullRequestReviewResponse:
        """Submit a review on a pull request.

        Inline comments are redacted before being forwarded to the
        service, so accidental secrets in a comment body never land
        in the provider payload or in the audit log.
        """
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        reviews_service = self._get_github_reviews_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            number_int = int(str(number).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "PR number must be an integer string.",
                details={"number": number},
            )

        comments_payload = [
            {
                "path": c.path,
                "line": c.line,
                "side": c.side,
                "body": redact_text(c.body),
            }
            for c in payload.comments
        ]

        review = await reviews_service.submit_review(
            repo=repo,
            number=number_int,
            event=payload.event,
            body=redact_text(payload.body) if payload.body else None,
            comments=comments_payload,
            token=token,
        )

        self._audit(
            AuditEventType.OTHER,
            action="submit_pr_review",
            message=(
                f"Submitted review on PR #{number_int} "
                f"({payload.event})."
            ),
            payload={
                "number": number_int,
                "event": payload.event,
                "review_id": review.review_id,
                "inline_comments": len(payload.comments),
            },
            repository_id=repository_id,
            provider=provider_id.value,
            actor="user",
        )

        return self._to_review_response(review)

    async def create_pr_comment(
        self,
        repository_id: str,
        number: str,
        payload: CreatePullRequestCommentRequest,
        *,
        provider: Optional[str] = None,
    ) -> PullRequestCommentResponse:
        """Create a general or inline comment on a pull request.

        Dispatches based on the payload shape:

            - Both `path` and `line` set  -> inline comment on that
              file/line, via `/pulls/{n}/comments`.
            - Otherwise                   -> general comment on the
              conversation, via `/issues/{n}/comments`.

        The two paths return structurally similar but distinct
        resources; both are mapped to `PullRequestCommentResponse`
        so the UI has a single type to render.
        """
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        reviews_service = self._get_github_reviews_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            number_int = int(str(number).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "PR number must be an integer string.",
                details={"number": number},
            )

        is_inline = bool(payload.path and payload.line)

        if is_inline:
            inline = await reviews_service.create_review_comment(
                repo=repo,
                number=number_int,
                body=redact_text(payload.body),
                path=payload.path or "",
                line=payload.line or 0,
                side=payload.side or "RIGHT",
                commit_id=payload.commit_id,
                token=token,
            )
            self._audit(
                AuditEventType.OTHER,
                action="create_pr_inline_comment",
                message=(
                    f"Added inline comment on PR #{number_int} "
                    f"at {payload.path}:{payload.line}."
                ),
                payload={
                    "number": number_int,
                    "comment_id": inline.comment_id,
                    "path": inline.path,
                    "line": inline.line,
                },
                repository_id=repository_id,
                provider=provider_id.value,
                actor="user",
            )
            return self._to_review_comment_response(inline)

        general = await reviews_service.create_issue_comment(
            repo=repo,
            number=number_int,
            body=redact_text(payload.body),
            token=token,
        )
        self._audit(
            AuditEventType.OTHER,
            action="create_pr_comment",
            message=f"Added comment on PR #{number_int}.",
            payload={
                "number": number_int,
                "comment_id": general.comment_id,
            },
            repository_id=repository_id,
            provider=provider_id.value,
            actor="user",
        )
        return self._to_issue_comment_response(general)

    async def list_pr_reviews_and_comments(
        self,
        repository_id: str,
        number: str,
        *,
        provider: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """Return reviews, general comments, and inline comments.

        Returns a plain dict (not a Pydantic model) because the
        response is a composite of three independent collections and
        adding a dedicated schema for it would not buy anything the
        frontend does not already have. The keys and value shapes
        are stable and typed by the Pydantic classes referenced
        below:

            {
                "reviews": PullRequestReviewResponse[],
                "general_comments": PullRequestCommentResponse[],
                "inline_comments": PullRequestCommentResponse[],
            }
        """
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        reviews_service = self._get_github_reviews_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            number_int = int(str(number).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "PR number must be an integer string.",
                details={"number": number},
            )

        reviews = await reviews_service.list_reviews(
            repo=repo, number=number_int, token=token
        )
        general, inline = await reviews_service.list_comments(
            repo=repo, number=number_int, token=token
        )

        return {
            "reviews": [self._to_review_response(r) for r in reviews],
            "general_comments": [
                self._to_issue_comment_response(c) for c in general
            ],
            "inline_comments": [
                self._to_review_comment_response(c) for c in inline
            ],
        }

    # ------------------------------------------------------------------
    # 12. Internal helpers for pull requests
    # ------------------------------------------------------------------

    def _get_github_pr_service(
        self, provider_id: ProviderId
    ) -> GitHubPullRequestService:
        """Return the PR service for a provider, or raise.

        Only GitHub is wired today. Other providers raise
        `UnsupportedCapabilityError` so the UI can surface a
        capability message instead of a raw 500.
        """
        if provider_id not in _PR_CAPABLE_PROVIDERS:
            raise UnsupportedCapabilityError(
                "Pull request operations are only supported for GitHub.",
                details={"provider": provider_id.value},
            )
        if self._pr_service is None:
            self._pr_service = GitHubPullRequestService()
        return self._pr_service

    def _get_github_reviews_service(
        self, provider_id: ProviderId
    ) -> GitHubReviewsService:
        """Return the reviews service for a provider, or raise."""
        if provider_id not in _PR_CAPABLE_PROVIDERS:
            raise UnsupportedCapabilityError(
                "Review operations are only supported for GitHub.",
                details={"provider": provider_id.value},
            )
        if self._reviews_service is None:
            self._reviews_service = GitHubReviewsService()
        return self._reviews_service

    @staticmethod
    def _to_pull_request_response(
        pr: ChangeRequestRef,
    ) -> PullRequestResponse:
        """Map a `ChangeRequestRef` to a `PullRequestResponse`.

        `author_login` and `body` are currently None because the
        `ChangeRequestRef` dataclass does not carry those fields.
        Extending the dataclass to include them is tracked as a
        separate enhancement; the UI still works without them.
        """
        return PullRequestResponse(
            provider=(
                pr.provider.value
                if isinstance(pr.provider, ProviderId)
                else str(pr.provider)
            ),
            repository_id=pr.repository_id,
            number=str(pr.number_or_iid),
            web_url=redact_url(pr.web_url),
            state=(
                pr.state.value
                if hasattr(pr.state, "value")
                else str(pr.state)
            ),
            source_branch=pr.source_branch,
            target_branch=pr.target_branch,
            title=pr.title,
            is_draft=pr.is_draft,
            created_at=pr.created_at,
            updated_at=pr.updated_at,
            merged_at=pr.merged_at,
        )

    @staticmethod
    def _to_review_response(review: Any) -> PullRequestReviewResponse:
        """Map a `ReviewRef` to a `PullRequestReviewResponse`."""
        return PullRequestReviewResponse(
            review_id=str(review.review_id),
            state=review.state,
            body=review.body,
            author_login=review.author_login,
            submitted_at=review.submitted_at,
        )

    @staticmethod
    def _to_review_comment_response(
        comment: Any,
    ) -> PullRequestCommentResponse:
        """Map a `ReviewCommentRef` to a `PullRequestCommentResponse`."""
        return PullRequestCommentResponse(
            comment_id=str(comment.comment_id),
            body=comment.body,
            author_login=comment.author_login,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
            path=comment.path,
            line=comment.line,
            side=comment.side,
            in_reply_to_id=comment.in_reply_to_id,
        )

    @staticmethod
    def _to_issue_comment_response(
        comment: Any,
    ) -> PullRequestCommentResponse:
        """Map an `IssueCommentRef` to a `PullRequestCommentResponse`.

        General comments have no `path`, `line`, or `side`; those
        fields are explicitly set to None so the response shape is
        identical to the inline case and the frontend can use one
        type for both.
        """
        return PullRequestCommentResponse(
            comment_id=str(comment.comment_id),
            body=comment.body,
            author_login=comment.author_login,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
            path=None,
            line=None,
            side=None,
            in_reply_to_id=None,
        )

    # ------------------------------------------------------------------
    # 13. Issue operations (GitHub-only in this phase)
    # ------------------------------------------------------------------
    #
    # These methods delegate to `GitHubIssueService`. Only GitHub is
    # wired today; the GitLab and Bitbucket adapters do not expose
    # the issue endpoints through the facade yet. A non-GitHub
    # provider raises `UnsupportedCapabilityError` so the UI can
    # present a clear message instead of a raw 500.
    #
    # Unlike pull requests, issues are NOT policy-gated: creating an
    # issue or commenting on one does not touch code, branches, or
    # the repository's contents. All write operations are still
    # audited (see `_audit` calls below) so an operator can review
    # the trail of what the user did.

    async def list_issues(
        self,
        repository_id: str,
        *,
        provider: Optional[str] = None,
        state: str = "open",
        labels: Optional[Sequence[str]] = None,
        page: int = 1,
        page_size: int = 30,
    ) -> IssueListResponse:
        """List issues for a repository.

        Pull requests are filtered out by the underlying service;
        the response contains only real issues. Pass ``state="all"``
        to include both open and closed issues.

        Args:
            repository_id: Repository identifier (ID or `owner/repo`).
            provider: Optional provider override. Defaults to the
                connected provider, which is GitHub today.
            state: ``"open"``, ``"closed"``, or ``"all"``.
            labels: Optional label filter. Empty/None means no filter.
            page: 1-based page number.
            page_size: Page size, capped by the service at 100.
        """
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_issue_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        issues = await service.list_issues(
            repo=repo,
            token=token,
            state=state,
            labels=labels,
            page=page,
            page_size=page_size,
        )

        items = [
            self._to_issue_response(
                issue,
                provider_id=provider_id,
                repository_id=repository_id,
            )
            for issue in issues
        ]
        return IssueListResponse(
            items=tuple(items),
            total=len(items),
            state=state.lower(),
        )

    async def get_issue(
        self,
        repository_id: str,
        number: str,
        *,
        provider: Optional[str] = None,
    ) -> IssueResponse:
        """Fetch a single issue by its number."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_issue_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            number_int = int(str(number).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "Issue number must be an integer string.",
                details={"number": number},
            )

        issue = await service.get_issue(
            repo=repo, number=number_int, token=token
        )
        return self._to_issue_response(
            issue,
            provider_id=provider_id,
            repository_id=repository_id,
        )

    async def create_issue(
        self,
        payload: CreateIssueRequest,
    ) -> IssueResponse:
        """Create a new issue on the remote.

        The title and body are redacted before the request is sent,
        so a token accidentally pasted into an issue description
        never leaves the process in cleartext.
        """
        provider_id = (
            ProviderId(payload.provider.strip().lower())
            if payload.provider
            else self._default_provider_id()
        )
        service = self._get_github_issue_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, payload.repository_id, provider_id, token
        )

        issue = await service.create_issue(
            repo=repo,
            title=redact_text(payload.title),
            body=redact_text(payload.body or ""),
            labels=payload.labels,
            assignees=payload.assignees,
            token=token,
        )

        self._audit(
            AuditEventType.OTHER,
            action="create_issue",
            message=f"Created issue #{issue.number}.",
            payload={
                "number": issue.number,
                "web_url": redact_url(issue.web_url or ""),
                "labels": list(payload.labels),
            },
            repository_id=payload.repository_id,
            provider=provider_id.value,
            actor="user",
        )

        return self._to_issue_response(
            issue,
            provider_id=provider_id,
            repository_id=payload.repository_id,
        )

    async def update_issue_state(
        self,
        repository_id: str,
        number: str,
        payload: UpdateIssueStateRequest,
        *,
        provider: Optional[str] = None,
    ) -> IssueResponse:
        """Change an issue's state (close or reopen)."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_issue_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            number_int = int(str(number).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "Issue number must be an integer string.",
                details={"number": number},
            )

        issue = await service.update_issue_state(
            repo=repo,
            number=number_int,
            state=payload.state,
            state_reason=payload.state_reason,
            token=token,
        )

        self._audit(
            AuditEventType.OTHER,
            action="update_issue_state",
            message=(
                f"Updated issue #{number_int} state to "
                f"'{payload.state}'."
            ),
            payload={
                "number": number_int,
                "state": payload.state,
                "state_reason": payload.state_reason,
            },
            repository_id=repository_id,
            provider=provider_id.value,
            actor="user",
        )

        return self._to_issue_response(
            issue,
            provider_id=provider_id,
            repository_id=repository_id,
        )

    async def list_issue_comments(
        self,
        repository_id: str,
        number: str,
        *,
        provider: Optional[str] = None,
    ) -> Sequence[IssueCommentResponse]:
        """List the comments on an issue, oldest first."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_issue_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            number_int = int(str(number).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "Issue number must be an integer string.",
                details={"number": number},
            )

        comments = await service.list_comments(
            repo=repo, number=number_int, token=token
        )
        return tuple(
            self._to_issue_thread_response(c) for c in comments
        )

    async def create_issue_comment(
        self,
        repository_id: str,
        number: str,
        payload: CreateIssueCommentRequest,
        *,
        provider: Optional[str] = None,
    ) -> IssueCommentResponse:
        """Add a comment to an existing issue."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_issue_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            number_int = int(str(number).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "Issue number must be an integer string.",
                details={"number": number},
            )

        comment = await service.create_comment(
            repo=repo,
            number=number_int,
            body=redact_text(payload.body),
            token=token,
        )

        self._audit(
            AuditEventType.OTHER,
            action="create_issue_comment",
            message=f"Added comment on issue #{number_int}.",
            payload={
                "number": number_int,
                "comment_id": comment.comment_id,
            },
            repository_id=repository_id,
            provider=provider_id.value,
            actor="user",
        )

        return self._to_issue_thread_response(comment)

    # ------------------------------------------------------------------
    # 14. Internal helpers for issues
    # ------------------------------------------------------------------

    def _get_github_issue_service(
        self, provider_id: ProviderId
    ) -> GitHubIssueService:
        """Return the issue service for a provider, or raise.

        Only GitHub is wired today. Other providers raise
        `UnsupportedCapabilityError` so the UI can surface a
        capability message instead of a raw 500.
        """
        if provider_id != ProviderId.GITHUB:
            raise UnsupportedCapabilityError(
                "Issue operations are only supported for GitHub.",
                details={"provider": provider_id.value},
            )
        if self._issue_service is None:
            self._issue_service = GitHubIssueService()
        return self._issue_service

    @staticmethod
    def _to_issue_response(
        issue: Any,
        *,
        provider_id: ProviderId,
        repository_id: str,
    ) -> IssueResponse:
        """Map an `IssueRef` to an `IssueResponse`.

        The `IssueRef` dataclass does not carry the provider or the
        repository id, so the two are passed in by the caller. The
        `web_url` field is required by the schema (not Optional) but
        the dataclass carries it as Optional; a missing URL becomes
        an empty string rather than a schema violation.
        """
        return IssueResponse(
            provider=(
                provider_id.value
                if isinstance(provider_id, ProviderId)
                else str(provider_id)
            ),
            repository_id=repository_id,
            number=str(issue.number),
            web_url=issue.web_url or "",
            state=issue.state,
            title=issue.title,
            body=issue.body,
            author_login=issue.author_login,
            labels=tuple(issue.labels),
            comments_count=int(issue.comments_count),
            is_pull_request=bool(issue.is_pull_request),
            state_reason=issue.state_reason,
            created_at=issue.created_at,
            updated_at=issue.updated_at,
            closed_at=issue.closed_at,
        )

    @staticmethod
    def _to_issue_thread_response(
        comment: Any,
    ) -> IssueCommentResponse:
        """Map an `IssueCommentRef` (from `issues.py`) to the API response.

        NOTE on naming:
            There is already a `_to_issue_comment_response` method
            on this class that maps the PR-side `IssueCommentRef`
            (from `reviews.py`) to a `PullRequestCommentResponse`.
            The two dataclasses share a name but live in different
            modules and produce different response types. This
            helper is deliberately named `_to_issue_thread_response`
            to avoid collision and to convey that it is the
            issue-side (conversation thread) mapper.
        """
        return IssueCommentResponse(
            comment_id=str(comment.comment_id),
            body=comment.body,
            author_login=comment.author_login,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
        )

    # ------------------------------------------------------------------
    # 15. Merge / rebase / conflict resolution (local workspace)
    # ------------------------------------------------------------------
    #
    # These operations run entirely inside the workspace: no remote
    # calls, no tokens, no provider dispatch. They are audited for
    # the same reason every other mutating operation is audited —
    # so an operator can review what the user changed — but they are
    # NOT policy-gated: policy in this subsystem gates REMOTE side
    # effects (push, PR merge, branch delete on the remote). A local
    # merge is an editor action, not a remote write.

    async def merge_workspace_branches(
        self,
        workspace_id: str,
        payload: MergeBranchesRequest,
    ) -> MergeOperationResponse:
        """Merge one local branch into another inside the workspace.

        On a clean merge, returns ``kind="clean"``. On a conflicted
        merge, returns ``kind="conflicted"`` with the current
        conflict list; the caller then drives the conflict
        resolution workflow and finally calls
        `finalize_workspace_operation`.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        outcome = await self.merge_manager.merge_branch(
            workspace_id,
            source_branch=payload.source_branch,
            target_branch=payload.target_branch,
            strategy=payload.strategy,
            commit_message=payload.commit_message,
            workspace_path=ws_ref.workspace_path,
        )

        self._audit(
            AuditEventType.OTHER,
            action="merge_workspace_branches",
            message=(
                f"Merged '{payload.source_branch}' into "
                f"'{payload.target_branch}' "
                f"(outcome: {outcome.kind})."
            ),
            payload={
                "source_branch": payload.source_branch,
                "target_branch": payload.target_branch,
                "strategy": payload.strategy,
                "kind": outcome.kind,
                "conflicts": len(outcome.conflicts),
            },
            workspace_id=workspace_id,
            repository_id=ws_ref.repository_id,
            actor="user",
        )

        return self._to_merge_operation_response(
            workspace_id=workspace_id, outcome=outcome
        )

    async def rebase_workspace_branch(
        self,
        workspace_id: str,
        payload: RebaseBranchRequest,
    ) -> MergeOperationResponse:
        """Rebase one local branch onto another inside the workspace."""
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        outcome = await self.merge_manager.rebase_branch(
            workspace_id,
            branch=payload.branch,
            onto=payload.onto,
            workspace_path=ws_ref.workspace_path,
        )

        self._audit(
            AuditEventType.OTHER,
            action="rebase_workspace_branch",
            message=(
                f"Rebased '{payload.branch}' onto '{payload.onto}' "
                f"(outcome: {outcome.kind})."
            ),
            payload={
                "branch": payload.branch,
                "onto": payload.onto,
                "kind": outcome.kind,
                "conflicts": len(outcome.conflicts),
            },
            workspace_id=workspace_id,
            repository_id=ws_ref.repository_id,
            actor="user",
        )

        return self._to_merge_operation_response(
            workspace_id=workspace_id, outcome=outcome
        )

    async def finalize_workspace_operation(
        self,
        workspace_id: str,
    ) -> MergeOperationResponse:
        """Finalize whichever operation (merge or rebase) is in progress.

        Dispatches on the current Git state. The caller is expected
        to have resolved and staged all conflicts before calling
        this; the underlying managers refuse to commit when
        unresolved conflicts remain.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        state = await self.conflict_manager.detect_operation_state(
            workspace_id, workspace_path=ws_ref.workspace_path
        )

        if state == OperationState.MERGING:
            outcome = await self.merge_manager.finalize_merge(
                workspace_id,
                workspace_path=ws_ref.workspace_path,
            )
            action = "finalize_merge"
        elif state == OperationState.REBASING:
            outcome = await self.merge_manager.finalize_rebase(
                workspace_id,
                workspace_path=ws_ref.workspace_path,
            )
            action = "finalize_rebase"
        else:
            raise ConflictError(
                f"Cannot finalize: current state is '{state.value}', "
                "not 'merging' or 'rebasing'.",
                details={"state": state.value},
            )

        self._audit(
            AuditEventType.OTHER,
            action=action,
            message=(
                f"Finalized in-progress {state.value} operation "
                f"(outcome: {outcome.kind})."
            ),
            payload={
                "state": state.value,
                "kind": outcome.kind,
                "head_sha": outcome.head_sha,
            },
            workspace_id=workspace_id,
            repository_id=ws_ref.repository_id,
            actor="user",
        )

        return self._to_merge_operation_response(
            workspace_id=workspace_id, outcome=outcome
        )

    async def abort_workspace_operation(
        self,
        workspace_id: str,
    ) -> ConflictListResponse:
        """Abort whichever operation (merge / rebase / cherry-pick /
        revert) is in progress and return the resulting state.

        Returns a `ConflictListResponse` with `operation_state="none"`
        and an empty file list on success. The `message` field
        echoes which operation was aborted.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        aborted_state = await self.merge_manager.abort_operation(
            workspace_id,
            workspace_path=ws_ref.workspace_path,
        )

        self._audit(
            AuditEventType.OTHER,
            action="abort_workspace_operation",
            message=(
                f"Aborted in-progress operation "
                f"(was: {aborted_state.value})."
            ),
            payload={"aborted_state": aborted_state.value},
            workspace_id=workspace_id,
            repository_id=ws_ref.repository_id,
            actor="user",
        )

        return ConflictListResponse(
            workspace_id=workspace_id,
            operation_state=OperationState.NONE.value,
            files=(),
            total=0,
        )

    async def list_workspace_conflicts(
        self,
        workspace_id: str,
    ) -> ConflictListResponse:
        """Return the current operation state and conflicted files.

        Safe to call at any time; returns ``operation_state="none"``
        and an empty file list when no operation is in progress.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        state = await self.conflict_manager.detect_operation_state(
            workspace_id, workspace_path=ws_ref.workspace_path
        )
        files: Sequence[ConflictEntry] = ()
        if state != OperationState.NONE:
            files = await self.conflict_manager.list_conflicts(
                workspace_id, workspace_path=ws_ref.workspace_path
            )

        return ConflictListResponse(
            workspace_id=workspace_id,
            operation_state=state.value,
            files=tuple(
                self._to_conflict_file_response(entry) for entry in files
            ),
            total=len(files),
        )

    async def resolve_workspace_conflicts(
        self,
        workspace_id: str,
        payload: ResolveConflictsRequest,
    ) -> ResolveConflictsResponse:
        """Resolve conflicted files by taking a side, or by staging
        files the caller has edited manually.

        Resolution strategies:

            - ``"ours"``    : run ``git checkout --ours`` on each
                              requested path. When ``paths`` is empty,
                              every currently-conflicted file is
                              resolved.
            - ``"theirs"``  : same, but ``--theirs``.
            - ``"manual"``  : the caller has already edited the
                              files in the workspace; run ``git add``
                              on each path. ``paths`` is required.

        Requested paths are validated against the CURRENT conflict
        set. A path that is not (or no longer) conflicted is
        rejected: resolving a non-conflicted path with
        ``--ours/--theirs`` produces surprising results in Git, and
        silently ignoring the path leaves the caller with a wrong
        mental model of what happened.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        state = await self.conflict_manager.detect_operation_state(
            workspace_id, workspace_path=ws_ref.workspace_path
        )
        if state == OperationState.NONE:
            raise ConflictError(
                "No merge or rebase is in progress.",
                details={"state": state.value},
            )

        current = await self.conflict_manager.list_conflicts(
            workspace_id, workspace_path=ws_ref.workspace_path
        )
        current_paths = {entry.path for entry in current}

        # Determine the target paths.
        requested = list(payload.paths)
        if payload.strategy == "manual" and not requested:
            raise ValidationError(
                "The 'manual' strategy requires an explicit list of "
                "paths to mark resolved.",
            )

        if not requested:
            target_paths = sorted(current_paths)
        else:
            # Reject any path that is not (or no longer) conflicted.
            unknown = [p for p in requested if p not in current_paths]
            if unknown:
                raise ValidationError(
                    "One or more paths are not currently conflicted.",
                    details={"unknown_paths": unknown},
                )
            target_paths = requested

        if not target_paths:
            return ResolveConflictsResponse(
                workspace_id=workspace_id,
                operation_state=state.value,
                resolved_count=0,
                remaining=tuple(
                    self._to_conflict_file_response(e)
                    for e in current
                ),
                message="No conflicts to resolve.",
            )

        resolved_count = 0
        if payload.strategy == "ours":
            resolved_count = await self.conflict_manager.resolve_using_ours(
                workspace_id,
                paths=target_paths,
                workspace_path=ws_ref.workspace_path,
            )
        elif payload.strategy == "theirs":
            resolved_count = (
                await self.conflict_manager.resolve_using_theirs(
                    workspace_id,
                    paths=target_paths,
                    workspace_path=ws_ref.workspace_path,
                )
            )
        else:  # manual
            await self.conflict_manager.mark_resolved(
                workspace_id,
                paths=target_paths,
                workspace_path=ws_ref.workspace_path,
            )
            resolved_count = len(target_paths)

        # Recompute remaining conflicts and state after resolution.
        remaining = await self.conflict_manager.list_conflicts(
            workspace_id, workspace_path=ws_ref.workspace_path
        )
        new_state = await self.conflict_manager.detect_operation_state(
            workspace_id, workspace_path=ws_ref.workspace_path
        )

        self._audit(
            AuditEventType.OTHER,
            action="resolve_workspace_conflicts",
            message=(
                f"Resolved {resolved_count} conflicted file(s) "
                f"using strategy '{payload.strategy}'."
            ),
            payload={
                "strategy": payload.strategy,
                "resolved_count": resolved_count,
                "remaining": len(remaining),
                "state": new_state.value,
            },
            workspace_id=workspace_id,
            repository_id=ws_ref.repository_id,
            actor="user",
        )

        return ResolveConflictsResponse(
            workspace_id=workspace_id,
            operation_state=new_state.value,
            resolved_count=resolved_count,
            remaining=tuple(
                self._to_conflict_file_response(e) for e in remaining
            ),
            message=(
                f"Resolved {resolved_count} file(s); "
                f"{len(remaining)} remain."
            ),
        )

    # ------------------------------------------------------------------
    # 16. Internal helpers for merge / conflict responses
    # ------------------------------------------------------------------

    @staticmethod
    def _to_conflict_file_response(
        entry: Any,
    ) -> ConflictFileResponse:
        """Map a `ConflictEntry` to a `ConflictFileResponse`.

        The `is_delete_modify` and `is_add_add` flags are derived
        properties on `ConflictEntry`; they are read here rather
        than reconstructed so that the two views can never drift.
        """
        return ConflictFileResponse(
            path=entry.path,
            has_base=bool(entry.has_base),
            has_ours=bool(entry.has_ours),
            has_theirs=bool(entry.has_theirs),
            is_delete_modify=bool(entry.is_delete_modify),
            is_add_add=bool(entry.is_add_add),
        )

    @classmethod
    def _to_merge_operation_response(
        cls,
        *,
        workspace_id: str,
        outcome: MergeOutcome,
    ) -> MergeOperationResponse:
        """Map a `MergeOutcome` to a `MergeOperationResponse`."""
        return MergeOperationResponse(
            workspace_id=workspace_id,
            kind=outcome.kind,
            operation_state=(
                outcome.operation_state.value
                if hasattr(outcome.operation_state, "value")
                else str(outcome.operation_state)
            ),
            conflicts=tuple(
                cls._to_conflict_file_response(entry)
                for entry in outcome.conflicts
            ),
            head_sha=outcome.head_sha,
            message=outcome.message or "",
        )

    # ------------------------------------------------------------------
    # 17. Local tags (workspace)
    # ------------------------------------------------------------------
    #
    # These operations act on the workspace's LOCAL Git repository.
    # They never touch the remote. Creating a LOCAL tag does not push
    # it; a separate "push tag" operation would be required to make
    # it visible upstream. That operation is intentionally out of
    # scope for this phase.

    async def list_workspace_tags(
        self,
        workspace_id: str,
        *,
        max_tags: int = 5000,
    ) -> TagListResponse:
        """List every local Git tag in the workspace.

        Returns an empty ``items`` sequence for a workspace with no
        tags. The list is sorted alphabetically by name (Git's
        native `for-each-ref` order), which is what the UI displays.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        tags = await self.tag_manager.list_tags(
            workspace_id,
            workspace_path=ws_ref.workspace_path,
            max_tags=max_tags,
        )
        return TagListResponse(
            items=tuple(self._to_tag_response(t) for t in tags),
            total=len(tags),
        )

    async def create_workspace_tag(
        self,
        workspace_id: str,
        payload: CreateTagRequest,
    ) -> TagResponse:
        """Create a local Git tag in the workspace.

        A tag with a non-empty ``message`` is created as an annotated
        tag; without one it is created as a lightweight tag.
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        tag = await self.tag_manager.create_tag(
            workspace_id,
            workspace_path=ws_ref.workspace_path,
            name=payload.name,
            message=(
                redact_text(payload.message) if payload.message else None
            ),
            target_sha=payload.target_sha,
            force=payload.force,
        )

        self._audit(
            AuditEventType.OTHER,
            action="create_workspace_tag",
            message=f"Created tag '{payload.name}'.",
            payload={
                "name": payload.name,
                "sha": tag.sha,
                "is_annotated": tag.is_annotated,
                "force": payload.force,
            },
            workspace_id=workspace_id,
            repository_id=ws_ref.repository_id,
            actor="user",
        )

        return self._to_tag_response(tag)

    async def delete_workspace_tag(
        self,
        workspace_id: str,
        name: str,
    ) -> ActionSuccessResponse:
        """Delete a local Git tag from the workspace.

        Only the LOCAL tag is deleted. If the tag was previously
        pushed to the remote, the remote copy remains until a
        separate remote-delete operation runs (out of scope for
        this phase).
        """
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if ws_ref is None:
            raise NotFoundError(
                f"Workspace '{workspace_id}' was not found."
            )

        await self.tag_manager.delete_tag(
            workspace_id,
            workspace_path=ws_ref.workspace_path,
            name=name,
        )

        self._audit(
            AuditEventType.OTHER,
            action="delete_workspace_tag",
            message=f"Deleted tag '{name}'.",
            payload={"name": name},
            workspace_id=workspace_id,
            repository_id=ws_ref.repository_id,
            actor="user",
        )

        return ActionSuccessResponse(
            message=f"Tag '{name}' deleted."
        )

    # ------------------------------------------------------------------
    # 18. GitHub releases (remote)
    # ------------------------------------------------------------------
    #
    # A GitHub release bundles a tag, a title, release notes, and
    # optional assets. This class manages the release lifecycle
    # (list, get, create, delete). It does NOT manage asset uploads;
    # that is a separate flow that requires the uploads.github.com
    # host. It also does NOT delete the underlying tag on delete.

    async def list_releases(
        self,
        repository_id: str,
        *,
        provider: Optional[str] = None,
        include_drafts: bool = False,
        page: int = 1,
        page_size: int = 30,
    ) -> ReleaseListResponse:
        """List releases for a repository.

        Drafts are excluded by default. Pass ``include_drafts=True``
        to include them.
        """
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_release_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        releases = await service.list_releases(
            repo=repo,
            token=token,
            include_drafts=include_drafts,
            page=page,
            page_size=page_size,
        )
        return ReleaseListResponse(
            items=tuple(self._to_release_response(r) for r in releases),
            total=len(releases),
        )

    async def get_release(
        self,
        repository_id: str,
        release_id: str,
        *,
        provider: Optional[str] = None,
    ) -> ReleaseResponse:
        """Fetch a single release by its numeric GitHub ID."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_release_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            numeric_id = int(str(release_id).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "release_id must be an integer string.",
                details={"release_id": release_id},
            )

        release = await service.get_release(
            repo=repo, release_id=numeric_id, token=token
        )
        return self._to_release_response(release)

    async def create_release(
        self,
        payload: CreateReleaseRequest,
    ) -> ReleaseResponse:
        """Create a GitHub release.

        The release body and name are redacted before being sent,
        so a token accidentally pasted into release notes never
        leaves the process in cleartext.
        """
        provider_id = (
            ProviderId(payload.provider.strip().lower())
            if payload.provider
            else self._default_provider_id()
        )
        service = self._get_github_release_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, payload.repository_id, provider_id, token
        )

        release = await service.create_release(
            repo=repo,
            tag_name=payload.tag_name,
            name=redact_text(payload.name) if payload.name else "",
            body=redact_text(payload.body) if payload.body else "",
            target_commitish=payload.target_commitish,
            draft=payload.draft,
            prerelease=payload.prerelease,
            token=token,
        )

        self._audit(
            AuditEventType.OTHER,
            action="create_release",
            message=(
                f"Created release for tag '{payload.tag_name}' "
                f"(draft={payload.draft}, "
                f"prerelease={payload.prerelease})."
            ),
            payload={
                "tag_name": payload.tag_name,
                "release_id": release.release_id,
                "draft": payload.draft,
                "prerelease": payload.prerelease,
                "html_url": redact_url(release.html_url),
            },
            repository_id=payload.repository_id,
            provider=provider_id.value,
            actor="user",
        )

        return self._to_release_response(release)

    async def delete_release(
        self,
        repository_id: str,
        release_id: str,
        *,
        provider: Optional[str] = None,
    ) -> ActionSuccessResponse:
        """Delete a GitHub release.

        Only the release object is deleted. The underlying Git tag
        on the remote is NOT deleted; it remains in the repository.
        """
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        service = self._get_github_release_service(provider_id)
        adapter = self.registry.get(provider_id.value)
        token = await self._require_token(provider_id)
        repo = await self._resolve_repo(
            adapter, repository_id, provider_id, token
        )

        try:
            numeric_id = int(str(release_id).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "release_id must be an integer string.",
                details={"release_id": release_id},
            )

        await service.delete_release(
            repo=repo, release_id=numeric_id, token=token
        )

        self._audit(
            AuditEventType.OTHER,
            action="delete_release",
            message=f"Deleted release {numeric_id}.",
            payload={"release_id": numeric_id},
            repository_id=repository_id,
            provider=provider_id.value,
            actor="user",
        )

        return ActionSuccessResponse(
            message=(
                f"Release {numeric_id} deleted. "
                "The underlying Git tag was not removed."
            )
        )

    # ------------------------------------------------------------------
    # 19. Internal helpers for tags and releases
    # ------------------------------------------------------------------

    def _get_github_release_service(
        self, provider_id: ProviderId
    ) -> GitHubReleaseService:
        """Return the release service for a provider, or raise.

        Only GitHub is wired today. Other providers raise
        `UnsupportedCapabilityError` so the UI can present a clear
        message instead of a raw 500.
        """
        if provider_id != ProviderId.GITHUB:
            raise UnsupportedCapabilityError(
                "Release operations are only supported for GitHub.",
                details={"provider": provider_id.value},
            )
        if self._release_service is None:
            self._release_service = GitHubReleaseService()
        return self._release_service

    @staticmethod
    def _to_tag_response(tag: Any) -> TagResponse:
        """Map a `TagRef` to a `TagResponse`."""
        return TagResponse(
            name=tag.name,
            sha=tag.sha,
            is_annotated=bool(tag.is_annotated),
            message=tag.message,
            tagger_name=tag.tagger_name,
            tagger_email=tag.tagger_email,
            tagged_at=tag.tagged_at,
        )

    @staticmethod
    def _to_release_response(release: Any) -> ReleaseResponse:
        """Map a `ReleaseRef` to a `ReleaseResponse`."""
        return ReleaseResponse(
            release_id=str(release.release_id),
            tag_name=release.tag_name,
            name=release.name,
            body=release.body,
            html_url=redact_url(release.html_url) or "",
            is_draft=bool(release.is_draft),
            is_prerelease=bool(release.is_prerelease),
            author_login=release.author_login,
            target_commitish=release.target_commitish,
            created_at=release.created_at,
            published_at=release.published_at,
        )

    # ------------------------------------------------------------------
    # 20. Source Control subsystem settings
    # ------------------------------------------------------------------
    #
    # These settings are NOT about connection credentials (those
    # live in the vault and are managed via /integrations). They
    # control BEHAVIOR: whether the subsystem is enabled at all,
    # which panel opens by default, how diffs are rendered, and how
    # often the frontend polls.
    #
    # Persistence is a plain JSON file under
    # `backend/data/source_control/settings.json`. That choice is
    # deliberate:
    #
    #   - The file is small (~1 KB) and read once at startup.
    #   - It survives every browser clear-cache action, so a user
    #     who flips the switch ON does not have to flip it again
    #     after clearing site data.
    #   - It can be edited by hand for advanced users (the schema
    #     is validated on read, so a typo never breaks the app).
    #   - It is backed up by the same backup routine as the rest of
    #     `backend/data/`.
    #
    # Unknown fields in the JSON are ignored; missing fields fall
    # back to their defaults. This makes forward-compatible upgrades
    # trivial: a new field added in a future release simply gets its
    # default until the user changes it.

    _SETTINGS_FILE_NAME = "settings.json"

    def _settings_path(self) -> Path:
        return self._data_root / self._SETTINGS_FILE_NAME

    @staticmethod
    def _default_settings_dict() -> Dict[str, Any]:
        """
        The authoritative defaults for the subsystem.

        Kept as a plain dict (not the Pydantic model) so that
        `read_settings` can be written without constructing a model
        when the file is missing (the common case at first run).

        The branch-protection fields below are editable from the
        UI settings page. Their defaults match the historical
        hardcoded values in `domain.policies` so that existing
        deployments behave identically after the upgrade — the
        difference is that they can now be customized instead of
        being locked into the source code.

        The four new fields (Task 1.1, Phase 1):

            protected_branches
                Replaces the hardcoded `DEFAULT_PROTECTED_BRANCH_PATTERNS`
                when non-empty. The default list is intentionally
                SHORTER than the hardcoded one (main/master/trunk
                vs the full 18-pattern list) so that the UI can
                display a comprehensible starting set. Users who
                need the full list can add the extra patterns
                explicitly, or leave the setting absent and the
                policy engine falls back to its built-in defaults.

            user_override_protected
                Per-branch exemptions. A branch listed here is
                treated as unprotected even if it matches a
                protected pattern. This is how a repository owner
                expresses "I know what I'm doing, let me push to
                main."

            auto_create_feature_branch
                When True (default), a push targeting a protected
                branch is redirected to a new `peldrun/manual-XXX`
                feature branch instead of being refused. The new
                branch name is recorded in the operation metadata
                and surfaced in the UI so the user always knows
                where the commit landed.

            allow_push_to_protected
                Fallback policy when auto-branching is disabled.
                Defaults to "ask", which requires the user to
                grant an explicit `push_to_protected` approval
                scope during review. This is the safest behavior
                that still lets the owner override when needed.
        """
        return {
            "enabled": False,
            "default_panel_mode": "workspace",
            "default_source_control_mode": "browse",
            "auto_open_on_operation": True,
            "diff_view_mode": "unified",
            "diff_sample_lines": 500,
            "polling_interval_ms": 3000,
            "list_page_size": 30,
            # ---- Branch protection (Task 1.1, Phase 1) ----
            "protected_branches": ("main", "master", "trunk"),
            "user_override_protected": (),
            "auto_create_feature_branch": True,
            "allow_push_to_protected": "ask",
        }

    async def read_settings(self) -> SourceControlSettingsResponse:
        """
        Return the current settings, or the factory defaults when
        no settings file exists.

        A corrupt file is treated as "no file": the defaults are
        returned instead of surfacing a 500 to the UI. The corrupt
        file is logged and left on disk so an operator can inspect
        it; the next `write_settings` call overwrites it.
        """
        path = self._settings_path()
        defaults = self._default_settings_dict()

        if not path.exists():
            return SourceControlSettingsResponse(**defaults)

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to read settings file %s: %s. Using defaults.",
                path,
                type(exc).__name__,
            )
            return SourceControlSettingsResponse(**defaults)

        if not isinstance(data, dict):
            logger.warning(
                "Settings file %s is not a JSON object. Using defaults.",
                path,
            )
            return SourceControlSettingsResponse(**defaults)

        # Merge user values on top of the defaults so a partial file
        # (e.g. from an older release) does not break loading. Also
        # filters unknown keys silently, so a stale field from an
        # older version never produces a validation error.
        merged = {**defaults}
        for key in defaults.keys():
            if key in data:
                merged[key] = data[key]

        try:
            return SourceControlSettingsResponse(**merged)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Settings file %s failed validation: %s. Using defaults.",
                path,
                type(exc).__name__,
            )
            return SourceControlSettingsResponse(**defaults)

    async def write_settings(
        self,
        payload: UpdateSourceControlSettingsRequest,
    ) -> SourceControlSettingsResponse:
        """
        Merge `payload` into the current settings and persist.

        A partial update (only some fields present) is the common
        case: the settings page writes one field at a time, so we
        must not reset the others. Values from the payload are
        validated by the Pydantic model before reaching this
        method, so a bad value never reaches disk.
        """
        current = await self.read_settings()
        current_dict = current.model_dump()

        # Only merge fields the caller actually provided.
        incoming = payload.model_dump(exclude_none=True)
        merged = {**current_dict, **incoming}

        # Validate the merged result so an inconsistent combination
        # (e.g. an out-of-range number) never lands on disk.
        try:
            merged_model = SourceControlSettingsResponse(**merged)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(
                f"Invalid settings payload: {exc}",
                details={"merged": merged},
            ) from exc

        # Write atomically: temp file + os.replace, so a crash
        # mid-write never leaves a half-written settings file.
        path = self._settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(merged_model.model_dump(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)

        self._audit(
            AuditEventType.OTHER,
            action="write_source_control_settings",
            message="Updated Source Control subsystem settings.",
            payload=dict(incoming),
            actor="user",
        )

        return merged_model


# Global singleton cache for FastAPI dependency injection
_facade_instance: Optional[SourceControlFacade] = None


def get_source_control_facade() -> SourceControlFacade:
    """Dependency injection provider yielding the SourceControlFacade singleton."""
    global _facade_instance
    if _facade_instance is None:
        _facade_instance = SourceControlFacade()
    return _facade_instance
