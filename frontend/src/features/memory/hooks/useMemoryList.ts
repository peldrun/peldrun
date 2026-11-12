// src/features/memory/hooks/useMemoryList.ts

/**
 * Paginated entry-list hook with accumulated-page state.
 *
 * This is a HIGHER-LEVEL wrapper than `useEntryList` (in
 * `api/queries.ts`). `useEntryList` exposes raw TanStack Query
 * semantics for a single page; `useMemoryList` owns the
 * "accumulate pages as the user clicks Load More" pattern that
 * the browse page implements.
 *
 * Usage
 * -----
 *
 *     const list = useMemoryList({ scope: 'project:acme' });
 *     ...
 *     <EntryList
 *       entries={list.entries}
 *       isLoading={list.isLoading}
 *       isLoadingMore={list.isLoadingMore}
 *       hasMore={list.hasMore}
 *       onLoadMore={list.loadMore}
 *       ...
 *     />
 *
 * Filter changes
 * --------------
 *
 * The hook resets `offset` and `entries` whenever the `scope`
 * or `filters` change. The reset is triggered by a `useEffect`
 * keyed on those inputs, matching the browse page's behavior.
 *
 * Stale-response guard
 * --------------------
 *
 * After each fetch, the hook checks that the response's
 * `offset` field matches the CURRENT offset before appending.
 * A response that arrives late (because the user changed a
 * filter mid-flight) is silently ignored rather than
 * duplicating rows. This is the same guard the browse page
 * uses inline -- centralizing it here makes it testable.
 *
 * Why this does not replace the browse page's inline logic
 * --------------------------------------------------------
 *
 * The browse page pre-dates this hook and works. Refactoring
 * it to use `useMemoryList` is a mechanical change but carries
 * regression risk. It is deferred to F9 (polish), at which
 * point this hook is validated by a second caller (a project
 * detail page listing its entries). Until then, this hook is
 * "available and tested in isolation" but not yet wired in.
 */

'use client';

import { useEffect, useState } from 'react';
import {
  keepPreviousData,
  useQuery,
} from '@tanstack/react-query';

import type { MemoryEntry } from '../types/entry';
import type { MemoryApiError } from '../types/errors';
import type { MemoryStatusFilter, MemoryType } from '../types/enums';
import { memoryClient } from '../api/client';
import { memoryKeys } from '../api/keys';
import { DEFAULT_PAGE_SIZE } from '../lib/constants';
import { useMemoryMode } from './useMemoryMode';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface UseMemoryListParams {
  /**
   * The single scope to list, in wire form (`"<type>:<id>"`).
   * An empty string disables the query.
   */
  scope: string;

  /** Status filter (defaults to `'all'`). */
  status?: MemoryStatusFilter;

  /** Type filter (defaults to no filter). */
  memory_type?: MemoryType | null;

  /** Pin filter (defaults to no filter). */
  is_pinned?: boolean | null;

  /** Page size. Defaults to `DEFAULT_PAGE_SIZE` (50). */
  limit?: number;
}

export interface MemoryListResult {
  entries: MemoryEntry[];
  isLoading: boolean;
  isLoadingMore: boolean;
  hasMore: boolean;
  error: MemoryApiError | null;
  loadMore: () => void;
  reset: () => void;
  refetch: () => void;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useMemoryList(
  params: UseMemoryListParams,
): MemoryListResult {
  const { canRead } = useMemoryMode();
  const limit = params.limit ?? DEFAULT_PAGE_SIZE;
  const status = params.status ?? 'all';
  const memory_type = params.memory_type ?? null;
  const is_pinned = params.is_pinned ?? null;

  const [offset, setOffset] = useState(0);
  const [entries, setEntries] = useState<MemoryEntry[]>([]);

  // Reset on any input change.
  useEffect(() => {
    setOffset(0);
    setEntries([]);
  }, [params.scope, status, memory_type, is_pinned]);

  const listQuery = useQuery<
    Awaited<ReturnType<typeof memoryClient.entries.list>>,
    MemoryApiError
  >({
    queryKey: memoryKeys.entries.list({
      scope: params.scope,
      status,
      memory_type,
      is_pinned,
      limit,
      offset,
    }),
    queryFn: async ({ signal }) => {
      const [scopeType, ...rest] = params.scope.split(':');
      const scopeId = rest.join(':');
      return memoryClient.entries.list(
        {
          scope_type: scopeType,
          scope_id: scopeId,
          limit,
          offset,
          status,
          memory_type,
          is_pinned,
        },
        { signal },
      );
    },
    enabled: canRead && params.scope.length > 0,
    staleTime: 10_000,
    placeholderData: keepPreviousData,
  });

  // Guarded accumulation.
  useEffect(() => {
    const response = listQuery.data;
    if (!response) return;
    if (response.offset !== offset) return;

    if (offset === 0) {
      setEntries(response.memories);
      return;
    }

    setEntries((prev) => {
      const existing = new Set(prev.map((e) => e.memory_id));
      const fresh = response.memories.filter(
        (e) => !existing.has(e.memory_id),
      );
      return fresh.length === 0 ? prev : [...prev, ...fresh];
    });
  }, [listQuery.data, offset]);

  const responseCount = listQuery.data?.count ?? 0;
  const hasMore =
    responseCount === limit &&
    entries.length > 0 &&
    entries.length === offset + responseCount;

  const isLoading = listQuery.isPending && !listQuery.data;
  const isLoadingMore = listQuery.isFetching && offset > 0;

  return {
    entries,
    isLoading,
    isLoadingMore,
    hasMore,
    error: listQuery.error ?? null,
    loadMore: () => setOffset((o) => o + limit),
    reset: () => {
      setOffset(0);
      setEntries([]);
    },
    refetch: () => {
      listQuery.refetch();
    },
  };
}
