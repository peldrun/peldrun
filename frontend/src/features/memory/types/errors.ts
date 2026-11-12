// src/features/memory/types/errors.ts

/**
 * Error types and normalization for the PELDRUN Memory API.
 *
 * The memory backend emits errors in FOUR distinct shapes, not
 * one. A naive client that assumes a single shape silently loses
 * information on the other three. This module provides:
 *
 *   1. Typed representations of every wire shape (below).
 *   2. A single `MemoryApiError` class that callers can consume
 *      uniformly (extends the spec's class with `context` and
 *      `rawBody` for diagnostics).
 *   3. `classifyMemoryError(status, body, fallbackOperation)` --
 *      the ONE function that turns any response body into a
 *      `MemoryApiError`. Every API call site goes through this.
 *   4. `uxHintFor(error)` -- the per-error-code UX policy from
 *      the spec's section 4.5 table, so error-handling code in
 *      components can ask "what should I DO?" without a
 *      hardcoded switch at each call site.
 *
 * The four backend error shapes
 * -----------------------------
 *
 * **Shape 1 -- Uniform error envelope** (from ``_error_response``
 * in memory_crud / memory_projects / memory_import /
 * memory_settings / memory_export / memory_usage):
 *
 *     {
 *       "status": "error",
 *       "operation": "<op>",
 *       "error": "<error-type>",
 *       "message": "<human text>",
 *       "context": { ... }   // optional, varies per error
 *     }
 *
 * **Shape 2 -- HTTPException.detail as dict** (from direct
 * `raise HTTPException(detail={...})` calls in memory_crud /
 * memory_projects / memory_settings / memory_import / memory_export /
 * memory_usage; FastAPI serializes it verbatim):
 *
 *     {
 *       "detail": {
 *         "error": "<error-type>",
 *         "message": "<human text>",
 *         "fields": [ {...}, ... ],   // optional
 *         "hint": "<short string>"    // optional
 *       }
 *     }
 *
 * **Shape 3 -- FastAPI 422 default** (from Pydantic body
 * validation, e.g. extra fields, malformed types). This shape
 * CANNOT be avoided until the app-level
 * `RequestValidationError` handler is wired (see
 * `memory_settings.py` module docstring for the exact plan):
 *
 *     {
 *       "detail": [
 *         { "loc": ["body", "field"], "msg": "...", "type": "..." },
 *         ...
 *       ]
 *     }
 *
 * **Shape 4 -- `_pydantic_error_to_400` in memory_settings** (a
 * structured 400 that WRAPS the 422 shape inside `detail`):
 *
 *     {
 *       "detail": {
 *         "error": "invalid_settings",
 *         "message": "Settings validation failed.",
 *         "fields": [ {...}, ... ]
 *       }
 *     }
 *
 * Shape 4 is technically a specialization of shape 2 (both have
 * a dict `detail`), and the classifier handles them with the
 * same branch. The distinction matters only in the docs, not in
 * the normalization code.
 *
 * How the API client uses this
 * ----------------------------
 *
 * `memoryFetch` in `features/memory/api/client.ts` calls
 * `classifyMemoryError(response.status, parsedBody, operation)`
 * on EVERY non-ok response, and re-throws the returned
 * `MemoryApiError`. Components then either:
 *
 *   * Catch it and inspect `error.errorType` for a bespoke
 *     response, OR
 *   * Call `uxHintFor(error)` for the standard policy (toast vs
 *     inline, retry button, "go to settings" link), OR
 *   * Call `error.fieldMessage('content')` for form-level
 *     field errors.
 *
 * Because the classifier is the only producer of
 * `MemoryApiError` instances, components can rely on:
 *
 *   * `error.statusCode` always matching the HTTP status,
 *   * `error.errorType` always being a non-empty string,
 *   * `error.fields` always being an array (possibly empty),
 *   * `error.message` always being non-empty.
 *
 * No shape from the backend can violate these invariants --
 * every branch of the classifier supplies a sensible fallback.
 */

// NOTE on error-code placement: `enums.ts` does not (and should
// not) define error-type strings -- error types are cross-cutting
// string identifiers, not enum-like value sets. Error codes are
// declared in THIS module because they are a client-side
// normalization concern, not a wire-format concern, and are
// meaningless to any other module until `classifyMemoryError`
// maps a wire payload to them.

// ---------------------------------------------------------------------------
// Wire shapes
// ---------------------------------------------------------------------------

/**
 * A field-level validation error, as produced by Pydantic and
 * forwarded by the backend.
 *
 * ``loc`` is the field's location path. For Pydantic, this is
 * typically ``["body", "<field>"]`` or ``["body", "<nested>",
 * "<field>"]``. The classifier normalizes every element to a
 * string, so callers can safely ``loc.join('.')``.
 *
 * ``msg`` is the human-readable message. Usually English, always
 * safe to display (Pydantic does not leak internals in ``msg``).
 *
 * ``type`` is the Pydantic error code (e.g. ``"value_error"``,
 * ``"missing"``, ``"string_too_long"``). Stable across Pydantic
 * versions; safe to switch on for fine-grained behavior.
 */
export interface MemoryFieldError {
  loc: string[];
  msg: string;
  type: string;
}

/**
 * Free-form context attached to some errors.
 *
 * Its keys vary by error type -- e.g. ``mode_disabled`` carries
 * ``{"mode": "disabled"}``, while ``invalid_settings`` carries
 * field-level details in ``fields[]`` rather than ``context``.
 * The type is intentionally loose: consumers must narrow on
 * specific keys and provide sensible fallbacks.
 */
export type MemoryErrorContext = Record<string, unknown>;

/**
 * Shape 1: the uniform error envelope returned by the backend's
 * ``_error_response`` helper.
 *
 * Every router except the app-level 422 handler uses this shape
 * for its own errors -- it is the dominant form.
 */
export interface MemoryErrorEnvelope {
  status: 'error';
  operation: string;
  error: string;
  message: string;
  context?: MemoryErrorContext;
}

/**
 * The ``detail`` object inside an ``HTTPException(detail={...})``
 * response (shape 2 and shape 4).
 *
 * ``fields`` is present on validation errors, absent on
 * operational errors (mode, not-found, service-down).
 *
 * ``hint`` is present only for a handful of errors -- currently
 * ``confirmation_required`` uses it to advertise the exact query
 * param expected (``"confirm=yes"``).
 */
export interface MemoryHttpExceptionDetail {
  error: string;
  message: string;
  fields?: MemoryFieldError[];
  hint?: string;
  context?: MemoryErrorContext;
}

/**
 * Shape 2 / 4: an HTTPException whose ``detail`` is a dict.
 */
export interface MemoryHttpExceptionResponse {
  detail: MemoryHttpExceptionDetail;
}

/**
 * One item in FastAPI's default 422 body (shape 3).
 *
 * ``loc`` elements may be strings OR numbers -- FastAPI puts an
 * index in the path when the offending value is inside an array
 * (e.g. ``["body", "tags", 3]`` for the 4th tag). The classifier
 * coerces every element to a string for uniform handling.
 */
export interface MemoryFastApiValidationItem {
  loc: (string | number)[];
  msg: string;
  type: string;
}

/**
 * Shape 3: FastAPI's default 422 response.
 *
 * In this shape, ``detail`` is an ARRAY of validation items, not
 * a dict. This is the shape the classifier MUST distinguish from
 * shape 2 -- a naive ``body.detail.error`` read on a 422 returns
 * ``undefined`` and silently loses the actual validation errors.
 */
export interface MemoryFastApiValidationResponse {
  detail: MemoryFastApiValidationItem[];
}

/**
 * The union of every possible non-2xx response body from the
 * memory API.
 *
 * Note: additional shapes may be added as the backend evolves.
 * Consumers should NOT exhaustively switch on this union -- use
 * the classifier, which is forward-compatible (an unknown shape
 * falls through to a status-based fallback).
 */
export type MemoryErrorResponse =
  | MemoryErrorEnvelope
  | MemoryHttpExceptionResponse
  | MemoryFastApiValidationResponse;

// ---------------------------------------------------------------------------
// Error type constants
// ---------------------------------------------------------------------------

/**
 * Every error code the memory backend is known to emit, gathered
 * from a full read of the backend routers.
 *
 * The union type ``KnownMemoryErrorType`` (below) is derived
 * from this object, so adding a code here makes it visible to
 * ``switch`` statements and typo-checked comparisons throughout
 * the feature.
 *
 * Naming: values are the exact strings the backend sends (some
 * snake_case, some CamelCase -- the backend is not consistent).
 * The CONSTANT NAME is CamelCase for ergonomics.
 *
 * Two families exist:
 *
 *   * **snake_case codes** -- the backend's intentional error
 *     taxonomy: ``mode_disabled``, ``not_found``,
 *     ``scope_required``, etc. These are the ones the UI
 *     switches on for behavior (see ``uxHintFor``).
 *
 *   * **CamelCase Python class names** -- the backend's
 *     ``_error_response`` sets ``error`` to ``type(exc).__name__``
 *     for some errors (notably ``MemoryValidationError``,
 *     ``MemoryNotFoundError``, ``MemoryUnavailableError``).
 *     These arrive on shape 1 responses when the router uses
 *     ``_error_response`` rather than a manual 400.
 *
 * Both must be handled, but only the snake_case ones appear in
 * the UX hint table -- the CamelCase ones fall back to the
 * class-based defaults (e.g. ``MemoryUnavailableError`` gets a
 * retry button, everything else gets a plain toast).
 */
export const MemoryErrorType = {
  // --- Mode / configuration ---
  ModeDisabled: 'mode_disabled',
  ModeReadOnly: 'mode_read_only',
  InvalidSettings: 'invalid_settings',
  EmptyUpdate: 'empty_update',
  EmptyOverride: 'empty_override',
  NestedOverrides: 'nested_overrides',

  // --- Scope ---
  MissingScopeType: 'missing_scope_type',
  MissingScopeId: 'missing_scope_id',
  InvalidScopeType: 'invalid_scope_type',
  InvalidScopeRef: 'invalid_scope_ref',
  IncompleteScope: 'incomplete_scope',
  CannotResolveScope: 'cannot_resolve_scope',
  MultipleScopesAmbiguous: 'multiple_scopes_ambiguous',
  ScopeRequired: 'scope_required',
  NoScopes: 'no_scopes',
  InvalidTargetScopeType: 'invalid_target_scope_type',
  InvalidTargetScopeId: 'invalid_target_scope_id',
  UnsupportedTargetScope: 'unsupported_target_scope',
  UserScopeRequiresExplicit: 'user_scope_requires_explicit',

  // --- Entry / value validation ---
  ForbiddenSourceType: 'forbidden_source_type',
  InvalidMemoryType: 'invalid_memory_type',
  InvalidStatus: 'invalid_status',
  InvalidProjectId: 'invalid_project_id',
  Duplicate: 'duplicate',

  // --- Lookup ---
  NotFound: 'not_found',

  // --- Destructive confirmation ---
  ConfirmationRequired: 'confirmation_required',

  // --- Import ---
  MissingFile: 'missing_file',
  FileTooLarge: 'file_too_large',
  MissingSource: 'missing_source',
  InvalidBody: 'invalid_body',
  InvalidEncoding: 'invalid_encoding',
  InvalidJson: 'invalid_json',
  InvalidJsonShape: 'invalid_json_shape',
  UnsupportedContentType: 'unsupported_content_type',

  // --- Export ---
  InvalidFormat: 'invalid_format',
  InvalidLimit: 'invalid_limit',

  // --- Usage ---
  InvalidPeriod: 'invalid_period',
  InvalidBy: 'invalid_by',
  InvalidGranularity: 'invalid_granularity',

  // --- Platform ---
  ServiceNotInitialized: 'service_not_initialized',
  RequestValidation: 'request_validation',

  // --- Python exception class names (from _error_response) ---
  MemoryValidationError: 'MemoryValidationError',
  MemoryNotFoundError: 'MemoryNotFoundError',
  MemoryUnavailableError: 'MemoryUnavailableError',
  InternalError: 'InternalError',

  // --- Client-side only (never emitted by the backend) ---
  UnknownError: 'UnknownError',
  NetworkError: 'NetworkError',
  ClientError: 'ClientError',
} as const;

/**
 * The literal type of every known error code.
 *
 * Use this for exhaustiveness checks and for typed `switch`
 * statements. Import `MemoryErrorType` (the const object) when
 * you need the string values, and `KnownMemoryErrorType` (the
 * type) when you need to declare a variable that can only hold
 * known codes.
 */
export type KnownMemoryErrorType =
  (typeof MemoryErrorType)[keyof typeof MemoryErrorType];

/**
 * Any error code, known or unknown.
 *
 * The `(string & {})` intersection preserves IntelliSense
 * autocomplete for the known literals while still accepting any
 * string. The backend is free to add new codes at any time, and
 * the client must not break when it does -- so `errorType` on
 * `MemoryApiError` is this wider type, not the narrower one.
 */
export type MemoryErrorType = KnownMemoryErrorType | (string & {});

// ---------------------------------------------------------------------------
// The client-side error class
// ---------------------------------------------------------------------------

/**
 * The single error class thrown by every memory API call.
 *
 * Fields mirror the spec's section 4.5 class, extended with
 * `context` (for backend diagnostics) and `rawBody` (for logs
 * and bug reports). Neither extension affects the message; they
 * are strictly for developers.
 *
 * Invariants (guaranteed by ``classifyMemoryError``):
 *
 *   * ``statusCode`` is the exact HTTP status.
 *   * ``errorType`` is a non-empty string.
 *   * ``fields`` is always an array (possibly empty).
 *   * ``message`` is always a non-empty string.
 *
 * The class is intended to be caught, inspected, and rendered --
 * it is NOT intended for programmatic distinction between error
 * categories that both surface identically to the user. Use the
 * `is(...)` method with a constant from `MemoryErrorType` for
 * that distinction, not ``instanceof`` checks against subclass
 * hierarchies (there are none).
 *
 * Design note -- why not subclasses per error type?
 *
 * 1. The backend's error code set is OPEN (new codes can appear
 *    at any release). A subclass hierarchy would require the
 *    frontend to ship a new build for every backend error code.
 *
 * 2. Most call sites do not care about the specific error --
 *    they want the standard UX behavior (toast, inline, retry).
 *    That behavior is a mapping from `errorType` to a hint, not
 *    a class hierarchy.
 *
 * 3. Two errors with the same `errorType` (e.g. two 404s from
 *    different operations) are structurally identical for the
 *    UI's purposes -- the operation is on the object, but not
 *    the type.
 */
export class MemoryApiError extends Error {
  /**
   * The backend operation name (e.g. ``"create"``,
   * ``"settings_update"``). Set to the caller-provided fallback
   * when the response body does not include one -- which is
   * common on shapes 2-4.
   */
  public readonly operation: string;

  /**
   * The error code. Use ``MemoryErrorType.*`` constants for
   * comparisons to get typo-checking and IntelliSense.
   */
  public readonly errorType: MemoryErrorType;

  /** The HTTP status code the server returned. */
  public readonly statusCode: number;

  /**
   * Field-level validation errors, when present. Empty array
   * (never undefined) so callers can always iterate safely.
   */
  public readonly fields: MemoryFieldError[];

  /**
   * Backend-provided context (varies by error type). Empty
   * object (never undefined) so callers can always index
   * safely with `??` fallbacks.
   */
  public readonly context: MemoryErrorContext;

  /**
   * The raw response body as received. Useful for logging and
   * bug reports; should NOT be rendered to users. May be `null`
   * when the response had no parseable JSON body (e.g. an HTML
   * 502 from a proxy).
   */
  public readonly rawBody: unknown;

  constructor(params: {
    operation: string;
    errorType: MemoryErrorType;
    statusCode: number;
    message: string;
    fields?: MemoryFieldError[];
    context?: MemoryErrorContext;
    rawBody?: unknown;
  }) {
    super(params.message);
    this.name = 'MemoryApiError';
    this.operation = params.operation;
    this.errorType = params.errorType;
    this.statusCode = params.statusCode;
    this.fields = params.fields ?? [];
    this.context = params.context ?? {};
    this.rawBody = params.rawBody;

    // Preserve the stack trace across transpilation targets
    // where `extends Error` alone loses it.
    if (typeof (Error as unknown as { captureStackTrace?: unknown })
      .captureStackTrace === 'function') {
      (Error as unknown as {
        captureStackTrace: (target: object, ctor: Function) => void;
      }).captureStackTrace(this, MemoryApiError);
    }
  }

  /** Compare against a known error type constant. */
  is(errorType: MemoryErrorType): boolean {
    return this.errorType === errorType;
  }

  /**
   * `true` iff ``loc`` (as a dot-joined path) appears in the
   * field-error list.
   */
  hasField(loc: string | string[]): boolean {
    const key = Array.isArray(loc) ? loc.join('.') : loc;
    return this.fields.some((f) => f.loc.join('.') === key);
  }

  /**
   * Return the message of the first field error matching ``loc``,
   * or ``undefined`` when no such field error exists.
   *
   * ``loc`` may be a suffix of the actual location -- the backend
   * prefixes with ``"body"``, but callers typically only care
   * about the leaf field name. So ``fieldMessage("content")``
   * finds a ``["body", "content"]`` error. If you need to match
   * the full path exactly, use ``hasField`` with the full array.
   */
  fieldMessage(loc: string | string[]): string | undefined {
    if (Array.isArray(loc)) {
      const key = loc.join('.');
      return this.fields.find((f) => f.loc.join('.') === key)?.msg;
    }
    // Leaf-name match: accept any loc whose last element equals
    // the query string, OR whose dot-joined form equals it (in
    // case the caller passes a pre-joined path).
    return this.fields.find((f) => {
      const joined = f.loc.join('.');
      return joined === loc || f.loc[f.loc.length - 1] === loc;
    })?.msg;
  }
}

// ---------------------------------------------------------------------------
// Normalization
// ---------------------------------------------------------------------------

/** Internal: is `value` a plain object (not null, not array)? */
function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Internal: is `value` a non-empty string? */
function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

/**
 * Normalize the ``fields`` value from any of the four shapes into
 * a well-formed ``MemoryFieldError[]``.
 *
 * Tolerates missing ``msg`` or ``type`` (returns sensible
 * defaults) so a slightly malformed backend response still
 * produces usable field errors rather than throwing. This
 * matters for shape 3 (FastAPI 422), whose items are documented
 * as ``{loc, msg, type}`` but have historically omitted ``type``
 * in some FastAPI minor versions.
 */
function normalizeFields(raw: unknown): MemoryFieldError[] {
  if (!Array.isArray(raw)) return [];

  const out: MemoryFieldError[] = [];
  for (const item of raw) {
    if (!isObject(item)) continue;
    if (!Array.isArray(item.loc)) continue;
    if (!isNonEmptyString(item.msg)) continue;

    out.push({
      loc: item.loc.map((v) => String(v)),
      msg: item.msg,
      type: isNonEmptyString(item.type) ? item.type : 'unknown',
    });
  }
  return out;
}

/**
 * Map an HTTP status code to a sensible default error type.
 *
 * Only used when the response body does not carry an error
 * type at all (e.g. a proxy's 502 with an HTML body). Every
 * shape 1-4 body carries its own error code, so this fallback
 * fires only on truly unrecognized bodies.
 */
function defaultErrorTypeForStatus(statusCode: number): MemoryErrorType {
  if (statusCode === 404) return MemoryErrorType.NotFound;
  if (statusCode === 413) return MemoryErrorType.FileTooLarge;
  if (statusCode === 415) return MemoryErrorType.UnsupportedContentType;
  if (statusCode === 503) return MemoryErrorType.MemoryUnavailableError;
  if (statusCode >= 500) return MemoryErrorType.InternalError;
  if (statusCode >= 400) return MemoryErrorType.RequestValidation;
  return MemoryErrorType.UnknownError;
}

/**
 * Map an HTTP status code to a safe, generic user-facing message.
 *
 * Only used when the body carries no ``message`` field. The
 * messages here are deliberately vague ("The request could not
 * be completed") rather than fabricated-specific -- the goal is
 * to give the user something to see, not to guess at what went
 * wrong.
 */
function defaultMessageForStatus(statusCode: number): string {
  switch (statusCode) {
    case 400:
      return 'The request was rejected. Check your inputs and try again.';
    case 401:
      return 'Authentication required.';
    case 403:
      return 'You do not have permission to perform this action.';
    case 404:
      return 'The requested item was not found.';
    case 413:
      return 'The uploaded file is too large.';
    case 415:
      return 'The upload format is not supported.';
    case 422:
      return 'Some fields are invalid. Review your inputs.';
    case 500:
      return 'The memory service encountered an internal error.';
    case 503:
      return 'The memory service is temporarily unavailable. Try again shortly.';
    default:
      return statusCode >= 500
        ? 'An unexpected server error occurred.'
        : 'The request could not be completed.';
  }
}

/**
 * Turn any memory-API error response body into a
 * ``MemoryApiError``.
 *
 * This is the SINGLE function that every API call site should
 * use to produce a `MemoryApiError`. It handles all four wire
 * shapes and NEVER throws -- every malformed input produces a
 * valid `MemoryApiError` with sensible fallbacks.
 *
 * @param statusCode        The HTTP status from the response.
 * @param rawBody           The parsed JSON body, or `null` /
 *                          `undefined` when the body could not
 *                          be parsed (e.g. HTML error page,
 *                          empty body).
 * @param fallbackOperation The operation name to use when the
 *                          body does not include one. Callers
 *                          pass the operation they were
 *                          attempting (e.g. `"create"`,
 *                          `"settings_update"`). This makes
 *                          logs actionable even for shape 2-4
 *                          responses, which never include
 *                          `operation`.
 */
export function classifyMemoryError(
  statusCode: number,
  rawBody: unknown,
  fallbackOperation: string,
): MemoryApiError {
  // --- Shape 1: uniform error envelope -----------------------
  if (isObject(rawBody) && rawBody.status === 'error') {
    const operation = isNonEmptyString(rawBody.operation)
      ? rawBody.operation
      : fallbackOperation;
    const errorType = isNonEmptyString(rawBody.error)
      ? rawBody.error
      : defaultErrorTypeForStatus(statusCode);
    const message = isNonEmptyString(rawBody.message)
      ? rawBody.message
      : defaultMessageForStatus(statusCode);
    return new MemoryApiError({
      operation,
      errorType,
      statusCode,
      message,
      fields: normalizeFields(rawBody.fields),
      context: isObject(rawBody.context)
        ? (rawBody.context as MemoryErrorContext)
        : {},
      rawBody,
    });
  }

  // --- Shapes 2 / 4: HTTPException.detail as dict ------------
  if (
    isObject(rawBody) &&
    isObject(rawBody.detail) &&
    !Array.isArray(rawBody.detail)
  ) {
    const detail = rawBody.detail;
    const errorType = isNonEmptyString(detail.error)
      ? detail.error
      : defaultErrorTypeForStatus(statusCode);
    const message = isNonEmptyString(detail.message)
      ? detail.message
      : defaultMessageForStatus(statusCode);
    return new MemoryApiError({
      operation: fallbackOperation,
      errorType,
      statusCode,
      message,
      fields: normalizeFields(detail.fields),
      context: isObject(detail.context)
        ? (detail.context as MemoryErrorContext)
        : {},
      rawBody,
    });
  }

  // --- Shape 3: FastAPI 422 with detail array ----------------
  if (isObject(rawBody) && Array.isArray(rawBody.detail)) {
    const fields = normalizeFields(rawBody.detail);
    const message =
      fields.length > 0
        ? `Validation failed: ${fields[0].msg}`
        : defaultMessageForStatus(statusCode);
    return new MemoryApiError({
      operation: fallbackOperation,
      errorType: MemoryErrorType.RequestValidation,
      statusCode,
      message,
      fields,
      context: {},
      rawBody,
    });
  }

  // --- Fallback: no recognizable body ------------------------
  return new MemoryApiError({
    operation: fallbackOperation,
    errorType: defaultErrorTypeForStatus(statusCode),
    statusCode,
    message: defaultMessageForStatus(statusCode),
    fields: [],
    context: {},
    rawBody,
  });
}

// ---------------------------------------------------------------------------
// UX policy
// ---------------------------------------------------------------------------

/**
 * The per-error UX policy used by components to decide HOW to
 * surface a given error.
 *
 * Derived from the spec's section 4.5 table, expanded with the
 * ``tone`` field for styling consistency (warning vs error vs
 * info maps to different badge colors in the design system).
 *
 * Components are NOT required to obey ``uxHintFor`` -- a specific
 * flow (e.g. the import wizard) may have a better UX than the
 * generic default. But ``uxHintFor`` should be the starting
 * point, and any deviation should be documented in the
 * component's own comment.
 */
export interface MemoryErrorUxHint {
  /**
   * How to surface the error:
   *
   *   * ``toast``     -- transient notification (5s).
   *   * ``inline``    -- attach to a specific field in the form.
   *   * ``redirect``  -- navigate away (e.g. back to list after 404).
   *   * ``silent``    -- log only, do not show anything (used for
   *                     errors the user triggered on purpose and
   *                     does not need to be told about).
   */
  behavior: 'toast' | 'inline' | 'redirect' | 'silent';

  /**
   * When ``true``, the error toast carries a "Try again" button
   * that re-runs the mutation. Only set for genuinely transient
   * failures (backend unavailable, network timeout).
   */
  showRetry: boolean;

  /**
   * When ``true``, the error toast/panel carries a "Go to
   * settings" link. Set for mode errors and configuration
   * errors that the user can actually fix.
   */
  suggestSettings: boolean;

  /**
   * For ``behavior: 'inline'``, where the message should appear.
   *
   *   * ``'field-from-loc'`` -- use the first ``error.fields[i].loc``
   *                             to target a specific form field.
   *   * ``'content'``       -- the ``content`` textarea specifically
   *                             (used for the duplicate error,
   *                             whose location is not always in
   *                             ``fields``).
   */
  inlineFieldTarget?: 'field-from-loc' | 'content';

  /**
   * Visual tone of the surface. Maps to colors in the design
   * system:
   *
   *   * ``error``   -- red; the action FAILED and nothing happened.
   *   * ``warning`` -- amber; the action was rejected for a
   *                    reason the user might be able to fix.
   *   * ``info``    -- blue; the outcome is not a bug, just
   *                    informational (e.g. 404 on a stale link).
   */
  tone: 'error' | 'warning' | 'info';
}

const DEFAULT_UX_HINT: MemoryErrorUxHint = {
  behavior: 'toast',
  showRetry: false,
  suggestSettings: false,
  tone: 'error',
};

/**
 * Per-error-code UX policy. Entries here OVERRIDE the default.
 *
 * Only snake_case codes appear -- the CamelCase Python exception
 * names (``MemoryUnavailableError`` etc.) are handled by the
 * class-name fallbacks in ``uxHintFor`` below, so that they can
 * apply the same policy as their snake_case counterparts.
 */
const KNOWN_UX_HINTS: Partial<Record<KnownMemoryErrorType, MemoryErrorUxHint>> = {
  // --- Mode errors: user CAN fix, send them to settings ----------
  [MemoryErrorType.ModeDisabled]: {
    behavior: 'toast',
    showRetry: false,
    suggestSettings: true,
    tone: 'warning',
  },
  [MemoryErrorType.ModeReadOnly]: {
    behavior: 'toast',
    showRetry: false,
    suggestSettings: true,
    tone: 'warning',
  },

  // --- Scope errors: usually a frontend bug, surface loudly ------
  [MemoryErrorType.ScopeRequired]: {
    behavior: 'toast',
    showRetry: false,
    suggestSettings: false,
    tone: 'error',
  },

  // --- 404: informational, offer a way out ----------------------
  [MemoryErrorType.NotFound]: {
    behavior: 'redirect',
    showRetry: false,
    suggestSettings: false,
    tone: 'info',
  },
  [MemoryErrorType.MemoryNotFoundError]: {
    behavior: 'redirect',
    showRetry: false,
    suggestSettings: false,
    tone: 'info',
  },

  // --- User-scope write guard ----------------------------------
  [MemoryErrorType.UserScopeRequiresExplicit]: {
    behavior: 'toast',
    showRetry: false,
    suggestSettings: false,
    tone: 'warning',
  },

  // --- Duplicate content: highlight the textarea ----------------
  [MemoryErrorType.Duplicate]: {
    behavior: 'inline',
    showRetry: false,
    suggestSettings: false,
    inlineFieldTarget: 'content',
    tone: 'warning',
  },
  [MemoryErrorType.MemoryValidationError]: {
    behavior: 'inline',
    showRetry: false,
    suggestSettings: false,
    inlineFieldTarget: 'field-from-loc',
    tone: 'error',
  },

  // --- Validation (shape 3): inline per-field -------------------
  [MemoryErrorType.RequestValidation]: {
    behavior: 'inline',
    showRetry: false,
    suggestSettings: false,
    inlineFieldTarget: 'field-from-loc',
    tone: 'error',
  },
  [MemoryErrorType.InvalidSettings]: {
    behavior: 'inline',
    showRetry: false,
    suggestSettings: false,
    inlineFieldTarget: 'field-from-loc',
    tone: 'error',
  },

  // --- Transient: retry is meaningful ---------------------------
  [MemoryErrorType.MemoryUnavailableError]: {
    behavior: 'toast',
    showRetry: true,
    suggestSettings: false,
    tone: 'error',
  },
  [MemoryErrorType.NetworkError]: {
    behavior: 'toast',
    showRetry: true,
    suggestSettings: false,
    tone: 'error',
  },

  // --- Guard rails ---------------------------------------------
  [MemoryErrorType.ConfirmationRequired]: {
    behavior: 'toast',
    showRetry: false,
    suggestSettings: false,
    tone: 'warning',
  },
  [MemoryErrorType.FileTooLarge]: {
    behavior: 'inline',
    showRetry: false,
    suggestSettings: false,
    inlineFieldTarget: 'field-from-loc',
    tone: 'error',
  },

  // --- Platform: 500 is always a bug, do not retry -------------
  [MemoryErrorType.InternalError]: {
    behavior: 'toast',
    showRetry: false,
    suggestSettings: false,
    tone: 'error',
  },
};

/**
 * Return the standard UX policy for a given error.
 *
 * Falls back to ``DEFAULT_UX_HINT`` (plain toast, no retry, no
 * settings link, error tone) for unknown error codes. This
 * ensures new backend error codes never break the UI -- they
 * simply surface as a plain error toast until a specific policy
 * is added here.
 */
export function uxHintFor(error: MemoryApiError): MemoryErrorUxHint {
  const known = KNOWN_UX_HINTS[error.errorType as KnownMemoryErrorType];
  return known ?? DEFAULT_UX_HINT;
}

// ---------------------------------------------------------------------------
// Convenience predicates
// ---------------------------------------------------------------------------

/**
 * `true` iff the error is a mode-related rejection (either
 * ``mode_disabled`` or ``mode_read_only``).
 *
 * Used by components that want to offer a "Settings" link
 * without inspecting the specific error type -- the two codes
 * share the same UX (a settings link is the only fix).
 */
export function isModeError(error: MemoryApiError): boolean {
  return (
    error.errorType === MemoryErrorType.ModeDisabled ||
    error.errorType === MemoryErrorType.ModeReadOnly
  );
}

/**
 * `true` iff the error is a field-level validation failure.
 *
 * Covers all three validation shapes (422, structured 400, and
 * the client-side ``MemoryValidationError`` code). Used by
 * forms to switch into "show inline errors" mode without
 * checking each code individually.
 */
export function isValidationError(error: MemoryApiError): boolean {
  return (
    error.errorType === MemoryErrorType.RequestValidation ||
    error.errorType === MemoryErrorType.MemoryValidationError ||
    error.errorType === MemoryErrorType.InvalidSettings ||
    error.errorType === MemoryErrorType.Duplicate ||
    error.fields.length > 0
  );
}

/**
 * `true` iff the error is a "transient, retrying may help"
 * failure. Used to enable the retry button in toast components.
 *
 * The definition includes:
 *   * 5xx responses (backend crash / unavailability),
 *   * ``MemoryUnavailableError`` (backend down by design),
 *   * ``NetworkError`` (client could not reach the server).
 *
 * Deliberately EXCLUDES 4xx responses -- a validation error will
 * not resolve by retrying the same body.
 */
export function isTransientError(error: MemoryApiError): boolean {
  if (error.errorType === MemoryErrorType.MemoryUnavailableError) {
    return true;
  }
  if (error.errorType === MemoryErrorType.NetworkError) {
    return true;
  }
  return error.statusCode >= 500;
}
