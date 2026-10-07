"""
backend/peldrun/runtime/diagnostics.py

PELDRUN Core Structured Diagnostics & Latency Telemetry Subsystem.

Provides comprehensive metrics collection for autonomous agent execution:
- Invocation metrics: TTFT (Time to First Token), latency, throughput (tokens/sec).
- Step and tool metrics: execution duration, call frequency, failure categorization.
- OpenTelemetry GenAI Semantic Conventions compatibility mapping.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from peldrun.runtime.contract import RunStatus

logger = logging.getLogger("peldrun.runtime.diagnostics")


class ToolExecutionMetric(BaseModel):
    """Execution telemetry captured for an individual tool invocation."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    tool_name: str = Field(..., description="Name of the invoked tool")
    step: int = Field(default=1, description="Agent execution step sequence index")
    started_at: float = Field(default_factory=time.time)
    completed_at: Optional[float] = None
    duration_ms: float = Field(default=0.0)
    success: bool = Field(default=True)
    error_message: Optional[str] = None


class InvocationTelemetry(BaseModel):
    """Fine-grained latency and token throughput metrics for a single LLM request."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    invocation_id: str
    provider: str
    model: str
    mode: str = "agent"

    started_at: float
    first_token_at: Optional[float] = None
    completed_at: float

    total_latency_ms: float = 0.0
    ttft_ms: Optional[float] = None
    generation_latency_ms: Optional[float] = None
    tokens_per_second: Optional[float] = None

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None

    cost_nano_usd: int = 0
    finish_reason: Optional[str] = None
    failure_reason: Optional[str] = None


class RunDiagnostics(BaseModel):
    """Holistic diagnostic and performance report for an entire agent run."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    run_id: str
    job_id: str
    chat_id: Optional[str] = None
    status: RunStatus = RunStatus.COMPLETED

    started_at: float = Field(default_factory=time.time)
    completed_at: Optional[float] = None
    total_duration_ms: float = 0.0

    total_steps: int = 0
    total_tool_calls: int = 0
    successful_tool_calls: int = 0
    failed_tool_calls: int = 0

    tool_metrics: List[ToolExecutionMetric] = Field(default_factory=list)
    invocations: List[InvocationTelemetry] = Field(default_factory=list)

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cached_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cost_nano_usd: int = 0

    avg_ttft_ms: Optional[float] = None
    overall_tokens_per_second: Optional[float] = None
    failure_reason: Optional[str] = None

    otel_attributes: Dict[str, Any] = Field(default_factory=dict)


class DiagnosticsCollector:
    """Thread-safe lifecycle metrics aggregator during agent workflow execution."""

    def __init__(self, run_id: str, job_id: str, chat_id: Optional[str] = None) -> None:
        self.run_id = run_id
        self.job_id = job_id
        self.chat_id = chat_id
        self.started_at = time.time()

        self._current_step: int = 0
        self._active_tools: Dict[str, ToolExecutionMetric] = {}
        self._completed_tools: List[ToolExecutionMetric] = []
        self._invocations: List[InvocationTelemetry] = []

    def set_step(self, step_num: int) -> None:
        """Track current execution step progression."""
        self._current_step = max(self._current_step, step_num)

    def start_tool(self, tool_name: str, step_num: Optional[int] = None) -> str:
        """Record the initiation of a tool execution cycle."""
        step = step_num if step_num is not None else self._current_step
        tool_metric = ToolExecutionMetric(
            tool_name=tool_name,
            step=step,
            started_at=time.time(),
        )
        call_key = f"{tool_name}_{len(self._completed_tools)}_{time.time()}"
        self._active_tools[call_key] = tool_metric
        return call_key

    def complete_tool(
        self,
        call_key: str,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> None:
        """Mark a pending tool call as completed and calculate duration."""
        metric = self._active_tools.pop(call_key, None)
        if metric is None:
            return

        now = time.time()
        metric.completed_at = now
        metric.duration_ms = max(0.0, (now - metric.started_at) * 1000.0)
        metric.success = success
        metric.error_message = error_message
        self._completed_tools.append(metric)

    def record_llm_invocation(
        self,
        invocation_id: str,
        provider: str,
        model: str,
        started_at: float,
        completed_at: float,
        first_token_at: Optional[float] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        cached_tokens: Optional[int] = None,
        reasoning_tokens: Optional[int] = None,
        cost_nano_usd: int = 0,
        finish_reason: Optional[str] = None,
        failure_reason: Optional[str] = None,
        mode: str = "agent",
    ) -> InvocationTelemetry:
        """Calculate and register throughput and latency metrics for an LLM call."""
        total_latency_ms = max(0.0, (completed_at - started_at) * 1000.0)

        ttft_ms: Optional[float] = None
        generation_latency_ms: Optional[float] = None
        tokens_per_sec: Optional[float] = None

        if first_token_at is not None and first_token_at >= started_at:
            ttft_ms = (first_token_at - started_at) * 1000.0
            gen_duration = completed_at - first_token_at
            if gen_duration > 0 and output_tokens and output_tokens > 0:
                generation_latency_ms = gen_duration * 1000.0
                tokens_per_sec = round(output_tokens / gen_duration, 2)
        elif output_tokens and output_tokens > 0 and total_latency_ms > 0:
            tokens_per_sec = round(output_tokens / (total_latency_ms / 1000.0), 2)

        telemetry = InvocationTelemetry(
            invocation_id=invocation_id,
            provider=provider.strip().lower(),
            model=model.strip(),
            mode=mode,
            started_at=started_at,
            first_token_at=first_token_at,
            completed_at=completed_at,
            total_latency_ms=round(total_latency_ms, 2),
            ttft_ms=round(ttft_ms, 2) if ttft_ms is not None else None,
            generation_latency_ms=round(generation_latency_ms, 2) if generation_latency_ms is not None else None,
            tokens_per_second=tokens_per_sec,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_nano_usd=cost_nano_usd,
            finish_reason=finish_reason,
            failure_reason=failure_reason,
        )
        self._invocations.append(telemetry)
        return telemetry

    def finalize(
        self,
        status: RunStatus,
        failure_reason: Optional[str] = None,
    ) -> RunDiagnostics:
        """Compile and seal canonical RunDiagnostics with OpenTelemetry conventions."""
        completed_at = time.time()
        total_duration_ms = max(0.0, (completed_at - self.started_at) * 1000.0)

        total_tools = len(self._completed_tools)
        successful_tools = sum(1 for t in self._completed_tools if t.success)
        failed_tools = sum(1 for t in self._completed_tools if not t.success)

        tot_inp = sum(inv.input_tokens or 0 for inv in self._invocations)
        tot_out = sum(inv.output_tokens or 0 for inv in self._invocations)
        tot_all = sum(inv.total_tokens or (tot_inp + tot_out) for inv in self._invocations)
        tot_cached = sum(inv.cached_tokens or 0 for inv in self._invocations)
        tot_reas = sum(inv.reasoning_tokens or 0 for inv in self._invocations)
        tot_cost = sum(inv.cost_nano_usd for inv in self._invocations)

        ttft_values = [inv.ttft_ms for inv in self._invocations if inv.ttft_ms is not None]
        avg_ttft = round(sum(ttft_values) / len(ttft_values), 2) if ttft_values else None

        # Calculate weighted average tokens/second
        total_gen_duration = sum(
            (inv.generation_latency_ms or inv.total_latency_ms) / 1000.0
            for inv in self._invocations
            if (inv.output_tokens or 0) > 0
        )
        overall_throughput: Optional[float] = None
        if total_gen_duration > 0 and tot_out > 0:
            overall_throughput = round(tot_out / total_gen_duration, 2)

        # OpenTelemetry GenAI Semantic Conventions Mapping
        primary_provider = self._invocations[0].provider if self._invocations else "peldrun"
        primary_model = self._invocations[0].model if self._invocations else "default"

        otel_attrs: Dict[str, Any] = {
            "gen_ai.system": primary_provider,
            "gen_ai.request.model": primary_model,
            "gen_ai.response.model": primary_model,
            "gen_ai.usage.input_tokens": tot_inp,
            "gen_ai.usage.output_tokens": tot_out,
            "gen_ai.operation.name": "agent_run",
            "peldrun.run.id": self.run_id,
            "peldrun.run.job_id": self.job_id,
            "peldrun.run.step_count": self._current_step,
            "peldrun.run.tool_count": total_tools,
            "peldrun.run.tool_failure_count": failed_tools,
            "peldrun.run.llm_call_count": len(self._invocations),
            "peldrun.run.duration_ms": round(total_duration_ms, 2),
        }
        if self.chat_id:
            otel_attrs["gen_ai.conversation.id"] = self.chat_id
        if avg_ttft is not None:
            otel_attrs["gen_ai.response.time_to_first_chunk"] = avg_ttft / 1000.0

        return RunDiagnostics(
            run_id=self.run_id,
            job_id=self.job_id,
            chat_id=self.chat_id,
            status=status,
            started_at=self.started_at,
            completed_at=completed_at,
            total_duration_ms=round(total_duration_ms, 2),
            total_steps=self._current_step,
            total_tool_calls=total_tools,
            successful_tool_calls=successful_tools,
            failed_tool_calls=failed_tools,
            tool_metrics=list(self._completed_tools),
            invocations=list(self._invocations),
            total_input_tokens=tot_inp,
            total_output_tokens=tot_out,
            total_tokens=tot_all,
            total_cached_tokens=tot_cached,
            total_reasoning_tokens=tot_reas,
            total_cost_nano_usd=tot_cost,
            avg_ttft_ms=avg_ttft,
            overall_tokens_per_second=overall_throughput,
            failure_reason=failure_reason,
            otel_attributes=otel_attrs,
        )


__all__ = [
    "ToolExecutionMetric",
    "InvocationTelemetry",
    "RunDiagnostics",
    "DiagnosticsCollector",
]