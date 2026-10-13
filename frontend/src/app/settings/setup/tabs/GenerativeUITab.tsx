// frontend/src/app/settings/setup/tabs/GenerativeUITab.tsx
//
// Rendering settings tab.
//
// This tab configures the Markdown pipeline that renders chat and agent
// messages. It replaces the previous OpenUI-based tab and exposes only the
// four rendering features that survived the OpenUI removal:
//
//   - Mermaid diagrams
//   - LaTeX math
//   - Charts (Recharts-backed ```chart blocks)
//   - Code syntax highlighting
//
// Presets are provided for common configurations. Any manual change shifts
// the selection into "Custom" mode.

"use client";

import React from "react";
import {
  Sparkles,
  GitFork,
  Sigma,
  BarChart3,
  Code2,
  Coins,
  RotateCcw,
  CheckCircle2,
  Activity,
  Clock,
  Layers,
  AlertCircle,
  Table as TableIcon,
  Trash2,
} from "lucide-react";

import {
  PRESET_DEFINITIONS,
  getActivePreset,
  getEffectiveMaxTokens,
  useGenerativeUIStore,
  type RenderingFeatureKey,
  type RenderingPreset,
} from "@/components/generative-ui";

// ---------------------------------------------------------------------------
// Feature metadata
// ---------------------------------------------------------------------------

interface FeatureMeta {
  key: RenderingFeatureKey;
  title: string;
  description: string;
  icon: React.ReactNode;
}

const FEATURE_META: readonly FeatureMeta[] = [
  {
    key: "mermaid",
    title: "Mermaid Diagrams",
    description:
      "Flowcharts, sequence diagrams, Gantt charts, state diagrams, and more.",
    icon: <GitFork size={15} className="text-blue-500" />,
  },
  {
    key: "latex",
    title: "LaTeX Math",
    description:
      "Inline and display math rendered through KaTeX. Supports standard LaTeX primitives.",
    icon: <Sigma size={15} className="text-purple-500" />,
  },
  {
    key: "charts",
    title: "Charts",
    description:
      "Recharts-backed bar, line, area, donut, and pie charts from JSON payloads.",
    icon: <BarChart3 size={15} className="text-emerald-500" />,
  },
  {
    key: "code_highlighting",
    title: "Code Highlighting",
    description:
      "Syntax highlighting for fenced code blocks via Prism.",
    icon: <Code2 size={15} className="text-amber-500" />,
  },
] as const;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function GenerativeUITab(): React.JSX.Element {
  // ---- Read state ----
  const enabled = useGenerativeUIStore((s) => s.enabled);
  const features = useGenerativeUIStore((s) => s.features);
  const limits = useGenerativeUIStore((s) => s.limits);
  const maxRetries = useGenerativeUIStore((s) => s.max_retries);
  const telemetry = useGenerativeUIStore((s) => s.telemetry);
  const activePreset: RenderingPreset = useGenerativeUIStore(getActivePreset);
  const effectiveMaxTokens = useGenerativeUIStore(getEffectiveMaxTokens);

  // ---- Read actions ----
  const setEnabled = useGenerativeUIStore((s) => s.setEnabled);
  const toggleFeature = useGenerativeUIStore((s) => s.toggleFeature);
  const setLimits = useGenerativeUIStore((s) => s.setLimits);
  const setMaxRetries = useGenerativeUIStore((s) => s.setMaxRetries);
  const applyPresetById = useGenerativeUIStore((s) => s.applyPresetById);
  const resetSettings = useGenerativeUIStore((s) => s.resetSettings);
  const resetTelemetry = useGenerativeUIStore((s) => s.resetTelemetry);

  // ---- Derived values ----
  const successRate =
    telemetry.totalRenders > 0
      ? Math.round(
          (telemetry.successfulRenders / telemetry.totalRenders) * 100
        )
      : 100;

  const totalErrors =
    telemetry.mermaidErrors + telemetry.chartErrors + telemetry.latexErrors;

  return (
    <div className="space-y-6 max-w-4xl text-foreground font-sans">
      {/* ----------------------------------------------------------------- */}
      {/* Header banner                                                     */}
      {/* ----------------------------------------------------------------- */}
      <div className="flex items-start justify-between pb-4 border-b border-border/60">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-primary" />
            <h2 className="text-base font-semibold tracking-tight">
              Rendering & Visual Canvas
            </h2>
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            Control which visual formats the assistant may use in its
            responses. Disabled formats fall back to plain Markdown.
          </p>
        </div>

        <label className="flex items-center gap-2.5 cursor-pointer bg-muted/40 px-3.5 py-1.5 rounded-xl border border-border/60 hover:bg-muted/60 transition-colors shrink-0">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="w-4 h-4 rounded text-primary focus:ring-primary cursor-pointer"
          />
          <span className="text-xs font-semibold">
            {enabled ? "Rendering Active" : "Rendering Disabled"}
          </span>
        </label>
      </div>

      {/* ----------------------------------------------------------------- */}
      {/* Presets                                                           */}
      {/* ----------------------------------------------------------------- */}
      <div
        className={
          enabled
            ? "space-y-6"
            : "space-y-6 opacity-40 pointer-events-none select-none"
        }
      >
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <label className="text-xs font-semibold flex items-center gap-2">
              <Layers className="h-4 w-4 text-primary" />
              <span>Preset Profiles</span>
            </label>
            {activePreset === "custom" && (
              <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20 font-bold uppercase tracking-wider">
                Custom
              </span>
            )}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {PRESET_DEFINITIONS.map((preset) => {
              const isSelected = activePreset === preset.id;
              return (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => applyPresetById(preset.id)}
                  className={`p-3.5 rounded-xl border text-left transition-all cursor-pointer flex flex-col justify-between gap-2 ${
                    isSelected
                      ? "border-primary bg-primary/5 shadow-xs"
                      : "border-border/70 bg-card/40 hover:bg-card/70"
                  }`}
                >
                  <div className="flex items-center justify-between w-full">
                    <span className="text-xs font-semibold text-foreground">
                      {preset.title}
                    </span>
                    {isSelected && (
                      <CheckCircle2 className="h-3.5 w-3.5 text-primary shrink-0" />
                    )}
                  </div>
                  <p className="text-[11px] text-muted-foreground leading-relaxed">
                    {preset.description}
                  </p>
                </button>
              );
            })}
          </div>
        </div>

        {/* --------------------------------------------------------------- */}
        {/* Feature toggles                                                 */}
        {/* --------------------------------------------------------------- */}
        <div className="space-y-3 pt-1">
          <label className="text-xs font-semibold flex items-center gap-2">
            <Activity className="h-4 w-4 text-primary" />
            <span>Rendering Features</span>
          </label>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
            {FEATURE_META.map((meta) => {
              const isChecked = features[meta.key];
              return (
                <label
                  key={meta.key}
                  className={`flex items-start gap-2.5 p-3 rounded-xl border text-xs cursor-pointer transition-all ${
                    isChecked
                      ? "border-primary/60 bg-primary/5 text-foreground shadow-xs"
                      : "border-border/60 bg-muted/20 text-muted-foreground hover:bg-muted/40"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={isChecked}
                    onChange={() => toggleFeature(meta.key)}
                    className="rounded text-primary focus:ring-primary w-4 h-4 cursor-pointer mt-0.5 shrink-0"
                  />
                  <div className="space-y-1 min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      {meta.icon}
                      <span className="font-semibold text-[12px]">
                        {meta.title}
                      </span>
                    </div>
                    <p className="text-[11px] opacity-80 leading-snug">
                      {meta.description}
                    </p>
                  </div>
                </label>
              );
            })}
          </div>
        </div>

        {/* --------------------------------------------------------------- */}
        {/* Limits                                                          */}
        {/* --------------------------------------------------------------- */}
        <div className="space-y-3 pt-1">
          <label className="text-xs font-semibold flex items-center gap-2">
            <Coins className="h-4 w-4 text-primary" />
            <span>Format Limits</span>
          </label>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {/* Max output tokens */}
            <LimitSlider
              icon={<Coins className="h-3.5 w-3.5 text-muted-foreground" />}
              label="Max Output Tokens"
              value={limits.max_output_tokens}
              displayValue={`${effectiveMaxTokens} tokens`}
              min={512}
              max={8192}
              step={256}
              hint="Upper bound on completion tokens per response."
              onChange={(v) => setLimits({ max_output_tokens: v })}
            />

            {/* Max mermaid nodes */}
            <LimitSlider
              icon={<GitFork className="h-3.5 w-3.5 text-muted-foreground" />}
              label="Max Mermaid Nodes"
              value={limits.max_mermaid_nodes}
              displayValue={`${limits.max_mermaid_nodes} nodes`}
              min={5}
              max={200}
              step={5}
              hint="Cap on the number of nodes in a single diagram."
              onChange={(v) => setLimits({ max_mermaid_nodes: v })}
            />

            {/* Max chart points */}
            <LimitSlider
              icon={<BarChart3 className="h-3.5 w-3.5 text-muted-foreground" />}
              label="Max Chart Points"
              value={limits.max_chart_points}
              displayValue={`${limits.max_chart_points} points`}
              min={5}
              max={200}
              step={5}
              hint="Maximum data points per chart payload."
              onChange={(v) => setLimits({ max_chart_points: v })}
            />

            {/* Max table rows */}
            <LimitSlider
              icon={<TableIcon className="h-3.5 w-3.5 text-muted-foreground" />}
              label="Max Table Rows"
              value={limits.max_table_rows}
              displayValue={`${limits.max_table_rows} rows`}
              min={5}
              max={300}
              step={5}
              hint="Soft cap for tabular data in a single response."
              onChange={(v) => setLimits({ max_table_rows: v })}
            />

            {/* Max retries */}
            <LimitSlider
              icon={
                <RotateCcw className="h-3.5 w-3.5 text-muted-foreground" />
              }
              label="Auto-Correction Retries"
              value={maxRetries}
              displayValue={`${maxRetries} ${maxRetries === 1 ? "retry" : "retries"}`}
              min={0}
              max={5}
              step={1}
              hint="Retry attempts on parse failure before falling back to plain Markdown."
              onChange={(v) => setMaxRetries(v)}
            />
          </div>
        </div>

        {/* --------------------------------------------------------------- */}
        {/* Telemetry                                                       */}
        {/* --------------------------------------------------------------- */}
        <div className="p-4 rounded-xl border border-border/70 bg-muted/20 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold flex items-center gap-1.5">
              <BarChart3 className="h-4 w-4 text-primary" />
              <span>Session Telemetry</span>
            </span>
            <div className="flex items-center gap-2">
              <span
                className={`text-[10px] font-mono px-2 py-0.5 rounded-full border font-bold ${
                  successRate >= 95
                    ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/20"
                    : successRate >= 80
                    ? "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20"
                    : "bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20"
                }`}
              >
                {successRate}% Success
              </span>
              <button
                type="button"
                onClick={resetTelemetry}
                title="Reset telemetry counters"
                className="p-1 rounded-md text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
              >
                <Trash2 size={12} />
              </button>
            </div>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 pt-1">
            <TelemetryCell
              label="Total Renders"
              value={telemetry.totalRenders}
            />
            <TelemetryCell
              label="Successful"
              value={telemetry.successfulRenders}
              tone="emerald"
            />
            <TelemetryCell
              label="Errors"
              value={totalErrors}
              tone={totalErrors > 0 ? "red" : "muted"}
            />
            <TelemetryCell
              label="Last Latency"
              value={`${telemetry.lastRenderLatencyMs}ms`}
              icon={<Clock size={10} />}
            />
          </div>

          {totalErrors > 0 && (
            <div className="flex items-start gap-2 text-[11px] text-muted-foreground pt-1">
              <AlertCircle size={12} className="shrink-0 mt-0.5" />
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                {telemetry.mermaidErrors > 0 && (
                  <span>
                    Mermaid:{" "}
                    <span className="font-mono text-red-500">
                      {telemetry.mermaidErrors}
                    </span>
                  </span>
                )}
                {telemetry.chartErrors > 0 && (
                  <span>
                    Charts:{" "}
                    <span className="font-mono text-red-500">
                      {telemetry.chartErrors}
                    </span>
                  </span>
                )}
                {telemetry.latexErrors > 0 && (
                  <span>
                    LaTeX:{" "}
                    <span className="font-mono text-red-500">
                      {telemetry.latexErrors}
                    </span>
                  </span>
                )}
              </div>
            </div>
          )}
        </div>

        {/* --------------------------------------------------------------- */}
        {/* Reset actions                                                   */}
        {/* --------------------------------------------------------------- */}
        <div className="flex items-center justify-between pt-2 border-t border-border/40">
          <p className="text-[11px] text-muted-foreground">
            Settings are saved locally and applied to every new message.
          </p>
          <button
            type="button"
            onClick={resetSettings}
            className="text-[11px] font-medium px-3 py-1.5 rounded-md border border-border/60 text-muted-foreground hover:text-foreground hover:bg-muted/40 transition-colors flex items-center gap-1.5"
          >
            <RotateCcw size={12} />
            <span>Reset to Defaults</span>
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

interface LimitSliderProps {
  icon: React.ReactNode;
  label: string;
  value: number;
  displayValue: string;
  min: number;
  max: number;
  step: number;
  hint?: string;
  onChange: (value: number) => void;
}

function LimitSlider({
  icon,
  label,
  value,
  displayValue,
  min,
  max,
  step,
  hint,
  onChange,
}: LimitSliderProps): React.JSX.Element {
  return (
    <div className="p-3.5 rounded-xl border border-border/70 bg-card/40 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-foreground flex items-center gap-1.5 min-w-0">
          {icon}
          <span className="truncate">{label}</span>
        </span>
        <span className="text-xs font-mono font-semibold text-primary shrink-0">
          {displayValue}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full h-1.5 bg-muted rounded-lg appearance-none cursor-pointer accent-primary"
      />
      {hint && (
        <p className="text-[10px] text-muted-foreground leading-relaxed">
          {hint}
        </p>
      )}
    </div>
  );
}

interface TelemetryCellProps {
  label: string;
  value: number | string;
  tone?: "muted" | "emerald" | "red";
  icon?: React.ReactNode;
}

function TelemetryCell({
  label,
  value,
  tone = "muted",
  icon,
}: TelemetryCellProps): React.JSX.Element {
  const valueColor =
    tone === "emerald"
      ? "text-emerald-600 dark:text-emerald-400"
      : tone === "red"
      ? "text-red-600 dark:text-red-400"
      : "text-foreground";

  return (
    <div className="p-2.5 rounded-lg bg-background border border-border/60 text-center">
      <div className="text-[10px] text-muted-foreground flex items-center justify-center gap-1">
        {icon}
        <span>{label}</span>
      </div>
      <div className={`text-sm font-bold font-mono ${valueColor}`}>
        {value}
      </div>
    </div>
  );
}

export default GenerativeUITab;
