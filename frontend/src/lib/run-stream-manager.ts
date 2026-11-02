// Path: frontend/src/lib/run-stream-manager.ts
"use client";

/**
 * Run Stream Manager — single owner of every active SSE connection.
 *
 * Replaces the three scattered EventSource paths that existed prior to
 * Phase D (`use-sse-stream.ts`, `useJobStream.ts`, `lib/sse.ts`). After
 * Phase F these three modules will be removed or converted to thin
 * compatibility shims over this manager.
 *
 * ## Responsibilities
 *
 *   1. Maintain exactly ONE EventSource per active jobId. Duplicate
 *      `connect()` calls are idempotent.
 *
 *   2. Listen to every named SSE event the backend may emit
 *      (`step_start`, `thought`, `tool_call`, `final`, `cancelled`, ...).
 *      Named events do not reach `onmessage`; they require explicit
 *      `addEventListener` per type.
 *
 *   3. Reduce each event into `stream-store` via `reduceEvent`.
 *
 *   4. Forward lifecycle events (status, final, error, cancelled, ...)
 *      to `session-store` via `applyRunLifecycleEvent`.
 *
 *   5. Reconnect with exponential backoff and full jitter, resuming
 *      from `after_sequence = lastSequence`.
 *
 *   6. Detect dead connections via heartbeat timeout and reconnect.
 *
 *   7. Close the connection permanently on any terminal event
 *      (final / done / error / cancelled / mcp_auth_required) and stop
 *      further reconnection attempts.
 *
 *   8. Notify subscribers (typically `ChatContainer`) of events and
 *      state changes, including an initial sync when a subscriber
 *      attaches to an already-active run.
 *
 *   9. Prune terminal runs from `stream-store` after
 *      `TERMINAL_RUN_RETENTION_MS` (owned by `stream-store`) or when
 *      the retention cap is exceeded.
 *
 *  10. Restore active runs on application load by calling
 *      `GET /api/run/jobs/active` (endpoint added in Phase D.3).
 *
 * ## Why a manager and not a hook
 *
 * An SSE connection must outlive any single React component. When the
 * user navigates from `/chat/{id}` to `/projects`, `ChatContainer`
 * unmounts but the underlying run keeps executing. A hook-based
 * connection would close on unmount, losing every subsequent event.
 *
 * The manager lives at module scope, so its lifetime equals the page's.
 * Components attach to it via `subscribe(jobId, listener)` and detach
 * via the returned cleanup function. The connection itself is only
 * closed by the manager (on terminal event, on explicit disconnect, or
 * when the page unloads).
 *
 * ## Contract references
 *
 *   * docs/state-contracts.md §5.2 — SSE → store matrix
 *   * docs/state-contracts.md §6.3 — Stream Store retention
 *   * docs/state-contracts.md §7   — Deletion policy
 */

import { useStreamStore, TERMINAL_RUN_RETENTION_MS } from "@/stores/stream-store";
import { useSessionStore, mapLifecycleEvent } from "@/stores/session-store";
import type { SSEEnvelope, ServerEventName, RunState, RunStatus } from "@/lib/types";

// The canonical event lists live in `chat-container/constants.ts`. That
// file is currently the shared source of truth used by `use-sse-stream.ts`
// as well. In Phase F, when `use-sse-stream.ts` is refactored to a thin
// shim over this manager, the constants will move to `lib/sse-events.ts`.
// Until then, importing from the component folder is the cheapest way to
// avoid drift between the two consumers.
import {
  SSE_EVENT_TYPES,
  DELEGATION_EVENT_TYPES,
} from "@/components/chat/chat-container/constants";


// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/** First reconnect delay (ms). Subsequent attempts double, capped. */
const RECONNECT_INITIAL_DELAY_MS = 1_000;

/** Maximum reconnect delay (ms). */
const RECONNECT_MAX_DELAY_MS = 30_000;

/** Maximum consecutive reconnect attempts before giving up. */
const RECONNECT_MAX_ATTEMPTS = 8;

/**
 * Heartbeat timeout (ms). The backend emits a `ping` event every 15
 * seconds (see `sse_events.py` `subscribe_events`). If no event —
 * including pings — arrives within this window, the connection is
 * considered dead.
 *
 * 45s = 3 × the server ping interval. This absorbs one lost ping
 * without false-positiving, while catching a real disconnect within
 * under a minute.
 */
const HEARTBEAT_TIMEOUT_MS = 45_000;

/** Interval (ms) between periodic terminal-run sweeps. */
const PRUNE_SWEEP_INTERVAL_MS = 60_000;

/**
 * Statuses that mark a run as permanently finished for the purposes of
 * the terminal-freeze guard inside `_handleMessage`.
 */
const TERMINAL_STATUS_VALUES: ReadonlySet<string> = new Set([
  "cancelled",
  "completed",
  "failed",
  "interrupted",
]);


// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Subscriber callback signature.
 *
 * `event` is `null` exactly once per subscription — during the
 * immediate state-sync that fires when `subscribe()` is called on an
 * already-active run. Subsequent invocations always carry the event
 * that triggered the state change.
 */
export type RunSubscriber = (run: RunState, event: SSEEnvelope | null) => void;

/** Options accepted by `connect()` and `registerRun()`. */
export interface ConnectOptions {
  /**
   * Sequence number to resume from. Defaults to the run's current
   * `lastSequence` in `stream-store` (which is 0 on a fresh run and
   * after a page reload where the store has been cleared).
   */
  afterSequence?: number;
}

/** Internal per-connection state. */
interface Connection {
  jobId: string;
  chatId: string;
  eventSource: EventSource;
  reconnectAttempts: number;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  heartbeatTimer: ReturnType<typeof setTimeout> | null;
  /** Set to true once a terminal event has been received. */
  terminalReached: boolean;
}


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Determine whether an SSE event marks the end of the run.
 *
 * The primary terminal events are `final`, `done`, `error`, `cancelled`,
 * and `mcp_auth_required`. The `status` event is also checked
 * defensively: if a future backend implementation delivers the terminal
 * transition through a `status` event (rather than the dedicated ones),
 * the manager still closes the connection correctly.
 */
function isTerminalEvent(
  eventType: string,
  data: Record<string, any> | undefined,
): boolean {
  const lower = eventType.toLowerCase();
  if (
    lower === "final" ||
    lower === "done" ||
    lower === "error" ||
    lower === "cancelled" ||
    lower === "mcp_auth_required"
  ) {
    return true;
  }
  if (lower === "status") {
    const raw = String(data?.status ?? data?.state ?? "").toLowerCase();
    if (TERMINAL_STATUS_VALUES.has(raw)) return true;
  }
  return false;
}

/**
 * Build an SSEEnvelope from a raw named-SSE payload.
 *
 * Backend payload shape (see `streaming.py`):
 *   {
 *     id:    "evt_abc",
 *     type:  "<event_name>",
 *     step:  3,
 *     content: "...",
 *     toolName: "...",
 *     data:  { ... },
 *     seq:   42,
 *   }
 *
 * Some events also nest `seq` / `id` inside `data`. Both placements are
 * handled; the top-level field wins when both are present.
 */
function buildEnvelope(
  jobId: string,
  eventType: string,
  raw: Record<string, any> | undefined,
): SSEEnvelope {
  const payload = raw ?? {};
  const data =
    payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)
      ? (payload.data as Record<string, any>)
      : {};

  const eventId =
    (typeof payload.id === "string" && payload.id) ||
    (typeof payload.event_id === "string" && payload.event_id) ||
    (typeof data.event_id === "string" && data.event_id) ||
    undefined;

  const sequence =
    (typeof payload.seq === "number" && payload.seq) ||
    (typeof payload.sequence === "number" && payload.sequence) ||
    (typeof data.seq === "number" && data.seq) ||
    (typeof data.sequence === "number" && data.sequence) ||
    undefined;

  return {
    jobId,
    // `mcp_auth_required`, `chunk`, and `refs_resolved` are emitted by
    // the backend but missing from `ServerEventName` today. They will
    // be added when `lib/types.ts` is updated in Phase E-follow. The
    // cast is safe because the reducer treats unknown types as
    // dropped events rather than throwing.
    type: eventType as ServerEventName,
    step: typeof payload.step === "number" ? payload.step : 1,
    step_id:
      (typeof payload.step_id === "string" && payload.step_id) ||
      (typeof data.step_id === "string" && data.step_id) ||
      undefined,
    event_id: eventId,
    id: eventId,
    sequence,
    seq: sequence,
    data: Object.keys(data).length > 0 ? data : (payload as Record<string, any>),
    metadata:
      payload.metadata && typeof payload.metadata === "object"
        ? (payload.metadata as Record<string, any>)
        : undefined,
  };
}

/** Compute the backoff delay with full jitter. */
function computeBackoffDelay(attempt: number): number {
  const exponential = RECONNECT_INITIAL_DELAY_MS * Math.pow(2, attempt);
  const capped = Math.min(exponential, RECONNECT_MAX_DELAY_MS);
  return Math.floor(Math.random() * capped);
}

function isBrowser(): boolean {
  return typeof window !== "undefined";
}


// ---------------------------------------------------------------------------
// RunStreamManager
// ---------------------------------------------------------------------------

class RunStreamManager {
  /** Active connections, keyed by jobId. */
  private connections: Map<string, Connection> = new Map();

  /** Subscribers, keyed by jobId. */
  private subscribers: Map<string, Set<RunSubscriber>> = new Map();

  /** Periodic prune timer. */
  private sweepTimer: ReturnType<typeof setInterval> | null = null;

  /** True once global listeners have been attached. */
  private lifecycleListenersAttached = false;

  /** True when the browser reports offline. */
  private browserOffline = false;

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /**
   * Open an SSE stream for the given job.
   *
   * Idempotent: a second `connect()` with the same `jobId` is a no-op.
   * If the run is already in a terminal state (per the store), no
   * stream is opened — there is nothing more to receive.
   */
  connect(jobId: string, chatId: string, options: ConnectOptions = {}): void {
    if (!isBrowser()) return;
    if (!jobId) return;

    // Idempotent: already connected.
    if (this.connections.has(jobId)) return;

    // Already terminal in the store: don't reopen.
    const existing = useStreamStore.getState().getRun(jobId);
    if (existing && TERMINAL_STATUS_VALUES.has(existing.status)) {
      return;
    }

    this.attachLifecycleListeners();
    this.ensureSweepTimer();

    const afterSequence =
      options.afterSequence ??
      existing?.lastSequence ??
      0;

    this._openConnection(jobId, chatId, afterSequence);
  }

  /**
   * Explicitly close the stream for a run.
   *
   * Used when:
   *   * the user deletes the chat/project that owns the run,
   *   * a component decides to stop tracking a run it no longer needs
   *     (rare — the manager owns connection lifetime),
   *   * `shutdown()` on app teardown.
   */
  disconnect(jobId: string): void {
    this._teardown(jobId, "explicit disconnect");
  }

  /**
   * Register a new run and open its stream.
   *
   * Convenience over `connect()` that also seeds the stores:
   *   * `stream-store.initializeRun(jobId, prompt)`
   *   * `session-store.registerRun(chatId, jobId, { title, prompt })`
   *
   * Safe to call multiple times for the same jobId — each step is
   * internally idempotent.
   */
  registerRun(
    jobId: string,
    chatId: string,
    options: {
      prompt?: string;
      title?: string;
      projectId?: string;
      agentId?: string;
      afterSequence?: number;
    } = {},
  ): void {
    if (!isBrowser()) return;
    if (!jobId || !chatId) return;

    useStreamStore.getState().initializeRun(jobId, options.prompt ?? "");
    useSessionStore.getState().registerRun(chatId, jobId, {
      title: options.title,
      prompt: options.prompt,
      projectId: options.projectId,
      agentId: options.agentId,
    });

    this.connect(jobId, chatId, { afterSequence: options.afterSequence });
  }

  /**
   * Attach a listener that receives events and state changes for a
   * specific run. The returned function detaches the listener.
   *
   * If the run already has state in the store, the listener fires
   * immediately with `event = null` so the subscriber can render the
   * current state without a race.
   */
  subscribe(jobId: string, listener: RunSubscriber): () => void {
    let listeners = this.subscribers.get(jobId);
    if (!listeners) {
      listeners = new Set();
      this.subscribers.set(jobId, listeners);
    }
    listeners.add(listener);

    // Initial sync.
    const currentRun = useStreamStore.getState().getRun(jobId);
    if (currentRun) {
      try {
        listener(currentRun, null);
      } catch (err) {
        console.warn(
          "[RunStreamManager] subscriber threw during initial sync:",
          err,
        );
      }
    }

    return () => {
      const set = this.subscribers.get(jobId);
      if (!set) return;
      set.delete(listener);
      if (set.size === 0) {
        this.subscribers.delete(jobId);
      }
    };
  }

  /** Return the set of jobIds with an active connection. */
  listActiveRuns(): string[] {
    return Array.from(this.connections.keys());
  }

  /**
   * Fetch the active-runs list from the backend and (re)open streams.
   *
   * Called by `AppDataBootstrap` on app load. Idempotent — if a job is
   * already connected, the second attempt is a no-op.
   *
   * Gracefully degrades when the endpoint is not yet available (e.g.
   * during staged rollouts where the frontend deploys before the
   * backend adds `/api/run/jobs/active`). The manager logs a debug
   * message and returns without opening anything.
   */
  async restoreActiveRuns(): Promise<void> {
    if (!isBrowser()) return;

    try {
      const res = await fetch("/api/run/jobs/active", { cache: "no-store" });
      if (!res.ok) {
        // Endpoint not yet implemented (Phase D.3). Silent no-op.
        console.debug(
          "[RunStreamManager] /api/run/jobs/active unavailable (HTTP %d); skipping restore.",
          res.status,
        );
        return;
      }

      const payload = await res.json();
      const jobs: Array<{
        job_id?: string;
        chat_id?: string;
        last_sequence?: number;
      }> = Array.isArray(payload?.jobs) ? payload.jobs : [];

      for (const item of jobs) {
        const jobId = item?.job_id;
        const chatId = item?.chat_id;
        if (typeof jobId !== "string" || !jobId) continue;
        if (typeof chatId !== "string" || !chatId) continue;

        this.connect(jobId, chatId, {
          afterSequence:
            typeof item.last_sequence === "number" ? item.last_sequence : 0,
        });
      }

      if (jobs.length > 0) {
        console.info(
          "[RunStreamManager] Restored %d active run(s) after app load.",
          jobs.length,
        );
      }
    } catch (err) {
      console.debug(
        "[RunStreamManager] Active-runs restore failed (endpoint may be unavailable):",
        err,
      );
    }
  }

  /**
   * Close every connection, clear every subscriber, and stop the sweep
   * timer. Intended for tests and for app teardown during hot reloads.
   */
  shutdown(): void {
    for (const jobId of Array.from(this.connections.keys())) {
      this._teardown(jobId, "shutdown");
    }
    this.subscribers.clear();
    this.stopSweepTimer();
  }

  // -------------------------------------------------------------------------
  // Connection lifecycle (private)
  // -------------------------------------------------------------------------

  private _openConnection(
    jobId: string,
    chatId: string,
    afterSequence: number,
  ): void {
    const url =
      afterSequence > 0
        ? `/api/run/jobs/${encodeURIComponent(jobId)}/stream?after_sequence=${afterSequence}`
        : `/api/run/jobs/${encodeURIComponent(jobId)}/stream`;

    let eventSource: EventSource;
    try {
      eventSource = new EventSource(url);
    } catch (err) {
      console.error(
        "[RunStreamManager] Failed to open EventSource for job %s:",
        jobId,
        err,
      );
      return;
    }

    const connection: Connection = {
      jobId,
      chatId,
      eventSource,
      reconnectAttempts: 0,
      reconnectTimer: null,
      heartbeatTimer: null,
      terminalReached: false,
    };
    this.connections.set(jobId, connection);

    // Reset heartbeat on every event.
    const resetHeartbeat = () => {
      if (connection.heartbeatTimer) {
        clearTimeout(connection.heartbeatTimer);
      }
      connection.heartbeatTimer = setTimeout(() => {
        // No event within the window. Tear down and reconnect.
        if (this.connections.get(jobId) !== connection) return;
        this._scheduleReconnect(jobId, "heartbeat timeout");
      }, HEARTBEAT_TIMEOUT_MS);
    };

    eventSource.onopen = () => {
      connection.reconnectAttempts = 0;
      resetHeartbeat();
    };

    eventSource.onerror = () => {
      // The spec fires onerror on transient failures too. Close
      // immediately to prevent the browser's automatic reconnect
      // (which ignores after_sequence and our backoff), then schedule
      // our own reconnect.
      try {
        eventSource.close();
      } catch {
        /* ignore */
      }
      if (connection.terminalReached) return;
      this._scheduleReconnect(jobId, "connection error");
    };

    // Attach a listener for every known event name. Named events do
    // NOT fire onmessage; they require addEventListener per type.
    const allEventNames = new Set<string>([
      ...(SSE_EVENT_TYPES as readonly string[]),
      ...(DELEGATION_EVENT_TYPES as readonly string[]),
    ]);

    for (const eventName of allEventNames) {
      eventSource.addEventListener(eventName, (e: Event) => {
        resetHeartbeat();

        // ─── Guard: distinguish built-in EventSource `error` from
        //     server-sent `event: error` ───────────────────────────
        //
        // The `EventSource` API uses the same event name ("error")
        // for two completely different things:
        //
        //   1. Built-in network/connection error (fired when the
        //      browser detects a disconnect, including a CLEAN
        //      server-side close after a `final` event). This event
        //      has NO `.data` payload.
        //
        //   2. Server-sent `event: error` message (the backend sent
        //      an explicit error event). This event carries a JSON
        //      string in `.data`.
        //
        // Without this guard, closing the connection after `final`
        // triggers the built-in error which our handler would
        // misinterpret as a server-side failure, overriding the
        // legitimate `completed` state (regression observed in
        // Phase F.3: backend logs "completed successfully", UI shows
        // "Execution error encountered.").
        //
        // The guard only applies to the `error` name in practice;
        // all other names (`final`, `thought`, `ping`, ...) are
        // unique to the server and never collide with built-in
        // EventSource events. Applying it to every name is
        // harmless and future-proof.
        const data = (e as MessageEvent).data;
        if (typeof data !== "string" || data.length === 0) {
          return;
        }

        try {
          const raw = JSON.parse(data);
          this._handleMessage(jobId, connection, eventName, raw);
        } catch (err) {
          console.warn(
            "[RunStreamManager] Failed to parse SSE payload for %s event on job %s:",
            eventName,
            jobId,
            err,
          );
        }
      });
    }

    // Also listen for the default message channel in case the backend
    // emits an event without a name field.
    eventSource.onmessage = (e: MessageEvent) => {
      resetHeartbeat();
      try {
        const raw = JSON.parse(e.data || "{}");
        const type =
          (typeof raw?.type === "string" && raw.type) || "status";
        this._handleMessage(jobId, connection, type, raw);
      } catch (err) {
        console.warn(
          "[RunStreamManager] Failed to parse unnamed SSE payload on job %s:",
          jobId,
          err,
        );
      }
    };

    resetHeartbeat();
  }

  private _handleMessage(
    jobId: string,
    connection: Connection,
    eventType: string,
    raw: any,
  ): void {
    // Ignore pings — they exist only to reset the heartbeat timer,
    // which was already handled by the caller.
    if (eventType.toLowerCase() === "ping") return;

    // ─── Terminal freeze ─────────────────────────────────────────
    //
    // After a terminal event (final, done, error, cancelled,
    // mcp_auth_required) has been processed, the run's state in
    // `stream-store` is authoritative and must not be modified.
    //
    // The most common late-arriving event is the built-in EventSource
    // `error` that the browser fires when the server closes the
    // socket. Even though the guard in the addEventListener callback
    // (see `_openConnection`) already filters that case, this second
    // guard is defense-in-depth: it protects against future event
    // sources that could re-trigger processing (e.g. a synthetic
    // event injected by tests, or a browser quirk that delivers the
    // built-in error with a non-empty `.data` payload).
    if (connection.terminalReached) return;

    const envelope = buildEnvelope(jobId, eventType, raw);

    // 1. Reduce into stream-store.
    try {
      useStreamStore.getState().applyEvent(jobId, envelope);
    } catch (err) {
      console.warn(
        "[RunStreamManager] stream-store.applyEvent threw for job %s:",
        jobId,
        err,
      );
    }

    // 2. Update session-store on lifecycle events only.
    const nextStatus = mapLifecycleEvent(eventType, envelope.data);
    if (nextStatus) {
      try {
        useSessionStore
          .getState()
          .applyRunLifecycleEvent(connection.chatId, jobId, eventType, envelope.data);
      } catch (err) {
        console.warn(
          "[RunStreamManager] session-store lifecycle update threw for job %s:",
          jobId,
          err,
        );
      }
    }

    // 3. Notify subscribers.
    const run = useStreamStore.getState().getRun(jobId);
    if (run) {
      const listeners = this.subscribers.get(jobId);
      if (listeners) {
        for (const listener of listeners) {
          try {
            listener(run, envelope);
          } catch (err) {
            console.warn(
              "[RunStreamManager] subscriber threw for job %s:",
              jobId,
              err,
            );
          }
        }
      }
    }

    // 4. Terminal handling — close permanently.
    if (isTerminalEvent(eventType, envelope.data)) {
      connection.terminalReached = true;
      // Let the current call stack finish (subscribers may want to
      // read the final state), then tear down on the next tick.
      setTimeout(() => {
        if (this.connections.get(jobId) === connection) {
          this._teardown(jobId, `terminal event: ${eventType}`);
        }
      }, 0);
    }
  }

  private _scheduleReconnect(jobId: string, reason: string): void {
    const connection = this.connections.get(jobId);
    if (!connection) return;

    // The browser is offline — do not burn attempts. The `online`
    // listener will retry on reconnection.
    if (this.browserOffline) {
      return;
    }

    if (connection.reconnectAttempts >= RECONNECT_MAX_ATTEMPTS) {
      console.warn(
        "[RunStreamManager] Giving up on job %s after %d attempts (%s).",
        jobId,
        RECONNECT_MAX_ATTEMPTS,
        reason,
      );
      this._teardown(jobId, `reconnect exhausted (${reason})`);
      return;
    }

    // Close the current EventSource; the new one will replace it.
    try {
      connection.eventSource.close();
    } catch {
      /* ignore */
    }

    const delay = computeBackoffDelay(connection.reconnectAttempts);
    connection.reconnectAttempts += 1;

    if (connection.reconnectTimer) clearTimeout(connection.reconnectTimer);
    connection.reconnectTimer = setTimeout(() => {
      const stillThere = this.connections.get(jobId);
      if (stillThere !== connection) return;
      if (connection.terminalReached) return;
      if (this.browserOffline) return;

      // Reopen with the updated last sequence.
      this.connections.delete(jobId);
      const lastSeq =
        useStreamStore.getState().getRun(jobId)?.lastSequence ?? 0;
      this._openConnection(jobId, connection.chatId, lastSeq);
    }, delay);
  }

  private _teardown(jobId: string, reason: string): void {
    const connection = this.connections.get(jobId);
    if (!connection) return;

    try {
      connection.eventSource.close();
    } catch {
      /* ignore */
    }
    if (connection.reconnectTimer) {
      clearTimeout(connection.reconnectTimer);
      connection.reconnectTimer = null;
    }
    if (connection.heartbeatTimer) {
      clearTimeout(connection.heartbeatTimer);
      connection.heartbeatTimer = null;
    }
    this.connections.delete(jobId);

    // Subscribers are intentionally NOT cleared here. They belong to
    // the ChatContainer, which may re-attach after a reconnect or
    // remain subscribed to read the final state. They are removed
    // when ChatContainer calls the unsubscribe function on unmount.

    if (reason !== "explicit disconnect" && reason !== "shutdown") {
      console.debug(
        "[RunStreamManager] Closed connection for job %s (%s).",
        jobId,
        reason,
      );
    }
  }

  // -------------------------------------------------------------------------
  // Sweep timer
  // -------------------------------------------------------------------------

  private ensureSweepTimer(): void {
    if (this.sweepTimer) return;
    if (!isBrowser()) return;

    this.sweepTimer = setInterval(() => {
      try {
        useStreamStore.getState().pruneTerminalRuns();
      } catch (err) {
        console.warn("[RunStreamManager] pruneTerminalRuns threw:", err);
      }
    }, PRUNE_SWEEP_INTERVAL_MS);
  }

  private stopSweepTimer(): void {
    if (this.sweepTimer) {
      clearInterval(this.sweepTimer);
      this.sweepTimer = null;
    }
  }

  // -------------------------------------------------------------------------
  // Global lifecycle listeners
  // -------------------------------------------------------------------------

  private attachLifecycleListeners(): void {
    if (this.lifecycleListenersAttached) return;
    if (!isBrowser()) return;
    this.lifecycleListenersAttached = true;

    window.addEventListener("offline", () => {
      this.browserOffline = true;
    });

    window.addEventListener("online", () => {
      this.browserOffline = false;
      // Attempt to reconnect any connection that was waiting on
      // network availability. Reconnect delays are honored per
      // connection.
      for (const [jobId, connection] of this.connections) {
        if (connection.terminalReached) continue;
        if (connection.reconnectTimer) {
          // Already scheduled; the timer will fire shortly.
          continue;
        }
        // If the connection exists but the source is dead, schedule
        // a reconnect to force a fresh open.
        if (connection.eventSource.readyState === EventSource.CLOSED) {
          this._scheduleReconnect(jobId, "browser came online");
        }
      }
    });
  }
}


// ---------------------------------------------------------------------------
// Singleton export
// ---------------------------------------------------------------------------

/**
 * The single instance that owns every SSE connection in the app.
 *
 * Consumers must import this object directly, not instantiate the class.
 * The class is intentionally not exported — the singleton is the only
 * supported entry point.
 */
export const runStreamManager = new RunStreamManager();


// ---------------------------------------------------------------------------
// Re-exports for convenience
// ---------------------------------------------------------------------------

/**
 * Re-exported so callers can reason about the retention window without
 * importing `stream-store` separately.
 */
export { TERMINAL_RUN_RETENTION_MS };
