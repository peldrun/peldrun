"""
Public surface of the ``security`` package.

This module re-exports every security-relevant class, function,
exception, and singleton exposed by the sub-modules. Callers SHOULD
import from this package (``from backend.integrations.source_control
.security import ...``) rather than from the individual sub-modules,
so that refactoring a sub-module's location does not break callers.

Design rules:
    - Every name listed in ``__all__`` MUST be importable from this
      package. Importing a name that exists only in a sub-module is a
      layering smell: it means the caller depends on the internal
      layout of the package.
    - Adding a new public symbol to a sub-module requires adding it
      to the import block and to ``__all__`` below.
    - Backward-compatibility aliases (``SecurityAuditLogger``,
      ``AuditSink``, ``InMemoryCredentialVault``,
      ``KeyringCredentialVault``, ``ApprovalManager``) are re-exported
      here so that callers written against older names continue to
      work.
"""

from __future__ import annotations

# --- Approval gate --------------------------------------------------------
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

# --- Audit logging --------------------------------------------------------
from .audit import (
    AuditEvent,
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    AuditSink,
    FileAppendOnlyAuditSink,
    InMemoryAuditSink,
    SecurityAuditEvent,
    SecurityAuditLogger,
    get_default_audit_logger,
    reset_default_audit_logger,
    set_default_audit_logger,
)

# --- OAuth flow and PKCE --------------------------------------------------
from .oauth_flow import (
    OAuthFlowCoordinator,
    OAuthSessionManager,
    compute_pkce_challenge,
    generate_pkce_verifier,
    generate_state_token,
    get_default_oauth_flow_coordinator,
    get_default_oauth_session_manager,
    reset_default_oauth_flow_coordinator,
    reset_default_oauth_session_manager,
    set_default_oauth_flow_coordinator,
    set_default_oauth_session_manager,
)

# --- Policy engine --------------------------------------------------------
from .policy_engine import (
    PolicyCheckResult,
    PolicyEngine,
    PolicyResolutionContext,
    get_default_policy_engine,
    reset_default_policy_engine,
    set_default_policy_engine,
)

# --- Redaction ------------------------------------------------------------
from .redaction import (
    MAX_REDACTION_INPUT,
    REDACTED,
    SENSITIVE_KEYS,
    SafeStr,
    contains_secret,
    is_likely_secret,
    redact_bytes,
    redact_data,
    redact_headers,
    redact_mapping,
    redact_sequence,
    redact_text,
    redact_url,
)

# --- Token lifecycle ------------------------------------------------------
from .token_service import (
    RefreshCallback,
    TokenRefreshResult,
    TokenService,
    get_default_token_service,
    reset_default_token_service,
    set_default_token_service,
)

# --- Credential vault -----------------------------------------------------
from .vault import (
    CredentialVault,
    FallbackVault,
    InMemoryCredentialVault,
    KeyringCredentialVault,
    VaultError,
    VaultHealth,
    WindowsNativeCredentialVault,
    get_credential_vault,
    get_default_vault,
    set_credential_vault,
    set_default_vault,
)

__all__ = [
    # --- Approval gate ------------------------------------------------
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
    # --- Audit logging ------------------------------------------------
    "AuditEvent",
    "AuditEventType",
    "AuditSeverity",
    "AuditLogger",
    "AuditSink",
    "InMemoryAuditSink",
    "FileAppendOnlyAuditSink",
    "SecurityAuditEvent",
    "SecurityAuditLogger",
    "get_default_audit_logger",
    "set_default_audit_logger",
    "reset_default_audit_logger",
    # --- OAuth flow and PKCE -----------------------------------------
    "OAuthFlowCoordinator",
    "OAuthSessionManager",
    "generate_pkce_verifier",
    "compute_pkce_challenge",
    "generate_state_token",
    "get_default_oauth_flow_coordinator",
    "set_default_oauth_flow_coordinator",
    "reset_default_oauth_flow_coordinator",
    "get_default_oauth_session_manager",
    "set_default_oauth_session_manager",
    "reset_default_oauth_session_manager",
    # --- Policy engine ------------------------------------------------
    "PolicyEngine",
    "PolicyCheckResult",
    "PolicyResolutionContext",
    "get_default_policy_engine",
    "set_default_policy_engine",
    "reset_default_policy_engine",
    # --- Redaction ----------------------------------------------------
    "REDACTED",
    "MAX_REDACTION_INPUT",
    "SENSITIVE_KEYS",
    "SafeStr",
    "redact_text",
    "redact_bytes",
    "redact_mapping",
    "redact_sequence",
    "redact_data",
    "redact_url",
    "redact_headers",
    "contains_secret",
    "is_likely_secret",
    # --- Token lifecycle ----------------------------------------------
    "TokenService",
    "TokenRefreshResult",
    "RefreshCallback",
    "get_default_token_service",
    "set_default_token_service",
    "reset_default_token_service",
    # --- Credential vault ---------------------------------------------
    "CredentialVault",
    "VaultError",
    "VaultHealth",
    "InMemoryCredentialVault",
    "KeyringCredentialVault",
    "WindowsNativeCredentialVault",
    "FallbackVault",
    "get_credential_vault",
    "set_credential_vault",
    "get_default_vault",
    "set_default_vault",
]
