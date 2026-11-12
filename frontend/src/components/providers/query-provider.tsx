// src/components/providers/query-provider.tsx

/**
 * Application-wide TanStack Query provider.
 *
 * Owns exactly ONE `QueryClient` instance for the entire app
 * tree. The client is created lazily inside `useState` so it is
 * stable across re-renders and per-request on the server (each
 * request gets a fresh client -- a shared client would leak
 * cached data between users).
 *
 * Default options
 * ---------------
 *
 *   * `staleTime: 30s` -- most reads benefit from a short
 *     window of "don't refetch immediately". The memory
 *     feature overrides this per query where it needs a
 *     different value (e.g. `Infinity` for adapters).
 *
 *   * `gcTime: 5min` -- keep unused queries in cache for a
 *     short while. Enables instant back-navigation to a page
 *     the user just viewed.
 *
 *   * `retry: 1` -- one retry on transient errors. More than
 *     one is usually noise; the user can always refresh.
 *
 *   * `refetchOnWindowFocus: false` -- focus refetching is a
 *     UX trade-off that fits dashboards, not a memory browser.
 *     Pages that want it can override per query.
 *
 *   * Mutations `retry: 0` -- a failed mutation means the
 *     operation is in an unknown state (did the write succeed
 *     before the connection broke?). Never auto-retry; the
 *     user must confirm.
 *
 * These are DEFAULTS. `features/memory/api/queries.ts` declares
 * its own `staleTime` per hook, and those take precedence.
 */

'use client';

import {
  QueryClient,
  QueryClientProvider,
} from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';

export function QueryProvider({ children }: { children: ReactNode }) {
  // `useState(() => new QueryClient(...))` -- lazy initializer
  // so React does not recreate the client on every render.
  // Without this, every parent re-render would drop the cache.
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            gcTime: 5 * 60_000,
            retry: 1,
            refetchOnWindowFocus: false,
            refetchOnReconnect: true,
          },
          mutations: {
            retry: 0,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={client}>
      {children}
    </QueryClientProvider>
  );
}
