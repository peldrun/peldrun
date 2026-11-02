// Path: frontend/src/stores/project-store.ts
"use client";

/**
 * Project Store — single source of truth for project metadata.
 *
 * Owns the canonical in-memory representation of every project the
 * frontend currently displays. The sidebar and the projects page both
 * consume this store; no other component is allowed to hold a local
 * copy of the projects list.
 *
 * Boundary (docs/state-contracts.md §6.2)
 * ---------------------------------------
 *   This store owns ONLY project metadata:
 *     - id, name, description, created_at.
 *   It does NOT own:
 *     - chat sessions belonging to a project (Session Store).
 *     - run state for any chat (Stream Store).
 *     - active UI selection state (local to the consuming component).
 *
 * Design decisions
 * ----------------
 *
 * 1. **Raw data, filtered display.**
 *    The store holds every project the backend returns — INCLUDING the
 *    reserved ``default_project``. Consumers filter it out themselves
 *    via `getOrderedProjects().filter(p => p.id !== "default_project")`.
 *    This matches the current sidebar behavior and keeps the store free
 *    of display-specific policy.
 *
 * 2. **Failure does NOT clear data.**
 *    A network failure preserves the last known projects so the user
 *    can keep working. Contract §13.9.
 *
 * 3. **Empty-result disambiguation.**
 *    The current `fetchProjects()` in ``chatsApi.ts`` returns `[]` on
 *    BOTH "no projects exist" AND "network failure". Until Phase C
 *    fixes that contract (plan §8.1), this store defensively preserves
 *    existing data when a non-forced load returns an empty array while
 *    the store already has projects. A ``force=true`` load honors the
 *    empty result (useful after an explicit user-triggered refresh).
 *
 * 4. **Idempotent initial load.**
 *    `loadProjects()` is a no-op after the first successful load unless
 *    ``force=true`` is passed. This makes it safe to call from
 *    `AppDataBootstrap` (which runs twice in React StrictMode) without
 *    triggering duplicate network requests.
 *
 * 5. **Append order, not prepend.**
 *    New projects are appended to `projectOrder` to match the backend's
 *    insertion order in ``index.json`` (see ``create_project`` in
 *    ``project_manager.py``: ``index.setdefault("projects", []).append(meta)``).
 *    Prepending would create a visual mismatch between the API response
 *    order (used by ``getProject`` in the backend) and the sidebar order.
 */

import { create } from "zustand";

import { Project, fetchProjects } from "@/lib/chatsApi";


/** The reserved project id. Always present in the backend response. */
export const DEFAULT_PROJECT_ID = "default_project";


// ---------------------------------------------------------------------------
// Store interface
// ---------------------------------------------------------------------------

interface ProjectState {
  // Data
  projectsById: Record<string, Project>;
  projectOrder: string[];

  // Loading state
  isLoading: boolean;
  isInitialized: boolean;
  error: string | null;

  // Read helpers
  getProject: (projectId: string) => Project | undefined;
  getOrderedProjects: () => Project[];

  // Write operations
  upsertProject: (project: Project) => void;
  removeProject: (projectId: string) => void;
  setProjects: (projects: Project[]) => void;

  // Network operations
  loadProjects: (force?: boolean) => Promise<void>;
  invalidateProjects: () => void;

  // Test/diagnostic helper — clears all state.
  _reset: () => void;
}


// ---------------------------------------------------------------------------
// Store implementation
// ---------------------------------------------------------------------------

export const useProjectStore = create<ProjectState>((set, get) => ({
  // Initial state
  projectsById: {},
  projectOrder: [],
  isLoading: false,
  isInitialized: false,
  error: null,

  // -------------------------------------------------------------------------
  // Read helpers
  // -------------------------------------------------------------------------

  getProject: (projectId) => get().projectsById[projectId],

  getOrderedProjects: () => {
    const state = get();
    const result: Project[] = [];
    for (const id of state.projectOrder) {
      const project = state.projectsById[id];
      if (project) result.push(project);
    }
    return result;
  },

  // -------------------------------------------------------------------------
  // Write operations
  // -------------------------------------------------------------------------

  upsertProject: (project) => {
    set((state) => {
      const existing = state.projectsById[project.id];

      if (existing) {
        // Merge: prefer fields from the incoming project, keep any
        // missing optional fields from the existing record.
        return {
          projectsById: {
            ...state.projectsById,
            [project.id]: { ...existing, ...project },
          },
        };
      }

      // New project: append to match backend insertion order.
      return {
        projectsById: {
          ...state.projectsById,
          [project.id]: project,
        },
        projectOrder: [...state.projectOrder, project.id],
      };
    });
  },

  removeProject: (projectId) => {
    set((state) => {
      if (!state.projectsById[projectId]) return state;
      const nextProjects = { ...state.projectsById };
      delete nextProjects[projectId];
      return {
        projectsById: nextProjects,
        projectOrder: state.projectOrder.filter((id) => id !== projectId),
      };
    });
  },

  setProjects: (projects) => {
    const projectsById: Record<string, Project> = {};
    const projectOrder: string[] = [];
    for (const p of projects) {
      // Guard against duplicate ids in the incoming list (backend
      // corruption / corrupted index.json recovery). Keep the first.
      if (projectsById[p.id]) continue;
      projectsById[p.id] = p;
      projectOrder.push(p.id);
    }
    set({
      projectsById,
      projectOrder,
      isInitialized: true,
      error: null,
    });
  },

  // -------------------------------------------------------------------------
  // Network operations
  // -------------------------------------------------------------------------

  loadProjects: async (force = false) => {
    const state = get();

    // Idempotency: skip if already loading, or if already initialized
    // and not forced. The `AppDataBootstrap` effect runs twice under
    // React StrictMode; this guard makes it a single network call.
    if (state.isLoading) return;
    if (!force && state.isInitialized) return;

    set({ isLoading: true, error: null });

    try {
      const projects = await fetchProjects();

      // Empty-result disambiguation (see header comment #3).
      //
      // `fetchProjects` currently returns `[]` on network failure as
      // well as on "no projects exist". A non-forced load that returns
      // empty while we already have data is treated as a probable
      // network glitch: preserve state and surface a soft error so the
      // UI can show "using cached data".
      //
      // A forced load honors the empty result — the user explicitly
      // asked for a fresh fetch and deserves an honest response even
      // if it means an empty list.
      //
      // NOTE: This heuristic is temporary. Once Phase C fixes the API
      // contract so that failures throw an ApiError, this branch can
      // be removed and the catch block below will handle all failure
      // paths uniformly.
      const hadProjects = Object.keys(state.projectsById).length > 0;
      const isEmpty = projects.length === 0;
      if (isEmpty && hadProjects && !force) {
        set({
          isLoading: false,
          isInitialized: true,
          error: "Failed to refresh projects — using cached data.",
        });
        return;
      }

      const projectsById: Record<string, Project> = {};
      const projectOrder: string[] = [];
      for (const p of projects) {
        if (projectsById[p.id]) continue;
        projectsById[p.id] = p;
        projectOrder.push(p.id);
      }

      set({
        projectsById,
        projectOrder,
        isLoading: false,
        isInitialized: true,
        error: null,
      });
    } catch (err) {
      // Failure must NOT clear existing data (docs/state-contracts.md §13.9).
      set({
        isLoading: false,
        error: err instanceof Error ? err.message : "Failed to load projects",
      });
    }
  },

  invalidateProjects: () => {
    // Mark the store as stale so the next `loadProjects()` refetches.
    // Does NOT clear the data — the current UI keeps working while a
    // background refresh is in flight.
    set({ isInitialized: false });
  },

  // -------------------------------------------------------------------------
  // Test helper
  // -------------------------------------------------------------------------

  _reset: () => {
    set({
      projectsById: {},
      projectOrder: [],
      isLoading: false,
      isInitialized: false,
      error: null,
    });
  },
}));
