// src/features/memory/api/queries.ts

/**
 * React Query read hooks for the PELDRUN Memory feature.
 *
 * Every hook here wraps exactly one `memoryClient` method and
 * one `memoryKeys` builder. The 1:1 correspondence is
 * deliberate: a reader can grep for `useEntryList`, find the
 * `memoryKeys.entries.list(...)` call, and know precisely which
 * cache entry the hook owns. Adding a derivation (mapping,
 * filtering, aggregation) inside a hook would obscure that
 * mapping and make invalidation harder to reason about --
 * derivations belong in `select` callbacks at the call site.
 *
 * Hook contract
 * -------------
 *
 * All hooks return TanStack Query's standard shape (`data`,
 * `isLoading`, `error`, `refetch`, ...). The `error` field is
 * ALWAYS `MemoryApiError` after a failure -- the client throws
 * nothing else (see `client.ts`'s `memoryFetch`). So call sites
 * do not need a runtime check:
 *
 *     const { data, error } = useSettings();
 *     if (error) {
 *       const hint = uxHintFor(error);  // error is MemoryApiError
 *       ...
 *     }
 *
 * `signal` is threaded through every query function so that
 * TanStack Query can cancel in-flight requests when the
 * component unmounts or the query key changes. This is not
 * optional: without it, cancelled queries update state after
 * unmount, which React 19 warns about in development and which
 * leaks memory in production.
 *
 * Stale times
 * -----------
 *
 * Each hook declares an explicit `staleTime` matching the
 * mutation frequency of the underlying data:
 *
 *   * settings      60s  -- changes on user action only
 *   * entries       10s  -- user browses fast
 *   * entry detail  30s  -- single entry, less churn
 *   * projects list 60s  -- aggregate, changes slowly
 *   * project stats 60s  -- aggregate
 *   * usage         60s  -- monitoring data, staleness ok
 *   * adapters      inf  -- static for the process lifetime
 *
 * These are DEFAULTS. Each hook accepts a `staleTime` override
 * for cases where the caller knows better (e.g. a test suite
 * that wants stale-while-revalidate disabled entirely).
 */

import {
  useQuery,
  type UseQueryOptions,
  type UseQueryResult,
} from '@tanstack/react-query';

import { memoryClient } from './client';
import { memoryKeys } from './keys';
import type {
  EntryListParams,
  UsageBreakdownParams,
  UsageSummaryParams,
  UsageTimeseriesParams,
} from './keys';
import type { MemoryApiError } from '../types/errors';
import type {
  GetMemoryResponse,
  ListMemoryResponse,
  ListProjectsResponse,
  ProjectSettingsGetResponse,
  ProjectStatsResponse,
  SettingsGetResponse,
  UsageBreakdownResponse,
  UsageSummaryResponse,
  UsageTimeseriesResponse,
  ImportAdaptersResponse,
} from '../types/responses';
import type { MemorySettings } from '../types/settings';

// ---------------------------------------------------------------------------
// Default stale times (milliseconds)
// ---------------------------------------------------------------------------

/**
 * Stale times in milliseconds, per data category.
 *
 * Exported so tests and stories can reference them symbolically
 * rather than duplicating magic numbers. The values themselves
 * are documented in the module JSDoc above.
 */
export const MEMORY_STALE_TIMES = {
  settings: 60_000,
  entries: 10_000,
  entryDetail: 30_000,
  projects: 60_000,
  usage: 60_000,
  adapters: Number.POSITIVE_INFINITY,
} as const;

// ---------------------------------------------------------------------------
// Type helpers
// ---------------------------------------------------------------------------

/**
 * The common option bag every hook accepts.
 *
 * `enabled` gates the query -- when `false`, the hook returns
 * the standard "idle" state (isLoading: false, data: undefined)
 * without firing a request. Essential for detail hooks whose
 * id comes from a route param that is briefly undefined on
 * first render.
 *
 * `staleTime` overrides the hook's default. Rarely needed;
 * documented per hook where it might matter.
 */
interface BaseQueryOptions {
  enabled?: boolean;
  staleTime?: number;
}

/**
 * The exact subset of `UseQueryOptions` this module forwards to
 * TanStack Query.
 *
 * We do NOT forward `select`, `placeholderData`, `initialData`,
 * `refetchOnWindowFocus`, `refetchInterval`, etc. Consumers who
 * need those can call `useQuery` directly with the exported
 * `memoryKeys` builder -- the point of a wrapper hook is to
 * pin the parts that must NOT vary (queryKey, queryFn,
 * staleTime), not to be a universal pass-through. Keeping the
 * surface narrow prevents the feature from acquiring
 * per-call-site caching quirks that are hard to debug later.
 */
type HookOptions<TData> = BaseQueryOptions & {
  /**
   * A callback that receives the raw response and returns a
   * derived value. Optional. Equivalent to TanStack Query's
   * `select`, exposed here so callers do not need to remember
   * the shape of the raw response.
   */
  select?: (data: TData) => unknown;
};

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

/**
 * Fetch the global memory settings snapshot.
 *
 * This query is ALWAYS enabled -- the settings page and every
 * mode-gated component need it. The endpoint succeeds even when
 * `mode === 'disabled'` (see the spec's "no mode gates on
 * settings" rule).
 *
 * The hook returns the FULL `SettingsGetResponse` envelope
 * (with `operation` and `status`) because the settings page
 * uses `operation` in its audit log. Components that only need
 * the settings object should use `useMemorySettings()` below --
 * a thin wrapper that applies `select: (r) => r.settings`.
 */
export function useMemorySettingsQuery(
  options: HookOptions<SettingsGetResponse> = {},
): UseQueryResult<SettingsGetResponse, MemoryApiError> {
  return useQuery<SettingsGetResponse, MemoryApiError>({
    queryKey: memoryKeys.settings.global(),
    queryFn: ({ signal }) =>
      memoryClient.settings.get({ signal }),
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.settings,
    enabled: options.enabled ?? true,
    select: options.select as
      | ((data: SettingsGetResponse) => SettingsGetResponse)
      | undefined,
  });
}

/**
 * Convenience wrapper: return only the `MemorySettings` object.
 *
 * The vast majority of call sites need `settings.mode`,
 * `settings.token_budget`, etc., not the `status`/`operation`
 * fields. This hook bakes the `select` in so callers do not
 * repeat it.
 *
 * `data` is `undefined` while loading or on error. Callers who
 * need to distinguish "loading" from "error" should use
 * `useMemorySettingsQuery` -- but for a plain "what is the
 * current mode?" read, this is the right hook.
 */
export function useMemorySettings(
  options: BaseQueryOptions = {},
): UseQueryResult<MemorySettings, MemoryApiError> {
  return useQuery<MemorySettings, MemoryApiError>({
    queryKey: memoryKeys.settings.global(),
    queryFn: async ({ signal }) => {
      const response = await memoryClient.settings.get({ signal });
      return response.settings;
    },
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.settings,
    enabled: options.enabled ?? true,
  });
}

/**
 * Fetch one project's (effective, override) settings pair.
 *
 * Returns the raw envelope because both `effective` and
 * `override` are used by the project settings form. The
 * `computeEffectiveOverride()` helper from `types/settings.ts`
 * is typically called on the result to determine what the user
 * has ACTUALLY customized.
 *
 * Disabled by default when `projectId` is empty -- the route
 * param is briefly undefined on first render of the project
 * detail page.
 */
export function useProjectSettings(
  projectId: string | undefined,
  options: HookOptions<ProjectSettingsGetResponse> = {},
): UseQueryResult<ProjectSettingsGetResponse, MemoryApiError> {
  return useQuery<ProjectSettingsGetResponse, MemoryApiError>({
    queryKey: memoryKeys.settings.project(projectId ?? ''),
    queryFn: ({ signal }) =>
      memoryClient.settings.getProject(projectId!, { signal }),
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.settings,
    enabled: (options.enabled ?? true) && Boolean(projectId),
    select: options.select as
      | ((data: ProjectSettingsGetResponse) => ProjectSettingsGetResponse)
      | undefined,
  });
}

// ---------------------------------------------------------------------------
// Entries
// ---------------------------------------------------------------------------

/**
 * List entries in a single scope, with filters and pagination.
 *
 * The hook takes the SAME params object that the query key
 * factory uses, so call sites cannot accidentally diverge the
 * cache key from the request body. The query is disabled
 * automatically when `params.scope` is empty -- useful on the
 * browse page before the scope is resolved from the URL.
 *
 * `keepPreviousData` is OFF by default. Pages that show a
 * fixed page size (not infinite scroll) should pass
 * `placeholderData: keepPreviousData` via a custom `useQuery`
 * if they want to avoid layout jumps during pagination -- but
 * the memory UI's "Load more" pattern does not need it.
 */
export function useEntryList(
  params: EntryListParams,
  options: HookOptions<ListMemoryResponse> = {},
): UseQueryResult<ListMemoryResponse, MemoryApiError> {
  return useQuery<ListMemoryResponse, MemoryApiError>({
    queryKey: memoryKeys.entries.list(params),
    queryFn: ({ signal }) => {
      // The list endpoint requires explicit scope_type and
      // scope_id; the client receives them split from the
      // single `scope` string. Splitting here (not in the
      // client) keeps the client's param shape aligned with
      // the wire format.
      const [scopeType, ...rest] = params.scope.split(':');
      const scopeId = rest.join(':');
      return memoryClient.entries.list(
        {
          scope_type: scopeType,
          scope_id: scopeId,
          limit: params.limit,
          offset: params.offset,
          status: params.status,
          memory_type: params.memory_type,
          is_pinned: params.is_pinned,
        },
        { signal },
      );
    },
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.entries,
    enabled: (options.enabled ?? true) && params.scope.length > 0,
    select: options.select as
      | ((data: ListMemoryResponse) => ListMemoryResponse)
      | undefined,
  });
}

/**
 * Fetch a single entry by id + scope.
 *
 * Both parameters are required by the backend's endpoint
 * signature (missing either is a 400 `scope_required`). The
 * hook is disabled when either is empty -- callers typically
 * pass route params, which are briefly undefined on first
 * render.
 *
 * On a 404 (entry not found in this scope), `error` carries
 * `errorType: 'not_found'`. The detail page should treat that
 * as "redirect to list" -- see `uxHintFor`, which returns
 * `behavior: 'redirect'` for this case.
 */
export function useEntryDetail(
  memoryId: string | undefined,
  scope: string | undefined,
  options: HookOptions<GetMemoryResponse> = {},
): UseQueryResult<GetMemoryResponse, MemoryApiError> {
  return useQuery<GetMemoryResponse, MemoryApiError>({
    queryKey: memoryKeys.entries.detail(memoryId ?? '', scope ?? ''),
    queryFn: ({ signal }) => {
      const [scopeType, ...rest] = (scope ?? '').split(':');
      const scopeId = rest.join(':');
      return memoryClient.entries.get(
        {
          memoryId: memoryId!,
          scope_type: scopeType,
          scope_id: scopeId,
        },
        { signal },
      );
    },
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.entryDetail,
    enabled:
      (options.enabled ?? true) &&
      Boolean(memoryId) &&
      Boolean(scope) &&
      (scope ?? '').includes(':'),
    select: options.select as
      | ((data: GetMemoryResponse) => GetMemoryResponse)
      | undefined,
  });
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

/**
 * List projects that have at least one memory entry.
 *
 * Empty projects are intentionally absent from the response
 * (see `responses.ts`). The projects page uses this to render
 * the sidebar's scope selector, so it is stale-timed with the
 * same 60s as the project stats -- the two are always viewed
 * together.
 */
export function useProjectsList(
  options: HookOptions<ListProjectsResponse> = {},
): UseQueryResult<ListProjectsResponse, MemoryApiError> {
  return useQuery<ListProjectsResponse, MemoryApiError>({
    queryKey: memoryKeys.projects.list(),
    queryFn: ({ signal }) => memoryClient.projects.list({ signal }),
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.projects,
    enabled: options.enabled ?? true,
    select: options.select as
      | ((data: ListProjectsResponse) => ListProjectsResponse)
      | undefined,
  });
}

/**
 * Fetch detailed statistics for one project.
 *
 * Disabled when `projectId` is empty. Empty projects (reachable
 * via a direct URL) return empty `types`/`top_tags` and
 * sentinel-valued numeric ranges -- the stats panel must render
 * defensively for those cases.
 */
export function useProjectStats(
  projectId: string | undefined,
  options: HookOptions<ProjectStatsResponse> = {},
): UseQueryResult<ProjectStatsResponse, MemoryApiError> {
  return useQuery<ProjectStatsResponse, MemoryApiError>({
    queryKey: memoryKeys.projects.stats(projectId ?? ''),
    queryFn: ({ signal }) =>
      memoryClient.projects.stats(projectId!, { signal }),
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.projects,
    enabled: (options.enabled ?? true) && Boolean(projectId),
    select: options.select as
      | ((data: ProjectStatsResponse) => ProjectStatsResponse)
      | undefined,
  });
}

// ---------------------------------------------------------------------------
// Usage
// ---------------------------------------------------------------------------

/**
 * Fetch the usage summary (KPI cards).
 *
 * `period` in params selects the time window. Different
 * periods are DIFFERENT cache entries (see `keys.ts`) -- so a
 * user switching between "This week" and "This month" sees a
 * loading state on first switch, then an instant cache hit on
 * subsequent toggles.
 */
export function useUsageSummary(
  params: UsageSummaryParams,
  options: HookOptions<UsageSummaryResponse> = {},
): UseQueryResult<UsageSummaryResponse, MemoryApiError> {
  return useQuery<UsageSummaryResponse, MemoryApiError>({
    queryKey: memoryKeys.usage.summary(params),
    queryFn: ({ signal }) =>
      memoryClient.usage.summary(
        {
          period: params.period,
          chat_id: params.chat_id,
          project_id: params.project_id,
        },
        { signal },
      ),
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.usage,
    enabled: options.enabled ?? true,
    select: options.select as
      | ((data: UsageSummaryResponse) => UsageSummaryResponse)
      | undefined,
  });
}

/**
 * Fetch the usage breakdown (pie/bar chart).
 *
 * `by` selects the dimension; `source` is accepted as an alias
 * for `scope` on the wire and normalized in the response. The
 * response's `by` field carries the NORMALIZED value -- the
 * chart's axis label should come from `response.by`, not from
 * the caller's input.
 */
export function useUsageBreakdown(
  params: UsageBreakdownParams,
  options: HookOptions<UsageBreakdownResponse> = {},
): UseQueryResult<UsageBreakdownResponse, MemoryApiError> {
  return useQuery<UsageBreakdownResponse, MemoryApiError>({
    queryKey: memoryKeys.usage.breakdown(params),
    queryFn: ({ signal }) =>
      memoryClient.usage.breakdown(
        {
          by: params.by,
          period: params.period,
          chat_id: params.chat_id,
          project_id: params.project_id,
        },
        { signal },
      ),
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.usage,
    enabled: options.enabled ?? true,
    select: options.select as
      | ((data: UsageBreakdownResponse) => UsageBreakdownResponse)
      | undefined,
  });
}

/**
 * Fetch the usage timeseries (line chart).
 *
 * No chat/project filters -- the endpoint does not accept them.
 * `granularity` controls bucket size; it is NOT cross-validated
 * with `period` (a `day` period with `month` granularity is a
 * legitimate one-bucket query).
 */
export function useUsageTimeseries(
  params: UsageTimeseriesParams,
  options: HookOptions<UsageTimeseriesResponse> = {},
): UseQueryResult<UsageTimeseriesResponse, MemoryApiError> {
  return useQuery<UsageTimeseriesResponse, MemoryApiError>({
    queryKey: memoryKeys.usage.timeseries(params),
    queryFn: ({ signal }) =>
      memoryClient.usage.timeseries(
        {
          period: params.period,
          granularity: params.granularity,
        },
        { signal },
      ),
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.usage,
    enabled: options.enabled ?? true,
    select: options.select as
      | ((data: UsageTimeseriesResponse) => UsageTimeseriesResponse)
      | undefined,
  });
}

// ---------------------------------------------------------------------------
// Import adapters
// ---------------------------------------------------------------------------

/**
 * Fetch the registered import adapters.
 *
 * `staleTime: Infinity` -- adapters are registered at backend
 * startup and never change at runtime in the current build. The
 * query caches forever within a session; a page reload picks up
 * any change (which requires a backend restart anyway).
 *
 * The wizard uses this to populate the "Force adapter" dropdown.
 * A user who does not override sees "Auto-detect" -- which is
 * `adapter_id: null` on the wire, not a "auto" string value
 * (there is no such adapter id).
 */
export function useImportAdapters(
  options: HookOptions<ImportAdaptersResponse> = {},
): UseQueryResult<ImportAdaptersResponse, MemoryApiError> {
  return useQuery<ImportAdaptersResponse, MemoryApiError>({
    queryKey: memoryKeys.importAdapters.all(),
    queryFn: ({ signal }) =>
      memoryClient.imports.listAdapters({ signal }),
    staleTime: options.staleTime ?? MEMORY_STALE_TIMES.adapters,
    enabled: options.enabled ?? true,
    select: options.select as
      | ((data: ImportAdaptersResponse) => ImportAdaptersResponse)
      | undefined,
  });
}
