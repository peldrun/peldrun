// frontend/src/app/settings/setup/tabs/llm/LLMTab.tsx
"use client";

import React, { useCallback, useEffect, useState } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import { ModelDiscoveryModal } from "../../ModelDiscoveryModal";
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

  const autoDetectActiveModelLimit = useCallback(() => {
    const meta = metadataVault[config.llm.model];
    if (meta && meta.max_context_length) {
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
        "Auto-Detected Context Window",
        `${config.llm.model}: ${detected.toLocaleString()} tokens`
      );
    } else {
      showToast.info(
        "No Metadata Found",
        "Using fallback default limit for active model."
      );
    }
  }, [config, metadataVault, setConfig]);

  /* -------------------------- LM Studio probe --------------------------- */

  const handleTestLMStudio = useCallback(async () => {
    setTestingId("lmstudio");
    try {
      const res = await testEndpoint(
        lmStudioSettings.baseUrl,
        lmStudioSettings.apiKey,
        lmStudioSettings.model,
        ""
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
      const res = await testEndpoint(
        testUrl,
        "",
        ollamaSettings.model || "test",
        ""
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

      showToast.success(
        "Health Check Completed",
        "All provider responses updated."
      );
    } finally {
      setTestingAll(false);
    }
  }, [cloudProviders, cloud, handleTestLMStudio, handleTestOllama]);

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
    (selectedModels: string[], primaryModel?: string) => {
      const target = discovery.discoveryTarget;
      if (!target) return;

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
          />
        )}

        {activeSubTab === "custom" && (
          <CustomEndpointsTab
            {...props}
            endpointForm={custom.endpointForm}
            setEndpointForm={custom.setEndpointForm}
            testingId={custom.testingId}
            handleTestCustomForm={custom.handleTestCustomForm}
            handleSaveCustomEndpoint={custom.handleSaveCustomEndpoint}
            handleDeleteCustomEndpoint={custom.handleDeleteCustomEndpoint}
            onOpenDiscovery={handleOpenDiscovery}
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
/>

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
          initialSavedModels={discovery.discoveryTarget.savedModels || []}
          onSaveModels={handleSaveDiscovered}
        />
      )}
    </div>
  );
}
