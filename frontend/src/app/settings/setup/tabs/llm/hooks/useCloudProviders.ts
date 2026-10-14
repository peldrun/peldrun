// frontend/src/app/settings/setup/tabs/llm/hooks/useCloudProviders.ts
"use client";

import { useCallback, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import type { CloudProviderVaultItem } from "../types";

/* -------------------------------------------------------------------------- */
/* Hook contract                                                              */
/* -------------------------------------------------------------------------- */

export interface UseCloudProvidersParams {
  cloudProviders: CloudProviderVaultItem[];
  setCloudProviders: Dispatch<SetStateAction<CloudProviderVaultItem[]>>;
  /**
   * Endpoint probe delegated to the parent tab. Kept as a dependency so the
   * hook can stay agnostic about the transport (fetch/axios/mock).
   */
  testEndpoint: (
    baseUrl: string,
    apiKey: string,
    model: string,
    apiType?: string
  ) => Promise<{ ok: boolean; message: string; latency?: number }>;
}

export interface UseCloudProvidersResult {
  /** Id of the provider currently being tested, or `null` when idle. */
  testingId: string | null;
  /** Per-provider API key visibility map (keyed by provider id). */
  showKeys: Record<string, boolean>;
  /** Toggles the reveal state of a provider's API key field. */
  toggleKey: (providerId: string) => void;
  /** Updates a single field on a provider entry (immutably). */
  updateProvider: (
    providerId: string,
    patch: Partial<CloudProviderVaultItem>
  ) => void;
  /**
   * Runs a credential/connectivity probe for the given provider.
   * Updates the provider's `status`, `latency`, and `lastError`.
   */
  handleTestCloud: (providerId: string) => Promise<void>;
  /** Clears the testing indicator. Exposed so the parent can chain calls. */
  setTestingId: Dispatch<SetStateAction<string | null>>;
}

/* -------------------------------------------------------------------------- */
/* Hook                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Encapsulates cloud-provider credential management and connectivity tests.
 *
 * Responsibilities:
 *   - Track `testingId` for the currently probed provider.
 *   - Track per-provider API key visibility (`showKeys`).
 *   - Provide an immutable `updateProvider` helper that automatically resets
 *     the `status` to `"untested"` when credentials or endpoint change.
 *   - Perform a live probe via `testEndpoint` and reflect the outcome in the
 *     provider's runtime fields (`status`, `latency`, `lastError`).
 */
export function useCloudProviders({
  cloudProviders,
  setCloudProviders,
  testEndpoint,
}: UseCloudProvidersParams): UseCloudProvidersResult {
  const [testingId, setTestingId] = useState<string | null>(null);
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({});

  /* -------------------------- Key visibility ---------------------------- */

  const toggleKey = useCallback((providerId: string) => {
    setShowKeys((prev) => ({ ...prev, [providerId]: !prev[providerId] }));
  }, []);

  /* -------------------------- Provider mutation ------------------------- */

  const updateProvider = useCallback(
    (providerId: string, patch: Partial<CloudProviderVaultItem>) => {
      setCloudProviders((prev) =>
        prev.map((item) =>
          item.id === providerId ? { ...item, ...patch } : item
        )
      );
    },
    [setCloudProviders]
  );

  /* -------------------------- Connectivity test ------------------------- */

  const handleTestCloud = useCallback(
    async (providerId: string) => {
      const cp = cloudProviders.find((p) => p.id === providerId);
      if (!cp) return;

      if (!cp.apiKey.trim()) {
        showToast.warning(
          `${cp.name} API Key Required`,
          "Please enter an API key before testing connection."
        );
        return;
      }

      setTestingId(providerId);
      try {
        const res = await testEndpoint(cp.baseUrl, cp.apiKey, cp.model, cp.type);

        setCloudProviders((prev) =>
          prev.map((item) =>
            item.id === providerId
              ? {
                  ...item,
                  status: res.ok ? "online" : "offline",
                  latency: res.latency,
                  lastError: res.ok ? undefined : res.message,
                }
              : item
          )
        );

        if (res.ok) {
          showToast.success(
            `${cp.name} Verified`,
            `Active and responsive (${res.latency} ms)`,
            res.latency
          );
        } else {
          showToast.error(`${cp.name} Connection Failed`, res.message);
        }
      } finally {
        setTestingId(null);
      }
    },
    [cloudProviders, setCloudProviders, testEndpoint]
  );

  return {
    testingId,
    showKeys,
    toggleKey,
    updateProvider,
    handleTestCloud,
    setTestingId,
  };
}
