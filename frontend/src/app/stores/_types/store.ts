// frontend/src/app/stores/_types/store.ts

import type { AgentUpsertPayload } from "@/lib/types";

/**
 * Filter modes for the Agents section.
 */
export type AgentFilter = "all" | "builtin" | "custom" | "active" | "disabled";

/**
 * Filter modes for the Tools section.
 */
export type ToolFilter = "all" | "builtin" | "custom";

/**
 * Result of an MCP extension connectivity test.
 */
export interface McpTestResult {
  ok: boolean;
  message: string;
  // Optional three-state classification used by the UI to pick the
  // badge color and label.
  //
  //   "success"  → green  "Connected"
  //   "failure"  → red    "Failed"
  //   "deferred" → amber  "OAuth server"
  //
  // "deferred" is set by the OAuth-server path in
  // useExtensionActions: verification requires a Bearer token that
  // only exists inside a chat session, so the Store UI cannot test
  // the connection. A green "Connected" badge would claim a success
  // that never happened; a red "Failed" badge would claim a failure
  // that also never happened. Amber honestly communicates "not
  // verified here — verify in chat".
  //
  // When omitted, the UI falls back to `ok` for backward
  // compatibility with any existing payloads.
  kind?: "success" | "failure" | "deferred";
}

/**
 * Map keyed by extension id, storing the last test result.
 */
export type McpTestResultMap = Record<string, McpTestResult>;

/**
 * Default payload used when creating a new agent via the builder modal.
 * Kept in a single place so both the page and the modal agree on defaults.
 */
export const DEFAULT_AGENT_FORM: AgentUpsertPayload = {
  name: "",
  role: "Specialist",
  icon: "Sparkles",
  core_color: "#10b981",
  description: "",
  system_prompt: "",
  tools: ["bash", "python_execute", "str_replace_editor", "web_search"],
  // Always initialise ``mcp_servers`` to an empty array — never
  // ``undefined`` — so a create/update that intentionally leaves
  // MCP servers unselected sends ``[]`` rather than omitting the
  // field. The backend treats both identically today, but this
  // keeps the form payload shape stable for future validation.
  mcp_servers: [],
  max_steps: 30,
};

/**
 * Upload category used by the unified upload modal.
 * - "tool"      → custom Python tool (.py) or JSON manifest
 * - "extension" → MCP server manifest (.json)
 */
export type UploadCategory = "tool" | "extension";

/**
 * Metadata for a single tool as seen by the Agent Tools modal.
 *
 * ``in_manifest`` marks tools that already appear in the agent's
 * baseline manifest (shown in the "Manifest Tools" section). Tools
 * with ``in_manifest = false`` are eligible for the "Extra Tools"
 * section.
 */
export interface AgentToolInfo {
  id: string;
  name: string;
  description: string;
  category: string;
  is_builtin: boolean;
  safety_level?: string | null;
  in_manifest: boolean;
}

/**
 * Full tool state for an agent, returned by ``GET /agents/{id}/tools``.
 *
 * The three user-facing lists follow the model:
 *   - ``manifest_tools``: developer-authored baseline (never mutated)
 *   - ``disabled_tools``: subset of manifest_tools to exclude at runtime
 *   - ``extra_tools``:    additions from the Store catalog
 *   - ``effective_tools``: what the runtime will actually use
 *
 * ``tools_editable`` reflects ``settings.tools_editable`` in the manifest
 * (locked agents cannot open the tools modal).
 */
export interface AgentToolsState {
  manifest_tools: string[];
  disabled_tools: string[];
  extra_tools: string[];
  effective_tools: string[];
  tools_editable: boolean;
  available_tools: AgentToolInfo[];
}


// ─────────────────────────────────────────────────────────────────
// Skills
// ─────────────────────────────────────────────────────────────────
//
// Types mirror the HTTP responses from ``omweb.routers.skills``:
//
//   GET  /api/skills                      → SkillSummary[]  (list)
//   GET  /api/skills/{id}                 → SkillDetail     (single)
//   POST /api/skills/install              → SkillInstallResponse
//   POST /api/skills/{id}/toggle          → SkillToggleResponse
//   DELETE /api/skills/{id}               → { deleted: true }
//   GET  /api/skills/{id}/preview         → SkillPreview
//
// These are duplicated locally rather than fetched from a shared
// module because the store page is the only consumer. If a second
// consumer appears (e.g. a mobile client), extract to ``@/lib/types``.

/**
 * Filter modes for the Skills section.
 *
 *   "all"      → every installed skill
 *   "builtin"  → shipped skills (``is_builtin: true``)
 *   "custom"   → user-installed skills (``is_builtin: false``)
 */
export type SkillFilter = "all" | "builtin" | "custom";

/**
 * Author block as it appears in a skill manifest. All fields
 * optional — a skill may be installed by a user who never filled
 * them in.
 */
export interface SkillAuthor {
  name?: string;
  url?: string;
  email?: string;
}

/**
 * Summary of one skill as returned by the list endpoint.
 *
 * Deliberately excludes the full ``manifest`` and ``frontmatter``
 * (which can each be several KB) and the sanitized ``icon_svg``
 * (up to 32 KB). Those are fetched on demand via the detail
 * endpoint and the icon endpoint respectively.
 */
export interface SkillSummary {
  id: string;
  version: string;
  name: string;
  display_name: string;
  description: string;
  short_description: string;
  status: string;
  enabled: boolean;
  is_builtin: boolean;
  is_deprecated: boolean;
  chat_invocable: boolean;
  agent_assignable: boolean;
  workflow_node: boolean;
  has_icon_svg: boolean;
  has_icon_path: boolean;
  icon_path: string | null;
  tags: string[];
  author: SkillAuthor;
  publisher_id: string | null;
  trust: string;
  license: string | null;
  homepage: string | null;
  docs_url: string | null;
  release_date: string | null;
  stability: string;
  capabilities: Record<string, unknown>;
  execution: Record<string, unknown>;
  package_dir: string;
}

/**
 * Full skill detail — includes the raw manifest and SKILL.md frontmatter.
 */
export interface SkillDetail extends SkillSummary {
  manifest: Record<string, unknown>;
  frontmatter: Record<string, unknown>;
}

/**
 * Response from ``POST /api/skills/install``.
 */
export interface SkillInstallResponse {
  status: string;
  skill: SkillSummary;
  was_upgrade: boolean;
  warnings: string[];
}

/**
 * Response from ``POST /api/skills/{id}/toggle``.
 */
export interface SkillToggleResponse {
  status: string;
  skill: SkillSummary;
}

/**
 * Response from ``GET /api/skills/{id}/preview`` — the full
 * instructions text for the details drawer.
 */
export interface SkillPreview {
  status: string;
  skill_id: string;
  version: string;
  instructions: string;
  instructions_length: number;
  frontmatter: Record<string, unknown>;
}


// ─────────────────────────────────────────────────────────────────
// Provider Packages (M9 / M10)
// ─────────────────────────────────────────────────────────────────
//
// Types mirror the HTTP responses from ``omweb.routers.store.providers``:
//
//   GET    /api/store/providers                       → ProviderListResponse
//   GET    /api/store/providers/catalog               → ProviderCatalogResponse
//   GET    /api/store/providers/catalog/{id}          → ProviderCatalogItemResponse
//   POST   /api/store/providers/catalog/refresh       → { total, message }
//   POST   /api/store/providers/upload                → ProviderInstallResponse
//   POST   /api/store/providers/{id}/install-from-catalog → ProviderInstallResponse
//   GET    /api/store/providers/{id}                  → ProviderDetailResponse
//   GET    /api/store/providers/{id}?include_manifest → + manifest
//   DELETE /api/store/providers/{id}                  → { removed: true }
//   POST   /api/store/providers/{id}/enable           → { provider }
//   POST   /api/store/providers/{id}/disable          → { provider }
//   POST   /api/store/providers/{id}/activate         → { provider, active_version }
//   GET    /api/store/providers/{id}/health           → { health }
//
// These are declared here (rather than fetched from a shared module)
// because the store page is the only consumer. If a second consumer
// appears, extract to ``@/lib/types``.

/**
 * Filter modes for the Providers section.
 *
 *   "all"        → every installed provider package
 *   "enabled"    → enabled only
 *   "disabled"   → disabled only
 *   "native"     → native transport (ships Python code)
 *   "compatible" → compatible transport (reuses a built-in adapter)
 *   "custom"     → custom transport (bespoke protocol, ships code)
 */
export type ProviderFilter =
  | "all"
  | "enabled"
  | "disabled"
  | "native"
  | "compatible"
  | "custom";

/**
 * Filter modes for the "Connections" tab inside the Configure modal.
 *
 *   "all"      → every connection for the package
 *   "active"   → only the currently-active connection
 *   "enabled"  → only enabled connections
 *   "disabled" → only disabled connections
 *
 * A package can have multiple saved connections but only ONE is
 * ``is_active`` at a time. The "active" filter makes that one
 * trivially discoverable even when the list is long.
 */
export type ProviderConnectionFilter =
  | "all"
  | "active"
  | "enabled"
  | "disabled";

/**
 * Transport a provider package uses to talk to its endpoint.
 *
 *   - "native"     → ships its own SDK / REST client (OpenAI, Anthropic,
 *                    Google, Ollama, LM Studio style).
 *   - "compatible" → reuses an existing built-in adapter by declaring
 *                    a ``base_adapter``; NO code shipped.
 *   - "custom"     → a bespoke protocol that does not fit either shape;
 *                    ships code like ``native`` but tagged differently
 *                    for the UI.
 */
export type ProviderTransport = "native" | "compatible" | "custom";

/**
 * One installed version of a provider package.
 *
 * ``hash`` is the SHA-256 of the original uploaded ZIP, computed at
 * install time. It is the idempotency key for re-installs and the
 * version directory name.
 *
 * ``entry_point`` and ``class_name`` are ``null`` for compatible
 * transports (they ship no code and reuse a built-in adapter).
 */
export interface ProviderPackageVersionRecord {
  version: string;
  hash: string;
  installed_at: string;
  size_bytes: number;
  file_count: number;
  entry_point: string | null;
  class_name: string | null;
}

/**
 * One installed provider package.
 *
 * Returned by ``GET /api/store/providers`` (list) and
 * ``GET /api/store/providers/{id}`` (single). Carries everything the
 * card and details drawer need without re-reading the on-disk
 * manifest.
 */
export interface ProviderPackageEntry {
  id: string;
  name: string;
  display_name: string | null;
  description: string;

  active_version: string;
  active_hash: string;
  enabled: boolean;

  installed_versions: ProviderPackageVersionRecord[];

  transport: ProviderTransport;
  api_contract: string;

  features: Record<string, boolean>;
  permissions: Record<string, unknown>;

  tags: string[];
  author: string | null;
  author_url?: string | null;
  homepage: string | null;

  /**
   * How this package arrived: ``"uploaded"`` (manual ZIP) or
   * ``"catalog"`` (installed from the store catalog).
   */
  source: string;

  /**
   * Connection fields the package's manifest declares. Each field
   * drives an input in the Store's Configure modal.
   *
   * Empty array = default two fields only (``base_url``, ``api_key``).
   * Non-empty = those two PLUS every field declared here.
   */
  connection_fields?: ConnectionFieldSpec[];

  /**
   * Non-functional hints for the Store UI (TLS warning, docs link,
   * custom headers). Do NOT affect adapter behavior — the adapter's
   * code is the source of truth.
   */
  runtime_hints?: RuntimeHints;

  /**
   * Whether the package declares any TLS certificates it needs
   * installed. Computed by the backend by reading the manifest.
   *
   *   - ``true``  — the package declares at least one certificate.
   *   - ``false`` — the package declares no certificates.
   *   - ``null``  — the backend could not read the manifest (degraded).
   *
   * Only used to decide whether to show the certificate badge on the
   * card. The full certificate list is fetched on demand from
   * ``GET /{provider_id}/certificates``.
   */
  has_certificates?: boolean | null;

  /**
   * Aggregate status of the package's required certificates.
   *
   * Populated by the backend on list/detail responses by reading the
   * manifest and the vault. The card uses this to render:
   *   - green badge "Certificates installed"
   *   - amber badge "Certificates missing"
   *   - no badge when ``none_required`` or ``unknown``.
   */
  certificates_status?: CertificatesStatus;

  /**
   * Whether the package currently has an ACTIVE connection (is_active
   * AND enabled). Populated by the API on list/detail responses —
   * not by the manifest.
   */
  has_active_connection?: boolean;

  /**
   * The name of the active connection (e.g. "Personal"), or ``null``.
   * Populated by the API.
   */
  active_connection_name?: string | null;

  /**
   * UUID of the active connection, or ``null``.
   */
  active_connection_id?: string | null;
}

// ---------------------------------------------------------------------------
// Connection fields + runtime hints (from the package's manifest)
// ---------------------------------------------------------------------------

/**
 * One user-facing field declared by a Provider Package.
 *
 * Mirrors ``ConnectionFieldSpec`` on the Python side. The ``type``
 * drives the HTML input kind in the Store's Configure modal:
 *
 *   - ``string``        → <input type="text">
 *   - ``password``      → <input type="password"> (masked in API)
 *   - ``url``           → <input type="url">
 *   - ``number``        → <input type="number">
 *   - ``boolean``       → <input type="checkbox">
 *   - ``select``        → <select> with ``options``
 *   - ``textarea``      → <textarea>
 *   - ``file``          → <input type="file"> (path stored, not contents)
 *   - ``secret_file``   → like ``file`` but value is masked in API
 *
 * The two conventional fields ``base_url`` and ``api_key`` are always
 * present even when the manifest does not declare them.
 */
export interface ConnectionFieldSpec {
  /** The key persisted in models_vault.json. */
  key: string;
  /** The label shown in the Configure modal. */
  label: string;
  type:
    | "string"
    | "password"
    | "url"
    | "number"
    | "boolean"
    | "select"
    | "textarea"
    | "file"
    | "secret_file";
  required?: boolean;
  default?: unknown;
  placeholder?: string | null;
  description?: string | null;
  /** For ``type === "select"``. */
  options?: Array<{ value: string; label: string }> | null;
  /** Conditional visibility: shown only when the referenced field(s) match. */
  visible_if?: Record<string, unknown> | null;
  /** For ``type`` in ``{"file", "secret_file"}`` — comma-separated extensions. */
  accept?: string | null;
  min_length?: number | null;
  max_length?: number | null;
  pattern?: string | null;
}

/**
 * Non-functional hints declared by a package's manifest.
 *
 * The adapter's code is the source of truth for actually ENFORCING
 * these — the hints only alert the user before they save a connection.
 */
export interface RuntimeHints {
  requires_custom_tls?: boolean;
  requires_mtls?: boolean;
  requires_token_refresh?: boolean;
  token_lifetime_seconds?: number | null;
  supports_env_vars?: boolean;
  docs_url?: string | null;
  custom_headers_from_fields?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Certificates (TLS certificates, CA bundles, mTLS keys)
// ---------------------------------------------------------------------------
//
// Some providers require a TLS certificate (root CA bundle, mTLS
// client certificate) that is NOT part of the standard trust store
// shipped with Python (certifi). The most prominent example is the
// Russian Trusted Root CA used by GigaChat and YandexGPT.
//
// A Provider Package declares the certificates it needs in its
// manifest. The Certificate Manager UI offers a one-click "Download &
// Install" flow, removing the need for the user to manually download
// the file and paste a path into a form.
//
// Certificates are stored on the filesystem and tracked in the vault
// (``provider_certificates``). The card badge shows whether all
// required certificates are installed.

/**
 * One certificate declaration from a package's manifest.
 *
 * Mirrors ``CertificateSpec`` on the Python side.
 */
export interface CertificateSpec {
  /** Stable identifier within the package (e.g. "russian_trusted_root_ca"). */
  id: string;
  /** User-facing label (e.g. "Russian Trusted Root CA"). */
  display_name: string;
  /** One-line explanation shown under the label. */
  description: string;
  /** HTTPS URL the Certificate Manager downloads from. */
  download_url: string;
  /** Optional hex-encoded SHA-256. Verified after download when present. */
  download_sha256?: string | null;
  /** Suggested filename for the installed file. */
  download_filename: string;
  /** Absolute path override; when null, a managed path is used. */
  default_destination?: string | null;
  /** What kind of TLS artifact this is. */
  certificate_type: "root_ca" | "bundle" | "client_cert" | "client_key";
  /** When true, the package will not work without this certificate. */
  required?: boolean;
  /** Optional list of connection field keys this certificate supports. */
  required_for_fields?: string[];
  /** Optional docs link explaining why the certificate is needed. */
  docs_url?: string | null;
  /** Optional human-readable issuer. */
  issuer?: string | null;
  /** Optional ISO-8601 expiry date. */
  expires_at?: string | null;
}

/**
 * Certificate status values.
 *
 *   - "installed" — file present, hash matches (or no hash was declared).
 *   - "missing"   — record exists but file is absent.
 *   - "invalid"   — file present but hash or format mismatch.
 *   - "expired"   — reserved for future use.
 *   - "pending"   — never attempted (no vault record, no file).
 */
export type CertificateStatus =
  | "installed"
  | "missing"
  | "invalid"
  | "expired"
  | "pending";

/**
 * One installed certificate record (persisted in the vault).
 *
 * Mirrors ``ProviderCertificateRecord`` on the Python side.
 */
export interface ProviderCertificateRecord {
  /** Composite key: "<provider_id>:<certificate_id>". */
  id: string;
  provider_package_id: string;
  certificate_id: string;
  /** Absolute path to the installed file, or null on failure. */
  installed_path?: string | null;
  /** SHA-256 of the file at install / last check. */
  sha256?: string | null;
  size_bytes: number;
  status: CertificateStatus;
  installed_at?: string | null;
  last_checked_at?: string | null;
  error_message?: string | null;
}

/**
 * Full status report returned by the certificates endpoint.
 *
 * Carries both the expected facts (from the manifest) and the actual
 * facts (from the filesystem). The UI uses ``status`` for the badge,
 * and the rest for the modal's detail rows.
 *
 * Mirrors ``CertificateStatusReport`` on the Python side.
 */
export interface CertificateStatusReport {
  provider_id: string;
  certificate_id: string;
  display_name: string;
  status: CertificateStatus;
  installed_path?: string | null;
  expected_sha256?: string | null;
  actual_sha256?: string | null;
  size_bytes: number;
  installed_at?: string | null;
  last_checked_at?: string | null;
  error_message?: string | null;
  download_url?: string | null;
  docs_url?: string | null;
  required: boolean;
}

/**
 * Aggregate certificate status for the card badge.
 *
 *   - "none_required"  — the package declares no certificates.
 *   - "all_installed"  — every required certificate is installed.
 *   - "some_missing"   — at least one required certificate is missing.
 *   - "unknown"        — backend could not compute (rare; treat as
 *                        neutral, no badge).
 */
export type CertificatesStatus =
  | "none_required"
  | "all_installed"
  | "some_missing"
  | "unknown";

/**
 * One saved connection for a Provider Package.
 *
 * A package can have MULTIPLE connections (personal, work, sandbox,
 * ...). Exactly one per package is marked ``is_active`` — that is the
 * one the runtime uses for dispatch.
 *
 * Fields follow the package's ``connection_fields`` schema. Secret
 * fields (``type`` in ``{"password", "secret_file"}``) are returned
 * from the API as ``"••••••••"`` — the caller MUST NOT attempt to use
 * the masked value.
 */
export interface ProviderConnection {
  /** Stable UUID, e.g. ``"conn_abc123..."``. */
  id: string;
  /** The package this connection belongs to (``manifest.id``). */
  provider_package_id: string;
  /** User-facing label (e.g. "Personal Account"). */
  name: string;
  /** Whether this connection is enabled. */
  enabled: boolean;
  /** Whether this is the ACTIVE connection for the package. */
  is_active: boolean;
  /** Field values keyed by ``ConnectionFieldSpec.key``. */
  fields: Record<string, unknown>;
  /** ISO timestamp. */
  created_at?: string;
  /** ISO timestamp. */
  updated_at?: string;
}

/**
 * One catalog item.
 *
 * Returned by ``GET /api/store/providers/catalog``. The
 * ``installable`` / ``installability_reason`` fields are computed
 * by the backend; ``download_url`` and ``checksum_sha256`` are both
 * required for an item to be installable (preview-only otherwise).
 */
export interface ProviderCatalogItem {
  id: string;
  name: string;
  display_name: string | null;
  description: string;
  version: string;
  transport: ProviderTransport;

  features: Record<string, boolean>;
  permissions: Record<string, unknown>;

  download_url: string | null;
  checksum_sha256: string | null;

  author: string | null;
  author_url?: string | null;
  homepage: string | null;
  tags: string[];

  /**
   * Connection fields the package will require once installed.
   * Useful for preview-only items so the UI can show what setup will
   * be needed before the user clicks Install.
   */
  connection_fields?: ConnectionFieldSpec[];

  /**
   * Certificates the package will require once installed. Surfaced in
   * the catalog preview so the user knows the setup cost BEFORE
   * clicking Install. Mirrors ``ProviderManifest.certificates``.
   */
  certificates?: CertificateSpec[];

  /**
   * Non-functional hints for the Store UI.
   */
  runtime_hints?: RuntimeHints;

  /**
   * Computed by the backend: true iff both ``download_url`` and
   * ``checksum_sha256`` are present and the checksum is a valid
   * SHA-256 hex digest. For ``compatible`` transports the backend
   * always returns ``true`` — they ship no code and no download.
   */
  installable: boolean;

  /**
   * Human-readable explanation of ``installable``. Designed to be
   * rendered verbatim in a badge tooltip.
   */
  installability_reason: string;

  /**
   * Human-readable warnings for the install confirmation dialog
   * (e.g. "May make outbound HTTP requests to the connection's
   * configured endpoint."). Empty when the item declares no
   * sensitive permissions.
   */
  permission_warnings: string[];
}

/**
 * One step in the install pipeline, as reported by the backend.
 *
 * ``status`` is one of ``"ok"`` / ``"failed"`` / ``"skipped"``.
 */
export interface ProviderInstallStep {
  name: string;
  status: "ok" | "failed" | "skipped";
  duration_ms: number;
  detail: string;
}

/**
 * Full install report — step timings, warnings, and the venv path
 * (``null`` for compatible transports, which create no venv).
 */
export interface ProviderInstallReport {
  dependencies_installed: boolean;
  dependencies_skipped: boolean;
  venv_path: string | null;
  duration_ms: number;
  steps: ProviderInstallStep[];
  warnings: string[];
}

/**
 * Response from ``POST /api/store/providers/upload`` and
 * ``POST /api/store/providers/{id}/install-from-catalog``.
 */
export interface ProviderInstallResult {
  id: string;
  version: string;
  hash: string;
  name: string;
  display_name: string | null;
  description: string;
  size_bytes: number;
  file_count: number;
  transport: ProviderTransport;
  features: Record<string, boolean>;
  permissions: Record<string, unknown>;
  install_report: ProviderInstallReport;
  warnings: string[];
}

/**
 * One filesystem or runtime check performed by the health endpoint.
 */
export interface ProviderHealthCheck {
  name: string;
  passed: boolean;
  message: string;
}

/**
 * Health report for a single provider package.
 *
 * ``status`` is ``"healthy"`` (all checks pass), ``"degraded"``
 * (some pass), or ``"broken"`` (none pass). The endpoint returns
 * ``null`` when the package is not installed.
 */
export interface ProviderHealthReport {
  id: string;
  version: string;
  hash: string;
  status: "healthy" | "degraded" | "broken";
  checks: ProviderHealthCheck[];
}

/**
 * Full response body of ``GET /api/store/providers/{id}?include_manifest=true``.
 *
 * ``manifest`` is ``null`` when the on-disk file is missing or
 * corrupt; ``manifest_unavailable_reason`` explains why.
 */
export interface ProviderDetailResponse {
  status: string;
  provider: ProviderPackageEntry;
  manifest?: Record<string, unknown> | null;
  manifest_unavailable_reason?: string;
}

/**
 * Structured error returned by every provider endpoint on failure.
 *
 * ``code`` is a stable string (e.g. ``PROVIDER_PACKAGE_INVALID``,
 * ``PROVIDER_PACKAGE_NOT_FOUND``) for programmatic handling;
 * ``details`` carries field-level context (e.g. which manifest
 * field failed validation).
 */
export interface ProviderApiError {
  error: string;
  code: string;
  details: Record<string, unknown>;
}
