// frontend/src/app/settings/setup/tabs/llm/components/LocalEnginesTab.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import {
  CheckCircle2,
  Cpu,
  Layers,
  Plus,
  RefreshCw,
  Terminal,
  XCircle,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/ToastNotification";
import { inferModelCapabilities } from "@/lib/modelMetadata";
import { ModelItemCard } from "./ModelItemCard";
import { Icon } from '@/components/Icon/Icon'
import type {
  InferredCapabilities,
  LocalEnginesTabProps,
} from "../types";

/* -------------------------------------------------------------------------- */
/* Constants                                                                  */
/* -------------------------------------------------------------------------- */

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

const EMPTY_MODELS_HINT = "No Ollama models found. Start Ollama and click ";

/**
 * The metadata vault uses a permissive entry type so backend additions do
 * not force coordinated type changes on every scan. This cast adapts a vault
 * entry to the metadata contract expected by `inferModelCapabilities`, which
 * treats every field as optional anyway.
 */
type InferenceMeta = Parameters<typeof inferModelCapabilities>[1];

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Sub-tab 2 of the LLM settings: local engines.
 *
 * Hosts two large cards:
 *   - LM Studio (OpenAI-compatible local server on port 1234),
 *   - Ollama (local server on port 11434).
 *
 * Each card exposes connection settings, a live test button, a manual
 * "add model" input, a "scan models" action, and a grid of `ModelItemCard`s
 * with capability badges.
 */
export function LocalEnginesTab({
  config,
  setConfig,
  lmStudioSettings,
  setLmStudioSettings,
  ollamaSettings,
  setOllamaSettings,
  availableModels,
  setAvailableModels,
  fetchingModels,
  handleFetchModels,
  assignDetectedModel,
  activateEngine,
  testEndpoint,
  metadataVault,
  newLMModelInput,
  setNewLMModelInput,
  newOllamaModelInput,
  setNewOllamaModelInput,
  scanningOllama,
  testingId,
  onOpenModelInfo,
  handleTestLMStudio,
  handleTestOllama,
  handleAddLMModel,
  handleRemoveLMModel,
  handleAddOllamaModel,
  handleRemoveOllamaModel,
  handleClearOllama,
  handleScanOllama,
}: LocalEnginesTabProps) {
  /* ---------------------- LM Studio derived helpers --------------------- */

  const handleClearLMModels = () => {
    setAvailableModels([]);
    if (typeof window !== "undefined") {
      try {
        window.localStorage.removeItem("omweb_scanned_models");
      } catch {
        /* Non-fatal. */
      }
    }
    showToast.info("List Cleared", "LM Studio models list cleared.");
  };

  const handleLMBaseUrlChange = (value: string) => {
    setLmStudioSettings({ ...lmStudioSettings, baseUrl: value });
    if (config.llm.provider === "lmstudio") {
      setConfig((p) => ({ ...p, llm: { ...p.llm, base_url: value } }));
    }
  };

  const handleLMApiKeyChange = (value: string) => {
    setLmStudioSettings({ ...lmStudioSettings, apiKey: value });
    if (config.llm.provider === "lmstudio") {
      setConfig((p) => ({ ...p, llm: { ...p.llm, api_key: value } }));
    }
  };

  const handleLMModelChange = (value: string) => {
    setLmStudioSettings({ ...lmStudioSettings, model: value });
    if (config.llm.provider === "lmstudio") {
      setConfig((p) => ({ ...p, llm: { ...p.llm, model: value } }));
    }
  };

  const handleLMMetadataEndpointChange = (value: string) => {
    setLmStudioSettings({ ...lmStudioSettings, metadataEndpoint: value });
  };

  const handleOllamaMetadataEndpointChange = (value: string) => {
    setOllamaSettings({ ...ollamaSettings, metadataEndpoint: value });
  };

  /* ---------------------- Ollama derived helpers ------------------------ */

  const handleOllamaBaseUrlChange = (value: string) => {
    setOllamaSettings({ ...ollamaSettings, baseUrl: value });
    if (config.llm.provider === "ollama") {
      const normalized = value.endsWith("/v1") ? value : `${value}/v1`;
      setConfig((p) => ({ ...p, llm: { ...p.llm, base_url: normalized } }));
    }
  };

  const handleOllamaModelChange = (value: string) => {
    setOllamaSettings({ ...ollamaSettings, model: value });
    if (config.llm.provider === "ollama") {
      setConfig((p) => ({ ...p, llm: { ...p.llm, model: value } }));
    }
  };

  /* -------------------------- Render ------------------------------------ */

  return (
    <div className="w-full max-w-7xl space-y-6 pt-2 mx-auto">
      {/* =================================================================== */}
      {/* 1. LM STUDIO CARD                                                   */}
      {/* =================================================================== */}
      <motion.div
        {...FADE_UP}
        className="relative p-5 rounded-md border border-card bg-gigbg shadow-sm space-y-4"
      >
        <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md
        bg-gradient-to-r from-transparent via-rb/10 to-transparent" />

        {/* Header */}
        <div className="flex items-center justify-between border-b border-border/60 pb-3 flex-wrap gap-2">
          <div className="flex items-center gap-2.5">
            <div className="p-2 icon">
              <Icon name="lm-studio" size={18} />
            </div>
            <span className="text-xs font-bold text-foreground uppercase tracking-wider">
              LM Studio Local Server (OpenAI-compatible)
            </span>
          </div>
          <span className="text-[11px] text-muted-foreground font-mono">
            Port: 1234 (Local)
          </span>
        </div>

        {/* Connection settings */}
        <div className="space-y-3 text-xs">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
                Base URL override
              </label>
              <input
                type="text"
                value={lmStudioSettings.baseUrl}
                onChange={(e) => handleLMBaseUrlChange(e.target.value)}
                placeholder="http://127.0.0.1:1234/v1"
                className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>

            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
                API Key (Optional for auth)
              </label>
              <input
                type="password"
                value={lmStudioSettings.apiKey}
                onChange={(e) => handleLMApiKeyChange(e.target.value)}
                placeholder="Leave blank for standard local server"
                className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>
          </div>

          <div>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
              Default LM Studio Model
            </label>
            <input
              type="text"
              value={lmStudioSettings.model}
              onChange={(e) => handleLMModelChange(e.target.value)}
              className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />
          </div>

          <div>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
              Metadata Endpoint (optional)
            </label>
            <input
              type="text"
              value={lmStudioSettings.metadataEndpoint || ""}
              onChange={(e) => handleLMMetadataEndpointChange(e.target.value)}
              placeholder="e.g. http://127.0.0.1:1234/api/v0/models"
              className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />
            <span className="text-[10px] text-muted-foreground block mt-0.5">
              Used by Auto-Detect to read the real context window. Leave
              empty to use <code>&lt;baseUrl&gt;/models</code>. Recommended:{" "}
              <code>/api/v0/models</code>.
            </span>
          </div>

          <div className="flex items-center justify-between pt-2 flex-wrap gap-2">
            <div className="flex items-center gap-2 flex-wrap">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleTestLMStudio}
                disabled={testingId === "lmstudio"}
                className="h-7 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
              >
                <Zap
                  size={11}
                  className={`text-amber-500 mr-1 ${
                    testingId === "lmstudio" ? "animate-spin" : ""
                  }`}
                />
                <span>
                  {testingId === "lmstudio" ? "Testing…" : "Test Connection"}
                </span>
              </Button>

              {lmStudioSettings.status === "online" && (
                <span className="text-[11px] text-emerald-600 dark:text-emerald-400 font-semibold flex items-center gap-1">
                  <CheckCircle2 size={12} /> Connected (
                  {lmStudioSettings.latency} ms)
                </span>
              )}
              {lmStudioSettings.status === "offline" && (
                <span className="text-[11px] text-rose-500 flex items-center gap-1 font-medium">
                  <XCircle size={12} />{" "}
                  {lmStudioSettings.lastError || "Unreachable"}
                </span>
              )}
            </div>

            <Button
              type="button"
              onClick={() =>
                activateEngine(
                  "lmstudio",
                  "LM Studio (Local)",
                  lmStudioSettings.model,
                  lmStudioSettings.baseUrl,
                  lmStudioSettings.apiKey,
                  ""
                )
              }
              className="h-7 text-xs bg-primary text-primary-foreground hover:bg-primary/70 font-medium cursor-pointer hover:scale-105
              transition-transform shadow-sm"
            >
              Set as Active Primary
            </Button>
          </div>
        </div>

        {/* Loaded models sub-card */}
        <div className="p-3.5 rounded-md border border-border/80 bg-background/50 space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
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
                  onClick={handleClearLMModels}
                  className="text-[11px] text-muted-foreground hover:text-rose-500 cursor-pointer transition-colors"
                >
                  Clear All
                </button>
              )}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleFetchModels}
                disabled={fetchingModels}
                className="h-6 text-[11px] border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
              >
                <RefreshCw
                  size={10}
                  className={`mr-1 ${fetchingModels ? "animate-spin" : ""}`}
                />
                <span>Scan Models</span>
              </Button>
            </div>
          </div>

          {/* Manual add */}
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={newLMModelInput}
              onChange={(e) => setNewLMModelInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleAddLMModel()}
              placeholder="Add local model ID (e.g. qwen3-vl-8b)…"
              className="flex-1 bg-background border border-border rounded-sm px-2.5 py-1 text-xs text-foreground font-mono placeholder:text-muted-foreground shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleAddLMModel}
              disabled={!newLMModelInput.trim()}
              className="h-6 text-[11px] cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
            >
              <Plus size={10} className="mr-1" /> Add
            </Button>
          </div>

          {availableModels.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-72 overflow-y-auto p-0.5">
              {availableModels.map((m) => {
                const isActive =
                  config.llm.provider === "lmstudio" && config.llm.model === m;
                const caps: InferredCapabilities = inferModelCapabilities(
                  m,
                  metadataVault[m] as InferenceMeta
                );
                return (
                  <ModelItemCard
                    key={m}
                    model={m}
                    isActive={isActive}
                    accent="#6366f1"
                    capabilities={caps}
                    onSetPrimary={() => assignDetectedModel(m, "primary")}
                    onSetVision={() => assignDetectedModel(m, "vision")}
                    onRemove={() => handleRemoveLMModel(m)}
                    onOpenInfo={() => onOpenModelInfo(m)}
                  />
                );
              })}
            </div>
          )}
        </div>
      </motion.div>

      {/* =================================================================== */}
      {/* 2. OLLAMA CARD                                                      */}
      {/* =================================================================== */}
      <motion.div
        {...FADE_UP}
        transition={{ ...FADE_UP.transition, delay: 0.05 }}
        className="relative p-5 rounded-md border border-border bg-card shadow-sm space-y-4"
      >
        <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md bg-gradient-to-r from-transparent via-amber-500/60 to-transparent" />

        {/* Header */}
        <div className="flex items-center justify-between border-b border-border/60 pb-3 flex-wrap gap-2">
          <div className="flex items-center gap-2.5">
            <div className="p-2 icon">
              <Icon name="ollama-mono" size={25} />

            </div>
            <span className="text-xs font-bold text-foreground uppercase tracking-wider">
              Ollama Local Server
            </span>
            <span className="text-[9px] px-1.5 py-0.5 rounded-sm bg-muted text-muted-foreground font-mono">
              Port: 11434 (Local)
            </span>
          </div>

          <div className="flex items-center gap-2">
            {ollamaSettings.status === "online" && (
              <span className="text-[11px] text-emerald-600 dark:text-emerald-400 font-semibold flex items-center gap-1">
                <CheckCircle2 size={12} /> Connected ({ollamaSettings.latency}{" "}
                ms)
              </span>
            )}
            {ollamaSettings.status === "offline" && (
              <span className="text-[11px] text-rose-500 flex items-center gap-1 font-medium">
                <XCircle size={12} />{" "}
                {ollamaSettings.lastError || "Unreachable"}
              </span>
            )}
          </div>
        </div>

        {/* Connection settings */}
        <div className="space-y-3 text-xs">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
                Ollama Base URL
              </label>
              <input
                type="text"
                value={ollamaSettings.baseUrl}
                onChange={(e) => handleOllamaBaseUrlChange(e.target.value)}
                placeholder="http://127.0.0.1:11434"
                className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>

            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
                Default Ollama Model
              </label>
              <input
                type="text"
                value={ollamaSettings.model}
                onChange={(e) => handleOllamaModelChange(e.target.value)}
                placeholder="e.g. llama3.2, qwen2.5, deepseek-r1"
                className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>
          </div>

          <div>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
              Metadata Endpoint (optional)
            </label>
            <input
              type="text"
              value={ollamaSettings.metadataEndpoint || ""}
              onChange={(e) => handleOllamaMetadataEndpointChange(e.target.value)}
              placeholder="e.g. http://127.0.0.1:11434/api/tags"
              className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />
            <span className="text-[10px] text-muted-foreground block mt-0.5">
              Used by Auto-Detect to read the real context window. Leave
              empty to use <code>&lt;baseUrl&gt;/models</code>.
            </span>
          </div>

          <div className="flex items-center justify-between pt-1 flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleTestOllama}
              disabled={testingId === "ollama"}
              className="h-7 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
            >
              <Zap
                size={11}
                className={`text-amber-500 mr-1 ${
                  testingId === "ollama" ? "animate-spin" : ""
                }`}
              />
              <span>
                {testingId === "ollama" ? "Testing…" : "Test Connection"}
              </span>
            </Button>

            {ollamaSettings.model && (
              <Button
                type="button"
                onClick={() => {
                  const baseUrl = ollamaSettings.baseUrl.endsWith("/v1")
                    ? ollamaSettings.baseUrl
                    : `${ollamaSettings.baseUrl}/v1`;
                  activateEngine(
                    "ollama",
                    "Ollama (Local)",
                    ollamaSettings.model,
                    baseUrl,
                    "",
                    ""
                  );
                }}
                className="h-7 text-xs bg-amber-600 hover:bg-amber-700 text-white font-medium cursor-pointer hover:scale-105 transition-transform shadow-sm"
              >
                Set as Active Primary
              </Button>
            )}
          </div>
        </div>

        {/* Discovered models sub-card */}
        <div className="p-3.5 rounded-md border border-border/80 bg-background/50 space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-2">

              <Icon name="ollama-light" size={18} />

              <span className="text-xs font-semibold text-foreground">
                Ollama Models ({ollamaSettings.savedModels?.length || 0})
              </span>
            </div>
            <div className="flex items-center gap-2">
              {ollamaSettings.savedModels &&
                ollamaSettings.savedModels.length > 0 && (
                  <button
                    type="button"
                    onClick={handleClearOllama}
                    className="text-[11px] text-muted-foreground hover:text-rose-500 cursor-pointer transition-colors"
                    title="Clear Ollama models and remove from engine selector"
                  >
                    Clear All
                  </button>
                )}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleScanOllama}
                disabled={scanningOllama}
                className="h-6 text-[11px] border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
              >
                <RefreshCw
                  size={10}
                  className={`mr-1 ${scanningOllama ? "animate-spin" : ""}`}
                />
                <span>Scan Models</span>
              </Button>
            </div>
          </div>

          {/* Manual add */}
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={newOllamaModelInput}
              onChange={(e) => setNewOllamaModelInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleAddOllamaModel()}
              placeholder="Add Ollama model ID (e.g. deepseek-r1:7b)…"
              className="flex-1 bg-background border border-border rounded-sm px-2.5 py-1 text-xs text-foreground font-mono placeholder:text-muted-foreground shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleAddOllamaModel}
              disabled={!newOllamaModelInput.trim()}
              className="h-6 text-[11px] cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
            >
              <Plus size={10} className="mr-1" /> Add
            </Button>
          </div>

          {ollamaSettings.savedModels && ollamaSettings.savedModels.length > 0 ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-72 overflow-y-auto p-0.5">
              {ollamaSettings.savedModels.map((m) => {
                const isActive =
                  config.llm.provider === "ollama" && config.llm.model === m;
                const caps: InferredCapabilities = inferModelCapabilities(
                  m,
                  metadataVault[m] as InferenceMeta
                );
                return (
                  <ModelItemCard
                    key={m}
                    model={m}
                    isActive={isActive}
                    accent="#f59e0b"
                    capabilities={caps}
                    onSetPrimary={() => {
                      const baseUrl = ollamaSettings.baseUrl.endsWith("/v1")
                        ? ollamaSettings.baseUrl
                        : `${ollamaSettings.baseUrl}/v1`;
                      setOllamaSettings((p) => ({ ...p, model: m }));
                      activateEngine(
                        "ollama",
                        "Ollama (Local)",
                        m,
                        baseUrl,
                        "",
                        ""
                      );
                    }}
                    onRemove={() => handleRemoveOllamaModel(m)}
                    onOpenInfo={() => onOpenModelInfo(m)}
                  />
                );
              })}
            </div>
          ) : (
            <div className="py-4 text-center text-xs text-muted-foreground bg-muted/20 border border-dashed border-border rounded-md">
              <span>
                {EMPTY_MODELS_HINT}
                &quot;Scan Models&quot; to detect local models.
              </span>
            </div>
          )}
        </div>
      </motion.div>
    </div>
  );
}
