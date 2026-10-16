from __future__ import annotations

# Capabilities & Capabilities Matrix
from .capabilities import ProviderCapabilities

# Normalized Errors
from .errors import (
    AuthExpiredError,
    AuthRequiredError,
    ConflictError,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    RateLimitedError,
    RemoteStateChangedError,
    SourceControlError,
    UnsupportedCapabilityError,
    ValidationError,
)

# Domain Enums, Models & Policies
from .domain.enums import (
    FileChangeType,
    ProviderId,
    SourceControlErrorCode,
)
from .domain.models import (
    BranchRef,
    ChangeRequestRef,
    CommitRef,
    DiffSummary,
    FileDiff,
    ProviderAccount,
    RepositoryRef,
)
from .domain.policies import (
    is_protected_branch,
    is_workflow_path,
)

# Abstract Ports
from .ports.auth import AuthPort
from .ports.change_request import ChangeRequestPort
from .ports.checks import ChecksPort
from .ports.provider import SourceControlProvider
from .ports.repository import RepositoryPort

# Provider Registry & Built-ins
from .registry import (
    ProviderRegistry,
    get_default_registry,
)
from .providers import (
    GitHubProviderAdapter,
    register_builtin_providers,
)

# Security & Governance
from .security.approval import (
    ApprovalGate,
    ApprovalTicket,
    compute_diff_sha256,
    get_default_approval_gate,
)
from .security.audit import (
    AuditLogger,
    get_default_audit_logger,
)
from .security.policy_engine import (
    PolicyEngine,
    get_default_policy_engine,
)
from .security.redaction import (
    redact_data,
    redact_text,
    redact_url,
)
from .security.token_service import (
    TokenService,
    get_default_token_service,
)
from .security.vault import (
    CredentialVault,
    get_default_vault,
)

# Operations & Recovery Engine
from .operations.journal import (
    OperationJournal,
    get_default_operation_journal,
)
from .operations.models import (
    OperationEvent,
    OperationRecord,
    OperationState,
)
from .operations.service import (
    OperationService,
    get_default_operation_service,
)

# Isolated Local Git Engine
from .git.diff import GitDiffEngine
from .git.executable import GitExecutable
from .git.path_policy import secure_resolve_path, validate_branch_name
from .git.push import GitPushManager, PushResult
from .git.workspace import GitWorkspaceManager

# Agent Tools Suite
from .tools import (
    CreateBranchArgs,
    CreateBranchTool,
    CreateChangeRequestArgs,
    CreateChangeRequestTool,
    InspectDiffArgs,
    InspectDiffTool,
    ListRepositoriesArgs,
    ListRepositoriesTool,
    PushBranchArgs,
    PushBranchTool,
    ReadFileArgs,
    ReadFileTool,
    RequestApprovalArgs,
    RequestApprovalTool,
    RunChecksArgs,
    RunChecksTool,
    get_all_source_control_tools,
    register_source_control_tools,
)

# API Schemas (Requests & Responses)
from .schemas import (
    ActionSuccessResponse,
    ApprovalTicketResponse,
    ApproveOperationRequest,
    AuditLogEntryResponse,
    BaseSCRequest,
    BaseSCResponse,
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

__all__ = [
    # Capabilities
    "ProviderCapabilities",
    # Error Hierarchy
    "SourceControlError",
    "AuthRequiredError",
    "AuthExpiredError",
    "PermissionDeniedError",
    "NotFoundError",
    "RateLimitedError",
    "NetworkError",
    "ConflictError",
    "ValidationError",
    "UnsupportedCapabilityError",
    "RemoteStateChangedError",
    "ProviderError",
    # Domain Enums & Policies
    "ProviderId",
    "FileChangeType",
    "SourceControlErrorCode",
    "is_protected_branch",
    "is_workflow_path",
    # Domain Entities
    "ProviderAccount",
    "RepositoryRef",
    "BranchRef",
    "CommitRef",
    "ChangeRequestRef",
    "DiffSummary",
    "FileDiff",
    # Abstract Ports
    "SourceControlProvider",
    "AuthPort",
    "RepositoryPort",
    "ChangeRequestPort",
    "ChecksPort",
    # Registry & Adapters
    "ProviderRegistry",
    "get_default_registry",
    "GitHubProviderAdapter",
    "register_builtin_providers",
    # Security, Vault & Governance
    "CredentialVault",
    "get_default_vault",
    "TokenService",
    "get_default_token_service",
    "ApprovalGate",
    "ApprovalTicket",
    "compute_diff_sha256",
    "get_default_approval_gate",
    "PolicyEngine",
    "get_default_policy_engine",
    "AuditLogger",
    "get_default_audit_logger",
    "redact_text",
    "redact_url",
    "redact_data",
    # Operations State Machine & Recovery
    "OperationState",
    "OperationRecord",
    "OperationEvent",
    "OperationJournal",
    "get_default_operation_journal",
    "OperationService",
    "get_default_operation_service",
    # Git Engine
    "GitExecutable",
    "GitWorkspaceManager",
    "GitDiffEngine",
    "GitPushManager",
    "PushResult",
    "validate_branch_name",
    "secure_resolve_path",
    # Agent Tools
    "ListRepositoriesTool",
    "ReadFileTool",
    "CreateBranchTool",
    "InspectDiffTool",
    "RunChecksTool",
    "RequestApprovalTool",
    "PushBranchTool",
    "CreateChangeRequestTool",
    "get_all_source_control_tools",
    "register_source_control_tools",
    "ListRepositoriesArgs",
    "ReadFileArgs",
    "CreateBranchArgs",
    "InspectDiffArgs",
    "RunChecksArgs",
    "RequestApprovalArgs",
    "PushBranchArgs",
    "CreateChangeRequestArgs",
    # Schemas - Requests
    "BaseSCRequest",
    "ConnectBeginRequest",
    "ValidateConnectionRequest",
    "DisconnectRequest",
    "CreateWorkspaceRequest",
    "RunChecksRequest",
    "CreateBranchRequest",
    "CreateOperationRequest",
    "ApproveOperationRequest",
    "RejectOperationRequest",
    "PushBranchRequest",
    "CreateChangeRequestPayload",
    # Schemas - Responses
    "BaseSCResponse",
    "ActionSuccessResponse",
    "ProviderCapabilitiesResponse",
    "ProviderDescriptorResponse",
    "ConnectBeginResponse",
    "ConnectionStatusResponse",
    "RepositoryResponse",
    "BranchResponse",
    "FileReadResponse",
    "FileDiffResponse",
    "DiffSummaryResponse",
    "CheckRunResponse",
    "CheckSummaryResponse",
    "ApprovalTicketResponse",
    "WorkspaceResponse",
    "PushBranchResponse",
    "ChangeRequestResponse",
    "OperationEventResponse",
    "OperationResponse",
    "AuditLogEntryResponse",
]
