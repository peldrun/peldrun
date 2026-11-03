// frontend/src/lib/providerApi.ts
/**
 * API client for the Provider Packages subsystem (M9 / M10).
 *
 * Wraps ``/api/store/providers/*`` with:
 *
 *   - Strongly-typed function signatures for every endpoint.
 *   - Consistent error handling via ``ProviderApiRequestError``, which
 *     carries the backend's structured ``{error, code, details}``
 *     payload so callers can render field-level errors without
 *     parsing strings.
 *   - A single ``getApiUrl`` call site so a future auth-header
 *     refactor touches one file, not fifteen.
 *
 * Functions are intentionally thin — no retry logic, no caching, no
 * optimistic updates. Those concerns belong to the calling hooks
 * (``useProviderActions``), which have access to the store-level
 * state that a retry policy needs.
 *
 * Every function throws ``ProviderApiRequestError`` on failure. The
 * caller is expected to catch it and either display a toast (via
 * ``showToast``) or record it in local state for inline display.
 *
 * @see ``frontend/src/app/stores/_types/store.ts`` for the full type
 *      surface this module returns.
 */

import type {
  CertificateStatusReport,
  ProviderApiError,
  ProviderCatalogItem,
  ProviderCertificateRecord,
  ProviderConnection,
  ProviderDetailResponse,
  ProviderHealthReport,
  ProviderInstallResult,
  ProviderPackageEntry,
} from "@/app/stores/_types/store";


// ---------------------------------------------------------------------------
// Base path
// ---------------------------------------------------------------------------

/**
 * All provider endpoints live under this prefix. Declared once so a
 * future path change is a one-line edit here.
 */
const PROVIDER_API_BASE = "/api/store/providers";


// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/**
 * Structured error thrown by every function in this module.
 *
 * The backend returns ``{detail: {error, code, details}}`` on failure
 * (see ``omweb/routers/store/providers.py``). This class rehydrates
 * that shape so callers can:
 *
 *   - Show a friendly message (``err.message``).
 *   - Branch on ``err.code`` for special handling
 *     (e.g. ``PROVIDER_PACKAGE_NOT_FOUND`` → show "Refresh" button).
 *   - Inspect ``err.details`` for field-level validation errors.
 *
 * ``status`` is the HTTP status (0 when the network itself failed).
 */
export class ProviderApiRequestError extends Error {
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
    this.name = "ProviderApiRequestError";
    this.code = code;
    this.details = details;
    this.status = status;
  }

  /** Build from a parsed ``ProviderApiError`` + HTTP status. */
  static fromPayload(
    payload: ProviderApiError,
    status: number,
  ): ProviderApiRequestError {
    return new ProviderApiRequestError(
      payload.error || `HTTP ${status}`,
      payload.code || "UNKNOWN_ERROR",
      payload.details || {},
      status,
    );
  }

  /** Build from a raw string or a non-structured 4xx/5xx body. */
  static fromRaw(
    message: string,
    status: number,
  ): ProviderApiRequestError {
    return new ProviderApiRequestError(
      message,
      status >= 500 ? "SERVER_ERROR" : "REQUEST_FAILED",
      {},
      status,
    );
  }
}


// ---------------------------------------------------------------------------
// Response parsing
// ---------------------------------------------------------------------------

/**
 * Extract a ``ProviderApiRequestError`` from a failed HTTP response.
 *
 * The backend uses two error shapes:
 *
 *   1. **Structured** — ``{"detail": {"error", "code", "details"}}``
 *      (raised by ``HTTPException(detail=exc.to_dict())`` in the router).
 *
 *   2. **Unstructured** — ``{"detail": "some string"}`` (raised by
 *      FastAPI internally, e.g. on routing errors) or plain text.
 *
 * Both are converted to ``ProviderApiRequestError``. The caller never
 * needs to inspect the raw response body.
 */
async function parseErrorResponse(
  response: Response,
): Promise<ProviderApiRequestError> {
  const status = response.status;

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON body (e.g. HTML error page from a reverse proxy).
    try {
      const text = await response.text();
      return ProviderApiRequestError.fromRaw(
        text.slice(0, 300) || `HTTP ${status}`,
        status,
      );
    } catch {
      return ProviderApiRequestError.fromRaw(`HTTP ${status}`, status);
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
      return ProviderApiRequestError.fromPayload(
        {
          error: detail.error,
          code:
            typeof detail.code === "string"
              ? detail.code
              : `HTTP_${status}`,
          details:
            detail.details && typeof detail.details === "object"
              ? (detail.details as Record<string, unknown>)
              : {},
        },
        status,
      );
    }
  }

  // Unstructured: {"detail": "message"}
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as Record<string, unknown>).detail;
    if (typeof detail === "string") {
      return ProviderApiRequestError.fromRaw(detail, status);
    }
  }

  // Anything else — dump a truncated JSON for diagnostics.
  return ProviderApiRequestError.fromRaw(
    `HTTP ${status}: ${JSON.stringify(body).slice(0, 300)}`,
    status,
  );
}


// ---------------------------------------------------------------------------
// Low-level fetch helper
// ---------------------------------------------------------------------------

/**
 * Send a request to ``PROVIDER_API_BASE + path`` and return parsed JSON.
 *
 * Throws ``ProviderApiRequestError`` on:
 *   - Network failure (``status: 0``, code ``"NETWORK_ERROR"``).
 *   - Any non-2xx response (structured or unstructured).
 *
 * Never returns ``null`` — an empty body is parsed as ``{}``.
 */
async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const url = `${PROVIDER_API_BASE}${path}`;

  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    throw new ProviderApiRequestError(
      err instanceof Error
        ? err.message
        : "Network request failed.",
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
// Installed providers
// ===========================================================================

/**
 * List every installed provider package.
 *
 * ``enabledOnly`` maps to the backend's ``?enabled_only=true`` query
 * parameter. The list endpoint returns installed packages only — for
 * the installable catalog, use ``listCatalog()``.
 */
export async function listProviders(
  enabledOnly: boolean = false,
): Promise<ProviderPackageEntry[]> {
  const suffix = enabledOnly ? "?enabled_only=true" : "";
  const data = await request<{
    status: string;
    providers: ProviderPackageEntry[];
  }>(`${suffix}`);

  return Array.isArray(data.providers) ? data.providers : [];
}

/**
 * Fetch a single provider package by its canonical ID.
 *
 * Set ``includeManifest = true`` to also receive the raw on-disk
 * ``manifest.json``. When the manifest file is missing or corrupt,
 * the response carries ``manifest: null`` and
 * ``manifest_unavailable_reason`` explaining why.
 */
export async function getProvider(
  providerId: string,
  includeManifest: boolean = false,
): Promise<ProviderDetailResponse> {
  const suffix = includeManifest ? "?include_manifest=true" : "";
  return request<ProviderDetailResponse>(
    `/${encodeURIComponent(providerId)}${suffix}`,
  );
}

/**
 * Delete a provider package and everything it owns on disk.
 */
export async function deleteProvider(
  providerId: string,
): Promise<{ status: string; provider_id: string; removed: boolean }> {
  return request(`/${encodeURIComponent(providerId)}`, {
    method: "DELETE",
  });
}

/**
 * Enable a previously disabled provider package.
 */
export async function enableProvider(
  providerId: string,
): Promise<ProviderPackageEntry> {
  const data = await request<{
    status: string;
    provider: ProviderPackageEntry;
  }>(`/${encodeURIComponent(providerId)}/enable`, { method: "POST" });

  if (!data.provider) {
    throw new ProviderApiRequestError(
      "Enable response was missing the provider object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.provider;
}

/**
 * Disable a provider package without uninstalling it.
 */
export async function disableProvider(
  providerId: string,
): Promise<ProviderPackageEntry> {
  const data = await request<{
    status: string;
    provider: ProviderPackageEntry;
  }>(`/${encodeURIComponent(providerId)}/disable`, { method: "POST" });

  if (!data.provider) {
    throw new ProviderApiRequestError(
      "Disable response was missing the provider object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.provider;
}

/**
 * Switch the active version to a previously installed one.
 */
export async function activateVersion(
  providerId: string,
  version: string,
): Promise<ProviderPackageEntry> {
  const data = await request<{
    status: string;
    provider: ProviderPackageEntry;
    active_version: string;
  }>(`/${encodeURIComponent(providerId)}/activate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version }),
  });

  if (!data.provider) {
    throw new ProviderApiRequestError(
      "Activate response was missing the provider object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.provider;
}

/**
 * Run filesystem health checks on a package's active version.
 *
 * Returns ``null`` when the package is not installed.
 */
export async function getProviderHealth(
  providerId: string,
): Promise<ProviderHealthReport | null> {
  try {
    const data = await request<{
      status: string;
      health: ProviderHealthReport;
    }>(`/${encodeURIComponent(providerId)}/health`);

    return data.health || null;
  } catch (err) {
    if (
      err instanceof ProviderApiRequestError &&
      err.code === "PROVIDER_PACKAGE_NOT_FOUND"
    ) {
      return null;
    }
    throw err;
  }
}


// ===========================================================================
// Upload + install
// ===========================================================================

/**
 * Upload a Provider Package ZIP and install it.
 */
export async function uploadProvider(
  file: File,
  installDependencies: boolean = true,
): Promise<ProviderInstallResult> {
  if (!file.name.toLowerCase().endsWith(".zip")) {
    throw new ProviderApiRequestError(
      "Provider Packages must be uploaded as .zip archives.",
      "PROVIDER_PACKAGE_WRONG_FILE_TYPE",
      { filename: file.name },
      400,
    );
  }

  const formData = new FormData();
  formData.append("file", file);
  formData.append(
    "install_dependencies",
    installDependencies ? "true" : "false",
  );

  const data = await request<{
    status: string;
    provider: ProviderInstallResult;
  }>("/upload", {
    method: "POST",
    body: formData,
  });

  if (!data.provider) {
    throw new ProviderApiRequestError(
      "Upload response was missing the provider object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.provider;
}


// ===========================================================================
// Catalog
// ===========================================================================

/**
 * Fetch every item in the merged Provider Package catalog.
 */
export async function listCatalog(): Promise<ProviderCatalogItem[]> {
  const data = await request<{
    status: string;
    catalog: ProviderCatalogItem[];
    total: number;
  }>("/catalog");

  return Array.isArray(data.catalog) ? data.catalog : [];
}

/**
 * Fetch a single catalog item by its ``id``.
 */
export async function getCatalogItem(
  itemId: string,
): Promise<ProviderCatalogItem> {
  const data = await request<{
    status: string;
    item: ProviderCatalogItem;
  }>(`/catalog/${encodeURIComponent(itemId)}`);

  if (!data.item) {
    throw new ProviderApiRequestError(
      "Catalog item response was missing the item object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.item;
}

/**
 * Reload the catalog from disk.
 */
export async function refreshCatalog(): Promise<{
  total: number;
  message: string;
}> {
  const data = await request<{
    status: string;
    total: number;
    message: string;
  }>("/catalog/refresh", { method: "POST" });

  return {
    total: data.total ?? 0,
    message: data.message ?? "Catalog reloaded.",
  };
}

/**
 * Download, verify, and install a catalog item.
 *
 * For ``compatible`` transports, the backend installs from the
 * catalog item's manifest directly (no ZIP, no download). For
 * ``native`` / ``custom``, the backend downloads the ZIP, verifies
 * its SHA-256, extracts it, and creates an isolated venv.
 */
export async function installFromCatalog(
  providerId: string,
): Promise<ProviderInstallResult> {
  const data = await request<{
    status: string;
    provider: ProviderInstallResult;
  }>(`/${encodeURIComponent(providerId)}/install-from-catalog`, {
    method: "POST",
  });

  if (!data.provider) {
    throw new ProviderApiRequestError(
      "Install-from-catalog response was missing the provider object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.provider;
}


// ===========================================================================
// Provider connections (M9/M10)
// ===========================================================================
//
// A Provider Package can have MULTIPLE saved connections. Exactly one
// is ``is_active`` — the runtime uses that one for dispatch. Every
// endpoint below is scoped to a specific package (``providerId``) and,
// where applicable, a specific connection (``connectionId``).

/**
 * List every saved connection for a package.
 *
 * Password and secret_file fields come back masked (``"••••••••"``)
 * — the caller must NOT attempt to use the masked value.
 */
export async function listConnections(
  providerId: string,
): Promise<ProviderConnection[]> {
  const data = await request<{
    status: string;
    provider_id: string;
    connections: ProviderConnection[];
    total: number;
  }>(`/${encodeURIComponent(providerId)}/connections`);

  return Array.isArray(data.connections) ? data.connections : [];
}

/**
 * Payload for ``createConnection`` and ``updateConnection``.
 *
 * ``fields`` is a free-form dict matching the package's
 * ``connection_fields`` schema. Secret fields submitted as the masked
 * placeholder are replaced on the backend with the stored value — so
 * a "save without editing the password" flow works correctly.
 */
export interface SaveConnectionPayload {
  name: string;
  fields: Record<string, unknown>;
  is_active?: boolean;
}

/**
 * Create a new connection for a package.
 */
export async function createConnection(
  providerId: string,
  payload: SaveConnectionPayload,
): Promise<ProviderConnection> {
  const data = await request<{
    status: string;
    connection: ProviderConnection;
  }>(`/${encodeURIComponent(providerId)}/connections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!data.connection) {
    throw new ProviderApiRequestError(
      "Create-connection response was missing the connection object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.connection;
}

/**
 * Update an existing connection.
 *
 * Any field omitted from ``payload.fields`` is preserved from the
 * current stored value.
 */
export async function updateConnection(
  providerId: string,
  connectionId: string,
  payload: SaveConnectionPayload,
): Promise<ProviderConnection> {
  const data = await request<{
    status: string;
    connection: ProviderConnection;
  }>(
    `/${encodeURIComponent(providerId)}/connections/${encodeURIComponent(connectionId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );

  if (!data.connection) {
    throw new ProviderApiRequestError(
      "Update-connection response was missing the connection object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.connection;
}

/**
 * Delete a connection.
 */
export async function deleteConnection(
  providerId: string,
  connectionId: string,
): Promise<{ status: string; connection_id: string; removed: boolean }> {
  return request(
    `/${encodeURIComponent(providerId)}/connections/${encodeURIComponent(connectionId)}`,
    { method: "DELETE" },
  );
}

/**
 * Enable a previously disabled connection.
 */
export async function enableConnection(
  providerId: string,
  connectionId: string,
): Promise<ProviderConnection> {
  const data = await request<{
    status: string;
    connection: ProviderConnection;
  }>(
    `/${encodeURIComponent(providerId)}/connections/${encodeURIComponent(connectionId)}/enable`,
    { method: "POST" },
  );

  if (!data.connection) {
    throw new ProviderApiRequestError(
      "Enable-connection response was missing the connection object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.connection;
}

/**
 * Disable a connection without deleting it.
 */
export async function disableConnection(
  providerId: string,
  connectionId: string,
): Promise<ProviderConnection> {
  const data = await request<{
    status: string;
    connection: ProviderConnection;
  }>(
    `/${encodeURIComponent(providerId)}/connections/${encodeURIComponent(connectionId)}/disable`,
    { method: "POST" },
  );

  if (!data.connection) {
    throw new ProviderApiRequestError(
      "Disable-connection response was missing the connection object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.connection;
}

/**
 * Mark this connection as the ACTIVE one for the package.
 *
 * Deactivates every sibling connection. The runtime will use this one
 * for future dispatch.
 */
export async function activateConnection(
  providerId: string,
  connectionId: string,
): Promise<ProviderConnection> {
  const data = await request<{
    status: string;
    connection: ProviderConnection;
  }>(
    `/${encodeURIComponent(providerId)}/connections/${encodeURIComponent(connectionId)}/activate`,
    { method: "POST" },
  );

  if (!data.connection) {
    throw new ProviderApiRequestError(
      "Activate-connection response was missing the connection object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.connection;
}

/**
 * Probe the connection by calling the provider's discovery endpoint.
 *
 * The backend replaces masked field values with the actual stored
 * values before the probe, so a test initiated from the UI (where the
 * field appears masked) still works correctly.
 */
export async function testConnection(
  providerId: string,
  connectionId: string,
): Promise<{
  ok: boolean;
  message: string;
  latency_ms: number;
  code?: string | null;
  status_code?: number | null;
}> {
  const data = await request<{
    status: string;
    ok: boolean;
    message: string;
    latency_ms: number;
    code?: string | null;
    status_code?: number | null;
  }>(
    `/${encodeURIComponent(providerId)}/connections/${encodeURIComponent(connectionId)}/test`,
    { method: "POST" },
  );

  return {
    ok: Boolean(data.ok),
    message: data.message || "",
    latency_ms: Number(data.latency_ms) || 0,
    code: data.code ?? null,
    status_code: data.status_code ?? null,
  };
}


// ===========================================================================
// TLS certificates (M9/M11)
// ===========================================================================
//
// A Provider Package can declare one or more TLS certificates it
// requires on the host (root CA bundles, mTLS client certificates).
// The Certificate Manager UI uses these endpoints to offer a
// one-click "Download & Install" flow.
//
// Installation happens in the MAIN backend process (not the worker
// subprocess) because the worker's network allowlist restricts
// outbound requests to the connection's ``base_url`` host. The CA
// host is different from the provider endpoint, so only the main
// process can reach it. See ``certificate_manager.py`` for details.

/**
 * List every certificate declared by a package's active manifest,
 * with its reconciled status.
 *
 * Status is reconciled live against the filesystem, so a user who
 * manually deleted the CA file will see ``missing`` here without
 * needing to click Test.
 *
 * The response contains one entry per declared certificate — even
 * those never installed (status ``pending``).
 */
export async function listCertificates(
  providerId: string,
): Promise<CertificateStatusReport[]> {
  const data = await request<{
    status: string;
    provider_id: string;
    certificates: CertificateStatusReport[];
    total: number;
  }>(`/${encodeURIComponent(providerId)}/certificates`);

  return Array.isArray(data.certificates) ? data.certificates : [];
}

/**
 * Options for ``installCertificate``.
 *
 * Both fields are optional — passing ``{}`` (or omitting the arg
 * entirely) uses the package's declared defaults.
 */
export interface InstallCertificateOptions {
  /**
   * Absolute path override for the installed file. When ``undefined``
   * or ``null``, the backend uses ``CertificateSpec.default_destination``
   * or a managed path under the provider's storage directory.
   */
  destination?: string | null;

  /**
   * When ``false``, an existing file at the destination causes a
   * conflict error instead of being overwritten. Default ``true``.
   */
  allow_overwrite?: boolean;
}

/**
 * Download, verify, and install a certificate.
 *
 * Returns the saved vault record plus a fresh status report. The
 * status report may be ``null`` if the post-install verification
 * failed (rare; a subsequent ``testCertificate`` call will retry).
 */
export async function installCertificate(
  providerId: string,
  certificateId: string,
  options?: InstallCertificateOptions,
): Promise<{
  record: ProviderCertificateRecord;
  statusReport: CertificateStatusReport | null;
}> {
  const body = {
    destination: options?.destination ?? null,
    allow_overwrite: options?.allow_overwrite ?? true,
  };

  const data = await request<{
    status: string;
    provider_id: string;
    certificate_id: string;
    record: ProviderCertificateRecord;
    status_report: CertificateStatusReport | null;
  }>(
    `/${encodeURIComponent(providerId)}/certificates/${encodeURIComponent(certificateId)}/install`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );

  if (!data.record) {
    throw new ProviderApiRequestError(
      "Install-certificate response was missing the record object.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }

  return {
    record: data.record,
    statusReport: data.status_report ?? null,
  };
}

/**
 * Re-check the status of an installed certificate without
 * re-downloading.
 *
 * Updates the vault record's status and ``last_checked_at`` fields,
 * then returns the full status report. Used by the UI's "Test"
 * button in the Certificate Manager modal.
 */
export async function testCertificate(
  providerId: string,
  certificateId: string,
): Promise<CertificateStatusReport> {
  const data = await request<{
    status: string;
    provider_id: string;
    certificate_id: string;
    status_report: CertificateStatusReport;
  }>(
    `/${encodeURIComponent(providerId)}/certificates/${encodeURIComponent(certificateId)}/test`,
    { method: "POST" },
  );

  if (!data.status_report) {
    throw new ProviderApiRequestError(
      "Test-certificate response was missing the status report.",
      "INVALID_RESPONSE",
      {},
      200,
    );
  }
  return data.status_report;
}

/**
 * Delete a certificate record and (optionally) its file from disk.
 *
 * Idempotent: deleting a certificate that was never installed returns
 * ``removed: false`` without error.
 */
export async function deleteCertificate(
  providerId: string,
  certificateId: string,
  deleteFile: boolean = true,
): Promise<{
  provider_id: string;
  certificate_id: string;
  removed: boolean;
}> {
  const suffix = deleteFile ? "" : "?delete_file=false";
  const data = await request<{
    status: string;
    provider_id: string;
    certificate_id: string;
    removed: boolean;
  }>(
    `/${encodeURIComponent(providerId)}/certificates/${encodeURIComponent(certificateId)}${suffix}`,
    { method: "DELETE" },
  );

  return {
    provider_id: data.provider_id ?? providerId,
    certificate_id: data.certificate_id ?? certificateId,
    removed: Boolean(data.removed),
  };
}


// ===========================================================================
// Human-readable error formatting
// ===========================================================================

/**
 * Format a ``ProviderApiRequestError`` (or any error) into a single
 * toast-friendly string that includes the structured error code when
 * available.
 *
 * Example outputs:
 *   - ``"Zip payload failed validation. [PROVIDER_PACKAGE_INVALID]"``
 *   - ``"Provider package 'acme' is not installed. [PROVIDER_PACKAGE_NOT_FOUND]"``
 *   - ``"Network request failed."``
 */
export function formatProviderError(err: unknown): string {
  if (err instanceof ProviderApiRequestError) {
    if (err.code && err.code !== "REQUEST_FAILED") {
      return `${err.message} [${err.code}]`;
    }
    return err.message;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return "Unexpected error.";
}
