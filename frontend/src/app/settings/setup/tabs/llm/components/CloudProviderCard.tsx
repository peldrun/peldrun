// frontend/src/app/settings/setup/tabs/llm/components/CloudProviderCard.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import {
  CheckCircle2,
  Eye,
  EyeOff,
  KeyRound,
  RefreshCw,
  ShieldAlert,
  XCircle,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/Icon/Icon";
import { resolveProviderIcon } from "../constants";
import type { CloudProviderCardProps } from "../types";

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

/**
 * Single cloud-provider card.
 *
 * Responsibilities (unchanged from the original LLM tab):
 *   - Display provider identity (icon, name, badge) + live status,
 *   - Edit model, base URL, and API key,
 *   - Expose "Set Primary" / "Set Vision" actions,
 *   - Trigger connection test and model discovery.
 *
 * Visual language aligned with the SearchTab reference design: motion fade,
 * icon capsule, hover scale on actionable buttons, and semantic status chips.
 */
export function CloudProviderCard({
  provider,
  isPrimary,
  isVision,
  isTesting,
  showKey,
  onToggleKey,
  onChange,
  onTest,
  onActivate,
  onSetVision,
  onOpenDiscovery,
}: CloudProviderCardProps) {
  const iconName = resolveProviderIcon(provider.id || provider.name);
  const status = provider.status;

  const savedModelValue =
    provider.savedModels && provider.savedModels.includes(provider.model)
      ? provider.model
      : provider.popularModels.includes(provider.model)
      ? provider.model
      : "custom";

  return (
    <motion.div
      {...FADE_UP}
      whileHover={{ y: -2, transition: { duration: 0.15 } }}
      className="relative p-4 rounded-md border border-border bg-card space-y-3.5 shadow-sm hover:border-primary/30 transition-all"
    >
      <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md bg-gradient-to-r from-transparent via-primary/40 to-transparent opacity-0 hover:opacity-100 transition-opacity" />

      {/* Header */}
      <div className="flex items-center justify-between border-b border-border/50 pb-2.5 gap-2 flex-wrap">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="shrink-0 flex items-center justify-center text-primary">
            <Icon
              name={iconName}
              size={16}
              title={`${provider.name} logo`}
             variant="light"
            />
          </span>

          <span className="text-xs font-bold text-foreground truncate">
            {provider.name}
          </span>

          <span className="text-[9px] px-1.5 py-0.5 rounded-sm bg-muted text-muted-foreground font-mono">
            {provider.badge}
          </span>

          {status === "online" && (
            <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded-sm border border-emerald-500/20">
              <CheckCircle2 size={11} /> Connected ({provider.latency} ms)
            </span>
          )}
          {status === "offline" && (
            <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-rose-600 dark:text-rose-400 bg-rose-500/10 px-2 py-0.5 rounded-sm border border-rose-500/20">
              <XCircle size={11} /> Connection Failed
            </span>
          )}
          {status === "untested" && (
            <span className="text-[10px] text-amber-500 bg-amber-500/10 px-2 py-0.5 rounded-sm border border-amber-500/20">
              Untested
            </span>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {isPrimary ? (
            <span className="text-[10px] font-bold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-2.5 py-1 rounded-sm border border-emerald-500/20">
              Active Primary
            </span>
          ) : (
            <button
              type="button"
              onClick={onActivate}
              className="px-2.5 py-1 rounded-sm text-[10px] bg-primary text-primary-foreground font-medium cursor-pointer hover:opacity-90 hover:scale-105 transition-transform"
            >
              Set Primary
            </button>
          )}

          {!isVision && (
            <button
              type="button"
              onClick={onSetVision}
              className="px-2 py-1 rounded-sm text-[10px] bg-violet-500/10 text-violet-500 font-medium hover:bg-violet-500/20 border border-violet-500/20 cursor-pointer transition-colors"
            >
              Set Vision
            </button>
          )}
        </div>
      </div>

      {/* Fields */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
        {/* Model */}
        <div>
          <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
            Model
          </label>
          <div className="space-y-1">
            <select
              value={savedModelValue}
              onChange={(e) => {
                if (e.target.value !== "custom") {
                  onChange({ model: e.target.value });
                }
              }}
              className="w-full bg-background border border-border rounded-sm px-2.5 py-1 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow cursor-pointer"
            >
              {provider.savedModels && provider.savedModels.length > 0 && (
                <optgroup label="Discovered & Saved Models">
                  {provider.savedModels.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </optgroup>
              )}
              <optgroup label="Popular Presets">
                {provider.popularModels.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </optgroup>
              <option value="custom">-- Custom Model ID --</option>
            </select>

            <input
              type="text"
              value={provider.model}
              onChange={(e) => onChange({ model: e.target.value })}
              placeholder="Model identifier…"
              className="w-full bg-background border border-border rounded-sm px-2.5 py-1 text-[11px] text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />
          </div>
        </div>

        {/* Base URL */}
        <div>
          <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
            Base Endpoint URL
          </label>
          <input
            type="text"
            value={provider.baseUrl}
            onChange={(e) =>
              onChange({ baseUrl: e.target.value, status: "untested" })
            }
            className="w-full bg-background border border-border rounded-sm px-2.5 py-1 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
          />
        </div>

        {/* API Key */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1">
              <KeyRound size={11} /> API Key
            </label>
            <button
              type="button"
              onClick={onToggleKey}
              className="text-[9px] text-muted-foreground hover:text-foreground flex items-center gap-0.5 cursor-pointer transition-colors"
            >
              {showKey ? <EyeOff size={10} /> : <Eye size={10} />}
              <span>{showKey ? "Hide" : "Show"}</span>
            </button>
          </div>
          <input
            type={showKey ? "text" : "password"}
            value={provider.apiKey}
            onChange={(e) =>
              onChange({ apiKey: e.target.value, status: "untested" })
            }
            placeholder={`Enter key for ${provider.name}…`}
            className="w-full bg-background border border-border rounded-sm px-2.5 py-1 text-xs text-foreground font-mono shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
          />
          <span className="text-[9px] text-muted-foreground block mt-1 font-mono">
            {provider.keyPrefixHint}
          </span>
        </div>
      </div>

      {/* Error banner */}
      {status === "offline" && provider.lastError && (
        <div className="p-2 rounded-sm bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs flex items-center gap-1.5">
          <ShieldAlert size={13} className="shrink-0" />
          <span className="font-mono text-[11px]">{provider.lastError}</span>
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center justify-between pt-1 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onTest}
            disabled={isTesting}
            className="h-7 px-2.5 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100 flex items-center gap-1"
          >
            <Zap
              size={11}
              className={`text-amber-500 ${isTesting ? "animate-spin" : ""}`}
            />
            <span>{isTesting ? "Verifying…" : "Test Connection"}</span>
          </Button>

          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onOpenDiscovery}
            className="h-7 px-2.5 text-xs border-border bg-card hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform flex items-center gap-1.5"
          >
            <RefreshCw size={11} className="text-primary" />
            <span>Scan Models</span>
            {provider.savedModels && provider.savedModels.length > 0 && (
              <span className="ml-1 px-1.5 py-0.5 text-[10px] rounded-sm bg-primary/15 text-primary font-mono font-bold">
                {provider.savedModels.length}
              </span>
            )}
          </Button>
        </div>

        <span className="text-[10px] text-muted-foreground font-mono">
          Credentials isolated to this provider
        </span>
      </div>
    </motion.div>
  );
}
