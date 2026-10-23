# backend/omweb/resource_monitor/aggregation.py
"""Mathematical aggregation and downsampling utilities for PELDRUN Resource Monitor.

Provides statistical reduction functions, peak-preserving timeseries downsampling,
and multi-span aggregation across tools and execution runs.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Union

from .models import (
    ExecutionRunModel,
    ExecutionSpanModel,
    RunStatus,
    SpanType,
    TimeseriesPoint,
    ToolConsumptionSummary,
)


def safe_mean(values: Sequence[Union[int, float]]) -> float:
    """Calculate arithmetic mean safely without division by zero.

    Args:
        values: Sequence of numeric values.

    Returns:
        The calculated mean or 0.0 if the sequence is empty.
    """
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def calculate_percentile(values: Sequence[Union[int, float]], percentile: float) -> float:
    """Calculate the k-th percentile using nearest-rank estimation.

    Args:
        values: Sequence of numeric values.
        percentile: Float between 0.0 and 100.0.

    Returns:
        The computed percentile value, or 0.0 for empty inputs.
    """
    if not values:
        return 0.0

    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])

    k = max(0.0, min(100.0, percentile))
    rank = (k / 100.0) * (n - 1)
    low_idx = int(math.floor(rank))
    high_idx = int(math.ceil(rank))

    if low_idx == high_idx:
        return float(sorted_vals[low_idx])

    weight = rank - low_idx
    return float(sorted_vals[low_idx] * (1.0 - weight) + sorted_vals[high_idx] * weight)


class TimeseriesAggregator:
    """Handles downsampling and bucket aggregation for charting telemetry."""

    @staticmethod
    def downsample_points(
        points: List[TimeseriesPoint],
        target_buckets: int = 100,
    ) -> List[TimeseriesPoint]:
        """Downsample a timeseries sequence into target buckets while preserving peaks.

        Args:
            points: Chronologically ordered list of TimeseriesPoint instances.
            target_buckets: Target maximum number of aggregated points to return.

        Returns:
            Reduced list of TimeseriesPoints representing the sampled range.
        """
        total_points = len(points)
        if total_points <= target_buckets or target_buckets <= 1:
            return points

        bucket_size = math.ceil(total_points / target_buckets)
        reduced: List[TimeseriesPoint] = []

        for i in range(0, total_points, bucket_size):
            chunk = points[i : i + bucket_size]
            if not chunk:
                continue

            # Bucket timestamp: midpoint of the bucket
            mid_idx = len(chunk) // 2
            timestamp_ms = chunk[mid_idx].timestamp_epoch_ms

            # Preserve peak CPU while calculating average for smooth trend
            cpu_vals = [p.host_cpu_percent for p in chunk]
            host_cpu_avg = round(safe_mean(cpu_vals), 2)
            host_cpu_peak = max(cpu_vals)
            # Retain higher peak if spike exceeded 20% over average
            effective_cpu = host_cpu_peak if (host_cpu_peak - host_cpu_avg) > 20.0 else host_cpu_avg

            # RAM telemetry
            ram_used_avg = int(safe_mean([p.host_ram_used_bytes for p in chunk]))
            peldrun_rss_max = max(p.peldrun_rss_bytes for p in chunk)
            children_rss_max = max(p.peldrun_children_rss_bytes for p in chunk)
            total_rss_max = max(p.peldrun_total_rss_bytes for p in chunk)
            max_active_runs = max(p.active_runs_count for p in chunk)

            reduced.append(
                TimeseriesPoint(
                    timestamp_epoch_ms=timestamp_ms,
                    host_cpu_percent=round(effective_cpu, 2),
                    host_ram_used_bytes=ram_used_avg,
                    peldrun_rss_bytes=peldrun_rss_max,
                    peldrun_children_rss_bytes=children_rss_max,
                    peldrun_total_rss_bytes=total_rss_max,
                    active_runs_count=max_active_runs,
                )
            )

        return reduced


class ToolMetricsAggregator:
    """Aggregates granular tool and reasoning spans into consumption profiles."""

    @staticmethod
    def aggregate_spans(spans: List[ExecutionSpanModel]) -> List[ToolConsumptionSummary]:
        """Group and summarize tool execution metrics across execution spans.

        Args:
            spans: List of execution spans to analyze.

        Returns:
            List of ToolConsumptionSummary instances sorted by cumulative CPU time.
        """
        # Filter for tool execution spans only
        tool_spans = [s for s in spans if s.span_type == SpanType.TOOL_CALL]
        if not tool_spans:
            return []

        grouped: Dict[str, List[ExecutionSpanModel]] = {}
        for s in tool_spans:
            grouped.setdefault(s.target_name, []).append(s)

        summaries: List[ToolConsumptionSummary] = []
        for tool_name, items in grouped.items():
            durations = [float(s.duration_ms or 0) for s in items]
            peak_rss_vals = [s.rss_peak_bytes or 0 for s in items]
            cpu_times = [s.cpu_time_consumed_ms or 0 for s in items]

            total_calls = len(items)
            avg_duration = round(safe_mean(durations), 2)
            avg_rss = int(safe_mean(peak_rss_vals))
            peak_rss = max(peak_rss_vals, default=0)
            cumulative_cpu = sum(cpu_times)

            summaries.append(
                ToolConsumptionSummary(
                    tool_name=tool_name,
                    total_calls=total_calls,
                    avg_duration_ms=avg_duration,
                    avg_rss_bytes=avg_rss,
                    peak_rss_bytes=peak_rss,
                    cumulative_cpu_time_ms=cumulative_cpu,
                )
            )

        # Sort descending by cumulative CPU time
        summaries.sort(key=lambda x: x.cumulative_cpu_time_ms, reverse=True)
        return summaries


class RunMetricsAggregator:
    """Computes summary statistics across multiple execution runs."""

    @staticmethod
    def summarize_runs(runs: List[ExecutionRunModel]) -> Dict[str, Any]:
        """Compile aggregate statistical metrics across a list of runs.

        Args:
            runs: List of ExecutionRunModel entities.

        Returns:
            Dictionary containing total count, status breakdown, duration distribution,
            and peak hardware metrics.
        """
        if not runs:
            return {
                "total_runs": 0,
                "status_breakdown": {},
                "avg_duration_ms": 0.0,
                "p95_duration_ms": 0.0,
                "peak_rss_bytes": 0,
                "total_cpu_time_ms": 0,
            }

        status_breakdown: Dict[str, int] = {}
        durations: List[float] = []
        peak_rss_vals: List[int] = []
        cpu_times: List[int] = []

        for r in runs:
            status_str = r.status.value
            status_breakdown[status_str] = status_breakdown.get(status_str, 0) + 1

            if r.duration_ms is not None:
                durations.append(float(r.duration_ms))
            peak_rss_vals.append(r.peak_rss_bytes or 0)
            cpu_times.append(r.cumulative_cpu_time_ms or 0)

        return {
            "total_runs": len(runs),
            "status_breakdown": status_breakdown,
            "avg_duration_ms": round(safe_mean(durations), 2),
            "p95_duration_ms": round(calculate_percentile(durations, 95.0), 2),
            "peak_rss_bytes": max(peak_rss_vals, default=0),
            "total_cpu_time_ms": sum(cpu_times),
        }
