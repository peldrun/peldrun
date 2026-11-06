// frontend/src/app/settings/setup/ModelDiscoveryModal.tsx
"use client";

import { ModelInfoModal } from "@/components/models/ModelInfoModal";
import React, { useState, useEffect, useMemo } from "react";
import {
  X,
  Search,
  Check,
  RefreshCw,
  AlertCircle,
  Cpu,
  Cloud,
  Terminal,
  Save,
  Info,
  CheckSquare,
  Square,
  Brain,
  Eye,
  Wrench,
  Database,
  Layers,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { inferModelCapabilities, fetchModelFacts } from "@/lib/modelMetadata";
import type { ModelFacts, CapabilitySource } from "@/lib/modelMetadata";

/**
 * Payload returned to the caller when the user confirms a model
 * selection. Carries the raw provider metadata plus the catalog
 * capability facts so the parent can persist them into
 * `storage/models_metadata.json` alongside the vault and budget
 * files (which are already saved from `models` + `defaultModel`).
 */
export interface ModelsDiscoverySavePayload {
  models: string[];
  defaultModel?: string;
  /**
   * Raw provider metadata as returned by `/api/config/fetch-models`
   * under `models_metadata` (display_name, params_string,
   * max_context_length, quantization, publisher, capabilities,
   * capability_sources, ...).
   */
  metadata: Record<string, any>;
  /**
   * Catalog capability facts (vision / tools / reasoning) fetched
   * from `/api/catalog/models/{modelId}`. Persisted so the badges
   * shown here match exactly what `ModelInfoModal` renders.
   */
  facts: Record<string, ModelFacts>;
}

interface ModelDiscoveryModalProps {
  isOpen: boolean;
  onClose: () => void;
  providerId: string;
  providerName: string;
  baseUrl: string;
  apiKey: string;
  providerType?: string;
  // Optional explicit metadata endpoint from the vault. When
  // provided, forwarded to `/api/config/fetch-models` as
  // `metadata_endpoint` so discovery honors the user's configured
  // URL instead of the backend recomputing one from `baseUrl`. This
  // is what prevents LM Studio base URLs like `.../api/v1` from
  // producing the double-suffixed `/api/api/v0/models` path.
  metadataEndpoint?: string;
  initialSavedModels?: string[];
  onSaveModels: (payload: ModelsDiscoverySavePayload) => void;
}

export function ModelDiscoveryModal({
  isOpen,
  onClose,
  providerId,
  providerName,
  baseUrl,
  apiKey,
  providerType = "",
  metadataEndpoint,
  initialSavedModels = [],
  onSaveModels,
}: ModelDiscoveryModalProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);
  const [modelsMetadata, setModelsMetadata] = useState<Record<string, any>>({});
  const [selectedModels, setSelectedModels] = useState<Set<string>>(
    new Set(initialSavedModels)
  );
  const [defaultModel, setDefaultModel] = useState<string>(
    initialSavedModels[0] || ""
  );
  const [isLoading, setIsLoading] = useState(false);
  const [infoModalModel, setInfoModalModel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchModelsFromApi = async () => {
    setIsLoading(true);
    setError(null);
    try {
      // Relative path — routed through Next.js rewrites so the frontend
      // works regardless of where the backend is deployed.
      const res = await fetch("/api/config/fetch-models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: baseUrl,
          api_key: apiKey,
          provider_id: providerId,
          provider_type: providerType,
          // Forward the user's explicit metadata endpoint when the
          // caller provided one. Empty string signals the backend to
          // use its native adapter discovery path (which probes
          // /api/v1/models → /api/v0/models → /v1/models in priority
          // order).
          metadata_endpoint: metadataEndpoint || "",
        }),
      });
      // Read the raw text FIRST so that a non-JSON response (e.g.
      // Starlette's "Internal Server Error" plain-text fallback) shows
      // the real HTTP status + snippet instead of a cryptic
      // "Unexpected token 'I'" parse error.
      const rawText = await res.text().catch(() => "");
      let data: any = null;
      try {
        data = rawText ? JSON.parse(rawText) : null;
      } catch {
        throw new Error(
          `Provider discovery returned non-JSON response (HTTP ${res.status}): ` +
            (rawText.slice(0, 240) || "<empty body>")
        );
      }

      if (!res.ok) {
        throw new Error(
          `Provider discovery failed (HTTP ${res.status}): ` +
            (data?.error || data?.message || rawText.slice(0, 240))
        );
      }

      if (data && data.ok && Array.isArray(data.models) && data.models.length > 0) {
        setDiscoveredModels(data.models);
        if (data.models_metadata) setModelsMetadata(data.models_metadata);
        // Pre-select models if already saved, or select first 3 if none saved
        if (initialSavedModels.length === 0) {
          const topFew = data.models.slice(0, 3);
          setSelectedModels(new Set(topFew));
          setDefaultModel(topFew[0] || "");
        }
      } else {
        const msg =
          data?.error || data?.message || "No models returned from provider API.";
        setError(msg);
      }
    } catch (err: any) {
      setError(err.message || "Failed to communicate with discovery endpoint.");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      setSelectedModels(new Set(initialSavedModels));
      setDefaultModel(initialSavedModels[0] || "");
      fetchModelsFromApi();
    }
  }, [isOpen]);

  const filteredModels = useMemo(() => {
    if (!searchQuery.trim()) return discoveredModels;
    const q = searchQuery.toLowerCase();
    return discoveredModels.filter((m) => m.toLowerCase().includes(q));
  }, [discoveredModels, searchQuery]);

  const toggleModelSelection = (model: string) => {
    setSelectedModels((prev) => {
      const next = new Set(prev);
      if (next.has(model)) {
        next.delete(model);
        if (defaultModel === model) {
          setDefaultModel(Array.from(next)[0] || "");
        }
      } else {
        next.add(model);
        if (!defaultModel) {
          setDefaultModel(model);
        }
      }
      return next;
    });
  };

  const handleSelectAllFiltered = () => {
    setSelectedModels((prev) => {
      const next = new Set(prev);
      filteredModels.forEach((m) => next.add(m));
      if (!defaultModel && filteredModels.length > 0) {
        setDefaultModel(filteredModels[0]);
      }
      return next;
    });
  };

  const handleClearSelection = () => {
    setSelectedModels(new Set());
    setDefaultModel("");
  };

  const handleSave = () => {
    const list = Array.from(selectedModels);
    onSaveModels({
      models: list,
      defaultModel: defaultModel || list[0],
      metadata: modelsMetadata,
      facts: modelFactsMap,
    });
    onClose();
  };

  // ------------------------------------------------------------------
  // Fetch capability facts for all discovered models in the background.
  // This guarantees the badges here match EXACTLY what ModelInfoModal
  // shows, because both rely on the same backend catalog.
  // ------------------------------------------------------------------
  const [modelFactsMap, setModelFactsMap] = useState<Record<string, ModelFacts>>({});

  useEffect(() => {
    if (!providerId || discoveredModels.length === 0) return;

    let cancelled = false;
    const fetchAllFacts = async () => {
      const newFacts: Record<string, ModelFacts> = {};
      // Chunking to avoid overwhelming the backend/browser with 60+ requests
      const batchSize = 10;
      for (let i = 0; i < discoveredModels.length; i += batchSize) {
        if (cancelled) return;
        const batch = discoveredModels.slice(i, i + batchSize);
        await Promise.all(
          batch.map(async (mId) => {
            if (modelFactsMap[mId]) return; // Already loaded
            try {
              const facts = await fetchModelFacts(providerId, mId);
              if (facts) newFacts[mId] = facts;
            } catch (e) {
              // Ignore individual fetch errors
            }
          })
        );
      }
      if (!cancelled && Object.keys(newFacts).length > 0) {
        setModelFactsMap((prev) => ({ ...prev, ...newFacts }));
      }
    };

          fetchAllFacts();
      return () => { cancelled = true; };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [discoveredModels, providerId]);
/* ------------------------------------------------------------------ */
  /* Source hint helper                                                 */
  /*                                                                    */
  /* Turns a CapabilitySource string into a human-readable label       */
  /* shown in the badge tooltip. Mirrors the backend enum exactly, so  */
  /* the user always knows WHERE the capability fact came from.        */
  /* ------------------------------------------------------------------ */
  const sourceHint = (source: string | undefined): string => {
    switch (source) {
      case "manual":
        return "Set manually by you";
      case "provider_live":
        return "Reported by provider (live)";
      case "provider_catalog":
        return "Reported by provider catalog";
      case "provider_metadata":
        return "Reported by provider metadata";
      case "model_metadata":
        return "Saved in model metadata";
      case "huggingface_config":
        return "From HuggingFace config";
      case "external_registry":
        return "From LiteLLM registry";
      case "capability_probe":
        return "Confirmed by capability probe";
      default:
        return "Source unknown";
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 bg-background/80 backdrop-blur-sm flex items-center justify-center p-4 animate-in fade-in">
      <div className="w-full max-w-lg bg-card border border-border rounded-md shadow-2xl overflow-hidden flex flex-col max-h-[85vh] font-sans">
        {/* Modal Header */}
        <div className="px-5 py-4 border-b border-border/80 flex items-center justify-between bg-card/60">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center text-primary">
              {providerId.includes("lmstudio") ? (
                <Cpu size={16} />
              ) : providerId.includes("custom") ? (
                <Terminal size={16} />
              ) : (
                <Cloud size={16} />
              )}
            </div>
            <div>
              <h3 className="font-heading font-semibold text-sm text-foreground">
                Discover Models: {providerName}
              </h3>
              <p className="text-[11px] text-muted-foreground font-mono">
                {baseUrl}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors cursor-pointer"
          >
            <X size={16} />
          </button>
        </div>

        {/* Search & Actions Bar */}
        <div className="p-3.5 border-b border-border/60 bg-muted/20 flex flex-col gap-2.5">
          <div className="relative">
            <Search
              size={14}
              className="absolute left-2.5 top-2.5 text-muted-foreground"
            />
            <input
              type="text"
              placeholder="Search available models..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-8 pr-3 py-1.5 rounded-md bg-background border border-border text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary font-mono"
            />
          </div>

          <div className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground font-mono text-[11px]">
              Selected:{" "}
              <strong className="text-foreground">{selectedModels.size}</strong>{" "}
              of {discoveredModels.length} models
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleSelectAllFiltered}
                className="text-[11px] text-primary hover:underline cursor-pointer flex items-center gap-1"
              >
                <CheckSquare size={12} />
                <span>Select All</span>
              </button>
              <span className="text-muted-foreground/40">|</span>
              <button
                type="button"
                onClick={handleClearSelection}
                className="text-[11px] text-muted-foreground hover:text-foreground cursor-pointer flex items-center gap-1"
              >
                <Square size={12} />
                <span>Clear</span>
              </button>
              <span className="text-muted-foreground/40">|</span>
              <button
                type="button"
                onClick={fetchModelsFromApi}
                disabled={isLoading}
                className="text-[11px] text-primary hover:underline cursor-pointer flex items-center gap-1 disabled:opacity-50"
              >
                <RefreshCw
                  size={11}
                  className={isLoading ? "animate-spin" : ""}
                />
                <span>Rescan</span>
              </button>
            </div>
          </div>
        </div>

        {/* Model List Body */}
        <div className="flex-1 overflow-y-auto p-3 space-y-1 min-h-[220px]">
          {isLoading ? (
            <div className="h-44 flex flex-col items-center justify-center gap-2 text-muted-foreground">
              <RefreshCw size={22} className="animate-spin text-primary" />
              <span className="text-xs">
                Querying available models from {providerName}...
              </span>
            </div>
          ) : error ? (
            <div className="p-4 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-600 dark:text-rose-400 text-xs space-y-2">
              <div className="flex items-center gap-2 font-semibold">
                <AlertCircle size={15} />
                <span>Discovery Error</span>
              </div>
              <p className="text-[11px] leading-relaxed">{error}</p>
              <Button
                variant="outline"
                size="sm"
                onClick={fetchModelsFromApi}
                className="h-7 text-xs border-rose-500/40 hover:bg-rose-500/20 text-rose-600 dark:text-rose-300"
              >
                Retry Scan
              </Button>
            </div>
          ) : filteredModels.length === 0 ? (
            <div className="h-44 flex flex-col items-center justify-center gap-1.5 text-muted-foreground">
              <Search size={22} className="opacity-40" />
              <span className="text-xs">No models match your search.</span>
            </div>
          ) : (
            filteredModels.map((modelId) => {
              const isSelected = selectedModels.has(modelId);
              const isDefault = defaultModel === modelId;
              const meta = modelsMetadata[modelId] || {};

              /* ---------------------------------------------------------- */
              /* Capability badges — read STRICTLY from metadata.           */
              /*                                                            */
              /* No name-pattern heuristics. A badge appears ONLY when the  */
              /* metadata explicitly declares the capability as `true`.     */
              /* When the metadata is silent (the common case for providers */
              /* whose /models response has no capability fields — DeepSeek,*/
              /* OpenAI, Anthropic), NO badge is shown. The user can open   */
              /* the info modal to enable it manually.                      */
              /*                                                            */
              /* The `provider_live` source (Ollama / LM Studio / Google)   */
              /* is populated during the last Scan Models run and appears   */
              /* here immediately; for other providers, `manual` is the     */
              /* only way to declare capabilities.                          */
              /* ---------------------------------------------------------- */
              // 1. Base capabilities from the discovery response
              //    (whatever `_parse_adapter_model_list` persisted under
              //    `capabilities` and `capability_sources`).
              //
              // 2. Catalog facts fetched asynchronously from
              //    `/api/catalog/models/{modelId}`. These overlay the
              //    discovery data — but ONLY when the catalog returns a
              //    `known: true` fact.
              //
              // Critical: an `unknown` fact MUST NOT erase a `true` value
              // that came from the provider's own `discover_models()`
              // response. The previous code did `isTools = facts.tools.value`
              // unconditionally, which silently overwrote a provider-declared
              // `trained_for_tool_use: true` with `false` whenever the
              // catalog endpoint returned `unknown` (e.g. for Provider
              // Packages like GigaChat where `/api/catalog` falls back to
              // LiteLLM and does not find the model).
              const inferredCaps = inferModelCapabilities(modelId, meta as any, providerId) || {};
              const facts = modelFactsMap[modelId];

              let isVision = inferredCaps.isVision || meta.capabilities?.vision === true;
              let isTools = inferredCaps.isTools || meta.capabilities?.trained_for_tool_use === true;
              let isReasoning = inferredCaps.isReasoning || meta.capabilities?.reasoning === true;

              let reasoningSource = inferredCaps.reasoningSource || meta.capability_sources?.reasoning || "unknown";
              let visionSource = inferredCaps.visionSource || meta.capability_sources?.vision || "unknown";
              let toolsSource = inferredCaps.toolsSource || meta.capability_sources?.trained_for_tool_use || "unknown";

              if (facts) {
                // Overlay only when the catalog is definitive. A missing
                // catalog entry must not silently downgrade a capability
                // that the provider itself declared.
                if (facts.vision.known) {
                  isVision = facts.vision.value;
                  if (facts.vision.source) {
                    visionSource = facts.vision.source as CapabilitySource;
                  }
                }
                if (facts.tools.known) {
                  isTools = facts.tools.value;
                  if (facts.tools.source) {
                    toolsSource = facts.tools.source as CapabilitySource;
                  }
                }
                if (facts.reasoning.known) {
                  isReasoning = facts.reasoning.value;
                  if (facts.reasoning.source) {
                    reasoningSource = facts.reasoning.source as CapabilitySource;
                  }
                }
              }

              const capabilitySources = {
                reasoning: reasoningSource,
                vision: visionSource,
                trained_for_tool_use: toolsSource,
              };

              return (
                <div
                  key={modelId}
                  onClick={() => toggleModelSelection(modelId)}
                  className={`flex flex-col p-2.5 rounded-lg text-xs font-mono transition-all cursor-pointer border gap-2 ${
                    isSelected
                      ? "bg-primary/5 border-primary/40 text-foreground shadow-2xs"
                      : "bg-card/60 hover:bg-muted/60 border-border/40 text-muted-foreground"
                  }`}
                >
                  <div className="flex items-center justify-between w-full">
                    <div className="flex items-center gap-2.5 truncate flex-1 mr-2">
                      <span
                        className={`w-4 h-4 rounded flex items-center justify-center border transition-colors shrink-0 ${
                          isSelected
                            ? "bg-primary text-primary-foreground border-primary"
                            : "border-border bg-background"
                        }`}
                      >
                        {isSelected && <Check size={11} />}
                      </span>
                      <span
                        className="truncate text-xs font-bold"
                        title={meta.display_name || modelId}
                      >
                        {meta.display_name || modelId}
                      </span>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setInfoModalModel(modelId);
                        }}
                        className="p-1 rounded text-muted-foreground hover:text-primary hover:bg-muted transition cursor-pointer shrink-0"
                        title="View Model Technical Specs & Capabilities"
                      >
                        <Info size={12} />
                      </button>
                    </div>

                    <div
                      className="flex items-center gap-1 shrink-0"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {isSelected && (
                        <button
                          type="button"
                          onClick={() => setDefaultModel(modelId)}
                          className={`px-2 py-0.5 rounded text-[10px] uppercase tracking-wider font-sans font-semibold transition-all ${
                            isDefault
                              ? "bg-emerald-500 text-white shadow-2xs"
                              : "bg-muted text-muted-foreground hover:text-foreground"
                          }`}
                          title="Set as Default Model for this Provider"
                        >
                          {isDefault ? "Default" : "Set Default"}
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Badges Row */}
                  <div className="flex flex-wrap items-center gap-1.5 pl-6 font-sans">
                    {isReasoning && (
                      <span
                        className="flex items-center gap-1 text-[9.5px] font-semibold px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20"
                        title={`Reasoning — ${sourceHint(capabilitySources.reasoning)}`}
                      >
                        <Brain size={10} /> Reasoning
                      </span>
                    )}
                    {isVision && (
                      <span
                        className="flex items-center gap-1 text-[9.5px] font-semibold px-1.5 py-0.5 rounded bg-violet-500/10 text-violet-600 dark:text-violet-400 border border-violet-500/20"
                        title={`Vision — ${sourceHint(capabilitySources.vision)}`}
                      >
                        <Eye size={10} /> Vision
                      </span>
                    )}
                    {isTools && (
                      <span
                        className="flex items-center gap-1 text-[9.5px] font-semibold px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-500/20"
                        title={`Tools — ${sourceHint(capabilitySources.trained_for_tool_use)}`}
                      >
                        <Wrench size={10} /> Tools
                      </span>
                    )}

                    {meta.params_string && (
                      <span className="flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground border border-border">
                        <Cpu size={9} /> {meta.params_string}
                      </span>
                    )}
                    {meta.max_context_length && (
                      <span className="flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground border border-border">
                        <Database size={9} />{" "}
                        {Math.round(meta.max_context_length / 1024)}K ctx
                      </span>
                    )}
                    {meta.quantization?.name && (
                      <span className="flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground border border-border">
                        <Layers size={9} /> {String(meta.quantization.name)}
                      </span>
                    )}
                    {meta.publisher && (
                      <span className="flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground border border-border">
                        {meta.publisher}
                      </span>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Modal Footer */}
        <div className="px-5 py-3.5 border-t border-border/80 flex items-center justify-between bg-card/60">
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            className="h-8 text-xs cursor-pointer"
          >
            Cancel
          </Button>

          <Button
            size="sm"
            onClick={handleSave}
            disabled={selectedModels.size === 0}
            className="h-8 text-xs bg-primary text-primary-foreground hover:opacity-90 flex items-center gap-1.5 cursor-pointer disabled:opacity-40 shadow-xs"
          >
            <Save size={13} />
            <span>Save Selected ({selectedModels.size})</span>
          </Button>
        </div>

        {/* Nested Technical Specs Modal — pass providerId so the
            capability facts are fetched from /api/catalog. */}
        <ModelInfoModal
          isOpen={Boolean(infoModalModel)}
          onClose={() => setInfoModalModel(null)}
          modelKey={infoModalModel || ""}
          explicitMeta={
            infoModalModel ? modelsMetadata[infoModalModel] || null : null
          }
          providerId={providerId}
        />
      </div>
    </div>
  );
}

export default ModelDiscoveryModal;
