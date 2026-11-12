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

  /**
   * Contextual scope: the chat this composer belongs to.
   *
   * When provided (and `scope` is NOT), the backend resolves the
   * full set of relevant scopes for this chat -- user:default,
   * the owning project (if any), and this chat -- and searches
   * all of them. This is the correct mode for the composer:
   * the user wants their personal memories to surface here, not
   * just the ones stored in this specific chat.
   */
  chat_id?: string;

  /**
   * Contextual scope: the project this composer belongs to.
   * Meaningful only when `chat_id` is absent (or in addition to
   * it -- the backend unions the two).
   */
  project_id?: string;

  /**
   * Explicit scope: search ONLY this scope.
   *
   * Use when the caller wants to check "does a similar entry
   * already exist IN THIS SCOPE?" -- e.g. an entry editor
   * previewing potential duplicates before saving. Different
   * from the contextual mode above, which intentionally widens
   * the search to user + project + chat.
   */
  scope?: { scope_type: MemoryScope; scope_id: string };

  /**
   * Contextual: the user id whose personal memories should be
   * included. `"default"` in the current single-user backend.
   * Only consulted when `scope` is NOT provided.
   */
  user_id?: string;

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
  chat_id,
  project_id,
  user_id,
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

    // Build the request body based on the mode the caller chose.
    //
    //   * `scope` provided     -> explicit single-scope search.
    //   * otherwise            -> contextual (backend unions user
    //                             + project + chat automatically).
    //
    // Mixing the two would be ambiguous -- the backend prefers
    // explicit when both are present -- so we pick one mode and
    // stick with it.
    const body: Parameters<typeof searchMutation.mutate>[0] = {
      query: debounced,
      limit,
    };

    let hasTarget = false;

    if (scope) {
      body.scope_type = scope.scope_type;
      body.scope_id = scope.scope_id;
      hasTarget = true;
    } else {
      if (chat_id) {
        body.chat_id = chat_id;
        hasTarget = true;
      }
      if (project_id) {
        body.project_id = project_id;
        hasTarget = true;
      }
      if (user_id) {
        body.user_id = user_id;
        hasTarget = true;
      }
    }

    // No target -> nothing to search against. The parent should
    // gate rendering in this case, but the guard makes the
    // component safe if used without one.
    if (!hasTarget) return;

    searchMutation.mutate(body);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced, scope, chat_id, project_id, user_id, limit]);

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
