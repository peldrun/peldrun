# backend/omweb/resource_monitor/config.py
"""Runtime configuration and tunable thresholds for PELDRUN Resource Monitor.

Provides strongly typed configuration models with environment variable overrides
for sampling intervals, buffer sizes, retention windows, and diagnostic heuristics.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class ResourceMonitorConfig(BaseModel):
    """Configuration settings governing the resource monitoring subsystem."""
    model_config = ConfigDict(from_attributes=True, validate_assignment=True)

    # Storage and database configuration
    db_path: Path = Field(
        default_factory=lambda: Path(
            os.environ.get(
                "PELDRUN_MONITOR_DB_PATH",
                str(Path("backend/storage/peldrun_monitor.db").resolve())
            )
        ),
        description="Filesystem path to the dedicated SQLite monitor database"
    )
    sqlite_busy_timeout_ms: int = Field(
        default=5000,
        description="SQLite busy timeout in milliseconds to prevent database locks"
    )
    batch_flush_max_samples: int = Field(
        default=50,
        ge=5,
        le=500,
        description="Maximum queued samples in memory before triggering an immediate flush"
    )
    batch_flush_interval_sec: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Interval in seconds to flush queued samples to SQLite"
    )

    # Adaptive sampling scheduler
    sampling_idle_interval_sec: float = Field(
        default=float(os.environ.get("PELDRUN_MONITOR_IDLE_SEC", "5.0")),
        ge=1.0,
        le=60.0,
        description="Sampling interval when no execution runs are active"
    )
    sampling_active_interval_sec: float = Field(
        default=float(os.environ.get("PELDRUN_MONITOR_ACTIVE_SEC", "1.0")),
        ge=0.5,
        le=10.0,
        description="Sampling interval during active execution runs"
    )
    sampling_throttled_interval_sec: float = Field(
        default=3.0,
        ge=1.0,
        le=15.0,
        description="Relaxed sampling interval when host CPU saturation threshold is breached"
    )
    cpu_throttle_threshold_percent: float = Field(
        default=85.0,
        ge=50.0,
        le=99.0,
        description="Host CPU usage percentage threshold triggering throttled sampling"
    )

    # Diagnostic heuristics and orphan detection
    orphan_grace_period_sec: float = Field(
        default=float(os.environ.get("PELDRUN_MONITOR_ORPHAN_GRACE_SEC", "30.0")),
        ge=5.0,
        le=300.0,
        description="Grace period in seconds after run completion before an unclosed child process is declared orphan"
    )
    leak_baseline_run_window: int = Field(
        default=5,
        ge=3,
        le=20,
        description="Number of consecutive runs evaluated to detect linear baseline memory creep"
    )
    leak_rss_delta_threshold_bytes: int = Field(
        default=50 * 1024 * 1024,  # 50 MB
        description="Uncollected RSS increase threshold across window runs triggering leak suspicion"
    )

    # Data retention and pruning policies
    raw_samples_retention_days: int = Field(
        default=int(os.environ.get("PELDRUN_MONITOR_RETENTION_DAYS", "14")),
        ge=1,
        le=90,
        description="Number of days to retain high-frequency raw timeseries samples"
    )
    hourly_rollups_retention_days: int = Field(
        default=90,
        ge=14,
        le=365,
        description="Number of days to retain hourly aggregated rollups"
    )
    daily_rollups_retention_days: int = Field(
        default=365,
        ge=30,
        le=1825,
        description="Number of days to retain daily and monthly rollups"
    )
    retention_prune_interval_hours: float = Field(
        default=24.0,
        ge=1.0,
        le=168.0,
        description="Frequency in hours for the automated retention pruning task"
    )

    # Subprocess execution patterns and names
    recognized_child_names: list[str] = Field(
        default_factory=lambda: [
            "node",
            "python",
            "python3",
            "chrome",
            "chromium",
            "msedge",
            "playwright",
            "bash",
            "sh",
            "cmd.exe",
            "powershell.exe",
        ],
        description="Process executable names monitored as potential child instances"
    )


def get_default_config() -> ResourceMonitorConfig:
    """Return default monitor configuration instance."""
    return ResourceMonitorConfig()
