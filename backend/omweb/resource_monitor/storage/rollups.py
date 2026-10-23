# backend/omweb/resource_monitor/storage/rollups.py
"""Periodic historical rollups and aggregation engine for PELDRUN Resource Monitor.

Computes hourly, daily, weekly, and monthly aggregated summaries across runs,
spans, high-frequency samples, and diagnostic anomalies for long-term capacity planning.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Dict, List, Optional
import uuid

from ..models import (
    DiagnosticEventType,
    ResourceRollupModel,
    RollupPeriodType,
)
from .database import MonitorDatabase
from .repositories import MonitorRepository

logger = logging.getLogger(__name__)


class RollupEngine:
    """Computes and stores periodic historical resource rollups."""

    def __init__(
        self,
        database: MonitorDatabase,
        repository: Optional[MonitorRepository] = None,
    ) -> None:
        self._db = database
        self._repo = repository or MonitorRepository(database)

    def compute_hourly_rollup(self, start_dt: datetime) -> Optional[ResourceRollupModel]:
        """Aggregate all metrics for a single 1-hour window.

        Args:
            start_dt: Starting datetime for the hourly bucket (normalized to hour start).

        Returns:
            The generated ResourceRollupModel, or None if computation failed.
        """
        # Normalize to start of hour
        bucket_start = start_dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        bucket_end = bucket_start + timedelta(hours=1)

        start_iso = bucket_start.isoformat()
        end_iso = bucket_end.isoformat()
        start_epoch_ms = int(bucket_start.timestamp() * 1000)
        end_epoch_ms = int(bucket_end.timestamp() * 1000)

        # 1. Aggregate runs metrics
        runs_query = """
            SELECT
                COUNT(*) AS total_runs,
                CAST(AVG(COALESCE(duration_ms, 0)) AS INTEGER) AS avg_duration_ms,
                MAX(COALESCE(peak_rss_bytes, 0)) AS peak_run_rss,
                SUM(COALESCE(cumulative_cpu_time_ms, 0)) AS total_cpu_time_ms
            FROM execution_runs
            WHERE started_at >= ? AND started_at < ?;
        """
        runs_rows = self._db.execute_read(runs_query, (start_iso, end_iso))
        runs_stats = runs_rows[0] if runs_rows else {}

        total_runs = int(runs_stats.get("total_runs") or 0)
        avg_run_duration_ms = int(runs_stats.get("avg_duration_ms") or 0)
        peak_run_rss = int(runs_stats.get("peak_run_rss") or 0)
        total_cpu_time_ms = int(runs_stats.get("total_cpu_time_ms") or 0)

        # 2. Aggregate raw samples metrics
        samples_query = """
            SELECT
                CAST(AVG(peldrun_rss_bytes + peldrun_children_rss_bytes) AS INTEGER) AS avg_rss_bytes,
                MAX(peldrun_rss_bytes + peldrun_children_rss_bytes) AS peak_sample_rss
            FROM resource_samples
            WHERE recorded_at_epoch_ms >= ? AND recorded_at_epoch_ms < ?;
        """
        samples_rows = self._db.execute_read(samples_query, (start_epoch_ms, end_epoch_ms))
        samples_stats = samples_rows[0] if samples_rows else {}

        avg_rss_bytes = int(samples_stats.get("avg_rss_bytes") or 0)
        peak_sample_rss = int(samples_stats.get("peak_sample_rss") or 0)
        overall_peak_rss = max(peak_run_rss, peak_sample_rss)

        # 3. Count orphan process events
        orphan_query = """
            SELECT COUNT(*) AS orphan_count
            FROM diagnostic_events
            WHERE event_type = ? AND recorded_at >= ? AND recorded_at < ?;
        """
        orphan_rows = self._db.execute_read(
            orphan_query,
            (DiagnosticEventType.ORPHAN_PROCESS.value, start_iso, end_iso),
        )
        orphan_count = int(orphan_rows[0]["orphan_count"]) if orphan_rows else 0

        # 4. Top consuming tools in this hour
        tool_summaries = self._repo.get_tool_consumption_summary(start_iso, end_iso)
        top_tools = [
            {
                "tool_name": t.tool_name,
                "calls": t.total_calls,
                "avg_duration_ms": t.avg_duration_ms,
                "peak_rss_bytes": t.peak_rss_bytes,
                "cpu_ms": t.cumulative_cpu_time_ms,
            }
            for t in tool_summaries[:5]
        ]
        top_tools_json = json.dumps(top_tools) if top_tools else None

        # Build model
        rollup = ResourceRollupModel(
            rollup_id=str(uuid.uuid4()),
            period_type=RollupPeriodType.HOURLY,
            period_start=start_iso,
            period_end=end_iso,
            total_runs_count=total_runs,
            avg_run_duration_ms=avg_run_duration_ms,
            avg_rss_bytes=avg_rss_bytes,
            peak_rss_bytes=overall_peak_rss,
            total_cpu_time_ms=total_cpu_time_ms,
            orphan_events_count=orphan_count,
            top_consuming_tools_json=top_tools_json,
        )

        # Remove existing rollup for this period to guarantee idempotence
        cleanup_query = "DELETE FROM resource_rollups WHERE period_type = 'HOURLY' AND period_start = ?;"
        self._db.execute_write(cleanup_query, (start_iso,))

        self._repo.insert_rollup(rollup)
        logger.debug("Computed hourly rollup for %s (runs=%d, avg_rss=%d)", start_iso, total_runs, avg_rss_bytes)
        return rollup

    def compute_daily_rollup(self, date_dt: datetime) -> Optional[ResourceRollupModel]:
        """Aggregate all hourly rollups into a unified daily rollup.

        Args:
            date_dt: Target date to roll up (normalized to midnight UTC).

        Returns:
            The generated ResourceRollupModel, or None if computation failed.
        """
        day_start = date_dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        start_iso = day_start.isoformat()
        end_iso = day_end.isoformat()

        hourly_rollups = self._repo.query_rollups(RollupPeriodType.HOURLY, start_iso, end_iso)

        if not hourly_rollups:
            # If no hourly rollups exist, fallback to direct hour computations
            current_hour = day_start
            while current_hour < day_end:
                self.compute_hourly_rollup(current_hour)
                current_hour += timedelta(hours=1)
            hourly_rollups = self._repo.query_rollups(RollupPeriodType.HOURLY, start_iso, end_iso)

        total_runs = sum(r.total_runs_count for r in hourly_rollups)
        total_cpu_ms = sum(r.total_cpu_time_ms for r in hourly_rollups)
        peak_rss = max((r.peak_rss_bytes for r in hourly_rollups), default=0)
        orphan_count = sum(r.orphan_events_count for r in hourly_rollups)

        active_hours = [r for r in hourly_rollups if r.total_runs_count > 0 or r.avg_rss_bytes > 0]
        avg_duration_ms = (
            int(sum(r.avg_run_duration_ms * r.total_runs_count for r in active_hours) / total_runs)
            if total_runs > 0 else 0
        )
        avg_rss_bytes = (
            int(sum(r.avg_rss_bytes for r in active_hours) / len(active_hours))
            if active_hours else 0
        )

        daily_rollup = ResourceRollupModel(
            rollup_id=str(uuid.uuid4()),
            period_type=RollupPeriodType.DAILY,
            period_start=start_iso,
            period_end=end_iso,
            total_runs_count=total_runs,
            avg_run_duration_ms=avg_duration_ms,
            avg_rss_bytes=avg_rss_bytes,
            peak_rss_bytes=peak_rss,
            total_cpu_time_ms=total_cpu_ms,
            orphan_events_count=orphan_count,
            top_consuming_tools_json=None,
        )

        # Idempotent replacement
        cleanup_query = "DELETE FROM resource_rollups WHERE period_type = 'DAILY' AND period_start = ?;"
        self._db.execute_write(cleanup_query, (start_iso,))

        self._repo.insert_rollup(daily_rollup)
        logger.info("Computed daily rollup for %s (runs=%d, peak_rss=%d)", start_iso, total_runs, peak_rss)
        return daily_rollup

    def compute_missing_hourly_rollups(self, max_hours_back: int = 48) -> int:
        """Scan historical hours and generate missing rollups up to the previous completed hour.

        Returns:
            Number of newly generated hourly rollups.
        """
        now_utc = datetime.now(timezone.utc)
        current_hour_start = now_utc.replace(minute=0, second=0, microsecond=0)
        oldest_hour_start = current_hour_start - timedelta(hours=max_hours_back)

        existing_rollups = self._repo.query_rollups(
            RollupPeriodType.HOURLY,
            oldest_hour_start.isoformat(),
            current_hour_start.isoformat(),
        )
        existing_starts = {r.period_start for r in existing_rollups}

        computed_count = 0
        scan_cursor = oldest_hour_start

        while scan_cursor < current_hour_start:
            cursor_iso = scan_cursor.isoformat()
            if cursor_iso not in existing_starts:
                self.compute_hourly_rollup(scan_cursor)
                computed_count += 1
            scan_cursor += timedelta(hours=1)

        if computed_count > 0:
            logger.info("Backfilled %d missing hourly rollups.", computed_count)

        return computed_count

    async def async_compute_hourly_rollup(self, start_dt: datetime) -> Optional[ResourceRollupModel]:
        """Asynchronously compute hourly rollup via worker thread."""
        return await asyncio.to_thread(self.compute_hourly_rollup, start_dt)

    async def async_compute_daily_rollup(self, date_dt: datetime) -> Optional[ResourceRollupModel]:
        """Asynchronously compute daily rollup via worker thread."""
        return await asyncio.to_thread(self.compute_daily_rollup, date_dt)

    async def async_compute_missing_hourly_rollups(self, max_hours_back: int = 48) -> int:
        """Asynchronously backfill missing hourly rollups via worker thread."""
        return await asyncio.to_thread(self.compute_missing_missing_hourly_rollups if hasattr(self, "compute_missing_missing_hourly_rollups") else self.compute_missing_hourly_rollups, max_hours_back)
