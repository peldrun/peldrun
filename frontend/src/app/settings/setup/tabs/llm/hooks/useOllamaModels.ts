// frontend/src/app/settings/setup/tabs/llm/hooks/useOllamaModels.ts
"use client";

import { useCallback, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import { saveLocalMetadataVault } from "@/lib/modelMetadata";
import type { OllamaSettings } from "../types";
import { STORAGE_KEYS, resolveApiUrl, writeLocalJson } from "../utils";

/* -------------------------------------------------------------------------- */
/* Hook contract                                                              */
/* -------------------------------------------------------------------------- */

export interface UseOllamaModelsParams {
  ollamaSettings: OllamaSettings;
  setOllamaSettings: Dispatch<SetStateAction<OllamaSettings>>;
}

export interface UseOllamaModelsResult {
  /** True while a backend model scan is in flight. */
  scanningOllama: boolean;
  /**
   * Adds a model ID to the Ollama list.
   * Returns `true` when appended, `false` when empty or already present.
   */
  addModel: (rawInput: string) => boolean;
  /** Removes a model ID and repairs the default selection when needed. */
  removeModel: (modelId: string) => void;
  /** Clears every Ollama model and resets status to `untested`. */
  clearAll: () => void;
  /** Scans the configured Ollama endpoint and merges discovered models. */
  scanModels: () => Promise<void>;
}

/* -------------------------------------------------------------------------- */
/* Internal helpers                                                           */
/* -------------------------------------------------------------------------- */

/**
 * Persists the Ollama settings snapshot to the dedicated localStorage vault.
 * Kept inline so every mutation writes the *full* state, matching the
 * original three handlers verbatim.
 */
function persistOllamaVault(state: OllamaSettings): void {
  writeLocalJson(STORAGE_KEYS.ollamaVault, state);
}

/* -------------------------------------------------------------------------- */
/* Hook                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Encapsulates every Ollama-related mutation:
 *   - manual add / remove of model IDs,
 *   - clearing the entire vault,
 *   - scanning the local Ollama endpoint for installed models.
 *
 * `scanningOllama` is owned by the hook because it is only meaningful in the
 * context of `scanModels()`; the caller simply reads it to render the
 * spinner state on the scan button.
 */
export function useOllamaModels({
  ollamaSettings,
  setOllamaSettings,
}: UseOllamaModelsParams): UseOllamaModelsResult {
  const [scanningOllama, setScanningOllama] = useState(false);

  /* -------------------------- Manual add -------------------------------- */

  const addModel = useCallback(
    (rawInput: string): boolean => {
      const trimmed = rawInput.trim();
      if (!trimmed) return false;

      const currentList = ollamaSettings.savedModels || [];
      if (currentList.includes(trimmed)) {
        showToast.info("Model Exists", "This model is already in Ollama list.");
        return false;
      }

      const updated = [...currentList, trimmed];
      const defaultModel = ollamaSettings.model || trimmed;

      setOllamaSettings((prev) => {
        const next: OllamaSettings = {
          ...prev,
          savedModels: updated,
          model: defaultModel,
        };
        persistOllamaVault(next);
        return next;
      });

      showToast.success("Ollama Model Added", trimmed);
      return true;
    },
    [ollamaSettings, setOllamaSettings]
  );

  /* -------------------------- Manual remove ----------------------------- */

  const removeModel = useCallback(
    (modelId: string) => {
      const currentList = ollamaSettings.savedModels || [];
      const updated = currentList.filter((m) => m !== modelId);
      const nextDefault =
        ollamaSettings.model === modelId
          ? updated[0] || ""
          : ollamaSettings.model;

      setOllamaSettings((prev) => {
        const next: OllamaSettings = {
          ...prev,
          savedModels: updated,
          model: nextDefault,
        };
        persistOllamaVault(next);
        return next;
      });

      showToast.info("Ollama Model Removed", modelId);
    },
    [ollamaSettings, setOllamaSettings]
  );

  /* -------------------------- Clear all --------------------------------- */

  const clearAll = useCallback(() => {
    setOllamaSettings((prev) => {
      const cleared: OllamaSettings = {
        ...prev,
        savedModels: [],
        model: "",
        status: "untested",
      };
      persistOllamaVault(cleared);
      return cleared;
    });

    showToast.info(
      "Ollama Cleared",
      "Removed all Ollama models and deactivated from engine selector."
    );
  }, [setOllamaSettings]);

  /* -------------------------- Scan endpoint ----------------------------- */

  const scanModels = useCallback(async () => {
    setScanningOllama(true);
    try {
      const url = resolveApiUrl("/api/config/fetch-models");

      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: ollamaSettings.baseUrl,
          api_key: "",
          provider_id: "ollama",
        }),
      });

      const data = await res.json();

      if (data.ok && Array.isArray(data.models) && data.models.length > 0) {
        if (data.models_metadata) {
          saveLocalMetadataVault(data.models_metadata);
        }

        const defaultModel = ollamaSettings.model || data.models[0];

        setOllamaSettings((prev) => {
          const next: OllamaSettings = {
            ...prev,
            savedModels: data.models as string[],
            model: defaultModel,
            status: "online",
          };
          persistOllamaVault(next);
          return next;
        });

        showToast.success(
          "Ollama Models Found",
          `Retrieved ${data.models.length} local Ollama models!`
        );
      } else {
        const msg =
          data.error ||
          data.message ||
          "No models returned from Ollama endpoint (Port 11434).";
        showToast.warning("Ollama Notice", msg);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      showToast.error("Ollama Scan Failed", message);
    } finally {
      setScanningOllama(false);
    }
  }, [ollamaSettings, setOllamaSettings]);

  return { scanningOllama, addModel, removeModel, clearAll, scanModels };
}
