/**
 * frontend/src/lib/bundleApi.ts
 *
 * Typed API client for the Tool Bundle subsystem.
 *
 * Mirrors the backend Pydantic schemas so responses are strongly typed
 * end-to-end. Upload uses XHR because `fetch` cannot report upload
 * progress; all other calls use `fetch` with JSON.
 */

// ---------------------------------------------------------------------------
// Types (mirror of backend schema)
// ---------------------------------------------------------------------------

export interface InstallStep {
  name: string;
  status: "ok" | "skipped" | "failed";
  duration_ms: number;
  detail: string;
}

export interface InstallReport {
  requirements_installed: boolean;
  requirements_skipped: boolean;
  duration_ms: number;
  steps: InstallStep[];
  warnings: string[];
}

export interface BundleInstallResult {
  id: string;
  version: string;
  hash: string;
  name: string;
  description: string;
  size_bytes: number;
  file_count: number;
  capabilities: string[];
  tags: string[];
  install_report: InstallReport;
  warnings: string[];
}

export interface BundleVersionRecord {
  version: string;
  hash: string;
  installed_at: string;
  size_bytes: number;
  file_count: number;
  entry_point: string;
  class_name: string | null;
}

export interface BundleEntry {
  id: string;
  name: string;
  description: string;
  active_version: string;
  active_hash: string;
  installed_versions: BundleVersionRecord[];
  status: "active" | "disabled" | "broken";
  safety_level: string;
  capabilities: string[];
  tags: string[];
  author: string | null;
  homepage: string | null;
}

export interface HealthCheck {
  name: string;
  passed: boolean;
  message: string;
}

export interface BundleHealthReport {
  id: string;
  version: string;
  hash: string;
  status: "healthy" | "degraded" | "broken";
  checks: HealthCheck[];
  checked_at: string;
}

// ---------------------------------------------------------------------------
// Base URL resolution
// ---------------------------------------------------------------------------

function getApiUrl(endpoint: string): string {
  // Relative path only - SSR/CSR parity. See next.config.mjs.
  return endpoint;
}

// ---------------------------------------------------------------------------
// Upload with progress
// ---------------------------------------------------------------------------

export function uploadBundle(
  file: File,
  options: { installRequirements: boolean } = { installRequirements: true },
  onProgress?: (percent: number) => void
): Promise<BundleInstallResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const url = getApiUrl("/api/store/tools/upload-bundle");

    const formData = new FormData();
    formData.append("file", file);
    formData.append("install_requirements", String(options.installRequirements));

    xhr.upload.onprogress = (e: ProgressEvent) => {
      if (e.lengthComputable && onProgress) {
        const pct = Math.round((e.loaded / e.total) * 100);
        onProgress(pct);
      }
    };

    xhr.onload = () => {
      let payload: any = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        // non-JSON response
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        if (payload && payload.bundle) {
          resolve(payload.bundle as BundleInstallResult);
        } else {
          reject(new Error("Malformed server response (no 'bundle' field)."));
        }
        return;
      }

      const detail =
        (payload && payload.detail && (payload.detail.error || payload.detail)) ||
        (payload && payload.message) ||
        `HTTP ${xhr.status}`;

      const code = payload?.detail?.code ? ` [${payload.detail.code}]` : "";
      reject(new Error(`${detail}${code}`));
    };

    xhr.onerror = () => reject(new Error("Network error during upload."));
    xhr.onabort = () => reject(new Error("Upload aborted."));

    xhr.open("POST", url);
    xhr.send(formData);
  });
}

// ---------------------------------------------------------------------------
// Standard JSON endpoints
// ---------------------------------------------------------------------------

async function jsonRequest<T>(
  method: string,
  endpoint: string,
  body?: unknown
): Promise<T> {
  const url = getApiUrl(endpoint);
  const init: RequestInit = {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  };

  const res = await fetch(url, init);
  const payload = await res.json().catch(() => ({}));

  if (!res.ok) {
    const detail =
      (payload && payload.detail && (payload.detail.error || payload.detail)) ||
      (payload && payload.message) ||
      `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return payload as T;
}

export async function listBundles(): Promise<BundleEntry[]> {
  const payload = await jsonRequest<{ bundles: BundleEntry[]; total: number }>(
    "GET",
    "/api/store/tools/bundles"
  );
  return payload.bundles || [];
}

export async function getBundle(bundleId: string): Promise<BundleEntry> {
  const payload = await jsonRequest<{ bundle: BundleEntry }>(
    "GET",
    `/api/store/tools/bundles/${encodeURIComponent(bundleId)}`
  );
  return payload.bundle;
}

export async function deleteBundle(
  bundleId: string,
  force: boolean = false
): Promise<void> {
  const suffix = force ? "?force=true" : "";
  await jsonRequest<unknown>(
    "DELETE",
    `/api/store/tools/bundles/${encodeURIComponent(bundleId)}${suffix}`
  );
}

export async function activateVersion(
  bundleId: string,
  version: string
): Promise<void> {
  await jsonRequest<unknown>(
    "POST",
    `/api/store/tools/bundles/${encodeURIComponent(bundleId)}/activate`,
    { version }
  );
}

export async function checkBundleHealth(
  bundleId: string
): Promise<BundleHealthReport> {
  const payload = await jsonRequest<{ health: BundleHealthReport }>(
    "GET",
    `/api/store/tools/bundles/${encodeURIComponent(bundleId)}/health`
  );
  return payload.health;
}
