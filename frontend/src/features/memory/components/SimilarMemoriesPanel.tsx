// src/features/memory/components/SimilarMemoriesPanel.tsx

/**
 * A "similar memories" preview, for the chat composer.
 *
 * Watches a live `query` prop (typically the user's current
 * message draft), debounces it, calls `POST /api/memory/search`
 * against the active scope, and renders the top candidates so
 * the user can see what the model will "remember" before
 * sending.
 *
 * UX contract
 * -----------
 *
 *   * Empty / short query (`< SEARCH_MIN_QUERY_LENGTH`): the
 *     panel is hidden entirely -- no empty state, no skeleton,
 *     no wasted request.
 *
 *   * Loading: a subtle inline "checking..." pill, no layout
 *     shift (the panel is height-stable at the pill level).
 *
 *   * Results: up to 3 entries, each one line with a truncated
 *     snippet. The panel is a HINT, not a full view -- clicking
 *     a candidate is deliberately NOT wired up, because acting
 *     on the entry belongs to a different surface (the browse
 *     page or the memory detail page).
 *
 *   * Empty results: a muted "no similar memories" line.
 *
 *   * Budget signal: when `dropped_for_budget > 0`, the panel
 *     appends "N memories omitted for size" in muted text --
 *     the same signal the spec requires in the usage dashboard
 *     (§1.4), surfaced at the point where it matters.
 *
 * Debouncing
 * ----------
 *
 * The panel does NOT debounce internally -- the parent owns
 * the input and passes a stable `query`. The panel debounces
 * locally via `useEffect`, using `SEARCH_DEBOUNCE_MS` from
 * `lib/constants`. Callers that already debounce (e.g. the
 * composer's rich editor) can pass `debounce={false}` to skip
 * the extra delay.
 */

'use client';

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Sparkles } from 'lucide-react';

import type { MemoryScope } from '../types/enums';
import type { MemoryEntry } from '../types/entry';
import {
  SEARCH_DEBOUNCE_MS,
  SEARCH_MIN_QUERY_LENGTH,
} from '../lib/constants';
import { useSearchMemories } from '../api/mutations';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface SimilarMemoriesPanelProps {
  /** The current query text (typically the composer's draft). */
  query: string;

  /** The scope to search. */
  scope: { scope_type: MemoryScope; scope_id: string };

  /** Maximum results to show. Defaults to 3. */
  limit?: number;

  /**
   * Whether to apply internal debouncing. Defaults to `true`.
   * Set to `false` when the caller already debounces.
   */
  debounce?: boolean;

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SimilarMemoriesPanel({
  query,
  scope,
  limit = 3,
  debounce = true,
  className,
}: SimilarMemoriesPanelProps) {
  const { t } = useTranslation('memory');
  const searchMutation = useSearchMemories();
  const [debounced, setDebounced] = useState(query);

  // ---- Debounce ------------------------------------------------
  useEffect(() => {
    if (!debounce) {
      setDebounced(query);
      return undefined;
    }
    const handle = setTimeout(() => setDebounced(query), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [query, debounce]);

  // ---- Fire search ---------------------------------------------
  useEffect(() => {
    if (debounced.trim().length < SEARCH_MIN_QUERY_LENGTH) return;
    searchMutation.mutate({
      query: debounced,
      scope_type: scope.scope_type,
      scope_id: scope.scope_id,
      limit,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced, scope.scope_type, scope.scope_id, limit]);

  // ---- Empty states --------------------------------------------
  const trimmed = debounced.trim();
  if (trimmed.length < SEARCH_MIN_QUERY_LENGTH) {
    return null;
  }

  const isPending = searchMutation.isPending;
  const data = searchMutation.data;
  const results: MemoryEntry[] = data?.results ?? [];
  const dropped = data?.dropped_for_budget ?? 0;

  return (
    <div
      className={cn(
        'rounded-md border border-border bg-muted/40 px-2.5 py-2 text-[11px]',
        className,
      )}
      aria-label={t('entry.similar.title')}
    >
      {/* Header */}
      <div className="mb-1 flex items-center gap-1 text-muted-foreground">
        <Sparkles size={10} aria-hidden="true" />
        <span className="font-medium">{t('entry.similar.title')}</span>
        {isPending && (
          <span className="ms-1 inline-flex items-center gap-1 opacity-70">
            <span
              aria-hidden="true"
              className="h-2 w-2 animate-spin rounded-full border border-muted-foreground border-t-transparent"
            />
            {t('entry.similar.checking')}
          </span>
        )}
      </div>

      {/* Body */}
      {!isPending && results.length === 0 && (
        <p className="text-muted-foreground">{t('entry.similar.empty')}</p>
      )}

      {results.length > 0 && (
        <ul className="space-y-0.5">
          {results.map((m) => (
            <li
              key={m.memory_id}
              className="truncate text-foreground/80"
              title={m.content}
            >
              · {m.content.slice(0, 120)}
            </li>
          ))}
        </ul>
      )}

      {/* Budget overflow signal */}
      {dropped > 0 && (
        <p className="mt-1 text-[10px] text-amber-700 dark:text-amber-400">
          {t('entry.similar.budgetDropped', { count: dropped })}
        </p>
      )}

      {/* Error surface */}
      {searchMutation.error && !isPending && (
        <p className="mt-1 text-destructive" role="alert">
          {searchMutation.error.message}
        </p>
      )}
    </div>
  );
}
