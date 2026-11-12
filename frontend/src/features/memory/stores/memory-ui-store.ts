// src/features/memory/stores/memory-ui-store.ts

/**
 * Zustand store for memory UI state.
 *
 * Scope of this store
 * -------------------
 *
 * This store holds UI state that:
 *
 *   1. Passes through more than two component layers (the
 *      spec's "no prop drilling > 2 levels" rule).
 *
 *   2. Must survive cross-page navigation (filters set on the
 *      browse page should persist if the user clicks into a
 *      detail and comes back).
 *
 *   3. Is NOT server data. Server data lives in TanStack Query
 *      and is never duplicated here -- a duplicate would be
 *      stale after the first mutation, and there is no clean
 *      way to keep it in sync.
 *
 * What belongs here
 * -----------------
 *
 *   * The browse page's filter state.
 *   * The currently selected scope (sidebar).
 *   * The set of entry ids selected for bulk operations.
 *
 * What does NOT belong here
 * -------------------------
 *
 *   * Settings, entries, projects, usage data -- TanStack
 *     Query.
 *
 *   * Modal open/closed state -- local `useState` is right
 *     when only one component cares.
 *
 *   * Form field values while editing -- the draft store
 *     (`memory-draft-store.ts`) handles those, because they
 *     have a different lifecycle (reset-on-cancel, dirty
 *     tracking) and much higher change frequency.
 *
 * Set serialization warning
 * -------------------------
 *
 * `selected_ids` is a `Set<string>`. Zustand's change
 * detection uses `Object.is` on the top-level state object
 * and, via selectors, on the selected slice. Every mutation
 * MUST create a NEW `Set` instance -- in-place mutation
 * (`set.add(id)`) would not trigger a re-render. The actions
 * below follow this rule; a caller that reads
 * `useMemoryUIStore.getState().selected_ids` and mutates it
 * directly is on their own.
 */

import { create } from 'zustand';

import type { MemoryType } from '../types/enums';
import type { MemoryScopeRef } from '../types/entry';
import { parseScopeRef } from '../types/entry';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * The browse-page filter set.
 *
 * Every field has a "no filter" default (`'all'`, `null`, `''`)
 * so an empty filter object is a valid state that the API
 * accepts without special-casing. `search_query` is reserved
 * for a future client-side filter; it is not currently wired
 * into any request.
 */
export interface BrowseFilters {
  status: 'all' | 'active';
  memory_type: MemoryType | null;
  is_pinned: boolean | null;
  search_query: string;
}

/**
 * The default filter set -- matches what the browse page shows
 * on first navigation. Exported so tests and the "Clear all"
 * button can reference it without duplicating literals.
 */
export const DEFAULT_FILTERS: BrowseFilters = {
  status: 'all',
  memory_type: null,
  is_pinned: null,
  search_query: '',
};

/**
 * The store's shape.
 *
 * Split into STATE (top) and ACTIONS (bottom) to make the
 * surfaces obvious. Actions are pure mutations of state with
 * no side effects -- no async, no fetch calls, no navigation.
 * That keeps the store trivially testable and prevents it from
 * becoming an application-object with hidden behavior.
 */
interface MemoryUIState {
  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------

  /** The current browse filters. */
  filters: BrowseFilters;

  /**
   * The scope currently selected in the sidebar.
   *
   * `null` means "no scope selected yet" -- the browse page
   * disables the list query in that state. Set to an explicit
   * `{ scope_type, scope_id }` when the user picks a scope or
   * arrives via a URL that includes `?scope=...`.
   */
  active_scope: MemoryScopeRef | null;

  /**
   * The set of entry ids selected for bulk operations.
   *
   * Owned by the browse page (and, in the future, any other
   * page that supports multi-select). Automatically cleared by
   * mutations that delete or archive the selected entries --
   * this store does NOT observe those mutations, so the calling
   * component is responsible for calling `clearSelection()`
   * after a successful bulk operation.
   */
  selected_ids: Set<string>;

  // ---------------------------------------------------------------------------
  // Filter actions
  // ---------------------------------------------------------------------------

  /**
   * Set one filter field.
   *
   * Typed so `setFilter('status', 'active')` compiles but
   * `setFilter('status', 42)` does not. The `key` argument
   * constrains `value` to that key's type -- TypeScript infers
   * the relation from `BrowseFilters`.
   */
  setFilter: <K extends keyof BrowseFilters>(
    key: K,
    value: BrowseFilters[K],
  ) => void;

  /** Merge a partial filter patch into the current filters. */
  setFilters: (patch: Partial<BrowseFilters>) => void;

  /** Reset every filter to `DEFAULT_FILTERS`. Does not touch selection or scope. */
  resetFilters: () => void;

  // ---------------------------------------------------------------------------
  // Scope actions
  // ---------------------------------------------------------------------------

  /**
   * Set the active scope from a structured ref, or `null` to
   * clear it.
   */
  setActiveScope: (scope: MemoryScopeRef | null) => void;

  /**
   * Set the active scope from its wire string (`"<type>:<id>"`),
   * e.g. when reading a `?scope=project:acme` URL parameter.
   *
   * A malformed string is treated as `null` (clear the scope)
   * rather than throwing -- the browse page should fall back to
   * "pick a scope" if the URL param is garbage, not crash.
   */
  setActiveScopeByString: (scopeString: string | null | undefined) => void;

  // ---------------------------------------------------------------------------
  // Selection actions
  // ---------------------------------------------------------------------------

  /** Toggle one id in the selection set. */
  toggleSelection: (id: string) => void;

  /** Replace the selection set with the provided ids. */
  selectAll: (ids: readonly string[]) => void;

  /** Empty the selection set. */
  clearSelection: () => void;

  /** `true` iff `id` is in the selection set. */
  isSelected: (id: string) => boolean;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

/**
 * The Zustand store hook.
 *
 * Usage:
 *
 *     // Subscribe to a single slice (avoids re-renders when
 *     // unrelated state changes):
 *     const status = useMemoryUIStore((s) => s.filters.status);
 *
 *     // Subscribe to an action (stable reference -- never
 *     // changes, so no re-renders):
 *     const setFilter = useMemoryUIStore((s) => s.setFilter);
 *
 *     // Read imperatively (e.g. in an event handler where the
 *     // latest state matters, not the render-time snapshot):
 *     const filters = useMemoryUIStore.getState().filters;
 *
 * `create<MemoryUIState>()(...)` -- the extra `()` -- is
 * required by Zustand v5's TypeScript signature for middleware
 * compatibility; without it, the returned hook's type loses
 * `getState` / `setState` in some IDE setups.
 */
export const useMemoryUIStore = create<MemoryUIState>()((set, get) => ({
  // -------------------------------------------------------------------------
  // Initial state
  // -------------------------------------------------------------------------
  filters: { ...DEFAULT_FILTERS },
  active_scope: null,
  selected_ids: new Set<string>(),

  // -------------------------------------------------------------------------
  // Filter actions
  // -------------------------------------------------------------------------

  setFilter: (key, value) => {
    set((state) => ({
      filters: { ...state.filters, [key]: value },
    }));
  },

  setFilters: (patch) => {
    set((state) => ({
      filters: { ...state.filters, ...patch },
    }));
  },

  resetFilters: () => {
    set({ filters: { ...DEFAULT_FILTERS } });
  },

  // -------------------------------------------------------------------------
  // Scope actions
  // -------------------------------------------------------------------------

  setActiveScope: (scope) => {
    set({ active_scope: scope });
  },

  setActiveScopeByString: (scopeString) => {
    // `parseScopeRef` returns `null` for malformed inputs, so
    // a garbage URL param clears the scope rather than throwing.
    const parsed = parseScopeRef(scopeString ?? null);
    set({ active_scope: parsed });
  },

  // -------------------------------------------------------------------------
  // Selection actions
  // -------------------------------------------------------------------------

  toggleSelection: (id) => {
    set((state) => {
      // New Set every time -- Zustand's Object.is comparison
      // requires a new reference to detect the change.
      const next = new Set(state.selected_ids);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return { selected_ids: next };
    });
  },

  selectAll: (ids) => {
    set({ selected_ids: new Set(ids) });
  },

  clearSelection: () => {
    // Fresh empty set -- `Object.is(new Set(), new Set())` is
    // false, so subscribers re-render even when they held an
    // empty set before. That's the correct behavior: a
    // "clear" button should always take effect, even if the
    // selection was already empty (removes the "wait, did it
    // work?" hesitation).
    set({ selected_ids: new Set() });
  },

  isSelected: (id) => {
    return get().selected_ids.has(id);
  },
}));
