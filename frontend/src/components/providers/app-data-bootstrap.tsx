// Path: frontend/src/components/providers/app-data-bootstrap.tsx
"use client";

/**
 * App Data Bootstrap — one-shot data initialization at app startup.
 *
 * Mounted exactly once at the application root (see `app/layout.tsx`).
 * Its sole job is to trigger the initial data loads for the centralized
 * stores and to wire up global recovery listeners:
 *
 *   1. Load the first page of sessions (Session Store).
 *   2. Load the full projects list (Project Store).
 *   3. Restore any active runs (Run Stream Manager → `/api/run/jobs/active`).
 *   4. Reconcile on browser reconnection (`online` event).
 *
 * ## Why a provider and not a `useEffect` inside a page
 *
 * The bootstrap must run before any page can render a stale view. If
 * each page did its own initial load, navigating to a page that had
 * not yet been visited would show an empty state until the load
 * completed, defeating the purpose of a centralized store. The provider
 * runs at the app root, so every page can assume the stores are already
 * populated (or actively loading) by the time it mounts.
 *
 * ## Why the loads are not awaited
 *
 * Each `load*` method has an internal `isLoading` guard. If we awaited
 * them sequentially here, a slow endpoint would block the mount cycle
 * and delay first paint. By firing them in parallel without awaiting,
 * the stores start their fetches immediately and report progress via
 * their own `isLoading` fields. Consumers that need the data can check
 * `isInitialized` before rendering.
 *
 * ## StrictMode safety
 *
 * React 18 StrictMode mounts each component twice in development. Every
 * `load*` method is idempotent (checks `isInitialized` before fetching),
 * so the double mount produces exactly one network request per store.
 *
 * ## Recovery on reconnect
 *
 * The `online` listener triggers `restoreActiveRuns()` again. If the
 * browser went offline while a run was executing, the stream manager's
 * reconnect attempt would have exhausted its backoff budget by the time
 * connectivity returned. This listener provides the recovery path.
 *
 * See docs/state-contracts.md §4 (App Data Bootstrap) and §5.2
 * (lifecycle matrix).
 */

import { useEffect } from "react";

import { useSessionStore } from "@/stores/session-store";
import { useProjectStore } from "@/stores/project-store";
import { useStreamStore } from "@/stores/stream-store";
import { runStreamManager } from "@/lib/run-stream-manager";


export function AppDataBootstrap() {
  useEffect(() => {
    // -----------------------------------------------------------------------
    // 1. Fire the initial loads in parallel.
    //
    // Each `load*` method is idempotent on both counts:
    //   * Concurrency: `if (state.isLoading) return;`
    //   * Redundancy:  `if (!force && state.isInitialized) return;`
    //
    // Errors are caught inside the store methods and surfaced via
    // `state.error`. We do not need a try/catch here — a rejection would
    // only happen for programming errors in the store itself.
    // -----------------------------------------------------------------------
    void useSessionStore.getState().loadSidebarSessions();
    void useProjectStore.getState().loadProjects();
    void runStreamManager.restoreActiveRuns();

    // -----------------------------------------------------------------------
    // 2. Reconcile on browser reconnection.
    //
    // When the browser goes offline and comes back, the SSE connections
    // that were open at the moment of disconnect will have exhausted
    // their reconnect budget (8 attempts with exponential backoff, max
    // 30s each — roughly 4 minutes total). By the time connectivity
    // returns, those connections are dead.
    //
    // This listener force-refreshes the active-runs list and reopens
    // any SSE connections the manager needs. It is idempotent: if a
    // connection is already open, `connect()` is a no-op.
    //
    // The session store's cached data is intentionally NOT refetched
    // here. The whole point of the centralized store is that stale data
    // is better than no data. The next user action (or a background
    // sync in a later phase) will refresh it. Forcing a fetch on every
    // reconnect would produce janky UI flickers on flaky networks.
    // -----------------------------------------------------------------------
    const handleOnline = () => {
      void runStreamManager.restoreActiveRuns();
    };

    window.addEventListener("online", handleOnline);

    // -----------------------------------------------------------------------
    // 3. Development-only store exposure.
    //
    // Attaches the stores and the stream manager to `window.__peldrunStores`
    // so that Phase C / Phase D acceptance tests can be run from the
    // DevTools console without needing to bundle a test harness.
    //
    // The `process.env.NODE_ENV` check is statically replaced at build
    // time, so this block is completely tree-shaken out of production
    // bundles. It is safe to leave in the source.
    //
    // Usage example:
    //
    //   // Success path
    //   await window.__peldrunStores.project.getState().loadProjects();
    //   window.__peldrunStores.project.getState().error  // → null
    //
    //   // Failure path (stop uvicorn first)
    //   await window.__peldrunStores.project.getState().loadProjects(true);
    //   window.__peldrunStores.project.getState().error  // → ApiError message
    // -----------------------------------------------------------------------
    if (process.env.NODE_ENV === "development" && typeof window !== "undefined") {
      (window as any).__peldrunStores = {
        session: useSessionStore,
        project: useProjectStore,
        stream: useStreamStore,
        manager: runStreamManager,
      };
    }

    return () => {
      window.removeEventListener("online", handleOnline);
      if (process.env.NODE_ENV === "development" && typeof window !== "undefined") {
        // Clean up so hot-reload doesn't accumulate stale references.
        delete (window as any).__peldrunStores;
      }
    };
  }, []);

  // Renders nothing — this is a side-effect-only provider.
  return null;
}


export default AppDataBootstrap;
