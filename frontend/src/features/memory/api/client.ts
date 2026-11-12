// src/features/memory/api/client.ts

/**
 * The typed HTTP client for the PELDRUN Memory API.
 *
 * This module is the ONLY place in the frontend that calls
 * `fetch('/api/memory/...')`. Every hook in `queries.ts` and
 * `mutations.ts` goes through the methods here. The rule exists
 * because:
 *
 *   1. **Error normalization must be uniform.** Every non-ok
 *      response is turned into a `MemoryApiError` via
 *      `classifyMemoryError`. Bypassing this module reintroduces
 *      the "which of the four error shapes did the backend
 *      send?" problem at every call site.
 *
 *   2. **Network failures must be handled identically.** A
 *      `fetch()` that throws (offline, DNS failure, TLS error)
 *      is not an HTTP status -- without this module's wrapper,
 *      call sites would catch a bare `TypeError` and have no
 *      uniform way to distinguish it from a programmer error.
 *
 *   3. **Abort signals must be honored everywhere.** React
 *      Query passes a `signal` for cancellation; if a method
 *      ignores it, cancelled queries still complete and update
 *      state after unmount. This module threads `signal`
 *      through every request.
 *
 *   4. **Types must flow end-to-end.** Every method's return
 *      type is the specific `*Response` envelope from
 *      `types/responses.ts`. A caller that forgets to handle an
 *      error path cannot accidentally treat the error body as a
 *      success -- `MemoryApiError` must be caught first.
 *
 * Base URL
 * --------
 *
 * `memoryClient` talks to `/api/memory` by default. The
 * frontend's Next.js app exposes a same-origin proxy under
 * `app/api/memory-proxy/[...path]` for the case where the
 * backend runs on a different host. The default
 * `MEMORY_API_BASE` works in both setups:
 *
 *   * Same origin: Next.js rewrites `/api/memory/*` to the
 *     backend (or the proxy Route Handler forwards it).
 *   * Proxy: set `MEMORY_API_BASE = '/api/memory-proxy'` at
 *     build time via `NEXT_PUBLIC_MEMORY_API_BASE`.
 *
 * The base URL is read from `NEXT_PUBLIC_MEMORY_API_BASE` at
 * module load, so tests and Storybook can override it with a
 * relative base that points at a local mock.
 *
 * Public surface
 * --------------
 *
 * Every endpoint the memory UI needs, grouped by concern:
 *
 *   * `settings.*` -- global + per-project settings.
 *   * `entries.*`  -- CRUD, search, promote, pin.
 *   * `projects.*` -- aggregation + bulk operations.
 *   * `usage.*`    -- summary, breakdown, timeseries.
 *   * `imports.*`  -- preview, apply, adapters list.
 *   * `exports.*`  -- download (zip blob or json body).
 */

import {
  classifyMemoryError,
  MemoryApiError,
  MemoryErrorType,
  type KnownMemoryErrorType,
} from '../types/errors';
import type { MemoryEntry, MemoryScopeRefString } from '../types/entry';
import type { MemoryMode, MemoryType } from '../types/enums';
import type {
  BulkArchiveResponse,
  BulkPurgeResponse,
  CreateMemoryResponse,
  DeleteMemoryResponse,
  ExportJsonResponse,
  GetMemoryResponse,
  ImportAdaptersResponse,
  ImportApplyResponse,
  ImportPreviewResponse,
  ListMemoryResponse,
  ListProjectsResponse,
  PinMemoryResponse,
  ProjectSettingsDeleteResponse,
  ProjectSettingsGetResponse,
  ProjectSettingsPutResponse,
  ProjectStatsResponse,
  PromoteMemoryResponse,
  SearchMemoryResponse,
  SettingsGetResponse,
  SettingsUpdateResponse,
  UpdateMemoryResponse,
  UsageBreakdownResponse,
  UsageSummaryResponse,
  UsageTimeseriesResponse,
} from '../types/responses';
import type {
  MemoryProjectOverride,
  MemorySettingsPatch,
} from '../types/settings';
import type {
  ExportDownloadResult,
  ExportQueryParams,
  ImportJsonRequest,
  ImportMultipartRequest,
  ImportTargetScope,
} from '../types/import-export';

// ---------------------------------------------------------------------------
// Base URL
// ---------------------------------------------------------------------------

/**
 * The base URL every request is prefixed with.
 *
 * Read from `NEXT_PUBLIC_MEMORY_API_BASE` so that environments
 * that route memory traffic through a proxy (or a different
 * origin during local development) do not need a code change.
 * The default works for the standard same-origin Next.js setup.
 *
 * NOTE: this is a MODULE-LEVEL constant, evaluated once at load
 * time. Tests that need a per-test base URL should stub
 * `globalThis.fetch` instead of overriding this constant.
 */
const MEMORY_API_BASE: string =
  (typeof process !== 'undefined' &&
    process.env.NEXT_PUBLIC_MEMORY_API_BASE) ||
  '/api/memory';

// ---------------------------------------------------------------------------
// Low-level fetch wrapper
// ---------------------------------------------------------------------------

/**
 * Options accepted by `memoryFetch`.
 *
 * Extends the standard `RequestInit` minus `body` and
 * `headers['Content-Type']` -- those are handled by the wrapper
 * (JSON stringification, Content-Type injection) unless the
 * caller overrides them for a multipart upload.
 */
interface MemoryFetchOptions extends Omit<RequestInit, 'body'> {
  /**
   * A JSON-serializable body. The wrapper stringifies it and
   * sets `Content-Type: application/json` automatically.
   *
   * Pass `FormData` here for multipart uploads -- the wrapper
   * detects it and does NOT stringify or set Content-Type (the
   * browser sets the boundary-laden Content-Type itself).
   */
  json?: unknown;
}

/**
 * Serialize a query-params record to a URLSearchParams.
 *
 * Skips `undefined` and `null` values -- the backend treats
 * their absence as "no filter". Booleans and numbers are
 * stringified; arrays are joined by the caller (no endpoint in
 * this API accepts an array parameter, so this module
 * deliberately does not support one).
 *
 * Empty-string values are KEPT. The distinction matters for
 * `?search=` (an explicit empty query, meaning "list all") vs
 * omitting `search` entirely -- the backend treats them
 * differently for some endpoints. If a specific endpoint wants
 * "omit when empty", the caller should pass `undefined` rather
 * than `''`.
 */
function buildQueryString(
  params: Record<string, string | number | boolean | null | undefined>,
): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    usp.set(key, String(value));
  }
  const query = usp.toString();
  return query ? `?${query}` : '';
}

/**
 * The core fetch wrapper.
 *
 * Responsibilities:
 *
 *   1. Prefix `path` with `MEMORY_API_BASE`.
 *   2. Stringify `json` bodies and set the Content-Type.
 *      Leave `FormData` bodies untouched (the browser sets the
 *      multipart Content-Type with the boundary).
 *   3. Parse the JSON response body (tolerantly -- `null` on
 *      empty / non-JSON bodies).
 *   4. On `response.ok === false`, throw `MemoryApiError` via
 *      `classifyMemoryError`.
 *   5. On a `fetch()` rejection (network failure, abort,
 *      timeout), throw `MemoryApiError` with
 *      `errorType: 'NetworkError'` -- EXCEPT when the rejection
 *      is an `AbortError`, which is re-thrown verbatim so React
 *      Query's cancellation logic works.
 *   6. Return the parsed body typed as `T`.
 *
 * The wrapper NEVER returns an error body -- every non-ok
 * status throws. This is the "errors as exceptions" convention
 * that pairs naturally with React Query's error handling and
 * with TypeScript's narrowing (`catch (e)` -> `MemoryApiError`
 * after the classifier is the ONLY thrower).
 */
async function memoryFetch<T>(
  path: string,
  options: MemoryFetchOptions = {},
): Promise<T> {
  const { json, headers, ...rest } = options;

  const finalHeaders = new Headers(headers);

  let body: BodyInit | undefined;
  if (json instanceof FormData) {
    // Browser sets Content-Type automatically (with boundary).
    // Setting it here would OVERRIDE the correct value.
    body = json;
  } else if (json !== undefined) {
    if (!finalHeaders.has('Content-Type')) {
      finalHeaders.set('Content-Type', 'application/json');
    }
    body = JSON.stringify(json);
  }

  // Accept header is set explicitly so that a future backend
  // change (e.g. content negotiation) does not break the client.
  if (!finalHeaders.has('Accept')) {
    finalHeaders.set('Accept', 'application/json');
  }

  const url = `${MEMORY_API_BASE}${path}`;

  let response: Response;
  try {
    response = await fetch(url, {
      ...rest,
      headers: finalHeaders,
      body,
    });
  } catch (err) {
    // AbortError must propagate verbatim so React Query can
    // recognize the cancellation and NOT surface it as an error.
    // `DOMException` with name 'AbortError' is the standard
    // shape; the `err instanceof DOMException` check is defensive
    // against a bare `Error` with the same name in older setups.
    if (
      err instanceof DOMException &&
      err.name === 'AbortError'
    ) {
      throw err;
    }
    if (
      err instanceof Error &&
      err.name === 'AbortError'
    ) {
      throw err;
    }

    // Everything else -- DNS failure, TLS error, offline,
    // CORS preflight failure -- becomes a NetworkError. The
    // caller sees a uniform `MemoryApiError` with a retryable
    // errorType. `statusCode: 0` is the conventional marker
    // for "no HTTP status received".
    const message =
      err instanceof Error
        ? err.message
        : 'Network request failed before reaching the server.';
    throw new MemoryApiError({
      operation: 'network',
      errorType: MemoryErrorType.NetworkError,
      statusCode: 0,
      message,
      rawBody: err,
    });
  }

  // Parse the body leniently. An empty 204/205, a truncated
  // response, or a proxy's HTML error page all produce `null`
  // here -- the classifier below handles the `null` case with
  // a status-derived fallback.
  let parsed: unknown = null;
  const contentType = response.headers.get('content-type') || '';
  if (response.status !== 204 && response.status !== 205) {
    if (contentType.includes('application/json')) {
      try {
        parsed = await response.json();
      } catch {
        // Invalid JSON in the response. Leave `parsed` as
        // `null`; the classifier will synthesize a sensible
        // error from the status code if needed.
        parsed = null;
      }
    } else {
      // Non-JSON body on an error response (e.g. an nginx 502
      // HTML page). Do not attempt to parse it -- the raw
      // text can be useful for logs but not for the classifier.
      try {
        parsed = await response.text();
      } catch {
        parsed = null;
      }
    }
  }

  if (!response.ok) {
    throw classifyMemoryError(
      response.status,
      parsed,
      extractOperationFromPath(path),
    );
  }

  // Success with a non-JSON body is a programming error at the
  // call site -- `memoryFetch` is documented to return JSON.
  // The export download path uses `downloadExport` (below)
  // which reads blobs, not this function.
  if (parsed === null && response.status !== 204 && response.status !== 205) {
    // Some endpoints (e.g. bulk-purge with an empty body on
    // 0 entries -- not currently the case, but future-proofing)
    // may legitimately return an empty 200. Treat empty as
    // `null as T` -- the caller's type will guide them.
    return undefined as T;
  }

  return parsed as T;
}

/**
 * Extract a human-readable operation name from a request path.
 *
 * Used as the `fallbackOperation` when a non-ok response has no
 * `operation` field of its own (which is the case for ALL shape
 * 2-4 errors -- see `errors.ts`). This makes server-side logs
 * and user-facing error reports identify the failing action
 * without needing a per-call-site operation constant.
 *
 * The mapping is heuristic: it strips the leading `/` and
 * replaces `/` with `_`. e.g.:
 *
 *   `/settings`               -> `settings`
 *   `/settings/projects/x`    -> `settings_projects_x`
 *   `/entries/mem_abc/pin`    -> `entries_mem_abc_pin`
 *
 * Callers with a more precise operation name (e.g. mutations
 * that know they are running `create`) pass it explicitly to
 * `classifyMemoryError` via their own wrapper.
 */
function extractOperationFromPath(path: string): string {
  const trimmed = path.startsWith('/') ? path.slice(1) : path;
  // Strip query strings if a caller accidentally included one.
  const noQuery = trimmed.split('?')[0];
  return noQuery.replace(/\//g, '_') || 'unknown';
}

// ---------------------------------------------------------------------------
// Public client
// ---------------------------------------------------------------------------

/**
 * The typed memory API client.
 *
 * Every method's signature mirrors its endpoint one-to-one:
 * query parameters become a params object, body fields become
 * a request object, and the return type is the specific
 * `*Response` envelope.
 *
 * Methods that mutate state (`create`, `update`, `delete`,
 * `pin`, `promote`, bulk operations) do NOT invalidate the
 * query cache themselves. Invalidation is the responsibility
 * of the mutation hooks in `mutations.ts` -- the client stays a
 * pure transport layer. This separation matters because a
 * mutation may need to invalidate MULTIPLE keys (e.g. create
 * invalidates entries.lists, projects.list, projects.stats),
 * and baking that policy into the client would couple
 * transport to cache concerns.
 */
export const memoryClient = {
  // -------------------------------------------------------------------------
  // Settings
  // -------------------------------------------------------------------------

  settings: {
    /**
     * `GET /api/memory/settings`.
     *
     * Always succeeds regardless of `mode` -- the backend
     * returns the settings even when `mode === 'disabled'`. The
     * settings page depends on this (see the spec's "no mode
     * gates on settings" rule).
     */
    get: (opts: { signal?: AbortSignal } = {}) =>
      memoryFetch<SettingsGetResponse>('/settings', {
        method: 'GET',
        signal: opts.signal,
      }),

    /**
     * `PATCH /api/memory/settings`.
     *
     * Applies a partial update. The body MUST contain at least
     * one field; the backend rejects empty patches with
     * `400 empty_update`. Callers should use
     * `isNoOpPatch()` from `types/settings.ts` to guard.
     */
    patch: (patch: MemorySettingsPatch, opts: { signal?: AbortSignal } = {}) =>
      memoryFetch<SettingsUpdateResponse>('/settings', {
        method: 'PATCH',
        json: patch,
        signal: opts.signal,
      }),

    /**
     * `GET /api/memory/settings/projects/{id}`.
     *
     * Returns both `effective` (merged) and `override` (raw).
     * The response type carries both; do NOT read `override`
     * directly to infer "what did the user customize?" -- use
     * `computeEffectiveOverride` for that (see settings.ts).
     */
    getProject: (projectId: string, opts: { signal?: AbortSignal } = {}) =>
      memoryFetch<ProjectSettingsGetResponse>(
        `/settings/projects/${encodeURIComponent(projectId)}`,
        { method: 'GET', signal: opts.signal },
      ),

    /**
     * `PUT /api/memory/settings/projects/{id}`.
     *
     * Full REPLACEMENT of the project's override slot. To merge
     * with an existing override, GET first, merge client-side,
     * PUT back.
     */
    putProject: (
      projectId: string,
      override: MemoryProjectOverride,
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<ProjectSettingsPutResponse>(
        `/settings/projects/${encodeURIComponent(projectId)}`,
        { method: 'PUT', json: override, signal: opts.signal },
      ),

    /**
     * `DELETE /api/memory/settings/projects/{id}`.
     *
     * Idempotent. Deleting an override that does not exist
     * returns 200 with the global settings as `effective`.
     */
    deleteProject: (
      projectId: string,
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<ProjectSettingsDeleteResponse>(
        `/settings/projects/${encodeURIComponent(projectId)}`,
        { method: 'DELETE', signal: opts.signal },
      ),
  },

  // -------------------------------------------------------------------------
  // Entries (CRUD + search + promote + pin)
  // -------------------------------------------------------------------------

  entries: {
    /**
     * `GET /api/memory` -- list entries in one scope.
     *
     * The endpoint requires a SINGLE explicit scope; passing
     * both a scope pair AND a chat_id/project_id would trigger
     * `400 multiple_scopes_ambiguous`. This method exposes the
     * scope pair directly (no contextual expansion) so that
     * callers cannot accidentally create an ambiguous request.
     */
    list: (
      params: {
        scope_type: string;
        scope_id: string;
        limit?: number;
        offset?: number;
        status?: 'active' | 'all';
        memory_type?: MemoryType | null;
        is_pinned?: boolean | null;
      },
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<ListMemoryResponse>(
        `/memory${buildQueryString({
          scope_type: params.scope_type,
          scope_id: params.scope_id,
          limit: params.limit,
          offset: params.offset,
          status: params.status,
          memory_type: params.memory_type ?? undefined,
          is_pinned: params.is_pinned ?? undefined,
        })}`.replace(/^\/memory/, ''),
        { method: 'GET', signal: opts.signal },
      ),

    /**
     * `POST /api/memory` -- create a new entry.
     *
     * The `source_type` must be one of the permitted values
     * (`explicit_user`, `agent_tool`, `extraction`). `import`
     * is rejected by the backend -- use the import endpoint
     * for imported content.
     *
     * NOTE: unlike `search`, this endpoint does NOT go through
     * the write pipeline -- it inserts directly via
     * `provider.create`. The response is always a fresh entry;
     * there is no `skipped` / `superseded` / `reactivated`
     * outcome on this path.
     */
    create: (
      body: {
        content: string;
        memory_type?: MemoryType;
        source_type?: string;
        scope_type?: string;
        scope_id?: string;
        chat_id?: string;
        project_id?: string;
        importance?: number;
        tags?: string[];
        is_pinned?: boolean;
      },
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<CreateMemoryResponse>('', {
        method: 'POST',
        json: body,
        signal: opts.signal,
      }),

    /**
     * `POST /api/memory/search` -- preview retrieval results.
     *
     * Read-only. Returns the entries that WOULD be injected into
     * a prompt for the given query, along with token accounting
     * (`used_tokens`, `token_budget`, `dropped_for_budget`) and
     * the `formatted` string the model would see.
     *
     * `scope_refs` in the response is the RESOLVED list (after
     * contextual expansion of `chat_id` / `project_id`). Use it
     * to display which scopes were actually consulted.
     */
    search: (
      body: {
        query: string;
        scope_type?: string;
        scope_id?: string;
        chat_id?: string;
        project_id?: string;
        user_id?: string;
        limit?: number;
      },
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<SearchMemoryResponse>('/search', {
        method: 'POST',
        json: body,
        signal: opts.signal,
      }),

    /**
     * `GET /api/memory/{id}` -- fetch one entry.
     *
     * The `scope_type` + `scope_id` query parameters are
     * MANDATORY on this endpoint. The `scope` value placed into
     * the URL is derived from them (they are the same pair the
     * caller passes to the query key -- see keys.ts).
     *
     * Response is `MemoryApiError` with `errorType: 'not_found'`
     * when the entry does not exist in the given scope. This is
     * NOT distinguished at the type level (it is a runtime
     * branch) -- callers should catch and check
     * `error.errorType === 'not_found'` explicitly.
     */
    get: (
      params: { memoryId: string; scope_type: string; scope_id: string },
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<GetMemoryResponse>(
        `/${encodeURIComponent(params.memoryId)}${buildQueryString({
          scope_type: params.scope_type,
          scope_id: params.scope_id,
        })}`,
        { method: 'GET', signal: opts.signal },
      ),

    /**
     * `PATCH /api/memory/{id}` -- partial update.
     *
     * The body MUST contain at least one field; the backend
     * rejects empty patches with `400 empty_update`. Each field
     * in the body overwrites the corresponding entry field --
     * this is NOT a deep merge (e.g. `tags` fully replaces the
     * existing tags array).
     *
     * The entry's `scope` (both type and id) is IMMUTABLE via
     * this endpoint. Use `/promote` to copy into user scope.
     */
    update: (
      params: {
        memoryId: string;
        scope_type: string;
        scope_id: string;
      },
      patch: {
        content?: string;
        importance?: number;
        tags?: string[];
        is_pinned?: boolean;
        memory_type?: MemoryType;
      },
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<UpdateMemoryResponse>(
        `/${encodeURIComponent(params.memoryId)}${buildQueryString({
          scope_type: params.scope_type,
          scope_id: params.scope_id,
        })}`,
        { method: 'PATCH', json: patch, signal: opts.signal },
      ),

    /**
     * `DELETE /api/memory/{id}` -- soft (default) or hard delete.
     *
     * Soft delete (`hard: false`) sets `status: 'archived'`. The
     * entry remains queryable with `?status=all` until purged.
     *
     * Hard delete (`hard: true`) is PERMANENT. The FTS index
     * entries are cleaned by a trigger -- no manual cleanup
     * needed. The UI MUST gate this behind a type-to-confirm
     * dialog (see `ConfirmDangerDialog`).
     */
    delete: (
      params: {
        memoryId: string;
        scope_type: string;
        scope_id: string;
        hard?: boolean;
      },
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<DeleteMemoryResponse>(
        `/${encodeURIComponent(params.memoryId)}${buildQueryString({
          scope_type: params.scope_type,
          scope_id: params.scope_id,
          hard: params.hard,
        })}`,
        { method: 'DELETE', signal: opts.signal },
      ),

    /**
     * `POST /api/memory/{id}/promote` -- COPY into user scope.
     *
     * The source entry is untouched; the response carries a NEW
     * entry in the target scope. The UI MUST state this in the
     * confirmation dialog ("this will copy, the original
     * stays") -- users expect move semantics by default.
     *
     * `target_scope_type` is always `'user'` in the current
     * backend; other target scopes are rejected with
     * `400 unsupported_target_scope`.
     */
    promote: (
      params: {
        memoryId: string;
        scope_type: string;
        scope_id: string;
      },
      body: {
        target_scope_type?: 'user';
        target_scope_id?: string;
      },
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<PromoteMemoryResponse>(
        `/${encodeURIComponent(params.memoryId)}/promote${buildQueryString({
          scope_type: params.scope_type,
          scope_id: params.scope_id,
        })}`,
        { method: 'POST', json: body, signal: opts.signal },
      ),

    /**
     * `POST /api/memory/{id}/pin` -- set pin state (toggle-style).
     *
     * The body's `pinned` field sets the TARGET state, not a
     * toggle. Sending `{pinned: true}` on an already-pinned
     * entry is a no-op that still returns 200.
     *
     * `importance` is optional; when omitted, the current
     * importance is preserved.
     */
    pin: (
      params: {
        memoryId: string;
        scope_type: string;
        scope_id: string;
      },
      body: { pinned: boolean; importance?: number },
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<PinMemoryResponse>(
        `/${encodeURIComponent(params.memoryId)}/pin${buildQueryString({
          scope_type: params.scope_type,
          scope_id: params.scope_id,
        })}`,
        { method: 'POST', json: body, signal: opts.signal },
      ),
  },

  // -------------------------------------------------------------------------
  // Projects
  // -------------------------------------------------------------------------

  projects: {
    /**
     * `GET /api/memory/projects` -- list projects with memory.
     *
     * Only projects with at least one entry appear; empty
     * projects are intentionally absent.
     */
    list: (opts: { signal?: AbortSignal } = {}) =>
      memoryFetch<ListProjectsResponse>('/projects', {
        method: 'GET',
        signal: opts.signal,
      }),

    /**
     * `GET /api/memory/projects/{id}/stats` -- detailed stats.
     *
     * Empty projects (reachable via a direct URL) return empty
     * `types`/`top_tags` arrays and sentinel-valued
     * `importance`/`created_range`. Render defensively.
     */
    stats: (projectId: string, opts: { signal?: AbortSignal } = {}) =>
      memoryFetch<ProjectStatsResponse>(
        `/projects/${encodeURIComponent(projectId)}/stats`,
        { method: 'GET', signal: opts.signal },
      ),

    /**
     * `POST /api/memory/projects/{id}/bulk-archive`.
     *
     * Idempotent. `archived_count` in the response is how many
     * ACTIVE entries transitioned on THIS call; already-archived
     * entries are untouched.
     *
     * Requires `mode=read_write`; rejected with
     * `errorType: 'mode_read_only'` otherwise.
     */
    bulkArchive: (projectId: string, opts: { signal?: AbortSignal } = {}) =>
      memoryFetch<BulkArchiveResponse>(
        `/projects/${encodeURIComponent(projectId)}/bulk-archive`,
        { method: 'POST', signal: opts.signal },
      ),

    /**
     * `POST /api/memory/projects/{id}/bulk-purge?confirm=yes`.
     *
     * PERMANENT deletion of every entry in the project. The
     * `confirm` query parameter is required by the backend
     * (rejects with `400 confirmation_required` if absent or
     * not equal to `"yes"`, case-insensitive).
     *
     * The UI MUST gate this behind a type-project-name-to-confirm
     * dialog on top of the confirm param.
     */
    bulkPurge: (projectId: string, opts: { signal?: AbortSignal } = {}) =>
      memoryFetch<BulkPurgeResponse>(
        `/projects/${encodeURIComponent(projectId)}/bulk-purge?confirm=yes`,
        { method: 'POST', signal: opts.signal },
      ),
  },

  // -------------------------------------------------------------------------
  // Usage
  // -------------------------------------------------------------------------

  usage: {
    /**
     * `GET /api/memory/usage` -- summary KPIs.
     *
     * `period` defaults to `"all"` on the backend; callers
     * should pass an explicit value from `UsagePeriod` for
     * consistent caching (different periods are distinct
     * queries, see keys.ts).
     */
    summary: (
      params: {
        period?: 'day' | 'week' | 'month' | 'all';
        chat_id?: string | null;
        project_id?: string | null;
      } = {},
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<UsageSummaryResponse>(
        `/usage${buildQueryString({
          period: params.period,
          chat_id: params.chat_id ?? undefined,
          project_id: params.project_id ?? undefined,
        })}`,
        { method: 'GET', signal: opts.signal },
      ),

    /**
     * `GET /api/memory/usage/breakdown` -- per-dimension slice.
     *
     * `source` is accepted as an alias for `scope` on the wire,
     * and the response's `by` field echoes the NORMALIZED value
     * (`scope`). Callers who display "grouped by: <by>" should
     * use the response's `by`, not their input.
     */
    breakdown: (
      params: {
        by?: 'scope' | 'source' | 'project' | 'chat';
        period?: 'day' | 'week' | 'month' | 'all';
        chat_id?: string | null;
        project_id?: string | null;
      } = {},
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<UsageBreakdownResponse>(
        `/usage/breakdown${buildQueryString({
          by: params.by,
          period: params.period,
          chat_id: params.chat_id ?? undefined,
          project_id: params.project_id ?? undefined,
        })}`,
        { method: 'GET', signal: opts.signal },
      ),

    /**
     * `GET /api/memory/usage/timeseries` -- bucketed series.
     *
     * The endpoint does NOT accept chat_id/project_id filters
     * (see memory_usage.py). `period`/`granularity` are not
     * cross-validated -- `{period: 'day', granularity: 'month'}`
     * returns one bucket.
     */
    timeseries: (
      params: {
        period?: 'day' | 'week' | 'month' | 'all';
        granularity?: 'day' | 'week' | 'month';
      } = {},
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<UsageTimeseriesResponse>(
        `/usage/timeseries${buildQueryString({
          period: params.period,
          granularity: params.granularity,
        })}`,
        { method: 'GET', signal: opts.signal },
      ),
  },

  // -------------------------------------------------------------------------
  // Import
  // -------------------------------------------------------------------------

  imports: {
    /**
     * `GET /api/memory/import/adapters` -- registered adapters.
     *
     * Response does NOT carry an `operation` field (documented
     * as a backend inconsistency in responses.ts). The type
     * reflects the actual wire shape.
     */
    listAdapters: (opts: { signal?: AbortSignal } = {}) =>
      memoryFetch<ImportAdaptersResponse>('/import/adapters', {
        method: 'GET',
        signal: opts.signal,
      }),

    /**
     * `POST /api/memory/import/preview` -- parse without writing.
     *
     * Accepts either a multipart request (file upload) or a
     * JSON request (inline source). The two builder helpers
     * below (`previewFromFile`, `previewFromSource`) encode the
     * content-type choice explicitly so call sites cannot mix
     * the two.
     */
    previewFromFile: (
      req: ImportMultipartRequest,
      opts: { signal?: AbortSignal } = {},
    ) => {
      const fd = new FormData();
      fd.set('file', req.file);
      fd.set('scope_type', req.target.scope_type);
      fd.set('scope_id', req.target.scope_id);
      if (req.adapter_id) fd.set('adapter_id', req.adapter_id);
      return memoryFetch<ImportPreviewResponse>('/import/preview', {
        method: 'POST',
        json: fd,
        signal: opts.signal,
      });
    },

    previewFromSource: (
      req: ImportJsonRequest,
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<ImportPreviewResponse>('/import/preview', {
        method: 'POST',
        json: {
          source: req.source,
          scope_type: req.target.scope_type,
          scope_id: req.target.scope_id,
          adapter_id: req.adapter_id ?? null,
        },
        signal: opts.signal,
      }),

    /**
     * `POST /api/memory/import/apply` -- write every viable
     * candidate.
     *
     * Best-effort: a single candidate's failure does not abort
     * the batch. The response reports per-candidate errors in
     * `result.errors`. Callers MUST show created/skipped counts
     * FIRST, then a collapsible errors panel.
     *
     * The two builder helpers mirror `previewFromFile` /
     * `previewFromSource` -- same request shape, different URL.
     */
    applyFromFile: (
      req: ImportMultipartRequest,
      opts: { signal?: AbortSignal } = {},
    ) => {
      const fd = new FormData();
      fd.set('file', req.file);
      fd.set('scope_type', req.target.scope_type);
      fd.set('scope_id', req.target.scope_id);
      if (req.adapter_id) fd.set('adapter_id', req.adapter_id);
      return memoryFetch<ImportApplyResponse>('/import/apply', {
        method: 'POST',
        json: fd,
        signal: opts.signal,
      });
    },

    applyFromSource: (
      req: ImportJsonRequest,
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<ImportApplyResponse>('/import/apply', {
        method: 'POST',
        json: {
          source: req.source,
          scope_type: req.target.scope_type,
          scope_id: req.target.scope_id,
          adapter_id: req.adapter_id ?? null,
        },
        signal: opts.signal,
      }),
  },

  // -------------------------------------------------------------------------
  // Export
  // -------------------------------------------------------------------------

  exports: {
    /**
     * `GET /api/memory/export?format=json` -- fetch the JSON
     * package as a typed body.
     *
     * Useful for the "inspect before download" panel. The
     * response body IS the package (`{manifest, memories}`) --
     * NOT a `status: "ok"` envelope (see `ExportJsonResponse`'s
     * JSDoc for the inconsistency).
     */
    downloadJson: (
      params: ExportQueryParams,
      opts: { signal?: AbortSignal } = {},
    ) =>
      memoryFetch<ExportJsonResponse>(
        `/export${buildQueryString({
          scope_type: params.scope_type,
          scope_id: params.scope_id,
          format: 'json',
          status: params.status,
          limit: params.limit,
        })}`,
        { method: 'GET', signal: opts.signal },
      ),

    /**
     * `GET /api/memory/export?format=zip` -- fetch the zip
     * package as a Blob.
     *
     * This method does NOT go through `memoryFetch` because the
     * response is binary, not JSON. It duplicates the
     * error-normalization path so that a failed zip download
     * still throws a `MemoryApiError` (the backend returns a
     * JSON error envelope even for zip requests that fail at
     * the parameter-validation stage -- the failure happens
     * BEFORE serialization to zip).
     *
     * The returned `ExportDownloadResult` gathers the Blob and
     * the `X-Export-*` headers. The UI's success screen uses
     * `filename` (parsed from `Content-Disposition`), `truncated`,
     * `entriesCount`, and `totalMatched`.
     */
    downloadZip: async (
      params: ExportQueryParams,
      opts: { signal?: AbortSignal } = {},
    ): Promise<ExportDownloadResult> => {
      const url = `${MEMORY_API_BASE}/export${buildQueryString({
        scope_type: params.scope_type,
        scope_id: params.scope_id,
        format: 'zip',
        status: params.status,
        limit: params.limit,
      })}`;

      let response: Response;
      try {
        response = await fetch(url, {
          method: 'GET',
          signal: opts.signal,
          headers: { Accept: 'application/zip, application/json' },
        });
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          throw err;
        }
        throw new MemoryApiError({
          operation: 'export',
          errorType: MemoryErrorType.NetworkError,
          statusCode: 0,
          message:
            err instanceof Error
              ? err.message
              : 'Network request failed before reaching the server.',
          rawBody: err,
        });
      }

      if (!response.ok) {
        // The backend's `_error_response` path writes JSON even
        // for zip requests -- the error happens before the zip
        // serialization step. So we can parse and classify
        // normally.
        let parsed: unknown = null;
        try {
          parsed = await response.json();
        } catch {
          parsed = null;
        }
        throw classifyMemoryError(
          response.status,
          parsed,
          'export',
        );
      }

      const blob = await response.blob();

      const filename = parseContentDispositionFilename(
        response.headers.get('content-disposition'),
      ) ?? `memory-export-${params.scope_type}-${params.scope_id}.zip`;

      const entriesCount = parseInt(
        response.headers.get('x-export-entries') ?? '0',
        10,
      );
      const totalMatched = parseInt(
        response.headers.get('x-export-total-matched') ?? '0',
        10,
      );
      const truncated =
        (response.headers.get('x-export-truncated') ?? 'false') === 'true';

      const scopeHeader =
        response.headers.get('x-export-scope') ?? '';

      return {
        filename,
        sizeBytes: blob.size,
        scope: scopeHeader,
        entriesCount,
        truncated,
        totalMatched: totalMatched || entriesCount,
        blob,
      };
    },
  },
};

// ---------------------------------------------------------------------------
// Header parsing helper
// ---------------------------------------------------------------------------

/**
 * Parse the `filename*` (RFC 5987) or `filename` value from a
 * `Content-Disposition` header.
 *
 * Prefers the UTF-8 variant (`filename*=UTF-8''...`) because it
 * preserves non-ASCII characters, falling back to the ASCII
 * variant (`filename="..."`) for older setups. Returns `null`
 * when neither is present.
 *
 * The UTF-8 variant's value is percent-encoded; decode it with
 * `decodeURIComponent`. If decoding throws (malformed header),
 * fall back to the raw value rather than failing the download.
 */
function parseContentDispositionFilename(
  header: string | null,
): string | null {
  if (!header) return null;

  // Priority 1: RFC 5987 UTF-8 variant.
  const utf8Match = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (utf8Match) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return utf8Match[1];
    }
  }

  // Priority 2: ASCII quoted variant.
  const asciiMatch = /filename="([^"]+)"/i.exec(header);
  if (asciiMatch) {
    return asciiMatch[1];
  }

  return null;
}

// ---------------------------------------------------------------------------
// Convenience re-exports
// ---------------------------------------------------------------------------
//
// Consumers of the client typically also need the error class
// and classifier (to catch and inspect). Re-exporting them here
// saves a second import statement in the common case where a
// mutation hook's `onError` handler needs both the client and
// the error type.

export { MemoryApiError } from '../types/errors';
export type { KnownMemoryErrorType };
