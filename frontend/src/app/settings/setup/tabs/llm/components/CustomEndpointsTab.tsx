// frontend/src/app/settings/setup/tabs/llm/components/CustomEndpointsTab.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import {
  ChevronRight,
  Plus,
  RefreshCw,
  Terminal,
  Trash2,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/Icon/Icon";
import { ProviderIconPicker } from "./ProviderIconPicker";
import { resolveProviderIcon } from "../constants";
import type {
  ApiModeType,
  CustomEndpointWithLogo,
  CustomEndpointsTabProps,
} from "../types";

/* -------------------------------------------------------------------------- */
/* Constants                                                                  */
/* -------------------------------------------------------------------------- */

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

const API_MODES: readonly ApiModeType[] = [
  "Auto-detect",
  "Chat Completions",
  "Responses API",
  "Anthropic Messages",
];

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Sub-tab 4 of the LLM settings: user-defined custom endpoints.
 *
 * Hosts:
 *   - the list of saved custom endpoints (with per-endpoint Scan / Use / Delete),
 *   - the "Add Custom Endpoint" panel, including the new provider logo picker.
 *
 * The provider logo is persisted alongside each endpoint under the
 * `badge` key so that user-created providers visually match the
 * built-in ones on subsequent renders.
 */
export function CustomEndpointsTab({
  customEndpoints,
  setCustomEndpoints,
  activateEngine,
  endpointForm,
  setEndpointForm,
  testingId,
  handleTestCustomForm,
  handleSaveCustomEndpoint,
  handleDeleteCustomEndpoint,
  onOpenDiscovery,
}: CustomEndpointsTabProps) {
  /* -------------------------- Actions ----------------------------------- */

  const handleOpenScan = (ce: CustomEndpointWithLogo) => {
    onOpenDiscovery({
      id: ce.id,
      name: ce.name,
      baseUrl: ce.endpointUrl,
      apiKey: ce.apiKey || "",
      type: "",
      savedModels: ce.savedModels || [],
      isCustom: true,
      badge: ce.badge,
    });
  };

  /* -------------------------- Render ------------------------------------ */

  return (
    <div className="w-full max-w-7xl space-y-5 pt-2 mx-auto">
      {/* Breadcrumb */}
      <motion.div
        {...FADE_UP}
        className="text-xs text-muted-foreground flex items-center gap-1 font-mono"
      >
        <span>Settings</span>
        <ChevronRight size={11} className="opacity-60" />
        <span>Providers</span>
        <ChevronRight size={11} className="opacity-60" />
        <span className="text-foreground font-bold">Custom Endpoints</span>
      </motion.div>

      {/* Saved endpoints list */}
      <motion.div
        {...FADE_UP}
        transition={{ ...FADE_UP.transition, delay: 0.05 }}
        className="space-y-3"
      >
        {customEndpoints.length === 0 ? (
          <div className="p-6 rounded-md border border-dashed border-border bg-card text-center text-xs text-muted-foreground">
            No custom endpoints configured yet. Add your local vLLM, FastChat,
            or remote custom API servers below.
          </div>
        ) : (
          customEndpoints.map((ce) => {
            const withLogo = ce as CustomEndpointWithLogo;
            const iconName = withLogo.badge
              ? withLogo.badge
              : resolveProviderIcon(ce.providerId || ce.name);

            return (
              <motion.div
                key={ce.id}
                whileHover={{ y: -2, transition: { duration: 0.15 } }}
                className="relative p-4 rounded-md border border-card bg-muted shadow-md transition-all flex items-center justify-between gap-3 flex-wrap"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <span className="shrink-0 w-9 h-9 flex items-center justify-center text-primary">
                    <Icon
                      name={iconName}
                      size={22}
                      title={`${ce.name} logo`}
                    />
                  </span>
                  <div className="space-y-0.5 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-bold text-foreground truncate">
                        {ce.name}
                      </span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded-sm bg-muted text-muted-foreground font-mono">
                        {ce.defaultModel}
                      </span>
                    </div>
                    <span
                      className="text-xs text-muted-foreground font-mono block truncate"
                      title={ce.endpointUrl}
                    >
                      {ce.endpointUrl}
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-2 shrink-0">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => handleOpenScan(withLogo)}
                    className="h-8 px-2.5 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform flex items-center gap-1"
                    title="Scan available models for this endpoint"
                  >
                    <RefreshCw size={11} className="text-primary" />
                    <span>Scan</span>
                    {ce.savedModels && ce.savedModels.length > 0 && (
                      <span className="ml-1 px-1.5 py-0.5 text-[10px] rounded-sm bg-primary/15 text-primary font-mono font-bold">
                        {ce.savedModels.length}
                      </span>
                    )}
                  </Button>

                  <Button
                    type="button"
                    onClick={() =>
                      activateEngine(
                        ce.providerId,
                        ce.name,
                        ce.defaultModel,
                        ce.endpointUrl,
                        ce.apiKey,
                        ""
                      )
                    }
                    className="h-8 px-3 text-xs bg-primary text-primary-foreground font-medium flex items-center gap-1.5 cursor-pointer hover:scale-105 transition-transform shadow-sm"
                  >
                    <Zap size={12} />
                    <span>Use</span>
                  </Button>

                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDeleteCustomEndpoint(ce.id)}
                    className="h-8 w-8 p-0 text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 cursor-pointer transition-colors"
                  >
                    <Trash2 size={14} />
                  </Button>
                </div>
              </motion.div>
            );
          })
        )}
      </motion.div>

      {/* Add new endpoint panel */}
      <motion.div
        {...FADE_UP}
        transition={{ ...FADE_UP.transition, delay: 0.1 }}
        className="relative p-5 rounded-md border border-border bg-gigbg shadow-sm space-y-4"
      >
        <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md
         bg-gradient-to-r from-transparent via-rb/60 to-transparent" />

        {/* Header */}
        <div className="flex items-center gap-2.5 pb-2 border-b border-border/60">
          <div className="p-2 icon">
            <Plus size={14} className="text-primary" />
          </div>
          <span className="text-xs font-bold text-foreground uppercase tracking-wider">
            Add Custom Endpoint
          </span>
        </div>

        <div className="space-y-3.5 text-xs">
          {/* Name + Provider ID + Logo */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3.5">
            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
                Name
              </label>
              <input
                type="text"
                value={endpointForm.name}
                onChange={(e) =>
                  setEndpointForm({ ...endpointForm, name: e.target.value })
                }
                placeholder="e.g. Local vLLM, FastChat…"
                className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>

            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
                Provider ID
              </label>
              <input
                type="text"
                value={endpointForm.providerId}
                onChange={(e) =>
                  setEndpointForm({
                    ...endpointForm,
                    providerId: e.target.value,
                  })
                }
                placeholder="e.g. local-vllm, custom-endpoint"
                className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground shadow-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>

            <ProviderIconPicker
              value={endpointForm.badge}
              onChange={(iconName) =>
                setEndpointForm({ ...endpointForm, badge: iconName })
              }
            />
          </div>

          {/* Endpoint URL */}
          <div>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
              Endpoint URL
            </label>
            <input
              type="text"
              value={endpointForm.endpointUrl}
              onChange={(e) =>
                setEndpointForm({
                  ...endpointForm,
                  endpointUrl: e.target.value,
                })
              }
              placeholder="e.g. http://127.0.0.1:8000/v1"
              className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground shadow-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />
          </div>

          {/* Metadata Endpoint (optional) */}
          <div>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
              Metadata Endpoint (optional)
            </label>
            <input
              type="text"
              value={endpointForm.metadataEndpoint || ""}
              onChange={(e) =>
                setEndpointForm({
                  ...endpointForm,
                  metadataEndpoint: e.target.value,
                })
              }
              placeholder="Leave empty to use <endpointUrl>/models"
              className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground shadow-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />
            <span className="text-[10px] text-muted-foreground block mt-0.5">
              Used by Auto-Detect to read the context window from a path
              different from <code>&lt;endpointUrl&gt;/models</code>.
            </span>
          </div>

          {/* API Mode */}
          <div>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5">
              API Mode
            </label>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-1 p-1 rounded-sm bg-custom border border-border">
              {API_MODES.map((mode) => {
                const isActive = endpointForm.apiMode === mode;
                return (
                  <button
                    key={mode}
                    type="button"
                    onClick={() =>
                      setEndpointForm({ ...endpointForm, apiMode: mode })
                    }
                    className={`py-1.5 px-2 rounded-sm text-[11px] font-medium transition-all cursor-pointer ${
                      isActive
                        ? "bg-card text-foreground shadow-sm font-bold border border-border"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {mode}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Default model + context */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3.5">
            <div className="md:col-span-2">
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
                Default Model
              </label>
              <input
                type="text"
                value={endpointForm.defaultModel}
                onChange={(e) =>
                  setEndpointForm({
                    ...endpointForm,
                    defaultModel: e.target.value,
                  })
                }
                placeholder="e.g. meta-llama/Llama-3-8B-Instruct"
                className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground shadow-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>

            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
                Context
              </label>
              <input
                type="text"
                value={endpointForm.contextWindow}
                onChange={(e) =>
                  setEndpointForm({
                    ...endpointForm,
                    contextWindow: e.target.value,
                  })
                }
                placeholder="Auto"
                className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground shadow-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>
          </div>

          {/* API Key */}
          <div>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
              API Key
            </label>
            <input
              type="password"
              value={endpointForm.apiKey}
              onChange={(e) =>
                setEndpointForm({ ...endpointForm, apiKey: e.target.value })
              }
              placeholder="Leave blank for unauthenticated local servers"
              className="w-full bg-background border border-border rounded-sm px-3 py-1.5 text-xs text-foreground shadow-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />
          </div>

          {/* Use for new chats */}
          <div className="flex items-center gap-4 pt-1">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={endpointForm.useForNewChats}
                onChange={(e) =>
                  setEndpointForm({
                    ...endpointForm,
                    useForNewChats: e.target.checked,
                  })
                }
                className="rounded-sm border-border text-primary cursor-pointer"
              />
              <span className="text-xs text-foreground">
                Use for new chats
              </span>
            </label>
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2.5 pt-3 border-t border-border/60 flex-wrap">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleTestCustomForm}
              disabled={testingId === "custom_form"}
              className="h-8 px-3 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100 flex items-center gap-1.5"
            >
              <Zap
                size={12}
                className={`text-amber-500 ${
                  testingId === "custom_form" ? "animate-spin" : ""
                }`}
              />
              <span>Test</span>
            </Button>

            <Button
              type="button"
              size="sm"
              onClick={handleSaveCustomEndpoint}
              className="h-8 px-4 text-xs bg-primary text-primary-foreground font-medium cursor-pointer hover:scale-105 transition-transform shadow-sm"
            >
              Save
            </Button>

            <span className="text-[11px] text-muted-foreground ml-2">
              New endpoint
            </span>
          </div>
        </div>
      </motion.div>

      {/* Footer note */}
      <motion.div
        {...FADE_UP}
        transition={{ ...FADE_UP.transition, delay: 0.15 }}
        className="flex items-center gap-2 text-[11px] text-muted-foreground font-mono"
      >
        <Terminal size={12} className="opacity-60" />
        <span>
          Custom endpoints are stored locally under <code>omweb_custom_endpoints</code>.
        </span>
      </motion.div>
    </div>
  );
}
