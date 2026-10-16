from __future__ import annotations

from .approval import (
    ApprovalGate,
    ApprovalManager,
    ApprovalTicket,
    compute_diff_sha256,
    get_default_approval_gate,
    get_default_approval_manager,
    reset_default_approval_gate,
    reset_default_approval_manager,
    set_default_approval_gate,
    set_default_approval_manager,
)
from .audit import (
    AuditLogger,
    AuditSink,
    FileAppendOnlyAuditSink,
    InMemoryAuditSink,
    SecurityAuditEvent,
    SecurityAuditLogger,
    get_default_audit_logger,
    reset_default_audit_logger,
    set_default_audit_logger,
)
from .oauth_flow import (
    OAuthSessionManager,
    compute_pkce_challenge,
    generate_pkce_verifier,
    generate_state_token,
    get_default_oauth_session_manager,
    reset_default_oauth_session_manager,
    set_default_oauth_session_manager,
)
from .policy_engine import (
    PolicyEngine,
    get_default_policy_engine,
    reset_default_policy_engine,
    set_default_policy_engine,
)
from .redaction import (
    SENSITIVE_KEYS,
    redact_data,
    redact_text,
    redact_url,
)
from .token_service import (
    TokenService,
    get_default_token_service,
    reset_default_token_service,
    set_default_token_service,
)
from .vault import (
    CredentialVault,
    InMemoryCredentialVault,
    KeyringCredentialVault,
    VaultError,
    WindowsNativeCredentialVault,
    get_credential_vault,
    get_default_vault,
    set_credential_vault,
    set_default_vault,
)

__all__ = [
    # Credential Vault
    "CredentialVault",
    "InMemoryCredentialVault",
    "KeyringCredentialVault",
    "WindowsNativeCredentialVault",
    "VaultError",
    "get_credential_vault",
    "set_credential_vault",
    "get_default_vault",
    "set_default_vault",
    # Redaction Utilities
    "SENSITIVE_KEYS",
    "redact_url",
    "redact_text",
    "redact_data",
    # Approval Gate
    "compute_diff_sha256",
    "ApprovalTicket",
    "ApprovalGate",
    "ApprovalManager",
    "get_default_approval_gate",
    "get_default_approval_manager",
    "set_default_approval_gate",
    "set_default_approval_manager",
    "reset_default_approval_gate",
    "reset_default_approval_manager",
    # Policy Engine
    "PolicyEngine",
    "get_default_policy_engine",
    "set_default_policy_engine",
    "reset_default_policy_engine",
    # Token Lifecycle Management
    "TokenService",
    "get_default_token_service",
    "set_default_token_service",
    "reset_default_token_service",
    # OAuth and PKCE Flow
    "generate_pkce_verifier",
    "compute_pkce_challenge",
    "generate_state_token",
    "OAuthSessionManager",
    "get_default_oauth_session_manager",
    "set_default_oauth_session_manager",
    "reset_default_oauth_session_manager",
    # Security Audit Logging
    "SecurityAuditEvent",
    "AuditSink",
    "InMemoryAuditSink",
    "FileAppendOnlyAuditSink",
    "SecurityAuditLogger",
    "AuditLogger",
    "get_default_audit_logger",
    "set_default_audit_logger",
    "reset_default_audit_logger",
]
