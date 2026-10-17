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
    from ...integrations.source_control.errors import (
        AuthRequiredError,
        NotFoundError,
        PermissionDeniedError,
        RemoteStateChangedError,
        ValidationError,
    )
    from ...integrations.source_control.security.audit import (
        AuditEventType,
        AuditSeverity,
    )
    from ...integrations.source_control.security.oauth_flow import (
        OAuthFlowCoordinator,
        OAuthProviderCallbacks,
    )
    from ...integrations.source_control.security.policy_engine import (
        PolicyResolutionContext,
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
    from backend.integrations.source_control.errors import (
        AuthRequiredError,
        NotFoundError,
        PermissionDeniedError,
        RemoteStateChangedError,
        ValidationError,
    )
    from backend.integrations.source_control.security.audit import (
        AuditEventType,
        AuditSeverity,
    )
    from backend.integrations.source_control.security.oauth_flow import (
        OAuthFlowCoordinator,
        OAuthProviderCallbacks,
    )
    from backend.integrations.source_control.security.policy_engine import (
        PolicyResolutionContext,
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


logger = logging.getLogger(__name__)


#: Default OAuth redirect URI. Used only when the caller does not
#: supply one. Matches the loopback-only policy enforced by the OAuth
#: coordinator.
_DEFAULT_OAUTH_REDIRECT = "http://127.0.0.1:8765/oauth/callback"

#: Name of the facade-private connection index file. This file
#: contains only non-secret metadata (provider, account ID, base URL,
#: username); tokens live in the OS credential vault.
_CONNECTIONS_FILE_NAME = "connections.json"

#: Maximum number of diff sample lines returned to the UI.
_MAX_DIFF_SAMPLE_LINES = 500


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


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
    ) -> None:
        # Provider registry
        self.registry = registry or get_default_registry()
        register_builtin_providers(registry=self.registry)

        # Credential and security services
        self.vault = vault or get_default_vault()
        self.token_service = token_service or get_default_token_service()
        self.policy_engine = policy_engine or get_default_policy_engine()
        self.approval_gate = approval_gate or get_default_approval_gate()
        self.audit_logger = audit_logger or get_default_audit_logger()

        # Operation service (deferred import to avoid circular import)
        if operation_service is not None:
            self.operation_service = operation_service
        else:
            from backend.integrations.source_control.operations.service import (
                get_default_operation_service,
            )
            self.operation_service = get_default_operation_service()

        # Workspace root and path policy
        resolved_base = (
            Path(base_workspace_dir or os.getcwd()).resolve()
            / "workspaces"
            / "source_control"
        )
        resolved_base.mkdir(parents=True, exist_ok=True)
        self._workspace_root = resolved_base

        from backend.integrations.source_control.git.path_policy import (
            WorkspacePathPolicy,
        )
        self._path_policy = WorkspacePathPolicy(root=resolved_base)

        # Git services share the same path policy so that all
        # operations agree on the workspace root.
        if workspace_manager is not None:
            self.workspace_manager = workspace_manager
        else:
            self.workspace_manager = GitWorkspaceManager(
                path_policy=self._path_policy
            )

        self.diff_engine = diff_engine or GitDiffEngine(
            path_policy=self._path_policy
        )
        self.push_manager = push_manager or GitPushManager(
            path_policy=self._path_policy
        )

        # OAuth
        self.oauth_coordinator = oauth_coordinator or OAuthFlowCoordinator()

        # Facade-private connection registry. This is NOT a secret
        # store: only identifiers and timestamps are kept here.
        self._connections: Dict[str, _ConnectionRecord] = {}
        self._load_connections()

    # ------------------------------------------------------------------
    # 1. Connection registry helpers
    # ------------------------------------------------------------------

    def _connections_path(self) -> Path:
        return self._workspace_root / _CONNECTIONS_FILE_NAME

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

    def _default_provider_id(self) -> ProviderId:
        """Return the provider to use when the request does not specify one."""
        if "github" in self._connections:
            return ProviderId.GITHUB
        if self._connections:
            try:
                return ProviderId(next(iter(self._connections.keys())))
            except ValueError:
                pass
        return ProviderId.GITHUB

    def _require_token(
        self,
        provider_id: ProviderId,
    ) -> str:
        """
        Return the current access token for the given provider.

        Raises `AuthRequiredError` when no connection exists or when
        the token is missing from the vault.
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
        if not token:
            raise AuthRequiredError(
                f"Credentials for '{provider_id.value}' are missing."
            )
        return token

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

    async def begin_connection(
        self, provider: str, payload: ConnectBeginRequest
    ) -> ConnectBeginResponse:
        """Start an OAuth 2.0 PKCE handshake and return the authorization URL."""
        provider_id = ProviderId(provider.strip().lower())

        # Ensure the adapter exists.
        self.registry.get(provider_id.value)

        scopes = getattr(payload, "scopes", None) or None
        redirect_uri = (
            getattr(payload, "redirect_uri", None) or _DEFAULT_OAUTH_REDIRECT
        )

        result = self.oauth_coordinator.begin_authorization(
            provider_id,
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

        auth_result = await self.oauth_coordinator.complete_authorization(
            provider_id,
            state=state,
            code=code,
            redirect_uri=redirect_uri,
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
        token = self._require_token(provider_id)

        repos = await adapter.list_repositories(
            query=query, limit=limit, token=token
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
                description=(
                    r.metadata.get("description") if r.metadata else None
                ),
                metadata=dict(r.metadata or {}),
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
        token = self._require_token(provider_id)

        r: RepositoryRef = await adapter.get_repository(
            repository_id, token=token
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
            description=(r.metadata.get("description") if r.metadata else None),
            metadata=dict(r.metadata or {}),
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
        token = self._require_token(provider_id)

        repo = await adapter.get_repository(repository_id, token=token)
        branches = await adapter.list_branches(
            repo=repo, limit=limit, token=token
        )

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
        token = self._require_token(provider_id)

        repo = await adapter.get_repository(repository_id, token=token)

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
        """Read a remote file at a given revision."""
        provider_id = (
            ProviderId(provider.strip().lower())
            if provider
            else self._default_provider_id()
        )
        adapter = self.registry.get(provider_id.value)
        token = self._require_token(provider_id)

        repo = await adapter.get_repository(repository_id, token=token)
        raw_bytes = await adapter.read_file(
            repo=repo, file_path=file_path, ref=ref, token=token
        )

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

    # ------------------------------------------------------------------
    # 5. Workspaces, Diffs & CI Checks
    # ------------------------------------------------------------------

    async def create_workspace(
        self, payload: CreateWorkspaceRequest
    ) -> WorkspaceResponse:
        """Create an isolated local workspace bound to a real repository."""
        provider_id = self._default_provider_id()
        adapter = self.registry.get(provider_id.value)
        token = self._require_token(provider_id)

        repo = await adapter.get_repository(
            payload.repository_id, token=token
        )

        policy_ctx = PolicyResolutionContext(
            operation="create_workspace",
            provider=provider_id,
            repository=repo,
            target_branch=getattr(payload, "branch_name", None),
            actor="agent",
        )
        self.policy_engine.require(policy_ctx)

        ws_ref = await self.workspace_manager.create_workspace(
            repo=repo,
            base_sha=getattr(payload, "base_sha", None),
            source_branch=getattr(payload, "branch_name", None),
            use_worktree=True,
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

    async def inspect_workspace_diff(
        self, workspace_id: str, base_ref: Optional[str] = None
    ) -> DiffSummaryResponse:
        """Compute the diff of a workspace and return a review payload."""
        ws_ref = self.workspace_manager.get_workspace(workspace_id)
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
        """Query CI checks for the workspace commit."""
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
        token = self._require_token(provider_id)

        repo = await adapter.get_repository(
            ws_ref.repository_id, token=token
        )
        effective_ref = (
            getattr(payload, "ref", None)
            or ws_ref.head_sha
            or ws_ref.current_branch
        )

        summary: CheckSummaryRef = await adapter.get_check_summary(
            repo=repo, ref=effective_ref, token=token
        )

        runs = [
            CheckRunResponse(
                name=r.name,
                status=r.status,
                conclusion=r.conclusion,
                web_url=(
                    redact_url(getattr(r, "web_url", "") or "") or None
                ),
                started_at=getattr(r, "started_at", None),
                completed_at=getattr(r, "completed_at", None),
            )
            for r in (summary.runs or ())
        ]

        total = getattr(summary, "total", 0)
        succeeded = getattr(summary, "succeeded", 0)
        failed = getattr(summary, "failed", 0)
        pending = getattr(summary, "pending", 0)

        if failed > 0:
            overall_state = "failure"
        elif pending > 0:
            overall_state = "pending"
        elif total == 0:
            overall_state = "no_checks"
        else:
            overall_state = "success"

        is_passing = overall_state in ("success", "neutral")

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

        policy_ctx = PolicyResolutionContext(
            operation=payload.operation_type,
            provider=provider_id,
            target_branch=clean_target,
            actor="agent",
        )
        self.policy_engine.require(policy_ctx)

        metadata: Dict[str, Any] = {}
        title = getattr(payload, "title", None)
        description = getattr(payload, "description", None)
        if title:
            metadata["title"] = redact_text(title)
        if description:
            metadata["description"] = redact_text(description)

        record: OperationRecord = await self.operation_service.create_operation(
            repository_id=payload.repository_id,
            kind=payload.operation_type,
            provider=provider_id,
            target_branch=clean_target,
            workspace_id=getattr(payload, "workspace_id", None),
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
        return await self._map_operation_response(record)

    async def request_operation_approval(
        self, operation_id: str
    ) -> ApprovalTicketResponse:
        """Freeze a diff proposal and open an approval ticket."""
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

        ticket: ApprovalTicket = self.approval_gate.create_ticket(
            operation_id=record.operation_id,
            repository_id=record.repository_id,
            operation=record.kind,
            base_sha=base_sha,
            diff_sha256=diff_hash,
            target_branch=record.target_branch or "",
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
            operation_type=record.kind,
            repository_id=record.repository_id,
            target_branch=ticket.target_branch,
            base_sha=ticket.base_sha,
            diff_sha256=ticket.diff_sha256,
            total_files=len(summary.files),
            touches_workflow_files=bool(workflow_files),
            is_protected_target=is_protected_branch(ticket.target_branch),
            approved_by_user=ticket.approved_by_user,
            expires_at=ticket.expires_at,
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

        # Enforce the protected-branch rule before touching the remote.
        ticket = self.approval_gate.get_ticket(payload.approval_id)
        if ticket is None:
            raise ValidationError(
                f"Approval ticket '{payload.approval_id}' is not known."
            )
        if is_protected_branch(ticket.target_branch):
            raise PermissionDeniedError(
                f"Pushing to protected branch '{ticket.target_branch}' "
                "is forbidden by policy."
            )

        # Consume the ticket only after all local checks pass.
        consumed = self.approval_gate.consume(
            approval_id=payload.approval_id,
            expected_operation_id=operation_id,
            expected_repository_id=record.repository_id,
            expected_operation=record.kind,
            expected_base_sha=record.base_sha or ticket.base_sha,
            expected_diff_sha256=current_hash,
            expected_target_branch=record.target_branch or ticket.target_branch,
            actor="system",
        )

        provider_id = (
            record.provider
            if isinstance(record.provider, ProviderId)
            else ProviderId(str(record.provider))
        )
        token = self._require_token(provider_id)

        # Transition to PUSHING before the remote side effect so that
        # a crash mid-push leaves a recoverable state.
        await self.operation_service.transition(
            operation_id,
            OperationStatus.PUSHING,
            metadata={"remote": getattr(payload, "remote_name", "origin")},
            actor="system",
        )

        try:
            push_result: PushResult = await self.push_manager.push_branch(
                workspace_id=record.workspace_id,
                local_branch=ws_ref.current_branch,
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
            await self.operation_service.transition(
                operation_id,
                OperationStatus.FAILED_RETRYABLE,
                reason=f"Push failed: {type(exc).__name__}",
                metadata={"error": type(exc).__name__},
                actor="system",
            )
            raise

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

        await self.operation_service.transition(
            operation_id,
            OperationStatus.PUSHED,
            metadata={
                "local_sha": push_result.local_sha,
                "remote_sha": push_result.remote_sha,
            },
            actor="system",
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

        provider_id = ProviderId(
            (getattr(payload, "provider", None) or self._default_provider_id().value)
            .strip()
            .lower()
        )
        adapter = self.registry.get(provider_id.value)
        token = self._require_token(provider_id)

        # Consume the ticket with the record's bound fields.
        self.approval_gate.consume(
            approval_id=approval_id,
            expected_operation_id=operation_id,
            expected_repository_id=record.repository_id,
            expected_operation=record.kind,
            expected_base_sha=record.base_sha or "",
            expected_diff_sha256=record.diff_sha256 or "",
            expected_target_branch=record.target_branch
            or payload.target_branch,
            actor="system",
        )

        repo = await adapter.get_repository(
            payload.repository_id, token=token
        )
        cr: ChangeRequestRef = await adapter.create_change_request(
            repo=repo,
            source_branch=payload.source_branch,
            target_branch=payload.target_branch,
            title=redact_text(payload.title),
            body=redact_text(getattr(payload, "body", "") or ""),
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
            title=payload.title,
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
        )


# Global singleton cache for FastAPI dependency injection
_facade_instance: Optional[SourceControlFacade] = None


def get_source_control_facade() -> SourceControlFacade:
    """Dependency injection provider yielding the SourceControlFacade singleton."""
    global _facade_instance
    if _facade_instance is None:
        _facade_instance = SourceControlFacade()
    return _facade_instance
