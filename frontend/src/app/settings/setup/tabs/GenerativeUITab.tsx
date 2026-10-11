// frontend/src/app/settings/setup/tabs/GenerativeUITab.tsx
"use client";

import React from "react";
import {
  Sparkles,
  Layers,
  Cpu,
  BarChart3,
  RotateCcw,
  CheckCircle2,
  AlertCircle,
  Clock,
  Coins,
} from "lucide-react";
import {
  useGenerativeUIStore,
  ALL_SUPPORTED_COMPONENTS,
} from "@/components/generative-ui/store";
import type { GenerativeUIPreset } from "@/components/generative-ui/types";

export function GenerativeUITab(): React.JSX.Element {
  const {
    enabled,
    preset,
    allowedComponents,
    maxOutputTokens,
    maxRetries,
    telemetry,
    setEnabled,
    setPreset,
    toggleComponent,
    setMaxOutputTokens,
    setMaxRetries,
  } = useGenerativeUIStore();

  const presets: { id: GenerativeUIPreset; title: string; desc: string }[] = [
    {
      id: "economic",
      title: "Economic (Min Tokens)",
      desc: "Strictly limited to Cards and Action Buttons (~120 prompt tokens).",
    },
    {
      id: "balanced",
      title: "Balanced (Recommended)",
      desc: "Adds Tables, Metrics, Alerts, and Test Results (~220 prompt tokens).",
    },
    {
      id: "full",
      title: "Full Power",
      desc: "Enables all 11 catalog components including Charts and Forms (~350 prompt tokens).",
    },
    {
      id: "custom",
      title: "Custom Selection",
      desc: "Manually toggle individual components to match your exact workflow.",
    },
  ];

  const successRate =
    telemetry.totalInvocations > 0
      ? Math.round(
          (telemetry.successfulRenders / telemetry.totalInvocations) * 100
        )
      : 100;

  return (
    <div className="space-y-6 max-w-4xl text-foreground font-sans">
      {/* Header Banner */}
      <div className="flex items-start justify-between pb-4 border-b border-border/60">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-primary" />
            <h2 className="text-base font-semibold tracking-tight">
              Generative UI & OpenUI Engine
            </h2>
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            Configure dynamic interactive canvas components and manage strict token boundaries.
          </p>
        </div>

        {/* Master Kill-Switch Toggle */}
        <label className="flex items-center gap-2.5 cursor-pointer bg-muted/40 px-3 py-1.5 rounded-xl border border-border/60 hover:bg-muted/60 transition-colors">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="w-4 h-4 rounded text-primary focus:ring-primary cursor-pointer"
          />
          <span className="text-xs font-semibold">
            {enabled ? "Feature Enabled" : "Feature Disabled"}
          </span>
        </label>
      </div>

      {/* Main Settings Sections */}
      <div className={enabled ? "space-y-6" : "space-y-6 opacity-50 pointer-events-none"}>
        {/* Preset Profiles */}
        <div className="space-y-3">
          <label className="text-xs font-semibold flex items-center gap-2 text-foreground">
            <Layers className="h-4 w-4 text-primary" />
            <span>Catalog Profile Preset</span>
          </label>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {presets.map((p) => {
              const isSelected = preset === p.id;
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => setPreset(p.id)}
                  className={`p-3.5 rounded-xl border text-left transition-all cursor-pointer flex flex-col justify-between space-y-1.5 ${
                    isSelected
                      ? "border-primary bg-primary/5 shadow-xs"
                      : "border-border/70 bg-card/40 hover:bg-card/70"
                  }`}
                >
                  <div className="flex items-center justify-between w-full">
                    <span className="text-xs font-semibold text-foreground">
                      {p.title}
                    </span>
                    {isSelected && (
                      <CheckCircle2 className="h-3.5 w-3.5 text-primary shrink-0" />
                    )}
                  </div>
                  <p className="text-[11px] text-muted-foreground leading-relaxed">
                    {p.desc}
                  </p>
                </button>
              );
            })}
          </div>
        </div>

        {/* Allowed Components Catalog Selection */}
        <div className="space-y-3">
          <label className="text-xs font-semibold flex items-center gap-2 text-foreground">
            <Cpu className="h-4 w-4 text-primary" />
            <span>Active Catalog Components ({allowedComponents.length} of {ALL_SUPPORTED_COMPONENTS.length})</span>
          </label>
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2.5">
            {ALL_SUPPORTED_COMPONENTS.map((comp) => {
              const isChecked = allowedComponents.includes(comp);
              return (
                <label
                  key={comp}
                  className={`flex items-center gap-2 p-2.5 rounded-lg border text-xs cursor-pointer transition-colors ${
                    isChecked
                      ? "border-primary/50 bg-primary/5 text-foreground font-medium"
                      : "border-border/60 bg-muted/20 text-muted-foreground hover:bg-muted/40"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={isChecked}
                    onChange={() => toggleComponent(comp)}
                    className="rounded text-primary focus:ring-primary w-3.5 h-3.5 cursor-pointer"
                  />
                  <span className="font-mono text-[11px]">{comp}</span>
                </label>
              );
            })}
          </div>
        </div>

        {/* Budget and Guardrails */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-2">
          {/* Max Output Budget */}
          <div className="p-3.5 rounded-xl border border-border/70 bg-card/40 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-foreground flex items-center gap-1.5">
                <Coins className="h-3.5 w-3.5 text-muted-foreground" />
                <span>Max UI Output Budget</span>
              </span>
              <span className="text-xs font-mono font-semibold text-primary">
                {maxOutputTokens} tokens
              </span>
            </div>
            <input
              type="range"
              min={512}
              max={4096}
              step={256}
              value={maxOutputTokens}
              onChange={(e) => setMaxOutputTokens(Number(e.target.value))}
              className="w-full h-1.5 bg-muted rounded-lg appearance-none cursor-pointer accent-primary"
            />
            <p className="text-[10px] text-muted-foreground">
              Caps generative UI completion tokens per attempt to prevent budget runaway.
            </p>
          </div>

          {/* Self-Correction Retries */}
          <div className="p-3.5 rounded-xl border border-border/70 bg-card/40 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-foreground flex items-center gap-1.5">
                <RotateCcw className="h-3.5 w-3.5 text-muted-foreground" />
                <span>Auto-Correction Retries</span>
              </span>
              <span className="text-xs font-mono font-semibold text-primary">
                {maxRetries} retry
              </span>
            </div>
            <input
              type="range"
              min={0}
              max={2}
              step={1}
              value={maxRetries}
              onChange={(e) => setMaxRetries(Number(e.target.value))}
              className="w-full h-1.5 bg-muted rounded-lg appearance-none cursor-pointer accent-primary"
            />
            <p className="text-[10px] text-muted-foreground">
              Maximum retry attempts if syntax validation fails before falling back to Markdown.
            </p>
          </div>
        </div>

        {/* Live Telemetry & Accounting Panel */}
        <div className="p-4 rounded-xl border border-border/70 bg-muted/20 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold flex items-center gap-1.5 text-foreground">
              <BarChart3 className="h-4 w-4 text-primary" />
              <span>Session Telemetry & Observability</span>
            </span>
            <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20 font-bold">
              {successRate}% Success Rate
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 pt-1 text-center font-mono">
            <div className="p-2.5 rounded-lg bg-background border border-border/60">
              <div className="text-[10px] text-muted-foreground">Invocations</div>
              <div className="text-sm font-bold text-foreground">
                {telemetry.totalInvocations}
              </div>
            </div>
            <div className="p-2.5 rounded-lg bg-background border border-border/60">
              <div className="text-[10px] text-emerald-600 dark:text-emerald-400">Rendered UI</div>
              <div className="text-sm font-bold text-emerald-600 dark:text-emerald-400">
                {telemetry.successfulRenders}
              </div>
            </div>
            <div className="p-2.5 rounded-lg bg-background border border-border/60">
              <div className="text-[10px] text-amber-600 dark:text-amber-400">Markdown Fallbacks</div>
              <div className="text-sm font-bold text-amber-600 dark:text-amber-400">
                {telemetry.fallbacksToMarkdown}
              </div>
            </div>
            <div className="p-2.5 rounded-lg bg-background border border-border/60">
              <div className="text-[10px] text-muted-foreground flex items-center justify-center gap-1">
                <Clock size={10} />
                <span>Latency</span>
              </div>
              <div className="text-sm font-bold text-foreground">
                {telemetry.lastRenderLatencyMs}ms
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
