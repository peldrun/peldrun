// src/features/memory/hooks/useEntryDraft.ts

/**
 * Manage the lifecycle of a memory-entry draft.
 *
 * The `useMemoryDraftStore` (Zustand) owns the raw state; this
 * hook wires that state to a component's lifecycle:
 *
 *   * On mount, `createDraft(key, initial)` -- idempotent, so
 *     React 19 StrictMode's double-mount is safe.
 *   * On unmount, `destroyDraft(key)` -- prevents a stale
 *     draft from leaking into a future editor session.
 *   * Reads via the store selector so the component re-renders
 *     only when THIS draft changes, not when other drafts do.
 *
 * The hook is designed for `EntryEditor` and any future
 * create/edit form. `EntryEditor` currently manages its own
 * form state with `useState`; migrating it to this hook is a
 * F9 polish item, once a second caller validates the API.
 *
 * Key generation
 * --------------
 *
 * The `key` argument is a caller-supplied string. The standard
 * convention (documented on `useMemoryDraftStore`) is:
 *
 *   * `new:<scope_type>:<scope_id>` -- create mode.
 *   * `edit:<scope_type>:<scope_id>:<memory_id>` -- edit mode.
 *
 * The hook does not generate keys -- passing the key explicitly
 * lets a parent component create two editors for two different
 * entries without either overwriting the other.
 */

'use client';

import { useEffect } from 'react';

import type { MemoryEntryDraft } from '../types/entry';
import { useMemoryDraftStore } from '../stores/memory-draft-store';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface UseEntryDraftOptions {
  /**
   * The draft's unique key. MUST be stable across re-renders
   * (compute it once, e.g. via `useMemo`).
   */
  key: string;

  /**
   * The initial draft values. Captured once, when the draft is
   * first created. Subsequent renders of `initial` are IGNORED
   * -- the baseline for dirty detection never changes. To
   * reset to the initial values, call `reset()`.
   */
  initial: MemoryEntryDraft;

  /**
   * When `false`, the hook does NOT create or destroy the
   * draft. Use this to render an editor that should not have
   * its draft cleaned up on unmount (e.g. an embedded editor
   * inside a page that re-mounts). Defaults to `true`.
   */
  lifecycle?: boolean;
}

export interface EntryDraftResult {
  draft: MemoryEntryDraft | undefined;
  isDirty: boolean;
  update: (patch: Partial<MemoryEntryDraft>) => void;
  reset: () => void;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useEntryDraft(
  options: UseEntryDraftOptions,
): EntryDraftResult {
  const { key, initial, lifecycle = true } = options;

  // Subscriptions -- each selector returns a primitive or a
  // stable reference, so re-renders happen only on relevant
  // state changes.
  const draft = useMemoryDraftStore((s) => s.drafts.get(key)?.draft);
  const dirty = useMemoryDraftStore((s) => s.isDirty(key));

  // Lifecycle: create on mount, destroy on unmount.
  useEffect(() => {
    if (!lifecycle) return undefined;
    useMemoryDraftStore.getState().createDraft(key, initial);
    return () => {
      useMemoryDraftStore.getState().destroyDraft(key);
    };
    // Intentionally NOT depending on `initial` -- creating a
    // draft twice with different initial values would either
    // be a no-op (if it already exists) or silently fail to
    // update. The convention is: the key uniquely identifies
    // the draft's LIFECYCLE, so a new initial value means a
    // new key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, lifecycle]);

  return {
    draft,
    isDirty: dirty,
    update: (patch) =>
      useMemoryDraftStore.getState().updateDraft(key, patch),
    reset: () => useMemoryDraftStore.getState().resetDraft(key),
  };
}
