// frontend/src/features/resource-monitor/hooks/useProcessList.ts
/**
 * Hook for managing the tracked subprocess list and orphan-termination actions.
 * Encapsulates optimistic UI updates and safe error surfacing.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { resourceMonitorClient } from '../api/resource-monitor-client';
import type { ProcessInstance, TerminateOrphanResponse } from '../types';

interface UseProcessListOptions {
  includeTerminated?: boolean;
  enabled?: boolean;
  refreshIntervalMs?: number;
}

interface UseProcessListResult {
  processes: ProcessInstance[];
  orphans: ProcessInstance[];
  isLoading: boolean;
  error: Error | null;
  terminatingPid: number | null;
  refresh: () => Promise<void>;
  terminateOrphan: (pid: number, force?: boolean) => Promise<TerminateOrphanResponse>;
}

export function useProcessList(
  options: UseProcessListOptions = {},
): UseProcessListResult {
  const { includeTerminated = false, enabled = true, refreshIntervalMs = 5000 } = options;

  const [processes, setProcesses] = useState<ProcessInstance[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [terminatingPid, setTerminatingPid] = useState<number | null>(null);
  const mountedRef = useRef(true);

  const fetchProcesses = useCallback(async () => {
    if (!enabled) return;
    try {
      const data = await resourceMonitorClient.listProcesses(includeTerminated);
      if (!mountedRef.current) return;
      setProcesses(data);
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err as Error);
    } finally {
      if (mountedRef.current) setIsLoading(false);
    }
  }, [enabled, includeTerminated]);

  useEffect(() => {
    mountedRef.current = true;
    void fetchProcesses();
    if (!enabled || refreshIntervalMs <= 0) {
      return () => {
        mountedRef.current = false;
      };
    }
    const id = setInterval(fetchProcesses, refreshIntervalMs);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [fetchProcesses, enabled, refreshIntervalMs]);

  const terminateOrphan = useCallback(
    async (pid: number, force = false): Promise<TerminateOrphanResponse> => {
      setTerminatingPid(pid);
      try {
        const result = await resourceMonitorClient.terminateOrphan({ pid, force });
        // Optimistically remove terminated process from UI
        if (result.terminated && mountedRef.current) {
          setProcesses((prev) =>
            prev.map((p) =>
              p.pid === pid
                ? { ...p, is_orphan: false, terminated_at: new Date().toISOString() }
                : p,
            ),
          );
        }
        return result;
      } finally {
        if (mountedRef.current) setTerminatingPid(null);
      }
    },
    [],
  );

  const orphans = processes.filter((p) => p.is_orphan && p.terminated_at === null);

  return {
    processes,
    orphans,
    isLoading,
    error,
    terminatingPid,
    refresh: fetchProcesses,
    terminateOrphan,
  };
}
