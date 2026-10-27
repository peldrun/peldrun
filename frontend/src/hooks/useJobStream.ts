// frontend/src/hooks/useJobStream.ts

"use client";

import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import { Step } from "@/lib/types";

/**
 * Resilient SSE hook for streaming PELDRUN job events.
 *
 * Guarantees:
 *   - Automatic reconnection on transient network failures with
 *     exponential backoff and full jitter, capped at a documented
 *     maximum delay and maximum number of attempts.
 *   - Heartbeat detection: if no message is received within
 *     `heartbeatTimeoutMs`, the connection is treated as dead and
 *     re-established.
 *   - Transparent resume: on reconnect, the last seen sequence
 *     number is included as `?after_sequence=N` so that the backend
 *     can replay missed events without duplicates.
 *   - Terminal states (`final`, `complete`, `completed`, `done`,
 *     `error`, `cancelled`) permanently stop the stream.
 *   - Explicit `stopStream()` permanently stops the stream and
 *     disables further reconnection.
 *
 * All timing parameters are configurable via the second argument;
 * defaults are exported as module-level constants so operators can
 * override them at a single call site without touching the hook.
 */

/** Delay before the first reconnect attempt (milliseconds). */
export const DEFAULT_RECONNECT_INITIAL_DELAY_MS = 3_000;

/** Maximum delay between reconnect attempts (milliseconds). */
export const DEFAULT_RECONNECT_MAX_DELAY_MS = 30_000;

/** Maximum number of consecutive reconnect attempts before giving up. */
export const DEFAULT_RECONNECT_MAX_ATTEMPTS = 8;

/** Maximum silence (no events) before treating the stream as dead. */
export const DEFAULT_HEARTBEAT_TIMEOUT_MS = 15_000;

export interface JobStreamOptions {
  reconnectInitialDelayMs?: number;
  reconnectMaxDelayMs?: number;
  reconnectMaxAttempts?: number;
  heartbeatTimeoutMs?: number;
}

type ResolvedOptions = Required<JobStreamOptions>;

/**
 * Compute the next backoff delay using exponential growth with full
 * jitter. Full jitter (uniform random in [0, capped]) avoids the
 * thundering-herd effect when many clients reconnect at once after
 * a backend restart.
 */
function computeBackoffDelay(attempt: number, opts: ResolvedOptions): number {
  const exponential = opts.reconnectInitialDelayMs * Math.pow(2, attempt);
  const capped = Math.min(exponential, opts.reconnectMaxDelayMs);
  return Math.floor(Math.random() * capped);
}

export function useJobStream(jobId: string | null, options?: JobStreamOptions) {
  const resolved = useMemo<ResolvedOptions>(
    () => ({
      reconnectInitialDelayMs:
        options?.reconnectInitialDelayMs ?? DEFAULT_RECONNECT_INITIAL_DELAY_MS,
      reconnectMaxDelayMs:
        options?.reconnectMaxDelayMs ?? DEFAULT_RECONNECT_MAX_DELAY_MS,
      reconnectMaxAttempts:
        options?.reconnectMaxAttempts ?? DEFAULT_RECONNECT_MAX_ATTEMPTS,
      heartbeatTimeoutMs:
        options?.heartbeatTimeoutMs ?? DEFAULT_HEARTBEAT_TIMEOUT_MS,
    }),
    [
      options?.reconnectInitialDelayMs,
      options?.reconnectMaxDelayMs,
      options?.reconnectMaxAttempts,
      options?.heartbeatTimeoutMs,
    ]
  );

  const [steps, setSteps] = useState<Step[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const eventSourceRef = useRef<EventSource | null>(null);
  const lastSequenceRef = useRef<number>(0);
  const attemptRef = useRef<number>(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shouldReconnectRef = useRef<boolean>(true);

  const clearHeartbeat = useCallback(() => {
    if (heartbeatTimerRef.current !== null) {
      clearTimeout(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
  }, []);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const teardownConnection = useCallback(() => {
    clearHeartbeat();
    if (eventSourceRef.current) {
      // Detach handlers to avoid late callbacks after close().
      eventSourceRef.current.onmessage = null;
      eventSourceRef.current.onerror = null;
      eventSourceRef.current.onopen = null;
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
  }, [clearHeartbeat]);

  const stopStream = useCallback(() => {
    shouldReconnectRef.current = false;
    clearReconnectTimer();
    teardownConnection();
    setIsStreaming(false);
  }, [clearReconnectTimer, teardownConnection]);

  const connect = useCallback(() => {
    if (!jobId || !shouldReconnectRef.current) return;

    teardownConnection();

    // Resume from the last seen sequence number to avoid replaying
    // already-received events and to recover missed ones.
    const sseUrl = `/api/run/jobs/${encodeURIComponent(jobId)}/stream${
      lastSequenceRef.current > 0
        ? `?after_sequence=${lastSequenceRef.current}`
        : ""
    }`;

    const es = new EventSource(sseUrl);
    eventSourceRef.current = es;

    const scheduleReconnect = (reason: string) => {
      if (!shouldReconnectRef.current) return;

      if (attemptRef.current >= resolved.reconnectMaxAttempts) {
        setError(
          `Stream disconnected after ${resolved.reconnectMaxAttempts} ` +
            `reconnect attempts (${reason}).`
        );
        setIsStreaming(false);
        shouldReconnectRef.current = false;
        return;
      }

      const delay = computeBackoffDelay(attemptRef.current, resolved);
      attemptRef.current += 1;
      clearReconnectTimer();
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        if (shouldReconnectRef.current) {
          connect();
        }
      }, delay);
    };

    const resetHeartbeat = () => {
      clearHeartbeat();
      heartbeatTimerRef.current = setTimeout(() => {
        // No message within the heartbeat window: treat the
        // connection as dead and reconnect with backoff.
        teardownConnection();
        scheduleReconnect("heartbeat timeout");
      }, resolved.heartbeatTimeoutMs);
    };

    es.onopen = () => {
      // Connection established: reset the attempt counter so that a
      // stable connection does not exhaust the attempt budget.
      attemptRef.current = 0;
      setError(null);
      resetHeartbeat();
    };

    es.onmessage = (event) => {
      resetHeartbeat();
      try {
        if (!event.data) return;
        const payload = JSON.parse(event.data);

        // Ignore keepalive frames.
        if (payload.type === "ping" || payload.type === "chunk") return;

        // Monotonic sequence tracking for resume support.
        const currentSeq = payload.seq || payload.sequence || payload.data?.seq;
        if (typeof currentSeq === "number" && currentSeq > 0) {
          lastSequenceRef.current = Math.max(
            lastSequenceRef.current,
            currentSeq
          );
        }

        let contentStr = "";
        if (typeof payload.data === "string") {
          contentStr = payload.data;
        } else if (payload.data && typeof payload.data === "object") {
          contentStr =
            payload.data.thought ||
            payload.data.content ||
            payload.data.result ||
            payload.data.output ||
            JSON.stringify(payload.data, null, 2);
        } else {
          contentStr = JSON.stringify(payload, null, 2);
        }

        const stepType = (payload.type || "agent_step").toUpperCase();

        setSteps((prev) => {
          // Compute step_number inside the functional update to
          // avoid stale-closure issues under rapid concurrent
          // messages.
          const newStep: Step = {
            id:
              payload.id ||
              `step_${Date.now()}_${Math.random().toString(36).substring(2, 6)}`,
            step_number: payload.step || prev.length + 1,
            type: stepType,
            content: contentStr,
            timestamp: new Date().toISOString(),
            tool_name:
              payload.toolName ||
              payload.data?.tool_name ||
              payload.data?.tool,
            tool_args: payload.data?.arguments,
            attempt: payload.data?.attempt,
          };

          if (prev.some((s) => s.id === newStep.id)) {
            return prev;
          }
          return [...prev, newStep];
        });

        // Terminal lifecycle states permanently stop the stream.
        const terminalStates = [
          "final",
          "complete",
          "completed",
          "done",
          "error",
          "cancelled",
        ];
        if (
          terminalStates.includes(String(payload.type).toLowerCase()) ||
          terminalStates.includes(String(payload.status).toLowerCase())
        ) {
          shouldReconnectRef.current = false;
          clearReconnectTimer();
          teardownConnection();
          setIsStreaming(false);
        }
      } catch {
        // Non-JSON payload: append as a raw log line.
        setSteps((prev) => [
          ...prev,
          {
            id: `step_${Date.now()}`,
            step_number: prev.length + 1,
            type: "LOG",
            content: event.data,
            timestamp: new Date().toISOString(),
          },
        ]);
      }
    };

    es.onerror = () => {
      // The EventSource spec fires `onerror` on transient
      // disconnects too. Tear down and schedule a reconnect with
      // backoff.
      teardownConnection();
      scheduleReconnect("connection error");
    };
  }, [
    jobId,
    resolved,
    clearHeartbeat,
    clearReconnectTimer,
    teardownConnection,
  ]);

  useEffect(() => {
    shouldReconnectRef.current = true;
    attemptRef.current = 0;
    lastSequenceRef.current = 0;
    clearReconnectTimer();

    if (!jobId) {
      teardownConnection();
      setSteps([]);
      setIsStreaming(false);
      setError(null);
      return;
    }

    setSteps([]);
    setError(null);
    setIsStreaming(true);

    connect();

    return () => {
      shouldReconnectRef.current = false;
      clearReconnectTimer();
      teardownConnection();
    };
    // Intentionally depend only on jobId: `connect` and `resolved`
    // are stable enough for the lifetime of a single stream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  return { steps, isStreaming, error, stopStream };
}
