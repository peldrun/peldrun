// frontend/src/features/resource-monitor/hooks/useCurrentResources.ts
/**
 * Polling hook for the live resource snapshot.
 * - Pauses polling when the tab is hidden (Page Visibility API).
 * - Backs off on consecutive errors.
 * - Returns a stable, deeply-typed CurrentResourceSnapshot.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { resourceMonitorClient, ResourceMonitorApiError } from '../api/resource-monitor-client';
import type { CurrentResourceSnapshot } from '../types';

interface UseCurrentResourcesOptions {
  /** Base polling interval in ms while tab is visible. Default 2000. */
  intervalMs?: number;
  /** When false, polling is disabled entirely. Default true. */
  enabled?: boolean;
}

interface UseCurrentResourcesResult {
  snapshot: CurrentResourceSnapshot | null;
  error: ResourceMonitorApiError | Error | null;
  isLoading: boolean;
  lastUpdated: number | null;
  refresh: () => Promise<void>;
}

export function useCurrentResources(
  options: UseCurrentResourcesOptions = {},
): UseCurrentResourcesResult {
  const { intervalMs = 2000, enabled = true } = options;

  const [snapshot, setSnapshot] = useState<CurrentResourceSnapshot | null>(null);
  const [error, setError] = useState<ResourceMonitorApiError | Error | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);

  const mountedRef = useRef(true);
  const errorStreakRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchOnce = useCallback(async () => {
    try {
      const data = await resourceMonitorClient.getCurrentSnapshot();
      if (!mountedRef.current) return;
      setSnapshot(data);
      setError(null);
      setLastUpdated(Date.now());
      errorStreakRef.current = 0;
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err as Error);
      errorStreakRef.current += 1;
    } finally {
      if (mountedRef.current) setIsLoading(false);
    }
  }, []);

  const scheduleNext = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (!enabled) return;

    // Back off up to 30s after repeated failures; pause entirely when hidden
    const hidden =
      typeof document !== 'undefined' && document.visibilityState === 'hidden';
    const backoff = Math.min(30_000, intervalMs * Math.pow(2, errorStreakRef.current));
    const delay = hidden ? 30_000 : backoff;

    timerRef.current = setTimeout(async () => {
      await fetchOnce();
      scheduleNext();
    }, delay);
  }, [enabled, intervalMs, fetchOnce]);

  useEffect(() => {
    mountedRef.current = true;
    if (!enabled) {
      setIsLoading(false);
      return;
    }

    fetchOnce().then(scheduleNext);

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        fetchOnce().then(scheduleNext);
      }
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  const refresh = useCallback(async () => {
    await fetchOnce();
  }, [fetchOnce]);

  return { snapshot, error, isLoading, lastUpdated, refresh };
}
