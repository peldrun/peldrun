// Path: frontend/src/stores/session-store.ts
"use client";

/**
 * Session Store — single source of truth for chat session metadata.
 *
 * Owns the canonical in-memory representation of every chat session that
 * the frontend currently displays. Two presentation surfaces share this
 * store:
 *
 *   - Sidebar: reads `sidebarOrder` + `sessionsById`.
 *   - History / Project Workspace: call `loadSidebarSessions` with
 *     filters, or consume the same store via `useSessionStore` selectors.
 *
 * The store does NOT own run details (steps, tools, artifacts, streaming
 * events). Those live in `stream-store.ts` and are populated by the
 * upcoming `run-stream-manager`. The boundary is documented in
 * `docs/state-contracts.md` §6.1.
 *
 * Design decisions
 * ----------------
 *
 * 1. **Pagination, not bulk loading.**
 *    `loadSidebarSessions` fetches one page (50 items by default). Further
 *    pages are appended via `loadMoreSidebarSessions`. This is required by
 *    contract §7 (no full history load in the sidebar) and enforced by
 *    `fetchChatsPage`'s pagination API.
 *
 * 2. **Failure does NOT clear data.**
 *    A network error sets `error` but preserves `sessionsById` and
 *    `sidebarOrder`. The user retains their last known view while a retry
 *    is possible. This closes contract §13.9 ("API failure must not wipe
 *    existing data").
 *
 * 3. **Stale-write protection.**
 *    `updateSessionStatus` rejects events whose `jobId` differs from the
 *    session's current `job_id`. A session can contain many runs across
 *    its lifetime (one per turn), so an event from a stale run must never
 *    overwrite the state of a newer run. This closes contract §13.3.
 *
 * 4. **Terminal freeze.**
 *    Once a session reaches a terminal status (`cancelled`, `completed`,
 *    `failed`, `interrupted`), `updateSessionStatus` refuses further
 *    transitions. Only `registerRun` may reset the state, and only when
 *    a new run legitimately begins.
 *
 * 5. **Legacy status normalization.**
 *    Historic session documents may carry `stopped`, `active`, `done`, or
 *    `error`. These are normalized on every write path so the frontend
 *    never sees a pre-contract value.
 */

import { create } from "zustand";

import {
  ChatSession,
  fetchChatsPage,
  clampChatsPageLimit,
  DEFAULT_CHATS_PAGE_LIMIT,
} from "@/lib/chatsApi";


// ---------------------------------------------------------------------------
// Canonical session status
//
// Mirrors `SessionStatus` in docs/state-contracts.md §3. Kept here as a
// local type alias rather than in `lib/types.ts` so this module stays
// self-contained and can be imported by `stream-store.ts` and
// `run-stream-manager.ts` without circular dependencies. When
// `lib/types.ts` is updated in a later phase, this can be moved there
// and re-exported.
// ---------------------------------------------------------------------------

export type SessionStatus =
  | "pending"
  | "queued"
  | "starting"
  | "running"
  | "waiting_for_input"
  | "retrying"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed"
  | "interrupted";

const VALID_STATUSES: ReadonlySet<string> = new Set<SessionStatus>([
  "pending",
  "queued",
  "starting",
  "running",
  "waiting_for_input",
  "retrying",
  "paused",
  "cancelling",
  "cancelled",
  "completed",
  "failed",
  "interrupted",
]);

export const TERMINAL_STATUSES: ReadonlySet<SessionStatus> = new Set<SessionStatus>([
  "cancelled",
  "completed",
  "failed",
  "interrupted",
]);

const LEGACY_STATUS_MAP: Readonly<Record<string, SessionStatus>> = {
  stopped: "cancelled",
  active: "pending",
  done: "completed",
  error: "failed",
};

/**
 * Normalize an arbitrary status string into a `SessionStatus`.
 *
 * Legacy values are mapped to their canonical equivalent. Unknown values
 * fall back to `pending` (the safest non-terminal state). This is the
 * only place where a session's status field is allowed to be interpreted
 * as a union — every write path funnels through here.
 */
export function normalizeStatus(raw: string | null | undefined): SessionStatus {
  if (!raw) return "pending";
  const lower = String(raw).trim().toLowerCase();
  const legacy = LEGACY_STATUS_MAP[lower];
  if (legacy) return legacy;
  if (VALID_STATUSES.has(lower)) return lower as SessionStatus;
  return "pending";
}

/**
 * Map a lifecycle SSE event to the session status it implies.
 *
 * Returns `null` when the event does not affect the session status
 * (e.g. `thought`, `tool_call`, `step_start`). See the lifecycle matrix
 * in docs/state-contracts.md §5.2 for the authoritative table.
 *
 * This function is pure. It does not read from or write to the store.
 */
export function mapLifecycleEvent(
  eventType: string,
  data: Record<string, any> | undefined | null,
): SessionStatus | null {
  const payload = data ?? {};
  switch (eventType) {
    case "status":
    case "run_accepted":
    case "run_started": {
      const raw = payload.status || payload.state;
      if (typeof raw !== "string" || !raw) return null;
      return normalizeStatus(raw);
    }
    case "approval_required":
      return "waiting_for_input";
    case "approval_resolved":
      // Only meaningful when we were waiting for input. The caller
      // applies this on top of the current status; if the current is
      // already `running`, the transition is a no-op.
      return "running";
    case "mcp_auth_required":
      return "failed";
    case "final":
    case "done":
      return "completed";
    case "error": {
      const code = payload.code || payload.status;
      if (code === "cancelled") return "cancelled";
      return "failed";
    }
    case "cancelled":
      return "cancelled";
    default:
      return null;
  }
}


// ---------------------------------------------------------------------------
// Sidebar pagination constants
// ---------------------------------------------------------------------------

/** Default page size for the sidebar. Matches the backend default. */
export const SIDEBAR_PAGE_SIZE = DEFAULT_CHATS_PAGE_LIMIT; // 50


// ---------------------------------------------------------------------------
// Store interface
// ---------------------------------------------------------------------------

interface RegisterRunOptions {
  /** Title to use if the session is not already in the store. */
  title?: string;
  /** Prompt to use if the session is not already in the store. */
  prompt?: string;
  /** Project id to use if the session is not already in the store. */
  projectId?: string;
  /** Agent id to use if the session is not already in the store. */
  agentId?: string;
}

interface SessionState {
  // ── Global sidebar sessions (the "chat" view) ─────────────────────────
  sessionsById: Record<string, ChatSession>;
  sidebarOrder: string[];

  // Loading state
  isLoading: boolean;
  isInitialized: boolean;
  error: string | null;

  // Pagination
  nextCursor: string | null;
  hasMore: boolean;

  // ── Per-project sessions cache ────────────────────────────────────────
  //
  // Fetched on demand when the user opens /projects/{projectId}. Keyed
  // by projectId so switching between projects and back is instant (no
  // network round-trip). See docs/state-contracts.md §6.1.
  //
  // The cache is filled by `loadProjectSessions(projectId)` and
  // extended by `loadMoreProjectSessions(projectId)`. It is NOT
  // invalidated by SSE updates — the manager patches matching entries
  // via `patchSession`, so cache entries reflect live status changes.
  projectSessionsCache: Record<string, ChatSession[]>;
  projectSessionsCursor: Record<string, string | null>;
  projectSessionsHasMore: Record<string, boolean>;
  projectSessionsLoading: Record<string, boolean>;
  projectSessionsError: Record<string, string | null>;

  // Read helpers
  getSession: (chatId: string) => ChatSession | undefined;
  getOrderedSessions: () => ChatSession[];
  getProjectSessions: (projectId: string) => ChatSession[] | undefined;

  // Write operations
  upsertSession: (session: ChatSession) => void;
  patchSession: (chatId: string, patch: Partial<ChatSession>) => void;
  updateSessionStatus: (
    chatId: string,
    jobId: string,
    status: SessionStatus,
  ) => void;
  removeSession: (chatId: string) => void;
  registerRun: (chatId: string, jobId: string, options?: RegisterRunOptions) => void;

  // Network operations
  loadSidebarSessions: (force?: boolean) => Promise<void>;
  loadMoreSidebarSessions: () => Promise<void>;
  loadProjectSessions: (projectId: string, force?: boolean) => Promise<void>;
  loadMoreProjectSessions: (projectId: string) => Promise<void>;

  // Event application (called by run-stream-manager)
  applyRunLifecycleEvent: (
    chatId: string,
    jobId: string,
    eventType: string,
    data: Record<string, any>,
  ) => void;

  /** Wipe all sessions and project caches (used by History's "Purge All"). */
  clearAll: () => void;

  /** Mark a project's session cache as stale so the next load refetches. */
  invalidateProjectSessions: (projectId: string) => void;

  // Test/diagnostic helper — clears all state.
  // Not used by any production code path; callers in tests use
  // `useSessionStore.setState({...})` directly.
  _reset: () => void;
}


// ---------------------------------------------------------------------------
// Store implementation
// ---------------------------------------------------------------------------

export const useSessionStore = create<SessionState>((set, get) => ({
  // Initial state
  sessionsById: {},
  sidebarOrder: [],
  isLoading: false,
  isInitialized: false,
  error: null,
  nextCursor: null,
  hasMore: false,

  // Per-project cache (empty until a project is opened)
  projectSessionsCache: {},
  projectSessionsCursor: {},
  projectSessionsHasMore: {},
  projectSessionsLoading: {},
  projectSessionsError: {},

  // -------------------------------------------------------------------------
  // Read helpers
  // -------------------------------------------------------------------------

  getSession: (chatId) => get().sessionsById[chatId],

  getOrderedSessions: () => {
    const state = get();
    const result: ChatSession[] = [];
    for (const id of state.sidebarOrder) {
      const session = state.sessionsById[id];
      if (session) result.push(session);
    }
    return result;
  },

  getProjectSessions: (projectId) => {
    if (!projectId) return undefined;
    return get().projectSessionsCache[projectId];
  },

  // -------------------------------------------------------------------------
  // Write operations
  // -------------------------------------------------------------------------

  upsertSession: (session) => {
    set((state) => {
      const normalized: ChatSession = {
        ...session,
        status: normalizeStatus(session.status),
      };
      const existing = state.sessionsById[session.id];

      if (existing) {
        return {
          sessionsById: {
            ...state.sessionsById,
            [session.id]: { ...existing, ...normalized },
          },
        };
      }

      // New session: prepend to sidebar order so the most recent chat
      // appears at the top (matches backend sort order for the first
      // page of the sidebar).
      return {
        sessionsById: {
          ...state.sessionsById,
          [session.id]: normalized,
        },
        sidebarOrder: [session.id, ...state.sidebarOrder],
      };
    });
  },

  patchSession: (chatId, patch) => {
    set((state) => {
      const existing = state.sessionsById[chatId];

      // Even if the session is not in the global list, we may still
      // need to patch a matching entry inside a project cache. Guard
      // both cases so that patch operations from SSE and CRUD
      // propagate to every surface that shows the session.
      const normalizedPatch =
        patch.status !== undefined
          ? { ...patch, status: normalizeStatus(patch.status) }
          : patch;

      // Build a per-project patch map lazily: only clones project
      // arrays that actually contain this chatId.
      let patchedProjectCache: Record<string, ChatSession[]> | null = null;
      for (const [projectId, projectSessions] of Object.entries(
        state.projectSessionsCache,
      )) {
        const idx = projectSessions.findIndex((s) => s.id === chatId);
        if (idx === -1) continue;
        if (!patchedProjectCache) {
          patchedProjectCache = { ...state.projectSessionsCache };
        }
        const updatedArr = [...projectSessions];
        updatedArr[idx] = { ...updatedArr[idx], ...normalizedPatch };
        patchedProjectCache[projectId] = updatedArr;
      }

      const nextSessions = existing
        ? {
            ...state.sessionsById,
            [chatId]: { ...existing, ...normalizedPatch },
          }
        : state.sessionsById;

      if (!existing && !patchedProjectCache) return state;

      return {
        sessionsById: nextSessions,
        ...(patchedProjectCache
          ? { projectSessionsCache: patchedProjectCache }
          : {}),
      };
    });
  },

  updateSessionStatus: (chatId, jobId, status) => {
    set((state) => {
      const current = state.sessionsById[chatId];

      // The session may exist only in a project cache (e.g. the user
      // opened /projects/{id} without ever visiting /chat, so
      // `loadSidebarSessions` was never called). Handle both shapes.
      const inCache = current ?? findInProjectCache(state, chatId);

      if (!inCache) return state;

      // Stale-write protection (docs/state-contracts.md §7 stale write
      // guard). If the session is already bound to a different job,
      // this event belongs to a previous run and must not affect the
      // current one.
      if (inCache.job_id && inCache.job_id !== jobId) {
        return state;
      }

      // Terminal freeze.
      const currentNormalized = normalizeStatus(inCache.status);
      if (TERMINAL_STATUSES.has(currentNormalized)) {
        return state;
      }

      // No-op if the status has not changed.
      if (currentNormalized === status) {
        return state;
      }

      // Build the mutated record.
      const patchedSession = {
        ...inCache,
        status,
        updated_at: Date.now(),
      };

      // Patch the global list if present.
      const nextSessions = current
        ? {
            ...state.sessionsById,
            [chatId]: patchedSession,
          }
        : state.sessionsById;

      // Patch every project cache that contains this session.
      let nextProjectCache = state.projectSessionsCache;
      for (const [projectId, projectSessions] of Object.entries(
        state.projectSessionsCache,
      )) {
        const idx = projectSessions.findIndex((s) => s.id === chatId);
        if (idx === -1) continue;
        if (nextProjectCache === state.projectSessionsCache) {
          nextProjectCache = { ...state.projectSessionsCache };
        }
        const updatedArr = [...nextProjectCache[projectId]];
        updatedArr[idx] = patchedSession;
        nextProjectCache[projectId] = updatedArr;
      }

      return {
        sessionsById: nextSessions,
        ...(nextProjectCache !== state.projectSessionsCache
          ? { projectSessionsCache: nextProjectCache }
          : {}),
      };
    });
  },

  removeSession: (chatId) => {
    set((state) => {
      const hasGlobal = Boolean(state.sessionsById[chatId]);

      // Also check if the chat exists in any project cache. A chat
      // deleted from the history page must disappear from the project
      // detail view too.
      let hasInProject = false;
      for (const arr of Object.values(state.projectSessionsCache)) {
        if (arr.some((s) => s.id === chatId)) {
          hasInProject = true;
          break;
        }
      }

      if (!hasGlobal && !hasInProject) return state;

      const nextSessions = { ...state.sessionsById };
      delete nextSessions[chatId];

      let nextProjectCache = state.projectSessionsCache;
      if (hasInProject) {
        nextProjectCache = {};
        for (const [pid, arr] of Object.entries(state.projectSessionsCache)) {
          nextProjectCache[pid] = arr.filter((s) => s.id !== chatId);
        }
      }

      return {
        sessionsById: nextSessions,
        sidebarOrder: state.sidebarOrder.filter((id) => id !== chatId),
        ...(hasInProject ? { projectSessionsCache: nextProjectCache } : {}),
      };
    });
  },

  registerRun: (chatId, jobId, options) => {
    set((state) => {
      const current = state.sessionsById[chatId];

      if (current) {
        // Existing session: rebind to the new job and reset to `running`.
        // This is the ONLY place a terminal status may be reset, because
        // starting a new run on a completed chat is a legitimate action
        // (multi-turn conversation).
        return {
          sessionsById: {
            ...state.sessionsById,
            [chatId]: {
              ...current,
              job_id: jobId,
              status: "running",
              updated_at: Date.now(),
            },
          },
        };
      }

      // Session not in the store yet — create a minimal stub so that
      // immediate SSE events are captured even before `loadSidebarSessions`
      // completes. The next `upsertSession` from `GET /api/chats/{id}` will
      // enrich this stub with the authoritative fields.
      const stub: ChatSession = {
        id: chatId,
        title: options?.title || "New Session",
        job_id: jobId,
        prompt: options?.prompt || "",
        status: "running",
        created_at: Date.now(),
        updated_at: Date.now(),
        project_id: options?.projectId,
        agent_id: options?.agentId,
      };

      return {
        sessionsById: {
          ...state.sessionsById,
          [chatId]: stub,
        },
        sidebarOrder: [chatId, ...state.sidebarOrder],
      };
    });
  },

  // -------------------------------------------------------------------------
  // Network operations
  // -------------------------------------------------------------------------

  loadSidebarSessions: async (force = false) => {
    const state = get();

    // Idempotency guard: the `useEffect` in `AppDataBootstrap` runs twice
    // in React StrictMode, and tab focus may trigger another load. Neither
    // should cause a duplicate network request.
    if (state.isLoading) return;
    if (!force && state.isInitialized) return;

    set({ isLoading: true, error: null });

    try {
      const page = await fetchChatsPage({
        limit: clampChatsPageLimit(SIDEBAR_PAGE_SIZE),
        includeArchived: false,
      });

      const sessionsById: Record<string, ChatSession> = {};
      const sidebarOrder: string[] = [];

      for (const chat of page.items) {
        sessionsById[chat.id] = {
          ...chat,
          status: normalizeStatus(chat.status),
        };
        sidebarOrder.push(chat.id);
      }

      set({
        sessionsById,
        sidebarOrder,
        nextCursor: page.nextCursor,
        hasMore: page.hasMore,
        isInitialized: true,
        isLoading: false,
        error: null,
      });
    } catch (err) {
      // Failure must NOT clear existing data (docs/state-contracts.md §13.9).
      // Preserve everything; only report the error and release the loading flag.
      set({
        isLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to load sessions",
      });
    }
  },

  loadMoreSidebarSessions: async () => {
    const state = get();

    // Guard against concurrent calls and exhausted pagination.
    if (state.isLoading) return;
    if (!state.hasMore) return;
    if (!state.nextCursor) return;

    set({ isLoading: true, error: null });

    try {
      const page = await fetchChatsPage({
        limit: clampChatsPageLimit(SIDEBAR_PAGE_SIZE),
        cursor: state.nextCursor,
        includeArchived: false,
      });

      const nextSessions = { ...state.sessionsById };
      const nextOrder = [...state.sidebarOrder];

      for (const chat of page.items) {
        // Guard against duplicates that may have arrived while this call
        // was in flight (e.g. an `upsertSession` from a status update).
        if (!nextSessions[chat.id]) {
          nextSessions[chat.id] = {
            ...chat,
            status: normalizeStatus(chat.status),
          };
          nextOrder.push(chat.id);
        }
      }

      set({
        sessionsById: nextSessions,
        sidebarOrder: nextOrder,
        nextCursor: page.nextCursor,
        hasMore: page.hasMore,
        isLoading: false,
        error: null,
      });
    } catch (err) {
      set({
        isLoading: false,
        error:
          err instanceof Error
            ? err.message
            : "Failed to load more sessions",
      });
    }
  },

  // -------------------------------------------------------------------------
  // Project session loading
  //
  // Called by Sidebar when the user navigates to /projects/{projectId}.
  // Idempotent — once a project's sessions have been fetched, subsequent
  // calls are no-ops unless `force=true` is passed. This is the key
  // mechanism behind the "no unnecessary requests" requirement: opening
  // a project, navigating away, then coming back triggers exactly one
  // fetch on the very first visit.
  // -------------------------------------------------------------------------

  loadProjectSessions: async (projectId, force = false) => {
    if (!projectId) return;
    const state = get();

    // Idempotency guard.
    if (state.projectSessionsLoading[projectId]) return;
    if (!force && state.projectSessionsCache[projectId] !== undefined) return;

    set((s) => ({
      projectSessionsLoading: {
        ...s.projectSessionsLoading,
        [projectId]: true,
      },
      projectSessionsError: {
        ...s.projectSessionsError,
        [projectId]: null,
      },
    }));

    try {
      const page = await fetchChatsPage({
        projectId,
        limit: clampChatsPageLimit(SIDEBAR_PAGE_SIZE),
        includeArchived: false,
      });

      const normalized = page.items.map((c) => ({
        ...c,
        status: normalizeStatus(c.status),
      }));

      set((s) => ({
        projectSessionsCache: {
          ...s.projectSessionsCache,
          [projectId]: normalized,
        },
        projectSessionsCursor: {
          ...s.projectSessionsCursor,
          [projectId]: page.nextCursor,
        },
        projectSessionsHasMore: {
          ...s.projectSessionsHasMore,
          [projectId]: page.hasMore,
        },
        projectSessionsLoading: {
          ...s.projectSessionsLoading,
          [projectId]: false,
        },
      }));
    } catch (err) {
      set((s) => ({
        projectSessionsLoading: {
          ...s.projectSessionsLoading,
          [projectId]: false,
        },
        projectSessionsError: {
          ...s.projectSessionsError,
          [projectId]:
            err instanceof Error ? err.message : "Failed to load project sessions",
        },
      }));
    }
  },

  loadMoreProjectSessions: async (projectId) => {
    if (!projectId) return;
    const state = get();

    if (state.projectSessionsLoading[projectId]) return;
    if (!state.projectSessionsHasMore[projectId]) return;
    const cursor = state.projectSessionsCursor[projectId];
    if (!cursor) return;

    set((s) => ({
      projectSessionsLoading: {
        ...s.projectSessionsLoading,
        [projectId]: true,
      },
    }));

    try {
      const page = await fetchChatsPage({
        projectId,
        limit: clampChatsPageLimit(SIDEBAR_PAGE_SIZE),
        cursor,
        includeArchived: false,
      });

      set((s) => {
        const existing = s.projectSessionsCache[projectId] ?? [];
        const existingIds = new Set(existing.map((c) => c.id));
        const additions = page.items
          .filter((c) => !existingIds.has(c.id))
          .map((c) => ({ ...c, status: normalizeStatus(c.status) }));

        return {
          projectSessionsCache: {
            ...s.projectSessionsCache,
            [projectId]: [...existing, ...additions],
          },
          projectSessionsCursor: {
            ...s.projectSessionsCursor,
            [projectId]: page.nextCursor,
          },
          projectSessionsHasMore: {
            ...s.projectSessionsHasMore,
            [projectId]: page.hasMore,
          },
          projectSessionsLoading: {
            ...s.projectSessionsLoading,
            [projectId]: false,
          },
        };
      });
    } catch (err) {
      set((s) => ({
        projectSessionsLoading: {
          ...s.projectSessionsLoading,
          [projectId]: false,
        },
        projectSessionsError: {
          ...s.projectSessionsError,
          [projectId]:
            err instanceof Error
              ? err.message
              : "Failed to load more project sessions",
        },
      }));
    }
  },

  // -------------------------------------------------------------------------
  // Event application
  // -------------------------------------------------------------------------

  applyRunLifecycleEvent: (chatId, jobId, eventType, data) => {
    const nextStatus = mapLifecycleEvent(eventType, data);
    if (!nextStatus) return;
    get().updateSessionStatus(chatId, jobId, nextStatus);
  },

  clearAll: () => {
    // Wipes every session and every project cache. Used by the
    // History page's "Purge All Chats" action, which is a hard reset
    // of the entire chat surface (server-side deleteAllChats has
    // already run and the caller has confirmed the destructive
    // action).
    set({
      sessionsById: {},
      sidebarOrder: [],
      projectSessionsCache: {},
      projectSessionsCursor: {},
      projectSessionsHasMore: {},
      projectSessionsLoading: {},
      projectSessionsError: {},
      nextCursor: null,
      hasMore: false,
      isInitialized: false,
      error: null,
    });
  },

  invalidateProjectSessions: (projectId) => {
    // Marks a project's session cache as stale so the next
    // loadProjectSessions(projectId) call refetches. Used after a new
    // session is created inside a project, so the next visit to the
    // project detail page reflects it.
    set((state) => {
      if (!(projectId in state.projectSessionsCache)) return state;
      const nextCache = { ...state.projectSessionsCache };
      delete nextCache[projectId];
      const nextCursor = { ...state.projectSessionsCursor };
      delete nextCursor[projectId];
      const nextHasMore = { ...state.projectSessionsHasMore };
      delete nextHasMore[projectId];
      return {
        projectSessionsCache: nextCache,
        projectSessionsCursor: nextCursor,
        projectSessionsHasMore: nextHasMore,
      };
    });
  },

  // -------------------------------------------------------------------------
  // Test helper
  // -------------------------------------------------------------------------

  _reset: () => {
    set({
      sessionsById: {},
      sidebarOrder: [],
      isLoading: false,
      isInitialized: false,
      error: null,
      nextCursor: null,
      hasMore: false,
      projectSessionsCache: {},
      projectSessionsCursor: {},
      projectSessionsHasMore: {},
      projectSessionsLoading: {},
      projectSessionsError: {},
    });
  },
}));


// ---------------------------------------------------------------------------
// Internal helper — find a session by id across all project caches.
//
// Used by `updateSessionStatus` and `patchSession` when the session is
// not in the global sidebar list. Returns `undefined` when not found
// in any cache.
// ---------------------------------------------------------------------------

function findInProjectCache(
  state: SessionState,
  chatId: string,
): ChatSession | undefined {
  for (const projectSessions of Object.values(state.projectSessionsCache)) {
    const found = projectSessions.find((s) => s.id === chatId);
    if (found) return found;
  }
  return undefined;
}
