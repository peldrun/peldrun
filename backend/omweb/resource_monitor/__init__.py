# backend/omweb/resource_monitor/__init__.py
"""PELDRUN Resource Monitor Subsystem.

Provides internal telemetry, host metrics collection, process tree tracking,
hardware capacity discovery, and diagnostic analysis for PELDRUN AI executions.
"""

from __future__ import annotations

from .capabilities import (
    CapabilitiesProber,
    ResourceMonitorCapabilities,
    probe_capabilities,
)
from .config import (
    ResourceMonitorConfig,
    get_default_config,
)
from .environment import (
    EnvironmentInspector,
    capture_environment_snapshot,
)
from .models import (
    ChildProcessMetric,
    CurrentResourceSnapshot,
    DiagnosticEventModel,
    DiagnosticEventType,
    DiagnosticSeverity,
    EnvironmentSnapshotModel,
    ExecutionRunModel,
    ExecutionSpanModel,
    HostMetrics,
    MeasurementConfidence,
    MonitorSessionModel,
    MonitorSettingModel,
    MonitorSettingsUpdate,
    PeldrunProcessMetrics,
    ProcessInstanceModel,
    ProcessType,
    ResourceRollupModel,
    ResourceSampleModel,
    RollupPeriodType,
    RunStatus,
    SpanStatus,
    SpanType,
    TerminateOrphanRequest,
    TerminateOrphanResponse,
    TimeseriesPoint,
    TimeseriesResponse,
    ToolConsumptionSummary,
)

__all__ = [
    # Capabilities
    "CapabilitiesProber",
    "ResourceMonitorCapabilities",
    "probe_capabilities",
    # Config
    "ResourceMonitorConfig",
    "get_default_config",
    # Environment
    "EnvironmentInspector",
    "capture_environment_snapshot",
    # Models & Enums
    "ChildProcessMetric",
    "CurrentResourceSnapshot",
    "DiagnosticEventModel",
    "DiagnosticEventType",
    "DiagnosticSeverity",
    "EnvironmentSnapshotModel",
    "ExecutionRunModel",
    "ExecutionSpanModel",
    "HostMetrics",
    "MeasurementConfidence",
    "MonitorSessionModel",
    "MonitorSettingModel",
    "MonitorSettingsUpdate",
    "PeldrunProcessMetrics",
    "ProcessInstanceModel",
    "ProcessType",
    "ResourceRollupModel",
    "ResourceSampleModel",
    "RollupPeriodType",
    "RunStatus",
    "SpanStatus",
    "SpanType",
    "TerminateOrphanRequest",
    "TerminateOrphanResponse",
    "TimeseriesPoint",
    "TimeseriesResponse",
    "ToolConsumptionSummary",
]
