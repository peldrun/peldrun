# backend/omweb/resource_monitor/models.py
"""Domain models, enums, and validation schemas for PELDRUN Resource Monitor.

This module defines all enumeration types, database entity models, timeseries
sample definitions, and API data transfer objects (DTOs) used across the
resource monitoring subsystem.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


# ============================================================================
# Enumerations
# ============================================================================

class RunStatus(str, Enum):
    """Lifecycle status states for execution runs."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNEXPECTED_TERMINATION = "UNEXPECTED_TERMINATION"


class MeasurementConfidence(str, Enum):
    """Integrity and attribution confidence levels for recorded metrics.

    DIRECT: 100% verified attribution (e.g. dedicated subprocess PID).
    PROPORTIONAL: Calculated attribution under concurrent shared-process load.
    ESTIMATED: Heuristic approximation when exact isolation is not viable.
    """
    DIRECT = "DIRECT"
    PROPORTIONAL = "PROPORTIONAL"
    ESTIMATED = "ESTIMATED"


class ProcessType(str, Enum):
    """Categorization for monitored process instances."""
    MAIN = "MAIN"
    BROWSER_PLAYWRIGHT = "BROWSER_PLAYWRIGHT"
    ISOLATED_PYTHON = "ISOLATED_PYTHON"
    BASH_WORKER = "BASH_WORKER"
    AUXILIARY = "AUXILIARY"


class SpanType(str, Enum):
    """Granular execution span classifications."""
    TOOL_CALL = "TOOL_CALL"
    AGENT_STEP = "AGENT_STEP"
    REASONING = "REASONING"
    SANDBOX_EXEC = "SANDBOX_EXEC"


class SpanStatus(str, Enum):
    """Final outcome status for execution spans."""
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class DiagnosticEventType(str, Enum):
    """Categories of diagnostic alarms and anomalies."""
    ORPHAN_PROCESS = "ORPHAN_PROCESS"
    MEMORY_CREEP = "MEMORY_CREEP"
    CPU_SATURATION = "CPU_SATURATION"
    MEASUREMENT_GAP = "MEASUREMENT_GAP"


class DiagnosticSeverity(str, Enum):
    """Severity levels for diagnostic events."""
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class RollupPeriodType(str, Enum):
    """Aggregation period buckets for historical rollups."""
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


# ============================================================================
# Core Database Entity Schemas
# ============================================================================

class MonitorSessionModel(BaseModel):
    """Represents an application runtime lifecycle session."""
    model_config = ConfigDict(from_attributes=True)

    session_id: str
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ended_at: Optional[str] = None
    app_version: str = "1.0.0"
    python_version: str
    node_version: Optional[str] = None
    pid: int
    clean_exit: bool = False


class EnvironmentSnapshotModel(BaseModel):
    """Hardware specifications, container limits, and OS profile."""
    model_config = ConfigDict(from_attributes=True)

    snapshot_id: str
    session_id: str
    recorded_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    os_name: str
    os_release: str
    architecture: str
    is_docker: bool = False
    is_wsl: bool = False
    cpu_count_logical: int
    cpu_count_physical: Optional[int] = None
    total_physical_ram_bytes: int
    cgroup_memory_limit_bytes: Optional[int] = None
    cgroup_cpu_quota_us: Optional[int] = None
    capabilities_json: str = "{}"


class ExecutionRunModel(BaseModel):
    """Correlated execution run metrics and consumption bounds."""
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    session_id: str
    job_id: Optional[str] = None
    chat_id: Optional[str] = None
    agent_id: Optional[str] = None
    engine_type: Optional[str] = None
    status: RunStatus = RunStatus.PENDING
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = None
    peak_rss_bytes: int = 0
    cumulative_cpu_time_ms: int = 0
    attributed_children_count: int = 0
    measurement_confidence: MeasurementConfidence = MeasurementConfidence.DIRECT


class ProcessInstanceModel(BaseModel):
    """Tracked operating system process instance."""
    model_config = ConfigDict(from_attributes=True)

    process_instance_id: str
    session_id: str
    pid: int
    ppid: int
    name: str
    command_line: Optional[str] = None
    process_type: ProcessType = ProcessType.AUXILIARY
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    terminated_at: Optional[str] = None
    exit_code: Optional[int] = None
    is_orphan: bool = False


class ResourceSampleModel(BaseModel):
    """High-frequency host and process tree metric measurement."""
    model_config = ConfigDict(from_attributes=True)

    sample_id: Optional[int] = None
    session_id: str
    recorded_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    recorded_at_epoch_ms: int
    host_cpu_percent: float
    host_ram_used_bytes: int
    host_ram_available_bytes: int
    peldrun_rss_bytes: int
    peldrun_vms_bytes: int
    peldrun_cpu_percent: float
    peldrun_children_rss_bytes: int = 0
    active_runs_count: int = 0


class ExecutionSpanModel(BaseModel):
    """Granular execution span tracking consumption of individual tools or steps."""
    model_config = ConfigDict(from_attributes=True)

    span_id: str
    run_id: str
    parent_span_id: Optional[str] = None
    span_type: SpanType = SpanType.TOOL_CALL
    target_name: str
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = None
    process_instance_id: Optional[str] = None
    rss_start_bytes: Optional[int] = None
    rss_end_bytes: Optional[int] = None
    rss_peak_bytes: Optional[int] = None
    cpu_time_consumed_ms: Optional[int] = None
    status: SpanStatus = SpanStatus.COMPLETED


class DiagnosticEventModel(BaseModel):
    """Structured diagnostic anomaly, leak suspicion, or orphan notification."""
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    session_id: str
    run_id: Optional[str] = None
    recorded_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_type: DiagnosticEventType
    severity: DiagnosticSeverity
    message: str
    details_json: Optional[str] = None
    resolved_at: Optional[str] = Field(
        default=None,
        description="Timestamp when the underlying condition was resolved; None means still active.",
    )

    @property
    def is_active(self) -> bool:
        """True while the underlying anomaly has not been resolved."""
        return self.resolved_at is None


class ResourceRollupModel(BaseModel):
    """Aggregated historical summary across hour, day, week, or month."""
    model_config = ConfigDict(from_attributes=True)

    rollup_id: str
    period_type: RollupPeriodType
    period_start: str
    period_end: str
    total_runs_count: int
    avg_run_duration_ms: int
    avg_rss_bytes: int
    peak_rss_bytes: int
    total_cpu_time_ms: int
    orphan_events_count: int = 0
    top_consuming_tools_json: Optional[str] = None


class MonitorSettingModel(BaseModel):
    """Key-value persistence model for monitor configuration."""
    model_config = ConfigDict(from_attributes=True)

    setting_key: str
    setting_value: str
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ============================================================================
# API Request / Response DTOs
# ============================================================================

class HostMetrics(BaseModel):
    """Real-time host hardware metrics."""
    cpu_percent: float
    ram_total_bytes: int
    ram_used_bytes: int
    ram_available_bytes: int
    ram_percent: float
    disk_total_bytes: Optional[int] = None
    disk_used_bytes: Optional[int] = None
    disk_percent: Optional[float] = None


class PeldrunProcessMetrics(BaseModel):
    """Real-time metrics for PELDRUN main process and its subprocess tree."""
    pid: int
    cpu_percent: float
    main_rss_bytes: int
    children_rss_bytes: int
    total_rss_bytes: int
    total_vms_bytes: int
    children_count: int
    threads_count: int


class ChildProcessMetric(BaseModel):
    """Itemized metric for a single child process."""
    process_instance_id: Optional[str] = None
    pid: int
    ppid: int
    name: str
    process_type: ProcessType
    rss_bytes: int
    cpu_percent: float
    create_time: float
    is_orphan: bool = False
    command_line: Optional[str] = None


class CurrentResourceSnapshot(BaseModel):
    """Unified payload for live monitoring dashboard."""
    recorded_at: str
    recorded_at_epoch_ms: int
    host: HostMetrics
    peldrun: PeldrunProcessMetrics
    active_runs_count: int
    active_children: List[ChildProcessMetric] = Field(default_factory=list)
    has_active_diagnostics: bool = False


class TimeseriesPoint(BaseModel):
    """Individual timeseries data point for charting."""
    timestamp_epoch_ms: int
    host_cpu_percent: float
    host_ram_used_bytes: int
    peldrun_rss_bytes: int
    peldrun_children_rss_bytes: int
    peldrun_total_rss_bytes: int
    active_runs_count: int


class TimeseriesResponse(BaseModel):
    """Timeseries query response payload."""
    from_time: str
    to_time: str
    resolution_seconds: int
    points: List[TimeseriesPoint]


class ToolConsumptionSummary(BaseModel):
    """Aggregated tool resource consumption summary."""
    tool_name: str
    total_calls: int
    avg_duration_ms: float
    avg_rss_bytes: int
    peak_rss_bytes: int
    cumulative_cpu_time_ms: int


class TerminateOrphanRequest(BaseModel):
    """Request payload to terminate an orphan process."""
    process_instance_id: Optional[str] = None
    pid: int
    force: bool = False


class TerminateOrphanResponse(BaseModel):
    """Response outcome after attempting to terminate an orphan process."""
    pid: int
    terminated: bool
    message: str


class MonitorSettingsUpdate(BaseModel):
    """Payload for updating resource monitor runtime settings."""
    sampling_rate_idle_sec: Optional[int] = Field(None, ge=1, le=60)
    sampling_rate_active_sec: Optional[int] = Field(None, ge=1, le=10)
    retention_days: Optional[int] = Field(None, ge=1, le=365)
    orphan_grace_period_sec: Optional[int] = Field(None, ge=5, le=300)
    cpu_throttle_threshold_percent: Optional[float] = Field(None, ge=50.0, le=99.0)
