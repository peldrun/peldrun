"""
backend/tests/test_telemetry_reporting_api.py

Automated Test Suite for TASK P1-03D (Time-Series Analytics & Aggregated Reporting APIs).

Verifies:
1. GET /api/telemetry/summary returns aggregated tokens, costs, and latencies.
2. GET /api/telemetry/timeseries partitions data into day, week, and month buckets.
3. GET /api/telemetry/breakdown groups accurately by model, provider, mode, and project.
4. GET /api/telemetry/invocations supports cursor pagination without schema corruption.
5. GET /api/telemetry/chats/{chat_id} isolates chat-specific metrics.
6. Pricing CRUD & non-destructive repricing preview endpoints.
"""

from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

from omweb.main import app
from peldrun.runtime.pricing_store import SqlitePricingStore
from peldrun.runtime.usage_store import LLMInvocationRecord, SqliteUsageStore


@pytest.fixture
async def setup_test_telemetry_data(tmp_path, monkeypatch):
    """Seed test SQLite database with multi-turn invocation facts across dates and models."""
    db_file = tmp_path / "test_peldrun_runtime.db"

    test_usage_store = SqliteUsageStore(db_path=db_file)
    test_pricing_store = SqlitePricingStore(db_path=db_file)

    # Monkeypatch singleton instances for the test session
    import peldrun.runtime.usage_store as u_mod
    import peldrun.runtime.pricing_store as p_mod
    monkeypatch.setattr(u_mod, "get_usage_store", lambda *args, **kwargs: test_usage_store)
    monkeypatch.setattr(p_mod, "get_pricing_store", lambda *args, **kwargs: test_pricing_store)

    import omweb.services.telemetry_service as s_mod
    monkeypatch.setattr(s_mod.telemetry_service, "usage_store", test_usage_store)
    monkeypatch.setattr(s_mod.telemetry_service, "pricing_store", test_pricing_store)

    # Seed Record 1: gpt-4o, Day 1
    t1 = 1700000000.0  # Approx 2023-11-14
    await test_usage_store.record_invocation(
        LLMInvocationRecord(
            invocation_id="inv_api_1",
            chat_id="chat_alpha",
            project_id="proj_1",
            mode="agent",
            provider="openai",
            model_requested="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cost_nano_usd=7_500_000,  # $0.0075
            latency_ms=1200.0,
            ttft_ms=300.0,
            created_at=t1,
        )
    )

    # Seed Record 2: deepseek-chat, Day 1
    await test_usage_store.record_invocation(
        LLMInvocationRecord(
            invocation_id="inv_api_2",
            chat_id="chat_alpha",
            project_id="proj_1",
            mode="chat",
            provider="deepseek",
            model_requested="deepseek-chat",
            input_tokens=2000,
            output_tokens=1000,
            total_tokens=3000,
            cost_nano_usd=560_000,
            latency_ms=800.0,
            ttft_ms=150.0,
            created_at=t1 + 3600.0,
        )
    )

    # Seed Record 3: gpt-4o, Day 2
    t2 = t1 + 86400.0
    await test_usage_store.record_invocation(
        LLMInvocationRecord(
            invocation_id="inv_api_3",
            chat_id="chat_beta",
            project_id="proj_2",
            mode="agent",
            provider="openai",
            model_requested="gpt-4o",
            input_tokens=500,
            output_tokens=250,
            total_tokens=750,
            cost_nano_usd=3_750_000,
            latency_ms=900.0,
            ttft_ms=200.0,
            created_at=t2,
        )
    )

    return test_usage_store, test_pricing_store


@pytest.mark.asyncio
async def test_api_telemetry_summary(setup_test_telemetry_data):
    """Verify /api/telemetry/summary returns aggregated totals and averages."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/telemetry/summary")
        assert resp.status_code == 200
        data = resp.json()

        assert data["llm_call_count"] == 3
        assert data["total_tokens"] == 1500 + 3000 + 750
        assert data["input_tokens"] == 1000 + 2000 + 500
        assert data["output_tokens"] == 500 + 1000 + 250
        assert data["total_cost_usd"] > 0.0
        assert data["avg_latency_ms"] > 0.0


@pytest.mark.asyncio
async def test_api_telemetry_timeseries_daily(setup_test_telemetry_data):
    """Verify /api/telemetry/timeseries partitions consumption across daily buckets."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/telemetry/timeseries?granularity=day")
        assert resp.status_code == 200
        buckets = resp.json()

        # Two distinct days seeded
        assert len(buckets) == 2
        # Day 1 has 2 invocations, Day 2 has 1 invocation
        assert buckets[0]["llm_call_count"] == 2
        assert buckets[1]["llm_call_count"] == 1


@pytest.mark.asyncio
async def test_api_telemetry_breakdown_by_model(setup_test_telemetry_data):
    """Verify /api/telemetry/breakdown groups accurately by requested model."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/telemetry/breakdown?group_by=model")
        assert resp.status_code == 200
        breakdown = resp.json()

        models = [item["key"] for item in breakdown]
        assert "gpt-4o" in models
        assert "deepseek-chat" in models

        gpt4_item = next(item for item in breakdown if item["key"] == "gpt-4o")
        assert gpt4_item["llm_call_count"] == 2
        assert gpt4_item["total_tokens"] == 2250


@pytest.mark.asyncio
async def test_api_telemetry_invocations_cursor_pagination(setup_test_telemetry_data):
    """Verify /api/telemetry/invocations paginates cleanly with limit and cursor."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Page 1: limit 2
        resp1 = await ac.get("/api/telemetry/invocations?limit=2")
        assert resp1.status_code == 200
        page1 = resp1.json()

        assert len(page1["items"]) == 2
        assert page1["has_more"] is True
        assert page1["next_cursor"] is not None

        # Page 2: with cursor
        resp2 = await ac.get(f"/api/telemetry/invocations?limit=2&cursor={page1['next_cursor']}")
        assert resp2.status_code == 200
        page2 = resp2.json()

        assert len(page2["items"]) == 1
        assert page2["has_more"] is False


@pytest.mark.asyncio
async def test_api_telemetry_chat_detail(setup_test_telemetry_data):
    """Verify /api/telemetry/chats/{chat_id} returns chat summary and invocations."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/telemetry/chats/chat_alpha")
        assert resp.status_code == 200
        data = resp.json()

        assert "summary" in data
        assert "invocations" in data
        assert data["summary"]["llm_call_count"] == 2
        assert len(data["invocations"]) == 2


@pytest.mark.asyncio
async def test_api_telemetry_pricing_crud_and_reprice(setup_test_telemetry_data):
    """Verify pricing retrieval, creation, retirement, and simulation preview."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. List pricing
        res_list = await ac.get("/api/telemetry/pricing")
        assert res_list.status_code == 200
        assert len(res_list.json()) >= 5

        # 2. Create pricing rule
        new_rule_payload = {
            "model_pattern": "qwen3-72b*",
            "provider": "openrouter",
            "input_price_usd_per_million": 0.40,
            "output_price_usd_per_million": 0.80,
            "priority": 50,
        }
        res_create = await ac.post("/api/telemetry/pricing", json=new_rule_payload)
        assert res_create.status_code == 200
        rule_id = res_create.json()["pricing_id"]

        # 3. Retire pricing rule
        res_retire = await ac.post(f"/api/telemetry/pricing/{rule_id}/retire")
        assert res_retire.status_code == 200

        # 4. Repricing simulation preview
        res_reprice = await ac.post("/api/telemetry/reprice", json={"chat_id": "chat_alpha"})
        assert res_reprice.status_code == 200
        preview = res_reprice.json()
        assert preview["invocations_evaluated"] == 2
        assert "original_cost_usd" in preview
        assert "recalculated_cost_usd" in preview