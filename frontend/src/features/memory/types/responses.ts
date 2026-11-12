// src/features/memory/types/responses.ts

/**
 * HTTP response envelope types for every memory API endpoint.
 *
 * This module is the SINGLE SOURCE OF TRUTH for what each
 * endpoint returns. Every type here was transcribed directly from
 * the corresponding ``JSONResponse`` construction in the backend
 * routers -- not from the spec prose. Where the spec and the code
 * disagree, the CODE wins, and the discrepancy is documented
 * inline (see the ``ImportAdaptersResponse`` and
 * ``ExportJsonResponse`` entries below).
 *
 * Scope of this module
 * --------------------
 *
 *   * ``responses.ts`` (this file) -- success envelopes ONLY.
 *
 *   * ``errors.ts``                -- error envelopes and the
 *                                     normalization helper.
 *
 * Separating the two means a caller who handles the happy path
 * does not need to also import error types, and vice-versa.
 *
 * Naming convention
 * -----------------
 *
 * Every interface is named ``<Endpoint>Response`` where
 * ``<Endpoint>`` is the semantic action (``ListMemoryResponse``,
 * ``SearchMemoryResponse``, ...) rather than the URL path. Two
 * different URLs can share a semantic action (e.g. ``GET
 * /settings/projects/{id}`` and ``PUT /settings/projects/{id}``
 * are distinct actions but each has its own response type). The
 * URL is documented in each interface's JSDoc.
 *
 * Envelope shape
 * --------------
 *
 * The vast majority of endpoints follow the uniform envelope::
 *
 *     {"status": "ok", "operation": "<op>", ...payload}
 *
 * Two endpoints BREAK this pattern (documented at their type):
 * ``GET /import/adapters`` omits ``operation``; ``GET
 * /export?format=json`` omits both ``status`` and ``operation``
 * and returns the raw package shape instead. Consumers MUST NOT
 * assume ``status`` is present on every response -- check the
 * specific type.
 */

import type {
  MemoryEntry,
  MemoryScopeRefString,
} from './entry';
import type { RouterDecision } from './enums';
import type {
  MemoryProjectOverride,
  MemorySettings,
  MemorySettingsOverlay,
} from './settings';

// ---------------------------------------------------------------------------
// Base envelope
// ---------------------------------------------------------------------------

/**
 * The base shape every "standard" memory API response extends.
 *
 * Individual response types below extend this interface -- that
 * way a consumer can narrow ``response.status === 'ok'`` once and
 * keep the rest of the payload typed, without having to repeat the
 * base fields in every interface.
 *
 * ``operation`` -- the backend's canonical operation name for
 * this endpoint. It is NOT necessarily the URL path (e.g.
 * ``GET /settings`` returns ``operation: "settings_get"``).
 * Used in logging and diagnostics; never switch user-visible
 * behavior on it.
 */
export interface MemoryOkEnvelope {
  status: 'ok';
  operation: string;
}

// ---------------------------------------------------------------------------
// Settings responses
// ---------------------------------------------------------------------------

/**
 * Response of ``GET /api/memory/settings``.
 *
 * Always available -- even when ``settings.mode === 'disabled'``.
 * The UI MUST render the settings page regardless of mode; see
 * the spec's "no mode gates on settings" rule.
 */
export interface SettingsGetResponse extends MemoryOkEnvelope {
  operation: 'settings_get';
  settings: MemorySettings;
}

/**
 * Response of ``PATCH /api/memory/settings``.
 *
 * ``applied_fields`` lists the top-level field names that were
 * actually changed (sorted alphabetically by the backend). For a
 * nested change (e.g. ``token_budget.max_tokens``) the reported
 * field is ``"token_budget"`` -- the nested name is not broken
 * out. This is intentional: the field list identifies WHICH
 * patch keys were consumed, not which inner values changed.
 */
export interface SettingsUpdateResponse extends MemoryOkEnvelope {
  operation: 'settings_update';
  applied_fields: string[];
  settings: MemorySettings;
}

/**
 * Response of ``GET /api/memory/settings/projects/{project_id}``.
 *
 * Two representations of the same project's configuration:
 *
 *   * ``effective`` -- the merged result of global settings +
 *     this project's override. This is what the memory subsystem
 *     ACTUALLY uses for this project.
 *
 *   * ``override``  -- the raw override object. MAY be the full
 *     settings shape (with defaults filled in) OR only the
 *     explicitly-set delta -- the backend's behavior depends on
 *     how it internally stores the override. ``{}`` when the
 *     project has no override.
 *
 * Do NOT read ``override`` directly to determine "what fields did
 * the user customize?". Use ``computeEffectiveOverride`` from
 * ``settings.ts`` -- it compares ``override`` against ``effective``
 * and returns only the truly-different fields, which is what the
 * UI needs to render the "customize this project" form correctly.
 */
export interface ProjectSettingsGetResponse extends MemoryOkEnvelope {
  operation: 'project_settings_get';
  project_id: string;
  effective: MemorySettings;
  override: MemorySettingsOverlay;
}

/**
 * Response of ``PUT /api/memory/settings/projects/{project_id}``.
 *
 * A PUT REPLACES the project's override wholesale (HTTP PUT
 * semantics: full replacement of the override slot). To merge
 * with an existing override, read it first with a GET, edit, and
 * PUT back -- the backend has no PATCH for project settings.
 *
 * The response carries the freshly-computed ``effective`` (the
 * backend does not echo the override itself, because the caller
 * already knows what it just sent). This is sufficient for
 * optimistic UI: replace the local effective snapshot with this
 * value, done.
 */
export interface ProjectSettingsPutResponse extends MemoryOkEnvelope {
  operation: 'project_settings_put';
  project_id: string;
  applied_fields: string[];
  effective: MemorySettings;
}

/**
 * Response of ``DELETE /api/memory/settings/projects/{project_id}``.
 *
 * Idempotent: deleting an override that does not exist returns
 * 200 with ``effective`` equal to the global settings.
 */
export interface ProjectSettingsDeleteResponse extends MemoryOkEnvelope {
  operation: 'project_settings_delete';
  project_id: string;
  effective: MemorySettings;
}

// ---------------------------------------------------------------------------
// CRUD responses
// ---------------------------------------------------------------------------

/**
 * Response of ``GET /api/memory`` (list entries).
 *
 * The ``scope`` field is the SINGLE scope that was queried. List
 * is deliberately single-scope -- a caller that wants multiple
 * scopes must make multiple calls, or use ``/search`` (which
 * accepts a scope LIST).
 *
 * ``status_filter`` echoes the query parameter: ``"all"`` or
 * ``"active"``. Note: this is the FILTER, not the status of any
 * individual entry -- entries in ``memories`` may have any status
 * when ``status_filter === "all"``.
 *
 * Pagination is offset-based and additive: to fetch the next
 * page, add ``limit`` to the previous ``offset`` and issue the
 * same query. There is no cursor token to expire -- but there is
 * also no stability guarantee if entries are created or deleted
 * between requests (a new entry may shift the window). For the
 * memory UI's typical usage (list all, paginate forward), this is
 * fine; for "process every entry exactly once", use ``/export``
 * instead.
 */
export interface ListMemoryResponse extends MemoryOkEnvelope {
  operation: 'list';
  scope: MemoryScopeRefString;
  limit: number;
  offset: number;
  status_filter: 'active' | 'all';
  count: number;
  memories: MemoryEntry[];
}

/**
 * Response of ``POST /api/memory`` (create entry).
 *
 * Status code 201. ``memory`` is the fully-populated created
 * entry -- with server-assigned ``memory_id``, timestamps,
 * ``content_hash``, and computed ``scope``.
 *
 * NOTE on the write pipeline: the backend's write pipeline may
 * return ``created``, ``skipped``, ``superseded``, or
 * ``reactivated`` for a create request -- but THIS endpoint
 * (``POST /api/memory``) always takes the ``create()`` provider
 * path directly (bypassing the pipeline). It always returns a
 * newly-inserted entry, never a skip. The pipeline's
 * dedupe/supersede logic lives in the AGENT path
 * (``MemoryService.write``) and the import path, not here. So
 * the response is always a fresh entry.
 */
export interface CreateMemoryResponse extends MemoryOkEnvelope {
  operation: 'create';
  memory: MemoryEntry;
}

/**
 * Response of ``POST /api/memory/search``.
 *
 * A read-only "preview" of what retrieval would inject into a
 * prompt, given the query and scope list. Called by the UI's
 * "similar memories" panel beneath the composer, and by the
 * agent-tool path before saving a candidate.
 *
 * Field-by-field:
 *
 *   * ``query``             -- echoed back verbatim.
 *
 *   * ``scope_refs``        -- the RESOLVED scope list (after
 *                              contextual expansion of chat_id /
 *                              project_id into concrete scope
 *                              refs). Use this to display which
 *                              scopes were consulted.
 *
 *   * ``router_decision``   -- the retrieval router's
 *                              classification for this query.
 *
 *   * ``ranker_version``    -- opaque version tag of the ranking
 *                              algorithm. Changes when ranking
 *                              weights change.
 *
 *   * ``count``             -- entries returned (<= limit).
 *
 *   * ``used_tokens``       -- total tokens consumed by the
 *                              formatted memory block.
 *
 *   * ``token_budget``      -- the max_tokens ceiling applied for
 *                              this query (usually
 *                              ``settings.token_budget.max_tokens``,
 *                              but search overrides it with the
 *                              request's ``limit`` scaled
 *                              implicitly). The UI's "budget
 *                              utilization" bar uses
 *                              ``used_tokens / token_budget``.
 *
 *   * ``dropped_for_budget``-- count of entries that WERE relevant
 *                              but did not fit the budget. When
 *                              > 0, surface a "N memories omitted
 *                              for size" notice in the preview.
 *
 *   * ``results``           -- the surviving entries (post-rank,
 *                              post-budget-fit), in rank order.
 *
 *   * ``formatted``         -- the exact string that would be
 *                              injected into the system prompt
 *                              under the ``[RETRIEVED MEMORY]``
 *                              header. Useful for a "view as the
 *                              model sees it" debug panel; not
 *                              for inline display in normal UX.
 */
export interface SearchMemoryResponse extends MemoryOkEnvelope {
  operation: 'search';
  query: string;
  scope_refs: MemoryScopeRefString[];
  router_decision: RouterDecision;
  ranker_version: string;
  count: number;
  used_tokens: number;
  token_budget: number;
  dropped_for_budget: number;
  results: MemoryEntry[];
  formatted: string;
}

/**
 * Response of ``GET /api/memory/{memory_id}``.
 *
 * ``memory`` is absent iff the entry was not found in the given
 * scope (in which case the response is a 404, not this type --
 * see ``errors.ts``). So ``memory`` is always present on a 200.
 */
export interface GetMemoryResponse extends MemoryOkEnvelope {
  operation: 'get';
  memory: MemoryEntry;
}

/**
 * Response of ``PATCH /api/memory/{memory_id}``.
 *
 * ``applied_fields`` lists the top-level field names present in
 * the patch body (sorted). Because the endpoint rejects empty
 * patches with ``400 empty_update``, ``applied_fields`` is never
 * an empty array on success.
 *
 * Note: unlike the CREATE response, this endpoint goes through
 * ``provider.update`` directly (not the write pipeline). It
 * cannot return ``skipped``/``superseded``/``reactivated`` --
 * those outcomes exist only on the pipeline path. A successful
 * PATCH always means "the entry was updated".
 */
export interface UpdateMemoryResponse extends MemoryOkEnvelope {
  operation: 'update';
  applied_fields: string[];
  memory: MemoryEntry;
}

/**
 * Response of ``DELETE /api/memory/{memory_id}``.
 *
 * ``hard`` echoes the query parameter. When ``hard: false``
 * (default), the entry transitions to ``status: 'archived'`` and
 * remains queryable with ``?status=all``. When ``hard: true``,
 * the entry is PERMANENTLY removed (its FTS index entries are
 * cleaned by a trigger -- no manual cleanup needed).
 *
 * The response does NOT echo the deleted entry -- callers who
 * need to restore a soft-deleted entry must fetch it first (or
 * look at the archived entry). This is a deliberate design: the
 * response is minimal, the caller already has the entry from
 * whichever path led them to delete it.
 */
export interface DeleteMemoryResponse extends MemoryOkEnvelope {
  operation: 'delete';
  memory_id: string;
  scope: MemoryScopeRefString;
  hard: boolean;
}

/**
 * Response of ``POST /api/memory/{memory_id}/promote``.
 *
 * Promote COPIES an entry into the user scope (it does NOT move
 * it -- the source entry remains untouched). The new entry's
 * ``provenance`` records the source:
 * ``source_type: 'explicit_user'``,
 * ``imported_from: 'promoted_from:<source_scope>'``.
 *
 * The UI MUST make this "copy, not move" semantic explicit in
 * the confirmation dialog -- users often expect move semantics
 * when promoting something to a "higher" scope, and silently
 * duplicating their entry causes confusion when they later try to
 * edit "the" entry and find themselves editing one of two copies.
 */
export interface PromoteMemoryResponse extends MemoryOkEnvelope {
  operation: 'promote';
  source_memory_id: string;
  source_scope: MemoryScopeRefString;
  target_scope: MemoryScopeRefString;
  promoted: MemoryEntry;
}

/**
 * Response of ``POST /api/memory/{memory_id}/pin``.
 *
 * Pin is a toggle endpoint -- the body's ``pinned`` field sets the
 * target state (not a toggle-of-current). Sending
 * ``{pinned: true}`` on an already-pinned entry is a no-op that
 * still returns 200 with the current state.
 *
 * ``importance`` in the response reflects the state AFTER the
 * call. If the request included an explicit importance, the
 * response shows the new value; otherwise it shows the unchanged
 * value. ``applied_fields`` distinguishes the two cases --
 * ``["is_pinned"]`` vs ``["is_pinned", "importance"]``.
 *
 * The full ``memory`` object is included so the UI can replace
 * its cached copy atomically. This is the ONLY mutation that
 * supports optimistic UI in the memory feature (it is reversible
 * and low-risk) -- see the spec's "no optimistic updates on
 * dangerous operations" rule.
 */
export interface PinMemoryResponse extends MemoryOkEnvelope {
  operation: 'pin';
  memory_id: string;
  scope: MemoryScopeRefString;
  pinned: boolean;
  importance: number;
  applied_fields: string[];
  memory: MemoryEntry;
}

// ---------------------------------------------------------------------------
// Projects responses
// ---------------------------------------------------------------------------

/**
 * One project's summary in the ``GET /api/memory/projects`` list.
 *
 * ``last_updated`` is a Unix timestamp (seconds, float). The
 * backend may return ``int`` or ``float`` depending on the
 * provider's storage; TS receives it as ``number`` either way.
 */
export interface MemoryProjectSummary {
  project_id: string;
  total: number;
  active: number;
  archived: number;
  superseded: number;
  last_updated: number;
}

/**
 * Response of ``GET /api/memory/projects``.
 *
 * Only projects with at least one entry appear in the list --
 * projects whose entries were all purged (or that never had any)
 * are ABSENT. The UI should show "no memories yet" for these,
 * not a zero-count row.
 */
export interface ListProjectsResponse extends MemoryOkEnvelope {
  operation: 'list_projects';
  count: number;
  projects: MemoryProjectSummary[];
}

/**
 * Response of ``GET /api/memory/projects/{project_id}/stats``.
 *
 * Feeds the "project detail" page's statistics panel. The nested
 * shapes are all optional-content: a project with zero entries
 * (which cannot normally be reached via the projects list, but
 * can be reached via a direct URL) returns empty arrays for
 * ``types`` and ``top_tags``, and ``importance``/``created_range``
 * with sentinel values (the backend does not document what those
 * sentinels are -- treat the whole stats object as possibly-empty
 * and render defensively).
 */
export interface ProjectStats {
  project_id: string;
  total: number;
  active: number;
  archived: number;
  superseded: number;
  importance: {
    min: number;
    max: number;
    avg: number;
  };
  created_range: {
    oldest: number;
    newest: number;
  };
  last_updated: number;
  types: Array<{
    memory_type: string;
    count: number;
  }>;
  top_tags: Array<{
    tag: string;
    count: number;
  }>;
}

/**
 * Response of ``GET /api/memory/projects/{project_id}/stats``
 * (continued -- the wrapping envelope around ``ProjectStats``).
 */
export interface ProjectStatsResponse extends MemoryOkEnvelope {
  operation: 'project_stats';
  stats: ProjectStats;
}

/**
 * Response of ``POST /api/memory/projects/{project_id}/bulk-archive``.
 *
 * Idempotent. ``archived_count`` is how many rows transitioned
 * from ``active`` to ``archived`` on THIS call -- 0 if none were
 * active, even if the project has archived entries.
 */
export interface BulkArchiveResponse extends MemoryOkEnvelope {
  operation: 'bulk_archive';
  project_id: string;
  archived_count: number;
}

/**
 * Response of ``POST /api/memory/projects/{project_id}/bulk-purge``.
 *
 * PERMANENT deletion. ``purged_count`` is how many rows were
 * actually removed -- 0 if the project was already empty.
 *
 * The ``warning`` string is a user-facing sentence the backend
 * provides so the UI does not need to duplicate wording. Render
 * it verbatim as the confirmation or toast body.
 */
export interface BulkPurgeResponse extends MemoryOkEnvelope {
  operation: 'bulk_purge';
  project_id: string;
  purged_count: number;
  warning: string;
}

// ---------------------------------------------------------------------------
// Import responses
// ---------------------------------------------------------------------------

/**
 * The candidate set returned by ``POST /api/memory/import/preview``.
 *
 * The preview is READ-ONLY: no entries are written until the
 * caller POSTs the same source to ``/import/apply``. The backend
 * does NOT enforce this ordering -- a caller CAN skip preview
 * and call apply directly. The UI's two-step wizard enforces it
 * for the USER, but the API is open.
 *
 * ``candidates`` are the parsed entries as they WOULD be created
 * (before dedupe). ``to_create`` and ``to_skip`` are the counts
 * AFTER the dedupe pass runs. So ``candidates.length`` may be
 * greater than ``to_create + to_skip`` -- entries rejected by
 * validation are neither created nor skipped, but appear in
 * ``candidates`` for inspection.
 *
 * ``warnings`` is a list of human-readable strings for
 * validation rejections or security blocks. The UI MUST surface
 * these -- they often contain the ONLY explanation for why an
 * entry that "looks fine" in the file was rejected.
 */
export interface ImportPreview {
  candidates: MemoryEntry[];
  to_create: number;
  to_skip: number;
  warnings: string[];
}

/**
 * Response of ``POST /api/memory/import/preview``.
 *
 * ``scope`` echoes the resolved ``<type>:<id>`` target for the
 * would-be import. ``elapsed_ms`` is the parse-and-classify time
 * in milliseconds (float, 2 decimals) -- useful for
 * benchmarking large files.
 */
export interface ImportPreviewResponse extends MemoryOkEnvelope {
  operation: 'preview';
  scope: MemoryScopeRefString;
  elapsed_ms: number;
  preview: ImportPreview;
}

/**
 * The result set returned by ``POST /api/memory/import/apply``.
 *
 * Best-effort semantics: a failure on ONE candidate does not
 * abort the batch. ``created`` + ``skipped`` is the count of
 * candidates that reached a terminal state; ``errors`` lists the
 * ones that failed -- each error string is self-contained
 * (candidate index + content preview + reason).
 *
 * The UI should NOT treat any ``errors.length > 0`` as a total
 * failure -- show the created/skipped counts first, then a
 * collapsible "N errors" panel.
 */
export interface ImportApplyResult {
  created: number;
  skipped: number;
  errors: string[];
}

/**
 * Response of ``POST /api/memory/import/apply``.
 *
 * Same envelope as preview -- same ``scope`` and ``elapsed_ms``
 * fields, different inner payload.
 */
export interface ImportApplyResponse extends MemoryOkEnvelope {
  operation: 'apply';
  scope: MemoryScopeRefString;
  elapsed_ms: number;
  result: ImportApplyResult;
}

/**
 * One adapter entry in ``GET /api/memory/import/adapters``.
 *
 * Adapters are dynamically registered on the backend, so
 * ``adapter_id`` is typed as ``string`` not as the
 * ``KnownImportAdapterId`` union. The UI must treat unknown ids
 * as opaque.
 */
export interface ImportAdapter {
  adapter_id: string;
  description: string;
  is_builtin: boolean;
}

/**
 * Response of ``GET /api/memory/import/adapters``.
 *
 * NOTE: this is the ONLY import endpoint whose response does NOT
 * include an ``operation`` field. The backend's
 * ``list_adapters_endpoint`` returns ``{status: "ok", adapters}``
 * with no ``operation`` -- unlike every other endpoint in the
 * entire memory API. This is a backend inconsistency, not a
 * frontend design choice; the type reflects the actual wire
 * shape, and consumers must not assume the base envelope.
 */
export interface ImportAdaptersResponse {
  status: 'ok';
  adapters: ImportAdapter[];
}

// ---------------------------------------------------------------------------
// Export responses
// ---------------------------------------------------------------------------

/**
 * Opaque package manifest returned by ``GET /api/memory/export``
 * in ``format=json`` mode.
 *
 * The exact structure is defined by ``package_writer.build_package_payload``
 * on the backend, which is not available in this module's source
 * set. Until that writer's schema is stabilized and documented,
 * this type is deliberately opaque: consumers should treat
 * individual fields as ``unknown`` and narrow on access. Common
 * fields (``package_version``, ``format``, ``exported_at``,
 * ``entry_count``) are documented here as "likely present" but
 * not required -- do not assume their presence without a runtime
 * check.
 *
 * If the UI's JSON-preview panel needs richer typing, add a
 * narrow helper that validates the manifest against a Zod
 * schema; do not widen this type with fields that are not
 * actually guaranteed by the backend.
 */
export type MemoryPackageManifest = Record<string, unknown>;

/**
 * Response of ``GET /api/memory/export?format=json``.
 *
 * NOTE: this is the ONLY export endpoint with a NON-envelope
 * body. When ``format=json``, the backend returns the raw
 * package payload ``{manifest, memories}`` -- no ``status``, no
 * ``operation``, no ``count``. This is by design: the SAME
 * payload (modulo serialization) is what a user re-uploads via
 * ``POST /api/memory/import/apply``, so the JSON body MUST be
 * the package itself, not a wrapper.
 *
 * The zip response (``format=zip``) is ``application/zip``
 * binary, not a JSON body at all -- see
 * ``ExportZipResponseMetadata`` for the headers you can read
 * from that response.
 *
 * ``memories`` elements are entries serialized through
 * ``build_package_payload``, which may NOT add the computed
 * ``scope`` field (unlike ``_entry_to_dict``). The ``scope?``
 * optional on ``MemoryEntry`` handles this.
 */
export interface ExportJsonResponse {
  manifest: MemoryPackageManifest;
  memories: MemoryEntry[];
}

/**
 * Response metadata (headers) attached to the export endpoint,
 * regardless of format.
 *
 * The export endpoint sets these headers on BOTH ``format=zip``
 * and ``format=json`` responses. They carry information the body
 * cannot express (especially for the zip case, which has no JSON
 * body at all):
 *
 *   * ``X-Export-Scope``         -- the resolved scope ref.
 *   * ``X-Export-Entries``       -- number of entries in the
 *                                   response (after truncation).
 *   * ``X-Export-Truncated``     -- ``"true"`` or ``"false"``
 *                                   (always strings on the wire).
 *   * ``X-Export-Total-Matched`` -- number of entries in the
 *                                   scope BEFORE truncation.
 *
 * A caller that sees ``X-Export-Truncated: true`` must fetch the
 * remainder (e.g. by filtering by status, or by re-running with a
 * higher limit -- but the limit is hard-capped at 10,000 so for
 * very large scopes, use the ``status`` filter to split the
 * workload).
 */
export interface ExportZipResponseMetadata {
  'X-Export-Scope': string;
  'X-Export-Entries': string;
  'X-Export-Truncated': 'true' | 'false';
  'X-Export-Total-Matched': string;
}

// ---------------------------------------------------------------------------
// Usage responses
// ---------------------------------------------------------------------------

/**
 * Response of ``GET /api/memory/usage`` (summary).
 *
 * All fields are FLAT on the envelope -- the backend spreads the
 * summary dict directly into the response body:
 * ``{"status": "ok", "operation": "summary", **summary}``.
 *
 * Token fields:
 *
 *   * ``total_tokens``     -- sum of every token count below.
 *   * ``retrieval_tokens`` -- tokens spent on the retrieval step
 *                             (currently 0 -- reserved for a
 *                             future retrieval model).
 *   * ``context_tokens``   -- tokens in the memory section
 *                             injected into the prompt.
 *   * ``overhead_tokens``  -- prompt-formatting overhead (the
 *                             ``[RETRIEVED MEMORY]`` header,
 *                             numbering, separators).
 *
 * Scope token fields:
 *
 *   * ``user_tokens`` + ``project_tokens`` + ``chat_tokens`` +
 *     ``agent_tokens`` == ``context_tokens``.
 *
 * Budget fields:
 *
 *   * ``budget_tokens``      -- sum of ``max_tokens`` across all
 *                               runs in the period.
 *   * ``budget_utilization`` -- ``context_tokens / budget_tokens``.
 *
 * Entry-count fields:
 *
 *   * ``entries_retrieved`` -- total entries considered by the
 *                              ranker.
 *   * ``entries_used``      -- entries that survived the budget
 *                              fitter and were injected.
 *   * ``entries_dropped``   -- entries dropped for budget
 *                              reasons.
 *   * ``drop_rate``         -- ``entries_dropped /
 *                               entries_retrieved``. A sustained
 *                               rate above ~0.3 suggests the
 *                               budget is too small for the
 *                               workload.
 */
export interface UsageSummaryResponse extends MemoryOkEnvelope {
  operation: 'summary';
  period: string;
  run_count: number;
  total_tokens: number;
  retrieval_tokens: number;
  context_tokens: number;
  overhead_tokens: number;
  user_tokens: number;
  project_tokens: number;
  chat_tokens: number;
  agent_tokens: number;
  budget_tokens: number;
  budget_utilization: number;
  entries_retrieved: number;
  entries_used: number;
  entries_dropped: number;
  drop_rate: number;
}

/**
 * One bucket in the usage breakdown response.
 *
 * ``dimension`` echoes the query's ``by`` parameter (normalized
 * -- ``source`` becomes ``scope``). ``key`` is the bucket label
 * within that dimension (e.g. ``"user"``, ``"project:acme"``).
 * ``pct`` is ``tokens / total_tokens * 100`` rounded to one
 * decimal.
 */
export interface UsageBreakdownBucket {
  dimension: string;
  key: string;
  tokens: number;
  runs: number;
  pct: number;
}

/**
 * Response of ``GET /api/memory/usage/breakdown``.
 *
 * ``by`` echoes the NORMALIZED dimension (``source`` -> ``scope``),
 * not the raw query parameter. Consumers switching on this field
 * should use the normalized value.
 */
export interface UsageBreakdownResponse extends MemoryOkEnvelope {
  operation: 'breakdown';
  by: string;
  period: string;
  buckets: UsageBreakdownBucket[];
}

/**
 * One data point in the usage timeseries response.
 *
 * ``bucket`` is a date string whose granularity matches the
 * query's ``granularity``:
 *
 *   * ``day``   -> ``"YYYY-MM-DD"``
 *   * ``week``  -> ``"YYYY-Www"`` (ISO week, ``"2026-W45"``)
 *   * ``month`` -> ``"YYYY-MM"``
 */
export interface UsageTimeseriesPoint {
  bucket: string;
  runs: number;
  total_tokens: number;
  retrieval_tokens: number;
  context_tokens: number;
  overhead_tokens: number;
  entries_used: number;
}

/**
 * Response of ``GET /api/memory/usage/timeseries``.
 *
 * Period and granularity are not cross-validated: a query with
 * ``period=day&granularity=month`` returns one bucket (that is a
 * legitimate query, just an unusual one).
 */
export interface UsageTimeseriesResponse extends MemoryOkEnvelope {
  operation: 'timeseries';
  period: string;
  granularity: string;
  points: UsageTimeseriesPoint[];
}

// ---------------------------------------------------------------------------
// Type-guard helpers
// ---------------------------------------------------------------------------

/**
 * Narrow any memory-API response to the "ok" branch by checking
 * its ``status`` field.
 *
 * Use this in ``memoryClient`` call sites where the response type
 * is a union with an error envelope (from ``errors.ts``). Example:
 *
 *     const resp = await memoryClient.getSettings();
 *     if (isOkEnvelope(resp)) {
 *       // resp.settings is now typed
 *     }
 *
 * Note: this helper does NOT narrow the ``operation`` field.
 * Individual response types declare a literal ``operation`` --
 * TypeScript's discriminated-union narrowing on the
 * ``operation`` field works automatically once the union is
 * constructed at the call site (e.g.
 * ``SettingsGetResponse | SettingsUpdateResponse``). This helper
 * is the coarser "is this an error or not" gate.
 */
export function isOkEnvelope(
  response: { status?: unknown } | null | undefined,
): response is MemoryOkEnvelope {
  return (
    typeof response === 'object' &&
    response !== null &&
    response.status === 'ok'
  );
}
