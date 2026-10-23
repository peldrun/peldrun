# test_fallback_import.py
"""
Verifies that the facade loads successfully via the fallback
`except ImportError` path.

The test removes `backend/` from sys.path so that the try block's
`from integrations.source_control import ...` fails. It then imports
the facade, which should run the except block and load all its
symbols from `backend.integrations.source_control.*`.

Run from the project root:
    python test_fallback_import.py
"""
import sys


# --- Force the try block to fail ------------------------------------------
# Remove any sys.path entry that points at the `backend/` directory.
# Once it is gone, `from integrations.source_control import ...` (the
# facade's try block) cannot resolve and the except block runs.
sys.path[:] = [
    p for p in sys.path
    if "backend" not in str(p).replace("\\", "/").split("/")
]


# --- Sanity: verify the try path would fail -------------------------------
try:
    from integrations.source_control import ApprovalGate  # type: ignore  # noqa: F401
    print(
        "ERROR: 'integrations.source_control' is still importable; "
        "the fallback path cannot be exercised. Check PYTHONPATH.",
        file=sys.stderr,
    )
    sys.exit(3)
except ImportError:
    pass  # Expected. This is the condition that triggers the fallback.


# --- Import the facade; its except block runs -----------------------------
import backend.omweb.services.source_control_facade as facade_module


# --- Verify the except block imported all its symbols ---------------------
required_symbols = [
    # Classes from the main `from ... import (...)` block
    "SourceControlFacade",
    "ApprovalGate",
    "ApprovalTicket",
    "AuditLogger",
    "BranchRef",
    "ChangeRequestRef",
    "CheckSummaryRef",
    "CredentialVault",
    "DiffSummary",
    "FileDiff",
    "GitDiffEngine",
    "GitPushManager",
    "GitWorkspaceManager",
    "OperationRecord",
    "OperationService",
    "OperationStatus",
    "PolicyEngine",
    "ProviderAccount",
    "ProviderCapabilities",
    "ProviderId",
    "ProviderRegistry",
    "PushResult",
    "RepositoryRef",
    "SourceControlError",
    "TokenService",
    # Helper functions
    "get_default_approval_gate",
    "get_default_audit_logger",
    "get_default_policy_engine",
    "get_default_registry",
    "get_default_token_service",
    "get_default_vault",
    "is_protected_branch",
    "is_workflow_path",
    "redact_text",
    "redact_url",
    "register_builtin_providers",
    "validate_branch_name",
    # Errors
    "AuthRequiredError",
    "NotFoundError",
    "PermissionDeniedError",
    "RemoteStateChangedError",
    "UnsupportedCapabilityError",
    "ValidationError",
    # Audit / OAuth / Policy
    "AuditEventType",
    "AuditSeverity",
    "OAuthFlowCoordinator",
    "OAuthProviderCallbacks",
    "PolicyResolutionContext",
    # Schemas from the main block, INCLUDING the previously
    # missing `SaveProviderConfigRequest`
    "ActionSuccessResponse",
    "ApprovalTicketResponse",
    "ApproveOperationRequest",
    "AuditLogEntryResponse",
    "BranchResponse",
    "ChangeRequestResponse",
    "CheckRunResponse",
    "CheckSummaryResponse",
    "ConnectBeginRequest",
    "ConnectBeginResponse",
    "ConnectionStatusResponse",
    "CreateBranchRequest",
    "CreateChangeRequestPayload",
    "CreateOperationRequest",
    "CreateWorkspaceRequest",
    "DiffSummaryResponse",
    "DisconnectRequest",
    "FileDiffResponse",
    "FileReadResponse",
    "OperationEventResponse",
    "OperationResponse",
    "ProviderCapabilitiesResponse",
    "ProviderDescriptorResponse",
    "PushBranchRequest",
    "PushBranchResponse",
    "RejectOperationRequest",
    "RepositoryResponse",
    "RunChecksRequest",
    "SaveProviderConfigRequest",
    "ValidateConnectionRequest",
    "WorkspaceResponse",
]

missing = [n for n in required_symbols if not hasattr(facade_module, n)]

print("=" * 60)
print("Fallback Import Test")
print("=" * 60)

if missing:
    print(f"[FAIL] {len(missing)} symbol(s) missing from the facade module:")
    for name in missing:
        print(f"       - {name}")
    sys.exit(1)

print(f"[PASS] Facade loaded via the 'except ImportError' fallback")
print(f"[PASS] All {len(required_symbols)} required symbols are present,")
print(f"       including 'SaveProviderConfigRequest' (previously missing).")
print("=" * 60)
print("ALL TESTS PASSED")
