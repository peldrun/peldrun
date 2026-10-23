// frontend/src/features/resource-monitor/api/schemas.ts
/**
 * Zod runtime validation schemas for all Resource Monitor API responses.
 * These provide defense-in-depth against backend contract drift, ensuring the
 * frontend never renders garbage when a response shape changes unexpectedly.
 *
 * Keep every schema in sync with:
 *   backend/omweb/resource_monitor/models.py
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Primitives & constraints
// ---------------------------------------------------------------------------

const isoString = z.string().min(1);
const byteCount = z.number().int().nonnegative();
const percent = z.number().min(0).max(100);
const nonNegativeInt = z.number().int().nonnegative();
const positiveInt = z.number().int().positive();

// ---------------------------------------------------------------------------
// Enums (mirror backend str-Enums verbatim)
// ---------------------------------------------------------------------------

export const runStatusSchema = z.enum([
  'PENDING',
  'RUNNING',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'UNEXPECTED_TERMINATION',
]);

export const measurementConfidenceSchema = z.enum([
  'DIRECT',
  'PROPORTIONAL',
  'ESTIMATED',
]);

export const processTypeSchema = z.enum([
  'MAIN',
  'BROWSER_PLAYWRIGHT',
  'ISOLATED_PYTHON',
  'BASH_WORKER',
  'AUXILIARY',
]);

export const spanTypeSchema = z.enum([
  'TOOL_CALL',
  'AGENT_STEP',
  'REASONING',
  'SANDBOX_EXEC',
]);

export const spanStatusSchema = z.enum([
  'COMPLETED',
  'FAILED',
  'TIMED_OUT',
  'CANCELLED',
]);

export const diagnosticEventTypeSchema = z.enum([
  'ORPHAN_PROCESS',
  'MEMORY_CREEP',
  'CPU_SATURATION',
  'MEASUREMENT_GAP',
]);

export const diagnosticSeveritySchema = z.enum(['INFO', 'WARNING', 'CRITICAL']);

// ---------------------------------------------------------------------------
// Environment
// ---------------------------------------------------------------------------

export const environmentSnapshotSchema = z.object({
  snapshot_id: z.string(),
  session_id: z.string(),
  recorded_at: isoString,
  os_name: z.string(),
  os_release: z.string(),
  architecture: z.string(),
  is_docker: z.boolean(),
  is_wsl: z.boolean(),
  cpu_count_logical: positiveInt,
  cpu_count_physical: positiveInt.nullable(),
  total_physical_ram_bytes: byteCount,
  cgroup_memory_limit_bytes: byteCount.nullable(),
  cgroup_cpu_quota_us: z.number().int().nullable(),
  capabilities_json: z.string(),
});

// ---------------------------------------------------------------------------
// Real-time snapshot building blocks
// ---------------------------------------------------------------------------

export const hostMetricsSchema = z.object({
  cpu_percent: percent,
  ram_total_bytes: byteCount,
  ram_used_bytes: byteCount,
  ram_available_bytes: byteCount,
  ram_percent: percent,
  disk_total_bytes: byteCount.nullable(),
  disk_used_bytes: byteCount.nullable(),
  disk_percent: percent.nullable(),
});

export const peldrunProcessMetricsSchema = z.object({
  pid: positiveInt,
  cpu_percent: percent,
  main_rss_bytes: byteCount,
  children_rss_bytes: byteCount,
  total_rss_bytes: byteCount,
  total_vms_bytes: byteCount,
  children_count: nonNegativeInt,
  threads_count: nonNegativeInt,
});

export const childProcessMetricSchema = z.object({
  process_instance_id: z.string().nullable(),
  pid: positiveInt,
  ppid: positiveInt,
  name: z.string(),
  process_type: processTypeSchema,
  rss_bytes: byteCount,
  cpu_percent: percent,
  create_time: z.number(),
  is_orphan: z.boolean(),
  command_line: z.string().nullable(),
});

export const currentResourceSnapshotSchema = z.object({
  recorded_at: isoString,
  recorded_at_epoch_ms: z.number().int(),
  host: hostMetricsSchema,
  peldrun: peldrunProcessMetricsSchema,
  active_runs_count: nonNegativeInt,
  active_children: z.array(childProcessMetricSchema),
  has_active_diagnostics: z.boolean(),
});

// ---------------------------------------------------------------------------
// Processes
// ---------------------------------------------------------------------------

export const processInstanceSchema = z.object({
  process_instance_id: z.string(),
  session_id: z.string(),
  pid: positiveInt,
  ppid: positiveInt,
  name: z.string(),
  command_line: z.string().nullable(),
  process_type: processTypeSchema,
  created_at: isoString,
  terminated_at: isoString.nullable(),
  exit_code: z.number().int().nullable(),
  is_orphan: z.boolean(),
});

// ---------------------------------------------------------------------------
// Execution runs & spans
// ---------------------------------------------------------------------------

export const executionRunSchema = z.object({
  run_id: z.string(),
  session_id: z.string(),
  job_id: z.string().nullable(),
  chat_id: z.string().nullable(),
  agent_id: z.string().nullable(),
  engine_type: z.string().nullable(),
  status: runStatusSchema,
  started_at: isoString,
  ended_at: isoString.nullable(),
  duration_ms: z.number().int().nullable(),
  peak_rss_bytes: byteCount,
  cumulative_cpu_time_ms: nonNegativeInt,
  attributed_children_count: nonNegativeInt,
  measurement_confidence: measurementConfidenceSchema,
});

export const executionSpanSchema = z.object({
  span_id: z.string(),
  run_id: z.string(),
  parent_span_id: z.string().nullable(),
  span_type: spanTypeSchema,
  target_name: z.string(),
  started_at: isoString,
  ended_at: isoString.nullable(),
  duration_ms: z.number().int().nullable(),
  process_instance_id: z.string().nullable(),
  rss_start_bytes: byteCount.nullable(),
  rss_end_bytes: byteCount.nullable(),
  rss_peak_bytes: byteCount.nullable(),
  cpu_time_consumed_ms: z.number().int().nullable(),
  status: spanStatusSchema,
});

export const executionRunDetailsSchema = z.object({
  run: executionRunSchema,
  spans: z.array(executionSpanSchema),
});

// ---------------------------------------------------------------------------
// Timeseries
// ---------------------------------------------------------------------------

export const timeseriesPointSchema = z.object({
  timestamp_epoch_ms: z.number().int(),
  host_cpu_percent: percent,
  host_ram_used_bytes: byteCount,
  peldrun_rss_bytes: byteCount,
  peldrun_children_rss_bytes: byteCount,
  peldrun_total_rss_bytes: byteCount,
  active_runs_count: nonNegativeInt,
});

export const timeseriesResponseSchema = z.object({
  from_time: isoString,
  to_time: isoString,
  resolution_seconds: positiveInt,
  points: z.array(timeseriesPointSchema),
});

export const executionRunTimelineSchema = z.object({
  run: executionRunSchema,
  spans: z.array(executionSpanSchema),
  timeseries: z.array(timeseriesPointSchema),
});

// ---------------------------------------------------------------------------
// Tool consumption
// ---------------------------------------------------------------------------

export const toolConsumptionSummarySchema = z.object({
  tool_name: z.string(),
  total_calls: nonNegativeInt,
  avg_duration_ms: z.number().nonnegative(),
  avg_rss_bytes: byteCount,
  peak_rss_bytes: byteCount,
  cumulative_cpu_time_ms: nonNegativeInt,
});

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

export const diagnosticEventSchema = z.object({
  event_id: z.string(),
  session_id: z.string(),
  run_id: z.string().nullable(),
  recorded_at: isoString,
  event_type: diagnosticEventTypeSchema,
  severity: diagnosticSeveritySchema,
  message: z.string(),
  details_json: z.string().nullable(),
});

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

export const terminateOrphanResponseSchema = z.object({
  pid: positiveInt,
  terminated: z.boolean(),
  message: z.string(),
});

// ---------------------------------------------------------------------------
// Settings — matches GET /settings & PATCH /settings response shape
// (backend returns Dict from service.get_settings())
// ---------------------------------------------------------------------------

export const monitorSettingsSchema = z.object({
  sampling_idle_interval_sec: z.number().positive(),
  sampling_active_interval_sec: z.number().positive(),
  retention_days: positiveInt,
  orphan_grace_period_sec: z.number().positive(),
  cpu_throttle_threshold_percent: percent,
});

// ---------------------------------------------------------------------------
// Inferred TypeScript types (optional exports for component-level type-safety)
// ---------------------------------------------------------------------------

export type EnvironmentSnapshotZ = z.infer<typeof environmentSnapshotSchema>;
export type CurrentResourceSnapshotZ = z.infer<typeof currentResourceSnapshotSchema>;
export type ProcessInstanceZ = z.infer<typeof processInstanceSchema>;
export type ExecutionRunZ = z.infer<typeof executionRunSchema>;
export type ExecutionSpanZ = z.infer<typeof executionSpanSchema>;
export type TimeseriesResponseZ = z.infer<typeof timeseriesResponseSchema>;
export type ToolConsumptionSummaryZ = z.infer<typeof toolConsumptionSummarySchema>;
export type DiagnosticEventZ = z.infer<typeof diagnosticEventSchema>;
export type MonitorSettingsZ = z.infer<typeof monitorSettingsSchema>;
