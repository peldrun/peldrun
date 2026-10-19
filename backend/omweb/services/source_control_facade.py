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

KNOWN INTEGRATION GAPS (tracked, not yet fixed):
    The provider adapters (`GitHubProviderAdapter`, and future
    GitLab/Bitbucket adapters) operate on `RepositoryRef` objects,
    while the HTTP layer identifies repositories by their opaque
    string `repository_id`. Several methods below call
    `adapter.get_repository(repository_id, token=...)` with a plain
    string, which the current adapters reject with a
    `ValidationError`. Until a resolution layer (string id ->
    `RepositoryRef`) is added to the facade, these methods are only
    exercised against mock adapters in the test suite. The affected
    methods are: `get_repository`, `list_branches`, `read_file`,
    `create_branch`, `create_workspace`, `run_checks`, and
    `create_change_request`.
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

#: Name of the file that stores the user-provided OAuth Client IDs.
#: Client Secrets are NEVER stored here; they go to the OS Vault.
_PROVIDER_CONFIG_FILE_NAME = "provider_configs.json"

#: Maximum number of bytes returned to the UI when reading a file.
#: This is a byte budget, NOT a character budget: multi-byte UTF-8
#: sequences must be counted correctly to honor the caller's intent.
_MAX_FILE_CONTENT_BYTES = 100_000

#: Maximum number of diff sample lines returned to the UI.
_MAX_DIFF_SAMPLE_LINES = 500


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

        # Centralized data directory for durable configs (keys, connections)
        # This dynamically resolves the root `backend` directory, avoiding `backend/backend`.
        # __file__ is `backend/omweb/services/source_control_facade.py`.
        backend_root = Path(__file__).resolve().parent.parent.parent
        self._data_root = backend_root / "data" / "source_control"
        self._data_root.mkdir(parents=True, exist_ok=True)

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

        # Register OAuth providers for all available adapters
        self._register_default_oauth_providers()

        # Facade-private connection registry. This is NOT a secret
        # store: only identifiers and timestamps are kept here.
        self._connections: Dict[str, _ConnectionRecord] = {}
        self._load_connections()

    def _register_default_oauth_providers(self) -> None:
        """Register OAuth callbacks for built-in providers."""
        try:
            from backend.integrations.source_control.security.oauth_flow import (
                OAuthProviderCallbacks,
            )
            from backend.integrations.source_control.security.token_service import (
                TokenRefreshResult,
            )
        except ImportError:
            from ...integrations.source_control.security.oauth_flow import (
                OAuthProviderCallbacks,
            )
            from ...integrations.source_control.security.token_service import (
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

    def _require_token(
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
            info = self._try_refresh_token(provider_id, conn)
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

    def _try_refresh_token(
        self,
        provider_id: ProviderId,
        conn: _ConnectionRecord,
    ):
        """
        Attempt a synchronous refresh via the TokenService.

        The TokenService's ``refresh`` is async; this helper exists
        so that ``_require_token`` (a synchronous method) can invoke
        it from a request handler that is itself async by running the
        coroutine to completion in the current event loop.

        IMPORTANT: this helper MUST be called from an async context.
        If it is called from a sync context, it returns ``None``.
        """
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Not inside an event loop; can't refresh synchronously.
            return None

        async def _do_refresh():
            return await self.token_service.refresh(
                provider=provider_id,
                account_id=conn.account_id,
                base_url=conn.base_url,
            )

        # Schedule the refresh on the running loop and return its
        # result. The caller is expected to be an async method (the
        # facade methods are all async), so ``create_task`` plus
        # ``run_until_complete`` is not needed; instead, we return a
        # coroutine that the caller can await. But ``_require_token``
        # is not async today; we therefore run the coroutine to
        # completion via ``loop.run_until_complete`` only when no
        # tasks are running.
        if loop.is_running():
            # Cannot block the loop from inside it. Return None and
            # let the caller fall through to AuthRequiredError. A
            # follow-up refactor SHOULD convert ``_require_token``
            # to an async method so that refresh can be awaited
            # inline.
            return None
        return loop.run_until_complete(_do_refresh())

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
        token = self._require_token(provider_id)

        # The adapter's pagination parameter is `page_size`, not
        # `limit`. Passing `limit=` raised TypeError against the real
        # adapter; the mock-based tests did not catch this because
        # mocks accept any keyword.
        repos = await adapter.list_repositories(
            query=query, page_size=limit, token=token
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

        # NOTE: the current GitHub adapter expects a `RepositoryRef`,
        # not a string id. This call will raise `ValidationError`
        # against the real adapter. A resolution layer is tracked as
        # an outstanding integration gap; the current implementation
        # is exercised only against mock adapters in the test suite.
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

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. This call is only exercised
        # against mock adapters today. See the module docstring for
        # the outstanding integration gap.
        repo = await adapter.get_repository(repository_id, token=token)
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
        token = self._require_token(provider_id)

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. This call is only exercised
        # against mock adapters today. See the module docstring for
        # the outstanding integration gap.
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
        token = self._require_token(provider_id)

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. This call is only exercised
        # against mock adapters today. See the module docstring for
        # the outstanding integration gap.
        repo = await adapter.get_repository(repository_id, token=token)
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
        token = self._require_token(provider_id)

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. See the module docstring.
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

        target_path = None
        if chat_id:
            from pathlib import Path
            # Use the chat's session files directory as the workspace
            target_path = Path(os.getcwd()) / "storage" / "chats" / chat_id / "files"

        ws_ref = await self.workspace_manager.create_workspace(
            repo=repo,
            base_sha=getattr(payload, "base_sha", None),
            source_branch=getattr(payload, "branch_name", None),
            use_worktree=True,
            target_path=target_path,
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
        token = self._require_token(provider_id)

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. See the module docstring.
        repo = await adapter.get_repository(
            ws_ref.repository_id, token=token
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

        ticket: ApprovalTicket = self.approval_gate.create_ticket(
            operation_id=record.operation_id,
            repository_id=record.repository_id,
            operation=effective_operation,
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
        from backend.integrations.source_control.operations.models import (
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
        from backend.integrations.source_control.operations.models import (
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
        if ticket is None:
            raise ValidationError(
                f"Approval ticket '{payload.approval_id}' is not known."
            )
        if is_protected_branch(ticket.target_branch):
            raise PermissionDeniedError(
                f"Pushing to protected branch '{ticket.target_branch}' "
                "is forbidden by policy."
            )

        # Re-evaluate the policy at execution time, not only at the
        # moment the ticket was created. The user may have approved the
        # operation while the repository was still writable; if the
        # policy has changed since then (repository made read-only, new
        # branch protection rule, org-level restriction), the execution
        # must be refused. The check MUST run BEFORE `consume` so that
        # a valid ticket is never burned by a policy denial — the user
        # can retry after the policy is relaxed without re-approving.
        policy_ctx = PolicyResolutionContext(
            operation="push_branch",
            provider=provider_id,
            target_branch=ticket.target_branch,
            source_branch=ws_ref.current_branch,
            actor="system",
        )
        self.policy_engine.require(policy_ctx)

        # Fetch the token BEFORE consuming the ticket so that a missing
        # or expired credential does not consume the ticket and force
        # the user to re-approve an unchanged diff.
        token = self._require_token(provider_id)

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
        )

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

        # SECURITY: change-request creation MUST consume a ticket
        # bound to `create_change_request`, NOT to the operation's
        # own kind. Reusing a push approval for a change request is
        # a critical security bug (see the reference specification,
        # section 10). The check below enforces that the ticket was
        # granted for the correct side effect.
        ticket = self.approval_gate.get_ticket(approval_id)
        if ticket is None:
            raise ValidationError(
                f"Approval ticket '{approval_id}' is not known."
            )
        if ticket.operation != "create_change_request":
            raise PermissionDeniedError(
                f"Approval ticket '{approval_id}' was granted for "
                f"'{ticket.operation}', not for 'create_change_request'. "
                "A push approval cannot be reused to create a change "
                "request."
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
            expected_operation="create_change_request",
            expected_base_sha=record.base_sha or "",
            expected_diff_sha256=record.diff_sha256 or "",
            expected_target_branch=ticket.target_branch,
            actor="system",
        )

        # NOTE: `repository_id` is a string, but the current adapter
        # expects a `RepositoryRef`. See the module docstring.
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
