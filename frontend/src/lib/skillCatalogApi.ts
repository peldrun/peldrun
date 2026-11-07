// frontend/src/lib/skillCatalogApi.ts
/**
 * API client for the Skill Catalog subsystem.
 *
 * Wraps ``/api/skills/catalog/*`` with:
 *
 *   - Strongly-typed function signatures for every endpoint.
 *   - Consistent error handling via ``SkillCatalogApiError``, which
 *     carries the backend's structured ``{error, code, details}``
 *     payload so callers can render field-level errors without
 *     parsing strings.
 *   - A single base-path constant so a future path change is a
 *     one-line edit here.
 *
 * Functions are intentionally thin -- no retry logic, no caching, no
 * optimistic updates. Those concerns belong to the calling hook
 * (``useSkillActions``), which has access to the store-level state
 * that a retry policy needs.
 *
 * Every function throws ``SkillCatalogApiError`` on failure, EXCEPT
 * ``refreshSkillCatalog`` which returns the response body as-is even
 * when the backend reports ``status: "error"`` (a transient remote
 * fetch failure is not a programming error -- it is a state the UI
 * must surface, not a thrown exception).
 *
 * This module is deliberately independent of ``providerApi.ts``:
 * the two subsystems have no shared error type, no shared request
 * helper, and no shared response shape. Any change to one MUST NOT
 * affect the other. The small amount of duplication is the price of
 * that guarantee.
 *
 * @see ``frontend/src/app/stores/_types/store.ts`` for the full type
 *      surface this module returns.
 */

import type {
  SkillCatalogItem,
  SkillCatalogListResponse,
  SkillCatalogRefreshResponse,
  SkillInstallResponse,
} from "@/app/stores/_types/store";


// ---------------------------------------------------------------------------
// Base path
// ---------------------------------------------------------------------------

/**
 * All catalog endpoints live under this prefix. Declared once so a
 * future path change is a one-line edit here.
 */
const SKILL_CATALOG_API_BASE = "/api/skills/catalog";


// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/**
 * Structured error thrown by every function in this module EXCEPT
 * ``refreshSkillCatalog`` (which returns ``status: "error"`` on the
 * normal failure path and only throws on network/parse errors).
 *
 * The backend returns ``{detail: {error, code, details}}`` on HTTP
 * failure (see ``_skill_error_to_http`` in ``routers/skills.py``).
 * This class rehydrates that shape so callers can:
 *
 *   - Show a friendly message (``err.message``).
 *   - Branch on ``err.code`` for special handling
 *     (e.g. ``SKILL_CATALOG_NOT_INSTALLABLE`` -> show preview-only UI).
 *   - Inspect ``err.details`` for field-level validation errors.
 *
 * ``status`` is the HTTP status code (0 when the network itself
 * failed before a response was received).
 */
export class SkillCatalogApiError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly status: number;

  constructor(
    message: string,
    code: string,
    details: Record<string, unknown>,
    status: number,
  ) {
    super(message);
    this.name = "SkillCatalogApiError";
    this.code = code;
    this.details = details;
    this.status = status;
  }
}


// ---------------------------------------------------------------------------
// Response parsing
// ---------------------------------------------------------------------------

/**
 * Extract a ``SkillCatalogApiError`` from a failed HTTP response.
 *
 * The backend uses two error shapes:
 *
 *   1. **Structured** -- ``{"detail": {"error", "code", "details"}}``
 *      (raised by ``HTTPException(detail=exc.to_dict())`` in the
 *      catalog router).
 *
 *   2. **Unstructured** -- ``{"detail": "some string"}`` (raised by
 *      FastAPI internally, e.g. on routing errors) or plain text.
 *
 * Both are converted to ``SkillCatalogApiError``. The caller never
 * needs to inspect the raw response body.
 */
async function parseErrorResponse(
  response: Response,
): Promise<SkillCatalogApiError> {
  const status = response.status;

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON body (e.g. HTML error page from a reverse proxy).
    try {
      const text = await response.text();
      return new SkillCatalogApiError(
        text.slice(0, 300) || `HTTP ${status}`,
        "SKILL_CATALOG_HTTP_ERROR",
        {},
        status,
      );
    } catch {
      return new SkillCatalogApiError(
        `HTTP ${status}`,
        "SKILL_CATALOG_HTTP_ERROR",
        {},
        status,
      );
    }
  }

  // Structured shape: {"detail": {"error", "code", "details"}}
  if (
    body &&
    typeof body === "object" &&
    "detail" in body &&
    body.detail &&
    typeof body.detail === "object"
  ) {
    const detail = body.detail as Record<string, unknown>;
    if (typeof detail.error === "string") {
      return new SkillCatalogApiError(
        detail.error,
        typeof detail.code === "string"
          ? detail.code
          : `HTTP_${status}`,
        detail.details && typeof detail.details === "object"
          ? (detail.details as Record<string, unknown>)
          : {},
        status,
      );
    }
  }

  // Unstructured: {"detail": "message"}
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as Record<string, unknown>).detail;
    if (typeof detail === "string") {
      return new SkillCatalogApiError(
        detail,
        `HTTP_${status}`,
        {},
        status,
      );
    }
  }

  // Anything else -- dump a truncated JSON for diagnostics.
  return new SkillCatalogApiError(
    `HTTP ${status}: ${JSON.stringify(body).slice(0, 300)}`,
    "SKILL_CATALOG_HTTP_ERROR",
    {},
    status,
  );
}


// ---------------------------------------------------------------------------
// Low-level fetch helper
// ---------------------------------------------------------------------------

/**
 * Send a request to ``SKILL_CATALOG_API_BASE + path`` and return
 * parsed JSON.
 *
 * Throws ``SkillCatalogApiError`` on:
 *   - Network failure (``status: 0``, code ``"NETWORK_ERROR"``).
 *   - Any non-2xx response (structured or unstructured).
 *
 * Never returns ``null`` -- an empty body is parsed as ``{}``.
 */
async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const url = `${SKILL_CATALOG_API_BASE}${path}`;

  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    throw new SkillCatalogApiError(
      err instanceof Error ? err.message : "Network request failed.",
      "NETWORK_ERROR",
      {},
      0,
    );
  }

  if (!response.ok) {
    throw await parseErrorResponse(response);
  }

  // Parse JSON; a 2xx response with an empty body becomes ``{}``.
  try {
    return (await response.json()) as T;
  } catch {
    return {} as T;
  }
}


// ===========================================================================
// Public API
// ===========================================================================

/**
 * Fetch every item in the merged Skill Catalog.
 *
 * The backend merges four sources (builtin + remote + custom + extra)
 * and returns the union. The first call in a 24h window triggers a
 * remote fetch; subsequent calls return the cached snapshot until the
 * TTL expires, unless ``refreshSkillCatalog`` has been called.
 *
 * The full HTTP response also carries a ``status_info`` block for
 * debugging (remote fetch error, cache age, ...) -- it is
 * intentionally dropped here because the store UI does not surface
 * it. Callers that need it can use the raw ``request`` helper
 * pattern below (see ``refreshSkillCatalog``).
 */
export async function listSkillCatalog(): Promise<SkillCatalogItem[]> {
  const data = await request<SkillCatalogListResponse>("");
  return Array.isArray(data.items) ? data.items : [];
}

/**
 * Fetch a single catalog item by its canonical ID.
 *
 * Throws ``SkillCatalogApiError`` with code ``SKILL_CATALOG_ITEM_NOT_FOUND``
 * and ``status: 404`` when the ID does not exist. Any other failure
 * (network, parse) surfaces its own code.
 *
 * The details modal uses this to refresh the item's metadata on open,
 * so a long-lived tab does not display stale data from the 24h cache.
 */
export async function getSkillCatalogItem(
  itemId: string,
): Promise<SkillCatalogItem> {
  const data = await request<{
    status: string;
    item: SkillCatalogItem;
  }>(`/${encodeURIComponent(itemId)}`);

  if (!data.item) {
    throw new SkillCatalogApiError(
      "Catalog item response was missing the item object.",
      "INVALID_RESPONSE",
      { item_id: itemId },
      200,
    );
  }
  return data.item;
}

/**
 * Force-fetch the remote catalog, ignoring the 24h TTL.
 *
 * Two distinct outcomes are possible on success (HTTP 200):
 *
 *   - ``status: "success"`` -- the remote fetch succeeded; the
 *     response message carries the item count.
 *   - ``status: "error"``   -- the remote fetch FAILED (network
 *     error, bad URL, malformed JSON) but the backend still serves
 *     the last-known items (or the builtin catalog if none exist).
 *     The response message carries the exact error reason from
 *     ``status_info.remote_last_error``.
 *
 * This function does NOT throw on the ``status: "error"`` case --
 * that is a state the caller must surface to the user (typically as
 * a warning toast), not an exception. It DOES throw on network
 * failures and non-2xx HTTP responses, because those mean the
 * request never reached the backend at all.
 *
 * Callers should treat the two success-path statuses differently:
 *
 *     const resp = await refreshSkillCatalog();
 *     if (resp.status === "error") {
 *       showToast.warning("Refresh Failed", resp.message);
 *     } else {
 *       showToast.success("Refreshed", resp.message);
 *     }
 *     // In both cases, the item list may have changed:
 *     const items = await listSkillCatalog();
 */
export async function refreshSkillCatalog(): Promise<SkillCatalogRefreshResponse> {
  return request<SkillCatalogRefreshResponse>("/refresh", {
    method: "POST",
  });
}

/**
 * Download, verify, and install a skill from the catalog.
 *
 * This is a SYNCHRONOUS operation from the client's perspective: the
 * backend does the download + SHA-256 verification + install within
 * the request (usually 2-5 seconds), then returns the full result.
 * There is no job polling.
 *
 * The caller is expected to wrap this call in a UI state machine
 * (e.g. ``CatalogSkillInstallModal``) that:
 *
 *   - Disables the Install button on the card while the promise is
 *     pending.
 *   - Displays a progress UI while waiting.
 *   - Renders the returned ``SkillInstallResponse`` (or the thrown
 *     ``SkillCatalogApiError``) as success/error banners.
 *
 * Failure cases (all throw ``SkillCatalogApiError``):
 *
 *   - ``SKILL_CATALOG_ITEM_NOT_FOUND``       (404) -- ID does not exist.
 *   - ``SKILL_CATALOG_NOT_INSTALLABLE``      (400) -- item has no ZIP.
 *   - ``SKILL_CATALOG_CHECKSUM_MISMATCH``    (400) -- supply-chain guard.
 *   - ``SKILL_CATALOG_TIMEOUT``              (400) -- 60s hard cap.
 *   - ``SKILL_CATALOG_TOO_LARGE``            (400) -- >10 MB ZIP.
 *   - ``SKILL_CATALOG_DOWNLOAD_FAILED``      (400) -- HTTP error from
 *                                                     the ZIP host.
 *   - ``SKILL_CATALOG_UNSAFE_URL``           (400) -- non-HTTP scheme.
 *   - ``SKILL_CATALOG_INSTALL_FAILED``       (500) -- unexpected error
 *                                                     in the manager.
 *
 * On success, ``response.skill.id`` and ``response.skill.version``
 * carry the installed identity. Note that the defensive failure path
 * in the backend (install succeeded but registry refresh failed --
 * extremely unlikely) returns only ``{id, version, name}`` without
 * the full summary. Callers MUST only read ``id``, ``version``, and
 * ``name`` from this response to remain robust against both shapes.
 */
export async function installSkillFromCatalog(
  itemId: string,
): Promise<SkillInstallResponse> {
  return request<SkillInstallResponse>(
    `/${encodeURIComponent(itemId)}/install`,
    { method: "POST" },
  );
}


// ===========================================================================
// Human-readable error formatting
// ===========================================================================

/**
 * Format a ``SkillCatalogApiError`` (or any error) into a single
 * toast-friendly string that includes the structured error code when
 * available.
 *
 * Example outputs:
 *   - ``"Skill 'x' SHA-256 mismatch. [SKILL_CATALOG_CHECKSUM_MISMATCH]"``
 *   - ``"Catalog item 'x' cannot be installed: Preview only. [SKILL_CATALOG_NOT_INSTALLABLE]"``
 *   - ``"Network request failed."``
 *
 * Mirrors ``formatProviderError`` in ``providerApi.ts``. Kept as a
 * separate function (rather than sharing) to preserve the
 * subsystem-independence guarantee documented at the top of this
 * module.
 */
export function formatSkillCatalogError(err: unknown): string {
  if (err instanceof SkillCatalogApiError) {
    if (err.code && err.code !== "SKILL_CATALOG_HTTP_ERROR") {
      return `${err.message} [${err.code}]`;
    }
    return err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return "Unexpected error.";
}
