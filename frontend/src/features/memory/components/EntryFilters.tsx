// src/features/memory/components/EntryFilters.tsx

/**
 * The browse page's filter panel.
 *
 * Reads and writes the filter state via `useMemoryUIStore`, so
 * any component in the tree can read the current filters with
 * the same hook. The panel itself is stateless -- there is no
 * local mirror, no "apply" button. Every change hits the store
 * immediately and the parent list re-queries.
 *
 * Layout
 * ------
 *
 * Designed for a sidebar (`w-full` inside a `md:w-64` column)
 * OR as an inline horizontal bar. The `orientation` prop
 * switches between the two; both share the same field set.
 *
 * "Clear all" is disabled when no filter differs from
 * `DEFAULT_FILTERS` -- clicking it while everything is already
 * cleared would be a no-op, and disabling it makes that
 * explicit.
 */

'use client';

import { useTranslation } from 'react-i18next';
import { Filter, X } from 'lucide-react';

import {
  ALL_MEMORY_TYPES,
  MemoryType,
} from '../types/enums';
import {
  DEFAULT_FILTERS,
  useMemoryUIStore,
  type BrowseFilters,
} from '../stores/memory-ui-store';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface EntryFiltersProps {
  /** Layout orientation. Defaults to `'vertical'`. */
  orientation?: 'vertical' | 'horizontal';

  /**
   * When `true`, hides the "Clear all" button (e.g. when the
   * panel is rendered in a compact toolbar without room).
   */
  hideClear?: boolean;

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function EntryFilters({
  orientation = 'vertical',
  hideClear = false,
  className,
}: EntryFiltersProps) {
  const { t } = useTranslation('memory');
  const filters = useMemoryUIStore((s) => s.filters);
  const setFilter = useMemoryUIStore((s) => s.setFilter);
  const resetFilters = useMemoryUIStore((s) => s.resetFilters);

  const isDefault = filtersEqual(filters, DEFAULT_FILTERS);
  const isHorizontal = orientation === 'horizontal';

  return (
    <div
      className={cn(
        'text-xs',
        isHorizontal
          ? 'flex flex-wrap items-center gap-3'
          : 'space-y-3 rounded-lg border border-border bg-card p-3',
        className,
      )}
      role="group"
      aria-label={t('browse.filters.title')}
    >
      {/* Header (vertical only) */}
      {!isHorizontal && (
        <div className="flex items-center justify-between border-b border-border pb-2">
          <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
            <Filter size={12} aria-hidden="true" />
            {t('browse.filters.title')}
          </span>
          {!hideClear && (
            <button
              type="button"
              onClick={resetFilters}
              disabled={isDefault}
              className={cn(
                'inline-flex items-center gap-0.5 rounded px-1 text-[11px] text-muted-foreground',
                'hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
                'disabled:cursor-not-allowed disabled:opacity-40',
              )}
            >
              <X size={10} aria-hidden="true" />
              {t('browse.filters.clearAll')}
            </button>
          )}
        </div>
      )}

      {/* Status */}
      <fieldset className={cn(isHorizontal && 'inline-flex items-center gap-2')}>
        <legend className={cn('mb-1 text-[11px] font-medium text-foreground', isHorizontal && 'mb-0 me-1')}>
          {t('browse.filters.status')}
        </legend>
        <div className={cn('flex gap-3', isHorizontal && 'gap-2')}>
          {(['all', 'active'] as const).map((value) => (
            <label key={value} className="inline-flex items-center gap-1.5 text-muted-foreground">
              <input
                type="radio"
                name="memory-status-filter"
                checked={filters.status === value}
                onChange={() => setFilter('status', value)}
                className="h-3.5 w-3.5 border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
              />
              {t(`status.${value}`)}
            </label>
          ))}
        </div>
      </fieldset>

      {/* Type */}
      <div>
        <label
          htmlFor="memory-type-filter"
          className="mb-1 block text-[11px] font-medium text-foreground"
        >
          {t('browse.filters.type')}
        </label>
        <select
          id="memory-type-filter"
          value={filters.memory_type ?? ''}
          onChange={(e) =>
            setFilter(
              'memory_type',
              e.target.value === ''
                ? null
                : (e.target.value as MemoryType),
            )
          }
          className="h-7 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
        >
          <option value="">{t('common.empty') === 'فارغ' ? '—' : '—'}</option>
          {ALL_MEMORY_TYPES.map((mt) => (
            <option key={mt} value={mt}>
              {t(`type.${mt}`)}
            </option>
          ))}
        </select>
      </div>

      {/* Pinned */}
      <label className="inline-flex items-center gap-1.5 text-muted-foreground">
        <input
          type="checkbox"
          checked={filters.is_pinned === true}
          onChange={(e) =>
            setFilter('is_pinned', e.target.checked ? true : null)
          }
          className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
        />
        {t('browse.filters.pinned')}
      </label>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

/**
 * Compare two filter objects for equality.
 *
 * Used to disable "Clear all" when the current filters already
 * match the defaults. Compared field-by-field (not `JSON.stringify`)
 * so that property order does not matter and `null` values are
 * compared correctly.
 */
function filtersEqual(a: BrowseFilters, b: BrowseFilters): boolean {
  return (
    a.status === b.status &&
    a.memory_type === b.memory_type &&
    a.is_pinned === b.is_pinned &&
    a.search_query === b.search_query
  );
}
