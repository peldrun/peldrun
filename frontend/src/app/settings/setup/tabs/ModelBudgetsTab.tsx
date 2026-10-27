"use client";
/* frontend\src\app\settings\setup\tabs\ModelBudgetsTab.tsx */
import React, { useState, useEffect, useMemo } from "react";
import {
  Gauge,
  Sparkles,
  Save,
  ChevronDown,
  ChevronRight,
  RotateCcw,
  SlidersHorizontal,
  CheckCircle2,
  AlertTriangle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/Icon/Icon";
import { showToast } from "@/components/ui/ToastNotification";
import type {
  CloudProviderVaultItem,
  CustomEndpoint,
  LMStudioSettings,
  OllamaSettings,
  ModelBudgetsMap,
} from "../types";

/* ============================================================
 * Vault shape — mirrors backend/data/models_vault.json exactly
 * as written by config_rtr.load_vault_from_disk() / DEFAULT_VAULT.
 * ============================================================ */
interface VaultData {
  cloud_vault?: CloudProviderVaultItem[];
  custom_endpoints?: CustomEndpoint[];
  lmstudio_vault?: (LMStudioSettings & { provider?: string; badge?: string; latency?: number }) | null;
  ollama_vault?: (OllamaSettings & { badge?: string; latency?: number }) | null;
  scanned_models?: string[];
}

interface ModelBudgetsTabProps {
  /**
   * All props are optional. When omitted, the component reads
   * provider metadata straight from the backend models vault.
   */
  availableModels?: string[];
  lmStudioSettings?: LMStudioSettings;
  ollamaSettings?: OllamaSettings;
  cloudProviders?: CloudProviderVaultItem[];
  customEndpoints?: CustomEndpoint[];
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
  badge: string;
  icon: React.ReactNode;
  models: Array<{
    modelId: string;
    displayName: string;
    defaultContextWindow: number;
    defaultMaxOutput: number;
  }>;
}

/* ============================================================
 * Backend base URL resolution.
 *
 * In dev the Next.js server runs on 3000/3088 while the FastAPI
 * backend listens on 8088, so the backend is addressed directly.
 * In production both share an origin behind a reverse proxy.
 * ============================================================ */
function resolveBackendBase(): string {
  // Return empty string so all fetch calls use relative paths routed
  // through Next.js rewrites (see next.config.mjs). An absolute URL
  // here would bypass rewrites and cause SSR/CSR divergence.
  return "";
}

/* ============================================================
 * Vault endpoint probing.
 *
 * The FastAPI router (config_rtr.py) declares @router.get("/vault")
 * but the effective URL depends on the include_router prefix set
 * by the application bootstrap. Verified working prefix in dev:
 *   /api/config/vault
 *
 * The remaining candidates are kept as ordered fallbacks so that
 * moving the router mount point does not silently break the tab.
 * ============================================================ */
const VAULT_PATH_CANDIDATES = [
  "/api/config/vault",
  "/api/models/vault",
  "/api/vault",
  "/api/v1/vault",
  "/api/v1/models/vault",
  "/models/vault",
  "/vault",
];

function hasVaultPayload(payload: any): boolean {
  return Boolean(
    payload &&
      (payload.cloud_vault ||
        payload.custom_endpoints ||
        payload.lmstudio_vault ||
        payload.ollama_vault)
  );
}

async function probeVaultEndpoint(base: string): Promise<VaultData | null> {
  for (const path of VAULT_PATH_CANDIDATES) {
    const url = `${base}${path}`;
    try {
      const res = await fetch(url);
      if (!res.ok) continue;
      const data = await res.json();
      /* Backend may return { vault: {...} } or the payload bare */
      const payload = data?.vault ?? data;
      if (hasVaultPayload(payload)) {
        console.info(`[ModelBudgetsTab] Vault loaded from: ${url}`);
        return payload as VaultData;
      }
    } catch {
      /* Network error or malformed JSON — try the next candidate */
    }
  }

  console.warn(
    "[ModelBudgetsTab] Vault unavailable. Every candidate 404'd or returned an empty payload.",
    { tried: VAULT_PATH_CANDIDATES.map((p) => `${base}${p}`) }
  );
  return null;
}

/* ============================================================
 * resolveLucideIcon
 *
 * The `badge` field inside models_vault.json is used verbatim
 * as the icon identifier. Whatever string the vault stores for
 * a provider is passed straight to the local SVG inventory:
 *
 *   <Icon name="deepseek" />
 *   <Icon name="openai-chatgpt" />
 *   <Icon name="lm-studio" />
 *   <Icon name="gemini-google" />
 *
 * No alias table, no normalisation, no hardcoded mapping —
 * the vault is the single source of truth for provider metadata
 * and its visual identity alike.
 * ============================================================ */
function resolveLucideIcon(badge?: string): string {
  return (badge || "").trim();
}

function ProviderIcon({ badge, size = 15 }: { badge?: string; size?: number }) {
  const name = resolveLucideIcon(badge);
  if (!name) {
    return <Sparkles size={size} className="text-primary" />;
  }
  return <Icon name={name} size={size} />;
}

/* ============================================================
 * Intelligent multi-tier heuristic to resolve model limits.
 *
 * Resolution priority:
 *   Tier 1 — Hardware metadata cached from local inspection
 *            (populated by /api/config/fetch-models).
 *   Tier 2 — Explicit token-suffix patterns (-128k, :64k, ...).
 *   Tier 3 — Well-known cloud architecture specs (Gemini, Claude,
 *            GPT, DeepSeek, Qwen, Mistral, Llama, ...).
 *   Tier 4 — Conservative standard default.
 *
 * This resolver is the single source of truth for both the value
 * shown in the UI and the value persisted to disk. Any divergence
 * between "what the user sees" and "what gets saved" is therefore
 * impossible by construction.
 * ============================================================ */
function resolveIntelligentLimits(
  modelId: string,
  metadataVault: Record<string, any>
): { contextWindow: number; maxOutput: number; source: string } {
  const mid = modelId.toLowerCase();

  /* Tier 1: Hardware metadata lookup from local cache */
  const meta = metadataVault[modelId] || metadataVault[mid];
  if (meta && (meta.max_context_length || meta.context_length)) {
    const raw = parseInt(String(meta.max_context_length || meta.context_length), 10);
    if (!isNaN(raw) && raw > 0) {
      const output = raw >= 32768 ? 4096 : 1500;
      return { contextWindow: raw, maxOutput: output, source: "Hardware Inspection" };
    }
  }

  /* Tier 2: Explicit token-suffix patterns */
  if (mid.includes("-1m") || mid.includes(":1m")) return { contextWindow: 1048576, maxOutput: 8192, source: "1M Pattern" };
  if (mid.includes("-128k") || mid.includes(":128k")) return { contextWindow: 131072, maxOutput: 4096, source: "128K Pattern" };
  if (mid.includes("-64k") || mid.includes(":64k")) return { contextWindow: 65536, maxOutput: 4096, source: "64K Pattern" };
  if (mid.includes("-32k") || mid.includes(":32k")) return { contextWindow: 32768, maxOutput: 4096, source: "32K Pattern" };
  if (mid.includes("-16k") || mid.includes(":16k")) return { contextWindow: 16384, maxOutput: 2048, source: "16K Pattern" };

  /* Tier 3: Well-known cloud architectures (spec-based, not provider-based) */
  if (mid.includes("gemini")) return { contextWindow: 1048576, maxOutput: 8192, source: "Google Gemini 1M Spec" };
  if (mid.includes("claude")) return { contextWindow: 200000, maxOutput: 8192, source: "Anthropic Claude 200K Spec" };
  if (mid.includes("gpt-4o") || mid.includes("gpt-4") || mid.includes("o1") || mid.includes("o3"))
    return { contextWindow: 128000, maxOutput: 4096, source: "OpenAI 128K Spec" };
  if (mid.includes("deepseek")) return { contextWindow: 64000, maxOutput: 8192, source: "DeepSeek 64K Spec" };
  if (mid.includes("gigachat")) return { contextWindow: 32768, maxOutput: 4096, source: "GigaChat 32K Spec" };
  if (mid.includes("llama-3.1") || mid.includes("llama-3.2") || mid.includes("llama-3.3"))
    return { contextWindow: 131072, maxOutput: 4096, source: "Llama 3.x 128K Spec" };
  if (mid.includes("qwen2.5") || mid.includes("qwen3"))
    return { contextWindow: 32768, maxOutput: 4096, source: "Qwen 32K Spec" };
  if (mid.includes("mistral") || mid.includes("codestral"))
    return { contextWindow: 32768, maxOutput: 4096, source: "Mistral 32K Spec" };
  if (mid.includes("gemma")) return { contextWindow: 8192, maxOutput: 2048, source: "Gemma 8K Spec" };

  /* Tier 4: Safe fallback */
  return { contextWindow: 8192, maxOutput: 1500, source: "Standard Default" };
}

/* ============================================================
 * Component
 * ============================================================ */
export function ModelBudgetsTab(props: ModelBudgetsTabProps) {
  const [budgets, setBudgets] = useState<ModelBudgetsMap>({});
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});
  const [vault, setVault] = useState<VaultData | null>(null);

  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const [openProviders, setOpenProviders] = useState<Record<string, boolean>>({});
  const [expandedModels, setExpandedModels] = useState<Record<string, boolean>>({});
  const [modelRatios, setModelRatios] = useState<Record<string, BudgetRatios>>({});

  /* -------------------------------
   * Fetch budgets, metadata & vault
   * ------------------------------- */
  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      try {
        const base = resolveBackendBase();

        const [budgetsRes, metaRes, vaultData] = await Promise.all([
          fetch(`${base}/api/config/model-budgets`).catch(() => null),
          fetch(`${base}/api/config/models-metadata`).catch(() => null),
          probeVaultEndpoint(base),
        ]);

        /* --- Budgets --- */
        if (budgetsRes && budgetsRes.ok) {
          const bData = await budgetsRes.json();
          if (bData.ok && bData.budgets) {
            setBudgets(bData.budgets);
            const loadedRatios: Record<string, BudgetRatios> = {};
            for (const [mId, bConf] of Object.entries(
              bData.budgets as Record<string, any>
            )) {
              if (bConf.ratios) loadedRatios[mId] = bConf.ratios;
            }
            setModelRatios(loadedRatios);
          }
        }

        /* --- Metadata --- */
        if (metaRes && metaRes.ok) {
          const mData = await metaRes.json();
          if (mData.ok && mData.metadata) setMetadataVault(mData.metadata);
        }

        /* --- Vault (models_vault.json) --- */
        if (vaultData) setVault(vaultData);
      } catch (err: any) {
        console.error("Failed to load model budgets / vault:", err);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  /* ============================================================
   * Granular provider groups — 100% driven by models_vault.json.
   *
   * Provider metadata (name, badge, models) is read straight from
   * the vault. The `badge` field is passed verbatim as the icon
   * name to the local <Icon /> component, so whatever identifier
   * sits in models_vault.json is what gets rendered.
   * ============================================================ */
  const providerGroups = useMemo<GranularProviderGroup[]>(() => {
    const groups: GranularProviderGroup[] = [];

    /* Resolve data from vault, with props as fallback */
    const clouds: CloudProviderVaultItem[] =
      vault?.cloud_vault ?? props.cloudProviders ?? [];
    const customs: CustomEndpoint[] =
      vault?.custom_endpoints ?? props.customEndpoints ?? [];
    const lm = vault?.lmstudio_vault ?? (props.lmStudioSettings as any);
    const ol = vault?.ollama_vault ?? (props.ollamaSettings as any);

    const scanned: string[] = vault?.scanned_models ?? [];
    const avail: string[] =
      scanned.length > 0 ? scanned : props.availableModels ?? [];

    /* -------- 1. Cloud providers (fully dynamic, no hardcoded names) -------- */
    for (const cp of clouds) {
      const uniqueModels = Array.from(
        new Set(
          [...(cp.savedModels || []), ...(cp.popularModels || []), cp.model].filter(
            Boolean
          ) as string[]
        )
      );
      if (uniqueModels.length === 0) continue;

      groups.push({
        groupId: `provider_${cp.id}`,
        providerName: cp.name,
        providerKey: cp.id,
        badge: cp.badge || "",
        icon: <ProviderIcon badge={cp.badge} />,
        models: uniqueModels.map((m) => {
          const limits = resolveIntelligentLimits(m, metadataVault);
          return {
            modelId: m,
            displayName: m,
            defaultContextWindow: limits.contextWindow,
            defaultMaxOutput: limits.maxOutput,
          };
        }),
      });
    }

    /* -------- 2. LM Studio (from lmstudio_vault) -------- */
    if (lm) {
      const lmModels = Array.from(
        new Set(
          [...avail, ...(lm.savedModels || []), lm.model].filter(Boolean) as string[]
        )
      );
      if (lmModels.length > 0) {
        groups.push({
          groupId: "provider_lmstudio",
          providerName: "LM Studio",
          providerKey: "lmstudio",
          badge: lm.badge || "",
          icon: <ProviderIcon badge={lm.badge} />,
          models: lmModels.map((m) => {
            const limits = resolveIntelligentLimits(m, metadataVault);
            return {
              modelId: m,
              displayName: m,
              defaultContextWindow: limits.contextWindow,
              defaultMaxOutput: limits.maxOutput,
            };
          }),
        });
      }
    }

    /* -------- 3. Ollama (from ollama_vault) -------- */
    if (ol) {
      const olModels = Array.from(
        new Set([...(ol.savedModels || []), ol.model].filter(Boolean) as string[])
      );
      if (olModels.length > 0) {
        groups.push({
          groupId: "provider_ollama",
          providerName: "Ollama",
          providerKey: "ollama",
          badge: ol.badge || "",
          icon: <ProviderIcon badge={ol.badge} />,
          models: olModels.map((m) => {
            const limits = resolveIntelligentLimits(m, metadataVault);
            return {
              modelId: m,
              displayName: m,
              defaultContextWindow: limits.contextWindow,
              defaultMaxOutput: limits.maxOutput,
            };
          }),
        });
      }
    }

    /* -------- 4. Custom endpoints (from custom_endpoints) -------- */
    for (const ce of customs) {
      const ceModels = Array.from(
        new Set([...(ce.savedModels || []), ce.defaultModel].filter(Boolean) as string[])
      );
      if (ceModels.length === 0) continue;

      const badge = (ce as any).badge || "";

      groups.push({
        groupId: `provider_custom_${ce.id}`,
        providerName: ce.name,
        providerKey: ce.providerId || ce.id,
        badge,
        icon: <ProviderIcon badge={badge} />,
        models: ceModels.map((m) => {
          const limits = resolveIntelligentLimits(m, metadataVault);
          return {
            modelId: m,
            displayName: m,
            defaultContextWindow: limits.contextWindow,
            defaultMaxOutput: limits.maxOutput,
          };
        }),
      });
    }

    return groups;
  }, [vault, props, metadataVault]);

  /* -------------------------------
   * Accordion helpers
   * ------------------------------- */
  const toggleProvider = (groupId: string) =>
    setOpenProviders((prev) => ({ ...prev, [groupId]: !prev[groupId] }));

  const toggleModel = (modelId: string) =>
    setExpandedModels((prev) => ({ ...prev, [modelId]: !prev[modelId] }));

  const expandAllProviders = () => {
    const next: Record<string, boolean> = {};
    for (const g of providerGroups) next[g.groupId] = true;
    setOpenProviders(next);
  };

  const collapseAllProviders = () => setOpenProviders({});

  /* -------------------------------
   * Budget handlers
   * ------------------------------- */
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
    const sum =
      current.system + current.summary + current.recent + current.execution + current.current;
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
    const newSum =
      normalized.system +
      normalized.summary +
      normalized.recent +
      normalized.execution +
      normalized.current;
    normalized.recent += 100 - newSum;

    setModelRatios((prev) => ({ ...prev, [modelId]: normalized }));
    showToast.info("Distribution Normalized", "Section allocation rebalanced to exactly 100%.");
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
      "Limits Auto-Detected",
      `${modelId} → ${resolved.contextWindow.toLocaleString()} tokens (${resolved.source})`
    );
  };

  /* ============================================================
   * handleSaveAll — persist every model the user has touched.
   *
   * A model is considered "touched" when it appears in either:
   *   - `budgets`      → user edited context_window / max_output_tokens
   *   - `modelRatios`  → user edited the interactive distribution
   *
   * For any touched model that lacks explicit budget limits, the
   * intelligent resolver is invoked so the value persisted to disk
   * is *identical* to the value shown in the UI. Without this,
   * editing only the ratios on a 1M-context model (Gemini) would
   * silently downgrade its context window to a hard-coded 8K.
   * ============================================================ */
  const handleSaveAll = async () => {
    setSaving(true);
    try {
      const base = resolveBackendBase();

      /* Build a model → provider name lookup from the live groups
       * so every saved entry carries an accurate provider_name. */
      const providerByModel: Record<string, string> = {};
      for (const group of providerGroups) {
        for (const item of group.models) {
          providerByModel[item.modelId] = group.providerName;
        }
      }

      /* Union of every model the user has interacted with. */
      const touchedModelIds = new Set<string>([
        ...Object.keys(budgets),
        ...Object.keys(modelRatios),
      ]);

      const payloadBudgets: Record<string, any> = {};

      for (const modelId of touchedModelIds) {
        const explicitLimits = budgets[modelId];
        const ratios = modelRatios[modelId];

        /* Prefer the user-edited limits; otherwise fall back to the
         * intelligent resolver — never a hard-coded magic number. */
        const resolved = resolveIntelligentLimits(modelId, metadataVault);

        const entry: Record<string, any> = explicitLimits
          ? { ...explicitLimits }
          : {
              context_window: resolved.contextWindow,
              max_output_tokens: resolved.maxOutput,
            };

        if (!entry.provider_name) {
          entry.provider_name = providerByModel[modelId] || "Unknown Provider";
        }

        if (ratios) {
          entry.ratios = ratios;
        }

        payloadBudgets[modelId] = entry;
      }

      const res = await fetch(`${base}/api/config/model-budgets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ budgets: payloadBudgets }),
      });

      const data = await res.json();
      if (data.ok) {
        showToast.success(
          "Budgets Saved",
          "Context windows, output buffers, and section distributions persisted to disk."
        );
      } else {
        showToast.error("Save Failed", data.detail || "Unable to persist budget configuration.");
      }
    } catch (err: any) {
      showToast.error("Network Error", err.message);
    } finally {
      setSaving(false);
    }
  };

  /* -------------------------------
   * Render
   * ------------------------------- */
  return (
    <div className="w-full max-w-7xl space-y-6 pt-6 m-auto">
      {/* Header banner & global control actions */}
      <div className="p-4 rounded-sm border border-border bg-gigbg flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 shadow-xs">
        <div>
          <div className="flex items-center gap-2">
            <Gauge size={16} className="text-primary" />
            <h3 className="text-sm font-black text-foreground uppercase tracking-wide">
              Per-Model Context Windows & Token Budget Allocation
            </h3>
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Configure context windows, completion buffers, and per-section token allocation
            for every provider registered in{" "}
            <span className="font-mono text-primary">PELDRUN</span>.
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={
              Object.values(openProviders).some(Boolean)
                ? collapseAllProviders
                : expandAllProviders
            }
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
            <span>{saving ? "Saving Changes…" : "Save All Budgets"}</span>
          </Button>
        </div>
      </div>

      {/* Loading state */}
      {loading && providerGroups.length === 0 && (
        <div className="p-8 text-center rounded-lg border border-dashed border-border bg-card/50">
          <Sparkles size={28} className="mx-auto text-primary mb-2 animate-pulse" />
          <p className="text-sm text-muted-foreground">
            Loading provider registry from{" "}
            <span className="font-mono">models_vault.json</span>…
          </p>
        </div>
      )}

      {/* Empty state */}
      {!loading && providerGroups.length === 0 && (
        <div className="p-8 text-center rounded-lg border border-dashed border-border bg-card/50">
          <Sparkles size={28} className="mx-auto text-muted-foreground mb-2" />
          <p className="text-sm text-muted-foreground">
            No providers registered in{" "}
            <span className="font-mono">models_vault.json</span>. Add providers in the
            Models tab to define their budgets here.
          </p>
        </div>
      )}

      {/* Accordion providers — dynamically built from vault */}
      <div className="space-y-3.5">
        {providerGroups.map((group) => {
          const isOpen = Boolean(openProviders[group.groupId]);

          return (
            <div
              key={group.groupId}
              className="rounded-sm border border-border bg-card overflow-hidden shadow-xs transition-all"
            >
              {/* Provider accordion header */}
              <button
                type="button"
                onClick={() => toggleProvider(group.groupId)}
                className="w-full px-4 py-3 bg-muted hover:bg-custom transition flex items-center justify-between cursor-pointer border-b border-border/40"
              >
                <div className="flex items-center gap-2.5">
                  <div className="p-1">
                    {group.icon}
                  </div>
                  <span className="text-xs font-bold text-foreground uppercase tracking-wider">
                    {group.providerName}
                  </span>

                  <span className="text-[10px] px-2 py-0.5 rounded-xs bg-custom text-primary font-mono font-bold shadow-sm">
                    {group.models.length} {group.models.length === 1 ? "model" : "models"}
                  </span>
                </div>

                <div className="flex items-center gap-2 text-muted-foreground">
                  <span className="text-[11px] font-mono hidden sm:inline">
                    {isOpen ? "Collapse" : "Expand"}
                  </span>
                  {isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                </div>
              </button>

              {/* Models list */}
              {isOpen && (
                <div className="divide-y bg-muted divide-border/50 p-2 sm:p-3 space-y-2">
                  {group.models.map((item) => {
                    const modelConfig = budgets[item.modelId] || {
                      context_window: item.defaultContextWindow,
                      max_output_tokens: item.defaultMaxOutput,
                      provider_name: group.providerName,
                    };

                    const contextWindow =
                      modelConfig.context_window || item.defaultContextWindow;
                    const maxOutput =
                      modelConfig.max_output_tokens || item.defaultMaxOutput;
                    const effectiveInputBudget = Math.max(100, contextWindow - maxOutput);
                    const isExpanded = Boolean(expandedModels[item.modelId]);

                    const ratios = modelRatios[item.modelId] || { ...DEFAULT_RATIOS };
                    const sumRatios =
                      ratios.system +
                      ratios.summary +
                      ratios.recent +
                      ratios.execution +
                      ratios.current;
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
                        {/* Model row summary */}
                        <div className="p-3 bg-custom flex flex-col sm:flex-row sm:items-center justify-between gap-2.5">
                          <div className="flex items-center gap-2.5 truncate">
                            <button
                              type="button"
                              onClick={() => toggleModel(item.modelId)}
                              className="text-muted-foreground hover:text-foreground cursor-pointer"
                              title="Toggle configuration"
                            >
                              {isExpanded ? (
                                <ChevronDown size={14} />
                              ) : (
                                <ChevronRight size={14} />
                              )}
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
                              onClick={() =>
                                handleAutoDetect(item.modelId, group.providerName)
                              }
                              className="h-6 px-2 text-[10px] text-muted-foreground hover:text-primary flex items-center gap-1 cursor-pointer"
                              title="Auto-detect context limit from specifications"
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

                        {/* Interactive configuration panel */}
                        {isExpanded && (
                          <div className="p-3.5 bg-card/60 border-t border-border/50 space-y-4">
                            {/* Numeric inputs */}
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
                                      parseInt(e.target.value, 10) ||
                                        item.defaultContextWindow,
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
                                      parseInt(e.target.value, 10) ||
                                        item.defaultMaxOutput,
                                      group.providerName
                                    )
                                  }
                                  className="w-full bg-background border border-border rounded px-2.5 py-1 text-xs text-foreground font-mono shadow-xs"
                                />
                              </div>
                            </div>

                            {/* Interactive token budget controller */}
                            <div className="p-3 rounded-lg bg-background/80 border border-border/70 space-y-3">
                              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                                <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
                                  <SlidersHorizontal size={13} className="text-primary" />
                                  <span>Interactive Budget Distribution</span>
                                </div>

                                <div className="flex flex-wrap items-center gap-1">
                                  {RATIO_PRESETS.map((p) => (
                                    <button
                                      key={p.name}
                                      type="button"
                                      onClick={() =>
                                        handleApplyPreset(item.modelId, p.ratios)
                                      }
                                      className="px-2 py-0.5 rounded text-[10px] bg-muted hover:bg-primary/10 hover:text-primary border border-border/80 transition cursor-pointer"
                                    >
                                      {p.name}
                                    </button>
                                  ))}
                                </div>
                              </div>

                              {/* Segmented bar */}
                              <div className="w-full h-3 rounded bg-muted overflow-hidden flex shadow-inner cursor-pointer">
                                <div
                                  style={{ width: `${ratios.system}%` }}
                                  className="bg-sky-500 hover:brightness-110 transition-all"
                                  title={`System Prompt: ${ratios.system}% (${tokenSlices.system.toLocaleString()} tokens)`}
                                />
                                <div
                                  style={{ width: `${ratios.summary}%` }}
                                  className="bg-amber-500 hover:brightness-110 transition-all"
                                  title={`Conversation Summary: ${ratios.summary}% (${tokenSlices.summary.toLocaleString()} tokens)`}
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

                              {/* Sliders */}
                              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-2.5 pt-1 text-xs font-mono">
                                {/* System Prompt */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-sky-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-sky-500" />{" "}
                                      System Prompt
                                    </span>
                                    <span>{ratios.system}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="0"
                                    max="30"
                                    value={ratios.system}
                                    onChange={(e) =>
                                      handleUpdateRatio(
                                        item.modelId,
                                        "system",
                                        parseInt(e.target.value, 10)
                                      )
                                    }
                                    className="w-full accent-sky-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.system.toLocaleString()} tokens
                                  </span>
                                </div>

                                {/* Conversation Summary */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-amber-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-amber-500" />{" "}
                                      Conversation Summary
                                    </span>
                                    <span>{ratios.summary}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="0"
                                    max="40"
                                    value={ratios.summary}
                                    onChange={(e) =>
                                      handleUpdateRatio(
                                        item.modelId,
                                        "summary",
                                        parseInt(e.target.value, 10)
                                      )
                                    }
                                    className="w-full accent-amber-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.summary.toLocaleString()} tokens
                                  </span>
                                </div>

                                {/* Recent Turns */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-emerald-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-emerald-500" />{" "}
                                      Recent Turns
                                    </span>
                                    <span>{ratios.recent}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="10"
                                    max="80"
                                    value={ratios.recent}
                                    onChange={(e) =>
                                      handleUpdateRatio(
                                        item.modelId,
                                        "recent",
                                        parseInt(e.target.value, 10)
                                      )
                                    }
                                    className="w-full accent-emerald-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.recent.toLocaleString()} tokens
                                  </span>
                                </div>

                                {/* Execution State */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-violet-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-violet-500" />{" "}
                                      Execution State
                                    </span>
                                    <span>{ratios.execution}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="0"
                                    max="40"
                                    value={ratios.execution}
                                    onChange={(e) =>
                                      handleUpdateRatio(
                                        item.modelId,
                                        "execution",
                                        parseInt(e.target.value, 10)
                                      )
                                    }
                                    className="w-full accent-violet-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.execution.toLocaleString()} tokens
                                  </span>
                                </div>

                                {/* Current Message */}
                                <div className="p-2 rounded border border-border/60 bg-card/40 space-y-1">
                                  <div className="flex items-center justify-between text-[11px]">
                                    <span className="flex items-center gap-1 text-rose-500 font-semibold">
                                      <span className="h-2 w-2 rounded-full bg-rose-500" />{" "}
                                      Current Message
                                    </span>
                                    <span>{ratios.current}%</span>
                                  </div>
                                  <input
                                    type="range"
                                    min="0"
                                    max="30"
                                    value={ratios.current}
                                    onChange={(e) =>
                                      handleUpdateRatio(
                                        item.modelId,
                                        "current",
                                        parseInt(e.target.value, 10)
                                      )
                                    }
                                    className="w-full accent-rose-500 h-1.5 cursor-pointer"
                                  />
                                  <span className="text-[10px] text-muted-foreground block truncate">
                                    {tokenSlices.current.toLocaleString()} tokens
                                  </span>
                                </div>
                              </div>

                              {/* Validation status */}
                              <div className="flex items-center justify-between pt-1 border-t border-border/40 text-[11px]">
                                <div className="flex items-center gap-1.5">
                                  {isSumValid ? (
                                    <span className="flex items-center gap-1 text-emerald-600 dark:text-emerald-400 font-semibold">
                                      <CheckCircle2 size={12} /> Total allocation: 100%
                                    </span>
                                  ) : (
                                    <span className="flex items-center gap-1 text-amber-500 font-semibold">
                                      <AlertTriangle size={12} /> Total allocation:{" "}
                                      {sumRatios}% — requires rebalancing
                                    </span>
                                  )}
                                </div>

                                {!isSumValid && (
                                  <button
                                    type="button"
                                    onClick={() => handleNormalizeRatios(item.modelId)}
                                    className="text-[10px] text-primary hover:underline flex items-center gap-1 cursor-pointer font-bold"
                                  >
                                    <RotateCcw size={10} /> Rebalance to 100%
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
