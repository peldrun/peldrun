// src/features/memory/types/import-export.ts

/**
 * Client-side helper types for the import and export flows.
 *
 * Boundary with `responses.ts`:
 *
 *   * `responses.ts` -- the RESPONSE envelopes the backend
 *     RETURNS (preview result, apply result, adapters list,
 *     package manifest, export JSON body, usage summary, etc.).
 *
 *   * THIS module -- the REQUEST shapes the frontend BUILDS,
 *     plus client-only presentation helpers (file info, wizard
 *     step, download result). Nothing here corresponds to a
 *     backend "response" type; the two modules are complementary
 *     halves of the same feature surface.
 *
 * Contract source
 * ---------------
 *
 * The request shapes below mirror `memory_import.py` and
 * `memory_export.py` verbatim:
 *
 *   * Import accepts TWO content types on both `/preview` and
 *     `/apply`: `multipart/form-data` (file upload) and
 *     `application/json` (inline source). The two request
 *     builders in this module encode that split explicitly so a
 *     call site cannot accidentally mix them.
 *
 *   * Export accepts only query parameters (GET), no body. The
 *     `ExportQueryParams` type gathers them into one object so
 *     the API client can serialize consistently and the UI's
 *     export form can validate in one place.
 *
 * Design rules
 * ------------
 *
 *   * **No fabrication of backend shapes.** Where the backend's
 *     contract is "opaque / adapter decides" (e.g. the exact
 *     contents of the `source` field in a JSON import), the
 *     type here is `unknown` and the JSDoc explains what the
 *     built-in adapters accept. Widening to `any` would let
 *     call sites bypass the adapter contract; narrowing to a
 *     union the backend does not enforce would lie.
 *
 *   * **Constants live here, not in `lib/constants.ts`.** The
 *     100 MB upload cap and the 10 000-entry export cap are
 *     part of the BACKEND's contract, not UI preferences. Any
 *     call site that needs them imports from this module.
 *     `lib/constants.ts` (a later file) re-exports them for
 *     convenience but is never the source of truth.
 */

import type { MemoryEntry, MemoryScopeRefString } from './entry';
import type {
  ExportFormat,
  MemoryScope,
  MemoryStatusFilter,
} from './enums';
import type { MemoryPackageManifest } from './responses';

// ---------------------------------------------------------------------------
// Import -- constants
// ---------------------------------------------------------------------------

/**
 * Hard upload size limit enforced by the import router.
 *
 * Mirrors `_MAX_UPLOAD_BYTES` in `memory_import.py`. The backend
 * rejects anything larger with `413 file_too_large` before
 * buffering it. The frontend enforces the SAME limit client-side
 * (see `validateImportFile`) so the user gets an immediate error
 * instead of waiting for a multipart upload to travel over the
 * wire and come back rejected.
 *
 * This must be kept in sync with the backend constant. If the
 * backend changes it, the client-side check becomes
 * unnecessarily strict (or permissive). The two are documented
 * together; changing one is a review-checklist item.
 */
export const IMPORT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

/**
 * File extensions the import endpoint accepts.
 *
 * Mirrors the dispatch in `memory_import._decode_upload_to_source`:
 *
 *   * `.zip` / `.db` / `.sqlite` / `.sqlite3` -- raw bytes, the
 *     adapter registry sniffs the magic prefix to confirm.
 *   * `.md` / `.markdown` -- written to a temp path on the
 *     backend; the MarkdownAdapter reads from disk.
 *   * `.json` -- parsed to a dict on the backend; the
 *     JsonAdapter takes the dict directly.
 *
 * Any other extension reaches the adapter registry and is
 * rejected with `unsupported_content_type`. The frontend uses
 * this list to build the `<input accept>` attribute AND to
 * validate before upload so the user is told "wrong file type"
 * in the browser rather than after a round-trip.
 */
export const IMPORT_ACCEPTED_EXTENSIONS = [
  '.zip',
  '.json',
  '.md',
  '.markdown',
  '.db',
  '.sqlite',
  '.sqlite3',
] as const;

export type ImportFileExtension =
  (typeof IMPORT_ACCEPTED_EXTENSIONS)[number];

/**
 * The `accept` attribute value for the file input.
 *
 * Assembled from `IMPORT_ACCEPTED_EXTENSIONS` so adding a new
 * accepted extension updates both the validation list and the
 * browser's file picker filter in one place.
 */
export const IMPORT_ACCEPT_ATTRIBUTE: string =
  IMPORT_ACCEPTED_EXTENSIONS.join(',');

// ---------------------------------------------------------------------------
// Import -- request shapes
// ---------------------------------------------------------------------------

/**
 * The target scope shared by both import request shapes.
 *
 * Both `multipart/form-data` and `application/json` uploads
 * carry a `scope_type` and `scope_id`. The backend's
 * `_build_scope_ref` helper rejects any pair that is missing
 * either value or names an unknown scope type, so both fields
 * are required here.
 */
export interface ImportTargetScope {
  scope_type: MemoryScope;
  scope_id: string;
}

/**
 * The `multipart/form-data` import request body.
 *
 * Built from a `File` (the browser's native file object) plus
 * the target scope and an optional explicit adapter override.
 *
 * `adapter_id` semantics:
 *
 *   * `null` / `undefined` -- let the backend auto-detect based
 *     on the file's magic prefix. This is the default the
 *     wizard uses; the user only forces an adapter when they
 *     know auto-detection guessed wrong.
 *
 *   * A non-empty string -- force that specific adapter. Must
 *     be a registered adapter id (see the adapters list from
 *     `GET /import/adapters`).
 *
 * Both `/preview` and `/apply` accept the same request shape,
 * so a wizard that shows the user a preview can re-submit the
 * exact same object for the apply step. This is why the
 * "confirmed at" state does not need to serialize anything to
 * localStorage -- the `File` object is retained in component
 * state between steps.
 *
 * NOTE: `File` is a browser-only type. This interface must not
 * be imported from a Server Component or Route Handler; use it
 * only in Client Components and in the import wizard's
 * `'use client'` subtree.
 */
export interface ImportMultipartRequest {
  file: File;
  target: ImportTargetScope;
  adapter_id?: string | null;
}

/**
 * The `application/json` import request body.
 *
 * Used when the source is available INLINE -- as a JS object or
 * as a markdown string -- rather than as a browser file. The
 * typical uses:
 *
 *   * A "paste markdown" textarea in the import wizard (sends
 *     `source: <markdown text>` as a string).
 *
 *   * Re-importing the JSON body that a previous export produced
 *     (sends `source: {manifest, memories}` as a dict).
 *
 *   * A scripted tool that assembles a package in-memory and
 *     POSTs it without writing a file to disk.
 *
 * `source` is `unknown` -- the backend's adapter registry
 * decides what shapes are acceptable per adapter. The built-in
 * adapters document their expected input:
 *
 *   * `JsonAdapter` accepts `{manifest, memories}` (a full
 *     package object) OR a single memory object OR a JSON
 *     string.
 *   * `MarkdownAdapter` accepts a raw markdown string.
 *   * `SqliteAdapter` does NOT accept inline sources --
 *     binary SQLite must be uploaded via multipart.
 *
 * Because these shapes are adapter-specific, the type is
 * `unknown` here. Callers should pass sources they construct
 * from known adapters (e.g. a `File.text()` read for markdown,
 * or a `JSON.parse`'d export body) rather than accept arbitrary
 * user input as `source` -- the backend's security boundary
 * protects `user:` scope writes via `source_type`, but it does
 * NOT type-check the `source` shape beyond what each adapter
 * validates on its own.
 *
 * IMPORTANT: `source` MUST NOT be a `Blob`, `File`, `ArrayBuffer`,
 * or any other binary container -- JSON cannot carry raw bytes.
 * The backend's `_materialize_json_body` passes the source
 * through verbatim, and adapters that expect bytes will fail
 * with a clear error. Binary sources go via
 * `ImportMultipartRequest`.
 */
export interface ImportJsonRequest {
  source: unknown;
  target: ImportTargetScope;
  adapter_id?: string | null;
}

/**
 * Union of the two import request shapes.
 *
 * The discriminant is the presence of `file` (multipart) vs
 * `source` (JSON). Code that handles both can narrow with
 * `'file' in request` or `'source' in request`.
 */
export type ImportRequest =
  | ImportMultipartRequest
  | ImportJsonRequest;

// ---------------------------------------------------------------------------
// Import -- client-side file info
// ---------------------------------------------------------------------------

/**
 * Information about a selected file, as shown in the drop zone
 * before upload.
 *
 * Populated from the browser's `File` object plus a small amount
 * of frontend logic (extension detection). Used by the wizard's
 * upload step to display:
 *
 *   * the file name and size,
 *   * a warning when the size exceeds the cap (before upload),
 *   * the adapter that the backend would likely auto-select
 *     based on the extension (a HINT, not a promise).
 *
 * `detectedAdapter` mirrors the extension -> adapter mapping the
 * backend's registry applies at `select_adapter` time. It is a
 * UI hint only; the authoritative selection happens on the
 * server, and users can override it in the wizard.
 */
export interface ImportFileInfo {
  filename: string;
  sizeBytes: number;
  mimeType: string;
  extension: string;
  /**
   * The adapter id the backend would likely pick for this file's
   * extension, or `null` when the extension is not recognized.
   *
   * This is a DISPLAY hint. The backend re-detects at apply
   * time and may pick a different adapter if the file's magic
   * prefix disagrees with its extension (e.g. a `.json` file
   * that is actually a zip archive). Do not use this value for
   * anything the backend must trust.
   */
  detectedAdapter: string | null;
}

/**
 * The result of the client-side file validation that runs
 * before the wizard lets the user proceed.
 *
 * A discriminated union rather than a boolean + message pair:
 * callers must switch on `ok` to read `info`, so a call site
 * that forgets to handle the error case fails to compile.
 */
export type ImportFileValidation =
  | { ok: true; info: ImportFileInfo }
  | { ok: false; reason: ImportFileRejectionReason; message: string };

/**
 * The reasons `validateImportFile` can reject a file.
 *
 * Kept as a small closed enum (unlike error codes) because the
 * UI's response to each is fixed and specific:
 *
 *   * `too_large`     -- show size cap; do not let user proceed.
 *   * `wrong_type`    -- show accepted extensions; do not let
 *                        user proceed.
 *   * `empty`         -- usually a File-picker artifact; show
 *                        a "pick a file" prompt.
 */
export const ImportFileRejectionReason = {
  TOO_LARGE: 'too_large',
  WRONG_TYPE: 'wrong_type',
  EMPTY: 'empty',
} as const;

export type ImportFileRejectionReason =
  (typeof ImportFileRejectionReason)[keyof typeof ImportFileRejectionReason];

// ---------------------------------------------------------------------------
// Import -- wizard state
// ---------------------------------------------------------------------------

/**
 * The wizard's step identifier.
 *
 * The wizard is intentionally three-step (not two): the result
 * screen is distinct from the preview so the user can see what
 * actually happened (created / skipped / errors) even when the
 * import was partially successful. Collapsing "preview ->
 * result" into one step would hide per-candidate errors.
 *
 * The three steps map to the backend's flow:
 *
 *   1. `select`  -- pick a file and scope. No API call.
 *   2. `preview` -- POST /preview, render candidates and
 *                   warnings, wait for explicit confirmation.
 *   3. `result`  -- POST /apply, render created/skipped/errors.
 */
export type ImportWizardStep = 'select' | 'preview' | 'result';

// ---------------------------------------------------------------------------
// Export -- constants
// ---------------------------------------------------------------------------

/**
 * Hard cap on the number of entries a single export returns.
 *
 * Mirrors `_MAX_EXPORT_ENTRIES` in `memory_export.py`. The
 * export endpoint silently CLAMPS `limit` to this value, so a
 * request with `limit=999999` succeeds but returns at most
 * 10 000 entries and signals truncation via the
 * `X-Export-Truncated` header.
 *
 * The frontend enforces the same cap client-side (see
 * `validateExportLimit`) so the UI can disable the "Download"
 * button with a helpful message before the request even fires.
 */
export const EXPORT_MAX_ENTRIES = 10_000;

/**
 * Default entry limit when the caller does not specify one.
 *
 * Matches the backend's default `limit` parameter on the export
 * endpoint. Users get the full scope by default; only scripts
 * that explicitly want a slice pass a smaller value.
 */
export const EXPORT_DEFAULT_ENTRIES = 10_000;

// ---------------------------------------------------------------------------
// Export -- request shapes
// ---------------------------------------------------------------------------

/**
 * The query parameters accepted by `GET /api/memory/export`.
 *
 * Gathered into a single object so the API client has one
 * canonical serializer and the UI's export form has one
 * validation target.
 */
export interface ExportQueryParams {
  scope_type: MemoryScope;
  scope_id: string;
  format: ExportFormat;
  status: MemoryStatusFilter;
  limit: number;
}

// ---------------------------------------------------------------------------
// Export -- client-side results
// ---------------------------------------------------------------------------

/**
 * The result of a completed export download, as the UI needs to
 * display it.
 *
 * Assembled by the export client from:
 *
 *   * the binary `Blob` response body (for zip) or the JSON
 *     body (for json),
 *   * the `Content-Disposition` header (parsed to `filename`),
 *   * the `X-Export-*` headers (parsed to the metadata fields).
 *
 * The UI's success screen shows `filename`, `sizeBytes`,
 * `entriesCount`, and (when `truncated`) a warning banner
 * explaining that the export was capped at
 * `EXPORT_MAX_ENTRIES` and that a broader scope or a filtered
 * status should be used to fetch the remainder.
 */
export interface ExportDownloadResult {
  /**
   * The filename the browser should use when saving. Extracted
   * from `Content-Disposition` (the `filename*` variant when
   * present, falling back to `filename`).
   */
  filename: string;

  /** Size of the response body, in bytes. */
  sizeBytes: number;

  /** The scope the export was taken from, as `<type>:<id>`. */
  scope: MemoryScopeRefString;

  /** The number of entries actually returned. */
  entriesCount: number;

  /**
   * `true` when the server truncated the response to fit the
   * `limit` (or the hard 10 000 cap). When this is `true`, the
   * UI MUST surface `totalMatched` so the user knows there are
   * more entries available.
   */
  truncated: boolean;

  /**
   * The number of entries that matched the query BEFORE the
   * truncation. Equal to `entriesCount` when `truncated` is
   * `false`.
   */
  totalMatched: number;

  /**
   * The blob to save to disk. For `format=zip` this is the zip
   * archive bytes; for `format=json` this is the JSON body
   * re-serialized as a blob so a single download path handles
   * both.
   */
  blob: Blob;
}

/**
 * The parsed JSON-preview result for the export form's
 * "inspect before download" panel.
 *
 * Assembled from the same response body as `ExportDownloadResult`
 * but decoded as JSON instead of kept as a blob. The preview
 * panel shows `manifest` fields and a table of the first N
 * entries -- never all of them (10 000 rows in a browser table
 * is both slow and useless).
 */
export interface ExportJsonPreview {
  manifest: MemoryPackageManifest;
  memories: MemoryEntry[];
  truncated: boolean;
  totalMatched: number;
}
