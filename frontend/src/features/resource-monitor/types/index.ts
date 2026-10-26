// frontend/src/features/resource-monitor/types/index.ts
/**
 * TypeScript domain types mirroring the PELDRUN Resource Monitor backend
 * Pydantic models. Keep in sync with:
 *   backend/omweb/resource_monitor/models.py
 */

// ============================================================================
// Enumerations (mirror backend str-Enums)
// ============================================================================

export type RunStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED'
  | 'UNEXPECTED_TERMINATION';

export type MeasurementConfidence = 'DIRECT' | 'PROPORTIONAL' | 'ESTIMATED';

export type ProcessType =
  | 'MAIN'
  | 'BROWSER_PLAYWRIGHT'
  | 'ISOLATED_PYTHON'
  | 'BASH_WORKER'
  | 'AUXILIARY';

export type SpanType = 'TOOL_CALL' | 'AGENT_STEP' | 'REASONING' | 'SANDBOX_EXEC';

export type SpanStatus = 'COMPLETED' | 'FAILED' | 'TIMED_OUT' | 'CANCELLED';

export type DiagnosticEventType =
  | 'ORPHAN_PROCESS'
  | 'MEMORY_CREEP'
  | 'CPU_SATURATION'
  | 'MEASUREMENT_GAP';

export type DiagnosticSeverity = 'INFO' | 'WARNING' | 'CRITICAL';

// ============================================================================
// Environment & Capabilities
// ============================================================================

export interface EnvironmentSnapshot {
  snapshot_id: string;
  session_id: string;
  recorded_at: string;
  os_name: string;
  os_release: string;
  architecture: string;
  is_docker: boolean;
  is_wsl: boolean;
  cpu_count_logical: number;
  cpu_count_physical: number | null;
  total_physical_ram_bytes: number;
  cgroup_memory_limit_bytes: number | null;
  cgroup_cpu_quota_us: number | null;
  capabilities_json: string; // JSON-encoded ResourceMonitorCapabilities
}

// ============================================================================
// Real-Time Snapshot
// ============================================================================

export interface HostMetrics {
  cpu_percent: number;
  ram_total_bytes: number;
  ram_used_bytes: number;
  ram_available_bytes: number;
  ram_percent: number;
  disk_total_bytes: number | null;
  disk_used_bytes: number | null;
  disk_percent: number | null;
}

export interface PeldrunProcessMetrics {
  pid: number;
  cpu_percent: number;
  main_rss_bytes: number;
  children_rss_bytes: number;
  total_rss_bytes: number;
  total_vms_bytes: number;
  children_count: number;
  threads_count: number;
}

export interface ChildProcessMetric {
  process_instance_id: string | null;
  pid: number;
  ppid: number;
  name: string;
  process_type: ProcessType;
  rss_bytes: number;
  cpu_percent: number;
  create_time: number;
  is_orphan: boolean;
  command_line: string | null;
}

export interface CurrentResourceSnapshot {
  recorded_at: string;
  recorded_at_epoch_ms: number;
  host: HostMetrics;
  peldrun: PeldrunProcessMetrics;
  active_runs_count: number;
  active_children: ChildProcessMetric[];
  has_active_diagnostics: boolean;
}

// ============================================================================
// Process & Runs
// ============================================================================

export interface ProcessInstance {
  process_instance_id: string;
  session_id: string;
  pid: number;
  ppid: number;
  name: string;
  command_line: string | null;
  process_type: ProcessType;
  created_at: string;
  terminated_at: string | null;
  exit_code: number | null;
  is_orphan: boolean;
}

export interface ExecutionRun {
  run_id: string;
  session_id: string;
  job_id: string | null;
  chat_id: string | null;
  agent_id: string | null;
  engine_type: string | null;
  status: RunStatus;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  peak_rss_bytes: number;
  cumulative_cpu_time_ms: number;
  attributed_children_count: number;
  measurement_confidence: MeasurementConfidence;
}

export interface ExecutionSpan {
  span_id: string;
  run_id: string;
  parent_span_id: string | null;
  span_type: SpanType;
  target_name: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  process_instance_id: string | null;
  rss_start_bytes: number | null;
  rss_end_bytes: number | null;
  rss_peak_bytes: number | null;
  cpu_time_consumed_ms: number | null;
  status: SpanStatus;
}

export interface ExecutionRunDetails {
  run: ExecutionRun;
  spans: ExecutionSpan[];
}

export interface ExecutionRunTimeline extends ExecutionRunDetails {
  timeseries: TimeseriesPoint[];
}

// ============================================================================
// Timeseries
// ============================================================================

export interface TimeseriesPoint {
  timestamp_epoch_ms: number;
  host_cpu_percent: number;
  host_ram_used_bytes: number;
  peldrun_rss_bytes: number;
  peldrun_children_rss_bytes: number;
  peldrun_total_rss_bytes: number;
  active_runs_count: number;
}

export interface TimeseriesResponse {
  from_time: string;
  to_time: string;
  resolution_seconds: number;
  points: TimeseriesPoint[];
}

// ============================================================================
// Tool Consumption
// ============================================================================

export interface ToolConsumptionSummary {
  tool_name: string;
  total_calls: number;
  avg_duration_ms: number;
  avg_rss_bytes: number;
  peak_rss_bytes: number;
  cumulative_cpu_time_ms: number;
}

// ============================================================================
// Diagnostics
// ============================================================================

export interface DiagnosticEvent {
  event_id: string;
  session_id: string;
  run_id: string | null;
  recorded_at: string;
  event_type: DiagnosticEventType;
  severity: DiagnosticSeverity;
  message: string;
  details_json: string | null;
  resolved_at?: string | null;
}

// ============================================================================
// Actions
// ============================================================================

export interface TerminateOrphanRequest {
  process_instance_id?: string | null;
  pid: number;
  force?: boolean;
}

export interface TerminateOrphanResponse {
  pid: number;
  terminated: boolean;
  message: string;
}

// ============================================================================
// Settings
// ============================================================================

export interface MonitorSettings {
  sampling_idle_interval_sec: number;
  sampling_active_interval_sec: number;
  retention_days: number;
  orphan_grace_period_sec: number;
  cpu_throttle_threshold_percent: number;
}

export interface MonitorSettingsUpdate {
  sampling_rate_idle_sec?: number;
  sampling_rate_active_sec?: number;
  retention_days?: number;
  orphan_grace_period_sec?: number;
  cpu_throttle_threshold_percent?: number;
}

// ============================================================================
// Query Parameters
// ============================================================================

export interface RunsQueryParams {
  limit?: number;
  offset?: number;
  status?: RunStatus;
  agent_id?: string;
}

export interface TimeseriesQueryParams {
  from_epoch_ms?: number;
  to_epoch_ms?: number;
  resolution_sec?: number;
}

export type TimeseriesRange = '1h' | '6h' | '24h' | '7d' | '30d';
