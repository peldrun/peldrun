// Path: frontend/src/stores/stream-store.ts
"use client";

/**
 * Stream Store — in-memory catalogue of active and recently-terminated runs.
 *
 * Owns the detailed execution state of every run the frontend currently
 * knows about: steps, tools, artifacts, child runs, approvals, and the
 * reduced `RunState` object that the UI consumes for rendering.
 *
 * Boundary (docs/state-contracts.md §6.3)
 * ---------------------------------------
 *   * Stream Store owns RunState objects (the "what happened during
 *     this run" data).
 *   * Session Store owns chat metadata (title, project, status). Lifecycle
 *     transitions from SSE are forwarded to Session Store by the
 *     run-stream-manager — this store does NOT touch sessions directly.
 *
 * Retention policy (docs/state-contracts.md §6.3)
 * -----------------------------------------------
 *   * Active runs (any non-terminal status): retained indefinitely.
 *   * Terminal runs (cancelled, completed, failed, interrupted): retained
 *     for TERMINAL_RUN_RETENTION_MS (5 minutes) OR until the total number
 *     of terminal runs exceeds MAX_TERMINAL_RUNS_RETAINED (100), whichever
 *     happens first.
 *
 *   Rationale:
 *     - The 5-minute window covers "user accidentally refreshed the tab"
 *       and "user navigated to another page and back". No data is lost —
 *       session.json on disk is the source of truth; this is just the
 *       in-memory copy.
 *     - The 100-run cap prevents pathological memory growth when a user
 *       launches many short-lived runs in a single session.
 *
 *   `lastEventAt` doubles as the "terminal since" timestamp. Once a run
 *   reaches a terminal status, no more events arrive, so `lastEventAt`
 *   stops advancing and is exactly when the run ended.
 */

import { create } from "zustand";

import {
  RunState,
  SSEEnvelope,
  RunStatus,
} from "@/lib/types";
import { reduceEvent, initialRunState } from "@/lib/stream-reducer";


// ---------------------------------------------------------------------------
// Retention constants — exported for tests and future configurability.
// ---------------------------------------------------------------------------

/** How long a terminal run is retained in memory before pruning. */
export const TERMINAL_RUN_RETENTION_MS = 5 * 60 * 1000; // 5 minutes

/** Hard cap on terminal runs kept in memory. */
export const MAX_TERMINAL_RUNS_RETAINED = 100;

/** Statuses that indicate a run has reached a terminal state. */
const TERMINAL_RUN_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  "cancelled",
  "completed",
  "failed",
  "interrupted",
]);


// ---------------------------------------------------------------------------
// Store interface
// ---------------------------------------------------------------------------

interface StreamStore {
  // Data
  runs: Record<string, RunState>;

  // Read
  getRun: (jobId: string) => RunState | undefined;

  // Write
  initializeRun: (jobId: string, prompt?: string) => void;
  upsertRun: (jobId: string, state: RunState) => void;
  applyEvent: (jobId: string, event: SSEEnvelope) => void;
  clearRun: (jobId: string) => void;
  removeRun: (jobId: string) => void;

  // Retention
  pruneTerminalRuns: (now?: number) => void;

  // Test helper
  _reset: () => void;
}


// ---------------------------------------------------------------------------
// Store implementation
// ---------------------------------------------------------------------------

export const useStreamStore = create<StreamStore>((set, get) => ({
  runs: {},

  // -------------------------------------------------------------------------
  // Read
  // -------------------------------------------------------------------------

  getRun: (jobId) => get().runs[jobId],

  // -------------------------------------------------------------------------
  // Write
  // -------------------------------------------------------------------------

  initializeRun: (jobId, prompt = "") => {
    set((state) => {
      // Do not overwrite an existing run. The reducer has already
      // accumulated events; reinitializing would reset sequence tracking
      // and duplicate detection.
      if (state.runs[jobId]) return state;
      return {
        runs: {
          ...state.runs,
          [jobId]: initialRunState(jobId, prompt),
        },
      };
    });
  },

  upsertRun: (jobId, runState) => {
    set((state) => ({
      runs: {
        ...state.runs,
        [jobId]: runState,
      },
    }));
  },

  applyEvent: (jobId, event) => {
    set((state) => {
      const current = state.runs[jobId];
      if (!current) {
        // Run was not explicitly initialized — create a stub and apply
        // the event on top. This handles the case where an SSE event
        // arrives before `registerRun` was called (e.g. ChatContainer
        // mounted, subscribed, and received a replayed event before
        // completing the POST).
        const stub = initialRunState(jobId, "");
        const next = reduceEvent(stub, event);
        return {
          runs: {
            ...state.runs,
            [jobId]: next,
          },
        };
      }

      const next = reduceEvent(current, event);
      return {
        runs: {
          ...state.runs,
          [jobId]: next,
        },
      };
    });
  },

  clearRun: (jobId) => {
    set((state) => {
      if (!state.runs[jobId]) return state;
      const next = { ...state.runs };
      delete next[jobId];
      return { runs: next };
    });
  },

  removeRun: (jobId) => {
    // Alias for clearRun, added for API consistency with session-store
    // (which exposes `removeSession`). Keeping both names avoids a
    // breaking change for any existing `clearRun` callers while making
    // the naming uniform across stores.
    get().clearRun(jobId);
  },

  // -------------------------------------------------------------------------
  // Retention
  // -------------------------------------------------------------------------

  pruneTerminalRuns: (now = Date.now()) => {
    set((state) => {
      const runs = state.runs;

      // Step 1: drop terminal runs older than the retention window.
      const survivingAfterAge: Record<string, RunState> = {};
      const terminalRuns: Array<{ jobId: string; lastEventAt: number }> = [];

      for (const [jobId, run] of Object.entries(runs)) {
        if (!TERMINAL_RUN_STATUSES.has(run.status)) {
          // Non-terminal runs are always retained.
          survivingAfterAge[jobId] = run;
          continue;
        }

        const age = now - (run.lastEventAt || 0);
        if (age > TERMINAL_RUN_RETENTION_MS) {
          // Expired — drop.
          continue;
        }

        survivingAfterAge[jobId] = run;
        terminalRuns.push({ jobId, lastEventAt: run.lastEventAt || 0 });
      }

      // Step 2: enforce the hard cap by dropping the oldest terminal
      // runs first.
      if (terminalRuns.length > MAX_TERMINAL_RUNS_RETAINED) {
        terminalRuns.sort((a, b) => a.lastEventAt - b.lastEventAt);
        const toEvict = terminalRuns.length - MAX_TERMINAL_RUNS_RETAINED;
        for (let i = 0; i < toEvict; i++) {
          delete survivingAfterAge[terminalRuns[i].jobId];
        }
      }

      // Return the same state reference if nothing changed, so
      // `useSyncExternalStore` skips the re-render.
      const beforeCount = Object.keys(runs).length;
      const afterCount = Object.keys(survivingAfterAge).length;
      if (beforeCount === afterCount) {
        return state;
      }

      return { runs: survivingAfterAge };
    });
  },

  // -------------------------------------------------------------------------
  // Test helper
  // -------------------------------------------------------------------------

  _reset: () => {
    set({ runs: {} });
  },
}));
