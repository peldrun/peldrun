// frontend/src/components/generative-ui/catalog/PeldrunChart.tsx
"use client";

import React, { useState, useEffect, useMemo } from "react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  BarChart,
  Bar,
  AreaChart,
  Area,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";
import {
  BarChart3,
  LineChart as LineIcon,
  PieChart as PieIcon,
  AreaChart as AreaIcon,
  Code2,
  Eye,
  Copy,
  Check,
} from "lucide-react";
import type { PeldrunChartProps as BaseChartProps } from "../types";

export interface PeldrunChartProps extends Omit<Partial<BaseChartProps>, "type"> {
  title?: string;
  type?: "bar" | "line" | "donut" | "area" | "pie" | string;
  data?: any;
  unit?: string;
  xAxisKey?: string;
  seriesKeys?: string[];
  className?: string;
}

const PALETTE = [
  "#3b82f6", // Blue
  "#10b981", // Emerald
  "#f59e0b", // Amber
  "#ef4444", // Red
  "#8b5cf6", // Violet
  "#06b6d4", // Cyan
  "#ec4899", // Pink
  "#14b8a6", // Teal
  "#f97316", // Orange
  "#6366f1", // Indigo
];

interface NormalizedChartPayload {
  title: string;
  chartType: "bar" | "line" | "donut" | "area" | "pie";
  dataPoints: Array<Record<string, any>>;
  xAxisKey: string;
  seriesKeys: string[];
  unit?: string;
}

function parseAndNormalizeData(
  rawInput: any,
  fallbackType: string = "line",
  fallbackTitle?: string,
  fallbackUnit?: string
): NormalizedChartPayload | null {
  if (!rawInput) return null;

  let parsed = rawInput;
  if (typeof rawInput === "string") {
    try {
      parsed = JSON.parse(rawInput);
    } catch {
      return null;
    }
  }

  let title = fallbackTitle || "Performance & Engineering Chart";
  let chartTypeRaw = fallbackType;
  let unit = fallbackUnit;
  let rawList: any[] = [];
  let explicitXAxis: string | undefined;
  let explicitSeries: string[] | undefined;

  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const container = parsed.chart || parsed;
    if (container.title) title = container.title;
    if (container.type) chartTypeRaw = container.type;
    if (container.unit) unit = container.unit;
    if (container.xAxis || container.xAxisKey) {
      explicitXAxis = typeof container.xAxis === "string" ? container.xAxis : container.xAxisKey;
    }
    if (Array.isArray(container.series)) {
      explicitSeries = container.series.map((s: any) => (typeof s === "string" ? s : s.key || s.dataKey || s.name));
    }

    if (Array.isArray(container.data)) {
      rawList = container.data;
    } else if (Array.isArray(container.dataPoints)) {
      rawList = container.dataPoints;
    } else if (Array.isArray(container.points)) {
      rawList = container.points;
    }
  } else if (Array.isArray(parsed)) {
    rawList = parsed;
  }

  if (!rawList || rawList.length === 0) {
    return null;
  }

  // Sanitize Chart Type
  const typeLower = String(chartTypeRaw || "line").toLowerCase();
  let chartType: "bar" | "line" | "donut" | "area" | "pie" = "line";
  if (typeLower.includes("bar")) chartType = "bar";
  else if (typeLower.includes("area")) chartType = "area";
  else if (typeLower.includes("donut")) chartType = "donut";
  else if (typeLower.includes("pie")) chartType = "pie";
  else chartType = "line";

  // Identify X-Axis Key
  const firstItem = rawList[0];
  if (!firstItem || typeof firstItem !== "object") return null;

  const itemKeys = Object.keys(firstItem);
  let resolvedXAxis = explicitXAxis;

  if (!resolvedXAxis) {
    const xCandidates = ["time", "t", "x", "frequency", "freq", "omega", "step", "date", "label", "name"];
    resolvedXAxis = itemKeys.find((k) => xCandidates.includes(k.toLowerCase())) || itemKeys[0];
  }

  // Identify Numeric Series Keys
  let resolvedSeries = explicitSeries;
  if (!resolvedSeries || resolvedSeries.length === 0) {
    resolvedSeries = itemKeys.filter((k) => {
      if (k === resolvedXAxis) return false;
      const val = firstItem[k];
      return typeof val === "number" || (!isNaN(Number(val)) && val !== "");
    });
  }

  // Fallback for single label/value format
  if (resolvedSeries.length === 0 && itemKeys.includes("value")) {
    resolvedSeries = ["value"];
  }

  return {
    title,
    chartType,
    dataPoints: rawList,
    xAxisKey: resolvedXAxis || "label",
    seriesKeys: resolvedSeries,
    unit,
  };
}

export function PeldrunChart(props: PeldrunChartProps): React.JSX.Element | null {
  const [mounted, setMounted] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  const normalized = useMemo(() => {
    return parseAndNormalizeData(props.data, props.type, props.title, props.unit);
  }, [props.data, props.type, props.title, props.unit]);

  if (!normalized) {
    return null;
  }

  if (!mounted) {
    return (
      <div className="my-2.5 rounded-xl border border-border/80 bg-card/60 p-4 h-[280px] flex items-center justify-center">
        <div className="flex items-center gap-2 text-xs text-muted-foreground animate-pulse">
          <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
          <span>Initializing Recharts analytics canvas...</span>
        </div>
      </div>
    );
  }

  const { title, chartType, dataPoints, xAxisKey, seriesKeys, unit } = normalized;

  const handleCopyJson = () => {
    navigator.clipboard.writeText(JSON.stringify(dataPoints, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const renderIcon = () => {
    switch (chartType) {
      case "bar":
        return <BarChart3 size={15} className="text-primary shrink-0" />;
      case "area":
        return <AreaIcon size={15} className="text-primary shrink-0" />;
      case "pie":
      case "donut":
        return <PieIcon size={15} className="text-primary shrink-0" />;
      default:
        return <LineIcon size={15} className="text-primary shrink-0" />;
    }
  };

  const renderTooltip = () => (
    <Tooltip
      content={({ active, payload, label }) => {
        if (!active || !payload || payload.length === 0) return null;
        return (
          <div className="rounded-lg border border-border/80 bg-background/95 backdrop-blur-md p-2.5 shadow-lg text-xs space-y-1">
            <div className="font-semibold text-foreground border-b border-border/40 pb-1 font-mono">
              {String(label)}
            </div>
            <div className="space-y-0.5">
              {payload.map((entry, idx) => (
                <div key={`tip-${idx}`} className="flex items-center justify-between gap-3 text-[11px]">
                  <span className="flex items-center gap-1.5 text-muted-foreground">
                    <span className="w-2 h-2 rounded-full" style={{ backgroundColor: entry.color }} />
                    {entry.name}:
                  </span>
                  <span className="font-mono font-medium text-foreground">
                    {typeof entry.value === "number" ? entry.value.toLocaleString() : entry.value}
                    {unit ? ` ${unit}` : ""}
                  </span>
                </div>
              ))}
            </div>
          </div>
        );
      }}
    />
  );

  return (
    <div className={`my-2.5 rounded-xl border border-border/80 bg-card/60 backdrop-blur-sm overflow-hidden shadow-xs ${props.className || ""}`}>
      {/* Header Bar */}
      <div className="flex items-center justify-between px-3.5 py-2.5 bg-muted/30 border-b border-border/50 text-xs">
        <div className="flex items-center gap-2 font-medium text-foreground">
          {renderIcon()}
          <span className="font-semibold tracking-tight">{title}</span>
          {seriesKeys.length > 0 && (
            <span className="px-1.5 py-0.5 text-[10px] font-mono rounded bg-primary/10 text-primary border border-primary/20">
              {seriesKeys.length} series
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setShowRaw(!showRaw)}
            title={showRaw ? "Show Visual Chart" : "Show Raw Data JSON"}
            className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors flex items-center gap-1 text-[11px]"
          >
            {showRaw ? <Eye size={13} /> : <Code2 size={13} />}
            <span>{showRaw ? "Chart" : "JSON"}</span>
          </button>
          <button
            type="button"
            onClick={handleCopyJson}
            title="Copy Dataset"
            className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
          >
            {copied ? <Check size={13} className="text-emerald-500" /> : <Copy size={13} />}
          </button>
        </div>
      </div>

      {/* Content Area */}
      <div className="p-4 bg-background/20">
        {showRaw ? (
          <pre className="max-h-[280px] overflow-auto p-3 rounded-lg bg-background/80 border border-border/60 text-[11px] font-mono text-muted-foreground">
            {JSON.stringify(dataPoints, null, 2)}
          </pre>
        ) : (
          <div className="w-full h-[280px]">
            <ResponsiveContainer width="100%" height="100%">
              {chartType === "bar" ? (
                <BarChart data={dataPoints} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" opacity={0.2} vertical={false} />
                  <XAxis dataKey={xAxisKey} tick={{ fontSize: 10 }} tickLine={false} />
                  <YAxis tick={{ fontSize: 10 }} tickLine={false} />
                  {renderTooltip()}
                  {seriesKeys.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
                  {seriesKeys.map((key, idx) => (
                    <Bar
                      key={key}
                      dataKey={key}
                      fill={PALETTE[idx % PALETTE.length]}
                      radius={[4, 4, 0, 0]}
                      maxBarSize={40}
                    />
                  ))}
                </BarChart>
              ) : chartType === "area" ? (
                <AreaChart data={dataPoints} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                  <defs>
                    {seriesKeys.map((key, idx) => {
                      const color = PALETTE[idx % PALETTE.length];
                      return (
                        <linearGradient key={`grad-${key}`} id={`peldrun-area-${key}`} x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor={color} stopOpacity={0.35} />
                          <stop offset="95%" stopColor={color} stopOpacity={0.0} />
                        </linearGradient>
                      );
                    })}
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" opacity={0.2} vertical={false} />
                  <XAxis dataKey={xAxisKey} tick={{ fontSize: 10 }} tickLine={false} />
                  <YAxis tick={{ fontSize: 10 }} tickLine={false} />
                  {renderTooltip()}
                  {seriesKeys.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
                  {seriesKeys.map((key, idx) => {
                    const color = PALETTE[idx % PALETTE.length];
                    return (
                      <Area
                        key={key}
                        type="monotone"
                        dataKey={key}
                        stroke={color}
                        strokeWidth={2}
                        fill={`url(#peldrun-area-${key})`}
                      />
                    );
                  })}
                </AreaChart>
              ) : chartType === "pie" || chartType === "donut" ? (
                <PieChart>
                  {renderTooltip()}
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Pie
                    data={dataPoints}
                    dataKey={seriesKeys[0] || "value"}
                    nameKey={xAxisKey || "name"}
                    cx="50%"
                    cy="50%"
                    innerRadius={chartType === "donut" ? 50 : 0}
                    outerRadius={80}
                    paddingAngle={3}
                  >
                    {dataPoints.map((_, idx) => (
                      <Cell key={`cell-${idx}`} fill={PALETTE[idx % PALETTE.length]} />
                    ))}
                  </Pie>
                </PieChart>
              ) : (
                <LineChart data={dataPoints} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" opacity={0.2} vertical={false} />
                  <XAxis dataKey={xAxisKey} tick={{ fontSize: 10 }} tickLine={false} />
                  <YAxis tick={{ fontSize: 10 }} tickLine={false} />
                  {renderTooltip()}
                  {seriesKeys.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
                  {seriesKeys.map((key, idx) => (
                    <Line
                      key={key}
                      type="monotone"
                      dataKey={key}
                      stroke={PALETTE[idx % PALETTE.length]}
                      strokeWidth={2}
                      dot={dataPoints.length <= 25 ? { r: 2.5 } : false}
                      activeDot={{ r: 5 }}
                    />
                  ))}
                </LineChart>
              )}
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </div>
  );
}

export default PeldrunChart;
