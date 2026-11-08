// frontend/src/app/settings/setup/tabs/llm/LLMTab.tsx
"use client";

import React, { useCallback, useEffect, useState } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import {
  ModelDiscoveryModal,
  type ModelsDiscoverySavePayload,
} from "../../ModelDiscoveryModal";
import {
  SavedModelsManagerModal,
  type SavedModelsSavePayload,
} from "./components/SavedModelsManagerModal";
import { ModelInfoModal } from "@/components/models/ModelInfoModal";
import {
  fetchServerMetadata,
  saveLocalMetadataVault,
} from "@/lib/modelMetadata";
import { ModelBudgetsTab } from "../ModelBudgetsTab";
import { useDiscoveryTarget } from "./hooks/useDiscoveryTarget";
import { useLMStudioModels } from "./hooks/useLMStudioModels";
import { useOllamaModels } from "./hooks/useOllamaModels";
import { useCloudProviders } from "./hooks/useCloudProviders";
import { useCustomEndpoints } from "./hooks/useCustomEndpoints";
import { LLMHeader } from "./components/LLMHeader";
import { OverviewTab } from "./components/OverviewTab";
import { LocalEnginesTab } from "./components/LocalEnginesTab";
import { CloudProvidersTab } from "./components/CloudProvidersTab";
import { StoreProvidersTab } from "./components/StoreProvidersTab";
import { CustomEndpointsTab } from "./components/CustomEndpointsTab";
import { sanitizeTokenInput, ensureOllamaV1, STORAGE_KEYS, writeLocalJson } from "./utils";
import type {
  CloudProviderVaultItem,
  CustomEndpointWithLogo,
  HubSubTab,
  LLMTabProps,
  ModelMetadataVault,
} from "./types";

/* -------------------------------------------------------------------------- */
/* Metadata persistence helper                                                */
/* -------------------------------------------------------------------------- */

/**
 * Merge the raw provider metadata with the catalog capability facts
 * collected by `ModelDiscoveryModal`, then persist the result to
 * `/api/config/models-metadata`.
 *
 * Background
 * ----------
 * `ModelDiscoveryModal` collects two parallel sources of truth during
 * a scan:
 *
 *   1. `metadata` — the raw `/api/config/fetch-models` response
 *      (`display_name`, `params_string`, `max_context_length`,
 *      `capabilities`, `capability_sources`, ...).
 *   2. `facts` — the catalog capability facts fetched asynchronously
 *      from `/api/catalog/models/{modelId}`, used by the modal to
 *      render the Reasoning / Vision / Tools badges.
 *
 * The modal's badge rendering overlays `facts` on top of
 * `metadata.capabilities` (see the "overlay only when the catalog is
 * definitive" block in `ModelDiscoveryModal.tsx`). Without this
 * helper, only `metadata` would reach `models_metadata.json` — the
 * catalog facts would be lost, and a subsequent `ModelInfoModal`
 * render would briefly disagree with what the user just saw in the
 * discovery modal.
 *
 * Merge rules
 * -----------
 * For each capability (`vision`, `trained_for_tool_use`, `reasoning`):
 *   - Overlay `facts[field].value` onto `capabilities[field]` ONLY
 *     when `facts[field].known === true` (matches the UI's overlay
 *     rule exactly).
 *   - Record `facts[field].source` (falling back to
 *     `"external_registry"`) into `capability_sources[field]`.
 *
 * The backend's `_merge_metadata_entries` then applies its own
 * preservation rules — any existing `manual` source on disk wins over
 * the incoming automated source, so a rescan never clobbers a
 * user-authored capability.
 *
 * Failure handling
 * ----------------
 * The HTTP call is best-effort: a network blip must never block the
 * vault + budget save path that already succeeded before this runs.
 */
async function persistDiscoveryMetadata(
  payload: ModelsDiscoverySavePayload
): Promise<void> {
  const { metadata, facts } = payload;

  /* Deep-copy the raw metadata so mutations below cannot leak back into
     the modal's React state (the payload is a live reference). */
  const merged: Record<string, Record<string, unknown>> = {};
  for (const [modelId, entry] of Object.entries(metadata || {})) {
    if (entry && typeof entry === "object") {
      merged[modelId] = JSON.parse(JSON.stringify(entry));
    }
  }

  /* Overlay catalog facts on top. */
  for (const [modelId, factsEntry] of Object.entries(facts || {})) {
    if (!factsEntry || typeof factsEntry !== "object") continue;
    if (!merged[modelId]) merged[modelId] = {};

    const entry = merged[modelId] as Record<string, unknown>;
    const capabilities = {
      ...((entry.capabilities as Record<string, unknown>) || {}),
    };
    const sources = {
      ...((entry.capability_sources as Record<string, unknown>) || {}),
    };

    const overlay = (
      field: "vision" | "tools" | "reasoning",
      targetField: string
    ): void => {
      const fact = (factsEntry as Record<string, any>)[field];
      if (!fact || !fact.known) return;
      capabilities[targetField] = fact.value;
      sources[targetField] = fact.source || "external_registry";
    };

    overlay("vision", "vision");
    overlay("tools", "trained_for_tool_use");
    overlay("reasoning", "reasoning");

    entry.capabilities = capabilities;
    entry.capability_sources = sources;
  }

  if (Object.keys(merged).length === 0) return;

  try {
    await fetch("/api/config/models-metadata", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ metadata: merged }),
    });
  } catch {
    /* Non-fatal: metadata persistence must never abort the save flow. */
  }
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Root container for the LLM settings surface.
 *
 * Responsibilities:
 *   - Own the small amount of top-level UI state that is not delegated to a
 *     hook (active sub-tab, generic LM/Ollama testing indicator, model-info
 *     modal, metadata vault mirror, and the "testing all" flag).
 *   - Wire together the five domain hooks (discovery / LM Studio / Ollama /
 *     cloud providers / custom endpoints).
 *   - Route between the five sub-tabs and forward every required prop.
 *   - Host the two overlays (model info + model discovery modals) exactly as
 *     the original monolithic component did.
 *
 * Business logic, persistence, and derived values are intentionally kept in
 * the hooks so that each sub-tab stays purely presentational.
 */
export function LLMTab(props: LLMTabProps) {
  const {
    config,
    setConfig,
    lmStudioSettings,
    setLmStudioSettings,
    ollamaSettings,
    setOllamaSettings,
    cloudProviders,
    setCloudProviders,
    customEndpoints,
    setCustomEndpoints,
    availableModels,
    setAvailableModels,
    fetchingModels,
    handleFetchModels,
    assignDetectedModel,
    activateEngine,
    deactivateToDefault,
    testEndpoint,
  } = props;

  /* -------------------------- Top-level UI state ------------------------ */

  const [activeSubTab, setActiveSubTab] = useState<HubSubTab>("overview");
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testingAll, setTestingAll] = useState(false);
  const [newLMModelInput, setNewLMModelInput] = useState("");
  const [newOllamaModelInput, setNewOllamaModelInput] = useState("");
  const [infoModalModel, setInfoModalModel] = useState<string | null>(null);
  const [metadataVault, setMetadataVault] = useState<ModelMetadataVault>({});

  /**
   * Target provider whose saved-models list is currently being managed
   * via `SavedModelsManagerModal`. Two kinds are handled by this tab:
   *
   *   * `"cloud"` — a `CloudProviderVaultItem` from `cloud_vault`.
   *     Persistence goes through `setCloudProviders` +
   *     `writeLocalJson(STORAGE_KEYS.cloudVault)` (mirrors the
   *     `handleSaveDiscovered` path exactly).
   *
   *   * `"custom"` — a `CustomEndpoint` from `custom_endpoints`.
   *     Persistence goes through `custom.applyDiscoveredModels`, which
   *     already handles the localStorage write internally.
   *
   * `"package"` is NOT represented here — packages are managed
   * exclusively from `StoreProvidersTab`, which owns its own modal
   * state (see file 6). Keeping the kinds disjoint avoids an
   * accidental overwrite across the two storage backends
   * (`cloud_vault` / `custom_endpoints` vs `provider_package_models`).
   */
  type SavedModelsManagerTarget = {
    kind: "cloud" | "custom";
    id: string;
    name: string;
    savedModels: string[];
    defaultModel?: string;
  };

  const [savedModelsManagerTarget, setSavedModelsManagerTarget] =
    useState<SavedModelsManagerTarget | null>(null);

  /* -------------------------- Domain hooks ------------------------------ */

  const discovery = useDiscoveryTarget();

  const lmModels = useLMStudioModels({
    availableModels,
    setAvailableModels,
    lmStudioSettings,
  });

  const ollamaModels = useOllamaModels({
    ollamaSettings,
    setOllamaSettings,
  });

  const cloud = useCloudProviders({
    cloudProviders,
    setCloudProviders,
    testEndpoint,
  });

  const custom = useCustomEndpoints({
    customEndpoints,
    setCustomEndpoints,
    testEndpoint,
  });

  /* -------------------------- Metadata vault ---------------------------- */

  useEffect(() => {
    fetchServerMetadata().then((data) =>
      setMetadataVault((data || {}) as ModelMetadataVault)
    );

    const onUpdate = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      setMetadataVault(detail || {});
    };

    window.addEventListener("omweb:metadata-updated", onUpdate);
    return () => window.removeEventListener("omweb:metadata-updated", onUpdate);
  }, []);

  /* -------------------------- Derived values ---------------------------- */

  // [FIX-OVERVIEW-BUDGET] Read the active model's authoritative budget from
  // `models_budget.json` (the "Per-Model Context Windows" tab source of
  // truth) instead of only `config.toml`. Previously, when a user set a
  // per-model max_output_tokens of 4096, the Overview panel still displayed
  // the global config.toml fallback (1500), making the two panels
  // contradict each other.
  const [activeModelBudget, setActiveModelBudget] = useState<{
    context_window?: number;
    max_output_tokens?: number;
    temperature?: number;
  } | null>(null);

  useEffect(() => {
    const activeModel = config.llm.model;
    if (!activeModel) {
      setActiveModelBudget(null);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/config/model-budgets", {
          cache: "no-store",
        });
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (!cancelled && data?.ok && data.budgets) {
          setActiveModelBudget(data.budgets[activeModel] || null);
        }
      } catch {
        /* Keep null → fallback to config.toml values. */
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [config.llm.model]);

  const effectiveContextWindow =
    activeModelBudget?.context_window ??
    config.llm.context_window ??
    config.llm.max_tokens ??
    8192;

  const effectiveMaxOutput =
    activeModelBudget?.max_output_tokens ??
    config.llm.max_output_tokens ??
    1500;

  const effectiveInputBudget = Math.max(
    100,
    effectiveContextWindow - effectiveMaxOutput
  );

  /* -------------------------- Context auto-detect ----------------------- */

  const autoDetectActiveModelLimit = useCallback(async () => {
    const modelId = config.llm.model;
    if (!modelId) {
      showToast.warning("No active model", "Select a model first.");
      return;
    }

    // Resolve the metadata endpoint for the CURRENTLY active provider by
    // looking it up across the four provider stores. The user configured
    // it in the UI, so we honor it here.
    const activeProvider = (config.llm.provider || "").toLowerCase();
    let metadataEndpoint = "";
    if (activeProvider === "lmstudio") {
      metadataEndpoint = lmStudioSettings.metadataEndpoint || "";
    } else if (activeProvider === "ollama") {
      metadataEndpoint = ollamaSettings.metadataEndpoint || "";
    } else {
      const cloud = cloudProviders.find((p) => p.id === config.llm.provider);
      if (cloud) {
        metadataEndpoint = cloud.metadataEndpoint || "";
      } else {
        const custom = customEndpoints.find(
          (p) =>
            p.providerId === config.llm.provider || p.id === config.llm.provider
        );
        if (custom) metadataEndpoint = custom.metadataEndpoint || "";
      }
    }

    try {
      const res = await fetch("/api/config/fetch-models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: config.llm.base_url,
          api_key: config.llm.api_key || "",
          provider_id: config.llm.provider || "",
          provider_type: config.llm.api_type || "",
          metadata_endpoint: metadataEndpoint,
        }),
      });

      if (res.ok) {
        const data = await res.json();
        if (data.ok && data.models_metadata) {
          setMetadataVault((prev) => ({ ...prev, ...data.models_metadata }));
          const meta = data.models_metadata[modelId];
          if (meta && typeof meta.max_context_length === "number") {
            const detected = sanitizeTokenInput(meta.max_context_length, 8192);
            setConfig({
              ...config,
              llm: {
                ...config.llm,
                context_window: detected,
                max_tokens: detected,
              },
            });
            showToast.success(
              "Detected from provider API",
              `${modelId}: ${detected.toLocaleString()} tokens`
            );
            return;
          }
        }
      }
    } catch {
      /* fall through to cache */
    }

    // Fallback: check cached metadata
    const cached = metadataVault[modelId];
    if (cached && cached.max_context_length) {
      const detected = sanitizeTokenInput(cached.max_context_length, 8192);
      setConfig({
        ...config,
        llm: { ...config.llm, context_window: detected, max_tokens: detected },
      });
      showToast.success(
        "From cached metadata",
        `${modelId}: ${detected.toLocaleString()} tokens`
      );
      return;
    }

    showToast.warning(
      "Provider did not return context",
      `${modelId}: set context manually in Model Budgets.`
    );
  }, [
    config,
    metadataVault,
    setConfig,
    lmStudioSettings.metadataEndpoint,
    ollamaSettings.metadataEndpoint,
    cloudProviders,
    customEndpoints,
  ]);

  /* -------------------------- LM Studio probe --------------------------- */

  const handleTestLMStudio = useCallback(async () => {
    setTestingId("lmstudio");
    try {
      // Forward the user's explicit metadata endpoint so the backend
      // probe hits the URL they configured (e.g. ".../api/v1/models")
      // instead of a URL computed from `baseUrl`. Without this, a base
      // URL ending in `/api/v1` produced the double-suffixed
      // `/api/api/v0/models` path visible in the LM Studio server log.
      const res = await testEndpoint(
        lmStudioSettings.baseUrl,
        lmStudioSettings.apiKey,
        lmStudioSettings.model,
        "",
        lmStudioSettings.metadataEndpoint
      );

      setLmStudioSettings((prev) => ({
        ...prev,
        status: res.ok ? "online" : "offline",
        latency: res.latency,
        lastError: res.ok ? undefined : res.message,
      }));

      if (res.ok) {
        showToast.success(
          "LM Studio Connected",
          `Verified in ${res.latency} ms`,
          res.latency
        );
      } else {
        showToast.error("LM Studio Unreachable", res.message);
      }
    } finally {
      setTestingId(null);
    }
  }, [lmStudioSettings, setLmStudioSettings, testEndpoint]);

  /* -------------------------- Ollama probe ------------------------------ */

  const handleTestOllama = useCallback(async () => {
    setTestingId("ollama");
    const testUrl = ensureOllamaV1(ollamaSettings.baseUrl);
    try {
      // Ollama exposes a native metadata endpoint (typically
      // ".../api/tags"). Forward it verbatim when the user configured
      // one; the backend falls back to computing an endpoint from
      // `testUrl` otherwise.
      const res = await testEndpoint(
        testUrl,
        "",
        ollamaSettings.model || "test",
        "",
        ollamaSettings.metadataEndpoint
      );

      setOllamaSettings((prev) => ({
        ...prev,
        status: res.ok ? "online" : "offline",
        latency: res.latency,
        lastError: res.ok ? undefined : res.message,
      }));

      if (res.ok) {
        showToast.success(
          "Ollama Connected",
          `Verified in ${res.latency} ms`,
          res.latency
        );
      } else {
        showToast.error("Ollama Unreachable", res.message);
      }
    } finally {
      setTestingId(null);
    }
  }, [ollamaSettings, setOllamaSettings, testEndpoint]);

  /* -------------------------- Test all ---------------------------------- */

  const handleTestAll = useCallback(async () => {
    setTestingAll(true);
    showToast.info(
      "Health Check Initiated",
      "Testing all configured AI endpoints..."
    );

    try {
      await handleTestLMStudio();
      await handleTestOllama();

      for (const cp of cloudProviders) {
        if (cp.apiKey.trim()) {
          await cloud.handleTestCloud(cp.id);
        }
      }

      // [FIX-TEST-ALL] Custom endpoints were previously skipped by the
      // bulk health check. Iterate them here so the ConnectivityMatrix
      // reflects real status for every configured provider.
      for (const ce of customEndpoints) {
        await custom.handleTestCustomEndpoint(ce.id);
      }

      showToast.success(
        "Health Check Completed",
        "All provider responses updated."
      );
    } finally {
      setTestingAll(false);
    }
  }, [
    cloudProviders,
    cloud,
    customEndpoints,
    custom,
    handleTestLMStudio,
    handleTestOllama,
  ]);

  /* -------------------------- Model list wrappers ----------------------- */

  const handleAddLMModel = useCallback(() => {
    if (lmModels.addModel(newLMModelInput)) {
      setNewLMModelInput("");
    }
  }, [lmModels, newLMModelInput]);

  const handleAddOllamaModel = useCallback(() => {
    if (ollamaModels.addModel(newOllamaModelInput)) {
      setNewOllamaModelInput("");
    }
  }, [ollamaModels, newOllamaModelInput]);

  /* -------------------------- Discovery persistence --------------------- */

  const handleSaveDiscovered = useCallback(
    (payload: ModelsDiscoverySavePayload) => {
      const target = discovery.discoveryTarget;
      if (!target) return;

      /* Defensive normalization of the incoming payload. The modal
         always passes an array (built via `Array.from(Set)`), but a
         legacy caller or a transient state could theoretically hand
         us a non-iterable value. Guard here so the downstream
         `savedModels` in the cloud-provider state is ALWAYS a real
         array, which is what `CloudProviderCard` and the backend
         expect. */
      const selectedModels = Array.isArray(payload.models)
        ? payload.models
        : [];
      const primaryModel = payload.defaultModel;

      if (target.isCustom) {
        custom.applyDiscoveredModels(
          target.id,
          selectedModels,
          primaryModel
        );
      } else {
        setCloudProviders((prev: CloudProviderVaultItem[]) => {
          const next = prev.map((cp) =>
            cp.id === target.id
              ? {
                  ...cp,
                  savedModels: selectedModels,
                  model:
                    primaryModel ||
                    (selectedModels.length > 0
                      ? selectedModels[0]
                      : cp.model),
                }
              : cp
          );

          /* Mirror the cloud vault snapshot to localStorage, matching the
             original inline behaviour. */
          try {
            const stored = JSON.parse(
              window.localStorage.getItem(STORAGE_KEYS.cloudVault) || "[]"
            );
            const updated = stored.map((cp: CloudProviderVaultItem) =>
              cp.id === target.id
                ? {
                    ...cp,
                    savedModels: selectedModels,
                    model:
                      primaryModel ||
                      (selectedModels.length > 0
                        ? selectedModels[0]
                        : cp.model),
                  }
                : cp
            );
            writeLocalJson(STORAGE_KEYS.cloudVault, updated);
          } catch {
            /* Non-fatal: localStorage may be unavailable. */
          }

          return next;
        });
      }

      /* NEW: persist the enriched metadata (provider scan + catalog
         facts) to `storage/models_metadata.json`.

         This is the write that was missing before — the modal
         collected metadata + facts, but `handleSaveDiscovered` only
         forwarded `models` / `primaryModel` and silently dropped the
         rest, so `models_metadata.json` stayed stale after every
         save. Failures are swallowed inside the helper and never
         abort the vault/budget save that just completed. */
      void persistDiscoveryMetadata(payload);

      showToast.success(
        "Models Saved",
        `Saved ${selectedModels.length} models for ${target.name}`
      );
      discovery.closeDiscovery();
    },
    [discovery, custom, setCloudProviders]
  );

  /* -------------------------- Discovery opener -------------------------- */

  const handleOpenDiscovery = useCallback(
    (target: Parameters<typeof discovery.openDiscovery>[0]) => {
      discovery.openDiscovery(target);
    },
    [discovery]
  );

  /* -------------------------- Saved Models Manager ---------------------- */

  /**
   * Open the Saved Models Manager for a cloud provider.
   *
   * Normalizes `savedModels` defensively: a legacy vault entry may hold
   * a non-array value (see `CloudProviderCard.tsx`'s defensive comment).
   * Passing a non-iterable to `new Set(...)` inside the modal would
   * throw at render time.
   */
  const handleOpenCloudModelsManager = useCallback(
    (providerId: string) => {
      const cp = cloudProviders.find((p) => p.id === providerId);
      if (!cp) return;
      setSavedModelsManagerTarget({
        kind: "cloud",
        id: cp.id,
        name: cp.name,
        savedModels: Array.isArray(cp.savedModels) ? cp.savedModels : [],
        defaultModel: cp.model,
      });
    },
    [cloudProviders]
  );

  /**
   * Open the Saved Models Manager for a custom endpoint.
   *
   * The endpoint's `defaultModel` is passed as the initial default —
   * even when `savedModels` does not yet contain it. This mirrors the
   * `ModelDiscoveryModal` behavior, where `primaryModel` survives even
   * if the user unchecks it from the saved list.
   */
  const handleOpenCustomModelsManager = useCallback(
    (endpointId: string) => {
      const ce = customEndpoints.find((c) => c.id === endpointId);
      if (!ce) return;
      setSavedModelsManagerTarget({
        kind: "custom",
        id: ce.id,
        name: ce.name,
        savedModels: Array.isArray(ce.savedModels) ? ce.savedModels : [],
        defaultModel: ce.defaultModel,
      });
    },
    [customEndpoints]
  );

  /**
   * Persist the changes from `SavedModelsManagerModal`.
   *
   * Follows the exact same two-phase approach as `handleSaveDiscovered`:
   *
   *   1. Update React state (`setCloudProviders` or
   *      `custom.applyDiscoveredModels`) so the UI reflects the new
   *      list immediately.
   *
   *   2. Mirror the write to localStorage so the "Save Config" button
   *      in the sidebar (which reads from `cloudProviders` /
   *      `customEndpoints` state, not from the backend vault) sends
   *      the updated lists to `models_vault.json` on next click.
   *
   * The `savedModels` field is DELIBERATELY allowed to become empty.
   * Unlike the discovery modal — where "save with zero selections" is
   * a user error — the manager's "Keep None" action is a legitimate
   * cleanup: the user is removing every model and keeping only the
   * `defaultModel` as a placeholder.
   */
  const handleSaveSavedModels = useCallback(
    (payload: SavedModelsSavePayload) => {
      const target = savedModelsManagerTarget;
      if (!target) return;

      const selectedModels = Array.isArray(payload.models)
        ? payload.models
        : [];
      const primaryModel = payload.defaultModel;

      if (target.kind === "custom") {
        /* `applyDiscoveredModels` already persists to localStorage
           (`STORAGE_KEYS.customEndpoints`) and updates React state.
           Reusing it keeps the write path identical between discovery
           and management, eliminating a class of "one saves, the other
           doesn't" bugs. */
        custom.applyDiscoveredModels(
          target.id,
          selectedModels,
          primaryModel
        );
      } else {
        /* Cloud providers have no equivalent helper — mirror the
           `handleSaveDiscovered` cloud branch verbatim to preserve
           behavioral parity. */
        setCloudProviders((prev: CloudProviderVaultItem[]) => {
          const next = prev.map((cp) =>
            cp.id === target.id
              ? {
                  ...cp,
                  savedModels: selectedModels,
                  model:
                    primaryModel ||
                    (selectedModels.length > 0
                      ? selectedModels[0]
                      : cp.model),
                }
              : cp
          );

          try {
            const stored = JSON.parse(
              window.localStorage.getItem(STORAGE_KEYS.cloudVault) || "[]"
            );
            const updated = stored.map((cp: CloudProviderVaultItem) =>
              cp.id === target.id
                ? {
                    ...cp,
                    savedModels: selectedModels,
                    model:
                      primaryModel ||
                      (selectedModels.length > 0
                        ? selectedModels[0]
                        : cp.model),
                  }
                : cp
            );
            writeLocalJson(STORAGE_KEYS.cloudVault, updated);
          } catch {
            /* Non-fatal: localStorage may be unavailable. */
          }

          return next;
        });
      }

      showToast.success(
        "Saved Models Updated",
        `${selectedModels.length} model(s) kept for ${target.name}`
      );
      setSavedModelsManagerTarget(null);
    },
    [savedModelsManagerTarget, custom, setCloudProviders]
  );

  /* -------------------------- Render ------------------------------------ */

  return (
    <div className="llm-tab w-full font-sans pb-6">
      {/* Header + sub-tab navigation */}
      <LLMHeader
        activeSubTab={activeSubTab}
        onSubTabChange={(tab) => setActiveSubTab(tab)}
      />

      {/* Sub-tab routing */}
      <div className="pt-6">
        {activeSubTab === "overview" && (
          <OverviewTab
            {...props}
            metadataVault={metadataVault}
            autoDetectActiveModelLimit={autoDetectActiveModelLimit}
            effectiveContextWindow={effectiveContextWindow}
            effectiveMaxOutput={effectiveMaxOutput}
            effectiveInputBudget={effectiveInputBudget}
            sanitizeTokenInput={sanitizeTokenInput}
            handleTestLMStudio={handleTestLMStudio}
            handleTestOllama={handleTestOllama}
            handleTestCloud={cloud.handleTestCloud}
            handleTestAll={handleTestAll}
            testingAll={testingAll}
            testingId={testingId}
          />
        )}

        {activeSubTab === "lmstudio" && (
          <LocalEnginesTab
            {...props}
            metadataVault={metadataVault}
            newLMModelInput={newLMModelInput}
            setNewLMModelInput={setNewLMModelInput}
            newOllamaModelInput={newOllamaModelInput}
            setNewOllamaModelInput={setNewOllamaModelInput}
            scanningOllama={ollamaModels.scanningOllama}
            testingId={testingId}
            onOpenModelInfo={(model) => setInfoModalModel(model)}
            handleTestLMStudio={handleTestLMStudio}
            handleTestOllama={handleTestOllama}
            handleAddLMModel={handleAddLMModel}
            handleRemoveLMModel={lmModels.removeModel}
            handleAddOllamaModel={handleAddOllamaModel}
            handleRemoveOllamaModel={ollamaModels.removeModel}
            handleClearOllama={ollamaModels.clearAll}
            handleScanOllama={ollamaModels.scanModels}
          />
        )}

        {activeSubTab === "cloud" && (
          <CloudProvidersTab
            {...props}
            testingId={cloud.testingId}
            showKeys={cloud.showKeys}
            toggleKey={cloud.toggleKey}
            handleTestCloud={cloud.handleTestCloud}
            onOpenDiscovery={handleOpenDiscovery}
            onManageModels={handleOpenCloudModelsManager}
          />
        )}

        {activeSubTab === "store_providers" && (
          <StoreProvidersTab
            activateEngine={activateEngine}
            activeProviderId={config.llm.provider}
          />
        )}

        {activeSubTab === "custom" && (
          <CustomEndpointsTab
            {...props}
            endpointForm={custom.endpointForm}
            setEndpointForm={custom.setEndpointForm}
            testingId={custom.testingId}
            handleTestCustomForm={custom.handleTestCustomForm}
            handleTestCustomEndpoint={custom.handleTestCustomEndpoint}
            handleSaveCustomEndpoint={custom.handleSaveCustomEndpoint}
            handleDeleteCustomEndpoint={custom.handleDeleteCustomEndpoint}
            onOpenDiscovery={handleOpenDiscovery}
            onManageCustomModels={handleOpenCustomModelsManager}
          />
        )}

        {activeSubTab === "budgets" && (
          <ModelBudgetsTab
            availableModels={availableModels}
            lmStudioSettings={lmStudioSettings}
            ollamaSettings={ollamaSettings}
            cloudProviders={cloudProviders}
            customEndpoints={customEndpoints}
          />
        )}
      </div>

      {/* Model info modal */}
        {/* Model info modal */}
        <ModelInfoModal
        isOpen={Boolean(infoModalModel)}
        onClose={() => setInfoModalModel(null)}
        modelKey={infoModalModel || ""}
        explicitMeta={
            infoModalModel
            ? (metadataVault[infoModalModel] as Parameters<typeof ModelInfoModal>[0]["explicitMeta"])
            : null
        }
        providerId={config.llm.provider}
/>

      {/* Saved Models Manager modal — opens from "Manage" buttons in
          CloudProvidersTab and CustomEndpointsTab. Rendered before the
          discovery modal so it appears on top of any open provider
          card list without overlapping the discovery flow (both use
          z-50, but React renders them in JSX order; the manager is
          only mounted when explicitly requested). */}
      {savedModelsManagerTarget && (
        <SavedModelsManagerModal
          isOpen={true}
          onClose={() => setSavedModelsManagerTarget(null)}
          providerId={savedModelsManagerTarget.id}
          providerName={savedModelsManagerTarget.name}
          providerKind={savedModelsManagerTarget.kind}
          savedModels={savedModelsManagerTarget.savedModels}
          defaultModel={savedModelsManagerTarget.defaultModel}
          onSaveModels={handleSaveSavedModels}
        />
      )}

      {/* Model discovery modal */}
      {discovery.discoveryTarget && (
        <ModelDiscoveryModal
          isOpen={Boolean(discovery.discoveryTarget)}
          onClose={discovery.closeDiscovery}
          providerId={discovery.discoveryTarget.id}
          providerName={discovery.discoveryTarget.name}
          baseUrl={discovery.discoveryTarget.baseUrl}
          apiKey={discovery.discoveryTarget.apiKey}
          providerType={discovery.discoveryTarget.type}
          // The user's explicit metadata endpoint from the vault,
          // forwarded so the discovery query hits the URL they chose
          // instead of a URL derived from `baseUrl`. Undefined for
          // targets that do not set it; the modal then sends an empty
          // string and the backend uses its native adapter discovery
          // path (which probes /api/v1/models first).
          metadataEndpoint={discovery.discoveryTarget.metadataEndpoint}
          initialSavedModels={discovery.discoveryTarget.savedModels || []}
          onSaveModels={handleSaveDiscovered}
        />
      )}
    </div>
  );
}
