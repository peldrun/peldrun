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
  // scanned models). Callers that legitimately want to clear the
  // vault must set `__allowEmptyVault: true` on the partial payload,
  // which serves as an explicit, auditable opt-out.
  const hasAnyContent =
    (merged.lmstudio_vault?.savedModels?.length ?? 0) > 0 ||
    (merged.ollama_vault?.savedModels?.length ?? 0) > 0 ||
    (merged.cloud_vault?.length ?? 0) > 0 ||
    (merged.custom_endpoints?.length ?? 0) > 0 ||
    (merged.scanned_models?.length ?? 0) > 0;

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

  const endpoints = getCandidateEndpoints();
  for (const url of endpoints) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(merged),
      });
      if (res.ok) {
        const result = await res.json();
        if (result.vault) {
          memoryVault = result.vault;
          syncLocalStorage(result.vault);
          return result.vault;
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
    void saveVaultToBackend(current);
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
