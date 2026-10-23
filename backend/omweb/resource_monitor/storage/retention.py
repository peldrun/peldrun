# backend/omweb/resource_monitor/storage/retention.py
"""Automated data retention and pruning engine for PELDRUN Resource Monitor.

Enforces historical retention policies across high-resolution timeseries samples,
periodic rollups, and diagnostic logs to prevent unbounded SQLite file growth.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..config import ResourceMonitorConfig, get_default_config
from .database import MonitorDatabase

logger = logging.getLogger(__name__)


class RetentionManager:
    """Manages cleanup and compaction routines for monitor database tables."""

    def __init__(
        self,
        database: MonitorDatabase,
        config: Optional[ResourceMonitorConfig] = None,
    ) -> None:
        self._db = database
        self._config = config or get_default_config()

    def prune_raw_samples(self, retention_days: Optional[int] = None) -> int:
        """Delete high-frequency resource samples older than the retention threshold.

        Args:
            retention_days: Number of days to retain. Defaults to config setting.

        Returns:
            Number of deleted raw sample rows.
        """
        days = retention_days if retention_days is not None else self._config.raw_samples_retention_days
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_epoch_ms = int(cutoff_dt.timestamp() * 1000)

        query = "DELETE FROM resource_samples WHERE recorded_at_epoch_ms < ?;"
        deleted_count = self._db.execute_write(query, (cutoff_epoch_ms,))

        if deleted_count > 0:
            logger.info(
                "Pruned %d raw resource samples older than %d days (cutoff: %s).",
                deleted_count,
                days,
                cutoff_dt.isoformat(),
            )
        return deleted_count

    def prune_hourly_rollups(self, retention_days: Optional[int] = None) -> int:
        """Delete hourly rollups older than the configured threshold.

        Args:
            retention_days: Number of days to retain. Defaults to config setting.

        Returns:
            Number of deleted rollup records.
        """
        days = retention_days if retention_days is not None else self._config.hourly_rollups_retention_days
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff_dt.isoformat()

        query = "DELETE FROM resource_rollups WHERE period_type = 'HOURLY' AND period_end < ?;"
        deleted_count = self._db.execute_write(query, (cutoff_iso,))

        if deleted_count > 0:
            logger.info(
                "Pruned %d hourly rollups older than %d days.",
                deleted_count,
                days,
            )
        return deleted_count

    def prune_diagnostic_events(self, retention_days: Optional[int] = None) -> int:
        """Delete non-critical diagnostic events older than retention window.

        Args:
            retention_days: Number of days to retain. Defaults to config setting.

        Returns:
            Number of deleted diagnostic records.
        """
        days = retention_days if retention_days is not None else self._config.raw_samples_retention_days
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff_dt.isoformat()

        query = "DELETE FROM diagnostic_events WHERE severity = 'INFO' AND recorded_at < ?;"
        deleted_count = self._db.execute_write(query, (cutoff_iso,))

        if deleted_count > 0:
            logger.info("Pruned %d informational diagnostic events.", deleted_count)
        return deleted_count

    def prune_all(self, vacuum_if_deleted: bool = False) -> Dict[str, int]:
        """Execute complete retention cleanup across all eligible tables.

        Args:
            vacuum_if_deleted: When True and rows were deleted, executes database VACUUM.

        Returns:
            Dictionary containing deleted count tallies per entity type.
        """
        deleted_samples = self.prune_raw_samples()
        deleted_rollups = self.prune_hourly_rollups()
        deleted_diagnostics = self.prune_diagnostic_events()

        total_deleted = deleted_samples + deleted_rollups + deleted_diagnostics

        if vacuum_if_deleted and total_deleted > 5000:
            logger.info("Significant records deleted (%d total). Initiating database vacuum...", total_deleted)
            self._db.vacuum()

        return {
            "deleted_samples": deleted_samples,
            "deleted_rollups": deleted_rollups,
            "deleted_diagnostics": deleted_diagnostics,
            "total_deleted": total_deleted,
        }

    async def async_prune_all(self, vacuum_if_deleted: bool = False) -> Dict[str, int]:
        """Asynchronously execute retention pruning via worker thread."""
        return await asyncio.to_thread(self.prune_all, vacuum_if_deleted)
