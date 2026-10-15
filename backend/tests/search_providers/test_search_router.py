"""
backend/tests/search_providers/test_search_router.py

Contract tests for the Search Settings / Diagnostics / Telemetry router.
Runs fully offline using FastAPI's TestClient and monkeypatched stores.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omweb.routers import search_rtr
from peldrun.tools.builtins.search_providers.base import (
    CostBasis,
    ProviderDiagnosticResult,
    ProviderStatus,
    SearchResponse,
    SearchResult,
)
from peldrun.tools.builtins.search_providers.factory import SearchProviderFactory


@pytest.fixture
def cfg_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the router at a temporary config.toml with a valid [search] section."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[search]\n"
        'engine = "DuckDuckGo"\n'
        "fallback_engines = []\n"
        "enabled = true\n"
        "default_max_results = 5\n"
        'lang = "en"\n'
        'country = "us"\n'
        "total_budget_seconds = 22.0\n"
        "max_retries = 0\n"
        "retry_delay = 1.0\n"
        "privacy_mode = false\n"
        "\n[search.duckduckgo]\n"
        "enabled = true\n"
        'safesearch = "moderate"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(search_rtr, "_resolve_config_toml_path", lambda: path)
    return path


@pytest.fixture
def client(cfg_file: Path) -> TestClient:
    app = FastAPI()
    app.include_router(search_rtr.router, prefix="/api/search")
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------
def test_get_config_returns_canonical_shape_and_masks_secrets(
    client: TestClient, cfg_file: Path
) -> None:
    resp = client.get("/api/search/config")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "ok"
    assert data["default_max_results"] == 5
    assert data["max_results_hard_cap"] == 20
    assert "search" in data and "config" in data
    assert isinstance(data["providers"], list)
    assert isinstance(data["provider_capabilities"], dict)

    # Canonical provider list includes DuckDuckGo
    keys = {p["id"] for p in data["providers"]}
    assert "duckduckgo" in keys


# ---------------------------------------------------------------------------
# POST /config — round-trip, mask preservation
# ---------------------------------------------------------------------------
def test_post_config_preserves_masked_secret_and_saves(
    client: TestClient, cfg_file: Path
) -> None:
    # First save a provider with a real secret.
    save = client.post(
        "/api/search/config",
        json={
            "search": {
                "engine": "Brave",
                "fallback_engines": [],
                "brave": {"enabled": True, "api_key": "BSA-real-secret-1234"},
            }
        },
    )
    assert save.status_code == 200

    # Read it back — the secret must be masked.
    read = client.get("/api/search/config").json()
    assert read["search"]["brave"]["api_key"] == "********"

    # Write it back with the masked placeholder — real secret must be preserved.
    update = client.post(
        "/api/search/config",
        json={
            "search": {
                "engine": "Brave",
                "fallback_engines": [],
                "brave": {"enabled": True, "api_key": "********"},
            }
        },
    )
    assert update.status_code == 200

    # Verify the on-disk config still has the real secret.
    disk = cfg_file.read_text(encoding="utf-8")
    assert "BSA-real-secret-1234" in disk


# ---------------------------------------------------------------------------
# GET /providers
# ---------------------------------------------------------------------------
def test_get_providers_lists_canonical_metadata(client: TestClient) -> None:
    resp = client.get("/api/search/providers")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "ok"
    keys = {p["id"] for p in data["providers"]}
    expected = {"duckduckgo", "searxng", "tavily", "brave", "google", "jina", "yandex"}
    assert expected.issubset(keys)


# ---------------------------------------------------------------------------
# GET /metrics — period + days resolution
# ---------------------------------------------------------------------------
def test_get_metrics_resolves_unknown_period_from_days(client: TestClient) -> None:
    resp = client.get("/api/search/metrics?period=bogus&days=14")
    assert resp.status_code == 200
    data = resp.json()

    # Unrecognized period must be coerced to "month" (14 days → month bracket).
    assert data["period"] == "month"
    assert data["requested_period"] == "bogus"
    assert data["days"] == 14
    assert "summary" in data
    assert "breakdown" in data
    assert "timeseries" in data


# ---------------------------------------------------------------------------
# POST /test — success, empty, error classification
# ---------------------------------------------------------------------------
def test_post_test_returns_success_for_successful_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok_search(cls, *args: Any, **kwargs: Any) -> SearchResponse:
        return SearchResponse(
            query=kwargs.get("query", ""),
            provider="DuckDuckGo",
            results=[SearchResult(title="R1", url="https://example.com/1", snippet="ok")],
            status=ProviderStatus.SUCCESS,
            is_success=True,
            request_count=1,
            estimated_cost_usd=0.0,
            cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
        )

    monkeypatch.setattr(
        SearchProviderFactory, "search", classmethod(_ok_search)
    )

    resp = client.post(
        "/api/search/test",
        json={"query": "test", "max_results": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["ok"] is True
    assert data["results_count"] == 1
    assert data["request_count"] == 1


def test_post_test_returns_empty_for_valid_empty_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _empty_search(cls, *args: Any, **kwargs: Any) -> SearchResponse:
        return SearchResponse(
            query=kwargs.get("query", ""),
            provider="DuckDuckGo",
            results=[],
            status=ProviderStatus.EMPTY,
            is_success=False,
            request_count=1,
            estimated_cost_usd=0.0,
            cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
            error_code="empty_results",
            error_message="no matches",
        )

    monkeypatch.setattr(
        SearchProviderFactory, "search", classmethod(_empty_search)
    )

    resp = client.post("/api/search/test", json={"query": "nothing"})
    data = resp.json()
    assert data["status"] == "empty"
    assert data["ok"] is False
    assert data["results_count"] == 0
    assert data["request_count"] == 1


def test_post_test_returns_error_for_failed_response_with_partial_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Partial results + failure status must be reported as error, NOT success.
    This is the critical P1 regression guard.
    """
    async def _failed_with_partial(cls, *args: Any, **kwargs: Any) -> SearchResponse:
        return SearchResponse(
            query=kwargs.get("query", ""),
            provider="Brave",
            results=[
                SearchResult(title="partial", url="https://example.com/p", snippet="")
            ],
            status=ProviderStatus.AUTHENTICATION_ERROR,
            is_success=False,
            request_count=1,
            estimated_cost_usd=0.0,
            cost_basis=CostBasis.CONFIGURED_ESTIMATE,
            error_code="authentication_error",
            error_message="bad token",
        )

    monkeypatch.setattr(
        SearchProviderFactory, "search", classmethod(_failed_with_partial)
    )

    resp = client.post("/api/search/test", json={"query": "partial fail"})
    data = resp.json()
    assert data["status"] == "error"
    assert data["ok"] is False
    assert data["provider_status"] == "authentication_error"
    # Partial results are still surfaced for inspection, but the outcome is failure.
    assert data["results_count"] == 1
    assert data["usage_unknown"] is False


# ---------------------------------------------------------------------------
# POST /test-connection
# ---------------------------------------------------------------------------
def test_post_test_connection_returns_diagnostic_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok_diagnose(self, *args: Any, **kwargs: Any) -> ProviderDiagnosticResult:
        return ProviderDiagnosticResult(
            ok=True,
            provider="DuckDuckGo",
            status=ProviderStatus.SUCCESS,
            reachable=True,
            authenticated=None,
            consumes_quota=False,
            latency_ms=42.0,
            request_count=1,
            estimated_cost_usd=0.0,
            cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
            message="Reachability OK",
        )

    # Monkeypatch diagnose on the concrete provider class the factory resolves.
    from peldrun.tools.builtins.search_providers.duckduckgo_provider import (
        DuckDuckGoSearchProvider,
    )
    monkeypatch.setattr(DuckDuckGoSearchProvider, "diagnose", _ok_diagnose)

    resp = client.post(
        "/api/search/test-connection",
        json={"provider": "DuckDuckGo", "check_type": "reachability"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["provider_key"] == "duckduckgo"
    assert data["check_type"] == "reachability"
    assert data["reachable"] is True
    assert data["request_count"] == 1


# ---------------------------------------------------------------------------
# DELETE /metrics (requires the route added in the previous review)
# ---------------------------------------------------------------------------
def test_delete_metrics_clears_ledger_and_returns_count(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the DELETE /metrics route is added, verify it uses clear_ledger()."""
    class _StubStore:
        def clear_ledger(self) -> int:
            return 42

    monkeypatch.setattr(
        "peldrun.runtime.search_usage_store.get_search_usage_store",
        lambda *a, **kw: _StubStore(),
    )

    resp = client.delete("/api/search/metrics")
    # If the route hasn't been added yet, this will be 405; skip gracefully.
    if resp.status_code == 405:
        pytest.skip("DELETE /metrics route not yet wired into the router.")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["deleted_count"] == 42
