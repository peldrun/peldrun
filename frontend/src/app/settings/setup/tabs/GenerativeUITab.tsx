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
  Clock,
  Coins,
  Activity,
  Table as TableIcon,
  GitFork,
  Sigma,
  MousePointerClick,
  SlidersHorizontal,
} from "lucide-react";
import {
  useGenerativeUIStore,
  ALL_SUPPORTED_COMPONENTS,
} from "@/components/generative-ui/store";
import type { GenerativeUIPreset } from "@/components/generative-ui/types";

interface ComponentMeta {
  name: string;
  desc: string;
  tokens: string;
  category: "kpi" | "data" | "visuals" | "scientific";
}

const CATALOG_METADATA: ComponentMeta[] = [
  // Core Status & KPIs
  { name: "Card", desc: "Executive summaries & structured containers", tokens: "~20 tok", category: "kpi" },
  { name: "Metric", desc: "KPI numbers with trend arrows and deltas", tokens: "~15 tok", category: "kpi" },
  { name: "Badge", desc: "Service health & status pills", tokens: "~10 tok", category: "kpi" },
  { name: "Alert", desc: "Warning, error, and info callout banners", tokens: "~15 tok", category: "kpi" },
  { name: "Progress", desc: "Percentage task completion bars", tokens: "~12 tok", category: "kpi" },
  { name: "ActionButton", desc: "Interactive triggers for confirmed agent actions", tokens: "~10 tok", category: "kpi" },

  // Structured Data & Testing
  { name: "Table", desc: "Bounded responsive comparison tables (max 15 rows)", tokens: "~35 tok", category: "data" },
  { name: "TestResults", desc: "CI/CD pass/fail suites with auto percentages", tokens: "~25 tok", category: "data" },
  { name: "Accordion", desc: "Collapsible disclosure items for diagnostics", tokens: "~20 tok", category: "data" },

  // Advanced Visuals & Flows
  { name: "Chart", desc: "Shadcn Area, Bar, Line, and Donut analytics", tokens: "~45 tok", category: "visuals" },
  { name: "Diagram", desc: "Mermaid.js workflow, flowchart & agent DAGs", tokens: "~40 tok", category: "visuals" },
  { name: "Carousel", desc: "Multi-card horizontal slider & feature showcase", tokens: "~30 tok", category: "visuals" },

  // Scientific & Forms
  { name: "Form", desc: "Interactive parameter configuration inputs", tokens: "~35 tok", category: "scientific" },
  { name: "MathPlot", desc: "KaTeX LaTeX equations & 2D function curves", tokens: "~35 tok", category: "scientific" },
];

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

  const presets: { id: GenerativeUIPreset; title: string; desc: string; tokenEst: string }[] = [
    {
      id: "economic",
      title: "Economic (Minimal Tokens)",
      desc: "Restricted to Cards, Badges, and Action Buttons.",
      tokenEst: "~120 prompt tokens",
    },
    {
      id: "balanced",
      title: "Balanced (Recommended)",
      desc: "Adds Metrics, Tables, Alerts, Test Results, Diagrams, and Carousels.",
      tokenEst: "~260 prompt tokens",
    },
    {
      id: "full",
      title: "Full Power (All 14)",
      desc: "Enables all 14 components including Shadcn Charts, Forms, and Math Plots.",
      tokenEst: "~390 prompt tokens",
    },
    {
      id: "custom",
      title: "Custom Selection",
      desc: "Manually toggle individual components to tailor prompt budget.",
      tokenEst: "Variable",
    },
  ];

  const successRate =
    telemetry.totalInvocations > 0
      ? Math.round((telemetry.successfulRenders / telemetry.totalInvocations) * 100)
      : 100;

  const renderCategoryGroup = (
    title: string,
    categoryKey: ComponentMeta["category"],
    icon: React.ReactNode
  ) => {
    const items = CATALOG_METADATA.filter((c) => c.category === categoryKey);
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
          {icon}
          <span>{title}</span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2.5">
          {items.map((comp) => {
            const isChecked = allowedComponents.includes(comp.name);
            return (
              <label
                key={comp.name}
                className={`flex items-start gap-2.5 p-3 rounded-xl border text-xs cursor-pointer transition-all ${
                  isChecked
                    ? "border-primary/60 bg-primary/5 text-foreground shadow-xs"
                    : "border-border/60 bg-muted/20 text-muted-foreground hover:bg-muted/40"
                }`}
              >
                <input
                  type="checkbox"
                  checked={isChecked}
                  onChange={() => toggleComponent(comp.name)}
                  className="rounded text-primary focus:ring-primary w-4 h-4 cursor-pointer mt-0.5 shrink-0"
                />
                <div className="space-y-0.5 min-w-0">
                  <div className="flex items-center justify-between gap-1">
                    <span className="font-mono font-bold text-[12px]">{comp.name}</span>
                    <span className="text-[10px] text-muted-foreground font-mono">{comp.tokens}</span>
                  </div>
                  <p className="text-[11px] opacity-80 leading-snug line-clamp-2">
                    {comp.desc}
                  </p>
                </div>
              </label>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-6 max-w-4xl text-foreground font-sans">
      {/* Header Banner */}
      <div className="flex items-start justify-between pb-4 border-b border-border/60">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-primary" />
            <h2 className="text-base font-semibold tracking-tight">
              Generative UI & Visual Canvas Architecture
            </h2>
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            Configure autonomous UI generation, catalog component boundaries, and strict LLM token consumption.
          </p>
        </div>

        {/* Master Kill-Switch Toggle */}
        <label className="flex items-center gap-2.5 cursor-pointer bg-muted/40 px-3.5 py-1.5 rounded-xl border border-border/60 hover:bg-muted/60 transition-colors">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="w-4 h-4 rounded text-primary focus:ring-primary cursor-pointer"
          />
          <span className="text-xs font-semibold">
            {enabled ? "Engine Active" : "Engine Disabled"}
          </span>
        </label>
      </div>

      {/* Main Settings Sections */}
      <div className={enabled ? "space-y-6" : "space-y-6 opacity-40 pointer-events-none select-none"}>
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
                  <div className="text-[10px] font-mono text-primary/80 pt-1">
                    {p.tokenEst}
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Allowed Components Catalog Selection (Categorized) */}
        <div className="space-y-4 pt-1">
          <div className="flex items-center justify-between">
            <label className="text-xs font-semibold flex items-center gap-2 text-foreground">
              <Cpu className="h-4 w-4 text-primary" />
              <span>
                Active Component Catalog ({allowedComponents.length} of {ALL_SUPPORTED_COMPONENTS.length} Active)
              </span>
            </label>
            <span className="text-[11px] text-muted-foreground">
              Disabled components are completely removed from system prompt
            </span>
          </div>

          <div className="space-y-5 rounded-2xl border border-border/70 bg-card/30 p-4">
            {renderCategoryGroup("Core Status & KPIs", "kpi", <Activity size={13} className="text-primary" />)}
            {renderCategoryGroup("Structured Data & Testing", "data", <TableIcon size={13} className="text-emerald-500" />)}
            {renderCategoryGroup("Advanced Visuals & Flows", "visuals", <GitFork size={13} className="text-blue-500" />)}
            {renderCategoryGroup("Forms & Scientific", "scientific", <Sigma size={13} className="text-purple-500" />)}
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
            <p className="text-[10px] text-muted-foreground leading-relaxed">
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
            <p className="text-[10px] text-muted-foreground leading-relaxed">
              Maximum retry attempts if syntax validation fails before falling back to standard Markdown.
            </p>
          </div>
        </div>

        {/* Live Telemetry & Observability Panel */}
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
