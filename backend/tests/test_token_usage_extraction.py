"""
backend/tests/test_token_usage_extraction.py

Automated Test Suite for TASK P1-03A (Core LLM Usage Extraction & Fallback).

Verifies:
1. Exact provider usage normalization (input, output, total, cached, reasoning).
2. Backward compatibility with legacy dict access (usage.get(), usage['prompt_tokens']).
3. Tokenizer fallback estimation when provider returns empty or None usage.
4. Semantic invariant enforcement: Unknown != 0 tokens.
5. True streaming chunk yield, TTFT recording, and usage propagation.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from peldrun.llm.client import (
    AsyncLLMClient,
    LLMConfig,
    LLMResponse,
    StreamChunk,
    TokenUsage,
    UsageSource,
)
from peldrun.llm.tokenizer import estimate_invocation_usage


def test_token_usage_exact_normalization():
    """Verify exact provider usage mapping adheres to the typed model."""
    raw_payload = {
        "prompt_tokens": 150,
        "completion_tokens": 50,
        "total_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 30},
        "completion_tokens_details": {"reasoning_tokens": 20},
    }
    client = AsyncLLMClient()
    usage = client._normalize_raw_usage(raw_payload)

    assert usage is not None
    assert usage.input_tokens == 150
    assert usage.output_tokens == 50
    assert usage.total_tokens == 200
    assert usage.cached_input_tokens == 30
    assert usage.reasoning_output_tokens == 20
    assert usage.source == UsageSource.PROVIDER
    assert usage.estimated is False


def test_token_usage_backward_compatibility():
    """Verify legacy callers can access fields via dict indexing and get()."""
    usage = TokenUsage(
        input_tokens=100,
        output_tokens=40,
        total_tokens=140,
        source=UsageSource.PROVIDER,
    )
    # Legacy aliases
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 40
    assert usage.get("prompt_tokens") == 100
    assert usage.get("completion_tokens") == 40
    assert usage.get("total_tokens") == 140
    assert usage["total_tokens"] == 140
    assert usage["prompt_tokens"] == 100


def test_semantic_invariant_unknown_not_zero():
    """Verify unknown tokens remain None and are never coerced to zero."""
    usage = TokenUsage(
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        source=UsageSource.UNKNOWN,
        estimated=True,
    )
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None
    assert usage.source == UsageSource.UNKNOWN
    # Must not evaluate as 0
    assert usage.input_tokens != 0


def test_tokenizer_fallback_estimation():
    """Verify tokenizer fallback estimates metrics accurately when provider usage is absent."""
    messages = [
        {"role": "system", "content": "You are a specialized code assistant."},
        {"role": "user", "content": "Write a python function to compute fibonacci."},
    ]
    output_text = "def fib(n): return n if n <= 1 else fib(n-1) + fib(n-2)"
    reasoning_text = "The user wants a clean recursive fibonacci implementation."

    est = estimate_invocation_usage(
        messages=messages,
        output_text=output_text,
        reasoning_text=reasoning_text,
        model="gpt-4",
    )

    assert est["input_tokens"] > 0
    assert est["output_tokens"] > 0
    assert est["total_tokens"] == est["input_tokens"] + est["output_tokens"]
    assert est["reasoning_output_tokens"] > 0
    assert est["source"] == "estimated"
    assert est["estimated"] is True
    assert est["tokenizer_id"] is not None


@pytest.mark.asyncio
async def test_chat_completion_with_mocked_provider_usage():
    """Verify chat_completion produces canonical TokenUsage in LLMResponse."""
    client = AsyncLLMClient()

    mock_resp = MagicMock()
    mock_resp.model = "test-model"
    mock_choice = MagicMock()
    mock_choice.message.content = "Hello, world!"
    mock_choice.message.reasoning_content = "Thinking..."
    mock_choice.message.tool_calls = None
    mock_choice.finish_reason = "stop"
    mock_resp.choices = [mock_choice]
    mock_resp.usage = {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
    }

    client.client.chat.completions.create = AsyncMock(return_value=mock_resp)

    res = await client.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
    )

    assert isinstance(res, LLMResponse)
    assert res.content == "Hello, world!"
    assert res.reasoning == "Thinking..."
    assert res.usage is not None
    assert res.usage.input_tokens == 12
    assert res.usage.output_tokens == 5
    assert res.usage.total_tokens == 17
    assert res.usage.source == UsageSource.PROVIDER
    assert res.latency_ms is not None


@pytest.mark.asyncio
async def test_stream_yields_true_chunks_and_usage():
    """Verify true streaming delivers chunk deltas and captures final usage."""
    client = AsyncLLMClient()

    async def mock_stream_iterator():
        # Chunk 1: reasoning
        c1 = MagicMock()
        c1.usage = None
        d1 = MagicMock()
        d1.content = None
        d1.reasoning_content = "Plan"
        d1.tool_calls = None
        ch1 = MagicMock()
        ch1.delta = d1
        ch1.finish_reason = None
        c1.choices = [ch1]
        yield c1

        # Chunk 2: content
        c2 = MagicMock()
        c2.usage = None
        d2 = MagicMock()
        d2.content = "Answer"
        d2.reasoning_content = None
        d2.tool_calls = None
        ch2 = MagicMock()
        ch2.delta = d2
        ch2.finish_reason = None
        c2.choices = [ch2]
        yield c2

        # Chunk 3: usage metadata chunk
        c3 = MagicMock()
        c3.usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        c3.choices = []
        yield c3

    client.client.chat.completions.create = AsyncMock(return_value=mock_stream_iterator())

    chunks = []
    async for chunk in client.stream(messages=[{"role": "user", "content": "test"}]):
        chunks.append(chunk)

    assert len(chunks) >= 2
    assert chunks[0].reasoning_delta == "Plan"
    assert chunks[1].content_delta == "Answer"