from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

# Resilient imports for source control integration components
try:
    from ...integrations.source_control import (
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
        OperationState,
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
    from ...integrations.source_control.errors import (
        AuthRequiredError,
        NotFoundError,
        PermissionDeniedError,
        RemoteStateChangedError,
        ValidationError,
    )
    from ...integrations.source_control.schemas import (
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
    from ...integrations.source_control.security.oauth_flow import OAuthFlowCoordinator
except (ImportError, ValueError):
    from backend.integrations.source_control import (
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
        OperationState,
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
    from backend.integrations.source_control.errors import (
        AuthRequiredError,
        NotFoundError,
        PermissionDeniedError,
        RemoteStateChangedError,
        ValidationError,
    )
    from backend.integrations.source_control.schemas import (
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
    from backend.integrations.source_control.security.oauth_flow import OAuthFlowCoordinator


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
    ) -> None:
        self.registry = registry or get_default_registry()
        register_builtin_providers(self.registry)

        self.vault = vault or get_default_vault()
        self.token_service = token_service or get_default_token_service()
        self.policy_engine = policy_engine or get_default_policy_engine()
        self.approval_gate = approval_gate or get_default_approval_gate()
        self.audit_logger = audit_logger or get_default_audit_logger()

        if operation_service:
            self.operation_service = operation_service
        else:
            from backend.integrations.source_control.operations.service import get_default_operation_service
            self.operation_service = get_default_operation_service()

        resolved_base = Path(base_workspace_dir or os.getcwd()).resolve() / "workspaces" / "source_control"
        self.workspace_manager = workspace_manager or GitWorkspaceManager(base_workspace_dir=resolved_base)
        self.diff_engine = diff_engine or GitDiffEngine()
        self.push_manager = push_manager or GitPushManager()
        self.oauth_coordinator = oauth_coordinator or OAuthFlowCoordinator()

    # -----------------------------------------------------------------------
    # 1. Provider & Connection Orchestration
    # -----------------------------------------------------------------------

    async def list_providers(self) -> Sequence[ProviderDescriptorResponse]:
        """Discovers registered providers, feature support matrices, and connection status."""
        descriptors: list[ProviderDescriptorResponse] = []
        for pid in self.registry.list_registered():
            adapter = self.registry.get(pid)
            token = await self.token_service.get_access_token(adapter.provider_id)
            caps = adapter.capabilities

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
            web_url = getattr(manifest, "web_base_url", "https://github.com") if manifest else "https://github.com"
            api_url = getattr(manifest, "api_base_url", "https://api.github.com") if manifest else "https://api.github.com"

            descriptors.append(
                ProviderDescriptorResponse(
                    provider_id=adapter.provider_id.value,
                    display_name=adapter.provider_id.value.title() + " Cloud",
                    is_connected=bool(token),
                    capabilities=caps_resp,
                    web_base_url=web_url,
                    api_base_url=api_url,
                )
            )
        return descriptors

    async def list_connections(self) -> Sequence[ConnectionStatusResponse]:
        """Inspects connection status and account details for all supported providers."""
        connections: list[ConnectionStatusResponse] = []
        for pid in self.registry.list_registered():
            adapter = self.registry.get(pid)
            token = await self.token_service.get_access_token(adapter.provider_id)
            if not token:
                connections.append(
                    ConnectionStatusResponse(
                        provider=adapter.provider_id.value,
                        is_connected=False,
                    )
                )
                continue

            try:
                account: ProviderAccount = await adapter.get_authenticated_account(token)
                connections.append(
                    ConnectionStatusResponse(
                        provider=account.provider.value,
                        is_connected=account.connected,
                        account_id=account.account_id,
                        username=account.username,
                        base_url=account.base_url,
                        last_validated_at=datetime.utcnow(),
                    )
                )
            except Exception:
                connections.append(
                    ConnectionStatusResponse(
                        provider=adapter.provider_id.value,
                        is_connected=False,
                    )
                )
        return connections

    async def begin_connection(
        self, provider: str, payload: ConnectBeginRequest
    ) -> ConnectBeginResponse:
        """Initiates an OAuth 2.0 PKCE handshake and returns authorization URL."""
        provider_id = ProviderId(provider.strip().lower())
        adapter = self.registry.get(provider_id.value)
        manifest = getattr(adapter, "manifest", None)
        client_id = getattr(manifest, "client_id", "peldrun_public_client") if manifest else "peldrun_public_client"

        session = self.oauth_coordinator.start_flow(
            provider=provider_id,
            client_id=client_id,
            scopes=payload.scopes,
            redirect_uri=payload.redirect_uri,
        )

        auth_endpoint = getattr(manifest, "oauth_authorize_url", "https://github.com/login/oauth/authorize") if manifest else "https://github.com/login/oauth/authorize"
        auth_url = session.build_authorization_url(auth_endpoint)

        self.audit_logger.log_event(
            event_type="oauth_begin",
            provider=provider_id.value,
            details={"state": session.state, "redirect_uri": payload.redirect_uri},
        )

        return ConnectBeginResponse(
            provider=provider_id.value,
            authorization_url=auth_url,
            state=session.state,
            expires_in_seconds=600,
        )

    async def complete_oauth_callback(
        self, provider: str, code: str, state: str
    ) -> ActionSuccessResponse:
        """Completes OAuth PKCE code exchange, verifies token, and stores in secure vault."""
        provider_id = ProviderId(provider.strip().lower())
        adapter = self.registry.get(provider_id.value)

        session = self.oauth_coordinator.get_flow(state)
        if not session:
            raise ValidationError(f"Invalid or expired OAuth state parameter '{state}'.")

        token_response = await adapter.auth.exchange_code_for_token(
            code=code,
            code_verifier=session.code_verifier,
            redirect_uri=session.redirect_uri,
        )

        await self.token_service.save_tokens(
            provider_id=provider_id,
            access_token=token_response.access_token,
            refresh_token=token_response.refresh_token,
            expires_in=token_response.expires_in,
            scopes=token_response.scope.split(",") if token_response.scope else (),
        )

        self.oauth_coordinator.clear_flow(state)

        account = await adapter.get_authenticated_account(token_response.access_token)

        self.audit_logger.log_event(
            event_type="oauth_connected",
            provider=provider_id.value,
            details={"username": account.username, "account_id": account.account_id},
        )

        return ActionSuccessResponse(
            message=f"Successfully connected to {provider_id.value.title()} as '{account.username}'."
        )

    async def validate_connection(
        self, provider: str, payload: ValidateConnectionRequest
    ) -> ConnectionStatusResponse:
        """Validates connection by querying authenticated identity with optional refresh."""
        provider_id = ProviderId(provider.strip().lower())
        adapter = self.registry.get(provider_id.value)

        token = await self.token_service.get_access_token(
            provider_id=provider_id, force_refresh=payload.force_refresh
        )
        if not token:
            raise AuthRequiredError(f"No active credentials in vault for {provider_id.value.title()}.")

        account = await adapter.get_authenticated_account(token)

        return ConnectionStatusResponse(
            provider=account.provider.value,
            is_connected=account.connected,
            account_id=account.account_id,
            username=account.username,
            base_url=account.base_url,
            last_validated_at=datetime.utcnow(),
        )

    async def disconnect_provider(
        self, provider: str, payload: DisconnectRequest
    ) -> ActionSuccessResponse:
        """Revokes upstream token if requested and purges credentials from OS vault."""
        provider_id = ProviderId(provider.strip().lower())
        adapter = self.registry.get(provider_id.value)

        token = await self.token_service.get_access_token(provider_id)
        if token and payload.revoke_upstream_token:
            try:
                if hasattr(adapter.auth, "revoke_token"):
                    await adapter.auth.revoke_token(token)
            except Exception:
                pass

        await self.token_service.revoke_tokens(provider_id)

        self.audit_logger.log_event(
            event_type="provider_disconnected",
            provider=provider_id.value,
            details={"revoked_upstream": payload.revoke_upstream_token},
        )

        return ActionSuccessResponse(
            message=f"Disconnected provider '{provider_id.value.title()}' and purged credentials."
        )

    # -----------------------------------------------------------------------
    # 2. Repository & Remote Inspection
    # -----------------------------------------------------------------------

    async def list_repositories(
        self,
        provider: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 30,
    ) -> Sequence[RepositoryResponse]:
        """Lists accessible repositories with secret-sanitized clone URLs."""
        provider_id = ProviderId((provider or "github").strip().lower())
        adapter = self.registry.get(provider_id.value)

        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(f"Authentication required for {provider_id.value.title()}.")

        repos = await adapter.list_repositories(query=query, limit=limit, token=token)

        return [
            RepositoryResponse(
                provider=r.provider.value,
                repository_id=r.repository_id,
                namespace=r.namespace,
                name=r.name,
                full_name=f"{r.namespace}/{r.name}",
                default_branch=r.default_branch,
                web_url=redact_url(r.web_url),
                clone_url=redact_url(r.clone_url),
                is_private=r.is_private,
                description=r.metadata.get("description") if r.metadata else None,
                metadata=r.metadata,
            )
            for r in repos
        ]

    async def get_repository(
        self, repository_id: str, provider: Optional[str] = "github"
    ) -> RepositoryResponse:
        """Retrieves details of a specific repository."""
        provider_id = ProviderId((provider or "github").strip().lower())
        adapter = self.registry.get(provider_id.value)

        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(f"Authentication required for {provider_id.value.title()}.")

        r: RepositoryRef = await adapter.get_repository(repository_id, token=token)

        return RepositoryResponse(
            provider=r.provider.value,
            repository_id=r.repository_id,
            namespace=r.namespace,
            name=r.name,
            full_name=f"{r.namespace}/{r.name}",
            default_branch=r.default_branch,
            web_url=redact_url(r.web_url),
            clone_url=redact_url(r.clone_url),
            is_private=r.is_private,
            description=r.metadata.get("description") if r.metadata else None,
            metadata=r.metadata,
        )

    async def list_branches(
        self,
        repository_id: str,
        provider: Optional[str] = "github",
        limit: int = 50,
    ) -> Sequence[BranchResponse]:
        """Lists branches for a given repository."""
        provider_id = ProviderId((provider or "github").strip().lower())
        adapter = self.registry.get(provider_id.value)

        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(f"Authentication required for {provider_id.value.title()}.")

        repo = await adapter.get_repository(repository_id, token=token)
        branches = await adapter.list_branches(repo=repo, limit=limit, token=token)

        return [
            BranchResponse(
                name=b.name,
                head_sha=b.head_sha,
                is_default=b.is_default,
                is_protected=is_protected_branch(b.name),
            )
            for b in branches
        ]

    async def create_branch(
        self,
        repository_id: str,
        payload: CreateBranchRequest,
        provider: Optional[str] = "github",
    ) -> BranchResponse:
        """Creates a new branch on the remote repository."""
        provider_id = ProviderId((provider or "github").strip().lower())
        clean_branch = validate_branch_name(payload.branch_name)

        allowed, reason = self.policy_engine.is_operation_allowed(
            operation="create_branch",
            provider=provider_id.value,
            repository=repository_id,
            target_branch=clean_branch,
        )
        if not allowed:
            raise PermissionDeniedError(f"Policy denies branch creation: {reason}")

        adapter = self.registry.get(provider_id.value)
        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(f"Authentication required for {provider_id.value.title()}.")

        repo = await adapter.get_repository(repository_id, token=token)
        from_sha = payload.from_sha
        if not from_sha:
            def_branch = await adapter.get_branch(repo, repo.default_branch, token=token)
            from_sha = def_branch.head_sha

        if not from_sha:
            raise ValidationError("Could not resolve starting commit SHA.")

        b: BranchRef = await adapter.create_branch(
            repo=repo, branch_name=clean_branch, from_sha=from_sha, token=token
        )

        self.audit_logger.log_event(
            event_type="branch_created",
            provider=provider_id.value,
            repository=repository_id,
            details={"branch_name": clean_branch, "from_sha": from_sha},
        )

        return BranchResponse(
            name=b.name,
            head_sha=b.head_sha,
            is_default=b.is_default,
            is_protected=False,
        )

    async def read_file(
        self,
        repository_id: str,
        file_path: str,
        ref: Optional[str] = None,
        provider: Optional[str] = "github",
    ) -> FileReadResponse:
        """Reads a remote file with null-byte detection and budget truncation."""
        provider_id = ProviderId((provider or "github").strip().lower())
        adapter = self.registry.get(provider_id.value)

        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(f"Authentication required for {provider_id.value.title()}.")

        repo = await adapter.get_repository(repository_id, token=token)
        raw_bytes = await adapter.read_file(repo=repo, file_path=file_path, ref=ref, token=token)

        effective_ref = ref or repo.default_branch
        size_bytes = len(raw_bytes)

        if b"\x00" in raw_bytes:
            return FileReadResponse(
                repository_id=repository_id,
                file_path=file_path,
                ref=effective_ref,
                is_binary=True,
                size_bytes=size_bytes,
                is_truncated=False,
                content=None,
            )

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = raw_bytes.decode("latin-1", errors="replace")

        truncated = False
        if len(text.encode("utf-8")) > 100_000:
            text = text[:100_000] + "\n\n... [Truncated for context size] ..."
            truncated = True

        return FileReadResponse(
            repository_id=repository_id,
            file_path=file_path,
            ref=effective_ref,
            is_binary=False,
            size_bytes=size_bytes,
            is_truncated=truncated,
            content=text,
        )

    # -----------------------------------------------------------------------
    # 3. Workspaces, Diffs & CI Checks
    # -----------------------------------------------------------------------

    async def create_workspace(self, payload: CreateWorkspaceRequest) -> WorkspaceResponse:
        """Creates an isolated local Git workspace or worktree."""
        allowed, reason = self.policy_engine.is_operation_allowed(
            operation="create_workspace",
            repository=payload.repository_id,
            target_branch=payload.branch_name,
        )
        if not allowed:
            raise PermissionDeniedError(f"Policy denies creating workspace: {reason}")

        ws_ref = await self.workspace_manager.create_workspace(
            repository_id=payload.repository_id,
            base_sha=payload.base_sha or "HEAD",
            branch_name=payload.branch_name,
        )

        self.audit_logger.log_event(
            event_type="workspace_created",
            repository=payload.repository_id,
            details={"workspace_id": ws_ref.workspace_id, "branch": ws_ref.current_branch},
        )

        return WorkspaceResponse(
            workspace_id=ws_ref.workspace_id,
            repository_id=ws_ref.repository_id,
            current_branch=ws_ref.current_branch,
            base_sha=ws_ref.base_sha,
            head_sha=ws_ref.head_sha,
            is_dirty=ws_ref.is_dirty,
            created_at=ws_ref.created_at,
        )

    async def get_workspace(self, workspace_id: str) -> WorkspaceResponse:
        """Retrieves details of an isolated local workspace."""
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if not ws_ref:
            raise NotFoundError(f"Workspace '{workspace_id}' was not found.")

        return WorkspaceResponse(
            workspace_id=ws_ref.workspace_id,
            repository_id=ws_ref.repository_id,
            current_branch=ws_ref.current_branch,
            base_sha=ws_ref.base_sha,
            head_sha=ws_ref.head_sha,
            is_dirty=ws_ref.is_dirty,
            created_at=ws_ref.created_at,
        )

    async def inspect_workspace_diff(
        self, workspace_id: str, base_ref: Optional[str] = None
    ) -> DiffSummaryResponse:
        """Computes unified diff, detects CI workflow files, and calculates SHA-256 fingerprint."""
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if not ws_ref:
            raise NotFoundError(f"Workspace '{workspace_id}' was not found.")

        target_dir = Path(ws_ref.workspace_path)
        effective_base = base_ref or ws_ref.base_sha or "HEAD"

        summary: DiffSummary = await self.diff_engine.compute_diff(
            cwd=target_dir, base_ref=effective_base
        )
        raw_diff: str = await self.diff_engine.get_raw_diff(
            cwd=target_dir, base_ref=effective_base
        )

        touches_workflow = self.diff_engine.contains_workflow_changes(summary)
        workflow_files = list(self.diff_engine.get_workflow_files(summary))
        has_binary = self.diff_engine.contains_binary_files(summary)

        file_diffs = [
            FileDiffResponse(
                path=f.path,
                change_type=f.change_type.value,
                insertions=f.insertions,
                deletions=f.deletions,
                old_path=f.old_path,
                is_binary=f.is_binary,
                is_workflow_file=is_workflow_path(f.path),
            )
            for f in summary.files
        ]

        clean_diff = redact_text(raw_diff)
        lines = clean_diff.splitlines()
        truncated = False
        if len(lines) > 500:
            clean_diff = "\n".join(lines[:500]) + "\n\n... [Diff sample truncated] ..."
            truncated = True

        return DiffSummaryResponse(
            base_sha=summary.base_sha,
            head_sha=summary.head_sha,
            diff_sha256=summary.diff_sha256,
            total_files=summary.total_files,
            insertions=summary.insertions,
            deletions=summary.deletions,
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
        """Queries CI checks and commit statuses for workspace commit reference."""
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
        if not ws_ref:
            raise NotFoundError(f"Workspace '{workspace_id}' was not found.")

        provider_id = ProviderId(payload.provider.strip().lower())
        adapter = self.registry.get(provider_id.value)
        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(f"Authentication required for {provider_id.value.title()}.")

        repo = await adapter.get_repository(ws_ref.repository_id, token=token)
        effective_ref = payload.ref or ws_ref.head_sha or ws_ref.current_branch

        summary: CheckSummaryRef = await adapter.get_check_summary(
            repo=repo, ref=effective_ref, token=token
        )

        runs = [
            CheckRunResponse(
                name=r.name,
                status=r.status,
                conclusion=r.conclusion,
                web_url=redact_url(r.web_url) if r.web_url else None,
                started_at=r.started_at,
                completed_at=r.completed_at,
            )
            for r in summary.runs
        ]

        is_passing = summary.overall_state in {"success", "neutral"}

        return CheckSummaryResponse(
            ref=summary.ref,
            overall_state=summary.overall_state,
            is_passing=is_passing,
            total_count=summary.total_count,
            success_count=summary.success_count,
            failure_count=summary.failure_count,
            pending_count=summary.pending_count,
            message=f"Overall status: {summary.overall_state.upper()}",
            runs=runs,
        )

    # -----------------------------------------------------------------------
    # 4. Durable Operations, Approvals & Remote Execution
    # -----------------------------------------------------------------------

    async def create_operation(self, payload: CreateOperationRequest) -> OperationResponse:
        """Registers a new durable source control workflow operation."""
        provider_id = ProviderId(payload.provider.strip().lower())
        clean_target = validate_branch_name(payload.target_branch)

        allowed, reason = self.policy_engine.is_operation_allowed(
            operation=payload.operation_type,
            provider=provider_id.value,
            repository=payload.repository_id,
            target_branch=clean_target,
        )
        if not allowed:
            raise PermissionDeniedError(f"Policy denies operation: {reason}")

        record: OperationRecord = await self.operation_service.create_operation(
            operation_type=payload.operation_type,
            provider=provider_id.value,
            repository_id=payload.repository_id,
            target_branch=clean_target,
            workspace_id=payload.workspace_id,
            title=redact_text(payload.title) if payload.title else None,
            description=redact_text(payload.description) if payload.description else None,
        )

        self.audit_logger.log_event(
            event_type="operation_created",
            provider=provider_id.value,
            repository=payload.repository_id,
            operation_id=record.operation_id,
            details={"type": payload.operation_type, "target_branch": clean_target},
        )

        return self._map_operation_response(record)

    async def get_operation(self, operation_id: str) -> OperationResponse:
        """Retrieves durable operation state and timeline history."""
        record = await self.operation_service.get_operation(operation_id)
        if not record:
            raise NotFoundError(f"Operation '{operation_id}' was not found.")
        return self._map_operation_response(record)

    async def request_operation_approval(self, operation_id: str) -> ApprovalTicketResponse:
        """Computes cryptographic diff SHA-256 and freezes proposal for explicit human review."""
        record = await self.operation_service.get_operation(operation_id)
        if not record:
            raise NotFoundError(f"Operation '{operation_id}' was not found.")

        if not record.workspace_id:
            raise ValidationError(f"Operation '{operation_id}' has no bound workspace.")

        ws_ref = self.workspace_manager.get_workspace(record.workspace_id)
        if not ws_ref:
            raise NotFoundError(f"Bound workspace '{record.workspace_id}' was not found.")

        target_dir = Path(ws_ref.workspace_path)
        summary: DiffSummary = await self.diff_engine.compute_diff(
            cwd=target_dir, base_ref=ws_ref.base_sha
        )
        if summary.total_files == 0:
            raise ValidationError("No changes detected in workspace. Cannot request approval for empty diff.")

        ticket: ApprovalTicket = self.approval_gate.create_ticket(
            operation_id=record.operation_id,
            repository_id=record.repository_id,
            operation=record.operation_type,
            base_sha=summary.base_sha,
            diff_sha256=summary.diff_sha256,
            target_branch=record.target_branch,
        )

        await self.operation_service.transition_state(
            operation_id=record.operation_id,
            target_state=OperationState.REVIEW_REQUIRED,
            metadata={
                "approval_id": ticket.approval_id,
                "diff_sha256": ticket.diff_sha256,
                "base_sha": ticket.base_sha,
                "total_files": summary.total_files,
            },
        )

        self.audit_logger.log_event(
            event_type="approval_requested",
            repository=record.repository_id,
            operation_id=record.operation_id,
            details={"approval_id": ticket.approval_id, "diff_sha256": ticket.diff_sha256},
        )

        return ApprovalTicketResponse(
            approval_id=ticket.approval_id,
            operation_id=record.operation_id,
            operation_type=record.operation_type,
            repository_id=record.repository_id,
            target_branch=ticket.target_branch,
            base_sha=ticket.base_sha,
            diff_sha256=ticket.diff_sha256,
            total_files=summary.total_files,
            touches_workflow_files=self.diff_engine.contains_workflow_changes(summary),
            is_protected_target=is_protected_branch(ticket.target_branch),
            approved_by_user=ticket.approved_by_user,
            expires_at=ticket.expires_at,
        )

    async def approve_operation(
        self, operation_id: str, payload: ApproveOperationRequest
    ) -> ApprovalTicketResponse:
        """Human approval action explicitly authorizing the pending proposal ticket."""
        record = await self.operation_service.get_operation(operation_id)
        if not record:
            raise NotFoundError(f"Operation '{operation_id}' was not found.")

        ticket = self.approval_gate.approve_ticket(
            approval_id=payload.approval_id,
            user_id=payload.user_id or "user",
            notes=payload.notes,
        )

        await self.operation_service.transition_state(
            operation_id=operation_id,
            target_state=OperationState.APPROVED,
            metadata={"approved_by": payload.user_id, "notes": payload.notes},
        )

        self.audit_logger.log_event(
            event_type="approval_granted",
            repository=record.repository_id,
            operation_id=operation_id,
            user_id=payload.user_id or "user",
            details={"approval_id": ticket.approval_id},
        )

        return ApprovalTicketResponse(
            approval_id=ticket.approval_id,
            operation_id=record.operation_id,
            operation_type=record.operation_type,
            repository_id=record.repository_id,
            target_branch=ticket.target_branch,
            base_sha=ticket.base_sha,
            diff_sha256=ticket.diff_sha256,
            total_files=0,
            approved_by_user=True,
            expires_at=ticket.expires_at,
        )

    async def reject_operation(
        self, operation_id: str, payload: RejectOperationRequest
    ) -> ActionSuccessResponse:
        """Human rejection action invalidating proposal and recording audit explanation."""
        record = await self.operation_service.get_operation(operation_id)
        if not record:
            raise NotFoundError(f"Operation '{operation_id}' was not found.")

        self.approval_gate.reject_ticket(
            approval_id=payload.approval_id,
            user_id=payload.user_id or "user",
            reason=payload.reason,
        )

        await self.operation_service.transition_state(
            operation_id=operation_id,
            target_state=OperationState.CANCELLED,
            metadata={"rejection_reason": payload.reason, "rejected_by": payload.user_id},
        )

        self.audit_logger.log_event(
            event_type="approval_rejected",
            repository=record.repository_id,
            operation_id=operation_id,
            user_id=payload.user_id or "user",
            details={"reason": payload.reason},
        )

        return ActionSuccessResponse(
            message=f"Operation '{operation_id}' was rejected: {payload.reason}"
        )

    async def push_approved_branch(
        self, operation_id: str, payload: PushBranchRequest
    ) -> PushBranchResponse:
        """Validates single-use approval ticket and pushes verified branch commits."""
        ticket: ApprovalTicket = self.approval_gate.consume_approval(payload.approval_id)

        ws_ref = self.workspace_manager.get_workspace(payload.workspace_id)
        if not ws_ref:
            raise NotFoundError(f"Workspace '{payload.workspace_id}' was not found.")

        target_dir = Path(ws_ref.workspace_path)
        current_diff = await self.diff_engine.compute_diff(
            cwd=target_dir, base_ref=ticket.base_sha
        )
        if current_diff.diff_sha256 != ticket.diff_sha256:
            raise RemoteStateChangedError("Workspace changes mutated after approval was granted.")

        if is_protected_branch(ticket.target_branch):
            raise PermissionDeniedError(f"Pushing directly to protected branch '{ticket.target_branch}' is forbidden.")

        provider_id = ProviderId.GITHUB
        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError("Provider credentials missing in vault.")

        push_res: PushResult = await self.push_manager.push_branch(
            cwd=target_dir,
            branch_name=ws_ref.current_branch,
            remote_name=payload.remote_name,
            target_branch=ticket.target_branch,
            token=token,
            force_with_lease=True,
            set_upstream=payload.set_upstream,
        )

        await self.operation_service.transition_state(
            operation_id=operation_id,
            target_state=OperationState.PUSHED,
            metadata={
                "pushed_sha": push_res.pushed_sha,
                "remote_name": payload.remote_name,
                "upstream": push_res.upstream_ref,
            },
        )

        self.audit_logger.log_event(
            event_type="branch_pushed",
            repository=ticket.repository_id,
            operation_id=operation_id,
            details={"branch": ticket.target_branch, "pushed_sha": push_res.pushed_sha},
        )

        return PushBranchResponse(
            approval_id=payload.approval_id,
            operation_id=operation_id,
            branch_name=ticket.target_branch,
            pushed_sha=push_res.pushed_sha,
            remote_name=payload.remote_name,
            upstream_ref=push_res.upstream_ref,
            message=f"Pushed branch '{ticket.target_branch}' successfully.",
        )

    async def create_change_request(
        self, operation_id: str, payload: CreateChangeRequestPayload
    ) -> ChangeRequestResponse:
        """Opens a remote Pull Request on the source control provider."""
        provider_id = ProviderId(payload.provider.strip().lower())
        adapter = self.registry.get(provider_id.value)
        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(f"Authentication required for {provider_id.value.title()}.")

        if payload.approval_id:
            self.approval_gate.consume_approval(payload.approval_id)

        repo = await adapter.get_repository(payload.repository_id, token=token)
        cr: ChangeRequestRef = await adapter.create_change_request(
            repo=repo,
            source_branch=payload.source_branch,
            target_branch=payload.target_branch,
            title=redact_text(payload.title),
            body=redact_text(payload.body or ""),
            draft=payload.draft,
            token=token,
        )

        await self.operation_service.transition_state(
            operation_id=operation_id,
            target_state=OperationState.CHANGE_REQUEST_CREATED,
            metadata={"cr_number": cr.number_or_iid, "cr_url": cr.web_url},
        )

        self.audit_logger.log_event(
            event_type="change_request_created",
            provider=provider_id.value,
            repository=payload.repository_id,
            operation_id=operation_id,
            details={"number": cr.number_or_iid, "url": cr.web_url},
        )

        return ChangeRequestResponse(
            provider=provider_id.value,
            repository_id=payload.repository_id,
            number_or_iid=cr.number_or_iid,
            web_url=redact_url(cr.web_url),
            state=cr.state,
            source_branch=cr.source_branch,
            target_branch=cr.target_branch,
            title=payload.title,
            is_draft=payload.draft,
            message=f"Pull request #{cr.number_or_iid} created successfully.",
        )

    # -----------------------------------------------------------------------
    # 5. Security Audit Trail
    # -----------------------------------------------------------------------

    async def list_audit_events(
        self, limit: int = 100, operation_id: Optional[str] = None
    ) -> Sequence[AuditLogEntryResponse]:
        """Retrieves append-only audit events."""
        events = self.audit_logger.get_events(limit=limit, operation_id=operation_id)
        return [
            AuditLogEntryResponse(
                event_id=e.event_id,
                timestamp=e.timestamp,
                event_type=e.event_type,
                user_id=e.user_id,
                provider=e.provider,
                repository=e.repository,
                operation_id=e.operation_id,
                details=e.details,
            )
            for e in events
        ]

    # -----------------------------------------------------------------------
    # Internal Helpers
    # -----------------------------------------------------------------------

    def _map_operation_response(self, record: OperationRecord) -> OperationResponse:
        events = [
            OperationEventResponse(
                event_id=ev.event_id,
                from_state=ev.from_state.value if ev.from_state else "",
                to_state=ev.to_state.value,
                timestamp=ev.timestamp,
                metadata=ev.metadata,
            )
            for ev in record.events
        ]
        return OperationResponse(
            operation_id=record.operation_id,
            operation_type=record.operation_type,
            provider=record.provider,
            repository_id=record.repository_id,
            target_branch=record.target_branch,
            current_state=record.current_state.value,
            workspace_id=record.workspace_id,
            approval_id=record.approval_id,
            change_request_url=record.metadata.get("cr_url"),
            error_code=record.metadata.get("error_code"),
            error_message=record.metadata.get("error_message"),
            created_at=record.created_at,
            updated_at=record.updated_at,
            events=events,
        )


# Global singleton cache for FastAPI dependency injection
_facade_instance: Optional[SourceControlFacade] = None


def get_source_control_facade() -> SourceControlFacade:
    """Dependency injection provider yielding the configured SourceControlFacade singleton."""
    global _facade_instance
    if _facade_instance is None:
        _facade_instance = SourceControlFacade()
    return _facade_instance
