// src/features/memory/stores/memory-draft-store.ts

/**
 * Zustand store for in-progress memory entry drafts.
 *
 * Why a separate store from `memory-ui-store.ts`
 * -------------------------------------------------
 *
 * The UI store holds long-lived session state (filters, active
 * scope, selection). Drafts have a fundamentally different
 * lifecycle:
 *
 *   * They are created on editor mount and destroyed on editor
 *     unmount. A stale draft must NEVER leak into a new editor
 *     session -- "I opened the editor and it still shows the
 *     text I typed yesterday" is a memorable bug for users.
 *
 *   * Their change frequency is very high (every keystroke
 *     updates `content`). Mixing this with filter state would
 *     force every keystroke to re-render the browse page's
 *     filter UI (because Zustand selectors are per-hook, but
 *     the store instance is shared -- a filter subscriber that
 *     reads `filters` still re-runs its selector on every
 *     store change, and if the selector returns a new
 *     reference due to any shared state, it re-renders).
 *
 *   * Their "dirty" determination requires a snapshot of the
 *     initial values, which has no analogue in the UI store.
 *
 * Separate stores keep these concerns isolated. React's
 * reconciliation then re-renders only the components that
 * subscribe to a slice that actually changed.
 *
 * Draft identity
 * --------------
 *
 * Every draft is keyed by a `draftKey: string`. The convention
 * (not enforced by the store, but followed by `EntryEditor`)
 * is:
 *
 *   * ``new:<scope_type>:<scope_id>`` -- a fresh entry in the
 *     given scope. Two editors opened for two different scopes
 *     get two different drafts (never share).
 *
 *   * ``edit:<scope_type>:<scope_id>:<memory_id>`` -- editing
 *     an existing entry. The memory_id distinguishes two open
 *     edit editors for different entries in the same scope.
 *
 * The store is agnostic about the key's structure -- any unique
 * string works. Using the above convention makes debugging
 * easier (the key itself says what the draft is for) and
 * prevents the classic "shared draft" bug where two editors
 * opened simultaneously overwrite each other's state.
 *
 * Lifecycle
 * ---------
 *
 *     const draftKey = `new:${scopeType}:${scopeId}`;
 *     const draft = useMemoryDraftStore((s) =>
 *       s.drafts.get(draftKey)?.draft,
 *     );
 *     useEffect(() => {
 *       useMemoryDraftStore.getState().createDraft(draftKey, initial);
 *       return () => {
 *         useMemoryDraftStore.getState().destroyDraft(draftKey);
 *       };
 *     }, [draftKey]);
 *
 * `createDraft` is idempotent -- if a draft already exists for
 * the key (e.g. React 19 StrictMode double-mount), it is left
 * unchanged. This is deliberate: a React dev-mode double-mount
 * must not lose the user's in-progress typing on the second
 * mount.
 *
 * Not persisted
 * -------------
 *
 * Drafts live in memory only. A page refresh loses them, and
 * this is correct:
 *
 *   * A draft that survived across sessions would be a
 *     surprising state to encounter ("why is there text
 *     already?").
 *
 *   * The set of drafts grows unboundedly in localStorage
 *     unless a TTL/cap policy is added -- complexity that
 *     outweighs the benefit.
 *
 *   * The draft is short-lived by design (a user typing a
 *     memory entry in the editor).
 *
 * If persistence becomes a real product requirement, it belongs
 * in a wrapper around this store, not in the store itself --
 * the store's API (get/update/reset/destroy) does not change.
 */

import { create } from 'zustand';

import type { MemoryEntryDraft } from '../types/entry';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * A draft and its initial snapshot.
 *
 * `initial` is captured when the draft is CREATED and never
 * changes. It is the "clean" reference for dirty detection --
 * "dirty" means "differs from this exact snapshot", not
 * "differs from the last save".
 *
 * This distinction matters for the "edit an entry, change it,
 * change it back" case: a naive `lastSaved` model would call
 * the entry dirty (because the intermediate change happened),
 * but the user's intent is "I want to save this back to
 * exactly what it was". Comparing to `initial` correctly
 * reports the entry as clean.
 */
interface DraftRecord {
  draft: MemoryEntryDraft;
  initial: MemoryEntryDraft;
}

/**
 * The store's shape.
 *
 * `drafts` is a `Map` (not a plain object) so that:
 *   * `.get(key)` is O(1) without prototype chain lookups,
 *   * arbitrary keys (including `__proto__`, `constructor`,
 *     etc.) cannot collide with object internals,
 *   * iteration order is insertion order (some code paths
 *     rely on "the most recently created draft is last").
 *
 * The `Map` is treated as immutable -- every action that
 * mutates it creates a NEW `Map` and sets it on the store.
 * In-place mutation would not trigger re-renders (Zustand uses
 * `Object.is` on the returned state slice).
 */
interface MemoryDraftState {
  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------

  /** The map of active drafts, keyed by `draftKey`. */
  drafts: Map<string, DraftRecord>;

  // ---------------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------------

  /**
   * Create a draft at `key` with the given initial values.
   *
   * Idempotent: if a draft already exists at `key`, it is left
   * untouched and this is a no-op. Callers that WANT to reset
   * an existing draft should call `resetDraft` explicitly.
   *
   * `initial` must be a fresh object (the store does not clone
   * it). Passing a reference that the caller later mutates
   * would corrupt the dirty-detection baseline. The types
   * system cannot enforce this; the convention is documented
   * here and followed by `EntryEditor`.
   */
  createDraft: (key: string, initial: MemoryEntryDraft) => void;

  /**
   * Merge a partial update into the draft at `key`.
   *
   * No-op if the draft does not exist -- this is a defensive
   * choice: a caller racing the editor's `destroyDraft` (e.g.
   * a debounced search firing after unmount) should not throw,
   * and should not silently resurrect a destroyed draft.
   */
  updateDraft: (key: string, patch: Partial<MemoryEntryDraft>) => void;

  /**
   * Reset the draft at `key` back to its `initial` snapshot.
   *
   * Used by the "Revert changes" button. The draft record stays
   * in the map (so the editor does not need to re-`create`).
   */
  resetDraft: (key: string) => void;

  /**
   * Remove the draft at `key` entirely.
   *
   * Called from the editor's `useEffect` cleanup. Idempotent --
   * removing a nonexistent key is a no-op.
   */
  destroyDraft: (key: string) => void;

  /**
   * Return `true` iff the draft at `key` differs from its
   * initial snapshot in any field that AFFECTS the saved
   * value.
   *
   * `contentTouched` is deliberately excluded: it is a
   * UI-only flag (whether the user has typed in the textarea),
   * not part of what gets sent to the API. Including it would
   * mark a pristine draft dirty the moment the user clicks in
   * the textarea without typing anything.
   *
   * Returns `false` for a nonexistent key -- a missing draft
   * cannot be dirty.
   */
  isDirty: (key: string) => boolean;

  /**
   * Return the current draft record at `key`, or `undefined`
   * if no draft exists.
   *
   * Prefer the `useMemoryDraftStore((s) => s.drafts.get(key))`
   * selector for subscriptions -- this action is for imperative
   * reads in event handlers, where the latest state (not the
   * render-time snapshot) matters.
   */
  getDraft: (key: string) => DraftRecord | undefined;
}

// ---------------------------------------------------------------------------
// Dirty-detection helper
// ---------------------------------------------------------------------------

/**
 * Deep-compare two drafts on the fields that PARTICIPATE in a
 * save.
 *
 * Excludes `contentTouched` (UI-only). All other fields are
 * compared by value; `tags` is the only array and is compared
 * element-by-element.
 *
 * The comparison is intentionally strict about arrays (order
 * matters): `['a', 'b']` and `['b', 'a']` are treated as
 * DIFFERENT. Rationale: the API stores tags in the given order,
 * and the displayed order matches the stored order. A user who
 * reorders tags intends to save the new order -- treating the
 * reorder as "not dirty" would silently discard their intent.
 */
function draftsDiffer(a: MemoryEntryDraft, b: MemoryEntryDraft): boolean {
  if (a.content !== b.content) return true;
  if (a.memory_type !== b.memory_type) return true;
  if (a.scope_type !== b.scope_type) return true;
  if (a.scope_id !== b.scope_id) return true;
  if (a.importance !== b.importance) return true;
  if (a.is_pinned !== b.is_pinned) return true;
  if (a.tags.length !== b.tags.length) return true;
  for (let i = 0; i < a.tags.length; i++) {
    if (a.tags[i] !== b.tags[i]) return true;
  }
  // `contentTouched` is intentionally not compared.
  return false;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useMemoryDraftStore = create<MemoryDraftState>()((set, get) => ({
  // -------------------------------------------------------------------------
  // Initial state
  // -------------------------------------------------------------------------
  drafts: new Map(),

  // -------------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------------

  createDraft: (key, initial) => {
    set((state) => {
      if (state.drafts.has(key)) {
        // Idempotent: leave the existing draft untouched.
        // Returning the SAME state reference avoids an
        // unnecessary re-render on React 19 StrictMode's
        // double-mount.
        return state;
      }
      const next = new Map(state.drafts);
      next.set(key, {
        // Clone both sides so a caller that mutates its
        // `initial` argument (unusual, but possible in a
        // draft-then-edit flow) cannot corrupt our baseline.
        draft: { ...initial, tags: [...initial.tags] },
        initial: { ...initial, tags: [...initial.tags] },
      });
      return { drafts: next };
    });
  },

  updateDraft: (key, patch) => {
    set((state) => {
      const record = state.drafts.get(key);
      if (!record) {
        // No-op for a missing key. See the type's JSDoc for
        // the reasoning.
        return state;
      }
      // Clone the tags array if the patch supplies one, so
      // that the caller's array is not aliased into our
      // state (the caller may reuse or mutate it).
      const nextDraft: MemoryEntryDraft = {
        ...record.draft,
        ...patch,
        tags: patch.tags ? [...patch.tags] : record.draft.tags,
      };
      const next = new Map(state.drafts);
      next.set(key, { draft: nextDraft, initial: record.initial });
      return { drafts: next };
    });
  },

  resetDraft: (key) => {
    set((state) => {
      const record = state.drafts.get(key);
      if (!record) return state;
      const next = new Map(state.drafts);
      next.set(key, {
        draft: { ...record.initial, tags: [...record.initial.tags] },
        initial: record.initial,
      });
      return { drafts: next };
    });
  },

  destroyDraft: (key) => {
    set((state) => {
      if (!state.drafts.has(key)) return state;
      const next = new Map(state.drafts);
      next.delete(key);
      return { drafts: next };
    });
  },

  isDirty: (key) => {
    const record = get().drafts.get(key);
    if (!record) return false;
    return draftsDiffer(record.draft, record.initial);
  },

  getDraft: (key) => get().drafts.get(key),
}));
