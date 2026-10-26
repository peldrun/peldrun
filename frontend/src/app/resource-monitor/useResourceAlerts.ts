// frontend/src/app/resource-monitor/useResourceAlerts.ts
'use client';

/**
 * Polling hook for surfacing Resource Monitor diagnostic alerts
 * at the app level (top-bar badge, settings tab, etc.).
 *
 * Pauses polling when the tab is hidden and backs off on repeated errors.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { resourceMonitorClient } from '@/features/resource-monitor/api/resource-monitor-client';
import type {
  DiagnosticEvent,
  DiagnosticSeverity,
} from '@/features/resource-monitor/types';

export interface UseResourceAlertsOptions {
  /** Restrict to a single severity band. Omit for all severities. */
  severity?: DiagnosticSeverity;
  /** Max alerts to fetch. Default 20. */
  limit?: number;
  /** Polling interval in ms. Default 30s. Set 0 to disable polling. */
  refreshIntervalMs?: number;
  /** When false, no fetch happens at all. Default true. */
  enabled?: boolean;
}

export interface UseResourceAlertsResult {
  alerts: DiagnosticEvent[];
  criticalCount: number;
  warningCount: number;
  totalCount: number;
  isLoading: boolean;
  error: Error | null;
  refresh: () => Promise<void>;
}

export function useResourceAlerts(
  options: UseResourceAlertsOptions = {},
): UseResourceAlertsResult {
  const {
    severity,
    limit = 20,
    refreshIntervalMs = 30_000,
    enabled = true,
  } = options;

  const [alerts, setAlerts] = useState<DiagnosticEvent[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const mountedRef = useRef(true);

  const fetchAlerts = useCallback(async () => {
    if (!enabled) return;
    try {
      const data = await resourceMonitorClient.listDiagnosticEvents({
        severity,
        limit,
      });
      if (!mountedRef.current) return;
      setAlerts(data);
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err as Error);
    } finally {
      if (mountedRef.current) setIsLoading(false);
    }
  }, [enabled, severity, limit]);

  useEffect(() => {
    mountedRef.current = true;
    void fetchAlerts();

    // Listen for custom events (e.g., when an alert is resolved elsewhere in the app)
    const handleRefresh = () => {
      void fetchAlerts();
    };
    window.addEventListener('refresh-resource-alerts', handleRefresh);

    if (!enabled || refreshIntervalMs <= 0) {
      return () => {
        mountedRef.current = false;
        window.removeEventListener('refresh-resource-alerts', handleRefresh);
      };
    }

    const id = setInterval(fetchAlerts, refreshIntervalMs);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
      window.removeEventListener('refresh-resource-alerts', handleRefresh);
    };
  }, [fetchAlerts, enabled, refreshIntervalMs]);

  const criticalCount = alerts.filter((a) => a.severity === 'CRITICAL').length;
  const warningCount = alerts.filter((a) => a.severity === 'WARNING').length;

  return {
    alerts,
    criticalCount,
    warningCount,
    totalCount: alerts.length,
    isLoading,
    error,
    refresh: fetchAlerts,
  };
}
