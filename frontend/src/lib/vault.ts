// frontend/src/lib/vault.ts

"use client";

export interface VaultData {
  cloud_vault: any[];
  custom_endpoints: any[];
  lmstudio_vault: {
    baseUrl: string;
    model: string;
    apiKey: string;
    savedModels?: string[];
    [key: string]: any;
  };
  ollama_vault: {
    baseUrl: string;
    model: string;
    apiKey?: string;
    savedModels?: string[];
    [key: string]: any;
  };
  scanned_models: string[];
  /**
   * Store → Providers connection records. Managed exclusively by the
   * backend and by `useProviderPackages.saveModelsForPackage()` (which
   * POSTs directly to `/api/config/vault`).
   *
   * CRITICAL: The frontend settings page MUST NEVER include this key in
   * a `saveVaultToBackend()` payload. Doing so would overwrite the
   * backend's live value with a stale in-memory snapshot from
   * `memoryVault` — which is exactly the bug that caused Package
   * model selections to be silently erased whenever the user clicked
   * "Save Config" in the sidebar.
   */
  provider_connections?: any[];
  /**
   * Per-package model selections (Store → Providers). Same lifecycle
   * and same "never send from frontend" rule as `provider_connections`.
   */
  provider_package_models?: Record<
    string,
    { savedModels?: string[]; model?: string }
  >;
}

/**
 * Bootstrap values used only when:
 *   - The backend is unreachable on first page load, OR
 *   - Server-side rendering needs a placeholder snapshot.
 *
 * Once the backend is reachable, its response (which reflects
 * backend/data/models_vault.json) fully replaces these values.
 *
 * `DEFAULT_VAULT` is intentionally populated with the same default
 * LM Studio model that has historically shipped with Peldrun, so
 * that a first-run user with a fresh install sees a working default
 * before ever visiting the settings page.
 */
export const DEFAULT_VAULT: VaultData = {
  cloud_vault: [],
  custom_endpoints: [],
  lmstudio_vault: {
    baseUrl: "http://127.0.0.1:1234/v1",
    model: "qwen3-vl-8b-instruct",
    apiKey: "",
    savedModels: ["qwen3-vl-8b-instruct"],
  },
  ollama_vault: {
    baseUrl: "http://127.0.0.1:11434/v1",
    model: "",
    apiKey: "",
    savedModels: [],
  },
  scanned_models: [],
};

let memoryVault: VaultData | null = null;
let syncTimeout: any = null;
let isSyncingFromBackend = false;

/**
 * Candidate endpoints for vault fetch/save requests.
 *
 * A single relative path is returned because Next.js rewrites
 * (configured at build time via PELDRUN_BACKEND_URL in
 * next.config.mjs) route /api/config/vault to the active backend
 * on every environment. The previous implementation appended
 * `http://localhost:8088/...` candidates to compensate for a
 * missing rewrite, but that bypassed the configured backend.
 *
 * The function is kept (rather than inlining the constant) because
 * fetchVault and saveVaultToBackend both iterate over its result
 * as a list, and future environments may legitimately add probe
 * candidates (e.g. an API gateway prefix).
 */
function getCandidateEndpoints(): string[] {
  return ["/api/config/vault"];
}

export async function fetchVault(): Promise<VaultData> {
  if (typeof window === "undefined") {
    return DEFAULT_VAULT;
  }

  const endpoints = getCandidateEndpoints();
  for (const url of endpoints) {
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (res.ok) {
        const data: VaultData = await res.json();

        let needsMigration = false;
        const legacyCloud = localStorage.getItem("omweb_cloud_vault");
        const legacyCustom = localStorage.getItem("omweb_custom_endpoints");
        const legacyLM = localStorage.getItem("omweb_lmstudio_vault");
        const legacyOllama = localStorage.getItem("omweb_ollama_vault");

        if (
          (!data.cloud_vault || data.cloud_vault.length === 0) &&
          legacyCloud
        ) {
          try {
            const parsed = JSON.parse(legacyCloud);
            if (Array.isArray(parsed) && parsed.length > 0) {
              data.cloud_vault = parsed;
              needsMigration = true;
            }
          } catch {}
        }

        if (
          (!data.custom_endpoints || data.custom_endpoints.length === 0) &&
          legacyCustom
        ) {
          try {
            const parsed = JSON.parse(legacyCustom);
            if (Array.isArray(parsed) && parsed.length > 0) {
              data.custom_endpoints = parsed;
              needsMigration = true;
            }
          } catch {}
        }

        if (legacyLM) {
          try {
            const parsed = JSON.parse(legacyLM);
            if (
              parsed &&
              typeof parsed === "object" &&
              parsed.apiKey &&
              !data.lmstudio_vault?.apiKey
            ) {
              data.lmstudio_vault = { ...data.lmstudio_vault, ...parsed };
              needsMigration = true;
            }
          } catch {}
        }

        if (legacyOllama) {
          try {
            const parsed = JSON.parse(legacyOllama);
            if (
              parsed &&
              typeof parsed === "object" &&
              parsed.savedModels?.length > 0 &&
              (!data.ollama_vault?.savedModels ||
                data.ollama_vault.savedModels.length === 0)
            ) {
              data.ollama_vault = { ...data.ollama_vault, ...parsed };
              needsMigration = true;
            }
          } catch {}
        }

        if (needsMigration) {
          await saveVaultToBackend(data);
        }

        memoryVault = data;
        syncLocalStorage(data);
        window.dispatchEvent(
          new CustomEvent("omweb:vault-updated", { detail: data })
        );
        return data;
      }
    } catch {}
  }

  return getLocalVaultFallback();
}

export async function saveVaultToBackend(
  partial: Partial<VaultData>
): Promise<VaultData> {
  const current = memoryVault || getLocalVaultFallback();
  const merged: VaultData = {
    ...current,
    ...partial,
  };

  // SAFETY GUARD — prevent accidental overwrite of the backend vault.
  //
  // Root cause of the earlier data-loss incident: the frontend
  // bootstrap (`DEFAULT_VAULT`) was temporarily emptied, and the
  // auto-sync pipeline pushed it to `POST /api/config/vault`, which
  // replaces each top-level key wholesale. The result was an empty
  // `backend/data/models_vault.json` with no recovery path.
  //
  // This guard blocks any POST whose payload has zero models across
  // every source (LM Studio, Ollama, cloud, custom endpoints,
  // scanned models, provider connections, provider package models).
  // Callers that legitimately want to clear the vault must set
  // `__allowEmptyVault: true` on the partial payload, which serves as
  // an explicit, auditable opt-out.
  //
  // The `provider_*` fields are included in the check so that a vault
  // which holds ONLY provider packages (no cloud, no local, no custom)
  // is correctly recognized as non-empty — otherwise a legitimate
  // write would be blocked by a false positive.
  const hasAnyContent =
    (merged.lmstudio_vault?.savedModels?.length ?? 0) > 0 ||
    (merged.ollama_vault?.savedModels?.length ?? 0) > 0 ||
    (merged.cloud_vault?.length ?? 0) > 0 ||
    (merged.custom_endpoints?.length ?? 0) > 0 ||
    (merged.scanned_models?.length ?? 0) > 0 ||
    (merged.provider_connections?.length ?? 0) > 0 ||
    Object.keys(merged.provider_package_models ?? {}).length > 0;

  if (!hasAnyContent && !(partial as any).__allowEmptyVault) {
    console.warn(
      "[vault] Blocked a POST that would have overwritten " +
        "backend/data/models_vault.json with an empty payload. " +
        "To clear the vault intentionally, pass " +
        "__allowEmptyVault: true."
    );
    // Keep local state consistent so the UI does not flicker, but
    // do NOT send the empty payload to the backend.
    memoryVault = merged;
    syncLocalStorage(merged);
    return merged;
  }

  memoryVault = merged;
  syncLocalStorage(merged);

  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent("omweb:vault-updated", { detail: merged })
    );
  }

  // ------------------------------------------------------------------
  // CRITICAL FIX — send ONLY `partial`, never `merged`.
  //
  // The backend route `save_models_vault` uses per-key merge semantics:
  //
  //     for key in [...]:
  //         if key in payload:
  //             current[key] = payload[key]
  //
  // A key NOT present in the payload is left completely untouched.
  //
  // The previous implementation sent `merged` (every key from
  // `memoryVault` folded together with `partial`). That payload
  // ALWAYS contained `provider_package_models` and
  // `provider_connections` — sourced from `memoryVault`, which is a
  // stale in-memory snapshot that is never refreshed by
  // `useProviderPackages.saveModelsForPackage()` (that hook POSTs
  // directly to `/api/config/vault`, bypassing this function entirely).
  //
  // Concrete failure this fixes:
  //
  //   1. App boot → `fetchVault()` populates memoryVault, at which
  //      point `provider_package_models = {}` (empty).
  //   2. User saves models for Package A via Store Providers →
  //      `saveModelsForPackage` POSTs `{ provider_package_models: { A: {...} } }`
  //      → backend disk is correct, but `memoryVault` still holds
  //      `provider_package_models = {}`.
  //   3. User clicks "Save Config" in the sidebar →
  //      `saveVaultToBackend({ cloud_vault, custom_endpoints, lmstudio_vault, ollama_vault, scanned_models })`.
  //      The old code sent `merged` including the STALE empty
  //      `provider_package_models = {}` → backend faithfully applied
  //      the merge → Package A's models were ERASED FROM DISK.
  //
  // By sending only the keys the caller explicitly supplied, the
  // backend's per-key merge leaves every other key (including
  // `provider_package_models` and `provider_connections`) intact.
  // This is the only correct read-modify-write contract for a
  // per-key-merge endpoint in the presence of multiple write paths.
  // ------------------------------------------------------------------
  const payload: Record<string, unknown> = {
    ...(partial as Record<string, unknown>),
  };
  // Strip the guard opt-out flag before sending — it is frontend-only
  // bookkeeping and must never reach the backend.
  delete payload.__allowEmptyVault;

  const endpoints = getCandidateEndpoints();
  for (const url of endpoints) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        const result = await res.json();
        if (result.vault) {
          // The backend returns the FULL post-merge vault, so this is
          // our opportunity to reconcile `memoryVault` (including
          // `provider_package_models`, which may have been mutated by
          // another tab since the last `fetchVault`). Always trust
          // the backend's response over the local optimistic merge.
          memoryVault = result.vault as VaultData;
          syncLocalStorage(result.vault);
          return result.vault as VaultData;
        }
        return merged;
      }
    } catch {}
  }

  return merged;
}

export function syncLocalStorage(vault: VaultData): void {
  if (typeof window === "undefined") return;
  isSyncingFromBackend = true;
  try {
    localStorage.setItem(
      "omweb_cloud_vault",
      JSON.stringify(vault.cloud_vault || [])
    );
    localStorage.setItem(
      "omweb_custom_endpoints",
      JSON.stringify(vault.custom_endpoints || [])
    );
    localStorage.setItem(
      "omweb_lmstudio_vault",
      JSON.stringify(vault.lmstudio_vault || DEFAULT_VAULT.lmstudio_vault)
    );
    localStorage.setItem(
      "omweb_ollama_vault",
      JSON.stringify(vault.ollama_vault || DEFAULT_VAULT.ollama_vault)
    );
    localStorage.setItem(
      "omweb_scanned_models",
      JSON.stringify(vault.scanned_models || [])
    );
  } catch {
  } finally {
    isSyncingFromBackend = false;
  }
}

export function getLocalVaultFallback(): VaultData {
  if (typeof window === "undefined") return DEFAULT_VAULT;
  try {
    return {
      cloud_vault: JSON.parse(
        localStorage.getItem("omweb_cloud_vault") || "[]"
      ),
      custom_endpoints: JSON.parse(
        localStorage.getItem("omweb_custom_endpoints") || "[]"
      ),
      lmstudio_vault: JSON.parse(
        localStorage.getItem("omweb_lmstudio_vault") ||
          JSON.stringify(DEFAULT_VAULT.lmstudio_vault)
      ),
      ollama_vault: JSON.parse(
        localStorage.getItem("omweb_ollama_vault") ||
          JSON.stringify(DEFAULT_VAULT.ollama_vault)
      ),
      scanned_models: JSON.parse(
        localStorage.getItem("omweb_scanned_models") || "[]"
      ),
    };
  } catch {
    return DEFAULT_VAULT;
  }
}

export function debouncedSyncToBackend(delay = 150): void {
  if (syncTimeout) clearTimeout(syncTimeout);
  syncTimeout = setTimeout(() => {
    const current = getLocalVaultFallback();
    // ------------------------------------------------------------------
    // EXPLICIT PARTIAL — do NOT pass `current` wholesale.
    //
    // `getLocalVaultFallback()` is a snapshot of localStorage, which
    // only tracks the five frontend-managed keys (cloud_vault,
    // custom_endpoints, lmstudio_vault, ollama_vault, scanned_models).
    //
    // The backend-managed keys — `provider_connections` and
    // `provider_package_models` — are NEVER stored in localStorage.
    // If this function passed `current` directly to
    // `saveVaultToBackend`, those keys would be `undefined` in the
    // payload, and any subsequent code path that JSON-stringifies the
    // payload would either omit them (correct) or include empty
    // defaults (catastrophic — overwrites live data with `{}`/`[]`).
    //
    // Constructing the partial explicitly removes all ambiguity: only
    // the five keys this function is actually responsible for are
    // ever transmitted.
    // ------------------------------------------------------------------
    void saveVaultToBackend({
      cloud_vault: current.cloud_vault,
      custom_endpoints: current.custom_endpoints,
      lmstudio_vault: current.lmstudio_vault,
      ollama_vault: current.ollama_vault,
      scanned_models: current.scanned_models,
    });
  }, delay);
}

if (typeof window !== "undefined") {
  const originalSetItem = window.localStorage.setItem.bind(window.localStorage);
  window.localStorage.setItem = function (key: string, value: string) {
    originalSetItem(key, value);
    if (isSyncingFromBackend) return;
    if (
      key === "omweb_cloud_vault" ||
      key === "omweb_custom_endpoints" ||
      key === "omweb_lmstudio_vault" ||
      key === "omweb_ollama_vault" ||
      key === "omweb_scanned_models"
    ) {
      debouncedSyncToBackend();
    }
  };

  void fetchVault();
}
