// frontend/src/features/resource-monitor/hooks/useResourceHistory.ts
/**
 * Hook for querying timeseries telemetry over a selectable range.
 * Automatically derives resolution buckets and refetches when range changes.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { resourceMonitorClient } from '../api/resource-monitor-client';
import type { TimeseriesPoint, TimeseriesRange } from '../types';

interface UseResourceHistoryOptions {
  range?: TimeseriesRange;
  enabled?: boolean;
  /** Auto-refresh interval in ms. Default 10s. Set 0 to disable. */
  refreshIntervalMs?: number;
}

interface UseResourceHistoryResult {
  points: TimeseriesPoint[];
  fromTime: string | null;
  toTime: string | null;
  resolutionSeconds: number;
  isLoading: boolean;
  error: Error | null;
  setRange: (range: TimeseriesRange) => void;
  range: TimeseriesRange;
  refresh: () => Promise<void>;
}

// Bucket sizing: aim for ~120–300 points per range
const RANGE_CONFIG: Record<
  TimeseriesRange,
  { durationMs: number; resolutionSec: number }
> = {
  '1h': { durationMs: 60 * 60 * 1000, resolutionSec: 10 },
  '6h': { durationMs: 6 * 60 * 60 * 1000, resolutionSec: 30 },
  '24h': { durationMs: 24 * 60 * 60 * 1000, resolutionSec: 60 },
  '7d': { durationMs: 7 * 24 * 60 * 60 * 1000, resolutionSec: 600 },
  '30d': { durationMs: 30 * 24 * 60 * 60 * 1000, resolutionSec: 3600 },
};

export function useResourceHistory(
  options: UseResourceHistoryOptions = {},
): UseResourceHistoryResult {
  const { range: initialRange = '1h', enabled = true, refreshIntervalMs = 10_000 } = options;

  const [range, setRange] = useState<TimeseriesRange>(initialRange);
  const [points, setPoints] = useState<TimeseriesPoint[]>([]);
  const [fromTime, setFromTime] = useState<string | null>(null);
  const [toTime, setToTime] = useState<string | null>(null);
  const [resolutionSeconds, setResolutionSeconds] = useState<number>(
    RANGE_CONFIG[initialRange].resolutionSec,
  );
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<Error | null>(null);

  const config = useMemo(() => RANGE_CONFIG[range], [range]);

  const fetchData = useCallback(async () => {
    if (!enabled) return;
    setIsLoading(true);
    try {
      const to = Date.now();
      const from = to - config.durationMs;
      const result = await resourceMonitorClient.queryTimeseries({
        from_epoch_ms: from,
        to_epoch_ms: to,
        resolution_sec: config.resolutionSec,
      });
      setPoints(result.points);
      setFromTime(result.from_time);
      setToTime(result.to_time);
      setResolutionSeconds(result.resolution_seconds);
      setError(null);
    } catch (err) {
      setError(err as Error);
    } finally {
      setIsLoading(false);
    }
  }, [enabled, config]);

  useEffect(() => {
    void fetchData();
    if (!enabled || refreshIntervalMs <= 0) return;
    const id = setInterval(fetchData, refreshIntervalMs);
    return () => clearInterval(id);
  }, [fetchData, enabled, refreshIntervalMs]);

  return {
    points,
    fromTime,
    toTime,
    resolutionSeconds,
    isLoading,
    error,
    range,
    setRange,
    refresh: fetchData,
  };
}
