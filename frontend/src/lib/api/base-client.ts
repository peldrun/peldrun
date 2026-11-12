// src/lib/api/base-client.ts

/**
 * Generic HTTP client foundation for feature-level API layers.
 *
 * This module is DELIBERATELY independent of the memory feature.
 * It provides a reusable `apiFetch` primitive that any future
 * feature can wrap with its own error types, without duplicating
 * the network-layer concerns:
 *
 *   * JSON serialization / deserialization with tolerant parsing.
 *   * `FormData` passthrough (browser sets multipart headers).
 *   * Content-Type and Accept header management.
 *   * `AbortSignal` propagation for React Query cancellation.
 *   * Network error normalization (`fetch` rejection -> typed error).
 *
 * Why this does not replace `features/memory/api/client.ts`
 * ---------------------------------------------------------
 *
 * The memory client has three feature-specific concerns that a
 * generic wrapper must NOT own:
 *
 *   * `MemoryApiError` -- a class whose shape (errorType,
 *     fields, context) is dictated by the memory backend's
 *     error envelope taxonomy.
 *   * `classifyMemoryError` -- logic that inspects the four
 *     error body shapes and produces a `MemoryApiError`.
 *   * `MEMORY_API_BASE` -- a per-feature URL prefix.
 *
 * `apiFetch` accepts an `onError` callback that lets the
 * memory client plug its own classifier in WITHOUT the
 * generic layer knowing about memory at all. Any future
 * feature (skills, projects, ...) does the same with its own
 * error type.
 *
 * Unused today
 * ------------
 *
 * The memory feature currently calls `memoryFetch` directly
 * (which is a self-contained 90-line function). This module
 * exists because the spec (§4.2) calls for it, and because
 * the memory feature may eventually be refactored to use it.
 * Until then, this is dead infrastructure -- exported so it
 * can be picked up when a second feature needs it.
 *
 * Deliberately NOT exported from any barrel file. Import it
 * directly from `@/lib/api/base-client` when needed.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Options accepted by `apiFetch`.
 */
export interface ApiFetchOptions extends Omit<RequestInit, 'body'> {
  /**
   * A JSON-serializable body. Stringified automatically with
   * `Content-Type: application/json`.
   *
   * Pass `FormData` for multipart uploads -- the wrapper
   * detects it and does NOT stringify or set Content-Type
   * (the browser sets the boundary-laden value itself).
   */
  json?: unknown;

  /**
   * Base URL prepended to `path`. Defaults to `''` (the path
   * is used as-is, matching Next.js same-origin requests like
   * `/api/memory`).
   */
  baseUrl?: string;

  /**
   * Called on every non-ok HTTP response AND on every
   * `fetch()` rejection (network error). The callback is
   * expected to return an Error instance to throw, or to
   * throw its own error directly.
   *
   * If omitted, a plain `Error` is thrown with the HTTP
   * status and a generic message. That default is fine for
   * quick prototypes but NOT for production code -- it loses
   * the structured error body.
   */
  onError?: (
    status: number,
    parsedBody: unknown,
    rawResponse: Response | null,
  ) => Error;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Parse a response body tolerantly.
 *
 * Returns the parsed JSON when the response advertises
 * `application/json`, `null` for empty bodies, and the raw
 * text for non-JSON content types (HTML error pages, plain
 * text from a proxy). Never throws -- a malformed JSON body
 * yields `null` so the caller's error classifier can decide
 * what to do.
 */
async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204 || response.status === 205) return null;
  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('application/json')) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  try {
    return await response.text();
  } catch {
    return null;
  }
}

/**
 * Detect a "network failure" (fetch rejected before any
 * response arrived). Distinguished from an HTTP error where
 * the server responded with a non-2xx status.
 */
function isAbortError(err: unknown): boolean {
  if (err instanceof DOMException && err.name === 'AbortError') return true;
  if (err instanceof Error && err.name === 'AbortError') return true;
  return false;
}

// ---------------------------------------------------------------------------
// Core fetch
// ---------------------------------------------------------------------------

/**
 * Issue a fetch request and parse the response.
 *
 * Throws on:
 *
 *   * `fetch()` rejection that is NOT an abort (network
 *     failure, CORS preflight failure, DNS failure). The
 *     error is routed through `onError(0, null, null)` if
 *     provided.
 *
 *   * Non-ok HTTP responses. Routed through `onError(status,
 *     body, response)` if provided.
 *
 * `AbortError` propagates verbatim -- callers integrating
 * with TanStack Query MUST see the original error so that
 * cancelled queries are not surfaced as user-visible errors.
 *
 * On success, returns the parsed body typed as `T`. The
 * caller is responsible for narrowing `T` to the actual
 * response shape (e.g. via a feature-specific `*Response`
 * type from `types/responses.ts`).
 */
export async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const {
    json,
    baseUrl = '',
    onError,
    headers,
    ...rest
  } = options;

  const finalHeaders = new Headers(headers);
  if (!finalHeaders.has('Accept')) {
    finalHeaders.set('Accept', 'application/json');
  }

  let body: BodyInit | undefined;
  if (json instanceof FormData) {
    body = json;
  } else if (json !== undefined) {
    if (!finalHeaders.has('Content-Type')) {
      finalHeaders.set('Content-Type', 'application/json');
    }
    body = JSON.stringify(json);
  }

  const url = `${baseUrl}${path}`;

  let response: Response;
  try {
    response = await fetch(url, {
      ...rest,
      headers: finalHeaders,
      body,
    });
  } catch (err) {
    if (isAbortError(err)) throw err;
    if (onError) {
      throw onError(0, null, null);
    }
    throw err instanceof Error ? err : new Error('Network request failed.');
  }

  const parsed = await parseBody(response);

  if (!response.ok) {
    if (onError) {
      throw onError(response.status, parsed, response);
    }
    throw new Error(
      `Request failed with status ${response.status}`,
    );
  }

  return parsed as T;
}
