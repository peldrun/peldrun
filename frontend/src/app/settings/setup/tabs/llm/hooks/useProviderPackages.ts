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

  /** Icon base name for `<Icon name="..." />`. */
  iconName: string;

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
      const [pkgsRes, vaultRes] = await Promise.all([
        fetch("/api/store/providers").then((r) => r.json()),
        fetch("/api/config/vault").then((r) => r.json()),
      ]);

      const rawProviders: Array<Record<string, any>> =
        pkgsRes.providers || [];
      const connections: Array<Record<string, any>> =
        vaultRes.provider_connections || [];
      const savedModels: Record<
        string,
        { savedModels?: string[]; model?: string }
      > = vaultRes.provider_package_models || {};

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

        return {
          id: p.id,
          displayName: p.display_name || p.name || p.id,
          description: p.description || "",
          iconName: p.icon || p.id,
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
      // The backend's `save_models_vault` REPLACES the entire
      // `provider_package_models` map. To avoid wiping other
      // packages, we first read the current map, merge in our update,
      // then write the full map back.
      const vaultRes = await fetch("/api/config/vault").then((r) =>
        r.json()
      );
      const current: Record<string, unknown> =
        (vaultRes.provider_package_models as Record<string, unknown>) ||
        {};

      current[packageId] = {
        savedModels: [...savedModels],
        model,
      };

      const saveRes = await fetch("/api/config/vault", {
        method: "POST",
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
