// src/features/memory/hooks/useScopeFromContext.ts

/**
 * Resolve the "active memory scope" from the current context.
 *
 * "Context" here means the surrounding application route and/or
 * a `?scope=` URL parameter. Different pages have different
 * context sources:
 *
 *   * `/chat/[id]` pages have a chat id in the route -> the
 *     natural memory scope is `chat:<id>`.
 *   * `/projects/[projectId]` pages have a project id -> the
 *     natural scope is `project:<projectId>`.
 *   * `/memory/browse` reads `?scope=` explicitly.
 *
 * Rather than forcing each caller to re-implement this
 * resolution, the hook centralizes it. The resolution order
 * (documented on `resolveScopeFromContext` below) is:
 *
 *   1. Explicit `override` -- highest priority. When a caller
 *      already knows the scope (e.g. it came from a mutation
 *      result), it passes it in.
 *   2. `?scope=` URL parameter.
 *   3. Route params (`chat_id`, `project_id`).
 *   4. User's personal scope (`user:default`) -- the safe
 *      default for "I want SOMETHING to save into".
 *
 * `null` return
 * -------------
 *
 * When the caller explicitly opts out of the personal
 * fallback (`allowUserFallback: false`) and none of the higher
 * priority sources produce a scope, the hook returns `null`.
 * This is the correct value for pages that cannot meaningfully
 * write to `user:default` -- e.g. a project settings page
 * that is only about that project.
 *
 * NOT a data fetcher
 * ------------------
 *
 * The hook does NOT fetch project metadata or validate that
 * the resolved scope actually exists. That is the caller's
 * job (via `useProjectsList` or a direct query). The hook is
 * a pure resolver: given inputs, produce a scope ref or null.
 */

'use client';

import { useParams, useSearchParams } from 'next/navigation';
import { useMemo } from 'react';

import type { MemoryScopeRef } from '../types/entry';
import { parseScopeRef } from '../types/entry';
import { MemoryScope } from '../types/enums';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface UseScopeFromContextOptions {
  /**
   * An explicit scope that overrides everything else. When
   * present and non-null, no other source is consulted.
   */
  override?: MemoryScopeRef | null;

  /**
   * When `true` (default), the hook falls back to
   * `user:default` if no other source produced a scope. When
   * `false`, the hook returns `null` instead.
   */
  allowUserFallback?: boolean;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useScopeFromContext(
  options: UseScopeFromContextOptions = {},
): MemoryScopeRef | null {
  const { override = null, allowUserFallback = true } = options;
  const params = useParams();
  const searchParams = useSearchParams();

  return useMemo(() => {
    // 1. Explicit override wins.
    if (override) return override;

    // 2. `?scope=` URL parameter.
    const scopeParam = searchParams?.get('scope');
    if (scopeParam) {
      const parsed = parseScopeRef(scopeParam);
      if (parsed) return parsed;
    }

    // 3. Route params.
    const chatId = normalizeRouteParam(params?.id)
      ?? normalizeRouteParam(params?.chatId)
      ?? normalizeRouteParam(params?.chat_id);
    if (chatId) {
      return { scope_type: MemoryScope.CHAT, scope_id: chatId };
    }

    const projectId = normalizeRouteParam(params?.projectId)
      ?? normalizeRouteParam(params?.project_id);
    if (projectId) {
      return { scope_type: MemoryScope.PROJECT, scope_id: projectId };
    }

    // 4. Optional fallback to the user's personal scope.
    if (allowUserFallback) {
      return { scope_type: MemoryScope.USER, scope_id: 'default' };
    }

    return null;
  }, [override, allowUserFallback, params, searchParams]);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Normalize a Next.js route param, which may be `string`,
 * `string[]`, or `undefined` depending on the route shape.
 */
function normalizeRouteParam(
  value: string | string[] | undefined,
): string | null {
  if (Array.isArray(value)) return value[0] ?? null;
  if (typeof value === 'string' && value.length > 0) return value;
  return null;
}
