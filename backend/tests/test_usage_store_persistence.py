"""
backend/tests/test_usage_store_persistence.py

Automated Test Suite for TASK P1-03B (Dual-Layer Persistence & Dynamic Providers).

Verifies:
1. Idempotency: Duplicate invocation_id inserts are safely ignored (no double counting).
2. Integer Currency: cost_nano_usd avoids floating-point precision loss.
3. Dynamic Ingestion: Custom and unseen provider identifiers persist cleanly.
4. Local Endpoint Heuristic: Local endpoints evaluate to zero billing cost.
5. Durability Invariant: Chat deletion from project_manager does NOT delete SQLite token_ledger records.
6. Atomic Projection: session.json updates atomically and re-projects turn metrics.
"""

from pathlib import Path
import pytest

from omweb.project_manager import ProjectManager
from peldrun.runtime.usage_store import (
    NANO_USD_PER_USD,
    LLMInvocationRecord,
    SqliteUsageStore,
    is_local_endpoint,
)


@pytest.fixture
def temp_usage_store(tmp_path):
    """Instantiate a dedicated SQLite usage store in a temporary directory."""
    db_file = tmp_path / "test_peldrun_runtime.db"
    return SqliteUsageStore(db_path=db_file)


@pytest.fixture
def temp_project_manager(tmp_path, monkeypatch):
    """Instantiate a ProjectManager using an isolated temporary storage root."""
    storage_root = tmp_path / "storage"
    pm = ProjectManager()
    pm.storage_dir = storage_root
    pm.chats_dir = storage_root / "chats"
    pm.projects_dir = storage_root / "projects"
    pm.index_file = storage_root / "index.json"
    pm._ensure_storage_structure()
    return pm


@pytest.mark.asyncio
async def test_ledger_idempotency_insert_or_ignore(temp_usage_store):
    """Verify that duplicate invocation_id does not create duplicate accounting records."""
    record = LLMInvocationRecord(
        invocation_id="inv_unique_123",
        chat_id="chat_abc",
        provider="deepseek",
        model_requested="deepseek-chat",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cost_nano_usd=150_000,  # 0.00015 USD
    )

    first_insert = await temp_usage_store.record_invocation(record)
    assert first_insert is True

    # Attempt to insert identical record again (simulating network retry/reconnect)
    duplicate_insert = await temp_usage_store.record_invocation(record)
    assert duplicate_insert is False  # Ignored without error

    invocations = await temp_usage_store.list_chat_invocations("chat_abc")
    assert len(invocations) == 1
    assert invocations[0]["invocation_id"] == "inv_unique_123"
    assert invocations[0]["total_tokens"] == 150


@pytest.mark.asyncio
async def test_dynamic_provider_support(temp_usage_store):
    """Verify arbitrary custom providers are ingested dynamically without schema errors."""
    custom_record = LLMInvocationRecord(
        invocation_id="inv_custom_999",
        chat_id="chat_custom",
        provider="custom-vllm-cluster-west",
        model_requested="meta-llama/Llama-3-70b-custom",
        input_tokens=500,
        output_tokens=250,
        total_tokens=750,
        cost_nano_usd=375_000,
    )

    success = await temp_usage_store.record_invocation(custom_record)
    assert success is True

    retrieved = await temp_usage_store.get_invocation("inv_custom_999")
    assert retrieved is not None
    assert retrieved["provider"] == "custom-vllm-cluster-west"
    assert retrieved["cost_usd"] == 375_000 / NANO_USD_PER_USD


def test_is_local_endpoint_heuristics():
    """Verify local endpoints are accurately detected for zero billing attribution."""
    assert is_local_endpoint("lmstudio") is True
    assert is_local_endpoint("ollama") is True
    assert is_local_endpoint("custom_llm", "http://127.0.0.1:8000/v1") is True
    assert is_local_endpoint("custom_llm", "http://localhost:11434/v1") is True
    assert is_local_endpoint("openai", "https://api.openai.com/v1") is False
    assert is_local_endpoint("deepseek", "https://api.deepseek.com/v1") is False


@pytest.mark.asyncio
async def test_chat_deletion_preserves_immutable_ledger(temp_usage_store, temp_project_manager):
    """Verify deleting a chat from project_manager does NOT delete records from SQLite ledger."""
    chat_id = "chat_durable_test"
    job_id = "job_durable_test"

    # 1. Create chat workspace files
    temp_project_manager.save_chat_session(
        chat_id=chat_id,
        project_id="default_project",
        title="Test Durable Chat",
        job_id=job_id,
        prompt="Build something",
        events=[],
    )
    assert temp_project_manager.get_chat(chat_id) is not None

    # 2. Record ledger fact in SQLite
    record = LLMInvocationRecord(
        invocation_id="inv_durable_1",
        chat_id=chat_id,
        job_id=job_id,
        provider="lmstudio",
        model_requested="qwen-7b",
        input_tokens=200,
        output_tokens=100,
        total_tokens=300,
        cost_nano_usd=0,
    )
    await temp_usage_store.record_invocation(record)

    # 3. Delete chat via project manager
    deleted = temp_project_manager.delete_chat(chat_id)
    assert deleted is True
    assert temp_project_manager.get_chat(chat_id) is None

    # 4. Verify immutable SQLite ledger entry remains intact
    invocations = await temp_usage_store.list_chat_invocations(chat_id)
    assert len(invocations) == 1
    assert invocations[0]["invocation_id"] == "inv_durable_1"
    assert invocations[0]["total_tokens"] == 300


def test_atomic_session_projection_turn_usage(temp_project_manager):
    """Verify record_turn_usage updates turns and re-aggregates usage_summary atomically."""
    chat_id = "chat_turn_test"
    temp_project_manager.save_chat_session(
        chat_id=chat_id,
        project_id="default_project",
        title="Turn Aggregation Test",
        job_id="job_turn_test",
        prompt="Initial prompt",
        events=[],
    )

    # Record Turn 1
    summary1 = temp_project_manager.record_turn_usage(
        chat_id=chat_id,
        turn_id="turn_1",
        turn_usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001, "estimated_tokens": 0},
    )
    assert summary1["total_tokens"] == 150
    assert summary1["turn_count"] == 1

    # Record Turn 2
    summary2 = temp_project_manager.record_turn_usage(
        chat_id=chat_id,
        turn_id="turn_2",
        turn_usage={"input_tokens": 200, "output_tokens": 80, "cost_usd": 0.002, "estimated_tokens": 0},
    )
    assert summary2["input_tokens"] == 300
    assert summary2["output_tokens"] == 130
    assert summary2["total_tokens"] == 430
    assert summary2["total_cost_usd"] == 0.003
    assert summary2["turn_count"] == 2

    # Verify session.json persisted state
    chat_data = temp_project_manager.get_chat(chat_id)
    assert chat_data is not None
    assert chat_data["usage_summary"]["total_tokens"] == 430
    assert len(chat_data["turns"]) == 2