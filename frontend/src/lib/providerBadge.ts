// frontend/src/lib/providerBadge.ts

"use client";

/**
 * Provider badge resolution for the ChatThread identity header.
 *
 * Mirrors the icon-resolution logic in `engine-selector.tsx`, but
 * runs outside the dropdown context where the full provider list is
 * already loaded. The badge identifier ultimately comes from
 * backend/data/models_vault.json (see lib/vault.ts::syncLocalStorage).
 *
 * Supported provider categories:
 *   - LM Studio        → lmstudio_vault.badge        (localStorage)
 *   - Ollama           → ollama_vault.badge          (localStorage)
 *   - Cloud            → cloud_vault[].badge         (localStorage)
 *   - Custom endpoints → custom_endpoints[].badge    (localStorage)
 *   - Store packages   → provider_connections[].badge (server, async)
 *
 * The `provider_connections` array is intentionally NOT mirrored to
 * localStorage (see lib/vault.ts docstring), so package badges are
 * resolved asynchronously by the hook, with a module-level cache to
 * avoid repeated network calls across re-renders.
 */

import { useEffect, useState } from "react";

const LM_STUDIO_HINTS = ["lmstudio", "lm_studio", "lm-studio", "lm studio"];
const OLLAMA_HINTS = ["ollama"];

function parseJSON<T>(raw: string | null, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

/**
 * Extract a non-empty badge string from a vault entry.
 * Falls back to `icon` when `badge` is missing (older records).
 */
function pickBadge(entry: any): string | null {
  const badge = entry?.badge ?? entry?.icon;
  return typeof badge === "string" && badge.trim() ? badge.trim() : null;
}

/**
 * Synchronous resolver for local / cloud / custom providers.
 * Returns null when the provider is unknown or the vault has not
 * been synced to localStorage yet.
 */
export function resolveProviderBadge(
  providerName: string | null | undefined
): string | null {
  if (typeof window === "undefined" || !providerName) return null;

  const needle = providerName.trim().toLowerCase();
  if (!needle) return null;

  // 1. LM Studio (matched by name substring to cover all aliases).
  if (LM_STUDIO_HINTS.some((h) => needle.includes(h))) {
    const lm = parseJSON<Record<string, any> | null>(
      localStorage.getItem("omweb_lmstudio_vault"),
      null
    );
    return pickBadge(lm);
  }

  // 2. Ollama (same substring strategy as engine-selector).
  if (OLLAMA_HINTS.some((h) => needle.includes(h))) {
    const ollama = parseJSON<Record<string, any> | null>(
      localStorage.getItem("omweb_ollama_vault"),
      null
    );
    return pickBadge(ollama);
  }

  // 3. Cloud providers (exact name match — matches the write path in
  //    engine-selector::handleSelectEngine where provider_name = cp.name).
  const cloud = parseJSON<any[]>(
    localStorage.getItem("omweb_cloud_vault"),
    []
  );
  const cloudHit = cloud.find(
    (c) => typeof c?.name === "string" && c.name.toLowerCase() === needle
  );
  const cloudBadge = pickBadge(cloudHit);
  if (cloudBadge) return cloudBadge;

  // 4. Custom endpoints (exact name match — same rationale as cloud).
  const custom = parseJSON<any[]>(
    localStorage.getItem("omweb_custom_endpoints"),
    []
  );
  const customHit = custom.find(
    (c) => typeof c?.name === "string" && c.name.toLowerCase() === needle
  );
  return pickBadge(customHit);
}

// ---------------------------------------------------------------------------
// Async package badge resolution
// ---------------------------------------------------------------------------

/**
 * Module-level cache. Keyed by lowercase provider display name.
 * Populated once per tab, refreshed only on explicit invalidation.
 */
let packageBadgesCache: Map<string, string> | null = null;
let packageBadgesInflight: Promise<Map<string, string>> | null = null;

/**
 * Fetch provider_connections + enabled Store packages, and build a
 * display-name → badge map.
 *
 * The badge is taken from the connection record first (authoritative —
 * `save_provider_connection()` in config_rtr.py auto-fills it from the
 * manifest on every save), then from the package registry entry, and
 * finally from the package id.
 */
async function loadPackageBadges(): Promise<Map<string, string>> {
  if (packageBadgesCache) return packageBadgesCache;
  if (packageBadgesInflight) return packageBadgesInflight;

  packageBadgesInflight = (async () => {
    const map = new Map<string, string>();
    try {
      const [providersRes, vaultRes] = await Promise.all([
        fetch("/api/store/providers").then((r) => r.json()),
        fetch("/api/config/vault").then((r) => r.json()),
      ]);

      const providers: any[] = providersRes?.providers || [];
      const connections: any[] = vaultRes?.provider_connections || [];

      for (const p of providers) {
        if (!p?.enabled) continue;

        const conn = connections.find(
          (c: any) =>
            c?.provider_package_id === p.id &&
            c?.is_active === true &&
            c?.enabled === true
        );
        if (!conn) continue;

        // Resolution priority matches config_rtr.save_provider_connection:
        //   connection.badge → package.badge → package.icon → package.id
        const badge =
          pickBadge(conn) ?? pickBadge(p) ?? (p?.id ? String(p.id) : null);
        if (!badge) continue;

        const displayName = (
          p.display_name ||
          p.name ||
          p.id ||
          ""
        ).toString();
        if (displayName) {
          map.set(displayName.trim().toLowerCase(), badge);
        }
      }
    } catch {
      // Network failure is non-fatal — the caller falls back to the
      // generic Bot icon when no badge is resolved.
    }
    packageBadgesCache = map;
    packageBadgesInflight = null;
    return map;
  })();

  return packageBadgesInflight;
}

/** Drop the cached package badge map (e.g. after the user installs a package). */
export function invalidatePackageBadgesCache(): void {
  packageBadgesCache = null;
  packageBadgesInflight = null;
}

// ---------------------------------------------------------------------------
// React hook
// ---------------------------------------------------------------------------

/**
 * Returns the resolved provider badge identifier for the given
 * provider name, or `null` when it cannot be resolved.
 *
 * The hook:
 *   1. Resolves synchronously from localStorage on first render.
 *   2. Falls back to an async package lookup when the sync resolver
 *      returns null (covers Store-installed Provider Packages).
 *   3. Re-resolves whenever the vault changes (omweb:vault-updated).
 */
export function useProviderBadge(
  providerName: string | null | undefined
): string | null {
  const [badge, setBadge] = useState<string | null>(() =>
    resolveProviderBadge(providerName)
  );

  useEffect(() => {
    let cancelled = false;

    const sync = resolveProviderBadge(providerName);
    setBadge(sync);
    if (sync) return;

    if (!providerName || !providerName.trim()) return;
    const needle = providerName.trim().toLowerCase();

    void loadPackageBadges().then((map) => {
      if (cancelled) return;
      const pkgBadge = map.get(needle) ?? null;
      if (pkgBadge) setBadge(pkgBadge);
    });

    return () => {
      cancelled = true;
    };
  }, [providerName]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onVaultUpdate = () => {
      const sync = resolveProviderBadge(providerName);
      if (sync) setBadge(sync);
    };
    window.addEventListener("omweb:vault-updated", onVaultUpdate);
    return () =>
      window.removeEventListener("omweb:vault-updated", onVaultUpdate);
  }, [providerName]);

  return badge;
}
