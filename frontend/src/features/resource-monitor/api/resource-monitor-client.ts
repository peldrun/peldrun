// frontend/src/features/resource-monitor/api/resource-monitor-client.ts
/**
 * Typed fetch client for the PELDRUN Resource Monitor REST API.
 * All responses are runtime-validated with Zod schemas so that contract
 * violations surface as clear errors instead of silent UI corruption.
 */

import { z } from 'zod';

import type {
  CurrentResourceSnapshot,
  DiagnosticEvent,
  DiagnosticEventType,
  DiagnosticSeverity,
  EnvironmentSnapshot,
  ExecutionRun,
  ExecutionRunDetails,
  ExecutionRunTimeline,
  MonitorSettings,
  MonitorSettingsUpdate,
  ProcessInstance,
  RunsQueryParams,
  TerminateOrphanRequest,
  TerminateOrphanResponse,
  TimeseriesQueryParams,
  TimeseriesResponse,
  ToolConsumptionSummary,
} from '../types';

import {
  currentResourceSnapshotSchema,
  diagnosticEventSchema,
  environmentSnapshotSchema,
  executionRunDetailsSchema,
  executionRunSchema,
  executionRunTimelineSchema,
  monitorSettingsSchema,
  processInstanceSchema,
  terminateOrphanResponseSchema,
  timeseriesResponseSchema,
  toolConsumptionSummarySchema,
} from './schemas';

// ---------------------------------------------------------------------------
// Base configuration
// ---------------------------------------------------------------------------

const API_PREFIX = '/api/resource-monitor';
const DEFAULT_TIMEOUT_MS = 15_000;

/**
 * Structured error thrown by every client method. The `detail` payload
 * (usually the FastAPI `{ detail: ... }` body) is preserved for logging.
 */
export class ResourceMonitorApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly endpoint: string,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = 'ResourceMonitorApiError';
  }
}

// ---------------------------------------------------------------------------
// Core request helper
// ---------------------------------------------------------------------------

async function request<T>(
  endpoint: string,
  init: RequestInit = {},
  schema: z.ZodType<T>,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  // Build headers robustly (init.headers may be Headers, array, or record)
  const headers = new Headers(init.headers);
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  if (init.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  try {
    const response = await fetch(`${API_PREFIX}${endpoint}`, {
      ...init,
      signal: controller.signal,
      headers,
    });

    if (!response.ok) {
      let detail: unknown;
      const contentType = response.headers.get('Content-Type') ?? '';
      if (contentType.includes('application/json')) {
        detail = await response.json().catch(() => undefined);
      } else {
        detail = await response.text().catch(() => undefined);
      }
      throw new ResourceMonitorApiError(
        `Request to ${endpoint} failed with status ${response.status}`,
        response.status,
        endpoint,
        detail,
      );
    }

    const json: unknown = await response.json();
    const parsed = schema.safeParse(json);
    if (!parsed.success) {
      throw new ResourceMonitorApiError(
        `Response validation failed for ${endpoint}`,
        response.status,
        endpoint,
        parsed.error.format(),
      );
    }
    return parsed.data;
  } catch (error) {
    if (error instanceof ResourceMonitorApiError) throw error;
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ResourceMonitorApiError(
        `Request to ${endpoint} timed out after ${timeoutMs}ms`,
        0,
        endpoint,
      );
    }
    throw new ResourceMonitorApiError(
      `Network error on ${endpoint}: ${(error as Error).message}`,
      0,
      endpoint,
      error,
    );
  } finally {
    clearTimeout(timeout);
  }
}

// ---------------------------------------------------------------------------
// Query-string helper
// ---------------------------------------------------------------------------

function toQuery(params: Record<string, unknown> | undefined): string {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

// ---------------------------------------------------------------------------
// Public client
// ---------------------------------------------------------------------------

export const resourceMonitorClient = {
  /** GET /environment */
  async getEnvironment(): Promise<EnvironmentSnapshot> {
    return request('/environment', { method: 'GET' }, environmentSnapshotSchema);
  },

  /** GET /current */
  async getCurrentSnapshot(): Promise<CurrentResourceSnapshot> {
    return request('/current', { method: 'GET' }, currentResourceSnapshotSchema);
  },

  /** GET /processes */
  async listProcesses(includeTerminated = false): Promise<ProcessInstance[]> {
    return request(
      `/processes${toQuery({ include_terminated: includeTerminated })}`,
      { method: 'GET' },
      z.array(processInstanceSchema),
    );
  },

  /** GET /runs */
  async listRuns(params: RunsQueryParams = {}): Promise<ExecutionRun[]> {
    return request(
      `/runs${toQuery(params as Record<string, unknown>)}`,
      { method: 'GET' },
      z.array(executionRunSchema),
    );
  },

  /** GET /runs/{run_id} */
  async getRunDetails(runId: string): Promise<ExecutionRunDetails> {
    return request(
      `/runs/${encodeURIComponent(runId)}`,
      { method: 'GET' },
      executionRunDetailsSchema,
    );
  },

  /** GET /runs/{run_id}/timeline */
  async getRunTimeline(runId: string): Promise<ExecutionRunTimeline> {
    return request(
      `/runs/${encodeURIComponent(runId)}/timeline`,
      { method: 'GET' },
      executionRunTimelineSchema,
    );
  },

  /** GET /timeseries */
  async queryTimeseries(
    params: TimeseriesQueryParams = {},
  ): Promise<TimeseriesResponse> {
    return request(
      `/timeseries${toQuery(params as Record<string, unknown>)}`,
      { method: 'GET' },
      timeseriesResponseSchema,
    );
  },

  /** GET /tools/summary */
  async getToolConsumptionSummary(
    fromTime?: string,
    toTime?: string,
  ): Promise<ToolConsumptionSummary[]> {
    return request(
      `/tools/summary${toQuery({ from_time: fromTime, to_time: toTime })}`,
      { method: 'GET' },
      z.array(toolConsumptionSummarySchema),
    );
  },

  /** GET /diagnostics */
  async listDiagnosticEvents(
    filters: {
      severity?: DiagnosticSeverity;
      event_type?: DiagnosticEventType;
      limit?: number;
    } = {},
  ): Promise<DiagnosticEvent[]> {
    return request(
      `/diagnostics${toQuery(filters as Record<string, unknown>)}`,
      { method: 'GET' },
      z.array(diagnosticEventSchema),
    );
  },

  /** POST /actions/terminate-orphan */
  async terminateOrphan(
    payload: TerminateOrphanRequest,
  ): Promise<TerminateOrphanResponse> {
    return request(
      '/actions/terminate-orphan',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      terminateOrphanResponseSchema,
    );
  },

  /** GET /settings */
  async getSettings(): Promise<MonitorSettings> {
    return request('/settings', { method: 'GET' }, monitorSettingsSchema);
  },

  /** PATCH /settings */
  async updateSettings(updates: MonitorSettingsUpdate): Promise<MonitorSettings> {
    return request(
      '/settings',
      {
        method: 'PATCH',
        body: JSON.stringify(updates),
      },
      monitorSettingsSchema,
    );
  },

  /** GET /export — returns a Blob for direct download */
  async exportData(params: {
    format: 'json' | 'csv';
    from_time?: string;
    to_time?: string;
  }): Promise<Blob> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
    try {
      const response = await fetch(
        `${API_PREFIX}/export${toQuery(params as Record<string, unknown>)}`,
        { method: 'GET', signal: controller.signal },
      );
      if (!response.ok) {
        throw new ResourceMonitorApiError(
          `Export failed with status ${response.status}`,
          response.status,
          '/export',
        );
      }
      return await response.blob();
    } finally {
      clearTimeout(timeout);
    }
  },
};
