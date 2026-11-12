// src/features/memory/hooks/useMemoryMode.ts

/**
 * Read the current memory mode and derive the UI gates that
 * depend on it.
 *
 * The hook returns both the raw `MemoryMode` value (from
 * `useMemorySettings`) AND boolean derivatives that a
 * component typically needs. Having the derivatives computed
 * once means every caller agrees on what each mode implies --
 * no `mode === 'read_write'` scattered across 20 files that
 * can drift out of sync with the spec's policy table.
 *
 * Gate semantics (spec §1.3)
 * --------------------------
 *
 *   * **canRead**       -- `read_only` or `read_write`.
 *     False only when `disabled`.
 *
 *   * **canWrite**      -- `read_write` only.
 *
 *   * **canImport**     -- `read_only` or `read_write`.
 *     Import is a write, but the spec (§1.3) treats it as a
 *     permitted write in READ_ONLY mode via a special
 *     exemption. The backend enforces this; the UI gate
 *     matches.
 *
 *   * **isDisabled**    -- convenience, `mode === 'disabled'`.
 *
 *   * **isLoading**     -- the settings query has not
 *     resolved yet. Components that would gate on `canWrite`
 *     should treat `isLoading` as "don't show write UI yet"
 *     rather than assuming read-only.
 *
 * Before the query resolves, all gates are `false` and
 * `isLoading` is `true`. This fails CLOSED: no accidental
 * writes during the initial paint, no flash of "you can
 * write" that then flips to "you cannot".
 */

'use client';

import { useMemorySettings } from '../api/queries';
import type { MemoryMode } from '../types/enums';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface MemoryModeGates {
  /** The raw mode, or `null` while the settings query is loading. */
  mode: MemoryMode | null;

  /** `true` while the settings query has not resolved yet. */
  isLoading: boolean;

  /** Any form of read is allowed (`read_only` or `read_write`). */
  canRead: boolean;

  /** Full write access is allowed (`read_write` only). */
  canWrite: boolean;

  /** Import is allowed (`read_only` or `read_write`). */
  canImport: boolean;

  /** The subsystem is entirely off (`disabled`). */
  isDisabled: boolean;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useMemoryMode(): MemoryModeGates {
  const settingsQuery = useMemorySettings();
  const mode = settingsQuery.data?.mode ?? null;
  const isLoading = settingsQuery.isLoading;

  return {
    mode,
    isLoading,
    canRead: mode === 'read_only' || mode === 'read_write',
    canWrite: mode === 'read_write',
    canImport: mode === 'read_only' || mode === 'read_write',
    isDisabled: mode === 'disabled',
  };
}
