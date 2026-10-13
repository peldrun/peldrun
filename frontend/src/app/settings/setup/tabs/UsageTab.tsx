"use client";

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { motion, AnimatePresence, useMotionValue, useSpring, useTransform } from "framer-motion";
import {
  Coins,
  DollarSign,
  Layers,
  RefreshCw,
  Plus,
  Trash2,
  Printer,
  Calendar,
  FileDown,
  Activity,
  TrendingUp,
  Clock,
  Cpu,
  Award,
  BarChart3,
  Sparkles,
  Loader2,
  XCircle,
  CheckCircle2,
  Zap,
  Wallet,
  Gauge,
} from "lucide-react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  PieChart,
  Pie,
  Cell,
  RadialBarChart,
  RadialBar,
} from "recharts";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/ToastNotification";
import {
  formatTokenCount,
  formatCostUsd,
  formatLatencyMs,
} from "@/components/chat/UsageBadges";

// ============================================================
// Types
// ============================================================
interface SummaryData {
  llm_call_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
  reasoning_output_tokens: number;
  estimated_tokens: number;
  estimated_percentage: number;
  cost_nano_usd: number;
  total_cost_usd: number;
  avg_latency_ms: number;
  avg_ttft_ms: number;
  retry_count: number;
}

interface TimeseriesItem {
  bucket: string;
  llm_call_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_nano_usd: number;
  total_cost_usd: number;
}

interface BreakdownItem {
  dimension: string;
  key: string;
  llm_call_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_nano_usd: number;
  total_cost_usd: number;
  avg_latency_ms: number;
}

interface PricingItem {
  pricing_id: string;
  provider: string | null;
  model_pattern: string;
  input_price_usd_per_million: number;
  output_price_usd_per_million: number;
  cached_input_price_usd_per_million: number | null;
  priority: number;
  active: boolean;
}

// ============================================================
// Constants
// ============================================================
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

// ============================================================
// Animated Counter Component
// ============================================================
interface AnimatedNumberProps {
  value: number;
  decimals?: number;
  prefix?: string;
  suffix?: string;
  duration?: number;
}

function AnimatedNumber({
  value,
  decimals = 0,
  prefix = "",
  suffix = "",
  duration = 1.2,
}: AnimatedNumberProps) {
  const motionValue = useMotionValue(0);
  const springValue = useSpring(motionValue, {
    duration: duration * 1000,
    bounce: 0,
  });
  const displayValue = useTransform(springValue, (latest) =>
    `${prefix}${latest.toFixed(decimals)}${suffix}`
  );
  const [display, setDisplay] = useState(`${prefix}${(0).toFixed(decimals)}${suffix}`);

  useEffect(() => {
    motionValue.set(value);
  }, [value, motionValue]);

  useEffect(() => {
    const unsubscribe = displayValue.on("change", (latest) => setDisplay(latest));
    return () => unsubscribe();
  }, [displayValue]);

  return <span>{display}</span>;
}

// ============================================================
// KPI Card Component
// ============================================================
interface KpiCardProps {
  label: string;
  icon: React.ReactNode;
  accent: string;
  subtitle: string;
  hint?: string;
  delay?: number;
  animatedValue: {
    value: number;
    decimals?: number;
    prefix?: string;
    suffix?: string;
  };
  badge?: string;
}

function KpiCard({
  label,
  icon,
  accent,
  subtitle,
  hint,
  delay = 0,
  animatedValue,
  badge,
}: KpiCardProps) {
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
              <AnimatedNumber
                value={animatedValue.value}
                decimals={animatedValue.decimals ?? 0}
                prefix={animatedValue.prefix ?? ""}
                suffix={animatedValue.suffix ?? ""}
              />
            </div>
            {badge && (
              <span
                className="text-[10px] font-mono font-bold px-1.5 py-0.5 rounded"
                style={{
                  backgroundColor: `${accent}20`,
                  color: accent,
                }}
              >
                {badge}
              </span>
            )}
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

// ============================================================
// Usage Tab Component
// ============================================================
export function UsageTab() {
  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState<SummaryData | null>(null);
  const [timeseries, setTimeseries] = useState<TimeseriesItem[]>([]);
  const [breakdown, setBreakdown] = useState<BreakdownItem[]>([]);
  const [pricingRules, setPricingRules] = useState<PricingItem[]>([]);

  const [granularity, setGranularity] = useState<"day" | "week" | "month">("day");
  const [breakdownGroup, setBreakdownGroup] = useState<"model" | "provider" | "mode">("model");

  // New Pricing Rule Form
  const [newPattern, setNewPattern] = useState("");
  const [newProvider, setNewProvider] = useState("");
  const [newInputPrice, setNewInputPrice] = useState("0.15");
  const [newOutputPrice, setNewOutputPrice] = useState("0.60");
  const [newCachedPrice, setNewCachedPrice] = useState("0.075");
  const [savingPrice, setSavingPrice] = useState(false);

  const abortRef = useRef<AbortController | null>(null);

  // ----------------------------------------------------------
  // Data Loading
  // ----------------------------------------------------------
  const fetchTelemetry = useCallback(async () => {
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    try {
      const [sumRes, timeRes, breakRes, priceRes] = await Promise.all([
        fetch("/api/telemetry/summary", { signal: controller.signal }),
        fetch(`/api/telemetry/timeseries?granularity=${granularity}`, {
          signal: controller.signal,
        }),
        fetch(`/api/telemetry/breakdown?group_by=${breakdownGroup}`, {
          signal: controller.signal,
        }),
        fetch("/api/telemetry/pricing", { signal: controller.signal }),
      ]);

      if (sumRes.ok) setSummary(await sumRes.json());
      if (timeRes.ok) setTimeseries(await timeRes.json());
      if (breakRes.ok) setBreakdown(await breakRes.json());
      if (priceRes.ok) setPricingRules(await priceRes.json());
    } catch (err: any) {
      if (err.name !== "AbortError") {
        showToast.error("Telemetry Load Failed", err.message);
      }
    } finally {
      setLoading(false);
    }
  }, [granularity, breakdownGroup]);

  useEffect(() => {
    fetchTelemetry();
    return () => abortRef.current?.abort();
  }, [fetchTelemetry]);

  // ----------------------------------------------------------
  // Pricing Rule Handlers
  // ----------------------------------------------------------
  const handleCreatePrice = async () => {
    if (!newPattern.trim()) {
      showToast.warning("Model Pattern Required", "Please enter a model name or pattern.");
      return;
    }
    setSavingPrice(true);
    try {
      const res = await fetch("/api/telemetry/pricing", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model_pattern: newPattern.trim(),
          provider: newProvider.trim() || null,
          input_price_usd_per_million: parseFloat(newInputPrice) || 0.0,
          output_price_usd_per_million: parseFloat(newOutputPrice) || 0.0,
          cached_input_price_usd_per_million: newCachedPrice
            ? parseFloat(newCachedPrice)
            : null,
          priority: 50,
        }),
      });
      if (res.ok) {
        showToast.success("Pricing Rule Saved", newPattern);
        setNewPattern("");
        setNewProvider("");
        fetchTelemetry();
      } else {
        const d = await res.json();
        showToast.error("Failed to Save", d.detail || "Error saving rule.");
      }
    } catch (err: any) {
      showToast.error("Save Error", err.message);
    } finally {
      setSavingPrice(false);
    }
  };

  const handleRetirePrice = async (pricingId: string) => {
    try {
      const res = await fetch(`/api/telemetry/pricing/${pricingId}/retire`, {
        method: "POST",
      });
      if (res.ok) {
        showToast.info(
          "Pricing Rule Retired",
          "Rule deactivated safely without data mutation."
        );
        fetchTelemetry();
      }
    } catch (err: any) {
      showToast.error("Retire Failed", err.message);
    }
  };

  const handleExport = (format: "csv" | "json") => {
    window.open(`/api/telemetry/reports/export?format=${format}`, "_blank");
  };

  const handlePrint = () => {
    window.print();
  };

  // ----------------------------------------------------------
  // Derived Data for Charts
  // ----------------------------------------------------------
  const maxBreakdownTokens = useMemo(
    () => Math.max(...breakdown.map((b) => b.total_tokens), 1),
    [breakdown]
  );

  const maxTimeseriesTokens = useMemo(
    () => Math.max(...timeseries.map((t) => t.total_tokens), 1),
    [timeseries]
  );

  const radialData = useMemo(() => {
    const total = summary?.total_tokens ?? 0;
    if (total === 0) {
      return [
        { name: "Input", value: 0, fill: RADIAL_COLORS[0] },
        { name: "Output", value: 0, fill: RADIAL_COLORS[1] },
        { name: "Cached", value: 0, fill: RADIAL_COLORS[2] },
      ];
    }
    const inputPct = ((summary?.input_tokens ?? 0) / total) * 100;
    const outputPct = ((summary?.output_tokens ?? 0) / total) * 100;
    const cachedPct = Math.min(
      ((summary?.cached_input_tokens ?? 0) / total) * 100,
      100
    );
    return [
      { name: "Input Tokens", value: inputPct, fill: RADIAL_COLORS[0] },
      { name: "Output Tokens", value: outputPct, fill: RADIAL_COLORS[1] },
      { name: "Cached Tokens", value: cachedPct, fill: RADIAL_COLORS[2] },
    ];
  }, [summary]);

  const pieData = useMemo(() => {
    return breakdown.slice(0, 7).map((item, idx) => ({
      name: item.key,
      value: item.total_tokens,
      cost: item.total_cost_usd,
      calls: item.llm_call_count,
      fill: PIE_COLORS[idx % PIE_COLORS.length],
    }));
  }, [breakdown]);

  // ----------------------------------------------------------
  // Render
  // ----------------------------------------------------------
  return (
    <div className="h-full w-full overflow-y-auto bg-background text-foreground font-sans flex flex-col relative">
      {/* Ambient Background */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-40 -left-40 w-[500px] h-[500px] rounded-full bg-primary/5 blur-3xl" />
        <div className="absolute top-1/3 -right-40 w-[400px] h-[400px] rounded-full bg-emerald-500/5 blur-3xl" />
        <div className="absolute bottom-0 left-1/3 w-[450px] h-[450px] rounded-full bg-amber-500/5 blur-3xl" />
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
              <Coins size={16} className="text-primary" />
            </div>
            <div>
              <h1 className="text-sm font-semibold font-heading tracking-wide">
                Token Accounting & Cost Metering
              </h1>
              <p className="text-[10px] text-muted-foreground">
                Durable SQLite ledger · exact unit economics · multi-model telemetry
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <Button
              variant="outline"
              size="sm"
              onClick={fetchTelemetry}
              disabled={loading}
              className="h-8 text-xs gap-1.5 cursor-pointer hover:scale-105 transition-transform"
            >
              <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
              <span className="hidden sm:inline">Refresh</span>
            </Button>

            <Button
              variant="outline"
              size="sm"
              onClick={() => handleExport("csv")}
              className="h-8 text-xs gap-1.5 cursor-pointer hover:scale-105 transition-transform"
              title="Export usage facts as CSV"
            >
              <FileDown size={12} />
              <span className="hidden sm:inline">Export CSV</span>
            </Button>

            <Button
              variant="outline"
              size="sm"
              onClick={handlePrint}
              className="h-8 text-xs gap-1.5 cursor-pointer hover:scale-105 transition-transform"
              title="Generate printer-ready PDF view"
            >
              <Printer size={12} />
              <span className="hidden sm:inline">Print Report</span>
            </Button>
          </div>
        </div>
      </motion.header>

      {/* Main Content */}
      <main className="max-w-7xl w-full mx-auto p-6 space-y-6 flex-1 relative">
        {/* KPI Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <KpiCard
            label="Total Tokens Consumed"
            icon={<Cpu size={18} className="text-primary" />}
            accent="#3b82f6"
            subtitle={`In: ${formatTokenCount(summary?.input_tokens || 0)} · Out: ${formatTokenCount(summary?.output_tokens || 0)}`}
            hint={`${summary?.llm_call_count || 0} LLM invocations`}
            delay={0}
            animatedValue={{ value: summary?.total_tokens || 0 }}
          />

          <KpiCard
            label="Calculated Expenditure"
            icon={<DollarSign size={18} className="text-emerald-500" />}
            accent="#10b981"
            subtitle={`Micro: ${((summary?.cost_nano_usd || 0) / 1000).toLocaleString()} µUSD`}
            hint="Based on pricing catalog"
            delay={0.05}
            animatedValue={{
              value: summary?.total_cost_usd || 0,
              decimals: 4,
              prefix: "$",
            }}
            badge="USD"
          />

          <KpiCard
            label="Cache & Reasoning"
            icon={<Zap size={18} className="text-blue-500" />}
            accent="#3b82f6"
            subtitle={`Reasoning: ${formatTokenCount(summary?.reasoning_output_tokens || 0)} tok`}
            hint="Prompt cache savings applied"
            delay={0.1}
            animatedValue={{ value: summary?.cached_input_tokens || 0 }}
            badge="CACHED"
          />

          <KpiCard
            label="Latency & Accuracy"
            icon={<Gauge size={18} className="text-amber-500" />}
            accent="#f59e0b"
            subtitle={`TTFT: ${formatLatencyMs(summary?.avg_ttft_ms || 0)}`}
            hint={`${summary?.estimated_percentage || 0}% estimated via tokenizer fallback`}
            delay={0.15}
            animatedValue={{
              value: summary?.avg_latency_ms || 0,
              decimals: 0,
              suffix: " ms",
            }}
          />
        </div>

        {/* Charts Row: Timeseries + Radial Composition */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Timeseries Area Chart */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.2 }}
            className="lg:col-span-2 p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden"
          >
            <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-primary/60 to-transparent" />
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-primary/10 border border-primary/20">
                  <BarChart3 size={16} className="text-primary" />
                </div>
                <div>
                  <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                    Consumption Trends
                  </h3>
                  <p className="text-[11px] text-muted-foreground">
                    Token usage and cost over time
                  </p>
                </div>
              </div>

              <div className="flex items-center bg-background border border-border rounded-lg p-0.5 text-xs font-medium">
                {(["day", "week", "month"] as const).map((g) => (
                  <button
                    key={g}
                    onClick={() => setGranularity(g)}
                    className={`px-3 py-1 rounded-md transition-all cursor-pointer relative ${
                      granularity === g
                        ? "text-primary-foreground font-semibold"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {granularity === g && (
                      <motion.div
                        layoutId="granularity-pill"
                        className="absolute inset-0 bg-primary rounded-md shadow-peldrun-xs"
                        transition={{ type: "spring", stiffness: 400, damping: 30 }}
                      />
                    )}
                    <span className="relative z-10 uppercase">{g}</span>
                  </button>
                ))}
              </div>
            </div>

            <div className="h-72 w-full pt-2">
              {timeseries.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart
                    data={timeseries}
                    margin={{ top: 10, right: 10, left: -20, bottom: 0 }}
                  >
                    <defs>
                      <linearGradient id="tokenGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.5} />
                        <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="inputGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#10b981" stopOpacity={0.4} />
                        <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="outputGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#8b5cf6" stopOpacity={0.4} />
                        <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="rgba(148, 163, 184, 0.1)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="bucket"
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
                      dataKey="total_tokens"
                      stroke="#3b82f6"
                      strokeWidth={2}
                      fillOpacity={1}
                      fill="url(#tokenGradient)"
                      name="Total Tokens"
                    />
                    <Area
                      type="monotone"
                      dataKey="input_tokens"
                      stroke="#10b981"
                      strokeWidth={2}
                      fillOpacity={1}
                      fill="url(#inputGradient)"
                      name="Input Tokens"
                    />
                    <Area
                      type="monotone"
                      dataKey="output_tokens"
                      stroke="#8b5cf6"
                      strokeWidth={2}
                      fillOpacity={1}
                      fill="url(#outputGradient)"
                      name="Output Tokens"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-muted-foreground text-xs gap-3">
                  <div className="p-4 rounded-full bg-muted/50">
                    <Calendar size={28} className="opacity-40" />
                  </div>
                  <span className="font-medium">No timeseries records found</span>
                  <span className="text-[10px] opacity-70">
                    Try a different granularity or run some LLM calls
                  </span>
                </div>
              )}
            </div>
          </motion.div>

          {/* Radial Composition Panel */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.25 }}
            className="p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden"
          >
            <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-emerald-500/60 to-transparent" />
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
                <Award size={16} className="text-emerald-500" />
              </div>
              <div>
                <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                  Token Composition
                </h3>
                <p className="text-[11px] text-muted-foreground">
                  Input · output · cached breakdown
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

        {/* Second Row: Breakdown Pie + Breakdown Table */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Breakdown Pie */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.3 }}
            className="p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden"
          >
            <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-purple-500/60 to-transparent" />
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-purple-500/10 border border-purple-500/20">
                  <Layers size={16} className="text-purple-500" />
                </div>
                <div>
                  <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                    Distribution
                  </h3>
                  <p className="text-[11px] text-muted-foreground">
                    Grouped by {breakdownGroup}
                  </p>
                </div>
              </div>
            </div>

            <div className="flex items-center bg-background border border-border rounded-lg p-0.5 text-[10px] font-medium w-fit">
              {(["model", "provider", "mode"] as const).map((b) => (
                <button
                  key={b}
                  onClick={() => setBreakdownGroup(b)}
                  className={`px-2 py-0.5 rounded-md transition-all cursor-pointer relative ${
                    breakdownGroup === b
                      ? "text-primary-foreground font-semibold"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {breakdownGroup === b && (
                    <motion.div
                      layoutId="breakdown-pill"
                      className="absolute inset-0 bg-primary rounded-md"
                      transition={{ type: "spring", stiffness: 400, damping: 30 }}
                    />
                  )}
                  <span className="relative z-10 uppercase">{b}</span>
                </button>
              ))}
            </div>

            <div className="h-48 w-full">
              {pieData.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={pieData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="50%"
                      outerRadius={70}
                      innerRadius={42}
                      paddingAngle={3}
                      stroke="none"
                    >
                      {pieData.map((entry, index) => (
                        <Cell
                          key={`cell-${index}`}
                          fill={PIE_COLORS[index % PIE_COLORS.length]}
                        />
                      ))}
                    </Pie>
                    <Tooltip contentStyle={CHART_TOOLTIP_STYLE} />
                  </PieChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-muted-foreground text-xs gap-2">
                  <Layers size={24} className="opacity-30" />
                  <span>No breakdown data</span>
                </div>
              )}
            </div>

            <div className="space-y-2 pt-2 border-t border-border/60 max-h-40 overflow-y-auto">
              {pieData.map((item, idx) => {
                const percentage =
                  maxBreakdownTokens > 0 ? (item.value / maxBreakdownTokens) * 100 : 0;
                return (
                  <motion.div
                    key={item.name}
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ duration: 0.35, delay: idx * 0.05 }}
                    className="space-y-1.5"
                  >
                    <div className="flex items-center justify-between text-xs gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <span
                          className="w-2 h-2 rounded-full shrink-0"
                          style={{
                            backgroundColor: item.fill,
                            boxShadow: `0 0 8px ${item.fill}80`,
                          }}
                        />
                        <span className="font-medium truncate" title={item.name}>
                          {item.name}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 font-mono text-[10px] shrink-0">
                        <span className="text-muted-foreground">
                          {item.calls}c
                        </span>
                        <span className="text-amber-500 font-semibold">
                          ${Number(item.cost).toFixed(3)}
                        </span>
                      </div>
                    </div>
                    <div className="h-1 rounded-full bg-muted overflow-hidden">
                      <motion.div
                        initial={{ width: 0 }}
                        animate={{ width: `${percentage}%` }}
                        transition={{
                          duration: 0.8,
                          delay: idx * 0.05 + 0.2,
                          ease: "easeOut",
                        }}
                        className="h-full rounded-full"
                        style={{ backgroundColor: item.fill }}
                      />
                    </div>
                  </motion.div>
                );
              })}
            </div>
          </motion.div>

          {/* Breakdown Bar Chart */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.35 }}
            className="lg:col-span-2 p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden"
          >
            <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-amber-500/60 to-transparent" />
            <div className="flex items-center justify-between flex-wrap gap-3">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-amber-500/10 border border-amber-500/20">
                  <DollarSign size={16} className="text-amber-500" />
                </div>
                <div>
                  <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                    Cost vs Volume
                  </h3>
                  <p className="text-[11px] text-muted-foreground">
                    Tokens consumed and cost by {breakdownGroup}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-3 text-[11px]">
                <div className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full bg-amber-500" />
                  <span className="text-muted-foreground">Cost (USD)</span>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full bg-cyan-500" />
                  <span className="text-muted-foreground">Tokens</span>
                </div>
              </div>
            </div>

            <div className="h-72 w-full pt-2">
              {breakdown.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={breakdown}
                    margin={{ top: 10, right: 10, left: -20, bottom: 0 }}
                  >
                    <defs>
                      <linearGradient id="costBarUsage" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#f59e0b" stopOpacity={1} />
                        <stop offset="100%" stopColor="#f59e0b" stopOpacity={0.4} />
                      </linearGradient>
                      <linearGradient id="tokenBarUsage" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#06b6d4" stopOpacity={1} />
                        <stop offset="100%" stopColor="#06b6d4" stopOpacity={0.4} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="rgba(148, 163, 184, 0.1)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="key"
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
                    <Tooltip
                      contentStyle={CHART_TOOLTIP_STYLE}
                      cursor={{ fill: "rgba(148, 163, 184, 0.05)" }}
                    />
                    <Bar
                      dataKey="total_cost_usd"
                      fill="url(#costBarUsage)"
                      radius={[6, 6, 0, 0]}
                      name="Cost (USD)"
                      maxBarSize={40}
                    />
                    <Bar
                      dataKey="total_tokens"
                      fill="url(#tokenBarUsage)"
                      radius={[6, 6, 0, 0]}
                      name="Total Tokens"
                      maxBarSize={40}
                    />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-muted-foreground text-xs gap-2">
                  <BarChart3 size={28} className="opacity-30" />
                  <span>No breakdown metrics available</span>
                </div>
              )}
            </div>
          </motion.div>
        </div>

        {/* Pricing Catalog */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.4 }}
          className="p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden"
        >
          <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-emerald-500/60 to-transparent" />

          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
              <Sparkles size={16} className="text-emerald-500" />
            </div>
            <div>
              <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                Dynamic Unit Economics & Model Pricing Catalog
              </h3>
              <p className="text-[11px] text-muted-foreground">
                Set custom input and output rates per 1M tokens. Local engines remain $0.00.
              </p>
            </div>
          </div>

          {/* Add Pricing Rule Form */}
          <div className="p-4 rounded-lg border border-border/60 bg-background/60 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
            <div>
              <label className="text-[10px] font-semibold text-muted-foreground block mb-1 uppercase tracking-wide">
                Model Pattern
              </label>
              <input
                type="text"
                value={newPattern}
                onChange={(e) => setNewPattern(e.target.value)}
                placeholder="e.g. gpt-4o*"
                className="w-full bg-card border border-border rounded-lg px-3 py-1.5 text-xs text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>

            <div>
              <label className="text-[10px] font-semibold text-muted-foreground block mb-1 uppercase tracking-wide">
                Provider (Optional)
              </label>
              <input
                type="text"
                value={newProvider}
                onChange={(e) => setNewProvider(e.target.value)}
                placeholder="e.g. openai"
                className="w-full bg-card border border-border rounded-lg px-3 py-1.5 text-xs text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>

            <div>
              <label className="text-[10px] font-semibold text-muted-foreground block mb-1 uppercase tracking-wide">
                Input $/1M
              </label>
              <input
                type="number"
                step="0.01"
                value={newInputPrice}
                onChange={(e) => setNewInputPrice(e.target.value)}
                className="w-full bg-card border border-border rounded-lg px-3 py-1.5 text-xs text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>

            <div>
              <label className="text-[10px] font-semibold text-muted-foreground block mb-1 uppercase tracking-wide">
                Output $/1M
              </label>
              <input
                type="number"
                step="0.01"
                value={newOutputPrice}
                onChange={(e) => setNewOutputPrice(e.target.value)}
                className="w-full bg-card border border-border rounded-lg px-3 py-1.5 text-xs text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
              />
            </div>

            <div className="flex items-end">
              <Button
                type="button"
                size="sm"
                onClick={handleCreatePrice}
                disabled={savingPrice || !newPattern.trim()}
                className="w-full h-8 text-xs gap-1.5 bg-primary text-primary-foreground font-medium cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
              >
                {savingPrice ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <Plus size={12} />
                )}
                <span>{savingPrice ? "Saving..." : "Add Rate"}</span>
              </Button>
            </div>
          </div>

          {/* Existing Rules Table */}
          <div className="overflow-x-auto rounded-lg border border-border/40">
            <table className="w-full text-left text-xs font-mono">
              <thead className="bg-muted/30">
                <tr className="border-b border-border/60 text-muted-foreground text-[10px] uppercase">
                  <th className="py-2.5 px-4 font-semibold">Model Pattern</th>
                  <th className="py-2.5 px-4 font-semibold">Provider</th>
                  <th className="py-2.5 px-4 font-semibold">Input / 1M</th>
                  <th className="py-2.5 px-4 font-semibold">Output / 1M</th>
                  <th className="py-2.5 px-4 font-semibold">Cached / 1M</th>
                  <th className="py-2.5 px-4 font-semibold text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {pricingRules.length === 0 ? (
                  <tr>
                    <td
                      colSpan={6}
                      className="py-8 text-center text-muted-foreground text-xs font-sans"
                    >
                      No pricing rules configured yet.
                    </td>
                  </tr>
                ) : (
                  pricingRules.map((rule, idx) => (
                    <motion.tr
                      key={rule.pricing_id}
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      transition={{ delay: idx * 0.03 }}
                      className="hover:bg-muted/30 transition-colors"
                    >
                      <td className="py-2.5 px-4 font-bold text-foreground">
                        {rule.model_pattern}
                      </td>
                      <td className="py-2.5 px-4 text-muted-foreground">
                        {rule.provider || "Any (Global)"}
                      </td>
                      <td className="py-2.5 px-4 text-emerald-500">
                        ${rule.input_price_usd_per_million.toFixed(4)}
                      </td>
                      <td className="py-2.5 px-4 text-violet-500">
                        ${rule.output_price_usd_per_million.toFixed(4)}
                      </td>
                      <td className="py-2.5 px-4 text-blue-500">
                        {rule.cached_input_price_usd_per_million !== null
                          ? `$${rule.cached_input_price_usd_per_million.toFixed(4)}`
                          : "—"}
                      </td>
                      <td className="py-2.5 px-4 text-right">
                        {rule.pricing_id.startsWith("prc_seed_") ? (
                          <span className="text-[10px] text-muted-foreground/60 italic font-sans">
                            Default
                          </span>
                        ) : (
                          <button
                            type="button"
                            onClick={() => handleRetirePrice(rule.pricing_id)}
                            className="p-1.5 rounded text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 cursor-pointer transition-all"
                            title="Soft-retire pricing rule"
                          >
                            <Trash2 size={12} />
                          </button>
                        )}
                      </td>
                    </motion.tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </motion.div>
      </main>
    </div>
  );
}

export default UsageTab;
