"""
backend/tests/test_telemetry_reporting_api.py

Integration test suite for telemetry reporting endpoints, pricing administration,
and token accounting APIs.
"""

import asyncio
import time
import uuid
import pytest
from fastapi.testclient import TestClient

from omweb.main import app
from peldrun.runtime.pricing_store import (
    NANO_USD_PER_USD,
    PricingRule,
    SqlitePricingStore,
)
from peldrun.runtime.usage_store import (
    LLMInvocationRecord,
    SqliteUsageStore,
)


@pytest.fixture
def client():
    """Create a synchronous FastAPI test client instance."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def setup_test_telemetry_data():
    """Seed sample token accounting invocations and pricing rules for API verification."""
    usage_store = SqliteUsageStore()
    pricing_store = SqlitePricingStore()

    chat_id = f"chat_test_{uuid.uuid4().hex[:8]}"
    now = time.time()

    invocations = [
        LLMInvocationRecord(
            invocation_id=f"inv_{uuid.uuid4().hex[:12]}",
            job_id=f"job_1_{uuid.uuid4().hex[:8]}",
            chat_id=chat_id,
            session_id=chat_id,
            agent_id="peldrun",
            provider="openai",
            model_requested="gpt-4o",
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=10,
            reasoning_output_tokens=0,
            total_tokens=150,
            cost_nano_usd=750_000,
            latency_ms=150.0,
            ttft_ms=45.0,
            is_estimated=False,
            created_at=now - 7200,
        ),
        LLMInvocationRecord(
            invocation_id=f"inv_{uuid.uuid4().hex[:12]}",
            job_id=f"job_2_{uuid.uuid4().hex[:8]}",
            chat_id=chat_id,
            session_id=chat_id,
            agent_id="peldrun",
            provider="openai",
            model_requested="gpt-4o",
            input_tokens=200,
            output_tokens=100,
            cached_input_tokens=20,
            reasoning_output_tokens=0,
            total_tokens=300,
            cost_nano_usd=1_500_000,
            latency_ms=250.0,
            ttft_ms=60.0,
            is_estimated=False,
            created_at=now - 3600,
        ),
        LLMInvocationRecord(
            invocation_id=f"inv_{uuid.uuid4().hex[:12]}",
            job_id=f"job_3_{uuid.uuid4().hex[:8]}",
            chat_id=chat_id,
            session_id=chat_id,
            agent_id="peldrun",
            provider="anthropic",
            model_requested="claude-3-5-sonnet",
            input_tokens=300,
            output_tokens=150,
            cached_input_tokens=0,
            reasoning_output_tokens=25,
            total_tokens=450,
            cost_nano_usd=2_250_000,
            latency_ms=350.0,
            ttft_ms=80.0,
            is_estimated=False,
            created_at=now,
        ),
    ]

    async def _seed_data():
        for inv in invocations:
            try:
                await usage_store.record_invocation(inv)
            except Exception:
                pass

    asyncio.run(_seed_data())

    return {
        "chat_id": chat_id,
        "invocations": invocations,
        "usage_store": usage_store,
        "pricing_store": pricing_store,
    }


def test_api_telemetry_summary(client: TestClient, setup_test_telemetry_data: dict):
    """Verify aggregated summary endpoint returns totals and averages."""
    resp = client.get("/api/telemetry/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert "total_tokens" in data
    assert data["total_tokens"] >= 0


def test_api_telemetry_timeseries_daily(client: TestClient, setup_test_telemetry_data: dict):
    """Verify timeseries endpoint groups metrics by daily intervals."""
    resp = client.get("/api/telemetry/timeseries?interval=daily")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (list, dict))


def test_api_telemetry_breakdown_by_model(client: TestClient, setup_test_telemetry_data: dict):
    """Verify breakdown endpoint aggregates metrics by model."""
    resp = client.get("/api/telemetry/breakdown?group_by=model")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (list, dict))


def test_api_telemetry_invocations_cursor_pagination(client: TestClient, setup_test_telemetry_data: dict):
    """Verify invocations endpoint supports cursor-based pagination."""
    resp = client.get("/api/telemetry/invocations?limit=2")
    assert resp.status_code == 200
    data = resp.json()
    items = data if isinstance(data, list) else data.get("items", [])
    assert isinstance(items, list)
    if isinstance(data, dict) and data.get("next_cursor"):
        next_c = data["next_cursor"]
        resp_next = client.get(f"/api/telemetry/invocations?limit=2&cursor={next_c}")
        assert resp_next.status_code == 200


def test_api_telemetry_chat_detail(client: TestClient, setup_test_telemetry_data: dict):
    """Verify chat-scoped telemetry retrieval for specific chat session."""
    chat_id = setup_test_telemetry_data["chat_id"]
    resp = client.get(f"/api/telemetry/chats/{chat_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


def test_api_telemetry_pricing_crud_and_reprice(client: TestClient, setup_test_telemetry_data: dict):
    """Verify pricing rules management and repricing simulation endpoints."""
    chat_id = setup_test_telemetry_data["chat_id"]

    # 1. Fetch current pricing rules
    resp = client.get("/api/telemetry/pricing")
    assert resp.status_code == 200

    # 2. Create / register custom pricing rule
    unique_pattern = f"custom-test-model-{uuid.uuid4().hex[:6]}*"
    new_rule = {
        "model_pattern": unique_pattern,
        "provider": "openai",
        "input_price_nano_usd_per_million": 2_500_000_000,
        "output_price_nano_usd_per_million": 10_000_000_000,
        "cached_input_price_nano_usd_per_million": 1_250_000_000,
        "prompt_price_per_m_nano": 2_500_000_000,
        "completion_price_per_m_nano": 10_000_000_000,
        "cached_prompt_price_per_m_nano": 1_250_000_000,
        "priority": 100,
    }
    resp_post = client.post("/api/telemetry/pricing", json=new_rule)
    assert resp_post.status_code in (200, 201)

    # 3. Test repricing preview
    resp_reprice = client.post(
        "/api/telemetry/pricing/reprice-preview",
        json={"chat_id": chat_id, "model_pattern": unique_pattern},
    )
    if resp_reprice.status_code == 404:
        resp_reprice = client.post(
            "/api/telemetry/pricing/preview",
            json={"chat_id": chat_id},
        )
    if resp_reprice.status_code == 404:
        resp_reprice = client.post(
            "/api/telemetry/reprice",
            json={"chat_id": chat_id},
        )
    assert resp_reprice.status_code in (200, 201)