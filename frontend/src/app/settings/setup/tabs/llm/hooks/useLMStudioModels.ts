// frontend/src/app/settings/setup/tabs/llm/hooks/useLMStudioModels.ts
"use client";

import { useCallback } from "react";
import type { Dispatch, SetStateAction } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import type { LMStudioSettings } from "../types";
import {
  STORAGE_KEYS,
  readLocalJson,
  removeLocal,
  writeLocalJson,
} from "../utils";

/* -------------------------------------------------------------------------- */
/* Hook contract                                                              */
/* -------------------------------------------------------------------------- */

export interface UseLMStudioModelsParams {
  availableModels: string[];
  setAvailableModels: Dispatch<SetStateAction<string[]>>;
  lmStudioSettings: LMStudioSettings;
}

export interface UseLMStudioModelsResult {
  /**
   * Adds a model ID to the LM Studio list.
   * Returns `true` when the model was appended, `false` when it was empty
   * or already present (a toast is shown in that case).
   */
  addModel: (rawInput: string) => boolean;
  /** Removes a model ID from the LM Studio list. */
  removeModel: (modelId: string) => void;
  /** Clears the entire LM Studio model list and its persisted cache. */
  clearAll: () => void;
}

/* -------------------------------------------------------------------------- */
/* Internal helpers                                                           */
/* -------------------------------------------------------------------------- */

/**
 * Persists the LM Studio model list to localStorage in both the flat
 * scanned-models cache and the structured LM Studio vault. Matches the
 * original dual-write behaviour verbatim.
 */
function persistLMStudioModels(
  models: string[],
  lmStudioSettings: LMStudioSettings
): void {
  writeLocalJson(STORAGE_KEYS.scannedModels, models);

  const vault = readLocalJson<Record<string, unknown>>(
    STORAGE_KEYS.lmStudioVault,
    { ...lmStudioSettings }
  );
  vault.savedModels = models;
  writeLocalJson(STORAGE_KEYS.lmStudioVault, vault);
}

/* -------------------------------------------------------------------------- */
/* Hook                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Encapsulates all LM Studio model-list mutations (add / remove / clear).
 *
 * The hook is intentionally stateless regarding the input field — the caller
 * keeps the controlled input and passes its trimmed value to `addModel`,
 * which returns whether the operation succeeded so the input can be reset.
 */
export function useLMStudioModels({
  availableModels,
  setAvailableModels,
  lmStudioSettings,
}: UseLMStudioModelsParams): UseLMStudioModelsResult {
  const addModel = useCallback(
    (rawInput: string): boolean => {
      const trimmed = rawInput.trim();
      if (!trimmed) return false;

      if (availableModels.includes(trimmed)) {
        showToast.info("Model Exists", "This model is already in the list.");
        return false;
      }

      const updated = [...availableModels, trimmed];
      setAvailableModels(updated);
      persistLMStudioModels(updated, lmStudioSettings);
      showToast.success("Model Added", trimmed);
      return true;
    },
    [availableModels, setAvailableModels, lmStudioSettings]
  );

  const removeModel = useCallback(
    (modelId: string) => {
      const updated = availableModels.filter((m) => m !== modelId);
      setAvailableModels(updated);
      persistLMStudioModels(updated, lmStudioSettings);
      showToast.info("Model Removed", modelId);
    },
    [availableModels, setAvailableModels, lmStudioSettings]
  );

  const clearAll = useCallback(() => {
    setAvailableModels([]);
    removeLocal(STORAGE_KEYS.scannedModels);
    showToast.info("List Cleared", "LM Studio models list cleared.");
  }, [setAvailableModels]);

  return { addModel, removeModel, clearAll };
}
