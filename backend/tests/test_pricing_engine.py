"""
backend/tests/test_pricing_engine.py

Automated Test Suite for TASK P1-03C (Dynamic Pricing & Cost Calculation Engine).

Verifies:
1. Deterministic resolution hierarchy (Exact provider/model -> Pattern -> Fallback).
2. Local zero-billing attribution for LM Studio and Ollama.
3. Integer Nano-USD cost calculation with prompt caching discount.
4. Soft-retirement semantics (rule remains inactive, not deleted).
5. Non-destructive repricing preview preserving immutable ledger records.
"""

from pathlib import Path
import pytest

from peldrun.llm.client import TokenUsage
from peldrun.runtime.pricing_store import (
    NANO_USD_PER_USD,
    PricingRule,
    SqlitePricingStore,
)
from peldrun.runtime.usage_store import LLMInvocationRecord, SqliteUsageStore


@pytest.fixture
def isolated_pricing_store(tmp_path):
    """Instantiate a dedicated SQLite pricing store in a temporary directory."""
    db_file = tmp_path / "test_peldrun_runtime.db"
    return SqlitePricingStore(db_path=db_file)


@pytest.fixture
def isolated_usage_store(tmp_path):
    """Instantiate a companion SQLite usage store sharing the same test database."""
    db_file = tmp_path / "test_peldrun_runtime.db"
    return SqliteUsageStore(db_path=db_file)


@pytest.mark.asyncio
async def test_pricing_seed_catalog_loaded(isolated_pricing_store):
    """Verify built-in default seed rules are initialized automatically."""
    rules = await isolated_pricing_store.list_rules(active_only=True)
    assert len(rules) >= 5

    # Check for core seeded rules
    patterns = [r.model_pattern for r in rules]
    assert "gpt-4o*" in patterns
    assert "deepseek-chat*" in patterns


@pytest.mark.asyncio
async def test_deterministic_resolution_hierarchy(isolated_pricing_store):
    """Verify exact match takes precedence over wildcard pattern match."""
    # 1. Exact match for gpt-4o
    rule_exact = await isolated_pricing_store.resolve_pricing(
        provider="openai",
        model="gpt-4o",
    )
    assert rule_exact.model_pattern == "gpt-4o*"
    assert rule_exact.input_price_nano_usd_per_million == 2_500_000_000

    # 2. Add an explicit exact model override with higher priority
    override = PricingRule(
        pricing_id="prc_custom_gpt4o_exact",
        provider="openai",
        model_pattern="gpt-4o",
        input_price_nano_usd_per_million=2_000_000_000,  # $2.00 / 1M
        output_price_nano_usd_per_million=8_000_000_000,   # $8.00 / 1M
        priority=500,
    )
    await isolated_pricing_store.save_rule(override)

    resolved = await isolated_pricing_store.resolve_pricing(
        provider="openai",
        model="gpt-4o",
    )
    assert resolved.pricing_id == "prc_custom_gpt4o_exact"
    assert resolved.input_price_nano_usd_per_million == 2_000_000_000


@pytest.mark.asyncio
async def test_local_model_zero_billing(isolated_pricing_store):
    """Verify local models automatically evaluate to zero cost basis."""
    rule_lmstudio = await isolated_pricing_store.resolve_pricing(
        provider="lmstudio",
        model="qwen3-vl-8b",
        base_url="http://127.0.0.1:1234/v1",
    )
    assert rule_lmstudio.input_price_nano_usd_per_million == 0
    assert rule_lmstudio.output_price_nano_usd_per_million == 0

    rule_ollama = await isolated_pricing_store.resolve_pricing(
        provider="ollama",
        model="llama3.2:latest",
        base_url="http://localhost:11434/v1",
    )
    assert rule_ollama.input_price_nano_usd_per_million == 0
    assert rule_ollama.output_price_nano_usd_per_million == 0


@pytest.mark.asyncio
async def test_cost_calculation_integer_precision(isolated_pricing_store):
    """Verify accurate cost calculation with prompt caching and integer Nano-USD precision."""
    # Scenario: 1,000 input tokens (500 cached) and 200 output tokens on gpt-4o
    # gpt-4o seed: input = $2.50/1M (2500 nano/tok), cached = $1.25/1M (1250 nano/tok), output = $10.00/1M (10000 nano/tok)
    # Expected: (500 * 2500) + (500 * 1250) + (200 * 10000) = 1,250,000 + 625,000 + 2,000,000 = 3,875,000 Nano-USD ($0.003875)
    usage = TokenUsage(
        input_tokens=1000,
        output_tokens=200,
        total_tokens=1200,
        cached_input_tokens=500,
    )

    cost_nano, rule_id = await isolated_pricing_store.calculate_cost(
        usage=usage,
        provider="openai",
        model="gpt-4o",
    )

    assert cost_nano == 3_875_000
    assert cost_nano / NANO_USD_PER_USD == 0.003875
    assert rule_id.startswith("prc_")


@pytest.mark.asyncio
async def test_soft_retirement_preserves_history(isolated_pricing_store):
    """Verify retiring a pricing rule deactivates it without database deletion."""
    test_rule = PricingRule(
        pricing_id="prc_retire_me",
        provider="custom-corp",
        model_pattern="special-v1",
        input_price_nano_usd_per_million=5_000_000_000,
        output_price_nano_usd_per_million=15_000_000_000,
        priority=200,
    )
    await isolated_pricing_store.save_rule(test_rule)

    rule_before = await isolated_pricing_store.get_rule("prc_retire_me")
    assert rule_before is not None
    assert rule_before.active is True

    # Soft retire the rule
    success = await isolated_pricing_store.retire_rule("prc_retire_me")
    assert success is True

    # Confirm it still exists in persistent storage with active=False
    rule_after = await isolated_pricing_store.get_rule("prc_retire_me")
    assert rule_after is not None
    assert rule_after.active is False
    assert rule_after.effective_to is not None

    # Confirm it is excluded from active listings
    active_rules = await isolated_pricing_store.list_rules(active_only=True)
    assert "prc_retire_me" not in [r.pricing_id for r in active_rules]


@pytest.mark.asyncio
async def test_preview_repricing_without_ledger_mutation(isolated_pricing_store, isolated_usage_store):
    """Verify repricing simulation correctly projects costs without mutating token_ledger facts."""
    # 1. Record an original invocation in SQLite ledger with historical pricing
    inv_record = LLMInvocationRecord(
        invocation_id="inv_reprice_test_1",
        chat_id="chat_reprice",
        provider="deepseek",
        model_requested="deepseek-chat",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        total_tokens=2_000_000,
        cost_nano_usd=420_000_000,  # Originally $0.42 ($0.14 input + $0.28 output)
    )
    await isolated_usage_store.record_invocation(inv_record)

    # 2. Add a new updated pricing rule for deepseek-chat with higher priority
    new_rule = PricingRule(
        pricing_id="prc_deepseek_v2",
        provider="deepseek",
        model_pattern="deepseek-chat*",
        input_price_nano_usd_per_million=200_000_000,  # Updated to $0.20 / 1M
        output_price_nano_usd_per_million=400_000_000, # Updated to $0.40 / 1M
        priority=300,
    )
    await isolated_pricing_store.save_rule(new_rule)

    # 3. Execute non-destructive repricing preview
    preview = await isolated_pricing_store.preview_repricing(chat_id="chat_reprice")

    assert preview["invocations_evaluated"] == 1
    assert preview["original_cost_nano_usd"] == 420_000_000
    assert preview["original_cost_usd"] == 0.42

    # New cost: 1M * 0.20 + 1M * 0.40 = $0.60 (600,000,000 Nano-USD)
    assert preview["recalculated_cost_nano_usd"] == 600_000_000
    assert preview["recalculated_cost_usd"] == 0.60
    assert preview["difference_usd"] == 0.18

    # 4. Verify historical token_ledger record was NOT mutated
    ledger_record = await isolated_usage_store.get_invocation("inv_reprice_test_1")
    assert ledger_record is not None
    assert ledger_record["cost_nano_usd"] == 420_000_000  # Intact and unchanged!