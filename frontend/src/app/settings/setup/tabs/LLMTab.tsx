"use client";

import React, { useState, useMemo } from "react";
import {
  Sparkles, Layers, Info, Brain, Wrench, Database, RefreshCw, Check, AlertTriangle,
  Zap, Bot, Plus, ArrowUpCircle, Trash2, Eye, EyeOff, Sliders,
  Search, CheckCircle2, ShieldCheck, KeyRound, Globe, Server, Radio,
  HelpCircle, XCircle, ShieldAlert, Cpu, Cloud, Terminal, RotateCcw,
  X, CheckSquare, Gauge
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/ToastNotification";
import { ModelDiscoveryModal } from "../ModelDiscoveryModal";
import { ModelInfoModal } from "@/components/models/ModelInfoModal";
import { inferModelCapabilities, saveLocalMetadataVault, fetchServerMetadata } from "@/lib/modelMetadata";

import { ModelBudgetsTab } from "./ModelBudgetsTab";

import type {
  HubSubTab,
  CustomEndpoint,
  CloudProviderVaultItem,
  LMStudioSettings,
  OllamaSettings,
  FullAppConfig,
  ApiModeType
} from "../types";

interface LLMTabProps {
  config: FullAppConfig;
  setConfig: React.Dispatch<React.SetStateAction<FullAppConfig>>;
  lmStudioSettings: LMStudioSettings;
  setLmStudioSettings: React.Dispatch<React.SetStateAction<LMStudioSettings>>;
  ollamaSettings: OllamaSettings;
  setOllamaSettings: React.Dispatch<React.SetStateAction<OllamaSettings>>;
  cloudProviders: CloudProviderVaultItem[];
  setCloudProviders: React.Dispatch<React.SetStateAction<CloudProviderVaultItem[]>>;
  customEndpoints: CustomEndpoint[];
  setCustomEndpoints: React.Dispatch<React.SetStateAction<CustomEndpoint[]>>;
  availableModels: string[];
  setAvailableModels: React.Dispatch<React.SetStateAction<string[]>>;
  fetchingModels: boolean;
  scanError: string | null;
  handleFetchModels: () => void;
  assignDetectedModel: (model: string, target: "primary" | "vision") => void;
  activateEngine: (providerId: string, providerName: string, model: string, baseUrl: string, apiKey: string, apiType: string) => void;
  deactivateToDefault: () => void;
  testEndpoint: (baseUrl: string, apiKey: string, model: string, apiType?: string) => Promise<{ ok: boolean; message: string; latency?: number }>;
}

const EMPTY_ENDPOINT_FORM = {
  name: "",
  providerId: "",
  endpointUrl: "",
  apiMode: "Auto-detect" as ApiModeType,
  defaultModel: "",
  contextWindow: "Auto",
  apiKey: "",
  useForNewChats: true,
};

export function LLMTab({
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
  scanError,
  handleFetchModels,
  assignDetectedModel,
  activateEngine,
  deactivateToDefault,
  testEndpoint,
}: LLMTabProps) {
  const [activeSubTab, setActiveSubTab] = useState<HubSubTab>("overview");
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testingAll, setTestingAll] = useState(false);
  const [newLMModelInput, setNewLMModelInput] = useState("");
  const [newOllamaModelInput, setNewOllamaModelInput] = useState("");
  const [scanningOllama, setScanningOllama] = useState(false);
  const [infoModalModel, setInfoModalModel] = useState<string | null>(null);
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});

  React.useEffect(() => {
    fetchServerMetadata().then((data) => setMetadataVault(data || {}));
    const onUpdate = (e: any) => setMetadataVault(e.detail || {});
    window.addEventListener("omweb:metadata-updated", onUpdate);
    return () => window.removeEventListener("omweb:metadata-updated", onUpdate);
  }, []);

  React.useEffect(() => { fetchServerMetadata(); }, []);

  // Model Discovery Target State (For Cloud & Custom Endpoints)
  const [discoveryTarget, setDiscoveryTarget] = useState<{
    id: string;
    name: string;
    baseUrl: string;
    apiKey: string;
    type?: string;
    savedModels?: string[];
    isCustom?: boolean;
  } | null>(null);

  // Clean form state initialized with empty values
  const [endpointForm, setEndpointForm] = useState(EMPTY_ENDPOINT_FORM);

  const [showKeys, setShowKeys] = useState<{ [id: string]: boolean }>({});

  const toggleKey = (id: string) => {
    setShowKeys((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const sanitizeTokenInput = (raw: any, fallback: number = 8192): number => {
    const clean = parseInt(String(raw).replace(/[^0-9]/g, ""), 10);
    if (isNaN(clean) || clean <= 0) return fallback;
    if (clean > 2000000) return 2000000;
    return clean;
  };

  // Dynamic context and output limits resolution
  const effectiveContextWindow = config.llm.context_window || config.llm.max_tokens || 8192;
  const effectiveMaxOutput = config.llm.max_output_tokens || 1500;
  const effectiveInputBudget = Math.max(100, effectiveContextWindow - effectiveMaxOutput);

  const autoDetectActiveModelLimit = () => {
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
      showToast.success("Auto-Detected Context Window", `${config.llm.model}: ${detected.toLocaleString()} tokens`);
    } else {
      showToast.info("No Metadata Found", "Using fallback default limit for active model.");
    }
  };

  // ---------------- LM Studio Handlers ----------------
  const handleAddLMModel = () => {
    const trimmed = newLMModelInput.trim();
    if (!trimmed) return;
    if (availableModels.includes(trimmed)) {
      showToast.info("Model Exists", "This model is already in the list.");
      return;
    }
    const updated = [...availableModels, trimmed];
    setAvailableModels(updated);
    if (typeof window !== "undefined") {
      localStorage.setItem("omweb_scanned_models", JSON.stringify(updated));
      const sl = localStorage.getItem("omweb_lmstudio_vault");
      const vault = sl ? JSON.parse(sl) : { ...lmStudioSettings };
      vault.savedModels = updated;
      localStorage.setItem("omweb_lmstudio_vault", JSON.stringify(vault));
    }
    setNewLMModelInput("");
    showToast.success("Model Added", trimmed);
  };

  const handleRemoveLMModel = (modelToRemove: string) => {
    const updated = availableModels.filter((m) => m !== modelToRemove);
    setAvailableModels(updated);
    if (typeof window !== "undefined") {
      localStorage.setItem("omweb_scanned_models", JSON.stringify(updated));
      const sl = localStorage.getItem("omweb_lmstudio_vault");
      const vault = sl ? JSON.parse(sl) : { ...lmStudioSettings };
      vault.savedModels = updated;
      localStorage.setItem("omweb_lmstudio_vault", JSON.stringify(vault));
    }
    showToast.info("Model Removed", modelToRemove);
  };

  const handleTestLMStudio = async () => {
    setTestingId("lmstudio");
    try {
      const res = await testEndpoint(lmStudioSettings.baseUrl, lmStudioSettings.apiKey, lmStudioSettings.model, "");
      setLmStudioSettings((prev) => ({
        ...prev,
        status: res.ok ? "online" : "offline",
        latency: res.latency,
        lastError: res.ok ? undefined : res.message
      }));
      if (res.ok) {
        showToast.success("LM Studio Connected", `Verified in ${res.latency} ms`, res.latency);
      } else {
        showToast.error("LM Studio Unreachable", res.message);
      }
    } finally {
      setTestingId(null);
    }
  };

  // ---------------- Ollama Handlers ----------------
  const handleTestOllama = async () => {
    setTestingId("ollama");
    const testUrl = ollamaSettings.baseUrl.endsWith("/v1") ? ollamaSettings.baseUrl : `${ollamaSettings.baseUrl}/v1`;
    try {
      const res = await testEndpoint(testUrl, "", ollamaSettings.model || "test", "");
      setOllamaSettings((prev) => ({
        ...prev,
        status: res.ok ? "online" : "offline",
        latency: res.latency,
        lastError: res.ok ? undefined : res.message
      }));
      if (res.ok) {
        showToast.success("Ollama Connected", `Verified in ${res.latency} ms`, res.latency);
      } else {
        showToast.error("Ollama Unreachable", res.message);
      }
    } finally {
      setTestingId(null);
    }
  };

  const handleScanOllama = async () => {
    setScanningOllama(true);
    try {
      const fetchModelsUrl = typeof window !== "undefined" && (window.location.port === "3088" || window.location.port === "3000")
        ? "http://localhost:8088/api/config/fetch-models"
        : "/api/config/fetch-models";

      const res = await fetch(fetchModelsUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: ollamaSettings.baseUrl,
          api_key: "",
          provider_id: "ollama"
        })
      });
      const data = await res.json();
      if (data.ok && Array.isArray(data.models) && data.models.length > 0) {
        if (data.models_metadata) saveLocalMetadataVault(data.models_metadata);
        const defaultMod = ollamaSettings.model || data.models[0];
        setOllamaSettings((prev) => {
          const updated: OllamaSettings = {
            ...prev,
            savedModels: data.models,
            model: defaultMod,
            status: "online" as const
          };
          if (typeof window !== "undefined") {
            localStorage.setItem("omweb_ollama_vault", JSON.stringify(updated));
          }
          return updated;
        });
        showToast.success("Ollama Models Found", `Retrieved ${data.models.length} local Ollama models!`);
      } else {
        const msg = data.error || data.message || "No models returned from Ollama endpoint (Port 11434).";
        showToast.warning("Ollama Notice", msg);
      }
    } catch (e: any) {
      showToast.error("Ollama Scan Failed", e.message);
    } finally {
      setScanningOllama(false);
    }
  };

  const handleAddOllamaModel = () => {
    const trimmed = newOllamaModelInput.trim();
    if (!trimmed) return;
    const currentList = ollamaSettings.savedModels || [];
    if (currentList.includes(trimmed)) {
      showToast.info("Model Exists", "This model is already in Ollama list.");
      return;
    }
    const updated = [...currentList, trimmed];
    const defaultM = ollamaSettings.model || trimmed;
    setOllamaSettings((prev) => {
      const state = { ...prev, savedModels: updated, model: defaultM };
      if (typeof window !== "undefined") {
        localStorage.setItem("omweb_ollama_vault", JSON.stringify(state));
      }
      return state;
    });
    setNewOllamaModelInput("");
    showToast.success("Ollama Model Added", trimmed);
  };

  const handleRemoveOllamaModel = (modelToRemove: string) => {
    const currentList = ollamaSettings.savedModels || [];
    const updated = currentList.filter((m) => m !== modelToRemove);
    const newDefault = ollamaSettings.model === modelToRemove ? (updated[0] || "") : ollamaSettings.model;
    setOllamaSettings((prev) => {
      const state = { ...prev, savedModels: updated, model: newDefault };
      if (typeof window !== "undefined") {
        localStorage.setItem("omweb_ollama_vault", JSON.stringify(state));
      }
      return state;
    });
    showToast.info("Ollama Model Removed", modelToRemove);
  };

  const handleClearOllama = () => {
    setOllamaSettings((prev) => {
      const cleared = { ...prev, savedModels: [], model: "", status: "untested" as const };
      if (typeof window !== "undefined") {
        localStorage.setItem("omweb_ollama_vault", JSON.stringify(cleared));
      }
      return cleared;
    });
    showToast.info("Ollama Cleared", "Removed all Ollama models and deactivated from engine selector.");
  };

  // ---------------- Cloud & Custom Discovery Handlers ----------------
  const handleSaveDiscovered = (selectedModels: string[], primaryModel?: string) => {
    if (!discoveryTarget) return;

    if (discoveryTarget.isCustom) {
      setCustomEndpoints((prev) =>
        prev.map((ce) =>
          ce.id === discoveryTarget.id
            ? {
                ...ce,
                savedModels: selectedModels,
                defaultModel: primaryModel || (selectedModels.length > 0 ? selectedModels[0] : ce.defaultModel)
              }
            : ce
        )
      );
      if (typeof window !== "undefined") {
        try {
          const stored = JSON.parse(localStorage.getItem("omweb_custom_endpoints") || "[]");
          const updated = stored.map((ce: any) =>
            ce.id === discoveryTarget.id
              ? {
                  ...ce,
                  savedModels: selectedModels,
                  defaultModel: primaryModel || (selectedModels.length > 0 ? selectedModels[0] : ce.defaultModel)
                }
              : ce
          );
          localStorage.setItem("omweb_custom_endpoints", JSON.stringify(updated));
        } catch (e) {}
      }
    } else {
      setCloudProviders((prev) =>
        prev.map((cp) =>
          cp.id === discoveryTarget.id
            ? {
                ...cp,
                savedModels: selectedModels,
                model: primaryModel || (selectedModels.length > 0 ? selectedModels[0] : cp.model)
              }
            : cp
        )
      );
      if (typeof window !== "undefined") {
        try {
          const stored = JSON.parse(localStorage.getItem("omweb_cloud_vault") || "[]");
          const updated = stored.map((cp: any) =>
            cp.id === discoveryTarget.id
              ? {
                  ...cp,
                  savedModels: selectedModels,
                  model: primaryModel || (selectedModels.length > 0 ? selectedModels[0] : cp.model)
                }
              : cp
          );
          localStorage.setItem("omweb_cloud_vault", JSON.stringify(updated));
        } catch (e) {}
      }
    }

    showToast.success("Models Saved", `Saved ${selectedModels.length} models for ${discoveryTarget.name}`);
    setDiscoveryTarget(null);
  };

  const handleTestCloud = async (providerId: string) => {
    const cp = cloudProviders.find((p) => p.id === providerId);
    if (!cp) return;

    if (!cp.apiKey.trim()) {
      showToast.warning(`${cp.name} API Key Required`, "Please enter an API key before testing connection.");
      return;
    }

    setTestingId(providerId);
    try {
      const res = await testEndpoint(cp.baseUrl, cp.apiKey, cp.model, cp.type);
      setCloudProviders((prev) =>
        prev.map((item) =>
          item.id === providerId
            ? { ...item, status: res.ok ? "online" : "offline", latency: res.latency, lastError: res.ok ? undefined : res.message }
            : item
        )
      );

      if (res.ok) {
        showToast.success(`${cp.name} Verified`, `Active and responsive (${res.latency} ms)`, res.latency);
      } else {
        showToast.error(`${cp.name} Connection Failed`, res.message);
      }
    } finally {
      setTestingId(null);
    }
  };

  const handleTestAll = async () => {
    setTestingAll(true);
    showToast.info("Health Check Initiated", "Testing all configured AI endpoints...");
    try {
      await handleTestLMStudio();
      await handleTestOllama();
      for (const cp of cloudProviders) {
        if (cp.apiKey.trim()) {
          await handleTestCloud(cp.id);
        }
      }
      showToast.success("Health Check Completed", "All provider responses updated.");
    } finally {
      setTestingAll(false);
    }
  };

  const handleTestCustomForm = async () => {
    setTestingId("custom_form");
    try {
      const res = await testEndpoint(endpointForm.endpointUrl, endpointForm.apiKey, endpointForm.defaultModel, "");
      if (res.ok) {
        showToast.success("Custom Endpoint Connected", `Latency: ${res.latency} ms`, res.latency);
      } else {
        showToast.error("Custom Endpoint Failed", res.message);
      }
    } finally {
      setTestingId(null);
    }
  };

  const handleSaveCustomEndpoint = () => {
    if (!endpointForm.name.trim() || !endpointForm.endpointUrl.trim()) {
      showToast.warning("Missing Fields", "Name and Endpoint URL are required.");
      return;
    }
    const newEndpoint: CustomEndpoint = {
      id: `custom_${Date.now()}`,
      name: endpointForm.name.trim(),
      providerId: endpointForm.providerId.trim() || endpointForm.name.toLowerCase().replace(/\s+/g, "-"),
      endpointUrl: endpointForm.endpointUrl.trim(),
      apiMode: endpointForm.apiMode,
      defaultModel: endpointForm.defaultModel.trim() || "default",
      contextWindow: endpointForm.contextWindow || "Auto",
      apiKey: endpointForm.apiKey.trim(),
      status: "untested",
      useForNewChats: endpointForm.useForNewChats,
      savedModels: []
    };

    setCustomEndpoints((prev) => [newEndpoint, ...prev]);
    setEndpointForm(EMPTY_ENDPOINT_FORM);
    showToast.success("Custom Endpoint Saved", newEndpoint.name);
  };

  const handleDeleteCustomEndpoint = (id: string) => {
    setCustomEndpoints((prev) => prev.filter((item) => item.id !== id));
    showToast.info("Endpoint Removed", "Custom endpoint removed.");
  };

  return (
    <div className="llm-tab w-full space-y-6 max-auto font-sans pb-6">
      {/* Header */}
      <div className="border-b border-border w-full pb-0 pt-4 bg-custom">
        <div className="flex items-center gap-2 pr-4 pl-4">
          <Sparkles size={16} className="text-primary" />
          <h2 className="text-sm font-semibold font-heading text-foreground uppercase tracking-wide">
            Model Hub & Multi-Provider Architecture
          </h2>
        </div>
        <p className="text-xs text-muted-foreground mt-0.5 pr-4 pl-4">
          Dedicated engines for local GPUs, Ollama, cloud credentials, custom endpoints, and role routing.
        </p>

        {/* 4 Professional Sub-Tabs */}
        <div className="flex items-center gap-2 mt-4 border-b border-border/60 w-full bg-card m-auto max-auto">
          <button
            type="button"
            onClick={() => setActiveSubTab("overview")}
            className={`flex items-center gap-1.5 px-3 py-2 text-xs font-semibold border-b-2 transition-all cursor-pointer ${
              activeSubTab === "overview"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Radio size={13} />
            <span>Overview & Ops</span>
          </button>

          <button
            type="button"
            onClick={() => setActiveSubTab("lmstudio")}
            className={`flex items-center gap-1.5 px-3 py-2 text-xs font-semibold border-b-2 transition-all cursor-pointer ${
              activeSubTab === "lmstudio"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Cpu size={13} />
            <span>Local Engines (LM Studio & Ollama)</span>
          </button>

          <button
            type="button"
            onClick={() => setActiveSubTab("cloud")}
            className={`flex items-center gap-1.5 px-3 py-2 text-xs font-semibold border-b-2 transition-all cursor-pointer ${
              activeSubTab === "cloud"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Cloud size={13} />
            <span>Cloud Providers</span>
          </button>

          <button
            type="button"
            onClick={() => setActiveSubTab("custom")}
            className={`flex items-center gap-1.5 px-3 py-2 text-xs font-semibold border-b-2 transition-all cursor-pointer ${
              activeSubTab === "custom"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Terminal size={13} />
            <span>Custom Endpoints</span>
          </button>


            <button
              type="button"
              onClick={() => setActiveSubTab("budgets")}
              className={`flex items-center gap-1.5 px-3 py-2 text-xs font-semibold border-b-2 transition-all cursor-pointer ${
                activeSubTab === "budgets"
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              <Sliders size={13} />
              <span>Model Budgets & Context</span>
            </button>


        </div>
      </div>

      {/* ========================================================================= */}
      {/* SUB-TAB 1: OVERVIEW & OPS */}
      {/* ========================================================================= */}
      {activeSubTab === "overview" && (
        <div className="w-full max-w-7xl space-y-9 pt-8 m-auto">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="p-4 rounded-md border border-primary/50 bg-primary/5 space-y-2 shadow-xs ring-1 ring-primary/20">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-bold text-primary uppercase tracking-wider block">
                  Primary Autonomous Engine [llm]
                </span>
                <span className="flex items-center gap-1 text-[10px] font-bold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" /> Active
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm font-bold font-mono text-foreground">{config.llm.model}</span>
                <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-card border border-border text-foreground">
                  {config.llm.provider_name || config.llm.provider}
                </span>
              </div>
              <span className="text-[10px] text-muted-foreground font-mono block truncate">
                {config.llm.base_url}
              </span>
            </div>

            <div className="p-4 rounded-md border border-border bg-card space-y-2 shadow-xs">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider block">
                  Visual Perception Engine [llm.vision]
                </span>
                <span className="text-[10px] font-bold text-violet-500 bg-violet-500/10 px-2 py-0.5 rounded border border-violet-500/20">
                  Perception
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm font-bold font-mono text-foreground">{config.llm_vision.model}</span>
                <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-muted text-muted-foreground">
                  {config.llm_vision.provider_name || config.llm_vision.provider}
                </span>
              </div>
              <span className="text-[10px] text-muted-foreground font-mono block truncate">
                {config.llm_vision.base_url}
              </span>
            </div>
          </div>

          <div className="p-5 rounded-md border border-border bg-card space-y-4 shadow-sm">
            <div className="flex items-center justify-between border-b border-border/60 pb-3">
              <div>
                <span className="text-xs font-bold text-foreground uppercase tracking-wider block">
                  Connectivity Health Matrix
                </span>
                <span className="text-[10px] text-muted-foreground">
                  Truthful live verification. Switch active primary with one click.
                </span>
              </div>

              <Button
                variant="outline"
                size="sm"
                onClick={handleTestAll}
                disabled={testingAll}
                className="h-7 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs flex items-center gap-1.5"
              >
                <RefreshCw size={11} className={`text-primary ${testingAll ? "animate-spin" : ""}`} />
                <span>{testingAll ? "Pinging Endpoints..." : "Test All Endpoints"}</span>
              </Button>
            </div>

            <div className="divide-y divide-border/60 text-xs">
              {/* Row: LM Studio */}
              {(() => {
                const isPrimary = config.llm.provider === "lmstudio" || config.llm.base_url === lmStudioSettings.baseUrl;
                return (
                  <div className="py-2.5 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="relative flex h-2.5 w-2.5">
                        {lmStudioSettings.status === "online" ? (
                          <span className="inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500" />
                        ) : lmStudioSettings.status === "offline" ? (
                          <span className="inline-flex rounded-full h-2.5 w-2.5 bg-rose-500" />
                        ) : (
                          <span className="inline-flex rounded-full h-2.5 w-2.5 bg-amber-400" />
                        )}
                      </span>
                      <span className="font-semibold text-foreground">LM Studio (Local GPU)</span>
                      <span className="text-[10px] text-muted-foreground font-mono">({lmStudioSettings.model})</span>
                    </div>

                    <div className="flex items-center gap-2">
                      {lmStudioSettings.status === "online" && (
                        <span className="text-[10px] text-emerald-600 dark:text-emerald-400 font-mono font-semibold">
                          {lmStudioSettings.latency} ms
                        </span>
                      )}

                      {isPrimary ? (
                        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30">
                          Active Primary
                        </span>
                      ) : (
                        <button
                          onClick={() =>
                            activateEngine("lmstudio", "LM Studio (Local)", lmStudioSettings.model, lmStudioSettings.baseUrl, lmStudioSettings.apiKey, "")
                          }
                          className="px-2 py-0.5 rounded text-[10px] bg-primary/10 hover:bg-primary/20 text-primary font-medium cursor-pointer"
                        >
                          Activate
                        </button>
                      )}
                    </div>
                  </div>
                );
              })()}

              {/* Row: Ollama */}
              {(() => {
                const isPrimary = config.llm.provider === "ollama";
                return (
                  <div className="py-2.5 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="relative flex h-2.5 w-2.5">
                        {ollamaSettings.status === "online" ? (
                          <span className="inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500" />
                        ) : ollamaSettings.status === "offline" ? (
                          <span className="inline-flex rounded-full h-2.5 w-2.5 bg-rose-500" />
                        ) : (
                          <span className="inline-flex rounded-full h-2.5 w-2.5 bg-amber-400" />
                        )}
                      </span>
                      <span className="font-semibold text-foreground">Ollama (Local Server)</span>
                      <span className="text-[10px] text-muted-foreground font-mono">({ollamaSettings.model || "Not configured"})</span>
                    </div>

                    <div className="flex items-center gap-2">
                      {ollamaSettings.status === "online" && (
                        <span className="text-[10px] text-emerald-600 dark:text-emerald-400 font-mono font-semibold">
                          {ollamaSettings.latency} ms
                        </span>
                      )}

                      {isPrimary ? (
                        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30">
                          Active Primary
                        </span>
                      ) : (
                        ollamaSettings.model && (
                          <button
                            onClick={() => {
                              const b = ollamaSettings.baseUrl.endsWith("/v1") ? ollamaSettings.baseUrl : `${ollamaSettings.baseUrl}/v1`;
                              activateEngine("ollama", "Ollama (Local)", ollamaSettings.model, b, "", "");
                            }}
                            className="px-2 py-0.5 rounded text-[10px] bg-primary/10 hover:bg-primary/20 text-primary font-medium cursor-pointer"
                          >
                            Activate
                          </button>
                        )
                      )}
                    </div>
                  </div>
                );
              })()}

              {/* Rows: Cloud Providers */}
              {cloudProviders.map((cp) => {
                const isPrimary = config.llm.provider === cp.id || config.llm.base_url === cp.baseUrl;
                return (
                  <div key={cp.id} className="py-2.5 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="relative flex h-2.5 w-2.5">
                        {cp.status === "online" ? (
                          <span className="inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500" />
                        ) : cp.status === "offline" ? (
                          <span className="inline-flex rounded-full h-2.5 w-2.5 bg-rose-500" />
                        ) : (
                          <span className="inline-flex rounded-full h-2.5 w-2.5 bg-amber-400" />
                        )}
                      </span>
                      <span className="font-semibold text-foreground">{cp.name}</span>
                      <span className="text-[10px] text-muted-foreground font-mono">({cp.model})</span>
                    </div>

                    <div className="flex items-center gap-2">
                      {cp.status === "online" && (
                        <span className="text-[10px] text-emerald-600 dark:text-emerald-400 font-mono font-semibold">
                          {cp.latency} ms
                        </span>
                      )}

                      {isPrimary ? (
                        <div className="flex items-center gap-1.5">
                          <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30">
                            Active Primary
                          </span>
                          <button
                            onClick={deactivateToDefault}
                            className="px-2 py-0.5 rounded text-[10px] bg-muted hover:bg-muted/80 text-muted-foreground hover:text-foreground flex items-center gap-1 cursor-pointer"
                            title="Deactivate and revert to local LM Studio"
                          >
                            <RotateCcw size={10} />
                            <span>Revert</span>
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() =>
                            activateEngine(cp.id, cp.name, cp.model, cp.baseUrl, cp.apiKey, cp.type)
                          }
                          className="px-2 py-0.5 rounded text-[10px] bg-primary/10 hover:bg-primary/20 text-primary font-medium cursor-pointer"
                        >
                          Activate
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}

              {/* Rows: Custom Endpoints */}
              {customEndpoints.map((ce) => {
                const isPrimary = config.llm.base_url === ce.endpointUrl;
                return (
                  <div key={ce.id} className="py-2.5 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="h-2.5 w-2.5 rounded-full bg-slate-400" />
                      <span className="font-semibold text-foreground">{ce.name}</span>
                      <span className="text-[10px] text-muted-foreground font-mono">({ce.defaultModel})</span>
                    </div>

                    {isPrimary ? (
                      <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30">
                        Active Primary
                      </span>
                    ) : (
                      <button
                        onClick={() =>
                          activateEngine(ce.providerId, ce.name, ce.defaultModel, ce.endpointUrl, ce.apiKey, "")
                        }
                        className="px-2 py-0.5 rounded text-[10px] bg-primary/10 hover:bg-primary/20 text-primary font-medium cursor-pointer"
                      >
                        Activate
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Hyperparameters & Context Budget Panel */}
          <div className="p-5 rounded-md border border-border bg-card space-y-4 shadow-xs">
            <div className="flex items-center justify-between border-b border-border/60 pb-3">
              <div className="flex items-center gap-2">
                <Gauge size={16} className="text-primary" />
                <span className="text-xs font-bold text-foreground uppercase tracking-wider">
                  Context Window & Token Budget Architecture
                </span>
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={autoDetectActiveModelLimit}
                className="h-6 text-[11px] border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs flex items-center gap-1.5"
              >
                <Sparkles size={11} className="text-amber-500" />
                <span>Auto-detect from Model</span>
              </Button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <label className="text-[11px] font-medium text-muted-foreground block mb-1">
                  Context Window (Total Capacity)
                </label>
                <input
                  type="number"
                  value={effectiveContextWindow}
                  onChange={(e) => {
                    const val = sanitizeTokenInput(e.target.value, 8192);
                    setConfig({
                      ...config,
                      llm: { ...config.llm, context_window: val, max_tokens: val }
                    });
                  }}
                  className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground font-mono shadow-xs"
                />
                <span className="text-[10px] text-muted-foreground mt-0.5 block">
                  Total available token window (Inputs + Outputs)
                </span>
              </div>

              <div>
                <label className="text-[11px] font-medium text-muted-foreground block mb-1">
                  Max Output Tokens (Completion Budget)
                </label>
                <input
                  type="number"
                  value={effectiveMaxOutput}
                  onChange={(e) => {
                    const val = sanitizeTokenInput(e.target.value, 1500);
                    setConfig({
                      ...config,
                      llm: { ...config.llm, max_output_tokens: val }
                    });
                  }}
                  className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground font-mono shadow-xs"
                />
                <span className="text-[10px] text-muted-foreground mt-0.5 block">
                  Reserved token buffer strictly for LLM generation
                </span>
              </div>

              <div>
                <label className="text-[11px] font-medium text-muted-foreground block mb-1">
                  Temperature: {Number(config.llm.temperature).toFixed(2)}
                </label>
                <input
                  type="range"
                  min="0.0"
                  max="1.5"
                  step="0.05"
                  value={config.llm.temperature}
                  onChange={(e) => setConfig({ ...config, llm: { ...config.llm, temperature: parseFloat(e.target.value) || 0 } })}
                  className="w-full accent-primary cursor-pointer mt-2"
                />
                <span className="text-[10px] text-muted-foreground mt-0.5 block">
                  Deterministic (0.0) to Creative (1.5)
                </span>
              </div>
            </div>

            {/* Live Effective Context Budget Calculation Card */}
            <div className="p-3.5 rounded-lg border border-border/80 bg-background/50 space-y-2.5">
              <div className="flex items-center justify-between text-xs">
                <span className="font-semibold text-foreground flex items-center gap-1.5">
                  <Sliders size={13} className="text-primary" /> Effective Context Input Budget:
                </span>
                <span className="font-mono font-bold text-primary">
                  {effectiveInputBudget.toLocaleString()} tokens
                </span>
              </div>

              {/* Proportional Budget Bar */}
              <div className="w-full h-3 rounded bg-muted overflow-hidden flex shadow-inner">
                <div style={{ width: "10%" }} className="bg-sky-500" title="System Prompt: 10%" />
                <div style={{ width: "15%" }} className="bg-amber-500" title="Summary: 15%" />
                <div style={{ width: "50%" }} className="bg-emerald-500" title="Recent Turns: 50%" />
                <div style={{ width: "15%" }} className="bg-violet-500" title="Execution State: 15%" />
                <div style={{ width: "10%" }} className="bg-rose-500" title="Current Prompt: 10%" />
              </div>

              <div className="flex flex-wrap items-center justify-between text-[10px] text-muted-foreground font-mono gap-1 pt-0.5">
                <span className="flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full bg-sky-500" /> System (10%): {Math.round(effectiveInputBudget * 0.1)}
                </span>
                <span className="flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full bg-amber-500" /> Summary (15%): {Math.round(effectiveInputBudget * 0.15)}
                </span>
                <span className="flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full bg-emerald-500" /> Recent (50%): {Math.round(effectiveInputBudget * 0.5)}
                </span>
                <span className="flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full bg-violet-500" /> Exec (15%): {Math.round(effectiveInputBudget * 0.15)}
                </span>
                <span className="flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full bg-rose-500" /> Current (10%): {Math.round(effectiveInputBudget * 0.1)}
                </span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* SUB-TAB 2: LOCAL ENGINES (LM STUDIO & OLLAMA) */}
      {/* ========================================================================= */}
      {activeSubTab === "lmstudio" && (
        <div className="w-full max-w-7xl space-y-9 pt-8 m-auto">
          {/* 1. LM STUDIO CARD */}
          <div className="p-5 rounded-md border border-border bg-card space-y-4 shadow-sm">
            <div className="flex items-center justify-between border-b border-border/60 pb-3">
              <div className="flex items-center gap-2">
                <Cpu size={16} className="text-primary" />
                <span className="text-xs font-bold text-foreground uppercase tracking-wider">
                  LM Studio Local Server (OpenAI-compatible)
                </span>
              </div>
              <span className="text-[11px] text-muted-foreground font-mono">Port: 1234 (Local)</span>
            </div>

            <div className="space-y-3 text-xs">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="text-[11px] font-medium text-muted-foreground block mb-1">
                    Base URL override
                  </label>
                  <input
                    type="text"
                    value={lmStudioSettings.baseUrl}
                    onChange={(e) => {
                      const val = e.target.value;
                      setLmStudioSettings({ ...lmStudioSettings, baseUrl: val });
                      if (config.llm.provider === "lmstudio") {
                        setConfig((p) => ({ ...p, llm: { ...p.llm, base_url: val } }));
                      }
                    }}
                    placeholder="http://127.0.0.1:1234/v1"
                    className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground font-mono shadow-xs"
                  />
                </div>

                <div>
                  <label className="text-[11px] font-medium text-muted-foreground block mb-1">
                    API Key (Optional for auth)
                  </label>
                  <input
                    type="password"
                    value={lmStudioSettings.apiKey}
                    onChange={(e) => {
                      const val = e.target.value;
                      setLmStudioSettings({ ...lmStudioSettings, apiKey: val });
                      if (config.llm.provider === "lmstudio") {
                        setConfig((p) => ({ ...p, llm: { ...p.llm, api_key: val } }));
                      }
                    }}
                    placeholder="Leave blank for standard local server"
                    className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground font-mono shadow-xs"
                  />
                </div>
              </div>

              <div>
                <label className="text-[11px] font-medium text-muted-foreground block mb-1">
                  Default LM Studio Model
                </label>
                <input
                  type="text"
                  value={lmStudioSettings.model}
                  onChange={(e) => {
                    const val = e.target.value;
                    setLmStudioSettings({ ...lmStudioSettings, model: val });
                    if (config.llm.provider === "lmstudio") {
                      setConfig((p) => ({ ...p, llm: { ...p.llm, model: val } }));
                    }
                  }}
                  className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground font-mono shadow-xs"
                />
              </div>

              <div className="flex items-center justify-between pt-2">
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleTestLMStudio}
                    disabled={testingId === "lmstudio"}
                    className="h-7 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs"
                  >
                    <Zap size={11} className={`text-amber-500 mr-1 ${testingId === "lmstudio" ? "animate-spin" : ""}`} />
                    <span>{testingId === "lmstudio" ? "Testing..." : "Test Connection"}</span>
                  </Button>

                  {lmStudioSettings.status === "online" && (
                    <span className="text-[11px] text-emerald-600 dark:text-emerald-400 font-semibold flex items-center gap-1">
                      <CheckCircle2 size={12} /> Connected ({lmStudioSettings.latency} ms)
                    </span>
                  )}
                  {lmStudioSettings.status === "offline" && (
                    <span className="text-[11px] text-rose-500 flex items-center gap-1 font-medium">
                      <XCircle size={12} /> {lmStudioSettings.lastError || "Unreachable"}
                    </span>
                  )}
                </div>

                <Button
                  onClick={() =>
                    activateEngine("lmstudio", "LM Studio (Local)", lmStudioSettings.model, lmStudioSettings.baseUrl, lmStudioSettings.apiKey, "")
                  }
                  className="Primary Button h-7 text-xs bg-primary text-primary-foreground font-medium cursor-pointer shadow-xs"
                >
                  Set as Active Primary
                </Button>
              </div>
            </div>

            {/* Loaded LM Studio Models Sub-card */}
            <div className="p-3.5 rounded-lg border border-border/80 bg-background/50 space-y-3 pt-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Layers size={14} className="text-primary" />
                  <span className="text-xs font-semibold text-foreground">
                    Loaded GPU Models ({availableModels.length})
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  {availableModels.length > 0 && (
                    <button
                      type="button"
                      onClick={() => {
                        setAvailableModels([]);
                        if (typeof window !== "undefined") {
                          localStorage.removeItem("omweb_scanned_models");
                        }
                        showToast.info("List Cleared", "LM Studio models list cleared.");
                      }}
                      className="text-[11px] text-muted-foreground hover:text-rose-500 cursor-pointer"
                    >
                      Clear All
                    </button>
                  )}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleFetchModels}
                    disabled={fetchingModels}
                    className="h-6 text-[11px] border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs"
                  >
                    <RefreshCw size={10} className={`mr-1 ${fetchingModels ? "animate-spin" : ""}`} />
                    <span>Scan Models</span>
                  </Button>
                </div>
              </div>

              {/* Add manual model */}
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={newLMModelInput}
                  onChange={(e) => setNewLMModelInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleAddLMModel()}
                  placeholder="Add local model ID (e.g. qwen3-vl-8b)..."
                  className="flex-1 bg-background border border-border rounded px-2.5 py-1 text-xs text-foreground font-mono placeholder:text-muted-foreground shadow-xs"
                />
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={handleAddLMModel}
                  disabled={!newLMModelInput.trim()}
                  className="h-6 text-[11px] cursor-pointer"
                >
                  <Plus size={10} className="mr-1" /> Add
                </Button>
              </div>

              {availableModels.length > 0 && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-48 overflow-y-auto p-0.5">
                  {availableModels.map((m) => {
                    const isCurrentPrimary = config.llm.provider === "lmstudio" && config.llm.model === m;
                    const caps = inferModelCapabilities(m, metadataVault[m]);
                    return (
                      <div
                        key={m}
                        className={`p-2.5 rounded-lg border text-xs font-mono flex flex-col gap-1.5 transition-all ${
                          isCurrentPrimary ? "bg-primary/10 border-primary/40 shadow-xs" : "bg-card border-border/70 hover:border-border"
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-1.5 truncate flex-1 pr-2">
                            <span className="truncate font-semibold text-foreground" title={m}>{m}</span>
                            <button
                              type="button"
                              onClick={() => setInfoModalModel(m)}
                              className="p-1 rounded text-muted-foreground hover:text-primary hover:bg-muted transition cursor-pointer"
                              title="View Model Technical Specs & Capabilities"
                            >
                              <Info size={12} />
                            </button>
                          </div>

                          <div className="flex items-center gap-1 shrink-0 font-sans">
                            <button
                              type="button"
                              onClick={() => assignDetectedModel(m, "primary")}
                              className={`px-1.5 py-0.5 rounded text-[10px] font-semibold cursor-pointer ${
                                isCurrentPrimary ? "bg-emerald-500 text-white" : "bg-primary/15 text-primary hover:bg-primary/25"
                              }`}
                            >
                              {isCurrentPrimary ? "Active" : "Primary"}
                            </button>
                            <button
                              type="button"
                              onClick={() => assignDetectedModel(m, "vision")}
                              className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-violet-500/15 text-violet-500 hover:bg-violet-500/25 cursor-pointer"
                            >
                              Vision
                            </button>
                            <button
                              type="button"
                              onClick={() => handleRemoveLMModel(m)}
                              className="p-1 rounded text-muted-foreground hover:text-rose-500 cursor-pointer"
                            >
                              <Trash2 size={11} />
                            </button>
                          </div>
                        </div>

                        {/* Exterior Badges */}
                        <div className="flex flex-wrap items-center gap-1 font-sans">
                          {caps.isReasoning && (
                            <span className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.2 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
                              <Brain size={9} /> Reasoning
                            </span>
                          )}
                          {caps.isVision && (
                            <span className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.2 rounded bg-violet-500/10 text-violet-600 dark:text-violet-400 border border-violet-500/20">
                              <Eye size={9} /> Vision
                            </span>
                          )}
                          {caps.isTools && (
                            <span className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.2 rounded bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-500/20">
                              <Wrench size={9} /> Tools
                            </span>
                          )}
                          {caps.paramsDisplay && (
                            <span className="text-[9px] font-bold px-1.5 py-0.2 rounded bg-primary/15 text-primary border border-primary/30 font-mono">
                              {caps.paramsDisplay}
                            </span>
                          )}
                          {caps.quantDisplay && (
                            <span className="text-[9px] px-1.5 py-0.2 rounded bg-muted text-muted-foreground border border-border font-mono">
                              {caps.quantDisplay}
                            </span>
                          )}
                          {caps.contextDisplay && (
                            <span className="text-[9px] px-1.5 py-0.2 rounded bg-muted text-muted-foreground border border-border font-mono">
                              {caps.contextDisplay}
                            </span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          {/* 2. OLLAMA LOCAL SERVER CARD */}
          <div className="p-5 rounded-md border border-border bg-card space-y-4 shadow-sm">
            <div className="flex items-center justify-between border-b border-border/60 pb-3">
              <div className="flex items-center gap-2">
                <Terminal size={16} className="text-amber-500" />
                <span className="text-xs font-bold text-foreground uppercase tracking-wider">
                  Ollama Local Server
                </span>
                <span className="text-[9px] px-1.5 py-0.2 rounded bg-muted text-muted-foreground font-mono">
                  Port: 11434 (Local)
                </span>
              </div>
              <div className="flex items-center gap-2">
                {ollamaSettings.status === "online" && (
                  <span className="text-[11px] text-emerald-600 dark:text-emerald-400 font-semibold flex items-center gap-1">
                    <CheckCircle2 size={12} /> Connected ({ollamaSettings.latency} ms)
                  </span>
                )}
                {ollamaSettings.status === "offline" && (
                  <span className="text-[11px] text-rose-500 flex items-center gap-1 font-medium">
                    <XCircle size={12} /> {ollamaSettings.lastError || "Unreachable"}
                  </span>
                )}
              </div>
            </div>

            <div className="space-y-3 text-xs">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="text-[11px] font-medium text-muted-foreground block mb-1">
                    Ollama Base URL
                  </label>
                  <input
                    type="text"
                    value={ollamaSettings.baseUrl}
                    onChange={(e) => {
                      const val = e.target.value;
                      setOllamaSettings({ ...ollamaSettings, baseUrl: val });
                      if (config.llm.provider === "ollama") {
                        const b = val.endsWith("/v1") ? val : `${val}/v1`;
                        setConfig((p) => ({ ...p, llm: { ...p.llm, base_url: b } }));
                      }
                    }}
                    placeholder="http://127.0.0.1:11434"
                    className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground font-mono shadow-xs"
                  />
                </div>

                <div>
                  <label className="text-[11px] font-medium text-muted-foreground block mb-1">
                    Default Ollama Model
                  </label>
                  <input
                    type="text"
                    value={ollamaSettings.model}
                    onChange={(e) => {
                      const val = e.target.value;
                      setOllamaSettings({ ...ollamaSettings, model: val });
                      if (config.llm.provider === "ollama") {
                        setConfig((p) => ({ ...p, llm: { ...p.llm, model: val } }));
                      }
                    }}
                    placeholder="e.g. llama3.2, qwen2.5, deepseek-r1"
                    className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground font-mono shadow-xs"
                  />
                </div>
              </div>

              <div className="flex items-center justify-between pt-1">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleTestOllama}
                  disabled={testingId === "ollama"}
                  className="h-7 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs"
                >
                  <Zap size={11} className={`text-amber-500 mr-1 ${testingId === "ollama" ? "animate-spin" : ""}`} />
                  <span>{testingId === "ollama" ? "Testing..." : "Test Connection"}</span>
                </Button>

                {ollamaSettings.model && (
                  <Button
                    onClick={() => {
                      const b = ollamaSettings.baseUrl.endsWith("/v1") ? ollamaSettings.baseUrl : `${ollamaSettings.baseUrl}/v1`;
                      activateEngine("ollama", "Ollama (Local)", ollamaSettings.model, b, "", "");
                    }}
                    className="h-7 text-xs bg-amber-600 hover:bg-amber-700 text-white font-medium cursor-pointer shadow-xs"
                  >
                    Set as Active Primary
                  </Button>
                )}
              </div>
            </div>

            {/* Discovered Ollama Models Sub-card */}
            <div className="p-3.5 rounded-lg border border-border/80 bg-background/50 space-y-3 pt-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Layers size={14} className="text-amber-500" />
                  <span className="text-xs font-semibold text-foreground">
                    Ollama Models ({ollamaSettings.savedModels?.length || 0})
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  {ollamaSettings.savedModels && ollamaSettings.savedModels.length > 0 && (
                    <button
                      type="button"
                      onClick={handleClearOllama}
                      className="text-[11px] text-muted-foreground hover:text-rose-500 cursor-pointer"
                      title="Clear Ollama models and remove from engine selector"
                    >
                      Clear All
                    </button>
                  )}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleScanOllama}
                    disabled={scanningOllama}
                    className="h-6 text-[11px] border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs"
                  >
                    <RefreshCw size={10} className={`mr-1 ${scanningOllama ? "animate-spin" : ""}`} />
                    <span>Scan Models</span>
                  </Button>
                </div>
              </div>

              {/* Add manual Ollama model */}
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={newOllamaModelInput}
                  onChange={(e) => setNewOllamaModelInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleAddOllamaModel()}
                  placeholder="Add Ollama model ID (e.g. deepseek-r1:7b)..."
                  className="flex-1 bg-background border border-border rounded px-2.5 py-1 text-xs text-foreground font-mono placeholder:text-muted-foreground shadow-xs"
                />
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={handleAddOllamaModel}
                  disabled={!newOllamaModelInput.trim()}
                  className="h-6 text-[11px] cursor-pointer"
                >
                  <Plus size={10} className="mr-1" /> Add
                </Button>
              </div>

              {ollamaSettings.savedModels && ollamaSettings.savedModels.length > 0 ? (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-48 overflow-y-auto p-0.5">
                  {ollamaSettings.savedModels.map((m) => {
                    const isCurrentPrimary = config.llm.provider === "ollama" && config.llm.model === m;
                    const caps = inferModelCapabilities(m, metadataVault[m]);
                    return (
                      <div
                        key={m}
                        className={`p-2.5 rounded-lg border text-xs font-mono flex flex-col gap-1.5 transition-all ${
                          isCurrentPrimary ? "bg-amber-500/10 border-amber-500/40 shadow-xs" : "bg-card border-border/70 hover:border-border"
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-1.5 truncate flex-1 pr-2">
                            <span className="truncate font-semibold text-foreground" title={m}>{m}</span>
                            <button
                              type="button"
                              onClick={() => setInfoModalModel(m)}
                              className="p-1 rounded text-muted-foreground hover:text-amber-500 hover:bg-muted transition cursor-pointer"
                              title="View Model Technical Specs & Capabilities"
                            >
                              <Info size={12} />
                            </button>
                          </div>

                          <div className="flex items-center gap-1 shrink-0 font-sans">
                            <button
                              type="button"
                              onClick={() => {
                                const b = ollamaSettings.baseUrl.endsWith("/v1") ? ollamaSettings.baseUrl : `${ollamaSettings.baseUrl}/v1`;
                                setOllamaSettings((p) => ({ ...p, model: m }));
                                activateEngine("ollama", "Ollama (Local)", m, b, "", "");
                              }}
                              className={`px-1.5 py-0.5 rounded text-[10px] font-semibold cursor-pointer ${
                                isCurrentPrimary ? "bg-emerald-500 text-white" : "bg-amber-500/15 text-amber-600 dark:text-amber-400 hover:bg-amber-500/25"
                              }`}
                            >
                              {isCurrentPrimary ? "Active" : "Primary"}
                            </button>
                            <button
                              type="button"
                              onClick={() => handleRemoveOllamaModel(m)}
                              className="p-1 rounded text-muted-foreground hover:text-rose-500 cursor-pointer"
                            >
                              <Trash2 size={11} />
                            </button>
                          </div>
                        </div>

                        {/* Exterior Badges */}
                        <div className="flex flex-wrap items-center gap-1 font-sans">
                          {caps.isReasoning && (
                            <span className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.2 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
                              <Brain size={9} /> Reasoning
                            </span>
                          )}
                          {caps.isVision && (
                            <span className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.2 rounded bg-violet-500/10 text-violet-600 dark:text-violet-400 border border-violet-500/20">
                              <Eye size={9} /> Vision
                            </span>
                          )}
                          {caps.isTools && (
                            <span className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.2 rounded bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-500/20">
                              <Wrench size={9} /> Tools
                            </span>
                          )}
                          {caps.paramsDisplay && (
                            <span className="text-[9px] px-1.5 py-0.2 rounded bg-muted text-muted-foreground border border-border font-mono">
                              {caps.paramsDisplay}
                            </span>
                          )}
                          {caps.quantDisplay && (
                            <span className="text-[9px] px-1.5 py-0.2 rounded bg-muted text-muted-foreground border border-border font-mono">
                              {caps.quantDisplay}
                            </span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="py-4 text-center text-xs text-muted-foreground bg-muted/20 border border-dashed border-border rounded-lg">
                  <span>No Ollama models found. Start Ollama and click &quot;Scan Models&quot; to detect local models.</span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* SUB-TAB 3: CLOUD PROVIDERS */}
      {/* ========================================================================= */}
      {activeSubTab === "cloud" && (
        <div className="w-full max-w-7xl space-y-9 pt-8 m-auto">
          {cloudProviders.map((cp) => {
            const isTestingThis = testingId === cp.id;
            const isPrimary = config.llm.provider === cp.id || config.llm.base_url === cp.baseUrl;
            const isVision = config.llm_vision.provider === cp.id || config.llm_vision.base_url === cp.baseUrl;

            return (
              <div key={cp.id} className="p-4 rounded-md border border-border bg-card space-y-3.5 shadow-xs">
                <div className="flex items-center justify-between border-b border-border/50 pb-2.5">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-bold text-foreground">{cp.name}</span>
                    <span className="text-[9px] px-1.5 py-0.2 rounded bg-muted text-muted-foreground font-mono">
                      {cp.badge}
                    </span>

                    {cp.status === "online" && (
                      <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20">
                        <CheckCircle2 size={11} /> Connected ({cp.latency} ms)
                      </span>
                    )}
                    {cp.status === "offline" && (
                      <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-rose-600 dark:text-rose-400 bg-rose-500/10 px-2 py-0.5 rounded border border-rose-500/20">
                        <XCircle size={11} /> Connection Failed
                      </span>
                    )}
                    {cp.status === "untested" && (
                      <span className="text-[10px] text-amber-500 bg-amber-500/10 px-2 py-0.5 rounded">
                        Untested
                      </span>
                    )}
                  </div>

                  <div className="flex items-center gap-1.5">
                    {isPrimary ? (
                      <span className="text-[10px] font-bold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-2.5 py-1 rounded border border-emerald-500/20">
                        Active Primary
                      </span>
                    ) : (
                      <button
                        onClick={() => activateEngine(cp.id, cp.name, cp.model, cp.baseUrl, cp.apiKey, cp.type)}
                        className="px-2.5 py-1 rounded text-[10px] bg-primary text-primary-foreground font-medium cursor-pointer hover:opacity-90"
                      >
                        Set Primary
                      </button>
                    )}

                    {!isVision && (
                      <button
                        onClick={() => {
                          setConfig((prev) => ({
                            ...prev,
                            llm_vision: {
                              ...prev.llm_vision,
                              provider: cp.id,
                              provider_name: cp.name,
                              model: cp.model,
                              base_url: cp.baseUrl,
                              api_key: cp.apiKey
                            }
                          }));
                          showToast.info("Vision Engine Assigned", `${cp.name} (${cp.model})`);
                        }}
                        className="px-2 py-1 rounded text-[10px] bg-violet-500/10 text-violet-500 font-medium hover:bg-violet-500/20 border border-violet-500/20 cursor-pointer"
                      >
                        Set Vision
                      </button>
                    )}
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
                  <div>
                    <label className="text-[10px] font-medium text-muted-foreground block mb-0.5">Model</label>
                    <div className="space-y-1">
                      <select
                        value={
                          cp.savedModels && cp.savedModels.includes(cp.model)
                            ? cp.model
                            : cp.popularModels.includes(cp.model)
                            ? cp.model
                            : "custom"
                        }
                        onChange={(e) => {
                          if (e.target.value !== "custom") {
                            setCloudProviders((prev) =>
                              prev.map((p) => (p.id === cp.id ? { ...p, model: e.target.value } : p))
                            );
                          }
                        }}
                        className="w-full bg-background border border-border rounded px-2.5 py-1 text-xs text-foreground font-mono shadow-xs focus:ring-1 focus:ring-primary"
                      >
                        {cp.savedModels && cp.savedModels.length > 0 && (
                          <optgroup label="Discovered & Saved Models">
                            {cp.savedModels.map((m) => (
                              <option key={m} value={m}>{m}</option>
                            ))}
                          </optgroup>
                        )}
                        <optgroup label="Popular Presets">
                          {cp.popularModels.map((m) => (
                            <option key={m} value={m}>{m}</option>
                          ))}
                        </optgroup>
                        <option value="custom">-- Custom Model ID --</option>
                      </select>

                      <input
                        type="text"
                        value={cp.model}
                        onChange={(e) => {
                          const val = e.target.value;
                          setCloudProviders((prev) => prev.map((p) => (p.id === cp.id ? { ...p, model: val } : p)));
                        }}
                        placeholder="Model identifier..."
                        className="w-full bg-background border border-border rounded px-2.5 py-1 text-[11px] text-foreground font-mono shadow-xs"
                      />
                    </div>
                  </div>

                  <div>
                    <label className="text-[10px] font-medium text-muted-foreground block mb-0.5">Base Endpoint URL</label>
                    <input
                      type="text"
                      value={cp.baseUrl}
                      onChange={(e) => {
                        const val = e.target.value;
                        setCloudProviders((prev) => prev.map((p) => (p.id === cp.id ? { ...p, baseUrl: val, status: "untested" } : p)));
                      }}
                      className="w-full bg-background border border-border rounded px-2.5 py-1 text-xs text-foreground font-mono shadow-xs"
                    />
                  </div>

                  <div>
                    <div className="flex items-center justify-between mb-0.5">
                      <label className="text-[10px] font-medium text-muted-foreground flex items-center gap-1">
                        <KeyRound size={11} /> API Key
                      </label>
                      <button
                        type="button"
                        onClick={() => toggleKey(cp.id)}
                        className="text-[9px] text-muted-foreground hover:text-foreground flex items-center gap-0.5 cursor-pointer"
                      >
                        {showKeys[cp.id] ? <EyeOff size={10} /> : <Eye size={10} />}
                        <span>{showKeys[cp.id] ? "Hide" : "Show"}</span>
                      </button>
                    </div>
                    <input
                      type={showKeys[cp.id] ? "text" : "password"}
                      value={cp.apiKey}
                      onChange={(e) => {
                        const val = e.target.value;
                        setCloudProviders((prev) => prev.map((p) => (p.id === cp.id ? { ...p, apiKey: val, status: "untested" } : p)));
                      }}
                      placeholder={`Enter key for ${cp.name}...`}
                      className="w-full bg-background border border-border rounded px-2.5 py-1 text-xs text-foreground font-mono shadow-xs"
                    />
                    <span className="text-[9px] text-muted-foreground block mt-0.5">{cp.keyPrefixHint}</span>
                  </div>
                </div>

                {cp.status === "offline" && cp.lastError && (
                  <div className="p-2 rounded bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs flex items-center gap-1.5">
                    <ShieldAlert size={13} className="shrink-0" />
                    <span className="font-mono text-[11px]">{cp.lastError}</span>
                  </div>
                )}

                <div className="flex items-center justify-between pt-1">
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => handleTestCloud(cp.id)}
                      disabled={isTestingThis}
                      className="h-7 px-2.5 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs flex items-center gap-1"
                    >
                      <Zap size={11} className={`text-amber-500 ${isTestingThis ? "animate-spin" : ""}`} />
                      <span>{isTestingThis ? "Verifying..." : "Test Connection"}</span>
                    </Button>

                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        if (!cp.apiKey.trim()) {
                          showToast.warning(`${cp.name} API Key Required`, "Please enter an API key before scanning models.");
                          return;
                        }
                        setDiscoveryTarget({
                          id: cp.id,
                          name: cp.name,
                          baseUrl: cp.baseUrl,
                          apiKey: cp.apiKey,
                          type: cp.type,
                          savedModels: cp.savedModels || [],
                          isCustom: false
                        });
                      }}
                      className="h-7 px-2.5 text-xs border-border bg-card hover:bg-muted text-foreground cursor-pointer shadow-xs flex items-center gap-1.5"
                    >
                      <RefreshCw size={11} className="text-primary" />
                      <span>Scan Models</span>
                      {cp.savedModels && cp.savedModels.length > 0 && (
                        <span className="ml-1 px-1.5 py-0.2 text-[10px] rounded-full bg-primary/15 text-primary font-mono font-bold">
                          {cp.savedModels.length}
                        </span>
                      )}
                    </Button>
                  </div>

                  <span className="text-[10px] text-muted-foreground">
                    Credentials isolated to this provider
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* ========================================================================= */}
      {/* SUB-TAB 4: CUSTOM ENDPOINTS */}
      {/* ========================================================================= */}
      {activeSubTab === "custom" && (
        <div className="w-full max-w-7xl space-y-9 pt-8 m-auto">
          <div className="text-xs text-muted-foreground flex items-center gap-1 font-mono">
            <span>Settings</span>
            <span>&gt;</span>
            <span>Providers</span>
            <span>&gt;</span>
            <span className="text-foreground font-bold">Custom Endpoints</span>
          </div>

          <div className="space-y-3">
            {customEndpoints.length === 0 ? (
              <div className="p-6 rounded-md border border-dashed border-border bg-card text-center text-xs text-muted-foreground">
                No custom endpoints configured yet. Add your local vLLM, FastChat, or remote custom API servers below.
              </div>
            ) : (
              customEndpoints.map((ce) => (
                <div key={ce.id} className="p-4 rounded-md border border-border bg-card shadow-xs flex items-center justify-between">
                  <div className="space-y-0.5">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-bold text-foreground">{ce.name}</span>
                      <span className="text-[10px] px-1.5 py-0.2 rounded bg-muted text-muted-foreground font-mono">
                        {ce.defaultModel}
                      </span>
                    </div>
                    <span className="text-xs text-muted-foreground font-mono block">{ce.endpointUrl}</span>
                  </div>

                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setDiscoveryTarget({
                          id: ce.id,
                          name: ce.name,
                          baseUrl: ce.endpointUrl,
                          apiKey: ce.apiKey || "",
                          type: "",
                          savedModels: ce.savedModels || [],
                          isCustom: true
                        });
                      }}
                      className="h-8 px-2.5 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs flex items-center gap-1"
                      title="Scan available models for this endpoint"
                    >
                      <RefreshCw size={11} className="text-primary" />
                      <span>Scan</span>
                      {ce.savedModels && ce.savedModels.length > 0 && (
                        <span className="ml-1 px-1.5 py-0.2 text-[10px] rounded-full bg-primary/15 text-primary font-mono font-bold">
                          {ce.savedModels.length}
                        </span>
                      )}
                    </Button>

                    <Button
                      onClick={() =>
                        activateEngine(ce.providerId, ce.name, ce.defaultModel, ce.endpointUrl, ce.apiKey, "")
                      }
                      className="h-8 px-3 text-xs bg-primary text-primary-foreground font-medium flex items-center gap-1.5 cursor-pointer shadow-xs"
                    >
                      <Zap size={12} />
                      <span>Use</span>
                    </Button>

                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleDeleteCustomEndpoint(ce.id)}
                      className="h-8 w-8 p-0 text-muted-foreground hover:text-rose-500 cursor-pointer"
                    >
                      <Trash2 size={14} />
                    </Button>
                  </div>
                </div>
              ))
            )}
          </div>

          <div className="p-5 rounded-md border border-border bg-card space-y-4 shadow-sm">
            <div className="flex items-center gap-2 pb-2 border-b border-border/60">
              <Plus size={14} className="text-primary" />
              <span className="text-xs font-bold text-foreground uppercase tracking-wider">
                Add Custom Endpoint
              </span>
            </div>

            <div className="space-y-3.5 text-xs">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5">
                <div>
                  <label className="text-[11px] font-medium text-muted-foreground block mb-1">Name</label>
                  <input
                    type="text"
                    value={endpointForm.name}
                    onChange={(e) => setEndpointForm({ ...endpointForm, name: e.target.value })}
                    placeholder="e.g. Local vLLM, FastChat, Custom API"
                    className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground shadow-xs"
                  />
                </div>
                <div>
                  <label className="text-[11px] font-medium text-muted-foreground block mb-1">Provider ID</label>
                  <input
                    type="text"
                    value={endpointForm.providerId}
                    onChange={(e) => setEndpointForm({ ...endpointForm, providerId: e.target.value })}
                    placeholder="e.g. local-vllm, custom-endpoint"
                    className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground shadow-xs font-mono"
                  />
                </div>
              </div>

              <div>
                <label className="text-[11px] font-medium text-muted-foreground block mb-1">Endpoint URL</label>
                <input
                  type="text"
                  value={endpointForm.endpointUrl}
                  onChange={(e) => setEndpointForm({ ...endpointForm, endpointUrl: e.target.value })}
                  placeholder="e.g. http://127.0.0.1:8000/v1"
                  className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground shadow-xs font-mono"
                />
              </div>

              <div>
                <label className="text-[11px] font-medium text-muted-foreground block mb-1.5">API Mode</label>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-1 p-1 rounded-lg bg-background border border-border">
                  {(["Auto-detect", "Chat Completions", "Responses API", "Anthropic Messages"] as ApiModeType[]).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => setEndpointForm({ ...endpointForm, apiMode: mode })}
                      className={`py-1.5 px-2 rounded text-[11px] font-medium transition-all cursor-pointer ${
                        endpointForm.apiMode === mode
                          ? "bg-card text-foreground shadow-xs font-bold border border-border"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {mode}
                    </button>
                  ))}
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-3.5">
                <div className="md:col-span-2">
                  <label className="text-[11px] font-medium text-muted-foreground block mb-1">Default Model</label>
                  <input
                    type="text"
                    value={endpointForm.defaultModel}
                    onChange={(e) => setEndpointForm({ ...endpointForm, defaultModel: e.target.value })}
                    placeholder="e.g. meta-llama/Llama-3-8B-Instruct"
                    className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground shadow-xs font-mono"
                  />
                </div>
                <div>
                  <label className="text-[11px] font-medium text-muted-foreground block mb-1">Context</label>
                  <input
                    type="text"
                    value={endpointForm.contextWindow}
                    onChange={(e) => setEndpointForm({ ...endpointForm, contextWindow: e.target.value })}
                    placeholder="Auto"
                    className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground shadow-xs font-mono"
                  />
                </div>
              </div>

              <div>
                <label className="text-[11px] font-medium text-muted-foreground block mb-1">API Key</label>
                <input
                  type="password"
                  value={endpointForm.apiKey}
                  onChange={(e) => setEndpointForm({ ...endpointForm, apiKey: e.target.value })}
                  placeholder="Leave blank to keep current key or for unauthenticated local servers"
                  className="w-full bg-background border border-border rounded px-3 py-1.5 text-xs text-foreground shadow-xs font-mono"
                />
              </div>

              <div className="flex items-center gap-4 pt-1">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={endpointForm.useForNewChats}
                    onChange={(e) => setEndpointForm({ ...endpointForm, useForNewChats: e.target.checked })}
                    className="rounded border-border text-primary"
                  />
                  <span className="text-xs text-foreground">Use for new chats</span>
                </label>
              </div>

              <div className="flex items-center gap-2.5 pt-3 border-t border-border/60">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleTestCustomForm}
                  disabled={testingId === "custom_form"}
                  className="h-8 px-3 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs flex items-center gap-1.5"
                >
                  <Zap size={12} className={`text-amber-500 ${testingId === "custom_form" ? "animate-spin" : ""}`} />
                  <span>Test</span>
                </Button>

                <Button
                  type="button"
                  size="sm"
                  onClick={handleSaveCustomEndpoint}
                  className="h-8 px-4 text-xs bg-primary text-primary-foreground font-medium cursor-pointer shadow-xs"
                >
                  Save
                </Button>

                <span className="text-[11px] text-muted-foreground ml-2">New endpoint</span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* SUB-TAB: PER-MODEL BUDGETS & CONTEXT */}
      {/* ========================================================================= */}
      {activeSubTab === "budgets" && (
        <ModelBudgetsTab
          availableModels={availableModels}
          lmStudioSettings={lmStudioSettings}
          ollamaSettings={ollamaSettings}
          cloudProviders={cloudProviders}
          customEndpoints={customEndpoints}
        />
      )}

      {/* Model Discovery Modal */}
      <ModelInfoModal isOpen={Boolean(infoModalModel)} onClose={() => setInfoModalModel(null)} modelKey={infoModalModel || ""} explicitMeta={infoModalModel ? metadataVault[infoModalModel] : null} />
      {discoveryTarget && (
        <ModelDiscoveryModal
          isOpen={Boolean(discoveryTarget)}
          onClose={() => setDiscoveryTarget(null)}
          providerId={discoveryTarget.id}
          providerName={discoveryTarget.name}
          baseUrl={discoveryTarget.baseUrl}
          apiKey={discoveryTarget.apiKey}
          providerType={discoveryTarget.type}
          initialSavedModels={discoveryTarget.savedModels || []}
          onSaveModels={handleSaveDiscovered}
        />
      )}
    </div>
  );
}