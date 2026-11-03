// frontend/src/components/models/ModelInfoModal.tsx
"use client";

import React, { useEffect, useMemo, useState } from "react";
import {
  X,
  Cpu,
  Brain,
  Eye,
  Wrench,
  Database,
  Layers,
  HardDrive,
  FileCode,
  Zap,
  CheckCircle2,
  MinusCircle,
  RefreshCw,
  AlertCircle,
} from "lucide-react";
import {
  fetchModelFacts,
  getModelMetadata,
  inferModelCapabilities,
  setModelReasoningEffort,
  toggleModelReasoning,
  toggleModelTools,
  toggleModelVision,
} from "@/lib/modelMetadata";
import type {
  CapabilitySource,
  InferredCapabilities,
  ModelFacts,
} from "@/lib/modelMetadata";
import type { ModelMetadata } from "@/lib/types";

/* -------------------------------------------------------------------------- */
/* Source label helper                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Human-readable label for the origin of a capability fact. Shown in
 * tooltips next to each capability so the user knows whether the value
 * was set manually, reported by the provider, or inferred from an
 * external registry. Never shown inline (would clutter the compact
 * modal).
 */
function sourceLabel(source: string): string {
  switch (source) {
    case "manual":
      return "Set manually by you";
    case "provider_live":
      return "Reported by the provider (live)";
    case "provider_catalog":
      return "Reported by provider catalog";
    case "provider_metadata":
      return "Reported by provider metadata";
    case "model_metadata":
      return "Saved in model metadata";
    case "huggingface_config":
      return "From HuggingFace config.json";
    case "external_registry":
      return "From LiteLLM model registry";
    case "capability_probe":
      return "Confirmed by capability probe";
    case "runtime_observation":
      return "Observed at runtime";
    default:
      return "No information available";
  }
}

/* -------------------------------------------------------------------------- */
/* Public wrapper                                                             */
/* -------------------------------------------------------------------------- */

interface ModelInfoModalProps {
  isOpen: boolean;
  onClose: () => void;
  modelKey: string;
  explicitMeta?: ModelMetadata | null;

  /**
   * Canonical provider identifier (e.g. "ollama", "lmstudio",
   * "openai"). When provided, the modal fetches merged capability
   * facts from `/api/catalog/models/{id}` — which combines the vault,
   * LiteLLM, and HuggingFace sources. When absent, the modal falls
   * back to reading only the local vault (still truthful, but without
   * the external enrichment).
   */
  providerId?: string | null;

  /**
   * Optional connection id for composite-key lookups. Ignored when
   * `providerId` is not supplied.
   */
  connectionId?: string | null;
}

/**
 * Wrapper that defers hooks into a sub-component keyed by the
 * combination of `modelKey` and `providerId`.
 *
 * Two reasons for the split:
 *   1. React's rules of hooks forbid calling `useState` after an early
 *      `return null`. The sub-component lets the parent early-return
 *      without touching any hook.
 *   2. `key` forces a fresh mount whenever the user opens metadata for
 *      a different model or provider, so local state never goes stale.
 */
export function ModelInfoModal({
  isOpen,
  onClose,
  modelKey,
  explicitMeta,
  providerId,
  connectionId,
}: ModelInfoModalProps) {
  if (!isOpen || !modelKey) return null;
  return (
    <ModelInfoModalContent
      key={`${providerId || "_"}::${modelKey}`}
      onClose={onClose}
      modelKey={modelKey}
      explicitMeta={explicitMeta}
      providerId={providerId}
      connectionId={connectionId}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* Content                                                                    */
/* -------------------------------------------------------------------------- */

interface ContentProps {
  onClose: () => void;
  modelKey: string;
  explicitMeta?: ModelMetadata | null;
  providerId?: string | null;
  connectionId?: string | null;
}

function ModelInfoModalContent({
  onClose,
  modelKey,
  explicitMeta,
  providerId,
  connectionId,
}: ContentProps) {
  const [meta, setMeta] = useState<ModelMetadata>(
    () =>
      explicitMeta ||
      getModelMetadata(modelKey) || { key: modelKey, type: "llm" }
  );

  const [facts, setFacts] = useState<ModelFacts | null>(null);
  const [loadingFacts, setLoadingFacts] = useState(false);
  const [factsError, setFactsError] = useState<string | null>(null);

  // ------------------------------------------------------------------
  // Fetch merged facts on mount when providerId is available.
  //
  // This is a best-effort network call — the modal renders immediately
  // with vault-only data and upgrades to the merged view once the
  // fetch resolves. A failure leaves the modal in vault-only mode.
  // ------------------------------------------------------------------
  useEffect(() => {
    if (!providerId) return;
    let cancelled = false;
    setLoadingFacts(true);
    setFactsError(null);

    fetchModelFacts(providerId, modelKey, connectionId)
      .then((result) => {
        if (cancelled) return;
        if (result) {
          setFacts(result);
        } else {
          setFactsError("Catalog lookup unavailable — showing local metadata only.");
        }
        setLoadingFacts(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setFactsError(String(err || "Catalog lookup failed"));
        setLoadingFacts(false);
      });

    return () => {
      cancelled = true;
    };
  }, [providerId, modelKey, connectionId]);

  // ------------------------------------------------------------------
  // Unified capability view.
  //
  // Vault-only caps are computed first (they carry paramsDisplay,
  // quantDisplay, etc. from the local metadata). When merged facts
  // arrive, we overlay the capability fields with the fresh values
  // from the backend while keeping the vault-derived descriptive
  // fields (params, quant, arch).
  // ------------------------------------------------------------------
  const caps: InferredCapabilities = useMemo(() => {
    const vaultCaps = inferModelCapabilities(
      modelKey,
      meta,
      providerId || null,
      connectionId || null
    );
    if (!facts) return vaultCaps;

    const stateFromFact = (
      known: boolean,
      value: boolean
    ): "supported" | "unsupported" | "unknown" =>
      !known ? "unknown" : value ? "supported" : "unsupported";

    const contextFromFact =
      facts.context_window !== null && facts.context_window !== undefined
        ? facts.context_window >= 1024
          ? `${Math.round(facts.context_window / 1024)}K ctx`
          : `${facts.context_window} ctx`
        : vaultCaps.contextDisplay;

    return {
      ...vaultCaps,
      // Expose provider_id so ReasoningBox can decide whether "Run
      // Scan Models" is actionable advice for this provider.
      provider_id: providerId || vaultCaps.provider_id,
      isVision: facts.vision.value,
      isTools: facts.tools.value,
      isReasoning: facts.reasoning.value,

      visionState: stateFromFact(facts.vision.known, facts.vision.value),
      toolsState: stateFromFact(facts.tools.known, facts.tools.value),
      reasoningState: stateFromFact(
        facts.reasoning.known,
        facts.reasoning.value
      ),

      visionSource: (facts.vision.source as CapabilitySource) || "unknown",
      toolsSource: (facts.tools.source as CapabilitySource) || "unknown",
      reasoningSource:
        (facts.reasoning.source as CapabilitySource) || "unknown",

      reasoningMode: facts.reasoning_mode || vaultCaps.reasoningMode,
      reasoningEffortLevels:
        facts.reasoning_effort_levels.length > 0
          ? facts.reasoning_effort_levels
          : vaultCaps.reasoningEffortLevels,
      reasoningDefaultEffort:
        facts.reasoning_default_effort ?? vaultCaps.reasoningDefaultEffort,
      reasoningMinBudgetTokens:
        facts.reasoning_min_budget_tokens ??
        vaultCaps.reasoningMinBudgetTokens,
      reasoningMaxBudgetTokens:
        facts.reasoning_max_budget_tokens ??
        vaultCaps.reasoningMaxBudgetTokens,

      contextDisplay: contextFromFact,
    };
  }, [facts, meta, modelKey, providerId, connectionId]);

  // ------------------------------------------------------------------
  // Toggle handlers.
  //
  // Each handler:
  //   1. Persists the user's choice to localStorage + server.
  //   2. Updates local `meta` to mirror the write.
  //   3. Updates local `facts` in place so the UI reacts immediately
  //      without refetching.
  // ------------------------------------------------------------------
  const updateLocalMeta = (
    field: "vision" | "trained_for_tool_use" | "reasoning",
    value: boolean
  ) => {
    setMeta((prev) => {
      const currentDetails =
        ((prev as unknown as { capability_details?: Record<string, unknown> })
          .capability_details) || {};
      const reasoningDetail =
        (currentDetails.reasoning as Record<string, unknown> | undefined) || {};

      const nextDetails =
        field === "reasoning"
          ? {
              ...currentDetails,
              reasoning: {
                ...reasoningDetail,
                mode: "toggle",
                state: value ? "supported" : "unsupported",
              },
            }
          : currentDetails;

      return {
        ...prev,
        capabilities: { ...prev.capabilities, [field]: value },
        capability_sources: {
          ...((prev as unknown as {
            capability_sources?: Record<string, string>;
          }).capability_sources || {}),
          [field]: "manual",
        },
        capability_details: nextDetails,
      } as unknown as ModelMetadata;
    });

    // Mirror into facts so the UI reflects the change immediately.
    setFacts((prev) => {
      if (!prev) return prev;
      const updated = { ...prev };
      if (field === "vision") {
        updated.vision = { value, source: "manual", known: true };
      } else if (field === "trained_for_tool_use") {
        updated.tools = { value, source: "manual", known: true };
      } else {
        updated.reasoning = { value, source: "manual", known: true };
        updated.reasoning_mode = "toggle";
      }
      return updated;
    });
  };

  const handleToggleReasoning = () => {
    const next = toggleModelReasoning(modelKey);
    updateLocalMeta("reasoning", next);
  };

  const handleToggleVision = () => {
    const next = toggleModelVision(modelKey);
    updateLocalMeta("vision", next);
  };

  const handleToggleTools = () => {
    const next = toggleModelTools(modelKey);
    updateLocalMeta("trained_for_tool_use", next);
  };

  const handleSetEffort = (effort: string) => {
    setModelReasoningEffort(modelKey, effort);
    setMeta((prev) => {
      const currentDetails =
        ((prev as unknown as { capability_details?: Record<string, unknown> })
          .capability_details) || {};
      const reasoningDetail =
        (currentDetails.reasoning as Record<string, unknown> | undefined) || {};
      return {
        ...prev,
        capabilities: { ...prev.capabilities, reasoning: true },
        capability_sources: {
          ...((prev as unknown as {
            capability_sources?: Record<string, string>;
          }).capability_sources || {}),
          reasoning: "manual",
        },
        capability_details: {
          ...currentDetails,
          reasoning: {
            ...reasoningDetail,
            mode: "effort",
            effort,
            state: "supported",
          },
        },
      } as unknown as ModelMetadata;
    });
    setFacts((prev) =>
      prev
        ? {
            ...prev,
            reasoning: { value: true, source: "manual", known: true },
            reasoning_mode: "effort",
            reasoning_default_effort: effort,
          }
        : prev
    );
  };

  /* ------------------------- Display helpers ------------------------ */

  const formatBytes = (bytes?: number | null) => {
    if (!bytes || bytes <= 0) return null;
    const gb = bytes / (1024 * 1024 * 1024);
    if (gb >= 1) return `${gb.toFixed(2)} GB`;
    const mb = bytes / (1024 * 1024);
    return `${mb.toFixed(1)} MB`;
  };

  const loadedConfig =
    meta.loaded_instances && meta.loaded_instances[0]?.config;

  const currentEffort =
    (caps.reasoningDefaultEffort as string | null) ||
    (caps.reasoningEffortLevels[0] as string | undefined) ||
    null;

  /* ------------------------- Render --------------------------------- */

  return (
    <div className="fixed inset-0 z-[60] bg-background/80 backdrop-blur-xs flex items-center justify-center p-4 animate-in fade-in">
      <div className="w-full max-w-md bg-card border border-border rounded-xl shadow-2xl overflow-hidden flex flex-col font-sans">
        {/* Header */}
        <div className="px-5 py-4 border-b border-border/80 flex items-center justify-between bg-muted/30">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center text-primary">
              <Cpu size={18} />
            </div>
            <div className="min-w-0">
              <h3
                className="font-heading font-semibold text-sm text-foreground truncate max-w-[280px]"
                title={meta.display_name || modelKey}
              >
                {meta.display_name || modelKey}
              </h3>
              <p className="text-[11px] text-muted-foreground font-mono truncate max-w-[280px]">
                {meta.key || modelKey}
                {providerId ? ` · ${providerId}` : ""}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors cursor-pointer"
          >
            <X size={16} />
          </button>
        </div>

        {/* Facts loading banner */}
        {loadingFacts && (
          <div className="px-5 py-2 border-b border-border/60 bg-sky-500/5 text-[11px] text-sky-600 dark:text-sky-400 flex items-center gap-2">
            <RefreshCw size={11} className="animate-spin" />
            <span>Loading capability facts from catalog…</span>
          </div>
        )}
        {!loadingFacts && factsError && (
          <div className="px-5 py-2 border-b border-border/60 bg-amber-500/5 text-[11px] text-amber-600 dark:text-amber-400 flex items-center gap-2">
            <AlertCircle size={11} />
            <span>{factsError}</span>
          </div>
        )}

        {/* Capability boxes */}
        <div className="px-5 py-3 border-b border-border/60 bg-background/50 space-y-2">
          <ReasoningBox
            caps={caps}
            currentEffort={currentEffort}
            onToggle={handleToggleReasoning}
            onSetEffort={handleSetEffort}
          />
          <VisionBox caps={caps} onToggle={handleToggleVision} />
          <ToolsBox caps={caps} onToggle={handleToggleTools} />
        </div>

        {/* Technical Specs Grid */}
        <div className="p-5 space-y-3.5 text-xs overflow-y-auto max-h-[50vh]">
          <div className="grid grid-cols-2 gap-3">
            <div className="p-2.5 rounded-lg border border-border/70 bg-card/60 space-y-1">
              <span className="text-[10px] text-muted-foreground uppercase tracking-wider font-semibold block flex items-center gap-1">
                <Database size={11} className="text-primary" /> Context Window
              </span>
              <span className="font-mono font-bold text-foreground">
                {meta.max_context_length
                  ? `${meta.max_context_length.toLocaleString()} tokens (${Math.round(
                      meta.max_context_length / 1024
                    )}K)`
                  : caps.contextDisplay || "Not reported by provider"}
              </span>
            </div>

            <div className="p-2.5 rounded-lg border border-border/70 bg-card/60 space-y-1">
              <span className="text-[10px] text-muted-foreground uppercase tracking-wider font-semibold block flex items-center gap-1">
                <Layers size={11} className="text-primary" /> Parameters
              </span>
              <span className="font-mono font-bold text-foreground">
                {meta.params_string || "Not reported"}
              </span>
              {meta.publisher && (
                <span className="text-[10px] text-muted-foreground font-mono block truncate">
                  By {meta.publisher}
                </span>
              )}
            </div>

            <div className="p-2.5 rounded-lg border border-border/70 bg-card/60 space-y-1">
              <span className="text-[10px] text-muted-foreground uppercase tracking-wider font-semibold block flex items-center gap-1">
                <HardDrive size={11} className="text-primary" /> Quantization
              </span>
              <span className="font-mono font-bold text-foreground">
                {meta.quantization?.name || "Not reported"}
              </span>
              {meta.quantization?.bits_per_weight && (
                <span className="text-[10px] text-muted-foreground font-mono block">
                  {meta.quantization.bits_per_weight} bits per weight
                </span>
              )}
            </div>

            <div className="p-2.5 rounded-lg border border-border/70 bg-card/60 space-y-1">
              <span className="text-[10px] text-muted-foreground uppercase tracking-wider font-semibold block flex items-center gap-1">
                <FileCode size={11} className="text-primary" /> Architecture
              </span>
              <span className="font-mono font-bold text-foreground capitalize">
                {meta.architecture || "Not reported"}
              </span>
              {meta.format && (
                <span className="text-[10px] text-muted-foreground font-mono uppercase block">
                  Format: {meta.format}
                </span>
              )}
            </div>
          </div>

          {formatBytes(meta.size_bytes) && (
            <div className="flex items-center justify-between p-2 rounded-lg bg-muted/40 font-mono text-[11px]">
              <span className="text-muted-foreground">Disk Size:</span>
              <span className="font-bold text-foreground">
                {formatBytes(meta.size_bytes)}
              </span>
            </div>
          )}

          {loadedConfig && (
            <div className="p-3 rounded-lg border border-border bg-muted/20 space-y-2">
              <span className="text-[10px] font-bold text-foreground uppercase tracking-wider block flex items-center gap-1">
                <Zap size={12} className="text-amber-500" /> Loaded Instance
                Runtime Config
              </span>
              <div className="grid grid-cols-2 gap-2 text-[11px] font-mono">
                <div>
                  <span className="text-muted-foreground">KV Cache GPU: </span>
                  <span className="font-semibold">
                    {loadedConfig.offload_kv_cache_to_gpu ? "Yes" : "No"}
                  </span>
                </div>
                <div>
                  <span className="text-muted-foreground">Flash Attention: </span>
                  <span className="font-semibold">
                    {loadedConfig.flash_attention ? "Enabled" : "Disabled"}
                  </span>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-border/80 flex items-center justify-between bg-muted/20 text-xs text-muted-foreground font-mono">
          <span>Type: {meta.type || "llm"}</span>
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1 rounded bg-secondary hover:bg-secondary/80 text-secondary-foreground font-sans font-medium transition cursor-pointer"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
/* ========================================================================== */
/* Status badge helper — shared by all three boxes                            */
/* ========================================================================== */

/**
 * Render a prominent status badge that unambiguously communicates the
 * current on/off/supported state. This replaces the previous design
 * where the "Active" button was the only visual indicator of state.
 *
 * Variants:
 *   - green:     definitively ON (confirmed by provider)
 *   - amber:     ON but manually enabled (not provider-confirmed)
 *   - muted-off: user explicitly disabled (reversible)
 *   - muted-no:  provider says not supported (final)
 *   - unknown:   no information, user can enable manually
 */
type StatusVariant = "on-confirmed" | "on-manual" | "off-manual" | "off-provider" | "unknown";

function StatusBadge({ variant }: { variant: StatusVariant }) {
  const config: Record<
    StatusVariant,
    { label: string; icon: React.ReactNode; classes: string }
  > = {
    "on-confirmed": {
      label: "ON",
      icon: <CheckCircle2 size={9} />,
      classes: "bg-emerald-500 text-white",
    },
    "on-manual": {
      label: "ON",
      icon: <CheckCircle2 size={9} />,
      classes: "bg-amber-500 text-white",
    },
    "off-manual": {
      label: "OFF",
      icon: <MinusCircle size={9} />,
      classes: "bg-muted text-muted-foreground border border-border",
    },
    "off-provider": {
      label: "Not supported",
      icon: <MinusCircle size={9} />,
      classes: "bg-muted text-muted-foreground border border-border",
    },
    unknown: {
      label: "Not reported",
      icon: <AlertCircle size={9} />,
      classes: "bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/30",
    },
  };
  const cfg = config[variant];
  return (
    <span
      className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-sm text-[9px] font-bold uppercase tracking-wide ${cfg.classes}`}
    >
      {cfg.icon}
      {cfg.label}
    </span>
  );
}

/* ========================================================================== */
/* ReasoningBox                                                               */
/* ========================================================================== */

interface ReasoningBoxProps {
  caps: InferredCapabilities;
  currentEffort: string | null;
  onToggle: () => void;
  onSetEffort: (effort: string) => void;
}

/**
 * State matrix (redesigned to be unambiguous):
 *
 *   mode=automatic, isOn=true   → green panel, "ALWAYS ON", no button
 *   mode=adaptive               → sky panel, "ADAPTIVE", no button
 *
 *   isOn=true, source=confirmed → green panel, badge "ON", "Confirmed by
 *                                 provider", button "Turn Off"
 *   isOn=true, source=manual    → amber panel, badge "ON", "Enabled by
 *                                 you", "⚠ Not confirmed by provider",
 *                                 button "Turn Off"
 *
 *   isOn=false, source=manual   → muted panel, badge "OFF", "Disabled by
 *                                 you", button "Enable"
 *   isOn=false, source=provider → muted panel, badge "Not supported",
 *                                 no button
 *   isOn=false, source=unknown  → dashed amber, badge "Not reported",
 *                                 button "Enable"
 */
function ReasoningBox({
  caps,
  currentEffort,
  onToggle,
  onSetEffort,
}: ReasoningBoxProps) {
  const mode = caps.reasoningMode;
  const source = caps.reasoningSource;
  const isOn = caps.isReasoning;

  // Providers that expose a live capability endpoint (and therefore
  // benefit from running Scan Models again). For all other providers,
  // re-scanning will not produce new capability data — the user must
  // rely on the manual override or wait for the provider to add an
  // endpoint. This function has no hardcoded model names; it only
  // classifies PROVIDER families that have a documented capability
  // endpoint.
  // Which providers expose a live capability endpoint, PER capability.
  //
  // Ollama's /api/show returns a `capabilities` array that includes
  // both "tools" and "thinking" — so a rescan can detect either.
  //
  // LM Studio's /api/v0/models declares `capabilities: ["tool_use"]`
  // for tool support and uses `type: "vlm"` for vision. It has NO
  // field for reasoning, so rescanning will NEVER change reasoning
  // state on LM Studio.
  //
  // Google's model endpoint exposes neither tools nor reasoning.
  const providerSupportsLiveReasoningScan =
    caps.provider_id === "ollama";
  const providerSupportsLiveToolsScan =
    caps.provider_id === "ollama" ||
    caps.provider_id === "lmstudio" ||
    caps.provider_id === "lm_studio" ||
    caps.provider_id === "lm-studio";
  const providerSupportsLiveVisionScan =
    caps.provider_id === "ollama" ||
    caps.provider_id === "lmstudio" ||
    caps.provider_id === "lm_studio" ||
    caps.provider_id === "lm-studio";
  const isManual = source === "manual";
  const isProviderConfirmed =
    source === "provider_live" ||
    source === "provider_catalog" ||
    source === "provider_metadata" ||
    source === "external_registry" ||
    source === "huggingface_config";

  // -------- 1. Native reasoning, always on (no user control) --------
  if (mode === "automatic" && isOn) {
    return (
      <div className="flex items-center justify-between p-2.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10">
        <div className="flex items-start gap-2 min-w-0">
          <Brain size={16} className="text-emerald-500 shrink-0 mt-0.5" />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs font-semibold text-foreground">
                Reasoning / Thinking Mode
              </span>
              <StatusBadge variant="on-confirmed" />
            </div>
            <span className="text-[10px] text-emerald-600 dark:text-emerald-400 font-semibold block mt-0.5">
              Always on — built into this model
            </span>
            <span
              className="text-[9px] text-muted-foreground block truncate"
              title={sourceLabel(source)}
            >
              {sourceLabel(source)}
            </span>
          </div>
        </div>
        <span className="flex items-center gap-1 px-2 py-1 rounded text-[10px] font-bold bg-emerald-500 text-white shrink-0">
          <CheckCircle2 size={10} /> ALWAYS ON
        </span>
      </div>
    );
  }

  // -------- 2. Adaptive (provider decides) --------
  if (mode === "adaptive") {
    return (
      <div className="flex items-center justify-between p-2.5 rounded-lg border border-sky-500/40 bg-sky-500/10">
        <div className="flex items-start gap-2 min-w-0">
          <Brain size={16} className="text-sky-500 shrink-0 mt-0.5" />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs font-semibold text-foreground">
                Reasoning / Thinking Mode
              </span>
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-sm text-[9px] font-bold uppercase tracking-wide bg-sky-500 text-white">
                Adaptive
              </span>
            </div>
            <span className="text-[10px] text-sky-600 dark:text-sky-400 font-semibold block mt-0.5">
              Provider decides when to reason
            </span>
            <span
              className="text-[9px] text-muted-foreground block truncate"
              title={sourceLabel(source)}
            >
              {sourceLabel(source)}
            </span>
          </div>
        </div>
      </div>
    );
  }

  // -------- 3. ON (user-controllable) --------
  if (isOn) {
    // Two sub-variants: manual ON vs provider-confirmed ON.
    const panelClasses = isManual
      ? "border-amber-500/40 bg-amber-500/10"
      : "border-emerald-500/40 bg-emerald-500/10";
    const iconColor = isManual ? "text-amber-500" : "text-emerald-500";

    return (
      <div className={`p-2.5 rounded-lg border ${panelClasses} space-y-2`}>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-start gap-2 min-w-0">
            <Brain size={16} className={`${iconColor} shrink-0 mt-0.5`} />
            <div className="min-w-0">
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-xs font-semibold text-foreground">
                  Reasoning / Thinking Mode
                </span>
                <StatusBadge
                  variant={isManual ? "on-manual" : "on-confirmed"}
                />
              </div>
              <span
                className={`text-[10px] font-semibold block mt-0.5 ${
                  isManual
                    ? "text-amber-600 dark:text-amber-400"
                    : "text-emerald-600 dark:text-emerald-400"
                }`}
              >
                {isManual
                  ? "✓ Enabled by you"
                  : "✓ Confirmed by provider"}
              </span>
              <span
                className="text-[9px] text-muted-foreground block truncate"
                title={sourceLabel(source)}
              >
                {sourceLabel(source)}
              </span>
            </div>
          </div>
          <button
            type="button"
            onClick={onToggle}
            className="px-2.5 py-1 rounded text-[11px] font-semibold bg-muted text-muted-foreground hover:bg-muted/80 hover:text-foreground cursor-pointer shrink-0 border border-border"
            title="Click to turn reasoning off"
          >
            Turn Off
          </button>
        </div>

        {/* Warning when manually enabled (not provider-confirmed) */}
        {isManual && (
          <div className="flex items-start gap-1 pt-1.5 border-t border-amber-500/20 text-[10px] text-amber-700 dark:text-amber-400">
            <AlertCircle size={10} className="mt-0.5 shrink-0" />
            <span>
              {providerSupportsLiveReasoningScan ? (
                <>
                  Not confirmed by provider. This setting was enabled manually.
                  Run <strong>Scan Models</strong> to detect native support.
                </>
              ) : (
                <>
                  Provider's API does not advertise reasoning capability for
                  this model (LM Studio and most cloud providers never do).
                  Your override will be applied as-is when chatting.
                </>
              )}
            </span>
          </div>
        )}

        {/* Effort picker */}
        {mode === "effort" && caps.reasoningEffortLevels.length > 0 && (
          <div className="flex items-center gap-1.5 flex-wrap pt-1.5 border-t border-current/20">
            <span className="text-[10px] text-muted-foreground font-semibold uppercase tracking-wider">
              Level:
            </span>
            {caps.reasoningEffortLevels.map((level) => (
              <button
                key={level}
                type="button"
                onClick={() => onSetEffort(level)}
                className={`px-2 py-0.5 rounded text-[10px] font-semibold cursor-pointer ${
                  currentEffort === level
                    ? "bg-amber-500 text-white"
                    : "bg-muted text-muted-foreground hover:text-foreground"
                }`}
              >
                {level}
              </button>
            ))}
          </div>
        )}

        {/* Budget hint */}
        {mode === "budget" &&
          (caps.reasoningMinBudgetTokens !== null ||
            caps.reasoningMaxBudgetTokens !== null) && (
            <div className="pt-1.5 border-t border-current/20 text-[10px] text-muted-foreground font-mono">
              Budget: {caps.reasoningMinBudgetTokens ?? 0} –{" "}
              {caps.reasoningMaxBudgetTokens ?? "?"} tokens
            </div>
          )}
      </div>
    );
  }

  // -------- 4. OFF by user (manual, reversible) --------
  if (isManual && !isOn) {
    return (
      <div className="flex items-center justify-between p-2.5 rounded-lg border border-border bg-muted/30">
        <div className="flex items-start gap-2 min-w-0">
          <MinusCircle
            size={16}
            className="text-muted-foreground shrink-0 mt-0.5"
          />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs font-semibold text-foreground">
                Reasoning / Thinking Mode
              </span>
              <StatusBadge variant="off-manual" />
            </div>
            <span className="text-[10px] text-muted-foreground block mt-0.5">
              Disabled by you — click Enable to turn back on
            </span>
            <span
              className="text-[9px] text-muted-foreground/70 block truncate"
              title={sourceLabel(source)}
            >
              {sourceLabel(source)}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={onToggle}
          className="px-3 py-1 rounded text-xs font-semibold bg-amber-500 text-white hover:bg-amber-600 cursor-pointer transition-colors shadow-xs shrink-0"
        >
          Enable
        </button>
      </div>
    );
  }

  // -------- 5. OFF — provider says no (final) --------
  if (isProviderConfirmed && !isOn) {
    return (
      <div className="flex items-start gap-2 p-2.5 rounded-lg border border-border bg-muted/30">
        <MinusCircle
          size={16}
          className="text-muted-foreground shrink-0 mt-0.5"
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs font-semibold text-foreground">
              Reasoning / Thinking Mode
            </span>
            <StatusBadge variant="off-provider" />
          </div>
          <span
            className="text-[10px] text-muted-foreground block mt-0.5 truncate"
            title={sourceLabel(source)}
          >
            {sourceLabel(source)}
          </span>
        </div>
      </div>
    );
  }

  // -------- 6. Unreported (no info) --------
  return (
    <div className="flex items-center justify-between p-2.5 rounded-lg border border-dashed border-amber-500/40 bg-amber-500/5">
      <div className="flex items-start gap-2 min-w-0">
        <Brain size={16} className="text-amber-500/70 shrink-0 mt-0.5" />
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs font-semibold text-foreground">
              Reasoning / Thinking Mode
            </span>
            <StatusBadge variant="unknown" />
          </div>
          <span className="text-[10px] text-muted-foreground block mt-0.5">
            Provider did not declare — enable manually if this model reasons
          </span>
        </div>
      </div>
      <button
        type="button"
        onClick={onToggle}
        className="px-3 py-1 rounded text-xs font-semibold bg-amber-500 text-white hover:bg-amber-600 cursor-pointer transition-colors shadow-xs shrink-0"
      >
        Enable
      </button>
    </div>
  );
}

/* ========================================================================== */
/* VisionBox                                                                  */
/* ========================================================================== */

interface VisionBoxProps {
  caps: InferredCapabilities;
  onToggle: () => void;
}

function VisionBox({ caps, onToggle }: VisionBoxProps) {
  const source = caps.visionSource;
  const isOn = caps.isVision;
  const isManual = source === "manual";
  const isProviderConfirmed =
    source === "provider_live" ||
    source === "provider_catalog" ||
    source === "provider_metadata" ||
    source === "external_registry" ||
    source === "huggingface_config";

  // ON (any source)
  if (isOn) {
    const panelClasses = isManual
      ? "border-amber-500/40 bg-amber-500/10"
      : "border-violet-500/40 bg-violet-500/10";
    const iconColor = isManual ? "text-amber-500" : "text-violet-500";
    return (
      <div
        className={`flex items-center justify-between p-2.5 rounded-lg border ${panelClasses}`}
      >
        <div className="flex items-start gap-2 min-w-0">
          <Eye size={16} className={`${iconColor} shrink-0 mt-0.5`} />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs font-semibold text-foreground">
                Vision / Multimodal Input
              </span>
              <StatusBadge
                variant={isManual ? "on-manual" : "on-confirmed"}
              />
            </div>
            <span
              className={`text-[10px] font-semibold block mt-0.5 ${
                isManual
                  ? "text-amber-600 dark:text-amber-400"
                  : "text-violet-600 dark:text-violet-400"
              }`}
            >
              {isManual ? "✓ Enabled by you" : "✓ Confirmed by provider"}
            </span>
            <span
              className="text-[9px] text-muted-foreground block truncate"
              title={sourceLabel(source)}
            >
              {sourceLabel(source)}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={onToggle}
          className="px-2.5 py-1 rounded text-[11px] font-semibold bg-muted text-muted-foreground hover:bg-muted/80 hover:text-foreground cursor-pointer shrink-0 border border-border"
          title="Turn vision off for this model"
        >
          Turn Off
        </button>
      </div>
    );
  }

  // OFF by user
  if (isManual) {
    return (
      <div className="flex items-center justify-between p-2.5 rounded-lg border border-border bg-muted/30">
        <div className="flex items-start gap-2 min-w-0">
          <MinusCircle
            size={16}
            className="text-muted-foreground shrink-0 mt-0.5"
          />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs font-semibold text-foreground">
                Vision / Multimodal Input
              </span>
              <StatusBadge variant="off-manual" />
            </div>
            <span className="text-[10px] text-muted-foreground block mt-0.5">
              Disabled by you — click Enable to turn back on
            </span>
            <span
              className="text-[9px] text-muted-foreground/70 block truncate"
              title={sourceLabel(source)}
            >
              {sourceLabel(source)}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={onToggle}
          className="px-3 py-1 rounded text-xs font-semibold bg-violet-500 text-white hover:bg-violet-600 cursor-pointer transition-colors shadow-xs shrink-0"
        >
          Enable
        </button>
      </div>
    );
  }

  // OFF — provider says no
  if (isProviderConfirmed) {
    return (
      <div className="flex items-start gap-2 p-2.5 rounded-lg border border-border bg-muted/30">
        <MinusCircle
          size={16}
          className="text-muted-foreground shrink-0 mt-0.5"
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs font-semibold text-foreground">
              Vision / Multimodal Input
            </span>
            <StatusBadge variant="off-provider" />
          </div>
          <span
            className="text-[10px] text-muted-foreground block mt-0.5 truncate"
            title={sourceLabel(source)}
          >
            {sourceLabel(source)}
          </span>
        </div>
      </div>
    );
  }

  // Unknown
  return (
    <div className="flex items-center justify-between p-2.5 rounded-lg border border-dashed border-violet-500/40 bg-violet-500/5">
      <div className="flex items-start gap-2 min-w-0">
        <Eye size={16} className="text-violet-500/70 shrink-0 mt-0.5" />
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs font-semibold text-foreground">
              Vision / Multimodal Input
            </span>
            <StatusBadge variant="unknown" />
          </div>
          <span className="text-[10px] text-muted-foreground block mt-0.5">
            Provider did not declare — enable manually if this model accepts images
          </span>
        </div>
      </div>
      <button
        type="button"
        onClick={onToggle}
        className="px-3 py-1 rounded text-xs font-semibold bg-violet-500 text-white hover:bg-violet-600 cursor-pointer transition-colors shadow-xs shrink-0"
      >
        Enable
      </button>
    </div>
  );
}

/* ========================================================================== */
/* ToolsBox                                                                   */
/* ========================================================================== */

interface ToolsBoxProps {
  caps: InferredCapabilities;
  onToggle: () => void;
}

function ToolsBox({ caps, onToggle }: ToolsBoxProps) {
  const source = caps.toolsSource;
  const isOn = caps.isTools;
  const isManual = source === "manual";
  const isProviderConfirmed =
    source === "provider_live" ||
    source === "provider_catalog" ||
    source === "provider_metadata" ||
    source === "external_registry" ||
    source === "huggingface_config";

  if (isOn) {
    const panelClasses = isManual
      ? "border-amber-500/40 bg-amber-500/10"
      : "border-blue-500/40 bg-blue-500/10";
    const iconColor = isManual ? "text-amber-500" : "text-blue-500";
    return (
      <div
        className={`flex items-center justify-between p-2.5 rounded-lg border ${panelClasses}`}
      >
        <div className="flex items-start gap-2 min-w-0">
          <Wrench size={16} className={`${iconColor} shrink-0 mt-0.5`} />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs font-semibold text-foreground">
                Tool Calling / Function Use
              </span>
              <StatusBadge
                variant={isManual ? "on-manual" : "on-confirmed"}
              />
            </div>
            <span
              className={`text-[10px] font-semibold block mt-0.5 ${
                isManual
                  ? "text-amber-600 dark:text-amber-400"
                  : "text-blue-600 dark:text-blue-400"
              }`}
            >
              {isManual ? "✓ Enabled by you" : "✓ Confirmed by provider"}
            </span>
            <span
              className="text-[9px] text-muted-foreground block truncate"
              title={sourceLabel(source)}
            >
              {sourceLabel(source)}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={onToggle}
          className="px-2.5 py-1 rounded text-[11px] font-semibold bg-muted text-muted-foreground hover:bg-muted/80 hover:text-foreground cursor-pointer shrink-0 border border-border"
          title="Turn tool calling off for this model"
        >
          Turn Off
        </button>
      </div>
    );
  }

  if (isManual) {
    return (
      <div className="flex items-center justify-between p-2.5 rounded-lg border border-border bg-muted/30">
        <div className="flex items-start gap-2 min-w-0">
          <MinusCircle
            size={16}
            className="text-muted-foreground shrink-0 mt-0.5"
          />
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs font-semibold text-foreground">
                Tool Calling / Function Use
              </span>
              <StatusBadge variant="off-manual" />
            </div>
            <span className="text-[10px] text-muted-foreground block mt-0.5">
              Disabled by you — click Enable to turn back on
            </span>
            <span
              className="text-[9px] text-muted-foreground/70 block truncate"
              title={sourceLabel(source)}
            >
              {sourceLabel(source)}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={onToggle}
          className="px-3 py-1 rounded text-xs font-semibold bg-blue-500 text-white hover:bg-blue-600 cursor-pointer transition-colors shadow-xs shrink-0"
        >
          Enable
        </button>
      </div>
    );
  }

  if (isProviderConfirmed) {
    return (
      <div className="flex items-start gap-2 p-2.5 rounded-lg border border-border bg-muted/30">
        <MinusCircle
          size={16}
          className="text-muted-foreground shrink-0 mt-0.5"
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs font-semibold text-foreground">
              Tool Calling / Function Use
            </span>
            <StatusBadge variant="off-provider" />
          </div>
          <span
            className="text-[10px] text-muted-foreground block mt-0.5 truncate"
            title={sourceLabel(source)}
          >
            {sourceLabel(source)}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-center justify-between p-2.5 rounded-lg border border-dashed border-blue-500/40 bg-blue-500/5">
      <div className="flex items-start gap-2 min-w-0">
        <Wrench size={16} className="text-blue-500/70 shrink-0 mt-0.5" />
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs font-semibold text-foreground">
              Tool Calling / Function Use
            </span>
            <StatusBadge variant="unknown" />
          </div>
          <span className="text-[10px] text-muted-foreground block mt-0.5">
            Provider did not declare — enable manually if this model supports tools
          </span>
        </div>
      </div>
      <button
        type="button"
        onClick={onToggle}
        className="px-3 py-1 rounded text-xs font-semibold bg-blue-500 text-white hover:bg-blue-600 cursor-pointer transition-colors shadow-xs shrink-0"
      >
        Enable
      </button>
    </div>
  );
}

export default ModelInfoModal;
