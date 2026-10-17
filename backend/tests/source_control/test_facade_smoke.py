# backend/tests/source_control/test_facade_smoke.py
"""
Regression tests for the Source Control facade and its integration
with the FastAPI application.

This module serves two purposes:

    1. Smoke tests:
       Guarantee that the facade can be instantiated, that the provider
       registry is populated, and that the router is actually reachable
       through the FastAPI application. These fail loudly if a future
       refactor breaks one of the wiring invariants that the earlier
       integration audit flagged.

    2. Contract tests:
       Assert that the external service interfaces the facade depends on
       still expose the methods and fields the facade uses. When any of
       these changes, the corresponding assertion fails immediately,
       pointing to the exact contract that drifted.

Design rules:
    - Every test asserts on observable behavior. No test is a mere
      print statement.
    - Route discovery is defensive: not every entry in `app.routes`
      is an `APIRoute`; some are internal FastAPI containers. We only
      inspect attributes that are guaranteed to exist or fall back to
      `app.openapi()` for path enumeration.
    - Imports for `PushResult` and other port-level models use their
      canonical location under `ports/`, not `domain/`.
"""

from __future__ import annotations

import inspect

import pytest

# --- Production imports (canonical paths) ----------------------------

from backend.integrations.source_control.git.diff import GitDiffEngine
from backend.integrations.source_control.git.push import GitPushManager
from backend.integrations.source_control.git.workspace import GitWorkspaceManager
from backend.integrations.source_control.operations.service import OperationService
from backend.integrations.source_control.ports.repository import (
    PushResult,
)
from backend.integrations.source_control.registry import ProviderRegistry
from backend.integrations.source_control.security.approval import ApprovalGate
from backend.integrations.source_control.security.audit import AuditLogger
from backend.integrations.source_control.security.oauth_flow import (
    OAuthFlowCoordinator,
)
from backend.integrations.source_control.security.policy_engine import (
    PolicyEngine,
)
from backend.integrations.source_control.security.token_service import (
    TokenService,
)
from backend.integrations.source_control.domain.models import (
    CheckSummary,
    DiffSummary,
    WorkspaceRef,
)
from backend.omweb.services.source_control_facade import SourceControlFacade


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _iter_route_paths(app) -> list[str]:
    """
    Return every path registered on `app`, regardless of the internal
    router representation.

    `app.routes` is a mixed list. Some entries are `APIRoute`, some are
    `Mount`, some are internal containers that only appear while the
    router is being included. We therefore use `getattr(..., 'path',
    None)` and fall back to the OpenAPI schema, which is the most stable
    public representation of the routing tree.
    """
    paths: set[str] = set()

    for route in app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            paths.add(path)

    try:
        schema = app.openapi()
    except Exception:  # noqa: BLE001
        schema = {}
    for path in (schema.get("paths") or {}).keys():
        if isinstance(path, str):
            paths.add(path)

    return sorted(paths)


# ---------------------------------------------------------------------
# Smoke tests: wiring invariants
# ---------------------------------------------------------------------


def test_facade_can_be_instantiated() -> None:
    """
    The facade MUST be constructible with no arguments.

    This is the single most important wiring check: if it fails, no
    other integration path works.
    """
    facade = SourceControlFacade()
    assert facade is not None
    assert facade.registry is not None
    assert facade.token_service is not None
    assert facade.workspace_manager is not None
    assert facade.policy_engine is not None
    assert facade.approval_gate is not None
    assert facade.operation_service is not None
    assert facade.diff_engine is not None
    assert facade.push_manager is not None


@pytest.mark.asyncio
async def test_facade_list_providers() -> None:
    """`list_providers` MUST enumerate the built-in GitHub provider."""
    facade = SourceControlFacade()
    providers = await facade.list_providers()
    provider_ids = {p.provider_id for p in providers}
    assert "github" in provider_ids, (
        f"GitHub not listed by facade.list_providers(); got {provider_ids!r}"
    )


def test_router_is_registered_in_application() -> None:
    """
    The source-control router MUST be reachable through the FastAPI
    application.

    We assert on the OpenAPI schema because it is stable across FastAPI
    versions and does not depend on the internal `_IncludedRouter`
    representation.
    """
    from backend.omweb.main import app

    paths = _iter_route_paths(app)
    assert any(
        path.startswith("/api/source-control") for path in paths
    ), f"Source-control router is not registered. Paths: {paths!r}"

    # Every publicly documented endpoint should appear in the schema.
    expected_paths = {
        "/api/source-control/providers",
        "/api/source-control/connections",
        "/api/source-control/repositories",
        "/api/source-control/operations",
    }
    missing = expected_paths - set(paths)
    assert not missing, (
        f"Expected endpoints are missing from the OpenAPI schema: "
        f"{sorted(missing)!r}"
    )


# ---------------------------------------------------------------------
# Contract tests: external interfaces the facade relies on
# ---------------------------------------------------------------------


def test_provider_registry_contract() -> None:
    """ProviderRegistry MUST expose the methods the facade uses."""
    facade = SourceControlFacade()
    registry = facade.registry
    assert isinstance(registry, ProviderRegistry)

    for method_name in ("list_provider_ids", "get", "has", "register"):
        assert hasattr(registry, method_name), (
            f"ProviderRegistry is missing required method: {method_name}"
        )


def test_token_service_contract() -> None:
    """TokenService MUST match the synchronous accessor contract."""
    sig = inspect.signature(TokenService.get_access_token)
    params = list(sig.parameters)
    # self, provider, account_id, base_url
    assert params == ["self", "provider", "account_id", "base_url"], (
        f"TokenService.get_access_token signature changed: {params!r}"
    )
    # It MUST be synchronous (the facade calls it without `await`).
    assert not inspect.iscoroutinefunction(TokenService.get_access_token), (
        "TokenService.get_access_token is async; the facade calls it "
        "synchronously."
    )

    for method_name in ("store_token", "revoke", "refresh"):
        assert hasattr(TokenService, method_name), (
            f"TokenService is missing required method: {method_name}"
        )


def test_policy_engine_contract() -> None:
    """PolicyEngine MUST expose `check` and `require` (not `is_operation_allowed`)."""
    for method_name in ("check", "require", "resolve_policy"):
        assert hasattr(PolicyEngine, method_name), (
            f"PolicyEngine is missing required method: {method_name}"
        )
    assert not hasattr(PolicyEngine, "is_operation_allowed"), (
        "PolicyEngine still defines the legacy `is_operation_allowed` "
        "method; the facade uses `check`."
    )


def test_audit_logger_contract() -> None:
    """AuditLogger MUST expose `record` and `query` (not `log_event`)."""
    for method_name in ("record", "query"):
        assert hasattr(AuditLogger, method_name), (
            f"AuditLogger is missing required method: {method_name}"
        )


def test_operation_service_contract() -> None:
    """OperationService.transition MUST accept `to_status` keyword."""
    sig = inspect.signature(OperationService.transition)
    params = list(sig.parameters)
    assert "to_status" in params, (
        f"OperationService.transition no longer accepts `to_status`; "
        f"got parameters {params!r}"
    )
    assert "target_state" not in params, (
        "OperationService.transition still uses the legacy `target_state` "
        "parameter; the facade uses `to_status`."
    )

    for method_name in (
        "create_operation",
        "get_operation",
        "list_events",
        "attach_diff",
        "attach_approval",
        "attach_change_request",
    ):
        assert hasattr(OperationService, method_name), (
            f"OperationService is missing required method: {method_name}"
        )


def test_workspace_manager_contract() -> None:
    """GitWorkspaceManager MUST accept `path_policy`, not `base_workspace_dir`."""
    sig = inspect.signature(GitWorkspaceManager.__init__)
    params = set(sig.parameters)
    assert "path_policy" in params, (
        "GitWorkspaceManager.__init__ no longer accepts `path_policy`."
    )
    assert "base_workspace_dir" not in params, (
        "GitWorkspaceManager.__init__ unexpectedly re-added "
        "`base_workspace_dir`."
    )


def test_workspace_ref_fields() -> None:
    """WorkspaceRef MUST expose `is_clean` (facade uses `not is_clean`)."""
    fields = set(WorkspaceRef.__dataclass_fields__)
    assert "is_clean" in fields, (
        f"WorkspaceRef lost `is_clean`; got {sorted(fields)!r}"
    )
    assert "is_dirty" not in fields, (
        "WorkspaceRef unexpectedly added `is_dirty`; the facade uses "
        "`not is_clean`."
    )


def test_check_summary_fields() -> None:
    """CheckSummary MUST expose counters that match the facade mapper."""
    fields = set(CheckSummary.__dataclass_fields__)
    expected = {"total", "succeeded", "failed", "pending", "skipped", "runs"}
    missing = expected - fields
    assert not missing, (
        f"CheckSummary is missing fields the facade relies on: "
        f"{sorted(missing)!r}"
    )


def test_diff_summary_contract() -> None:
    """DiffSummary MUST expose `sha256()` and the fields the facade reads."""
    fields = set(DiffSummary.__dataclass_fields__)
    expected = {"files", "base_sha", "head_sha", "total_additions", "total_deletions"}
    missing = expected - fields
    assert not missing, (
        f"DiffSummary is missing fields the facade relies on: "
        f"{sorted(missing)!r}"
    )
    assert hasattr(DiffSummary, "sha256"), (
        "DiffSummary.sha256() is missing; the facade uses it to bind "
        "the approval ticket."
    )

    # Confirm the diff engine signature that the facade calls.
    diff_sig = inspect.signature(GitDiffEngine.compute_diff)
    diff_params = set(diff_sig.parameters)
    for required in ("workspace_id", "base_sha", "workspace_path"):
        assert required in diff_params, (
            f"GitDiffEngine.compute_diff is missing parameter "
            f"`{required}`; got {sorted(diff_params)!r}"
        )


def test_push_manager_contract() -> None:
    """GitPushManager.push_branch MUST accept the workspace-oriented args."""
    sig = inspect.signature(GitPushManager.push_branch)
    params = set(sig.parameters)
    required = {
        "workspace_id",
        "local_branch",
        "remote",
        "remote_branch",
        "workspace_path",
        "token",
    }
    missing = required - params
    assert not missing, (
        f"GitPushManager.push_branch is missing parameters the facade "
        f"relies on: {sorted(missing)!r}"
    )

    push_result_fields = set(PushResult.__dataclass_fields__)
    expected_fields = {
        "accepted",
        "local_branch",
        "local_sha",
        "remote_branch",
        "remote_sha",
        "rejected_reason",
        "message",
    }
    missing_fields = expected_fields - push_result_fields
    assert not missing_fields, (
        f"PushResult is missing fields the facade reads: "
        f"{sorted(missing_fields)!r}"
    )


def test_approval_gate_contract() -> None:
    """ApprovalGate MUST expose `consume` (canonical) plus grant/reject."""
    for method_name in ("consume", "grant", "reject", "create_ticket", "get_ticket"):
        assert hasattr(ApprovalGate, method_name), (
            f"ApprovalGate is missing required method: {method_name}"
        )

    consume_sig = inspect.signature(ApprovalGate.consume)
    params = set(consume_sig.parameters)
    required = {
        "approval_id",
        "expected_operation_id",
        "expected_repository_id",
        "expected_operation",
        "expected_base_sha",
        "expected_diff_sha256",
        "expected_target_branch",
    }
    missing = required - params
    assert not missing, (
        f"ApprovalGate.consume is missing expected-field parameters: "
        f"{sorted(missing)!r}"
    )


def test_oauth_coordinator_contract() -> None:
    """OAuthFlowCoordinator MUST expose begin/complete/sessions."""
    for method_name in (
        "begin_authorization",
        "complete_authorization",
        "register_provider",
    ):
        assert hasattr(OAuthFlowCoordinator, method_name), (
            f"OAuthFlowCoordinator is missing required method: {method_name}"
        )

    # `sessions` is a property exposing the pending-session manager.
    assert hasattr(OAuthFlowCoordinator, "sessions"), (
        "OAuthFlowCoordinator is missing the `sessions` property."
    )
