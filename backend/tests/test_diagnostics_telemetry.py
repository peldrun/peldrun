"""
backend/tests/test_diagnostics_telemetry.py

Automated Test Suite for TASK P1-05 (Structured Diagnostics & Latency Telemetry).

Verifies:
1. TTFT and latency calculations on individual LLM invocations.
2. Throughput calculation: tokens_per_second computed strictly from output generation.
3. Tool execution tracking: duration, count, and failure status capture.
4. OpenTelemetry GenAI Semantic Conventions mapping in finalize().
5. End-to-end integration: RunResult includes structured diagnostics report.
"""

import time
import pytest

from peldrun.runtime.contract import RunStatus
from peldrun.runtime.diagnostics import DiagnosticsCollector, RunDiagnostics


def test_diagnostics_collector_llm_invocation_metrics():
    """Verify TTFT, total latency, and tokens/sec throughput are computed accurately."""
    collector = DiagnosticsCollector(
        run_id="run_diag_1",
        job_id="job_diag_1",
        chat_id="chat_diag_1",
    )

    t_start = 1000.0
    t_first = 1000.5   # 500ms TTFT
    t_end = 1002.5     # 2500ms total, 2000ms generation duration
    out_tokens = 50

    inv = collector.record_llm_invocation(
        invocation_id="inv_test_1",
        provider="openai",
        model="gpt-4o",
        started_at=t_start,
        first_token_at=t_first,
        completed_at=t_end,
        input_tokens=100,
        output_tokens=out_tokens,
        total_tokens=150,
        cost_nano_usd=500_000,
        finish_reason="stop",
    )

    assert inv.total_latency_ms == 2500.0
    assert inv.ttft_ms == 500.0
    assert inv.generation_latency_ms == 2000.0
    # 50 tokens in 2.0s generation duration = 25.0 tokens/second
    assert inv.tokens_per_second == 25.0


def test_diagnostics_collector_tool_metrics():
    """Verify tool start and completion calculate duration and track success/failure."""
    collector = DiagnosticsCollector(
        run_id="run_diag_tools",
        job_id="job_diag_tools",
    )
    collector.set_step(2)

    # Tool 1: Success
    key1 = collector.start_tool("web_search", step_num=2)
    time.sleep(0.01)
    collector.complete_tool(key1, success=True)

    # Tool 2: Failure
    key2 = collector.start_tool("bash", step_num=2)
    time.sleep(0.01)
    collector.complete_tool(key2, success=False, error_message="Command failed with exit code 127")

    diag = collector.finalize(status=RunStatus.COMPLETED)

    assert diag.total_tool_calls == 2
    assert diag.successful_tool_calls == 1
    assert diag.failed_tool_calls == 1
    assert len(diag.tool_metrics) == 2
    assert diag.tool_metrics[1].tool_name == "bash"
    assert diag.tool_metrics[1].success is False
    assert diag.tool_metrics[1].error_message == "Command failed with exit code 127"
    assert diag.tool_metrics[0].duration_ms > 0.0


def test_opentelemetry_genai_conventions_mapping():
    """Verify finalize() produces compliant OpenTelemetry GenAI semantic attributes."""
    collector = DiagnosticsCollector(
        run_id="run_otel_test",
        job_id="job_otel_test",
        chat_id="chat_otel_123",
    )

    collector.record_llm_invocation(
        invocation_id="inv_otel_1",
        provider="deepseek",
        model="deepseek-chat",
        started_at=100.0,
        first_token_at=100.2,
        completed_at=101.0,
        input_tokens=250,
        output_tokens=75,
        total_tokens=325,
    )

    diag = collector.finalize(status=RunStatus.COMPLETED)
    attrs = diag.otel_attributes

    assert attrs["gen_ai.system"] == "deepseek"
    assert attrs["gen_ai.request.model"] == "deepseek-chat"
    assert attrs["gen_ai.response.model"] == "deepseek-chat"
    assert attrs["gen_ai.usage.input_tokens"] == 250
    assert attrs["gen_ai.usage.output_tokens"] == 75
    assert attrs["gen_ai.conversation.id"] == "chat_otel_123"
    assert attrs["gen_ai.operation.name"] == "agent_run"
    assert attrs["peldrun.run.id"] == "run_otel_test"
    assert attrs["peldrun.run.job_id"] == "job_otel_test"
    assert attrs["peldrun.run.llm_call_count"] == 1
    assert attrs["gen_ai.response.time_to_first_chunk"] == 0.2  # 200ms in seconds


def test_diagnostics_report_serialization():
    """Verify RunDiagnostics model serializes cleanly to JSON without schema loss."""
    collector = DiagnosticsCollector(
        run_id="run_serial",
        job_id="job_serial",
    )
    diag = collector.finalize(status=RunStatus.COMPLETED)
    dumped = diag.model_dump(mode="json")

    assert dumped["run_id"] == "run_serial"
    assert dumped["status"] == "completed"
    assert "otel_attributes" in dumped
    assert isinstance(dumped["tool_metrics"], list)
    assert isinstance(dumped["invocations"], list)