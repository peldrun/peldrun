// frontend/src/app/settings/setup/tabs/llm/hooks/useProviderPackages.ts
"use client";

import { useCallback, useEffect, useState } from "react";

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * One Provider Package as the tab renders it.
 *
 * This shape is deliberately self-contained — it does NOT reuse
 * `CloudProviderVaultItem` (which is designed for OpenAI / Anthropic
 * style entries). Packages have a different lifecycle:
 *
 *   * Their credentials live in `provider_connections` and are
 *     resolved by the backend dispatcher at request time.
 *   * Only the model selection lives in `provider_package_models`.
 *   * A package may be installed but have no active connection —
 *     the card must surface that state clearly.
 */
export interface ProviderPackageCard {
  /** Canonical package id (e.g. "gigachat"). */
  id: string;

  /** Human-friendly label (display_name || name || id). */
  displayName: string;

  /** Short description from the manifest. */
  description: string;

  /**
   * Icon base name for `<Icon name="..." />`. Used as the FALLBACK
   * when `badge` is absent or contains markup that fails sanitization.
   *
   * Sourced from the manifest's `icon` field via `/api/store/providers`,
   * falling back to the package id when the manifest does not declare
   * an icon.
   */
  iconName: string;

  /**
   * Canonical badge value for `<ProviderBadgeIcon badge={...} />`.
   *
   * Resolution priority (computed in `reload()`):
   *   1. `provider_connections[].badge` — auto-filled by the backend
   *      from `manifest.icon` inside `save_provider_connection()`.
   *      This is the AUTHORITATIVE source once a connection exists
   *      because it survives package-registry outages.
   *   2. Manifest `icon` field (via `/api/store/providers`). Used
   *      when no connection has been saved yet, or when all
   *      connections were deleted.
   *   3. `undefined` — the caller falls back to `iconName`.
   *
   * May contain EITHER an icon base name (e.g. "gigachat",
   * "openai-chatgpt") OR inline SVG markup (starting with `<svg`).
   * `<ProviderBadgeIcon>` detects the shape automatically, so no
   * `icon_type` flag is required at this layer.
   */
  badge?: string;

  /** Whether the package itself is enabled in the registry. */
  isEnabled: boolean;

  /** Whether an active + enabled connection exists. */
  hasActiveConnection: boolean;

  /** Name of the active connection (e.g. "GIGACHAT_API_PERS"). */
  activeConnectionName: string | null;

  /** Models the user has selected via ModelDiscoveryModal. */
  savedModels: string[];

  /** Currently selected model (first of savedModels by default). */
  model: string;
}

export interface UseProviderPackagesResult {
  packages: ProviderPackageCard[];
  isLoading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  saveModelsForPackage: (
    packageId: string,
    savedModels: string[],
    model: string
  ) => Promise<void>;
}

/* -------------------------------------------------------------------------- */
/* Hook                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Fetches installed Provider Packages + their connection state + their
 * saved model selections, and exposes a card-ready list.
 *
 * Data sources
 * ------------
 *   * `/api/store/providers`         → package registry
 *   * `/api/config/vault`            → `provider_connections` and
 *                                      `provider_package_models`
 *
 * Auto-refresh
 * ------------
 * Listens for `omweb:provider-package-updated` — fired by the Store
 * after install / uninstall / connection change. This is what makes
 * the tab update live without a page reload.
 */
export function useProviderPackages(): UseProviderPackagesResult {
  const [packages, setPackages] = useState<ProviderPackageCard[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  /* ---------------------- Load --------------------------------------- */

  const reload = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      // ------------------------------------------------------------------
      // CRITICAL: `cache: "no-store"` on BOTH fetches.
      //
      // `/api/config/vault` is live mutable state — it is rewritten by
      // `saveModelsForPackage` below, by the Store tab, and by every
      // connection mutation on the backend. The FastAPI route does NOT
      // emit `Cache-Control: no-store`, so without this directive the
      // browser applies heuristic caching and serves a stale snapshot
      // on the next mount.
      //
      // Concrete failure mode this prevents:
      //
      //   1. User opens Store Providers → vault is EMPTY at that moment
      //      → browser caches `{ provider_package_models: {} }`.
      //   2. User saves models for Package A → POST writes them to disk.
      //   3. User navigates away and returns → `reload()` fires
      //      → browser serves the CACHED empty snapshot
      //      → the card shows "No models discovered yet" even though the
      //        backend correctly persisted the user's selection.
      //
      // `/api/store/providers` is technically semi-static, but it is
      // also mutated by install/uninstall operations during the same
      // session. `no-store` on it costs nothing and keeps both reads
      // consistent with each other — critical because we join them by
      // package id below.
      // ------------------------------------------------------------------
      const [pkgsRes, vaultRes] = await Promise.all([
        fetch("/api/store/providers", { cache: "no-store" }).then((r) =>
          r.json()
        ),
        fetch("/api/config/vault", { cache: "no-store" }).then((r) =>
          r.json()
        ),
      ]);

      const rawProviders: Array<Record<string, any>> =
        pkgsRes.providers || [];
      const connections: Array<Record<string, any>> =
        vaultRes.provider_connections || [];
      const savedModels: Record<
        string,
        { savedModels?: string[]; model?: string }
      > = vaultRes.provider_package_models || {};

      // ------------------------------------------------------------------
      // Helper: return the first non-empty trimmed string from a list of
      // candidates, or `undefined` when none qualify.
      //
      // Treats whitespace-only values as absent so a legacy vault with
      // `"badge": " "` (a common artefact from older client versions)
      // does not silence the manifest fallback. Non-string values are
      // ignored — the vault is user-editable JSON, so `null`/numbers
      // can appear if someone hand-edits the file.
      // ------------------------------------------------------------------
      const pickFirstString = (
        ...candidates: Array<unknown>
      ): string | undefined => {
        for (const c of candidates) {
          if (typeof c === "string" && c.trim()) return c.trim();
        }
        return undefined;
      };

      const usable: ProviderPackageCard[] = rawProviders.map((p) => {
        const conn = connections.find(
          (c: Record<string, any>) =>
            c.provider_package_id === p.id &&
            c.is_active &&
            c.enabled
        );

        const saved = savedModels[p.id] || {};
        const modelsList = Array.isArray(saved.savedModels)
          ? saved.savedModels
          : [];

        // ------------------------------------------------------------------
        // Badge resolution.
        //
        // Priority:
        //   1. `provider_connections[].badge` — persisted by the backend
        //      (auto-filled from `manifest.icon` in
        //      `save_provider_connection`). Authoritative once a
        //      connection has been created — survives package-registry
        //      outages and supports inline SVG.
        //
        //   2. Manifest `icon` field — used when no connection has been
        //      saved yet. Same source as `iconName`, but passed through
        //      verbatim so `<ProviderBadgeIcon>` can detect SVG vs name.
        //
        //   3. `undefined` — the caller (`PackageCard`) falls back to
        //      `iconName`, which itself defaults to the package id.
        //
        // The chosen value may be either an icon base name or inline
        // SVG markup. `<ProviderBadgeIcon>` distinguishes the two by
        // checking for the `<svg` marker — no `icon_type` flag needed.
        // ------------------------------------------------------------------
        const badge = pickFirstString(
          conn?.badge,
          typeof p.icon === "string" ? p.icon : undefined,
        );

        return {
          id: p.id,
          displayName: p.display_name || p.name || p.id,
          description: p.description || "",
          iconName: p.icon || p.id,
          badge,
          isEnabled: Boolean(p.enabled),
          hasActiveConnection: Boolean(conn),
          activeConnectionName: conn?.name || null,
          savedModels: modelsList,
          model: saved.model || modelsList[0] || "",
        };
      });

      setPackages(usable);
    } catch (err) {
      const msg =
        err instanceof Error
          ? err.message
          : "Failed to load provider packages";
      setError(msg);
      setPackages([]);
    } finally {
      setIsLoading(false);
    }
  }, []);

  /* ---------------------- Save --------------------------------------- */

  const saveModelsForPackage = useCallback(
    async (
      packageId: string,
      savedModels: string[],
      model: string
    ): Promise<void> => {
      // ------------------------------------------------------------------
      // Read-modify-write against a REPLACE-semantics endpoint.
      //
      // The backend's `save_models_vault` REPLACES the entire
      // `provider_package_models` map. To avoid wiping other packages,
      // we must first read the CURRENT map, merge in our update, then
      // write the full map back.
      //
      // CRITICAL: `cache: "no-store"` on the read is non-negotiable.
      //
      // Without it, this exact data-loss sequence occurs:
      //
      //   1. User saves Package A models → vault = { A: {...} } on disk.
      //      POST response updates only in-memory `packages` state, NOT
      //      any HTTP cache — the GET cache is still the pre-save snapshot
      //      (empty), because no GET happened after the POST.
      //   2. User saves Package B models → `saveModelsForPackage` GETs
      //      `/api/config/vault`. Browser serves the CACHED empty map
      //      (from step 1's earlier read) → `current = {}`.
      //   3. `current["B"] = {...}` → POST writes `{ B: {...} }` with
      //      Package A COMPLETELY ABSENT.
      //   4. Result: Package A's saved models are silently erased from
      //      disk. The card still shows them in-session (local state was
      //      updated optimistically), so the user has NO visible signal
      //      until they reload the page — and by then the data is gone.
      //
      // `cache: "no-store"` forces a fresh read from the backend every
      // time, so the merge operates on the true on-disk state. This is
      // the ONLY way to make read-modify-write safe against a
      // REPLACE-semantics backend without introducing a server-side
      // PATCH endpoint.
      // ------------------------------------------------------------------
      const vaultRes = await fetch("/api/config/vault", {
        cache: "no-store",
      }).then((r) => r.json());
      const current: Record<string, unknown> =
        (vaultRes.provider_package_models as Record<string, unknown>) ||
        {};

      current[packageId] = {
        savedModels: [...savedModels],
        model,
      };

      // The POST itself is not cacheable (methods other than GET/HEAD
      // are not heuristically cached by browsers), but we still pass
      // `cache: "no-store"` for consistency and to signal intent
      // explicitly to any future refactor. `no-store` on a POST is a
      // no-op per the Fetch spec — the directive is purely documentary
      // here, ensuring a reader cannot mistake this write for a safe
      // cacheable operation.
      const saveRes = await fetch("/api/config/vault", {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_package_models: current }),
      });

      if (!saveRes.ok) {
        throw new Error(
          `Failed to persist model selection (HTTP ${saveRes.status})`
        );
      }

      // Update local state immediately so the card reflects the new
      // selection without waiting for another reload round-trip.
      setPackages((prev) =>
        prev.map((p) =>
          p.id === packageId
            ? {
                ...p,
                savedModels: [...savedModels],
                model,
              }
            : p
        )
      );
    },
    []
  );

  /* ---------------------- Lifecycle ---------------------------------- */

  useEffect(() => {
    reload();

    const onPackageUpdate = () => {
      reload();
    };
    window.addEventListener(
      "omweb:provider-package-updated",
      onPackageUpdate
    );

    return () => {
      window.removeEventListener(
        "omweb:provider-package-updated",
        onPackageUpdate
      );
    };
  }, [reload]);

  return { packages, isLoading, error, reload, saveModelsForPackage };
}
