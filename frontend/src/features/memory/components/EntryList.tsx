// src/features/memory/components/EntryList.tsx

/**
 * A vertical list of memory entries with selection, pagination,
 * and empty/loading/error states.
 *
 * Presentational: receives the already-fetched entries and the
 * callbacks. The parent page owns pagination state, filter
 * state, and mutation wiring -- this component just renders
 * what it is given and calls the callbacks. That split makes
 * the list trivially testable without a QueryClient and lets
 * the browse page decide how pagination accumulates (offset
 * increments vs. reset on filter change).
 *
 * Selection
 * ---------
 *
 * When `onToggleSelection` is provided, the list renders a
 * checkbox column with a "select all on this page" header.
 * Callers that do not want selection (e.g. the search preview
 * panel) simply omit the prop and the column disappears.
 *
 * Empty vs. loading
 * -----------------
 *
 * The three states are distinguished explicitly:
 *
 *   * `isLoading && entries.length === 0` -> skeleton cards.
 *   * `!isLoading && entries.length === 0` -> `<EmptyState>`.
 *   * `entries.length > 0` -> cards (and `isLoading` triggers
 *     a subtle inline spinner beside "Load more").
 *
 * A partial list (some entries already loaded, `isLoading` for
 * the next page) shows the existing cards -- never reverts to
 * skeletons. That is the correct behavior for offset-based
 * pagination: the user should never lose their place.
 */

'use client';

import { useTranslation } from 'react-i18next';
import { Brain, RefreshCw } from 'lucide-react';

import type { MemoryEntry } from '../types/entry';
import type { MemoryApiError } from '../types/errors';
import { cn } from '../lib/cn';
import { EmptyState } from './EmptyState';
import { EntryCard } from './EntryCard';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface EntryListProps {
  /** The entries to render, in rank/creation order. */
  entries: MemoryEntry[];

  /** Initial page loading (no entries yet). */
  isLoading?: boolean;

  /** Loading the next page (entries already visible). */
  isLoadingMore?: boolean;

  /** The last fetch failed. `error` carries the details. */
  error?: MemoryApiError | null;

  /** Whether more pages exist. Controls "Load more" visibility. */
  hasMore?: boolean;

  /** Called when the user clicks "Load more". */
  onLoadMore?: () => void;

  /** Called when the user clicks "Retry" after an error. */
  onRetry?: () => void;

  // ---- Per-card actions ------------------------------------------------

  onEdit?: (entry: MemoryEntry) => void;
  onDelete?: (entry: MemoryEntry) => void;
  onPromote?: (entry: MemoryEntry) => void;

  // ---- Selection (opt-in) ----------------------------------------------

  /**
   * The set of selected entry ids. When `undefined`, no checkbox
   * column is rendered.
   */
  selectedIds?: ReadonlySet<string>;

  /** Toggle one entry's selection. */
  onToggleSelection?: (memoryId: string) => void;

  /** Select every entry currently visible. */
  onSelectAllOnPage?: () => void;

  /** Clear the selection. */
  onClearSelection?: () => void;

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function EntryList({
  entries,
  isLoading = false,
  isLoadingMore = false,
  error = null,
  hasMore = false,
  onLoadMore,
  onRetry,
  onEdit,
  onDelete,
  onPromote,
  selectedIds,
  onToggleSelection,
  onSelectAllOnPage,
  onClearSelection,
  className,
}: EntryListProps) {
  const { t } = useTranslation('memory');

  const showSelection =
    selectedIds !== undefined && onToggleSelection !== undefined;
  const allSelected =
    showSelection &&
    entries.length > 0 &&
    entries.every((e) => selectedIds!.has(e.memory_id));
  const someSelected =
    showSelection && entries.some((e) => selectedIds!.has(e.memory_id));

  // ---- Error state ----------------------------------------------------
  if (error && entries.length === 0) {
    return (
      <EmptyState
        icon={RefreshCw}
        title={t('errors.generic')}
        description={error.message}
        size="spacious"
        action={
          onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="inline-flex h-8 items-center justify-center rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            >
              {t('common.retry')}
            </button>
          )
        }
        className={className}
      />
    );
  }

  // ---- Initial loading ------------------------------------------------
  if (isLoading && entries.length === 0) {
    return (
      <div className={cn('space-y-2', className)} aria-busy="true">
        {[0, 1, 2].map((i) => (
          <SkeletonCard key={i} />
        ))}
      </div>
    );
  }

  // ---- Empty --------------------------------------------------------
  if (!isLoading && entries.length === 0) {
    return (
      <EmptyState
        icon={Brain}
        title={t('browse.empty.title')}
        description={t('browse.empty.description')}
        size="spacious"
        className={className}
      />
    );
  }

  // ---- List ---------------------------------------------------------
  return (
    <div className={cn('space-y-2', className)} role="list">
      {/* Selection toolbar */}
      {showSelection && (
        <div className="flex items-center gap-2 border-b border-border pb-2 text-xs">
          <label className="inline-flex items-center gap-1.5 text-muted-foreground">
            <input
              type="checkbox"
              checked={allSelected}
              ref={(el) => {
                if (el) el.indeterminate = someSelected && !allSelected;
              }}
              onChange={(e) => {
                if (e.target.checked) {
                  onSelectAllOnPage?.();
                } else {
                  onClearSelection?.();
                }
              }}
              className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
              aria-label={t('a11y.selectAllOnPage')}
            />
            {allSelected
              ? t('common.clear')
              : t('a11y.selectAllOnPage')}
          </label>
          {someSelected && (
            <span className="tabular-nums text-muted-foreground">
              · {selectedIds!.size} {t('a11y.selectedCount')}
            </span>
          )}
        </div>
      )}

      {/* Cards */}
      {entries.map((entry) => (
        <div key={entry.memory_id} role="listitem" className="flex items-start gap-2">
          {showSelection && (
            <input
              type="checkbox"
              checked={selectedIds!.has(entry.memory_id)}
              onChange={() => onToggleSelection!(entry.memory_id)}
              className="mt-3.5 h-3.5 w-3.5 shrink-0 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
              aria-label={t('a11y.selectEntry')}
            />
          )}
          <EntryCard
            entry={entry}
            onEdit={onEdit ? () => onEdit(entry) : undefined}
            onDelete={onDelete ? () => onDelete(entry) : undefined}
            onPromote={onPromote ? () => onPromote(entry) : undefined}
            className="flex-1"
          />
        </div>
      ))}

      {/* Pagination footer */}
      {(hasMore || isLoadingMore) && (
        <div className="flex items-center justify-center py-2">
          <button
            type="button"
            onClick={onLoadMore}
            disabled={isLoadingMore}
            className={cn(
              'inline-flex h-8 items-center justify-center gap-1.5 rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground',
              'hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
              'disabled:cursor-not-allowed disabled:opacity-60',
            )}
          >
            {isLoadingMore && (
              <span
                aria-hidden="true"
                className="h-3 w-3 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent"
              />
            )}
            {isLoadingMore ? t('common.loadingMore') : t('common.loadMore')}
          </button>
        </div>
      )}

      {!hasMore && entries.length > 0 && (
        <p className="py-2 text-center text-[11px] text-muted-foreground">
          {t('common.noMore')}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton card
// ---------------------------------------------------------------------------

function SkeletonCard() {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3">
      <div className="flex items-center gap-2">
        <div className="h-4 w-16 animate-pulse rounded-full bg-muted" />
        <div className="h-4 w-16 animate-pulse rounded-full bg-muted" />
      </div>
      <div className="space-y-1.5">
        <div className="h-3 w-full animate-pulse rounded bg-muted" />
        <div className="h-3 w-4/5 animate-pulse rounded bg-muted" />
        <div className="h-3 w-2/3 animate-pulse rounded bg-muted" />
      </div>
    </div>
  );
}
