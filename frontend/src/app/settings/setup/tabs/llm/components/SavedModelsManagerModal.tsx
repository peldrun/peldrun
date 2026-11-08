// frontend/src/app/settings/setup/tabs/llm/components/SavedModelsManagerModal.tsx
"use client";

import { ModelInfoModal } from "@/components/models/ModelInfoModal";
import React, { useState, useEffect, useMemo } from "react";
import {
  X,
  Search,
  Check,
  Info,
  CheckSquare,
  Square,
  Brain,
  Eye,
  Wrench,
  Save,
  Plus,
  Cloud,
  Terminal,
  Package,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { fetchModelFacts, inferModelCapabilities } from "@/lib/modelMetadata";
import type { ModelFacts, CapabilitySource } from "@/lib/modelMetadata";

/* -------------------------------------------------------------------------- */
/* Payload contract                                                            */
/* -------------------------------------------------------------------------- */

/**
 * Payload returned to the caller when the user confirms changes to the
 * saved-models list.
 *
 * Only the resulting model list and the selected default are transmitted.
 * Capability edits (Vision / Tools / Reasoning) are handled exclusively
 * through the nested `ModelInfoModal` → `POST /api/config/models-metadata`,
 * so this payload never carries facts — they are fetched only for badge
 * rendering inside the modal itself.
 */
export interface SavedModelsSavePayload {
  /** The (possibly reduced) list of models to keep. */
  models: string[];
  /** The model marked as default, or `undefined` to preserve the caller's. */
  defaultModel?: string;
}

/* -------------------------------------------------------------------------- */
/* Props                                                                      */
/* -------------------------------------------------------------------------- */

interface SavedModelsManagerModalProps {
  isOpen: boolean;
  onClose: () => void;

  /**
   * Canonical provider identifier used for:
   *   * `<ModelInfoModal providerId={...}>` — determines catalog lookup.
   *   * `fetchModelFacts(providerId, modelId)` — badge source.
   */
  providerId: string;

  /** Human-readable provider name shown in the header. */
  providerName: string;

  /**
   * Disambiguates the header icon and subtitle. Has no functional effect —
   * all three kinds share the same save mechanism and are persisted by
   * the caller via its own path (setCloudProviders / saveModelsForPackage /
   * setCustomEndpoints).
   */
  providerKind: "cloud" | "package" | "custom";

  /** Currently saved models. Becomes the initial selection on open. */
  savedModels: string[];

  /** Initial default model. Falls back to `savedModels[0]`. */
  defaultModel?: string;

  /**
   * Called when the user clicks "Save Changes". The caller is responsible
   * for persisting the new list into the correct store and refreshing its
   * own state.
   */
  onSaveModels: (payload: SavedModelsSavePayload) => void;
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * "Saved Models Manager" — a management-only counterpart to
 * `ModelDiscoveryModal`.
 *
 * Both modals share the same visual language and the same badge /
 * capability-edit flow (nested `ModelInfoModal` → `/api/config/models-metadata`).
 * The distinction is purely about data acquisition:
 *
 *   * `ModelDiscoveryModal`:
 *       fetches `/api/config/fetch-models` on open → merges live provider
 *       results with the current vault → user picks which to ADD.
 *
 *   * `SavedModelsManagerModal` (this file):
 *       never calls the provider API → uses `savedModels` from props as
 *       the sole source → user picks which to KEEP + edits capabilities
 *       via the info button.
 *
 * This modal is intentionally read-only with respect to discovery — if
 * the user wants to add a brand-new model from the provider, they should
 * use the "Scan Models" button instead.
 */
export function SavedModelsManagerModal({
  isOpen,
  onClose,
  providerId,
  providerName,
  providerKind,
  savedModels,
  defaultModel: initialDefault,
  onSaveModels,
}: SavedModelsManagerModalProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedModels, setSelectedModels] = useState<Set<string>>(
    new Set(savedModels)
  );
  const [defaultModel, setDefaultModel] = useState<string>(
    initialDefault || savedModels[0] || ""
  );
  const [infoModalModel, setInfoModalModel] = useState<string | null>(null);
  const [newModelInput, setNewModelInput] = useState("");
  const [modelFactsMap, setModelFactsMap] = useState<Record<string, ModelFacts>>({});

  /* -------------------------- Reset on open ----------------------------- */
  useEffect(() => {
    if (!isOpen) return;
    setSelectedModels(new Set(savedModels));
    setDefaultModel(initialDefault || savedModels[0] || "");
    setSearchQuery("");
    setNewModelInput("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  /* -------------------------- Fetch facts for badges -------------------- */
  useEffect(() => {
    if (!isOpen || !providerId || savedModels.length === 0) return;

    let cancelled = false;
    const fetchAllFacts = async () => {
      const newFacts: Record<string, ModelFacts> = {};
      const batchSize = 10;
      for (let i = 0; i < savedModels.length; i += batchSize) {
        if (cancelled) return;
        const batch = savedModels.slice(i, i + batchSize);
        await Promise.all(
          batch.map(async (mId) => {
            if (modelFactsMap[mId]) return; // Already loaded
            try {
              const facts = await fetchModelFacts(providerId, mId);
              if (facts) newFacts[mId] = facts;
            } catch {
              /* Individual model fetch failures are non-fatal. */
            }
          })
        );
      }
      if (!cancelled && Object.keys(newFacts).length > 0) {
        setModelFactsMap((prev) => ({ ...prev, ...newFacts }));
      }
    };

    void fetchAllFacts();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, providerId, savedModels]);

  /* -------------------------- Derived model list ------------------------ */
  // Union of saved + selected, so manually-added models appear in the list.
  const allModels = useMemo(() => {
    return Array.from(new Set([...savedModels, ...Array.from(selectedModels)]));
  }, [savedModels, selectedModels]);

  const filteredModels = useMemo(() => {
    if (!searchQuery.trim()) return allModels;
    const q = searchQuery.toLowerCase();
    return allModels.filter((m) => m.toLowerCase().includes(q));
  }, [allModels, searchQuery]);

  /* -------------------------- Actions ----------------------------------- */
  const toggleModel = (model: string) => {
    setSelectedModels((prev) => {
      const next = new Set(prev);
      if (next.has(model)) {
        next.delete(model);
        if (defaultModel === model) {
          setDefaultModel(Array.from(next)[0] || "");
        }
      } else {
        next.add(model);
      }
      return next;
    });
  };

  const handleSelectAll = () => setSelectedModels(new Set(filteredModels));

  const handleClearSelection = () => {
    setSelectedModels(new Set());
    setDefaultModel("");
  };

  const handleAddManual = () => {
    const trimmed = newModelInput.trim();
    if (!trimmed) return;
    if (selectedModels.has(trimmed)) {
      setNewModelInput("");
      return;
    }
    setSelectedModels((prev) => new Set(prev).add(trimmed));
    if (!defaultModel) setDefaultModel(trimmed);
    setNewModelInput("");
  };

  const handleSave = () => {
    const list = Array.from(selectedModels);
    onSaveModels({
      models: list,
      defaultModel: defaultModel || list[0],
    });
    onClose();
  };

  /* -------------------------- Source hint helper ------------------------ */
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

  /* -------------------------- Header icon/kind -------------------------- */
  const headerIcon =
    providerKind === "package" ? (
      <Package size={16} />
    ) : providerKind === "custom" ? (
      <Terminal size={16} />
    ) : (
      <Cloud size={16} />
    );

  const headerLabel =
    providerKind === "package"
      ? "Package Provider"
      : providerKind === "custom"
      ? "Custom Endpoint"
      : "Cloud Provider";

  /* -------------------------- Render ------------------------------------ */
  return (
    <div className="fixed inset-0 z-50 bg-background/80 backdrop-blur-sm flex items-center justify-center p-4 animate-in fade-in">
      <div className="w-full max-w-lg bg-card border border-border rounded-md shadow-2xl overflow-hidden flex flex-col max-h-[85vh] font-sans">
        {/* Header */}
        <div className="px-5 py-4 border-b border-border/80 flex items-center justify-between bg-card/60">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center text-primary">
              {headerIcon}
            </div>
            <div>
              <h3 className="font-heading font-semibold text-sm text-foreground">
                Manage Saved Models: {providerName}
              </h3>
              <p className="text-[11px] text-muted-foreground font-mono">
                {headerLabel} • {savedModels.length} saved
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

        {/* Info banner */}
        <div className="px-5 py-2 border-b border-primary/20 bg-primary/5 flex items-start gap-2">
          <Info size={13} className="text-primary shrink-0 mt-0.5" />
          <p className="text-[11px] text-foreground/80 leading-relaxed">
            These models are already saved for this provider. Use the{" "}
            <Info size={10} className="inline -mt-0.5" /> icon to edit
            capabilities (Vision / Tools / Reasoning), or uncheck a model to
            remove it. Click <strong>Save Changes</strong> to apply.
          </p>
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
              placeholder="Search saved models..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-8 pr-3 py-1.5 rounded-md bg-background border border-border text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary font-mono"
            />
          </div>

          <div className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground font-mono text-[11px]">
              Kept:{" "}
              <strong className="text-foreground">{selectedModels.size}</strong>{" "}
              of {allModels.length} models
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleSelectAll}
                className="text-[11px] text-primary hover:underline cursor-pointer flex items-center gap-1"
              >
                <CheckSquare size={12} />
                <span>Keep All</span>
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
            </div>
          </div>
        </div>

        {/* Model List Body */}
        <div className="flex-1 overflow-y-auto p-3 space-y-1 min-h-[220px]">
          {filteredModels.length === 0 ? (
            <div className="h-44 flex flex-col items-center justify-center gap-1.5 text-muted-foreground">
              <Search size={22} className="opacity-40" />
              <span className="text-xs text-center px-4">
                {allModels.length === 0
                  ? "No saved models yet. Use \"Scan Models\" in the provider card to discover models first."
                  : "No models match your search."}
              </span>
            </div>
          ) : (
            filteredModels.map((modelId) => {
              const isSelected = selectedModels.has(modelId);
              const isDefault = defaultModel === modelId;

              const facts = modelFactsMap[modelId];
              const inferredCaps =
                inferModelCapabilities(modelId, {}, providerId) || {};

              let isVision = inferredCaps.isVision;
              let isTools = inferredCaps.isTools;
              let isReasoning = inferredCaps.isReasoning;
              let reasoningSource = inferredCaps.reasoningSource || "unknown";
              let visionSource = inferredCaps.visionSource || "unknown";
              let toolsSource = inferredCaps.toolsSource || "unknown";

              // Catalog overlay — same "known-only" rule as the discovery modal.
              if (facts) {
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
                  onClick={() => toggleModel(modelId)}
                  className={`flex flex-col p-2.5 rounded-lg text-xs font-mono transition-all cursor-pointer border gap-2 ${
                    isSelected
                      ? "bg-primary/5 border-primary/40 text-foreground shadow-2xs"
                      : "bg-card/60 hover:bg-muted/60 border-border/40 text-muted-foreground opacity-60"
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
                        title={modelId}
                      >
                        {modelId}
                      </span>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setInfoModalModel(modelId);
                        }}
                        className="p-1 rounded text-muted-foreground hover:text-primary hover:bg-muted transition cursor-pointer shrink-0"
                        title="View or edit model capabilities"
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

                  {/* Capability badges — same rendering rules as discovery modal */}
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
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Manual add row */}
        <div className="px-3.5 py-2 border-t border-border/40 bg-muted/20">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={newModelInput}
              onChange={(e) => setNewModelInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleAddManual()}
              placeholder="Add a model ID manually..."
              className="flex-1 bg-background border border-border rounded-sm px-2.5 py-1 text-xs text-foreground font-mono placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleAddManual}
              disabled={!newModelInput.trim()}
              className="h-7 px-2.5 text-[11px] cursor-pointer"
            >
              <Plus size={11} className="mr-1" /> Add
            </Button>
          </div>
        </div>

        {/* Footer */}
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
            className="h-8 text-xs bg-primary text-primary-foreground hover:opacity-90 flex items-center gap-1.5 cursor-pointer shadow-xs"
          >
            <Save size={13} />
            <span>Save Changes ({selectedModels.size})</span>
          </Button>
        </div>

        {/* Nested ModelInfoModal — identical to the discovery modal's flow. */}
        <ModelInfoModal
          isOpen={Boolean(infoModalModel)}
          onClose={() => setInfoModalModel(null)}
          modelKey={infoModalModel || ""}
          explicitMeta={null}
          providerId={providerId}
        />
      </div>
    </div>
  );
}

export default SavedModelsManagerModal;
