// frontend/src/app/settings/search/page.tsx
"use client";

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import Link from "next/link";
import { motion, AnimatePresence, useMotionValue, useSpring, useTransform } from "framer-motion";
import {
  Search,
  CheckCircle2,
  XCircle,
  RefreshCw,
  Activity,
  DollarSign,
  Database,
  ArrowLeft,
  Sliders,
  Play,
  TrendingUp,
  TrendingDown,
  Zap,
  Clock,
  Award,
  BarChart3,
  Globe,
  Sparkles,
  Loader2,
  Trash2,
  Lock,
  Info,
  AlertTriangle,
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
import { showToast, ToastContainer } from "@/components/ui/ToastNotification";

const resolveApiUrl = (endpoint: string): string => {
  if (typeof window !== "undefined") {
    const port = window.location.port;
    if (port === "3088" || port === "3000") {
      return `http://localhost:8088${endpoint}`;
    }
  }
  return endpoint;
};

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
  backgroundColor: "rgba(246, 247, 250, 0.95)",
  border: "1px solid rgba(55, 65, 81, 0.8)",
  fontSize: "11px",
  borderRadius: "10px",
  padding: "8px 12px",
  boxShadow: "0 10px 30px rgba(0, 0, 0, 0.4)",
  backdropFilter: "blur(8px)",
} as const;

const BILLABLE_ENGINES = new Set(["Tavily", "Brave", "Google", "Jina", "Yandex"]);

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
  value: string | number;
  icon: React.ReactNode;
  accent: string;
  subtitle: string;
  delay?: number;
  animatedValue?: {
    value: number;
    decimals?: number;
    prefix?: string;
    suffix?: string;
  };
  trend?: {
    direction: "up" | "down" | "neutral";
    label: string;
  };
}

function KpiCard({
  label,
  value,
  icon,
  accent,
  subtitle,
  delay = 0,
  animatedValue,
  trend,
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
        <div className="flex-1 space-y-3">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              {label}
            </span>
          </div>
          <div className="flex items-baseline gap-2">
            <div className="text-3xl font-bold font-mono tabular-nums" style={{ color: accent }}>
              {animatedValue ? (
                <AnimatedNumber
                  value={animatedValue.value}
                  decimals={animatedValue.decimals ?? 0}
                  prefix={animatedValue.prefix ?? ""}
                  suffix={animatedValue.suffix ?? ""}
                />
              ) : (
                value
              )}
            </div>
            {trend && (
              <span
                className={`flex items-center gap-0.5 text-[10px] font-semibold px-1.5 py-0.5 rounded ${
                  trend.direction === "up"
                    ? "text-emerald-500 bg-emerald-500/10"
                    : trend.direction === "down"
                    ? "text-rose-500 bg-rose-500/10"
                    : "text-muted-foreground bg-muted"
                }`}
              >
                {trend.direction === "up" && <TrendingUp size={10} />}
                {trend.direction === "down" && <TrendingDown size={10} />}
                {trend.label}
              </span>
            )}
          </div>
          <div className="text-[11px] text-muted-foreground">{subtitle}</div>
        </div>
        <div
          className="p-2.5 rounded-lg shrink-0"
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
// Provider Row Component
// ============================================================
interface ProviderRowProps {
  item: {
    provider: string;
    queries_count: number;
    cost_usd: number;
    success_rate_pct: number;
    avg_latency_ms: number;
  };
  index: number;
  maxQueries: number;
}

function ProviderRow({ item, index, maxQueries }: ProviderRowProps) {
  const percentage = maxQueries > 0 ? (item.queries_count / maxQueries) * 100 : 0;
  const color = PIE_COLORS[index % PIE_COLORS.length];

  return (
    <motion.div
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.35, delay: index * 0.05 }}
      className="space-y-1.5"
    >
      <div className="flex items-center justify-between text-xs">
        <div className="flex items-center gap-2">
          <span
            className="w-2 h-2 rounded-full shrink-0"
            style={{ backgroundColor: color, boxShadow: `0 0 8px ${color}80` }}
          />
          <span className="font-medium">{item.provider}</span>
        </div>
        <div className="flex items-center gap-3 font-mono text-[11px]">
          <span className="text-muted-foreground">{item.queries_count} req</span>
          <span className="text-amber-500 font-semibold" title="Estimated cost">
            Est. ${Number(item.cost_usd).toFixed(4)}
          </span>
        </div>
      </div>
      <div className="h-1 rounded-full bg-muted overflow-hidden">
        <motion.div
          initial={{ width: 0 }}
          animate={{ width: `${percentage}%` }}
          transition={{ duration: 0.8, delay: index * 0.05 + 0.2, ease: "easeOut" }}
          className="h-full rounded-full"
          style={{ backgroundColor: color }}
        />
      </div>
    </motion.div>
  );
}

// ============================================================
// Main Page Component
// ============================================================
export default function SearchTelemetryPage() {
  const [loading, setLoading] = useState<boolean>(true);
  const [clearingLedger, setClearingLedger] = useState<boolean>(false);
  const [telemetryPeriod, setTelemetryPeriod] = useState<string>("week");
  const [metrics, setMetrics] = useState<any>(null);

  const [testQuery, setTestQuery] = useState<string>("peldrun AI agents");
  const [testEngine, setTestEngine] = useState<string>("DuckDuckGo");
  const [testingQuery, setTestingQuery] = useState<boolean>(false);
  const [testQueryResult, setTestQueryResult] = useState<any>(null);

  const abortRef = useRef<AbortController | null>(null);

  // ----------------------------------------------------------
  // Data Loading
  // ----------------------------------------------------------
  const loadData = useCallback(async (period: string) => {
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const timeoutId = setTimeout(() => controller.abort(), 6000);

    try {
      setLoading(true);
      const metUrl = resolveApiUrl(`/api/search/metrics?period=${period}`);
      const metRes = await fetch(metUrl, {
        cache: "no-store",
        signal: controller.signal,
      }).catch(() => null);

      if (metRes && metRes.ok) {
        const metData = await metRes.json();
        setMetrics(metData);
      }
    } catch (err) {
      console.error("Error loading metrics:", err);
    } finally {
      clearTimeout(timeoutId);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData(telemetryPeriod);
    return () => abortRef.current?.abort();
  }, [loadData, telemetryPeriod]);

  // ----------------------------------------------------------
  // Clear Local Ledger Handler
  // ----------------------------------------------------------
  const handleClearLedger = async () => {
    if (clearingLedger) return;
    try {
      setClearingLedger(true);
      const res = await fetch(resolveApiUrl("/api/search/metrics"), {
        method: "DELETE",
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        showToast.success(
          "Ledger Cleared",
          data.message || "Local search usage history has been cleared."
        );
        setTestQueryResult(null);
        await loadData(telemetryPeriod);
      } else {
        showToast.error(
          "Clear Failed",
          data.detail || data.message || "Could not clear usage ledger."
        );
      }
    } catch (err: any) {
      showToast.error("Clear Failed", err.message);
    } finally {
      setClearingLedger(false);
    }
  };

  // ----------------------------------------------------------
  // Test Query Handler
  // ----------------------------------------------------------
  const handleRunTestQuery = async () => {
    if (!testQuery.trim() || testingQuery) return;
    try {
      setTestingQuery(true);
      setTestQueryResult(null);
      const queryUrl = resolveApiUrl("/api/search/test-query");
      const res = await fetch(queryUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: testEngine,
          query: testQuery,
          max_results: 3,
        }),
      });
      const data = await res.json();
      setTestQueryResult(data);
      if (data.ok) {
        showToast.success(
          "Search Success",
          `Returned ${data.results_count} snippets in ${data.latency_ms}ms.`
        );
        loadData(telemetryPeriod);
      } else if (data.status === "empty") {
        showToast.error(
          "Zero Matches (HTTP 200)",
          data.error_message || "Provider responded successfully with 0 matching documents."
        );
        loadData(telemetryPeriod);
      } else {
        showToast.error(
          `Search Failed (${data.status || "error"})`,
          data.error_message || "Provider search failed."
        );
        loadData(telemetryPeriod);
      }
    } catch (err: any) {
      showToast.error("Execution Error", err.message);
    } finally {
      setTestingQuery(false);
    }
  };

  // ----------------------------------------------------------
  // Derived Data for Charts
  // ----------------------------------------------------------
  const breakdownData = useMemo(() => metrics?.breakdown ?? [], [metrics]);

  const maxProviderQueries = useMemo(
    () => Math.max(...breakdownData.map((b: any) => b.queries_count), 1),
    [breakdownData]
  );

  const timeseriesData = useMemo(() => metrics?.timeseries ?? [], [metrics]);

  const summary = metrics?.summary ?? {
    total_queries: 0,
    total_cost_usd: 0,
    success_rate_pct: 0,
    failed_queries: 0,
    avg_latency_ms: 0,
    successful_queries: 0,
    total_results: 0,
  };

  const privacyModeActive = Boolean(metrics?.privacy_mode);

  const radialData = useMemo(
    () => [
      {
        name: "Success Rate",
        value: summary.success_rate_pct || 0,
        fill: RADIAL_COLORS[0],
      },
      {
        name: "Avg Latency Score",
        value: Math.max(0, 100 - Math.min(summary.avg_latency_ms / 50, 100)),
        fill: RADIAL_COLORS[1],
      },
      {
        name: "Cost Efficiency",
        value:
          summary.total_queries > 0
            ? Math.max(
                0,
                100 -
                  (summary.total_cost_usd / Math.max(summary.total_queries, 1)) * 20000
              )
            : 100,
        fill: RADIAL_COLORS[2],
      },
    ],
    [summary]
  );

  const engineOptions = [
    "DuckDuckGo",
    "SearXNG",
    "Tavily",
    "Brave",
    "Google",
    "Jina",
    "Yandex",
  ];

  const isSelectedEngineBillable = BILLABLE_ENGINES.has(testEngine);

  // ----------------------------------------------------------
  // Render
  // ----------------------------------------------------------
  return (
    <div className="h-screen w-full overflow-y-auto bg-background text-foreground font-sans flex flex-col relative">
      {/* Ambient Background */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-40 -left-40 w-[500px] h-[500px] rounded-full bg-primary/5 blur-3xl" />
        <div className="absolute top-1/3 -right-40 w-[400px] h-[400px] rounded-full bg-amber-500/5 blur-3xl" />
        <div className="absolute bottom-0 left-1/3 w-[450px] h-[450px] rounded-full bg-cyan-500/5 blur-3xl" />
      </div>

      {/* Header */}
      <motion.header
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="border-b border-border bg-card/60 backdrop-blur-xl px-6 py-4 sticky top-0 z-20"
      >
        <div className="max-w-7xl mx-auto flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-3">
            <Link
              href="/settings"
              className="p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-all cursor-pointer hover:scale-105 active:scale-95"
              title="Return to Main Settings"
            >
              <ArrowLeft size={16} />
            </Link>
            <div className="h-5 w-px bg-border/80" />
            <div className="flex items-center gap-2.5">
              <div className="p-2 rounded-lg bg-primary/10 border border-primary/20">
                <Activity size={16} className="text-primary" />
              </div>
              <div>
                <h1 className="text-sm font-semibold font-heading tracking-wide">
                  Search Intelligence & Usage Ledger
                </h1>
                <p className="text-[10px] text-muted-foreground">
                  Local SQLite telemetry · estimated cost basis · live contract diagnostics
                </p>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <span
              className={`hidden md:flex items-center gap-1.5 text-[10px] font-mono px-2.5 py-1 rounded-md border ${
                privacyModeActive
                  ? "border-purple-500/40 bg-purple-500/10 text-purple-500"
                  : "border-border bg-background/60 text-muted-foreground"
              }`}
              title="Local SQLite ledger query retention policy"
            >
              <Lock size={11} />
              <span>{privacyModeActive ? "Privacy: Hash-Only" : "Privacy: Full Query Text"}</span>
            </span>

            <Button
              onClick={handleClearLedger}
              variant="outline"
              disabled={clearingLedger || summary.total_queries === 0}
              className="h-8 px-3 text-xs gap-1.5 border-rose-500/30 text-rose-500 hover:bg-rose-500/10 cursor-pointer"
              title="Delete all local search usage records from SQLite"
            >
              {clearingLedger ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Trash2 size={12} />
              )}
              <span className="hidden sm:inline">Clear Ledger</span>
            </Button>

            <Link href="/settings">
              <Button
                variant="outline"
                className="h-8 text-xs gap-1.5 cursor-pointer hover:scale-105 transition-transform"
              >
                <Sliders size={12} />
                <span className="hidden sm:inline">Configure Engines</span>
              </Button>
            </Link>
            <Button
              onClick={() => loadData(telemetryPeriod)}
              variant="outline"
              disabled={loading}
              className="h-8 px-3 text-xs gap-1.5 cursor-pointer hover:scale-105 transition-transform"
            >
              <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
              <span className="hidden sm:inline">Refresh</span>
            </Button>
          </div>
        </div>
      </motion.header>

      {/* Main Content */}
      <main className="max-w-7xl w-full mx-auto p-6 space-y-6 flex-1 relative">
        {/* KPI Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <KpiCard
            label="Recorded Requests"
            value={summary.total_queries}
            icon={<Search size={18} className="text-primary" />}
            accent="#3b82f6"
            subtitle="Physical provider requests logged"
            delay={0}
            animatedValue={{ value: summary.total_queries }}
          />
          <KpiCard
            label="Estimated Spend"
            value={summary.total_cost_usd}
            icon={<DollarSign size={18} className="text-amber-500" />}
            accent="#f59e0b"
            subtitle="Local estimate (not a vendor invoice)"
            delay={0.05}
            animatedValue={{
              value: summary.total_cost_usd,
              decimals: 4,
              prefix: "$",
            }}
          />
          <KpiCard
            label="Result Hit Rate"
            value={summary.success_rate_pct}
            icon={<CheckCircle2 size={18} className="text-emerald-500" />}
            accent="#10b981"
            subtitle={`${summary.failed_queries} empty/failed · ${summary.successful_queries ?? 0} with results`}
            delay={0.1}
            animatedValue={{
              value: summary.success_rate_pct,
              decimals: 1,
              suffix: "%",
            }}
          />
          <KpiCard
            label="Average Latency"
            value={summary.avg_latency_ms}
            icon={<Clock size={18} className="text-cyan-500" />}
            accent="#06b6d4"
            subtitle="End-to-end provider response time"
            delay={0.15}
            animatedValue={{
              value: summary.avg_latency_ms,
              decimals: 0,
              suffix: " ms",
            }}
          />
        </div>

        {/* Live Test Playground */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.2 }}
          className="p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden"
        >
          <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-emerald-500/60 to-transparent" />

          <div className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
                <Sparkles size={16} className="text-emerald-500" />
              </div>
              <div>
                <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                  Live Diagnostic Search Playground
                </h3>
                <p className="text-[11px] text-muted-foreground">
                  Executes a real search query against the saved configuration in config.toml
                </p>
              </div>
            </div>

            {isSelectedEngineBillable && (
              <span className="flex items-center gap-1.5 text-[10px] font-mono text-amber-600 dark:text-amber-400 bg-amber-500/10 border border-amber-500/30 px-2.5 py-1 rounded-md">
                <AlertTriangle size={12} />
                <span>Live query against {testEngine} may consume 1 API credit/request</span>
              </span>
            )}
          </div>

          <div className="flex flex-col sm:flex-row gap-3">
            <select
              value={testEngine}
              onChange={(e) => setTestEngine(e.target.value)}
              className="bg-background border border-border rounded-lg px-3 py-2 text-xs text-foreground font-medium focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            >
              {engineOptions.map((eng) => (
                <option key={eng} value={eng}>
                  {eng}
                </option>
              ))}
            </select>

            <input
              type="text"
              value={testQuery}
              onChange={(e) => setTestQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !testingQuery && testQuery.trim()) {
                  handleRunTestQuery();
                }
              }}
              placeholder="Enter search test keyword..."
              className="flex-1 bg-background border border-border rounded-lg px-3 py-2 text-xs text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
            />

            <Button
              onClick={handleRunTestQuery}
              disabled={testingQuery || !testQuery.trim()}
              className="h-9 px-5 text-xs gap-1.5 bg-primary text-primary-foreground font-medium cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
            >
              {testingQuery ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Play size={12} />
              )}
              <span>{testingQuery ? "Executing..." : "Run Live Search"}</span>
            </Button>
          </div>

          <AnimatePresence mode="wait">
            {testQueryResult && (
              <motion.div
                key="result"
                initial={{ opacity: 0, y: 10, height: 0 }}
                animate={{ opacity: 1, y: 0, height: "auto" }}
                exit={{ opacity: 0, y: -10, height: 0 }}
                transition={{ duration: 0.3 }}
                className="mt-3 p-4 rounded-lg border border-border/80 bg-background/60 space-y-3 font-mono text-xs overflow-hidden"
              >
                <div className="flex items-center justify-between text-[11px] pb-2 border-b border-border/60 flex-wrap gap-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span
                      className={`w-2 h-2 rounded-full ${
                        testQueryResult.ok
                          ? "bg-emerald-500"
                          : testQueryResult.status === "empty"
                          ? "bg-amber-500"
                          : "bg-rose-500"
                      }`}
                    />
                    <span className="text-muted-foreground">Provider:</span>
                    <strong>{testQueryResult.provider}</strong>
                    {testQueryResult.status && (
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded border ${
                          testQueryResult.ok
                            ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-500"
                            : testQueryResult.status === "empty"
                            ? "border-amber-500/30 bg-amber-500/10 text-amber-500"
                            : "border-rose-500/30 bg-rose-500/10 text-rose-500"
                        }`}
                      >
                        status: {testQueryResult.status}
                      </span>
                    )}
                  </div>

                  <div className="flex items-center gap-3 text-muted-foreground flex-wrap">
                    <span className="flex items-center gap-1">
                      <Clock size={10} />
                      <strong>{testQueryResult.latency_ms}ms</strong>
                    </span>
                    <span className="flex items-center gap-1">
                      <Search size={10} />
                      <strong>{testQueryResult.results_count}</strong> results
                    </span>
                    {typeof testQueryResult.request_count === "number" && (
                      <span>
                        <strong>{testQueryResult.request_count}</strong> req
                      </span>
                    )}
                    {typeof testQueryResult.estimated_cost_usd === "number" && (
                      <span className="text-amber-500">
                        Est. <strong>${Number(testQueryResult.estimated_cost_usd).toFixed(4)}</strong>
                        {testQueryResult.cost_basis ? ` (${testQueryResult.cost_basis})` : ""}
                      </span>
                    )}
                  </div>
                </div>

                {testQueryResult.results?.length > 0 ? (
                  <div className="space-y-2">
                    {testQueryResult.results.map((res: any, i: number) => (
                      <motion.div
                        key={i}
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        transition={{ delay: i * 0.05 }}
                        className="p-3 rounded-lg bg-card/60 border border-border/40 space-y-1.5 hover:border-primary/40 transition-colors"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <a
                            href={res.url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-primary hover:underline font-semibold block truncate"
                          >
                            {i + 1}. {res.title}
                          </a>
                          {res.source && (
                            <span className="text-[10px] text-muted-foreground shrink-0">
                              {res.source}
                            </span>
                          )}
                        </div>
                        <p className="text-[11px] text-muted-foreground line-clamp-2">
                          {res.snippet}
                        </p>
                      </motion.div>
                    ))}
                  </div>
                ) : testQueryResult.status === "empty" ? (
                  <div className="flex items-center gap-2 text-amber-500 text-xs py-2">
                    <Info size={14} />
                    <span>
                      {testQueryResult.error_message ||
                        "Provider responded with HTTP 200, but zero documents matched this query."}
                    </span>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 text-rose-400 text-xs py-2">
                    <XCircle size={14} />
                    <span>
                      {testQueryResult.error_message ||
                        "Search request failed."}
                    </span>
                  </div>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>

        {/* Charts Row */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Main Telemetry Chart */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.25 }}
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
                    Telemetry Timeline
                  </h3>
                  <p className="text-[11px] text-muted-foreground">
                    Request volume and result hits over time
                  </p>
                </div>
              </div>

              <div className="flex items-center bg-background border border-border rounded-lg p-0.5 text-xs font-medium">
                {(["day", "week", "month", "all"] as const).map((p) => (
                  <button
                    key={p}
                    onClick={() => setTelemetryPeriod(p)}
                    className={`px-3 py-1 rounded-md transition-all cursor-pointer capitalize relative ${
                      telemetryPeriod === p
                        ? "text-primary-foreground font-semibold"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {telemetryPeriod === p && (
                      <motion.div
                        layoutId="period-pill"
                        className="absolute inset-0 bg-primary rounded-md shadow-peldrun-xs"
                        transition={{ type: "spring", stiffness: 400, damping: 30 }}
                      />
                    )}
                    <span className="relative z-10">
                      {p === "all" ? "All" : p}
                    </span>
                  </button>
                ))}
              </div>
            </div>

            <div className="h-72 w-full pt-2">
              {timeseriesData.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart
                    data={timeseriesData}
                    margin={{ top: 10, right: 10, left: -20, bottom: 0 }}
                  >
                    <defs>
                      <linearGradient id="queryGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.5} />
                        <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="successGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#10b981" stopOpacity={0.4} />
                        <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="rgba(148, 163, 184, 0.1)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="date"
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
                      dataKey="queries"
                      stroke="#3b82f6"
                      strokeWidth={2}
                      fillOpacity={1}
                      fill="url(#queryGradient)"
                      name="Total Requests"
                    />
                    <Area
                      type="monotone"
                      dataKey="successful"
                      stroke="#10b981"
                      strokeWidth={2}
                      fillOpacity={1}
                      fill="url(#successGradient)"
                      name="With Results"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-muted-foreground text-xs gap-3">
                  <div className="p-4 rounded-full bg-muted/50">
                    <Database size={28} className="opacity-40" />
                  </div>
                  <span className="font-medium">No telemetry recorded yet</span>
                  <span className="text-[10px] opacity-70">
                    Run a test query above to start collecting data
                  </span>
                </div>
              )}
            </div>
          </motion.div>

          {/* Radial Gauge Panel */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.3 }}
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

        {/* Second Row: Provider Distribution + Cost Breakdown */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Provider Distribution Pie */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.35 }}
            className="p-5 rounded-xl border border-border bg-card shadow-sm space-y-4 relative overflow-hidden"
          >
            <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-purple-500/60 to-transparent" />
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-purple-500/10 border border-purple-500/20">
                <Globe size={16} className="text-purple-500" />
              </div>
              <div>
                <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                  Provider Split
                </h3>
                <p className="text-[11px] text-muted-foreground">
                  Request distribution by engine
                </p>
              </div>
            </div>

            <div className="h-48 w-full">
              {breakdownData.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={breakdownData}
                      dataKey="queries_count"
                      nameKey="provider"
                      cx="50%"
                      cy="50%"
                      outerRadius={70}
                      innerRadius={42}
                      paddingAngle={3}
                      stroke="none"
                    >
                      {breakdownData.map((entry: any, index: number) => (
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
                  <Zap size={24} className="opacity-30" />
                  <span>No dispatch events</span>
                </div>
              )}
            </div>

            <div className="space-y-2.5 pt-2 border-t border-border/60 max-h-40 overflow-y-auto">
              {breakdownData.map((item: any, idx: number) => (
                <ProviderRow
                  key={item.provider}
                  item={item}
                  index={idx}
                  maxQueries={maxProviderQueries}
                />
              ))}
            </div>
          </motion.div>

          {/* Cost Bar Chart */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.4 }}
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
                    Estimated Cost & Latency Distribution
                  </h3>
                  <p className="text-[11px] text-muted-foreground">
                    Estimated spend and average response time by provider
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-3 text-[11px]">
                <div className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full bg-amber-500" />
                  <span className="text-muted-foreground">Est. Cost (USD)</span>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full bg-cyan-500" />
                  <span className="text-muted-foreground">Latency (ms)</span>
                </div>
              </div>
            </div>

            <div className="h-72 w-full pt-2">
              {breakdownData.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={breakdownData}
                    margin={{ top: 10, right: 10, left: -20, bottom: 0 }}
                  >
                    <defs>
                      <linearGradient id="costBar" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#f59e0b" stopOpacity={1} />
                        <stop offset="100%" stopColor="#f59e0b" stopOpacity={0.4} />
                      </linearGradient>
                      <linearGradient id="latencyBar" x1="0" y1="0" x2="0" y2="1">
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
                      dataKey="provider"
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
                      dataKey="cost_usd"
                      fill="url(#costBar)"
                      radius={[6, 6, 0, 0]}
                      name="Est. Cost (USD)"
                      maxBarSize={40}
                    />
                    <Bar
                      dataKey="avg_latency_ms"
                      fill="url(#latencyBar)"
                      radius={[6, 6, 0, 0]}
                      name="Latency (ms)"
                      maxBarSize={40}
                    />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-muted-foreground text-xs gap-2">
                  <DollarSign size={28} className="opacity-30" />
                  <span>No cost data available</span>
                </div>
              )}
            </div>
          </motion.div>
        </div>
      </main>

      <ToastContainer />
    </div>
  );
}
