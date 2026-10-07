"use client";

import React, { useState, useEffect, useMemo } from "react";
import {
  Gauge,
  Sliders,
  Sparkles,
  Save,
  Cpu,
  Terminal,
  Cloud,
  Box,
  ChevronDown,
  ChevronRight,
  RotateCcw,
  SlidersHorizontal,
  Bot,
  Zap,
  CheckCircle2,
  AlertTriangle
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/ToastNotification";
import type {
  CloudProviderVaultItem,
  CustomEndpoint,
  LMStudioSettings,
  OllamaSettings,
  ModelBudgetItemConfig,
  ModelBudgetsMap
} from "../types";

interface ModelBudgetsTabProps {
  availableModels: string[];
  lmStudioSettings: LMStudioSettings;
  ollamaSettings: OllamaSettings;
  cloudProviders: CloudProviderVaultItem[];
  customEndpoints: CustomEndpoint[];
}

interface BudgetRatios {
  system: number;
  summary: number;
  recent: number;
  execution: number;
  current: number;
}

const DEFAULT_RATIOS: BudgetRatios = {
  system: 10,
  summary: 15,
  recent: 50,
  execution: 15,
  current: 10,
};

const RATIO_PRESETS = [
  { name: "Balanced", ratios: { system: 10, summary: 15, recent: 50, execution: 15, current: 10 } },
  { name: "Agent & Tools Heavy", ratios: { system: 10, summary: 10, recent: 40, execution: 30, current: 10 } },
  { name: "Pure Conversation", ratios: { system: 10, summary: 15, recent: 65, execution: 0, current: 10 } },
  { name: "Deep Memory & Summary", ratios: { system: 10, summary: 30, recent: 40, execution: 10, current: 10 } },
];

interface GranularProviderGroup {
  groupId: string;
  providerName: string;
  providerKey: string;
  icon: React.ReactNode;
  models: Array<{
    modelId: string;
    displayName: string;
    defaultContextWindow: number;
    defaultMaxOutput: number;
  }>;
}

/**
 * Intelligent Multi-Tier Heuristic to accurately resolve model limits.
 */
function resolveIntelligentLimits(
  modelId: string,
  metadataVault: Record<string, any>
): { contextWindow: number; maxOutput: number; source: string } {
  const mid = modelId.toLowerCase();

  // Tier 1: Hardware Metadata lookup from local cache
  const meta = metadataVault[modelId] || metadataVault[mid];
  if (meta && (meta.max_context_length || meta.context_length)) {
    const raw = parseInt(String(meta.max_context_length || meta.context_length), 10);
    if (!isNaN(raw) && raw > 0) {
      const output = raw >= 32768 ? 4096 : 1500;
      return { contextWindow: raw, maxOutput: output, source: "Hardware Inspection" };
    }
  }

  // Tier 2: Explicit Token Suffix Patterns
  if (mid.includes("-1m") || mid.includes(":1m")) return { contextWindow: 1048576, maxOutput: 8192, source: "1M Pattern" };
  if (mid.includes("-128k") || mid.includes(":128k")) return { contextWindow: 131072, maxOutput: 4096, source: "128K Pattern" };
  if (mid.includes("-64k") || mid.includes(":64k")) return { contextWindow: 65536, maxOutput: 4096, source: "64K Pattern" };
  if (mid.includes("-32k") || mid.includes(":32k")) return { contextWindow: 32768, maxOutput: 4096, source: "32K Pattern" };
  if (mid.includes("-16k") || mid.includes(":16k")) return { contextWindow: 16384, maxOutput: 2048, source: "16K Pattern" };

  // Tier 3: Well-known Cloud Architectures
  if (mid.includes("gemini")) {
    return { contextWindow: 1048576, maxOutput: 8192, source: "Google Gemini 1M Spec" };
  }
  if (mid.includes("claude-3") || mid.includes("claude-3-5")) {
    return { contextWindow: 200000, maxOutput: 8192, source: "Anthropic Claude 200K Spec" };
  }
  if (mid.includes("gpt-4o") || mid.includes("gpt-4") || mid.includes("o1") || mid.includes("o3")) {
    return { contextWindow: 128000, maxOutput: 4096, source: "OpenAI 128K Spec" };
  }
  if (mid.includes("deepseek")) {
    return { contextWindow: 64000, maxOutput: 8192, source: "DeepSeek 64K Spec" };
  }
  if (mid.includes("llama-3.1") || mid.includes("llama-3.2") || mid.includes("llama-3.3")) {
    return { contextWindow: 131072, maxOutput: 4096, source: "Llama 3.x 128K Spec" };
  }
  if (mid.includes("qwen2.5") || mid.includes("qwen3")) {
    return { contextWindow: 32768, maxOutput: 4096, source: "Qwen 32K Spec" };
  }
  if (mid.includes("mistral") || mid.includes("codestral")) {
    return { contextWindow: 32768, maxOutput: 4096, source: "Mistral 32K Spec" };
  }

  // Tier 4: Fallback
  return { contextWindow: 8192, maxOutput: 1500, source: "Standard Default" };
}

export function ModelBudgetsTab({
  availableModels,
  lmStudioSettings,
  ollamaSettings,
  cloudProviders,
  customEndpoints,
}: ModelBudgetsTabProps) {
  const [budgets, setBudgets] = useState<ModelBudgetsMap>({});
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  // Accordion state: all providers closed by default
  const [openProviders, setOpenProviders] = useState<Record<string, boolean>>({});

  // Individual model configuration drawers
  const [expandedModels, setExpandedModels] = useState<Record<string, boolean>>({});

  // Local model ratio states
  const [modelRatios, setModelRatios] = useState<Record<string, BudgetRatios>>({});

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      try {
        const base =
          typeof window !== "undefined" &&
          (window.location.port === "3088" || window.location.port === "3000")
            ? "http://localhost:8088"
            : "";

        const [budgetsRes, metaRes] = await Promise.all([
          fetch(`${base}/api/config/model-budgets`).catch(() => null),
          fetch(`${base}/api/config/models-metadata`).catch(() => null),
        ]);

        if (budgetsRes && budgetsRes.ok) {
          const bData = await budgetsRes.json();
          if (bData.ok && bData.budgets) {
            setBudgets(bData.budgets);
            // Initialize custom ratios if present
            const loadedRatios: Record<string, BudgetRatios> = {};
            for (const [mId, bConf] of Object.entries(bData.budgets as Record<string, any>)) {
              if (bConf.ratios) {
                loadedRatios[mId] = bConf.ratios;
              }
            }
            setModelRatios(loadedRatios);
          }
        }

        if (metaRes && metaRes.ok) {
          const mData = await metaRes.json();
          if (mData.ok && mData.metadata) {
            setMetadataVault(mData.metadata);
          }
        }
      } catch (err: any) {
        console.error("Failed to load model budgets:", err);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  // Granular grouping: Each provider (Google, DeepSeek, OpenAI, Anthropic, LMStudio, Ollama, etc.) has its own group
  const providerGroups = useMemo<GranularProviderGroup[]>(() => {
    const groups: GranularProviderGroup[] = [];

    // 1. Google Gemini
    const googleItem = cloudProviders.find((p) => p.id === "google");
    if (googleItem) {
      const gModels = Array.from(
        new Set([...(googleItem.savedModels || []), ...(googleItem.popularModels || []), googleItem.model].filter(Boolean))
      );
      groups.push({
        groupId: "provider_google",
        providerName: "Google Gemini",
        providerKey: "google",
        icon: <Cloud size={15} className="text-sky-500" />,
        models: gModels.map((m) => ({
          modelId: m,
          displayName: m,
          defaultContextWindow: 1048576,
          defaultMaxOutput: 8192,
        })),
      });
    }

    // 2. DeepSeek
    const deepseekItem = cloudProviders.find((p) => p.id === "deepseek");
    if (deepseekItem) {
      const dModels = Array.from(
        new Set([...(deepseekItem.savedModels || []), ...(deepseekItem.popularModels || []), deepseekItem.model].filter(Boolean))
      );
      groups.push({
        groupId: "provider_deepseek",
        providerName: "DeepSeek",
        providerKey: "deepseek",
        icon: <Cloud size={15} className="text-blue-500" />,
        models: dModels.map((m) => ({
          modelId: m,
          displayName: m,
          defaultContextWindow: 64000,
          defaultMaxOutput: 8192,
        })),
      });
    }

    // 3. OpenAI
    const openaiItem = cloudProviders.find((p) => p.id === "openai");
    if (openaiItem) {
      const oModels = Array.from(
        new Set([...(openaiItem.savedModels || []), ...(openaiItem.popularModels || []), openaiItem.model].filter(Boolean))
      );
      groups.push({
        groupId: "provider_openai",
        providerName: "OpenAI",
        providerKey: "openai",
        icon: <Cloud size={15} className="text-emerald-500" />,
        models: oModels.map((m) => ({
          modelId: m,
          displayName: m,
          defaultContextWindow: 128000,
          defaultMaxOutput: 4096,
        })),
      });
    }

    // 4. Anthropic Claude
    const anthropicItem = cloudProviders.find((p) => p.id === "anthropic");
    if (anthropicItem) {
      const aModels = Array.from(
        new Set([...(anthropicItem.savedModels || []), ...(anthropicItem.popularModels || []), anthropicItem.model].filter(Boolean))
      );
      groups.push({
        groupId: "provider_anthropic",
        providerName: "Anthropic Claude",
        providerKey: "anthropic",
        icon: <Cloud size={15} className="text-amber-500" />,
        models: aModels.map((m) => ({
          modelId: m,
          displayName: m,
          defaultContextWindow: 200000,
          defaultMaxOutput: 8192,
        })),
      });
    }

    // Other Cloud Providers
    const knownCloudIds = ["google", "deepseek", "openai", "anthropic"];
    const otherClouds = cloudProviders.filter((p) => !knownCloudIds.includes(p.id));
    for (const oc of otherClouds) {
      const oModels = Array.from(
        new Set([...(oc.savedModels || []), ...(oc.popularModels || []), oc.model].filter(Boolean))
      );
      if (oModels.length > 0) {
        groups.push({
          groupId: `provider_${oc.id}`,
          providerName: oc.name,
          providerKey: oc.id,
          icon: <Cloud size={15} className="text-indigo-500" />,
          models: oModels.map((m) => ({
            modelId: m,
            displayName: m,
            defaultContextWindow: 32768,
            defaultMaxOutput: 4096,
          })),
        });
      }
    }

    // 5. LM Studio (Local GPU)
    const lmList = Array.from(
      new Set([...availableModels, ...(lmStudioSettings.savedModels || []), lmStudioSettings.model].filter(Boolean))
    );
    if (lmList.length > 0) {
      groups.push({
        groupId: "provider_lmstudio",
        providerName: "LM Studio (Local GPU)",
        providerKey: "lmstudio",
        icon: <Cpu size={15} className="text-primary" />,
        models: lmList.map((m) => ({
          modelId: m,
          displayName: m,
          defaultContextWindow: 8192,
          defaultMaxOutput: 1500,
        })),
      });
    }

    // 6. Ollama (Local Server)
    const olList = Array.from(
      new Set([...(ollamaSettings.savedModels || []), ollamaSettings.model].filter(Boolean))
    );
    if (olList.length > 0) {
      groups.push({
        groupId: "provider_ollama",
        providerName: "Ollama (Local Server)",
        providerKey: "ollama",
        icon: <Terminal size={15} className="text-amber-500" />,
        models: olList.map((m) => ({
          modelId: m,
          displayName: m,
          defaultContextWindow: 8192,
          defaultMaxOutput: 1500,
        })),
      });
    }

    // 7. Custom Endpoints
    for (const ce of customEndpoints) {
      const ceModels = Array.from(
        new Set([...(ce.savedModels || []), ce.defaultModel].filter(Boolean))
      );
      if (ceModels.length > 0) {
        groups.push({
          groupId: `provider_custom_${ce.id}`,
          providerName: `Custom: ${ce.name}`,
          providerKey: ce.providerId || ce.id,
          icon: <Box size={15} className="text-violet-500" />,
          models: ceModels.map((m) => ({
            modelId: m,
            displayName: m,
            defaultContextWindow: 8192,
            defaultMaxOutput: 1500,
          })),
        });
      }
    }

    return groups;
  }, [cloudProviders, availableModels, lmStudioSettings, ollamaSettings, customEndpoints]);

  const toggleProvider = (groupId: string) => {
    setOpenProviders((prev) => ({ ...prev, [groupId]: !prev[groupId] }));
  };

  const toggleModel = (modelId: string) => {
    setExpandedModels((prev) => ({ ...prev, [modelId]: !prev[modelId] }));
  };

  const expandAllProviders = () => {
    const next: Record<string, boolean> = {};
    for (const g of providerGroups) next[g.groupId] = true;
    setOpenProviders(next);
  };

  const collapseAllProviders = () => {
    setOpenProviders({});
  };

  const handleUpdateLimit = (
    modelId: string,
    field: "context_window" | "max_output_tokens",
    value: number,
    providerName: string
  ) => {
    setBudgets((prev) => {
      const current = prev[modelId] || {
        context_window: 8192,
        max_output_tokens: 1500,
        provider_name: providerName,
      };
      return {
        ...prev,
        [modelId]: {
          ...current,
          [field]: Math.max(100, value),
          provider_name: providerName,
        },
      };
    });
  };

  const handleUpdateRatio = (
    modelId: string,
    section: keyof BudgetRatios,
    value: number
  ) => {
    const current = modelRatios[modelId] || { ...DEFAULT_RATIOS };
    const updated = { ...current, [section]: Math.max(0, Math.min(100, value)) };
    setModelRatios((prev) => ({ ...prev, [modelId]: updated }));
  };

  const handleApplyPreset = (modelId: string, presetRatios: BudgetRatios) => {
    setModelRatios((prev) => ({ ...prev, [modelId]: { ...presetRatios } }));
  };

  const handleNormalizeRatios = (modelId: string) => {
    const current = modelRatios[modelId] || { ...DEFAULT_RATIOS };
    const sum = current.system + current.summary + current.recent + current.execution + current.current;
    if (sum === 0) {
      setModelRatios((prev) => ({ ...prev, [modelId]: { ...DEFAULT_RATIOS } }));
      return;
    }
    const factor = 100 / sum;
    const normalized: BudgetRatios = {
      system: Math.round(current.system * factor),
      summary: Math.round(current.summary * factor),
      recent: Math.round(current.recent * factor),
      execution: Math.round(current.execution * factor),
      current: Math.round(current.current * factor),
    };
    // Ensure exact 100
    const newSum = normalized.system + normalized.summary + normalized.recent + normalized.execution + normalized.current;
    normalized.recent += 100 - newSum;
    setModelRatios((prev) => ({ ...prev, [modelId]: normalized }));
    showToast.info("Normalized", "Budget distribution normalized to exactly 100%.");
  };

  const handleAutoDetect = (modelId: string, providerName: string) => {
    const resolved = resolveIntelligentLimits(modelId, metadataVault);
    setBudgets((prev) => ({
      ...prev,
      [modelId]: {
        context_window: resolved.contextWindow,
        max_output_tokens: resolved.maxOutput,
        provider_name: providerName,
      },
    }));

    showToast.success(
      "Auto-Detected Successfully",
      `${modelId}: ${resolved.contextWindow.toLocaleString()} tokens (${resolved.source})`
    );
  };

  const handleSaveAll = async () => {
    setSaving(true);
    try {
      const base =
        typeof window !== "undefined" &&
        (window.location.port === "3088" || window.location.port === "3000")
          ? "http://localhost:8088"
          : "";

      // Attach current ratios to payload
      const payloadBudgets: Record<string, any> = { ...budgets };
      for (const [mId, ratios] of Object.entries(modelRatios)) {
        if (!payloadBudgets[mId]) {
          payloadBudgets[mId] = { context_window: 8192, max_output_tokens: 1500 };
        }
        payloadBudgets[mId].ratios = ratios;
      }

      const res = await fetch(`${base}/api/config/model-budgets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ budgets: payloadBudgets }),
      });

      const data = await res.json();
      if (data.ok) {
        showToast.success("Budgets Saved", "All per-model limits & interactive distributions safely stored.");
      } else {
        showToast.error("Save Failed", data.detail || "Unable to save configuration.");
      }
    } catch (err: any) {
      showToast.error("Network Error", err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="w-full max-w-7xl space-y-6 pt-6 m-auto">
      {/* Top Banner & Control Actions */}
      <div className="p-4 rounded-lg border border-border bg-card flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 shadow-xs">
        <div>
          <div className="flex items-center gap-2">
            <Gauge size={16} className="text-primary" />
            <h3 className="text-sm font-bold text-foreground uppercase tracking-wide">
              Per-Model Context Window & Token Budget Architecture
            </h3>
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Configure dedicated token limits and live interactive distributions for each provider and model.
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={Object.values(openProviders).some(Boolean) ? collapseAllProviders : expandAllProviders}
            className="h-8 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs"
          >
            {Object.values(openProviders).some(Boolean) ? "Collapse All" : "Expand All"}
          </Button>

          <Button
            onClick={handleSaveAll}
            disabled={saving || loading}
            className="h-8 text-xs bg-primary text-primary-foreground font-medium flex items-center gap-1.5 cursor-pointer shadow-xs"
          >
            <Save size={13} className={saving ? "animate-spin" : ""} />
            <span>{saving ? "Saving Changes..." : "Save All Budgets"}</span>
          </Button>
        </div>
      </div>

      {/* Accordion Providers (Google, DeepSeek, OpenAI, Anthropic, LMStudio, Ollama, Custom) */}
      <div className="space-y-3.5">
        {providerGroups.map((group) => {
          const isOpen = Boolean(openProviders[group.groupId]);

          return (
            <div
              key={group.groupId}
              className="rounded-lg border border-border bg-card overflow-hidden shadow-xs transition-all"
            >
              {/* Provider Accordion Header (Click to Open / Close) */}
              <button
                type="button"
                onClick={() => toggleProvider(group.groupId)}
                className="w-full px-4 py-3 bg-muted/20 hover:bg-muted/40 transition flex items-center justify-between cursor-pointer border-b border-border/40"
              >
                <div className="flex items-center gap-2.5">
                  <div className="p-1 rounded bg-background border border-border/60">
                    {group.icon}
                  </div>
                  <span className="text-xs font-bold text-foreground uppercase tracking-wider">
                    {group.providerName}
                  </span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-primary/10 text-primary font-mono font-bold">
                    {group.models.length} models
                  </span>
                </div>

                <div className="flex items-center gap-2 text-muted-foreground">
                  <span className="text-[11px] font-mono hidden sm:inline">
                    {isOpen ? "Close group" : "Open group"}
                  </span>
                  {isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                </div>
              </button>

              {/* Models List Inside This Provider (Closed by Default) */}
              {isOpen && (
                <div className="divide-y divide-border/50 p-2 sm:p-3 space-y-2">
                  {group.models.map((item) => {
                    const modelConfig = budgets[item.modelId] || {
                      context_window: item.defaultContextWindow,
                      max_output_tokens: item.defaultMaxOutput,
                      provider_name: group.providerName,
                    };

                    const contextWindow = modelConfig.context_window || item.defaultContextWindow;
                    const maxOutput = modelConfig.max_output_tokens || item.defaultMaxOutput;
                    const effectiveInputBudget = Math.max(100, contextWindow - maxOutput);
                    const isExpanded = Boolean(expandedModels[item.modelId]);

                    // Model Interactive Ratios
                    const ratios = modelRatios[item.modelId] || { ...DEFAULT_RATIOS };
                    const sumRatios = ratios.system + ratios.summary + ratios.recent + ratios.execution + ratios.current;
                    const isSumValid = sumRatios === 100;

                    const tokenSlices = {
                      system: Math.round(effectiveInputBudget * (ratios.system / 100)),
                      summary: Math.round(effectiveInputBudget * (ratios.summary / 100)),
                      recent: Math.round(effectiveInputBudget * (ratios.recent / 100)),
                      execution: Math.round(effectiveInputBudget * (ratios.execution / 100)),
                      current: Math.round(effectiveInputBudget * (ratios.current / 100)),
                    };

                    return (
                      <div
                        key={item.modelId}
                        className="rounded-md border border-border/60 bg-background/50 overflow-hidden transition hover:border-border"
                      >
                        {/* Model Row Summary */}
                        <div className="p-3 flex flex-col sm:flex-row sm:items-center justify-between gap-2.5">
                          <div className="flex items-center gap-2.5 truncate">
                            <button
                              type="button"
                              onClick={() => toggleModel(item.modelId)}
                              className="text-muted-foreground hover:text-foreground cursor-pointer"
                              title="Toggle configuration"
                            >
                              {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                            </button>
                            <div className="truncate">
                              <span
                                className="text-xs font-bold font-mono text-foreground block truncate"
                                title={item.modelId}
                              >
                                {item.displayName}
                              </span>
                              <span className="text-[10px] text-muted-foreground">
                                {group.providerName}
                              </span>
                            </div>
                          </div>

                          {/* Quick Badges & Actions */}
                          <div className="flex items-center gap-2 shrink-0">
                            <div className="flex items-center gap-1.5 font-mono text-[10px]">
                              <span className="px-2 py-0.5 rounded bg-muted text-muted-foreground border border-border">
                                Context: {contextWindow.toLocaleString()}
                              </span>
                              <span className="px-2 py-0.5 rounded bg-muted text-muted-foreground border border-border">
                                Out: {maxOutput.toLocaleString()}
                              </span>
                              <span className="px-2 py-0.5 rounded bg-primary/10 text-primary font-bold border border-primary/20">
                                Budget: {effectiveInputBudget.toLocaleString()}
                              </span>
                            </div>

                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              onClick={() => handleAutoDetect(item.modelId, group.providerName)}
                              className="h-6 px-2 text-[10px] text-muted-foreground hover:text-primary flex items-center gap-1 cursor-pointer"
                              title="Auto-Detect context limit from specifications"
                            >
                              <Sparkles size={11} className="text-amber-500" />
                              <span>Auto-Detect</span>
                            </Button>

                            <button
                              type="button"
                              onClick={() => toggleModel(item.modelId)}
                              className="text-[11px] text-primary hover:underline ml-1 cursor-pointer font-medium"
                            >
                              {isExpanded ? "Close" : "Configure"}
                            </button>
                          </div>
                        </div>

                        {/* Interactive Configuration Panel */}
                        {isExpanded && (
                          <div className="p-3.5 bg-card/60 border-t border-border/50 space-y-4">
                            {/* Inputs */}
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                              <div>
                                <label className="text-[10px] font-medium text-muted-foreground block mb-1">
                                  Context Window (Total Tokens)
                                </label>
                                <input
                                  type="number"
                                  value={contextWindow}
                                  onChange={(e) =>
                                    handleUpdateLimit(
                                      item.modelId,
                                      "context_window",
                                      parseInt(e.target.value, 10) || item.defaultContextWindow,
                                      group.providerName
                                    )
                                  }
                                  className="w-full bg-background border border-border rounded px-2.5 py-1 text-xs text-foreground font-mono shadow-xs"
                                />
                              </div>

                              <div>
                                <label className="text-[10px] font-medium text-muted-foreground block mb-1">
                                  Max Output Tokens (Completion Buffer)
                                </label>
                                <input
                                  type="number"
                                  value={maxOutput}
                                  onChange={(e) =>
                                    handleUpdateLimit(
                                      item.modelId,
                                      "max_output_tokens",
                                      parseInt(e.target.value, 10) || item.defaultMaxOutput,
                                      group.providerName
                                    )
                                  }
                                  className="w-full bg-background border border-border rounded px-2.5 py-1 text-xs text-foreground font-mono shadow-xs"
                                />
                              </div>
                            </div>

                            {/* Interactive Token Budget Controller */}
                            <div className="p-3 rounded-lg bg-background/80 border border-border/70 space-y-3">
                              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                                <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
                                  <SlidersHorizontal size={13} className="text-primary" />
                                  <span>Interactive Budget Distribution</span>
                                </div>

                                {/* Presets Selector */}
                                <div className="flex flex-wrap items-center gap-1">
                                  {RATIO_PRESETS.map((p) => (
                                    <button
                                      key={p.name}
                                      type="button"
                                      onClick={() => handleApplyPreset(item.modelId, p.ratios)}
                                      className="px-2 py-0.5 rounded text-[10px] bg-muted hover:bg-primary/10 hover:text-primary border border-border/80 transition cursor-pointer"
                                    >
                                      {p.name}
                                    </button>
                                  ))}
                                </div>
                              </div>

                              {/* Interactive Segmented Bar */}
                              <div className="w-full h-3 rounded bg-muted overflow-hidden flex shadow-inner cursor-pointer">
                                <div
                                  style={{ width: `${ratios.system}%` }}
                                  className="bg-sky-500 hover:brightness-110 transition-all"
                                  title={`System Prompt: ${ratios.system}% (${tokenSlices.system.toLocaleString()} tokens)`}
                                />
                                <div
                                  style={{ width: `${ratios.summary}%` }}
                                  className="bg-amber-500 hover:brightness-110 transition-all"
                                  title={`Summary: ${ratios.summary}% (${tokenSlices.summary.toLocaleString()} tokens)`}
                                />
                                <div
                                  style={{ width: `${ratios.recent}%` }}
                                  className="bg-emerald-500 hover:brightness-110 transition-all"
                                  title={`Recent Turns: ${ratios.recent}% (${tokenSlices.recent.toLocaleString()} tokens)`}
                                />
                                <div
                                  style={{ width: `${ratios.execution}%` }}
                                  className="bg-violet-500 hover:brightness-110 transition-all"
                                  title={`Execution State: ${ratios.execution}% (${tokenSlices.execution.toLocaleString()} tokens)`}
                                />
                                <div
                                  style={{ width: `${ratios.current}%` }}
                                  className="bg-rose-500 hover:brightness-110 transition-all"
                                  title={`Current Message: ${ratios.current}% (${tokenSlices.current.toLocaleString()} tokens)`}
                                />
                              </div>

                              {/* Interactive Sliders for Each Section */}
                              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-2.5 pt-1 text-xs font-mono">
                                {/* System */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-sky-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-sky-500" /> System
                                    </span>
                                    <span>{ratios.system}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="0"
                                    max="30"
                                    value={ratios.system}
                                    onChange={(e) => handleUpdateRatio(item.modelId, "system", parseInt(e.target.value, 10))}
                                    className="w-full accent-sky-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.system.toLocaleString()} tokens
                                  </span>
                                </div>

                                {/* Summary */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-amber-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-amber-500" /> Summary
                                    </span>
                                    <span>{ratios.summary}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="0"
                                    max="40"
                                    value={ratios.summary}
                                    onChange={(e) => handleUpdateRatio(item.modelId, "summary", parseInt(e.target.value, 10))}
                                    className="w-full accent-amber-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.summary.toLocaleString()} tokens
                                  </span>
                                </div>

                                {/* Recent */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-emerald-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-emerald-500" /> Recent
                                    </span>
                                    <span>{ratios.recent}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="10"
                                    max="80"
                                    value={ratios.recent}
                                    onChange={(e) => handleUpdateRatio(item.modelId, "recent", parseInt(e.target.value, 10))}
                                    className="w-full accent-emerald-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.recent.toLocaleString()} tokens
                                  </span>
                                </div>

                                {/* Execution */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-violet-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-violet-500" /> Exec State
                                    </span>
                                    <span>{ratios.execution}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="0"
                                    max="40"
                                    value={ratios.execution}
                                    onChange={(e) => handleUpdateRatio(item.modelId, "execution", parseInt(e.target.value, 10))}
                                    className="w-full accent-violet-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.execution.toLocaleString()} tokens
                                  </span>
                                </div>

                                {/* Current */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-rose-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-rose-500" /> Current
                                    </span>
                                    <span>{ratios.current}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="0"
                                    max="30"
                                    value={ratios.current}
                                    onChange={(e) => handleUpdateRatio(item.modelId, "current", parseInt(e.target.value, 10))}
                                    className="w-full accent-rose-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.current.toLocaleString()} tokens
                                  </span>
                                </div>
                              </div>

                              {/* Validation and Normalization Status */}
                              <div className="flex items-center justify-between pt-1 border-t border-border/40 text-[11px]">
                                <div className="flex items-center gap-1.5">
                                  {isSumValid ? (
                                    <span className="flex items-center gap-1 text-emerald-600 dark:text-emerald-400 font-semibold">
                                      <CheckCircle2 size={12} /> Total Allocation: 100%
                                    </span>
                                  ) : (
                                    <span className="flex items-center gap-1 text-amber-500 font-semibold">
                                      <AlertTriangle size={12} /> Total Allocation: {sumRatios}% (Needs balance)
                                    </span>
                                  )}
                                </div>

                                {!isSumValid && (
                                  <button
                                    type="button"
                                    onClick={() => handleNormalizeRatios(item.modelId)}
                                    className="text-[10px] text-primary hover:underline flex items-center gap-1 cursor-pointer font-bold"
                                  >
                                    <RotateCcw size={10} /> Auto-Normalize to 100%
                                  </button>
                                )}
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}