// src/features/memory/api/keys.ts

/**
 * TanStack Query key factory for the PELDRUN Memory feature.
 *
 * Why a factory, not inline arrays
 * --------------------------------
 *
 * Raw key arrays (`['memory', 'entries', 'list', ...]`) scattered
 * across queries.ts and mutations.ts create three classes of bugs:
 *
 *   1. **Typos.** `['memory', 'entry', 'list']` (missing 's')
 *      silently fails to invalidate -- the query keeps serving
 *      stale data with no error anywhere.
 *
 *   2. **Shape drift.** One call site caches with
 *      `{limit, offset}` and another invalidates with
 *      `{offset, limit}` -- same intent, different hashes,
 *      cache miss. A factory centralizes the shape.
 *
 *   3. **Over/under-invalidation.** Invalidating
 *      `['memory', 'entries']` (too broad) nukes unrelated
 *      detail queries; invalidating
 *      `['memory', 'entries', 'list', {specific params}]`
 *      (too narrow) misses sibling pages.
 *
 * The factory pattern solves all three: one declaration, one
 * shape, hierarchical prefixes for partial invalidation.
 *
 * Cache-key hierarchy
 * -------------------
 *
 * Every key returned here starts with `'memory'` and follows the
 * shape:
 *
 *     ['memory', <namespace>, <subnamespace?>..., <args?>...]
 *
 * The hierarchy is DESIGNED for TanStack Query's prefix matching.
 * `queryClient.invalidateQueries({ queryKey: prefix })` matches
 * every cached key that STARTS WITH `prefix`. So:
 *
 *     memoryKeys.all                    // ['memory']
 *         invalidates EVERYTHING under memory
 *     memoryKeys.entries.all()          // ['memory', 'entries']
 *         invalidates both list and detail queries
 *     memoryKeys.entries.lists()        // ['memory', 'entries', 'list']
 *         invalidates only list queries (any params)
 *     memoryKeys.entries.list(params)   // ['memory', 'entries', 'list', params]
 *         invalidates exactly ONE cached list
 *
 * Invalidation policy (from the spec, section 4.4)
 * ------------------------------------------------
 *
 *   Mutation                 | Invalidate
 *   -------------------------|----------------------------------------
 *   PATCH settings           | settings.global(), settings.project(*),
 *                            |   usage.all() (budget changed)
 *   PUT project override     | settings.project(id), usage.all()
 *   DELETE project override  | settings.project(id), usage.all()
 *   POST create              | entries.lists(), projects.list(),
 *                            |   projects.stats(<scope_id>)
 *   PATCH update             | entries.lists(), entries.detail(id, scope)
 *   DELETE                   | entries.lists(), entries.detail(id, scope),
 *                            |   projects.stats(<scope_id>)
 *   POST pin                 | entries.lists(), entries.detail(id, scope)
 *                            |   (optimistic; see mutations.ts)
 *   POST promote             | entries.lists() (BOTH source and target
 *                            |   scopes -- one prefix, two invalidations
 *                            |   if scopes differ)
 *   POST bulk-archive        | entries.lists(), projects.list(),
 *                            |   projects.stats(id)
 *   POST bulk-purge          | entries.lists(), entries.details(),
 *                            |   projects.list(), projects.stats(id)
 *   POST import apply        | entries.lists(), projects.list()
 *
 * mutations.ts implements exactly this table; deviations should
 * be documented in the specific mutation's hook.
 *
 * Rules for consumers
 * -------------------
 *
 *   * **Never write a raw key array** outside this file. If you
 *     find yourself typing `['memory', ...]` in a `.ts` file
 *     other than this one, stop -- add a builder here instead.
 *     The spec's acceptance criterion for F1.2 is literally
 *     "grep does not find duplicate `['memory'`", enforced by
 *     this rule.
 *
 *   * **Pass params objects, not positional args.** Future
 *     fields (e.g. a new filter) extend the params interface
 *     without touching every call site.
 *
 *   * **Use `null` (not `undefined`) for "no value".** TanStack
 *     Query hashes via JSON.stringify; `{a: undefined}` and
 *     `{}` hash the same, but `{a: null}` and `{}` do NOT. The
 *     params interfaces below require `| null`, and the
 *     normalizers coerce `undefined` to `null` defensively, so
 *     cache misses from missing-vs-null confusion are
 *     impossible.
 */

import type {
  MemoryStatusFilter,
  MemoryType,
  UsageBreakdownDimension,
  UsageGranularity,
  UsagePeriod,
} from '../types';

// ---------------------------------------------------------------------------
// Parameter types
// ---------------------------------------------------------------------------
//
// Every query that takes parameters declares a named interface
// here, so call sites cannot drift. The interfaces enforce that
// "no value" is spelled `null`, never omitted -- see the
// cache-hashing note in the module JSDoc.

/**
 * Parameters for the entry-list query.
 *
 * All non-optional fields MUST be supplied on every call, even
 * when the user has not set a filter. Use `null` for "no
 * filter". This eliminates the `undefined`-vs-missing hash
 * divergence that would otherwise cause stale reads when a
 * filter is cleared.
 */
export interface EntryListParams {
  /**
   * The single scope to list, in wire form (`"<type>:<id>"`).
   * E.g. `"project:acme"`, `"user:default"`.
   *
   * Note: the list endpoint requires an EXPLICIT single scope;
   * multi-scope retrieval is only available via `/search`.
   */
  scope: string;

  /** Filter by lifecycle status. Matches the `status` query param. */
  status: MemoryStatusFilter;

  /** Filter by memory type, or `null` for all types. */
  memory_type: MemoryType | null;

  /** Filter by pin state, or `null` for both. */
  is_pinned: boolean | null;

  /** Page size. The backend caps this at 200; the UI uses 50. */
  limit: number;

  /** Pagination offset. The "load more" button increments this. */
  offset: number;
}

/**
 * Parameters for the usage summary query.
 *
 * `chat_id` and `project_id` are OPTIONAL filters on the wire
 * (the backend accepts their absence as "no filter"). Here they
 * are typed `string | null` to preserve the null-vs-undefined
 * hashing rule -- pass `null` when you do not want to filter.
 */
export interface UsageSummaryParams {
  period: UsagePeriod;
  chat_id: string | null;
  project_id: string | null;
}

/**
 * Parameters for the usage breakdown query.
 *
 * `by` is the dimension to slice tokens by. `source` is an
 * accepted backend alias for `scope`; the client SHOULD use the
 * canonical form (`scope`) when querying, and the response will
 * echo the normalized value.
 */
export interface UsageBreakdownParams {
  by: UsageBreakdownDimension;
  period: UsagePeriod;
  chat_id: string | null;
  project_id: string | null;
}

/**
 * Parameters for the usage timeseries query.
 *
 * `period` and `granularity` are NOT cross-validated by the
 * backend -- `{period: 'day', granularity: 'month'}` returns a
 * single-point series. Valid, if unusual.
 */
export interface UsageTimeseriesParams {
  period: UsagePeriod;
  granularity: UsageGranularity;
}

// ---------------------------------------------------------------------------
// Normalizers
// ---------------------------------------------------------------------------
//
// Each `normalize*` function returns a new object with exactly
// the fields declared in the params interface, in the interface's
// declared order, with any `undefined` coerced to `null`.
//
// Why normalize at all, if the interfaces already force explicit
// `null`?
//
//   1. DEFENSE IN DEPTH. A caller who has `strict: false` in
//      tsconfig, or who constructs params via `JSON.parse`, can
//      still slip `undefined` past the type system. The
//      normalizer catches it.
//
//   2. STABLE FIELD SET. TanStack hashes `{a: 1}` and
//      `{a: 1, b: undefined}` differently (JSON drops
//      `undefined`). The normalizer guarantees the field set is
//      always complete.
//
//   3. FUTURE-PROOFING. If a new field is added to an interface
//      but a call site forgot to pass it, the normalizer's
//      `?? null` fills it in rather than producing a
//      structurally different key.

function normalizeEntryListParams(p: EntryListParams): EntryListParams {
  return {
    scope: p.scope,
    status: p.status,
    memory_type: p.memory_type ?? null,
    is_pinned: p.is_pinned ?? null,
    limit: p.limit,
    offset: p.offset,
  };
}

function normalizeUsageSummaryParams(
  p: UsageSummaryParams,
): UsageSummaryParams {
  return {
    period: p.period,
    chat_id: p.chat_id ?? null,
    project_id: p.project_id ?? null,
  };
}

function normalizeUsageBreakdownParams(
  p: UsageBreakdownParams,
): UsageBreakdownParams {
  return {
    by: p.by,
    period: p.period,
    chat_id: p.chat_id ?? null,
    project_id: p.project_id ?? null,
  };
}

function normalizeUsageTimeseriesParams(
  p: UsageTimeseriesParams,
): UsageTimeseriesParams {
  return {
    period: p.period,
    granularity: p.granularity,
  };
}

// ---------------------------------------------------------------------------
// The key factory
// ---------------------------------------------------------------------------

/**
 * Canonical query-key factory for every memory query.
 *
 * Every function returns a `readonly` tuple -- TanStack Query's
 * `QueryKey` type is `readonly unknown[]`, so readonly tuples
 * satisfy it without a cast. Immutability also prevents the
 * classic "someone mutated the cached key array" bug.
 *
 * IMPORTANT: the entire object is `as const`. This makes:
 *
 *   * `memoryKeys.all` a literal `readonly ['memory']` -- usable
 *     as the root prefix for `invalidateQueries`.
 *   * the property graph deeply readonly -- so
 *     `memoryKeys.settings.global = ...` is a compile error.
 *
 * Each function ALSO has its own `as const` on its return
 * expression (required -- the outer `as const` does not narrow
 * function return types).
 */
export const memoryKeys = {
  /**
   * Root prefix. Invalidating this clears the ENTIRE memory
   * cache. Use only when you want a full reset (e.g. on logout,
   * or after the "disable memory" transition).
   */
  all: ['memory'] as const,

  // -------------------------------------------------------------------------
  // Settings
  // -------------------------------------------------------------------------

  settings: {
    /**
     * Prefix for ALL settings queries (global + every project).
     * Invalidating this resets both, which is correct after a
     * mode change (mode is a global setting but affects every
     * project's effective view).
     */
    all: () => [...memoryKeys.all, 'settings'] as const,

    /**
     * The global settings snapshot.
     *
     * Only ONE cache entry exists under this key -- the endpoint
     * takes no parameters. So `settings.global()` is both the
     * builder AND the invalidation prefix.
     */
    global: () => [...memoryKeys.settings.all(), 'global'] as const,

    /**
     * One project's (effective, override) settings pair.
     *
     * Invalidating `settings.project(id)` does NOT invalidate
     * sibling projects -- each project's override is
     * independent.
     */
    project: (projectId: string) =>
      [...memoryKeys.settings.all(), 'project', projectId] as const,
  },

  // -------------------------------------------------------------------------
  // Entries
  // -------------------------------------------------------------------------

  entries: {
    /**
     * Prefix for ALL entry queries (lists and details).
     *
     * Invalidating this is appropriate after bulk operations
     * that affect both list membership AND detail freshness --
     * e.g. bulk-purge, which can orphan every detail query.
     */
    all: () => [...memoryKeys.all, 'entries'] as const,

    /**
     * Prefix for ALL entry-list queries, regardless of params.
     *
     * Invalidating this catches every cached page. Use after
     * any write that can affect list membership (create,
     * update, delete, pin, import).
     */
    lists: () => [...memoryKeys.entries.all(), 'list'] as const,

    /**
     * One specific list (scope + filters + page).
     *
     * Normalizes params before embedding them, so two calls
     * with the same semantic params always produce the same
     * hash -- even if one caller omitted an `undefined` field
     * and the other passed `null`.
     */
    list: (params: EntryListParams) =>
      [
        ...memoryKeys.entries.lists(),
        normalizeEntryListParams(params),
      ] as const,

    /**
     * Prefix for ALL entry-detail queries, across all scopes.
     *
     * Invalidating this catches every detail view. Rarely
     * needed -- prefer `detail(id, scope)` for precision, or
     * rely on `lists()` invalidation which is usually enough.
     * Provided for the bulk-purge case where the affected ids
     * are not enumerable.
     */
    details: () => [...memoryKeys.entries.all(), 'detail'] as const,

    /**
     * One specific entry detail.
     *
     * The `scope` parameter is part of the key (not just the
     * id) because the backend's detail endpoint requires the
     * scope as a query parameter, and the same id in different
     * scopes is a distinct logical entity (even though
     * `memory_id` is globally unique in practice, the API
     * treats scope as part of the addressing scheme).
     */
    detail: (memoryId: string, scope: string) =>
      [...memoryKeys.entries.details(), memoryId, scope] as const,
  },

  // -------------------------------------------------------------------------
  // Projects
  // -------------------------------------------------------------------------

  projects: {
    /**
     * Prefix for ALL project-aggregation queries.
     */
    all: () => [...memoryKeys.all, 'projects'] as const,

    /**
     * The projects list (per-project counts).
     *
     * Only ONE cache entry exists -- the endpoint takes no
     * parameters.
     */
    list: () => [...memoryKeys.projects.all(), 'list'] as const,

    /**
     * One project's detailed statistics.
     *
     * Invalidating `projects.stats(id)` is appropriate after
     * any write that changes the project's entry counts or
     * distributions (create, delete, bulk-archive,
     * bulk-purge).
     */
    stats: (projectId: string) =>
      [...memoryKeys.projects.all(), 'stats', projectId] as const,
  },

  // -------------------------------------------------------------------------
  // Usage
  // -------------------------------------------------------------------------

  usage: {
    /**
     * Prefix for ALL usage queries.
     *
     * Invalidating `usage.all()` resets summary, breakdown, and
     * timeseries together. Appropriate after any settings
     * change that affects the token budget (because
     * `budget_utilization` and `drop_rate` in the summary
     * depend on the current budget).
     */
    all: () => [...memoryKeys.all, 'usage'] as const,

    /**
     * The usage summary for a period + optional filters.
     *
     * `period` and filters are part of the key: the summary
     * for "month, project:acme" is DIFFERENT data from
     * "month, all projects". Two entries coexist in cache.
     */
    summary: (params: UsageSummaryParams) =>
      [
        ...memoryKeys.usage.all(),
        'summary',
        normalizeUsageSummaryParams(params),
      ] as const,

    /**
     * The usage breakdown for a dimension + period + filters.
     *
     * The `by` dimension is part of the key -- switching from
     * "by scope" to "by project" is a DIFFERENT query, not a
     * refetch of the same one.
     */
    breakdown: (params: UsageBreakdownParams) =>
      [
        ...memoryKeys.usage.all(),
        'breakdown',
        normalizeUsageBreakdownParams(params),
      ] as const,

    /**
     * The usage timeseries for a period + granularity.
     *
     * No chat/project filters on this endpoint -- the backend
     * does not support them on `/usage/timeseries`.
     */
    timeseries: (params: UsageTimeseriesParams) =>
      [
        ...memoryKeys.usage.all(),
        'timeseries',
        normalizeUsageTimeseriesParams(params),
      ] as const,
  },

  // -------------------------------------------------------------------------
  // Import adapters
  // -------------------------------------------------------------------------

  importAdapters: {
    /**
     * The registered import adapters list.
     *
     * Fetched once when the import wizard opens. The list is
     * effectively static for the lifetime of the process --
     * adapters are registered at startup and do not change at
     * runtime in the current backend. So the query can use a
     * very long `staleTime` (see queries.ts). Included as a
     * query key anyway (rather than as a constant) so that the
     * wizard can render a loading state on first mount and a
     * cache hit on subsequent ones without any conditional
     * logic.
     */
    all: () => [...memoryKeys.all, 'import', 'adapters'] as const,
  },
} as const;

// ---------------------------------------------------------------------------
// Type helpers
// ---------------------------------------------------------------------------

/**
 * The type of a "fully-built" memory query key.
 *
 * Useful when declaring function parameters or React state that
 * holds a query key. Do NOT use this to construct keys -- use
 * the factory above.
 *
 * Note: this type only captures the KEY structure, not the
 * params that went into it. For type-safe invalidation with
 * exact params, call the specific factory function.
 */
export type MemoryQueryKey = readonly ['memory', ...readonly unknown[]];

/**
 * Detects whether a query key belongs to the memory feature.
 *
 * Used by generic utilities (e.g. a devtools filter, a logout
 * handler that wants to clear only the memory cache) that
 * receive keys from outside this module.
 *
 * NOTE: TanStack Query's `invalidateQueries({ queryKey: prefix })`
 * already does prefix matching internally -- this guard is for
 * callers who receive a single key from somewhere and need to
 * decide whether it is theirs to touch.
 */
export function isMemoryQueryKey(
  key: readonly unknown[] | undefined,
): key is MemoryQueryKey {
  return Array.isArray(key) && key.length > 0 && key[0] === 'memory';
}
