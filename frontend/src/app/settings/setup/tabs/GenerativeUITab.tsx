// frontend/src/app/settings/setup/tabs/GenerativeUITab.tsx
//
// Rendering settings tab.
//
// This tab configures the Markdown pipeline that renders chat and agent
// messages. It exposes controls for the four rendering features that
// survived the OpenUI removal:
//
//   - Mermaid diagrams
//   - LaTeX math
//   - Charts (Recharts-backed ```chart blocks)
//   - Code syntax highlighting
//
// Presets are provided for common configurations. Any manual change shifts
// the selection into "Custom" mode.
//
// Layout order: presets -> features -> limits -> telemetry -> KPIs -> charts.

"use client";

import React, { useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
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
  Award,
  Zap,
  Gauge,
  CircleDot,
} from "lucide-react";
import {
  ResponsiveContainer,
  RadialBarChart,
  RadialBar,
  Tooltip,
  PieChart,
  Pie,
  Cell,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
} from "recharts";

import {
  PRESET_DEFINITIONS,
  getActivePreset,
  getEffectiveMaxTokens,
  useGenerativeUIStore,
  type RenderingFeatureKey,
  type RenderingPreset,
} from "@/components/generative-ui";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PIE_COLORS = [
  "#10b981",
  "#3b82f6",
  "#8b5cf6",
  "#f59e0b",
  "#ec4899",
  "#06b6d4",
  "#64748b",
];

const RADIAL_COLORS = ["#10b981", "#3b82f6", "#8b5cf6", "#f59e0b"];

const CHART_TOOLTIP_STYLE = {
  backgroundColor: "rgba(17, 24, 39, 0.95)",
  border: "1px solid rgba(55, 65, 81, 0.8)",
  fontSize: "11px",
  borderRadius: "10px",
  padding: "8px 12px",
  boxShadow: "0 10px 30px rgba(0, 0, 0, 0.4)",
  backdropFilter: "blur(8px)",
} as const;

// ---------------------------------------------------------------------------
// Feature metadata
// ---------------------------------------------------------------------------

interface FeatureMeta {
  key: RenderingFeatureKey;
  title: string;
  description: string;
  icon: React.ReactNode;
  accent: string;
}

const FEATURE_META: readonly FeatureMeta[] = [
  {
    key: "mermaid",
    title: "Mermaid Diagrams",
    description:
      "Flowcharts, sequence diagrams, Gantt charts, state diagrams, and more.",
    icon: <GitFork size={16} className="text-blue-500" />,
    accent: "#3b82f6",
  },
  {
    key: "latex",
    title: "LaTeX Math",
    description:
      "Inline and display math rendered through KaTeX. Supports standard LaTeX primitives.",
    icon: <Sigma size={16} className="text-purple-500" />,
    accent: "#8b5cf6",
  },
  {
    key: "charts",
    title: "Charts",
    description:
      "Recharts-backed bar, line, area, donut, and pie charts from JSON payloads.",
    icon: <BarChart3 size={16} className="text-emerald-500" />,
    accent: "#10b981",
  },
  {
    key: "code_highlighting",
    title: "Code Highlighting",
    description: "Syntax highlighting for fenced code blocks via Prism.",
    icon: <Code2 size={16} className="text-amber-500" />,
    accent: "#f59e0b",
  },
] as const;

// ---------------------------------------------------------------------------
// Motion presets
// ---------------------------------------------------------------------------

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

// ---------------------------------------------------------------------------
// Main Component
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

  const enabledFeatureCount = useMemo(
    () => Object.values(features).filter(Boolean).length,
    [features]
  );

  const radialData = useMemo(() => {
    const reliability = successRate;
    const coverage = (enabledFeatureCount / 4) * 100;
    const errorFree =
      telemetry.totalRenders > 0
        ? Math.max(
            0,
            100 - (totalErrors / Math.max(telemetry.totalRenders, 1)) * 100
          )
        : 100;
    return [
      { name: "Reliability", value: reliability, fill: RADIAL_COLORS[0] },
      { name: "Coverage", value: coverage, fill: RADIAL_COLORS[1] },
      { name: "Stability", value: errorFree, fill: RADIAL_COLORS[2] },
    ];
  }, [successRate, enabledFeatureCount, totalErrors, telemetry.totalRenders]);

  const errorPieData = useMemo(() => {
    const items = [
      { name: "Mermaid", value: telemetry.mermaidErrors, fill: PIE_COLORS[0] },
      { name: "Charts", value: telemetry.chartErrors, fill: PIE_COLORS[1] },
      { name: "LaTeX", value: telemetry.latexErrors, fill: PIE_COLORS[2] },
    ].filter((i) => i.value > 0);
    return items;
  }, [telemetry]);

  const telemetryTrend = useMemo(() => {
    const base = telemetry.totalRenders || 0;
    const points = 7;
    const arr: { idx: string; renders: number; errors: number }[] = [];
    for (let i = 0; i < points; i++) {
      const ratio = (i + 1) / points;
      arr.push({
        idx: `T${i + 1}`,
        renders: Math.round(base * ratio),
        errors: Math.round(totalErrors * ratio),
      });
    }
    return arr;
  }, [telemetry.totalRenders, totalErrors]);

  return (
    <div className="h-full w-full overflow-y-auto bg-background text-foreground font-sans flex flex-col relative">
      {/* Ambient Background */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-40 -left-40 w-[500px] h-[500px] rounded-full bg-primary/5 blur-3xl" />
        <div className="absolute top-1/3 -right-40 w-[400px] h-[400px] rounded-full bg-purple-500/5 blur-3xl" />
        <div className="absolute bottom-0 left-1/3 w-[450px] h-[450px] rounded-full bg-emerald-500/5 blur-3xl" />
      </div>

      {/* Header */}
      <motion.header
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="border-b border-border bg-card/60 backdrop-blur-xl px-6 py-4 sticky top-0 z-20"
      >
        <div className="max-w-7xl mx-auto flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-2.5">
            <div className="p-2 rounded-lg bg-primary/10 border border-primary/20">
              <Sparkles size={16} className="text-primary" />
            </div>
            <div>
              <h1 className="text-sm font-semibold font-heading tracking-wide">
                Rendering & Visual Canvas
              </h1>
              <p className="text-[10px] text-muted-foreground">
                Control the visual formats the assistant may use in responses
              </p>
            </div>
          </div>

          <label
            className={`flex items-center gap-2.5 cursor-pointer px-3.5 py-1.5 rounded-xl border transition-all ${
              enabled
                ? "border-emerald-500/40 bg-emerald-500/10"
                : "border-border/60 bg-muted/40 hover:bg-muted/60"
            }`}
          >
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="w-4 h-4 rounded text-primary focus:ring-primary cursor-pointer"
            />
            <span
              className={`text-xs font-semibold ${
                enabled ? "text-emerald-600 dark:text-emerald-400" : ""
              }`}
            >
              {enabled ? "Rendering Active" : "Rendering Disabled"}
            </span>
            {enabled && (
              <motion.span
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                className="w-2 h-2 rounded-full bg-emerald-500"
                style={{ boxShadow: "0 0 8px rgba(16, 185, 129, 0.8)" }}
              />
            )}
          </label>
        </div>
      </motion.header>

      {/* Main Content */}
      <main className="max-w-7xl w-full mx-auto p-6 space-y-6 flex-1 relative">
        <AnimatePresence>
          {!enabled && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              className="overflow-hidden"
            >
              <div className="flex items-center gap-3 p-4 rounded-xl border border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400 text-xs">
                <AlertCircle size={16} className="shrink-0" />
                <span>
                  Rendering is disabled. All visual formats will fall back to
                  plain Markdown until you re-enable it above.
                </span>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <div className={enabled ? "" : "opacity-50 pointer-events-none select-none"}>
          {/* ========================================================== */}
          {/* 1. Preset Profiles                                         */}
          {/* ========================================================== */}
          <motion.div
            {...FADE_UP}
            transition={{ ...FADE_UP.transition, delay: 0 }}
            className="space-y-3 mb-6"
          >
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
              {PRESET_DEFINITIONS.map((preset, idx) => {
                const isSelected = activePreset === preset.id;
                return (
                  <motion.button
                    key={preset.id}
                    type="button"
                    initial={{ opacity: 0, y: 15 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.4, delay: idx * 0.05 }}
                    whileHover={{ y: -4 }}
                    onClick={() => applyPresetById(preset.id)}
                    className={`group relative p-4 rounded-xl border text-left transition-all cursor-pointer flex flex-col justify-between gap-2.5 overflow-hidden ${
                      isSelected
                        ? "border-primary bg-primary/5 shadow-md"
                        : "border-border/70 bg-card/40 hover:bg-card/70 hover:border-primary/40"
                    }`}
                  >
                    {isSelected && (
                      <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-primary to-transparent" />
                    )}
                    <div className="flex items-center justify-between w-full">
                      <span className="text-xs font-semibold text-foreground">
                        {preset.title}
                      </span>
                      {isSelected ? (
                        <CheckCircle2 className="h-4 w-4 text-primary shrink-0" />
                      ) : (
                        <CircleDot className="h-3.5 w-3.5 text-muted-foreground/40 group-hover:text-primary/60 shrink-0 transition-colors" />
                      )}
                    </div>
                    <p className="text-[11px] text-muted-foreground leading-relaxed">
                      {preset.description}
                    </p>
                  </motion.button>
                );
              })}
            </div>
          </motion.div>

          {/* ========================================================== */}
          {/* 2. Rendering Features                                      */}
          {/* ========================================================== */}
          <motion.div
            {...FADE_UP}
            transition={{ ...FADE_UP.transition, delay: 0.15 }}
            className="space-y-3 mb-6"
          >
            <label className="text-xs font-semibold flex items-center gap-2">
              <Zap className="h-4 w-4 text-primary" />
              <span>Rendering Features</span>
              <span className="text-[10px] font-mono text-muted-foreground ml-auto">
                {enabledFeatureCount} / 4 active
              </span>
            </label>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {FEATURE_META.map((meta, idx) => {
                const isChecked = features[meta.key];
                return (
                  <motion.label
                    key={meta.key}
                    initial={{ opacity: 0, y: 15 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.4, delay: 0.15 + idx * 0.05 }}
                    whileHover={{ y: -2 }}
                    className={`group relative flex items-start gap-3 p-4 rounded-xl border cursor-pointer transition-all overflow-hidden ${
                      isChecked
                        ? "border-primary/60 bg-primary/5 text-foreground shadow-sm"
                        : "border-border/60 bg-muted/20 text-muted-foreground hover:bg-muted/40 hover:border-primary/30"
                    }`}
                  >
                    {isChecked && (
                      <div
                        className="absolute top-0 left-0 right-0 h-[2px] opacity-70"
                        style={{
                          background: `linear-gradient(90deg, transparent 0%, ${meta.accent} 50%, transparent 100%)`,
                        }}
                      />
                    )}
                    <input
                      type="checkbox"
                      checked={isChecked}
                      onChange={() => toggleFeature(meta.key)}
                      className="rounded text-primary focus:ring-primary w-4 h-4 cursor-pointer mt-0.5 shrink-0"
                    />
                    <div className="space-y-1 min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        {meta.icon}
                        <span className="font-semibold text-[12px]">
                          {meta.title}
                        </span>
                      </div>
                      <p className="text-[11px] opacity-80 leading-snug">
                        {meta.description}
                      </p>
                    </div>
                    {isChecked && (
                      <motion.span
                        initial={{ scale: 0 }}
                        animate={{ scale: 1 }}
                        className="w-2 h-2 rounded-full shrink-0 mt-1.5"
                        style={{
                          backgroundColor: meta.accent,
                          boxShadow: `0 0 8px ${meta.accent}80`,
                        }}
                      />
                    )}
                  </motion.label>
                );
              })}
            </div>
          </motion.div>

          {/* ========================================================== */}
          {/* 3. Format Limits                                           */}
          {/* ========================================================== */}
          <motion.div
            {...FADE_UP}
            transition={{ ...FADE_UP.transition, delay: 0.3 }}
            className="space-y-3 mb-6"
          >
            <label className="text-xs font-semibold flex items-center gap-2">
              <Coins className="h-4 w-4 text-primary" />
              <span>Format Limits</span>
            </label>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
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
                delay={0.3}
              />
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
                delay={0.35}
              />
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
                delay={0.4}
              />
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
                delay={0.45}
              />
              <LimitSlider
                icon={<RotateCcw className="h-3.5 w-3.5 text-muted-foreground" />}
                label="Auto-Correction Retries"
                value={maxRetries}
                displayValue={`${maxRetries} ${maxRetries === 1 ? "retry" : "retries"}`}
                min={0}
                max={5}
                step={1}
                hint="Retry attempts on parse failure before falling back to plain Markdown."
                onChange={(v) => setMaxRetries(v)}
                delay={0.5}
              />
            </div>
          </motion.div>

          {/* ========================================================== */}
          {/* 4. Session Telemetry                                       */}
          {/* ========================================================== */}
          <motion.div
            {...FADE_UP}
            transition={{ ...FADE_UP.transition, delay: 0.55 }}
            className="p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden mb-6"
          >
            <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-cyan-500/60 to-transparent" />
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-cyan-500/10 border border-cyan-500/20">
                  <Gauge size={16} className="text-cyan-500" />
                </div>
                <div>
                  <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                    Session Telemetry
                  </h3>
                  <p className="text-[11px] text-muted-foreground">
                    Runtime diagnostics for the rendering pipeline
                  </p>
                </div>
              </div>
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
                  className="p-1.5 rounded-md text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 transition-colors cursor-pointer"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
              <div className="lg:col-span-2 grid grid-cols-2 sm:grid-cols-4 gap-2.5">
                <TelemetryCell
                  label="Total Renders"
                  value={telemetry.totalRenders}
                  tone="primary"
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
                  label="Latency"
                  value={`${telemetry.lastRenderLatencyMs}ms`}
                  tone="cyan"
                  icon={<Clock size={10} />}
                />
              </div>

              <div className="h-32 w-full flex items-center justify-center">
                {errorPieData.length > 0 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={errorPieData}
                        dataKey="value"
                        nameKey="name"
                        cx="50%"
                        cy="50%"
                        outerRadius={48}
                        innerRadius={28}
                        paddingAngle={3}
                        stroke="none"
                      >
                        {errorPieData.map((entry, index) => (
                          <Cell key={`err-cell-${index}`} fill={entry.fill} />
                        ))}
                      </Pie>
                      <Tooltip contentStyle={CHART_TOOLTIP_STYLE} />
                    </PieChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex flex-col items-center gap-2 text-muted-foreground text-[10px]">
                    <CheckCircle2 size={20} className="text-emerald-500 opacity-60" />
                    <span>No errors</span>
                  </div>
                )}
              </div>
            </div>

            {totalErrors > 0 && (
              <div className="flex items-start gap-2 text-[11px] text-muted-foreground pt-2 border-t border-border/60">
                <AlertCircle size={12} className="shrink-0 mt-0.5 text-rose-500" />
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {telemetry.mermaidErrors > 0 && (
                    <span className="flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
                      Mermaid:{" "}
                      <span className="font-mono text-rose-500 font-bold">
                        {telemetry.mermaidErrors}
                      </span>
                    </span>
                  )}
                  {telemetry.chartErrors > 0 && (
                    <span className="flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-blue-500" />
                      Charts:{" "}
                      <span className="font-mono text-rose-500 font-bold">
                        {telemetry.chartErrors}
                      </span>
                    </span>
                  )}
                  {telemetry.latexErrors > 0 && (
                    <span className="flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-violet-500" />
                      LaTeX:{" "}
                      <span className="font-mono text-rose-500 font-bold">
                        {telemetry.latexErrors}
                      </span>
                    </span>
                  )}
                </div>
              </div>
            )}
          </motion.div>

          {/* ========================================================== */}
          {/* 5. KPI Grid                                                */}
          {/* ========================================================== */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <KpiCard
              label="Total Renders"
              value={telemetry.totalRenders}
              icon={<Activity size={18} className="text-primary" />}
              accent="#3b82f6"
              subtitle="Rendering pipeline executions"
              hint={`${enabledFeatureCount} of 4 features active`}
              delay={0.6}
            />
            <KpiCard
              label="Success Rate"
              value={successRate}
              icon={<CheckCircle2 size={18} className="text-emerald-500" />}
              accent="#10b981"
              subtitle={`${telemetry.successfulRenders} successful renders`}
              hint="Target: 95% or above"
              delay={0.65}
              suffix="%"
            />
            <KpiCard
              label="Total Errors"
              value={totalErrors}
              icon={<AlertCircle size={18} className="text-rose-500" />}
              accent="#f43f5e"
              subtitle={`${telemetry.mermaidErrors} mermaid · ${telemetry.chartErrors} charts · ${telemetry.latexErrors} latex`}
              hint="Errors auto-fallback to plain Markdown"
              delay={0.7}
            />
            <KpiCard
              label="Last Latency"
              value={telemetry.lastRenderLatencyMs}
              icon={<Clock size={18} className="text-cyan-500" />}
              accent="#06b6d4"
              subtitle="Most recent render time"
              hint="Includes parse + render pipeline"
              delay={0.75}
              suffix=" ms"
            />
          </div>

          {/* ========================================================== */}
          {/* 6. Charts Row: Telemetry Trend + Health Metrics            */}
          {/* ========================================================== */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
            <motion.div
              {...FADE_UP}
              transition={{ ...FADE_UP.transition, delay: 0.8 }}
              className="lg:col-span-2 p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden"
            >
              <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-primary/60 to-transparent" />
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-primary/10 border border-primary/20">
                  <BarChart3 size={16} className="text-primary" />
                </div>
                <div>
                  <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                    Telemetry Timeline
                  </h3>
                  <p className="text-[11px] text-muted-foreground">
                    Cumulative renders and errors across the session
                  </p>
                </div>
              </div>

              <div className="h-64 w-full pt-2">
                {telemetry.totalRenders > 0 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart
                      data={telemetryTrend}
                      margin={{ top: 10, right: 10, left: -20, bottom: 0 }}
                    >
                      <defs>
                        <linearGradient id="renderGradient" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.5} />
                          <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                        </linearGradient>
                        <linearGradient id="errorGradient" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#f43f5e" stopOpacity={0.4} />
                          <stop offset="95%" stopColor="#f43f5e" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid
                        strokeDasharray="3 3"
                        stroke="rgba(148, 163, 184, 0.1)"
                        vertical={false}
                      />
                      <XAxis
                        dataKey="idx"
                        stroke="#64748b"
                        fontSize={10}
                        tickLine={false}
                        axisLine={false}
                      />
                      <YAxis
                        stroke="#64748b"
                        fontSize={10}
                        tickLine={false}
                        axisLine={false}
                      />
                      <Tooltip contentStyle={CHART_TOOLTIP_STYLE} />
                      <Area
                        type="monotone"
                        dataKey="renders"
                        stroke="#3b82f6"
                        strokeWidth={2}
                        fillOpacity={1}
                        fill="url(#renderGradient)"
                        name="Total Renders"
                      />
                      <Area
                        type="monotone"
                        dataKey="errors"
                        stroke="#f43f5e"
                        strokeWidth={2}
                        fillOpacity={1}
                        fill="url(#errorGradient)"
                        name="Cumulative Errors"
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="h-full flex flex-col items-center justify-center text-muted-foreground text-xs gap-3">
                    <div className="p-4 rounded-full bg-muted/50">
                      <Activity size={28} className="opacity-40" />
                    </div>
                    <span className="font-medium">No rendering activity yet</span>
                    <span className="text-[10px] opacity-70">
                      Telemetry will populate as the assistant renders content
                    </span>
                  </div>
                )}
              </div>
            </motion.div>

            <motion.div
              {...FADE_UP}
              transition={{ ...FADE_UP.transition, delay: 0.85 }}
              className="p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden"
            >
              <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-emerald-500/60 to-transparent" />
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
                  <Award size={16} className="text-emerald-500" />
                </div>
                <div>
                  <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                    Health Metrics
                  </h3>
                  <p className="text-[11px] text-muted-foreground">
                    Composite quality indicators
                  </p>
                </div>
              </div>

              <div className="h-52 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <RadialBarChart
                    cx="50%"
                    cy="50%"
                    innerRadius="35%"
                    outerRadius="95%"
                    barSize={10}
                    data={radialData}
                    startAngle={90}
                    endAngle={-270}
                  >
                    <RadialBar
                      background={{ fill: "rgba(148, 163, 184, 0.08)" }}
                      dataKey="value"
                      cornerRadius={6}
                    />
                    <Tooltip contentStyle={CHART_TOOLTIP_STYLE} />
                  </RadialBarChart>
                </ResponsiveContainer>
              </div>

              <div className="space-y-2 pt-2 border-t border-border/60">
                {radialData.map((item) => (
                  <div
                    key={item.name}
                    className="flex items-center justify-between text-[11px]"
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className="w-2 h-2 rounded-full"
                        style={{
                          backgroundColor: item.fill,
                          boxShadow: `0 0 6px ${item.fill}80`,
                        }}
                      />
                      <span className="text-muted-foreground">{item.name}</span>
                    </div>
                    <span
                      className="font-mono font-semibold"
                      style={{ color: item.fill }}
                    >
                      {item.value.toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </motion.div>
          </div>

          {/* ========================================================== */}
          {/* 7. Reset actions                                           */}
          {/* ========================================================== */}
          <motion.div
            {...FADE_UP}
            transition={{ ...FADE_UP.transition, delay: 0.9 }}
            className="flex items-center justify-between pt-4 border-t border-border/40"
          >
            <p className="text-[11px] text-muted-foreground">
              Settings are saved locally and applied to every new message.
            </p>
            <button
              type="button"
              onClick={resetSettings}
              className="text-[11px] font-medium px-3.5 py-2 rounded-lg border border-border/60 text-muted-foreground hover:text-foreground hover:bg-muted/40 hover:border-primary/40 transition-all flex items-center gap-1.5 cursor-pointer hover:scale-105"
            >
              <RotateCcw size={12} />
              <span>Reset to Defaults</span>
            </button>
          </motion.div>
        </div>
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

interface KpiCardProps {
  label: string;
  value: number | string;
  icon: React.ReactNode;
  accent: string;
  subtitle: string;
  hint?: string;
  delay?: number;
  suffix?: string;
}

function KpiCard({
  label,
  value,
  icon,
  accent,
  subtitle,
  hint,
  delay = 0,
  suffix = "",
}: KpiCardProps): React.JSX.Element {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, delay, ease: [0.16, 1, 0.3, 1] }}
      whileHover={{ y: -4, transition: { duration: 0.2 } }}
      className="group relative p-5 rounded-xl border border-border bg-card overflow-hidden shadow-sm hover:shadow-lg hover:border-primary/30 transition-all"
    >
      <div
        className="absolute top-0 left-0 right-0 h-[2px] opacity-60 group-hover:opacity-100 transition-opacity"
        style={{
          background: `linear-gradient(90deg, transparent 0%, ${accent} 50%, transparent 100%)`,
        }}
      />
      <div
        className="absolute -top-10 -right-10 w-32 h-32 rounded-full opacity-[0.08] group-hover:opacity-[0.15] transition-opacity blur-2xl"
        style={{ backgroundColor: accent }}
      />

      <div className="relative flex items-start justify-between">
        <div className="flex-1 space-y-3 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              {label}
            </span>
          </div>
          <div className="flex items-baseline gap-2 flex-wrap">
            <div
              className="text-2xl lg:text-3xl font-bold font-mono tabular-nums"
              style={{ color: accent }}
            >
              {value}
              {suffix && <span className="text-base ml-0.5">{suffix}</span>}
            </div>
          </div>
          <div className="text-[11px] text-muted-foreground truncate">{subtitle}</div>
          {hint && (
            <div className="text-[10px] text-muted-foreground/70 font-mono truncate">
              {hint}
            </div>
          )}
        </div>
        <div
          className="p-2.5 rounded-lg shrink-0 ml-2"
          style={{
            backgroundColor: `${accent}15`,
            border: `1px solid ${accent}30`,
          }}
        >
          {icon}
        </div>
      </div>
    </motion.div>
  );
}

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
  delay?: number;
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
  delay = 0,
}: LimitSliderProps): React.JSX.Element {
  const percentage = ((value - min) / (max - min)) * 100;

  return (
    <motion.div
      initial={{ opacity: 0, y: 15 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay }}
      className="p-4 rounded-xl border border-border/70 bg-card/40 space-y-3 hover:border-primary/30 transition-colors"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-foreground flex items-center gap-1.5 min-w-0">
          {icon}
          <span className="truncate">{label}</span>
        </span>
        <span className="text-xs font-mono font-semibold text-primary shrink-0 px-2 py-0.5 rounded bg-primary/10 border border-primary/20">
          {displayValue}
        </span>
      </div>

      <div className="relative">
        <div className="absolute inset-0 h-1.5 rounded-full bg-muted top-1/2 -translate-y-1/2 pointer-events-none" />
        <div
          className="absolute h-1.5 rounded-full top-1/2 -translate-y-1/2 pointer-events-none"
          style={{
            width: `${percentage}%`,
            background: "linear-gradient(90deg, #3b82f6, #8b5cf6)",
          }}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="relative w-full h-1.5 bg-transparent rounded-lg appearance-none cursor-pointer accent-primary z-10"
        />
      </div>

      {hint && (
        <p className="text-[10px] text-muted-foreground leading-relaxed">
          {hint}
        </p>
      )}
    </motion.div>
  );
}

interface TelemetryCellProps {
  label: string;
  value: number | string;
  tone?: "muted" | "emerald" | "red" | "primary" | "cyan";
  icon?: React.ReactNode;
}

function TelemetryCell({
  label,
  value,
  tone = "muted",
  icon,
}: TelemetryCellProps): React.JSX.Element {
  const toneClasses: Record<string, string> = {
    muted: "text-foreground",
    emerald: "text-emerald-600 dark:text-emerald-400",
    red: "text-rose-600 dark:text-rose-400",
    primary: "text-primary",
    cyan: "text-cyan-600 dark:text-cyan-400",
  };

  return (
    <div className="p-3 rounded-lg bg-background border border-border/60 text-center hover:border-primary/40 transition-colors">
      <div className="text-[10px] text-muted-foreground flex items-center justify-center gap-1 mb-1">
        {icon}
        <span>{label}</span>
      </div>
      <div className={`text-base font-bold font-mono ${toneClasses[tone]}`}>
        {value}
      </div>
    </div>
  );
}

export default GenerativeUITab;
