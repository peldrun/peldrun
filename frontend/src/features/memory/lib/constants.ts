// src/features/memory/lib/constants.ts

/**
 * Client-side numeric policy for the PELDRUN Memory feature.
 *
 * Boundaries with other modules
 * -----------------------------
 *
 *   * `types/import-export.ts` -- source of truth for the
 *     IMPORT/EXPORT wire-contract constants (upload size cap,
 *     accepted extensions, export entry cap). This module
 *     RE-EXPORTS them so consumers can import all numeric
 *     policy from one place.
 *
 *   * `types/settings.ts` -- source of truth for the settings
 *     bounds (`MemorySettingsBounds`). Also re-exported here.
 *
 *   * THIS module -- NEW constants that are neither wire-shape
 *     nor domain-type: client-side soft limits, UI pacing,
 *     confirmation tokens, pagination defaults, and the
 *     re-exports mentioned above.
 *
 * Why a re-export layer at all
 * ----------------------------
 *
 * A component that needs `MAX_TAGS`, `IMPORT_MAX_UPLOAD_BYTES`,
 * and `MemorySettingsBounds` (e.g. the combined import form
 * plus settings validator) would otherwise need three separate
 * imports from `types/*`. Since those types are already public,
 * the extra hop through `lib/constants` costs nothing and keeps
 * `lib/*` free of any internal knowledge about which types file
 * happens to define a given bound today.
 *
 * If the source-of-truth module ever moves a constant, only
 * this file needs to update its re-export -- the components
 * keep importing from `lib/constants`.
 */

// ---------------------------------------------------------------------------
// Re-exports -- single import point for numeric policy
// ---------------------------------------------------------------------------

export {
  IMPORT_MAX_UPLOAD_BYTES,
  IMPORT_ACCEPTED_EXTENSIONS,
  IMPORT_ACCEPT_ATTRIBUTE,
  EXPORT_MAX_ENTRIES,
  EXPORT_DEFAULT_ENTRIES,
} from '../types/import-export';

export { MemorySettingsBounds } from '../types/settings';

// ---------------------------------------------------------------------------
// Content and tags (client-side soft limits)
// ---------------------------------------------------------------------------

/**
 * Soft upper bound on `content` length, in characters.
 *
 * The backend's Pydantic `_CreateBody.content` field declares
 * `min_length=1` but NO upper bound at the router layer -- any
 * real upper limit is enforced inside `MemoryEntry.create()`
 * (the domain model), whose exact value is not exposed to the
 * frontend. This constant is a CONSERVATIVE client-side cap so
 * the editor can show an inline error rather than letting the
 * user paste a 500 KB document and wait for a backend
 * `MemoryValidationError`.
 *
 * If the real backend bound is discovered to be higher, raise
 * this value to match. If it is lower, still raise this value
 * (the backend will reject; the soft cap is meant to catch
 * obvious mistakes, not to be the last line of defense).
 *
 * The number itself (10 000) is chosen empirically: it is well
 * above any realistic memory entry, well below a typical
 * browser's clipboard size, and matches the upper bound many
 * chat-based memory systems use for paste-in content.
 */
export const MAX_CONTENT_LENGTH = 10_000;

/**
 * Hard cap on the number of tags per entry.
 *
 * Mirrors the backend's documented limit (`MemoryValidationError`
 * when exceeded). The client caps at the same number so the tag
 * editor refuses the over-limit case inline rather than letting
 * the whole save fail.
 */
export const MAX_TAGS = 32;

/**
 * Hard cap on a single tag's length, in characters.
 *
 * Same source as `MAX_TAGS` -- the backend's tag validator
 * enforces it. The client mirrors the number so the tag input
 * can refuse an over-long tag with an inline message.
 */
export const MAX_TAG_LENGTH = 64;

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

/**
 * Default number of entries per list request.
 *
 * Matches the backend's `limit` default on the list endpoint.
 * The pagination UI ("Load more") increments `offset` by this
 * number. Larger values are accepted by the backend (up to
 * `MAX_PAGE_SIZE`) but the browse page's rendering performance
 * degrades past ~150 entries, so this stays at 50.
 */
export const DEFAULT_PAGE_SIZE = 50;

/**
 * Hard cap on `limit` for the list endpoint.
 *
 * Mirrors `Query(50, ge=1, le=200)` on `memory_crud.list_endpoint`.
 * A request above this is rejected by FastAPI's query
 * validation with `422` (normalized to `400 request_validation`
 * by the app-level handler). The frontend's pagination never
 * approaches it, but the constant is exported for callers that
 * build their own requests.
 */
export const MAX_PAGE_SIZE = 200;

// ---------------------------------------------------------------------------
// Search pacing
// ---------------------------------------------------------------------------

/**
 * Debounce interval for the "similar memories" panel's search,
 * in milliseconds.
 *
 * The panel fires `POST /api/memory/search` on content changes
 * (trimmed to the last sentence). 300ms is the standard middle
 * ground: fast enough to feel live, slow enough that a fast
 * typist does not generate one request per keystroke.
 *
 * Not used by the search page's submit button -- that fires on
 * explicit user action, no debounce needed.
 */
export const SEARCH_DEBOUNCE_MS = 300;

/**
 * Minimum query length before the similar-memories panel fires
 * a search. Below this length, the query is too generic to
 * produce meaningful results and would waste a request per
 * keystroke.
 *
 * Not enforced by the backend -- `POST /search` accepts empty
 * queries (returns the top entries by rank). The threshold is
 * a client-side UX optimization only.
 */
export const SEARCH_MIN_QUERY_LENGTH = 3;

// ---------------------------------------------------------------------------
// Entry defaults
// ---------------------------------------------------------------------------

/**
 * The `importance` default for a new entry.
 *
 * Matches the backend's Pydantic default for `_CreateBody`.
 * Chosen so that an entry created without an explicit
 * importance contributes the NEUTRAL 0.10 to the ranker's
 * final score (0.20 * 50/100). Users who care about ranking
 * raise or lower it explicitly.
 */
export const DEFAULT_IMPORTANCE = 50;

/**
 * The `is_pinned` default for a new entry.
 *
 * Off by default. Pinning is an explicit user decision; a
 * default-on would silently boost every entry, defeating the
 * purpose of the pin signal entirely.
 */
export const DEFAULT_IS_PINNED = false;

// ---------------------------------------------------------------------------
// Confirmation tokens
// ---------------------------------------------------------------------------

/**
 * The literal string a user must type to confirm a HARD delete.
 *
 * Deliberately short, uppercase, and NOT localized: the goal is
 * to force a conscious action, not to be a friendly phrase.
 * Typing "DELETE" (or a project's name for bulk-purge) takes
 * just enough effort that an accidental click cannot trigger
 * an irreversible operation.
 *
 * Used by `ConfirmDangerDialog` for the hard-delete path. For
 * bulk-purge, the required token is the PROJECT NAME (passed
 * to the dialog as `confirmToken`), not this constant -- see
 * `BulkActionsBar`.
 */
export const HARD_DELETE_CONFIRM_TOKEN = 'DELETE';

// ---------------------------------------------------------------------------
// UI pacing
// ---------------------------------------------------------------------------

/**
 * Default toast duration, in milliseconds.
 *
 * Per the spec's §6.2 rule: toasts should stay long enough to
 * be read (5 seconds) and should offer "Undo" where possible.
 * The toast component itself accepts a per-toast duration
 * override; this is the default.
 */
export const TOAST_DURATION_MS = 5_000;

/**
 * Duration of the "Saved" pulse shown after a successful save,
 * in milliseconds.
 *
 * Short enough not to distract, long enough to be noticeable.
 * Used by `SettingsForm` and `EntryEditor` for the inline
 * "Saved ✓" indicator that replaces the Save button for a
 * moment.
 */
export const SAVE_SUCCESS_PULSE_MS = 1_200;

/**
 * Delay before showing a skeleton loader on a query, in
 * milliseconds.
 *
 * Prevents "flash of loading state" for fast queries (cache
 * hits, small responses). If the query resolves before this
 * delay elapses, the skeleton never appears -- the component
 * renders the empty state instead, and the content replaces it
 * seamlessly when it arrives.
 *
 * Standard React Query UX guidance.
 */
export const SKELETON_DELAY_MS = 200;

/**
 * Interval for `refetchInterval` on the usage dashboard,
 * in milliseconds.
 *
 * The usage data updates on a slow cadence (per run, not per
 * second), so 60s is frequent enough to feel live without
 * causing a query storm. Only the usage page sets
 * `refetchInterval` -- every other query relies on invalidation
 * after mutations.
 */
export const USAGE_REFETCH_INTERVAL_MS = 60_000;
