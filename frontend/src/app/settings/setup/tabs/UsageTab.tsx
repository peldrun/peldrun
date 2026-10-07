"use client";

import React, { useState, useEffect } from "react";
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
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/ToastNotification";
import {
  formatTokenCount,
  formatCostUsd,
  formatLatencyMs,
} from "@/components/chat/UsageBadges";

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

  const fetchTelemetry = async () => {
    setLoading(true);
    try {
      const [sumRes, timeRes, breakRes, priceRes] = await Promise.all([
        fetch("/api/telemetry/summary"),
        fetch(`/api/telemetry/timeseries?granularity=${granularity}`),
        fetch(`/api/telemetry/breakdown?group_by=${breakdownGroup}`),
        fetch("/api/telemetry/pricing"),
      ]);

      if (sumRes.ok) setSummary(await sumRes.json());
      if (timeRes.ok) setTimeseries(await timeRes.json());
      if (breakRes.ok) setBreakdown(await breakRes.json());
      if (priceRes.ok) setPricingRules(await priceRes.json());
    } catch (err: any) {
      showToast.error("Telemetry Load Failed", err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTelemetry();
  }, [granularity, breakdownGroup]);

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
          cached_input_price_usd_per_million: newCachedPrice ? parseFloat(newCachedPrice) : null,
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
      const res = await fetch(`/api/telemetry/pricing/${pricingId}/retire`, { method: "POST" });
      if (res.ok) {
        showToast.info("Pricing Rule Retired", "Rule deactivated safely without data mutation.");
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

  return (
    <div className="usage-tab w-full space-y-6 max-auto font-sans pb-8">
      {/* Header & Controls */}
      <div className="border-b border-border w-full pb-4 pt-4 bg-custom flex items-center justify-between px-4">
        <div>
          <div className="flex items-center gap-2">
            <Coins size={16} className="text-primary" />
            <h2 className="text-sm font-semibold font-heading text-foreground uppercase tracking-wide">
              Token Accounting & Cost Metering
            </h2>
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Durable SQLite ledger, exact unit economics, and multi-model consumption telemetry.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={fetchTelemetry}
            disabled={loading}
            className="h-7 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs"
          >
            <RefreshCw size={11} className={`mr-1 ${loading ? "animate-spin" : ""}`} />
            <span>Refresh</span>
          </Button>

          <Button
            variant="outline"
            size="sm"
            onClick={() => handleExport("csv")}
            className="h-7 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs"
            title="Export usage facts as CSV"
          >
            <FileDown size={11} className="mr-1" />
            <span>Export CSV</span>
          </Button>

          <Button
            variant="outline"
            size="sm"
            onClick={handlePrint}
            className="h-7 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs"
            title="Generate printer-ready PDF view"
          >
            <Printer size={11} className="mr-1" />
            <span>Print Report</span>
          </Button>
        </div>
      </div>

      {/* 1. Top KPI Summary Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 px-4">
        <div className="p-4 rounded-md border border-border bg-card shadow-xs space-y-1">
          <span className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider block">
            Total Tokens Consumed
          </span>
          <div className="flex items-baseline justify-between">
            <span className="text-2xl font-bold font-mono text-foreground">
              {formatTokenCount(summary?.total_tokens || 0)}
            </span>
            <span className="text-[11px] font-mono text-muted-foreground">
              {summary?.llm_call_count || 0} calls
            </span>
          </div>
          <span className="text-[10px] text-muted-foreground font-mono block">
            In: {formatTokenCount(summary?.input_tokens || 0)} | Out: {formatTokenCount(summary?.output_tokens || 0)}
          </span>
        </div>

        <div className="p-4 rounded-md border border-border bg-card shadow-xs space-y-1">
          <span className="text-[10px] font-bold text-emerald-600 dark:text-emerald-400 uppercase tracking-wider block">
            Calculated Expenditure
          </span>
          <div className="flex items-baseline justify-between">
            <span className="text-2xl font-bold font-mono text-foreground">
              {formatCostUsd(summary?.total_cost_usd || 0)}
            </span>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-600 font-mono font-bold">
              USD
            </span>
          </div>
          <span className="text-[10px] text-muted-foreground font-mono block">
            Micro: {((summary?.cost_nano_usd || 0) / 1000).toLocaleString()} µUSD
          </span>
        </div>

        <div className="p-4 rounded-md border border-border bg-card shadow-xs space-y-1">
          <span className="text-[10px] font-bold text-blue-600 dark:text-blue-400 uppercase tracking-wider block">
            Prompt Cache & Reasoning
          </span>
          <div className="flex items-baseline justify-between">
            <span className="text-2xl font-bold font-mono text-foreground">
              {formatTokenCount(summary?.cached_input_tokens || 0)}
            </span>
            <span className="text-[10px] text-blue-500 font-mono font-bold">Cached</span>
          </div>
          <span className="text-[10px] text-violet-500 font-mono block">
            Reasoning: {formatTokenCount(summary?.reasoning_output_tokens || 0)} tok
          </span>
        </div>

        <div className="p-4 rounded-md border border-border bg-card shadow-xs space-y-1">
          <span className="text-[10px] font-bold text-amber-600 dark:text-amber-400 uppercase tracking-wider block">
            Latency & Accuracy
          </span>
          <div className="flex items-baseline justify-between">
            <span className="text-2xl font-bold font-mono text-foreground">
              {formatLatencyMs(summary?.avg_latency_ms || 0)}
            </span>
            <span className="text-[10px] text-muted-foreground font-mono">
              TTFT: {formatLatencyMs(summary?.avg_ttft_ms || 0)}
            </span>
          </div>
          <span className="text-[10px] text-muted-foreground font-sans block">
            {summary?.estimated_percentage || 0}% estimated via tokenizer fallback
          </span>
        </div>
      </div>

      {/* 2. Timeline Analytics & Dimension Breakdown */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 px-4">
        {/* Timeline Table */}
        <div className="p-4 rounded-md border border-border bg-card shadow-xs space-y-3">
          <div className="flex items-center justify-between border-b border-border/60 pb-2">
            <div className="flex items-center gap-2">
              <Calendar size={14} className="text-primary" />
              <span className="text-xs font-bold text-foreground uppercase tracking-wider">
                Time-Series Trends
              </span>
            </div>
            <div className="flex items-center gap-1 bg-background p-0.5 rounded border border-border">
              {(["day", "week", "month"] as const).map((g) => (
                <button
                  key={g}
                  type="button"
                  onClick={() => setGranularity(g)}
                  className={`px-2 py-0.5 text-[10px] font-medium rounded transition-all cursor-pointer ${
                    granularity === g
                      ? "bg-primary text-primary-foreground font-bold"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {g.toUpperCase()}
                </button>
              ))}
            </div>
          </div>

          <div className="max-h-60 overflow-y-auto divide-y divide-border/50 text-xs font-mono">
            {timeseries.length === 0 ? (
              <div className="py-8 text-center text-muted-foreground text-xs font-sans">
                No timeseries records found for this period.
              </div>
            ) : (
              timeseries.map((item) => (
                <div key={item.bucket} className="py-2 flex items-center justify-between">
                  <div>
                    <span className="font-bold text-foreground">{item.bucket}</span>
                    <span className="text-[10px] text-muted-foreground ml-2">
                      ({item.llm_call_count} calls)
                    </span>
                  </div>
                  <div className="flex items-center gap-4 text-right">
                    <span>{formatTokenCount(item.total_tokens)} tok</span>
                    <span className="text-emerald-600 font-bold w-16">
                      {formatCostUsd(item.total_cost_usd)}
                    </span>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Dimension Breakdown Table */}
        <div className="p-4 rounded-md border border-border bg-card shadow-xs space-y-3">
          <div className="flex items-center justify-between border-b border-border/60 pb-2">
            <div className="flex items-center gap-2">
              <Layers size={14} className="text-primary" />
              <span className="text-xs font-bold text-foreground uppercase tracking-wider">
                Consumption Breakdown
              </span>
            </div>
            <div className="flex items-center gap-1 bg-background p-0.5 rounded border border-border">
              {(["model", "provider", "mode"] as const).map((b) => (
                <button
                  key={b}
                  type="button"
                  onClick={() => setBreakdownGroup(b)}
                  className={`px-2 py-0.5 text-[10px] font-medium rounded transition-all cursor-pointer ${
                    breakdownGroup === b
                      ? "bg-primary text-primary-foreground font-bold"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {b.toUpperCase()}
                </button>
              ))}
            </div>
          </div>

          <div className="max-h-60 overflow-y-auto divide-y divide-border/50 text-xs font-mono">
            {breakdown.length === 0 ? (
              <div className="py-8 text-center text-muted-foreground text-xs font-sans">
                No breakdown metrics available.
              </div>
            ) : (
              breakdown.map((item) => (
                <div key={item.key} className="py-2 flex items-center justify-between">
                  <div className="truncate max-w-[200px]" title={item.key}>
                    <span className="font-bold text-foreground">{item.key}</span>
                    <span className="text-[10px] text-muted-foreground block font-sans">
                      {item.llm_call_count} invocations
                    </span>
                  </div>
                  <div className="flex items-center gap-4 text-right">
                    <span>{formatTokenCount(item.total_tokens)} tok</span>
                    <span className="text-emerald-600 font-bold w-16">
                      {formatCostUsd(item.total_cost_usd)}
                    </span>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* 3. Model Pricing Catalog & Editor */}
      <div className="p-4 mx-4 rounded-md border border-border bg-card shadow-xs space-y-4">
        <div className="flex items-center justify-between border-b border-border/60 pb-2.5">
          <div className="flex items-center gap-2">
            <DollarSign size={16} className="text-primary" />
            <div>
              <span className="text-xs font-bold text-foreground uppercase tracking-wider block">
                Dynamic Unit Economics & Model Pricing Catalog
              </span>
              <span className="text-[10px] text-muted-foreground">
                Set custom input and output rates per 1M tokens. Local engines remain $0.00.
              </span>
            </div>
          </div>
        </div>

        {/* Add Pricing Rule Inline Form */}
        <div className="p-3 rounded bg-background border border-border grid grid-cols-1 sm:grid-cols-5 gap-2 text-xs">
          <div>
            <label className="text-[10px] font-medium text-muted-foreground block mb-0.5">
              Model Pattern (e.g. gpt-4o*)
            </label>
            <input
              type="text"
              value={newPattern}
              onChange={(e) => setNewPattern(e.target.value)}
              placeholder="Model pattern..."
              className="w-full bg-card border border-border rounded px-2 py-1 text-xs text-foreground font-mono"
            />
          </div>

          <div>
            <label className="text-[10px] font-medium text-muted-foreground block mb-0.5">
              Provider (Optional)
            </label>
            <input
              type="text"
              value={newProvider}
              onChange={(e) => setNewProvider(e.target.value)}
              placeholder="e.g. openai, deepseek"
              className="w-full bg-card border border-border rounded px-2 py-1 text-xs text-foreground font-mono"
            />
          </div>

          <div>
            <label className="text-[10px] font-medium text-muted-foreground block mb-0.5">
              Input $/1M
            </label>
            <input
              type="number"
              step="0.01"
              value={newInputPrice}
              onChange={(e) => setNewInputPrice(e.target.value)}
              className="w-full bg-card border border-border rounded px-2 py-1 text-xs text-foreground font-mono"
            />
          </div>

          <div>
            <label className="text-[10px] font-medium text-muted-foreground block mb-0.5">
              Output $/1M
            </label>
            <input
              type="number"
              step="0.01"
              value={newOutputPrice}
              onChange={(e) => setNewOutputPrice(e.target.value)}
              className="w-full bg-card border border-border rounded px-2 py-1 text-xs text-foreground font-mono"
            />
          </div>

          <div className="flex items-end">
            <Button
              type="button"
              size="sm"
              onClick={handleCreatePrice}
              disabled={savingPrice || !newPattern.trim()}
              className="w-full h-7 text-xs bg-primary text-primary-foreground font-medium cursor-pointer"
            >
              <Plus size={11} className="mr-1" />
              <span>Add Rate</span>
            </Button>
          </div>
        </div>

        {/* Existing Pricing Rules Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs font-mono">
            <thead>
              <tr className="border-b border-border/60 text-muted-foreground text-[10px] uppercase">
                <th className="py-2 pr-4 font-semibold">Model Pattern</th>
                <th className="py-2 pr-4 font-semibold">Provider</th>
                <th className="py-2 pr-4 font-semibold">Input / 1M</th>
                <th className="py-2 pr-4 font-semibold">Output / 1M</th>
                <th className="py-2 pr-4 font-semibold">Cached / 1M</th>
                <th className="py-2 font-semibold text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {pricingRules.map((rule) => (
                <tr key={rule.pricing_id} className="hover:bg-muted/30 transition-colors">
                  <td className="py-2 pr-4 font-bold text-foreground">{rule.model_pattern}</td>
                  <td className="py-2 pr-4 text-muted-foreground">{rule.provider || "Any (Global)"}</td>
                  <td className="py-2 pr-4">${rule.input_price_usd_per_million.toFixed(4)}</td>
                  <td className="py-2 pr-4">${rule.output_price_usd_per_million.toFixed(4)}</td>
                  <td className="py-2 pr-4 text-blue-500">
                    {rule.cached_input_price_usd_per_million !== null
                      ? `$${rule.cached_input_price_usd_per_million.toFixed(4)}`
                      : "—"}
                  </td>
                  <td className="py-2 text-right">
                    {rule.pricing_id.startsWith("prc_seed_") ? (
                      <span className="text-[10px] text-muted-foreground/60 italic font-sans">
                        Default
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => handleRetirePrice(rule.pricing_id)}
                        className="p-1 rounded text-muted-foreground hover:text-rose-500 cursor-pointer transition-colors"
                        title="Soft-retire pricing rule"
                      >
                        <Trash2 size={12} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default UsageTab;