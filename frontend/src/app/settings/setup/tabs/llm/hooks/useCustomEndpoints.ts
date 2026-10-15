// frontend/src/app/settings/setup/tabs/llm/hooks/useCustomEndpoints.ts
"use client";

import { useCallback, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import type { CustomEndpoint, CustomEndpointWithLogo } from "../types";
import type { EndpointFormState } from "../types";
import { EMPTY_ENDPOINT_FORM } from "../constants";
import { STORAGE_KEYS, slugifyProviderName, writeLocalJson } from "../utils";

/* -------------------------------------------------------------------------- */
/* Hook contract                                                              */
/* -------------------------------------------------------------------------- */

export interface UseCustomEndpointsParams {
  customEndpoints: CustomEndpoint[];
  setCustomEndpoints: Dispatch<SetStateAction<CustomEndpoint[]>>;
  /**
   * Endpoint probe delegated to the parent tab. Kept as a dependency so the
   * hook stays agnostic about the transport used by the caller.
   */
  testEndpoint: (
    baseUrl: string,
    apiKey: string,
    model: string,
    apiType?: string
  ) => Promise<{ ok: boolean; message: string; latency?: number }>;
}

export interface UseCustomEndpointsResult {
  /** Local form state for the "Add Custom Endpoint" panel. */
  endpointForm: EndpointFormState;
  /** Setter for the local form state (controlled by the parent). */
  setEndpointForm: Dispatch<SetStateAction<EndpointFormState>>;
  /** Id of the target currently being tested (`"custom_form"` for the form). */
  testingId: string | null;
  /** Runs a connectivity probe against the current form values. */
  handleTestCustomForm: () => Promise<void>;
  /** Persists the current form as a new custom endpoint. */
  handleSaveCustomEndpoint: () => void;
  /** Removes a custom endpoint by id. */
  handleDeleteCustomEndpoint: (id: string) => void;
  /**
   * Merges discovered models into an existing custom endpoint and persists
   * the result to localStorage.
   */
  applyDiscoveredModels: (
    endpointId: string,
    selectedModels: string[],
    primaryModel?: string
  ) => void;
}

/* -------------------------------------------------------------------------- */
/* Internal helpers                                                           */
/* -------------------------------------------------------------------------- */

/**
 * Persists the current custom-endpoint list to localStorage using the
 * canonical `omweb_custom_endpoints` key. Wrapped in a try/catch to mirror
 * the defensive behaviour of the original inline code.
 */
function persistCustomEndpoints(endpoints: CustomEndpoint[]): void {
  writeLocalJson(STORAGE_KEYS.customEndpoints, endpoints);
}

/* -------------------------------------------------------------------------- */
/* Hook                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Encapsulates the entire lifecycle of user-defined custom endpoints:
 *   - form state for creating a new endpoint (including the new `badge`),
 *   - create / delete operations,
 *   - connectivity probing for the in-progress form,
 *   - merging of models discovered through the discovery modal.
 *
 * The hook owns `endpointForm` and `testingId` because both are strictly
 * scoped to this section and have no meaning elsewhere in the LLM tab.
 */
export function useCustomEndpoints({
  customEndpoints,
  setCustomEndpoints,
  testEndpoint,
}: UseCustomEndpointsParams): UseCustomEndpointsResult {
  const [endpointForm, setEndpointForm] = useState<EndpointFormState>(
    EMPTY_ENDPOINT_FORM
  );
  const [testingId, setTestingId] = useState<string | null>(null);

  /* -------------------------- Test current form ------------------------- */

  const handleTestCustomForm = useCallback(async () => {
    setTestingId("custom_form");
    try {
      const res = await testEndpoint(
        endpointForm.endpointUrl,
        endpointForm.apiKey,
        endpointForm.defaultModel,
        ""
      );

      if (res.ok) {
        showToast.success(
          "Custom Endpoint Connected",
          `Latency: ${res.latency} ms`,
          res.latency
        );
      } else {
        showToast.error("Custom Endpoint Failed", res.message);
      }
    } finally {
      setTestingId(null);
    }
  }, [endpointForm, testEndpoint]);

  /* -------------------------- Create new endpoint ----------------------- */

  const handleSaveCustomEndpoint = useCallback(() => {
    if (!endpointForm.name.trim() || !endpointForm.endpointUrl.trim()) {
      showToast.warning("Missing Fields", "Name and Endpoint URL are required.");
      return;
    }

    const newEndpoint: CustomEndpointWithLogo = {
      id: `custom_${Date.now()}`,
      name: endpointForm.name.trim(),
      providerId:
        endpointForm.providerId.trim() ||
        slugifyProviderName(endpointForm.name),
      endpointUrl: endpointForm.endpointUrl.trim(),
      apiMode: endpointForm.apiMode,
      defaultModel: endpointForm.defaultModel.trim() || "default",
      contextWindow: endpointForm.contextWindow || "Auto",
      apiKey: endpointForm.apiKey.trim(),
      status: "untested",
      useForNewChats: endpointForm.useForNewChats,
      savedModels: [],
      badge: endpointForm.badge,
    };

    setCustomEndpoints((prev) => {
      const next = [newEndpoint, ...prev] as CustomEndpoint[];
      persistCustomEndpoints(next);
      return next;
    });

    setEndpointForm(EMPTY_ENDPOINT_FORM);
    showToast.success("Custom Endpoint Saved", newEndpoint.name);
  }, [endpointForm, setCustomEndpoints]);

  /* -------------------------- Delete endpoint --------------------------- */

  const handleDeleteCustomEndpoint = useCallback(
    (id: string) => {
      setCustomEndpoints((prev) => {
        const next = prev.filter((item) => item.id !== id);
        persistCustomEndpoints(next);
        return next;
      });
      showToast.info("Endpoint Removed", "Custom endpoint removed.");
    },
    [setCustomEndpoints]
  );

  /* -------------------------- Apply discovered models ------------------- */

  const applyDiscoveredModels = useCallback(
    (endpointId: string, selectedModels: string[], primaryModel?: string) => {
      setCustomEndpoints((prev) => {
        const next = prev.map((ce) => {
          if (ce.id !== endpointId) return ce;
          const fallbackPrimary =
            selectedModels.length > 0 ? selectedModels[0] : ce.defaultModel;
          return {
            ...ce,
            savedModels: selectedModels,
            defaultModel: primaryModel || fallbackPrimary,
          };
        });
        persistCustomEndpoints(next);
        return next;
      });
    },
    [setCustomEndpoints]
  );

  return {
    endpointForm,
    setEndpointForm,
    testingId,
    handleTestCustomForm,
    handleSaveCustomEndpoint,
    handleDeleteCustomEndpoint,
    applyDiscoveredModels,
  };
}
