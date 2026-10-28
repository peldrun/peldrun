// frontend/src/components/generative-ui/catalog/PeldrunChartBlock.tsx
//
// Chart renderer for the Markdown pipeline.
//
// Invoked by MarkdownRenderer.tsx as the handler for fenced code blocks
// tagged with `chart`. The block content is expected to be a JSON object
// matching one of the payload shapes defined in ../types.
//
// Supported payload shapes:
//
//   1. Simple (label/value):
//        { type, title, data: [{label, value}, ...] }
//
//   2. Single-series with x/y keys:
//        { type, title, data: [{xKey, yKey}, ...],
//          xAxisKey, yAxisKey? }
//
//   3. Multi-series:
//        { type, title, data: [{xKey, s1, s2}, ...],
//          xAxisKey, seriesKeys: ["s1", "s2"] }
//
// Telemetry writes are guarded by a ref so that a single chart instance
// reports at most one event per unique source string. This prevents the
// "Maximum update depth exceeded" error that could otherwise occur if
// store updates caused the effect to re-run on every render.
//
// Recharts is imported through next/dynamic so the heavy charting library
// is not included in the initial bundle.

"use client";

import React, { useEffect, useMemo, useRef, useState, useId } from "react";
import dynamic from "next/dynamic";
import {
  BarChart3,
  LineChart as LineIcon,
  PieChart as PieIcon,
  AreaChart as AreaIcon,
  AlertCircle,
  Copy,
  Check,
  Code2,
} from "lucide-react";

import { useGenerativeUIStore } from "../store";
import type {
  ChartPayload,
  ChartType,
  MultiSeriesChartPayload,
  SimpleChartPayload,
} from "../types";

// ---------------------------------------------------------------------------
// Dynamic Recharts imports
// ---------------------------------------------------------------------------

const LazyRecharts = dynamic(
  () =>
    import("recharts").then((mod) => {
      const Wrapped = (props: RechartsBundleProps) =>
        renderRechartsBundle(mod, props);
      Wrapped.displayName = "LazyRechartsBundle";
      return Wrapped;
    }),
  {
    ssr: false,
    loading: () => (
      <div className="h-56 flex items-center justify-center text-xs text-muted-foreground animate-pulse">
        Loading chart engine...
      </div>
    ),
  }
);

// ---------------------------------------------------------------------------
// Public props
// ---------------------------------------------------------------------------

export interface PeldrunChartBlockProps {
  /** Raw JSON source from the ```chart fenced block. */
  source: string;

  /**
   * When true, records telemetry events on failure and success.
   * Defaults to true.
   */
  telemetry?: boolean;
}

// ---------------------------------------------------------------------------
// Internal Recharts bundle component
// ---------------------------------------------------------------------------

interface RechartsBundleProps {
  payload: ChartPayload;
  colors: string[];
}

type RechartsModule = typeof import("recharts");

const PALETTE = [
  "#3b82f6",
  "#10b981",
  "#f59e0b",
  "#ef4444",
  "#8b5cf6",
  "#06b6d4",
  "#ec4899",
  "#14b8a6",
  "#f97316",
  "#6366f1",
];

function renderRechartsBundle(
  mod: RechartsModule,
  { payload, colors }: RechartsBundleProps
): React.JSX.Element {
  const {
    ResponsiveContainer,
    BarChart,
    Bar,
    LineChart,
    Line,
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
  } = mod;

  const axisStyle = {
    stroke: "currentColor",
    fontSize: 11,
    tickLine: false,
    axisLine: false,
  } as const;

  const tooltipContent = ({ active, payload: pts, label }: any) => {
    if (!active || !pts || pts.length === 0) return null;
    return (
      <div className="rounded-lg border border-border bg-card px-2.5 py-1.5 shadow-md text-xs space-y-0.5">
        {label !== undefined && (
          <div className="font-mono font-semibold text-foreground border-b border-border/40 pb-0.5 mb-1">
            {String(label)}
          </div>
        )}
        {pts.map((entry: any, idx: number) => (
          <div
            key={`tip-${idx}`}
            className="flex items-center justify-between gap-3 text-[11px]"
          >
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <span
                className="w-2 h-2 rounded-full"
                style={{ backgroundColor: entry.color }}
              />
              {entry.name}:
            </span>
            <span className="font-mono font-medium text-foreground">
              {typeof entry.value === "number"
                ? entry.value.toLocaleString()
                : String(entry.value)}
              {payload.unit ? ` ${payload.unit}` : ""}
            </span>
          </div>
        ))}
      </div>
    );
  };

  // Multi-series path.
  if (
    payload.type !== "donut" &&
    payload.type !== "pie" &&
    "xAxisKey" in payload
  ) {
    const multi = payload as MultiSeriesChartPayload;
    const commonAxis = (
      <>
        <CartesianGrid strokeDasharray="3 3" opacity={0.15} vertical={false} />
        <XAxis dataKey={multi.xAxisKey} {...axisStyle} />
        <YAxis {...axisStyle} width={48} />
        <Tooltip content={tooltipContent} />
        {multi.seriesKeys.length > 1 && (
          <Legend wrapperStyle={{ fontSize: 11 }} />
        )}
      </>
    );

    if (multi.type === "bar") {
      return (
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={multi.data}
            margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
          >
            {commonAxis}
            {multi.seriesKeys.map((key, idx) => (
              <Bar
                key={key}
                dataKey={key}
                fill={colors[idx % colors.length]}
                radius={[4, 4, 0, 0]}
                maxBarSize={48}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      );
    }

    if (multi.type === "area") {
      return (
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart
            data={multi.data}
            margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
          >
            <defs>
              {multi.seriesKeys.map((key, idx) => {
                const color = colors[idx % colors.length];
                return (
                  <linearGradient
                    key={`grad-${key}`}
                    id={`peldrun-chart-grad-${key}`}
                    x1="0"
                    y1="0"
                    x2="0"
                    y2="1"
                  >
                    <stop offset="5%" stopColor={color} stopOpacity={0.35} />
                    <stop offset="95%" stopColor={color} stopOpacity={0} />
                  </linearGradient>
                );
              })}
            </defs>
            {commonAxis}
            {multi.seriesKeys.map((key, idx) => {
              const color = colors[idx % colors.length];
              return (
                <Area
                  key={key}
                  type="monotone"
                  dataKey={key}
                  stroke={color}
                  strokeWidth={2}
                  fill={`url(#peldrun-chart-grad-${key})`}
                />
              );
            })}
          </AreaChart>
        </ResponsiveContainer>
      );
    }

    return (
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={multi.data}
          margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
        >
          {commonAxis}
          {multi.seriesKeys.map((key, idx) => (
            <Line
              key={key}
              type="monotone"
              dataKey={key}
              stroke={colors[idx % colors.length]}
              strokeWidth={2}
              dot={multi.data.length <= 25 ? { r: 2.5 } : false}
              activeDot={{ r: 5 }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    );
  }

  // Simple payload.
  const simple = payload as SimpleChartPayload;

  if (simple.type === "pie" || simple.type === "donut") {
    return (
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Tooltip content={tooltipContent} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Pie
            data={simple.data}
            dataKey="value"
            nameKey="label"
            cx="50%"
            cy="50%"
            innerRadius={simple.type === "donut" ? 50 : 0}
            outerRadius={80}
            paddingAngle={simple.type === "donut" ? 3 : 0}
          >
            {simple.data.map((_, idx) => (
              <Cell key={`cell-${idx}`} fill={colors[idx % colors.length]} />
            ))}
          </Pie>
        </PieChart>
      </ResponsiveContainer>
    );
  }

  const simpleAxis = (
    <>
      <CartesianGrid strokeDasharray="3 3" opacity={0.15} vertical={false} />
      <XAxis dataKey="label" {...axisStyle} />
      <YAxis {...axisStyle} width={48} />
      <Tooltip content={tooltipContent} />
    </>
  );

  if (simple.type === "bar") {
    return (
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={simple.data}
          margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
        >
          {simpleAxis}
          <Bar
            dataKey="value"
            fill={colors[0]}
            radius={[4, 4, 0, 0]}
            maxBarSize={48}
          />
        </BarChart>
      </ResponsiveContainer>
    );
  }

  if (simple.type === "area") {
    return (
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={simple.data}
          margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
        >
          <defs>
            <linearGradient
              id="peldrun-chart-grad-single"
              x1="0"
              y1="0"
              x2="0"
              y2="1"
            >
              <stop offset="5%" stopColor={colors[0]} stopOpacity={0.4} />
              <stop offset="95%" stopColor={colors[0]} stopOpacity={0} />
            </linearGradient>
          </defs>
          {simpleAxis}
          <Area
            type="monotone"
            dataKey="value"
            stroke={colors[0]}
            strokeWidth={2}
            fill="url(#peldrun-chart-grad-single)"
          />
        </AreaChart>
      </ResponsiveContainer>
    );
  }

  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart
        data={simple.data}
        margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
      >
        {simpleAxis}
        <Line
          type="monotone"
          dataKey="value"
          stroke={colors[0]}
          strokeWidth={2.5}
          dot={simple.data.length <= 25 ? { r: 3 } : false}
          activeDot={{ r: 6 }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Payload parsing and validation
// ---------------------------------------------------------------------------

const ALLOWED_CHART_TYPES: readonly ChartType[] = [
  "bar",
  "line",
  "area",
  "donut",
  "pie",
];

interface ParseResult {
  ok: true;
  payload: ChartPayload;
}

interface ParseError {
  ok: false;
  reason: string;
}

/**
 * Detects the value key for a single-series chart when the payload uses
 * the {xAxisKey, yAxisKey} shape instead of the {label, value} shape.
 *
 * Resolution order:
 *   1. Explicit `yAxisKey` if present and present in the first row.
 *   2. The first numeric key in the first row that is not the xAxisKey.
 */
function resolveValueKey(
  firstRow: Record<string, unknown>,
  xAxisKey: string,
  yAxisKey?: string,
): string | null {
  if (
    typeof yAxisKey === "string" &&
    yAxisKey.length > 0 &&
    yAxisKey in firstRow
  ) {
    return yAxisKey;
  }

  for (const key of Object.keys(firstRow)) {
    if (key === xAxisKey) continue;
    const value = firstRow[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return key;
    }
  }

  return null;
}

/**
 * Level 2 fallback: extracts a JSON object from surrounding text.
 *
 * Common LLM failure modes:
 *   - Wraps the JSON in extra markdown fences that slipped through the
 *     renderer's fence stripping.
 *   - Prefixes it with prose ("Here is the chart: { ... }").
 *
 * The extractor walks the source character by character tracking brace depth
 * and string state, so nested objects and escaped quotes are preserved.
 */
function extractChartJsonBlock(source: string): string | null {
  if (!source) return null;

  // Strip any residual fences
  const stripped = source
    .replace(/```(?:json)?\s*/gi, "")
    .replace(/```\s*$/g, "")
    .trim();

  if (stripped !== source.trim()) {
    return stripped;
  }

  // Walk to find the first complete top-level {...} block
  const firstBrace = stripped.indexOf("{");
  if (firstBrace === -1) return null;

  let depth = 0;
  let inString = false;
  let escape = false;
  for (let i = firstBrace; i < stripped.length; i += 1) {
    const ch = stripped[i];
    if (escape) { escape = false; continue; }
    if (ch === "\\") { escape = true; continue; }
    if (ch === '"') { inString = !inString; continue; }
    if (inString) continue;
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        const extracted = stripped.slice(firstBrace, i + 1);
        return extracted === source ? null : extracted;
      }
    }
  }

  return null;
}

/**
 * Level 3 fallback: repairs the single most common syntax error LLMs emit
 * — a trailing comma before a closing brace or bracket.
 *
 * Example:
 *   { "data": [1, 2, 3,] }   →   { "data": [1, 2, 3] }
 *
 * Deliberately does NOT attempt to fix single-quoted strings or unquoted
 * keys — those transformations risk corrupting legitimate string values
 * that contain apostrophes or special characters, and a corrupted string
 * is worse than a clear parse error.
 */
function repairChartJson(source: string): string | null {
  if (!source) return null;
  const repaired = source.replace(/,(\s*[}\]])/g, "$1");
  return repaired === source ? null : repaired;
}

/**
 * Builds the ordered list of candidates to try, capped at
 * `min(max_retries + 1, strategies_available)`.
 */
function buildChartAttempts(source: string, maxRetries: number): string[] {
  const candidates: Array<string | null> = [
    source,
    maxRetries >= 1 ? extractChartJsonBlock(source) : null,
    maxRetries >= 2 ? repairChartJson(extractChartJsonBlock(source) || source) : null,
    maxRetries >= 3 ? repairChartJson(source) : null,
  ];

  const out: string[] = [];
  let previous: string | null = null;
  for (const candidate of candidates) {
    if (!candidate || candidate === previous) continue;
    out.push(candidate);
    previous = candidate;
  }
  return out;
}

/**
 * Parses and validates a raw JSON string into a ChartPayload.
 *
 * See the header comment for the three supported payload shapes.
 *
 * Retries: if `maxRetries > 0`, the parser tries alternative extractions of
 * the same source (see `buildChartAttempts`) before giving up. Validation
 * errors (wrong shape, missing keys) are NOT retried — the data itself is
 * wrong, and re-parsing cannot fix a missing `label` field.
 */
function parseChartPayload(
  source: string,
  maxPoints: number,
  maxRetries: number = 0,
): ParseResult | ParseError {
  if (!source || !source.trim()) {
    return { ok: false, reason: "Empty chart block." };
  }

  const attempts = buildChartAttempts(source, maxRetries);
  let lastError: ParseError | null = null;

  for (const candidate of attempts) {
    let raw: unknown;
    try {
      raw = JSON.parse(candidate);
    } catch (err) {
      lastError = {
        ok: false,
        reason:
          (err as { message?: string })?.message ||
          "Chart payload is not valid JSON.",
      };
      continue;
    }

    // Delegate the parsed object to the existing validation logic by
    // re-parsing a serialized copy — kept as-is to avoid duplicating the
    // ~120-line validator that follows.
    return parseChartPayloadFromObject(raw, maxPoints);
  }

  return lastError || { ok: false, reason: "All chart parse attempts failed." };
}

/**
 * Validator extracted from the original `parseChartPayload` body. Takes an
 * already-parsed object and applies the full shape check.
 */
function parseChartPayloadFromObject(
  raw: unknown,
  maxPoints: number
): ParseResult | ParseError {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, reason: "Chart payload must be a JSON object." };
  }

  const obj = raw as Record<string, unknown>;

  const typeRaw = String(obj.type ?? "line").toLowerCase();
  const type = ALLOWED_CHART_TYPES.includes(typeRaw as ChartType)
    ? (typeRaw as ChartType)
    : "line";

  const title =
    typeof obj.title === "string" && obj.title.trim().length > 0
      ? obj.title.trim()
      : "Chart";

  const unit = typeof obj.unit === "string" ? obj.unit : undefined;
  const description =
    typeof obj.description === "string" ? obj.description : undefined;

  const data = obj.data;
  if (!Array.isArray(data) || data.length === 0) {
    return { ok: false, reason: "Chart payload is missing a `data` array." };
  }

  if (data.length > maxPoints) {
    return {
      ok: false,
      reason: `Chart has ${data.length} data points, exceeding the configured limit of ${maxPoints}.`,
    };
  }

  const firstRow = data[0] as Record<string, unknown> | null;
  if (!firstRow || typeof firstRow !== "object") {
    return { ok: false, reason: "Chart data rows must be objects." };
  }

  const xAxisKey =
    typeof obj.xAxisKey === "string" && obj.xAxisKey.length > 0
      ? obj.xAxisKey
      : null;

  const yAxisKey =
    typeof obj.yAxisKey === "string" ? obj.yAxisKey : undefined;

  // -----------------------------------------------------------------------
  // Multi-series: explicit `seriesKeys` array provided.
  // -----------------------------------------------------------------------
  if (xAxisKey && Array.isArray(obj.seriesKeys)) {
    const seriesKeys = (obj.seriesKeys as unknown[]).filter(
      (k): k is string => typeof k === "string" && k.length > 0,
    );

    if (seriesKeys.length === 0) {
      return {
        ok: false,
        reason: "`seriesKeys` must contain at least one key.",
      };
    }

    const missing = seriesKeys.filter((k) => !(k in firstRow));
    if (missing.length > 0) {
      return {
        ok: false,
        reason: `Missing series keys in data: ${missing.join(", ")}.`,
      };
    }

    return {
      ok: true,
      payload: {
        type: type === "donut" || type === "pie" ? "bar" : type,
        title,
        unit,
        description,
        data: data as Array<Record<string, string | number>>,
        xAxisKey,
        seriesKeys,
      } as MultiSeriesChartPayload,
    };
  }

  // -----------------------------------------------------------------------
  // Single-series with explicit x-axis key.
  //
  // The rows use {xKey, yKey} instead of {label, value}. We normalize
  // them into the {label, value} shape consumed by the simple renderer.
  // -----------------------------------------------------------------------
  if (xAxisKey) {
    const valueKey = resolveValueKey(firstRow, xAxisKey, yAxisKey);
    if (!valueKey) {
      return {
        ok: false,
        reason: `Could not determine a numeric value column in the first data row. Provide "yAxisKey" or include a numeric field other than "${xAxisKey}".`,
      };
    }

    const simpleData: Array<{ label: string; value: number }> = [];
    for (let i = 0; i < data.length; i += 1) {
      const row = data[i] as Record<string, unknown>;
      if (!row || typeof row !== "object") {
        return { ok: false, reason: `Row ${i + 1} is not an object.` };
      }

      const labelRaw = row[xAxisKey];
      const valueRaw = row[valueKey];

      if (labelRaw === undefined) {
        return {
          ok: false,
          reason: `Row ${i + 1} is missing the "${xAxisKey}" field.`,
        };
      }
      if (valueRaw === undefined) {
        return {
          ok: false,
          reason: `Row ${i + 1} is missing the "${valueKey}" field.`,
        };
      }

      const value = Number(valueRaw);
      if (!Number.isFinite(value)) {
        return {
          ok: false,
          reason: `Row ${i + 1} has a non-numeric "${valueKey}" value.`,
        };
      }

      simpleData.push({ label: String(labelRaw), value });
    }

    return {
      ok: true,
      payload: {
        type,
        title,
        unit,
        description,
        data: simpleData,
      } as SimpleChartPayload,
    };
  }

  // -----------------------------------------------------------------------
  // Pure simple path: {data: [{label, value}, ...]} with no x/y keys.
  // -----------------------------------------------------------------------
  const simpleData: Array<{ label: string; value: number }> = [];
  for (let i = 0; i < data.length; i += 1) {
    const row = data[i];
    if (!row || typeof row !== "object") {
      return { ok: false, reason: `Row ${i + 1} is not an object.` };
    }
    const r = row as Record<string, unknown>;
    const labelRaw = r.label;
    const valueRaw = r.value;

    if (labelRaw === undefined || valueRaw === undefined) {
      return {
        ok: false,
        reason: `Row ${i + 1} must have both "label" and "value" (or declare "xAxisKey" and "yAxisKey").`,
      };
    }

    const label = String(labelRaw);
    const value = Number(valueRaw);
    if (!Number.isFinite(value)) {
      return {
        ok: false,
        reason: `Row ${i + 1} has a non-numeric "value".`,
      };
    }

    simpleData.push({ label, value });
  }

  return {
    ok: true,
    payload: {
      type,
      title,
      unit,
      description,
      data: simpleData,
    } as SimpleChartPayload,
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PeldrunChartBlock({
  source,
  telemetry = true,
}: PeldrunChartBlockProps): React.JSX.Element {
  const maxPoints = useGenerativeUIStore((s) => s.limits.max_chart_points);
  // [FIX-MAX-RETRIES] Read the configured retry budget so the chart parser
  // can attempt alternative extractions when the initial JSON.parse fails.
  const maxRetries = useGenerativeUIStore((s) => s.max_retries);

  // [FIX-TELEMETRY-2] Actions are read via getState() at call time (see the
  // parallel fix in PeldrunDiagram.tsx) so rehydration cannot route updates
  // into a stale store instance. The previous useRef pattern was the same
  // stale-closure pitfall.

  // Ensures a single chart instance reports at most one telemetry event per
  // unique source string, even across re-renders.
  const recordedSourceRef = useRef<string | null>(null);

  const [mounted, setMounted] = useState(false);
  const [showSource, setShowSource] = useState(false);
  const [copied, setCopied] = useState(false);

  const reactId = useId().replace(/:/g, "_");

  const colors = useMemo(() => {
    const offset = reactId
      .split("")
      .reduce((acc, ch) => acc + ch.charCodeAt(0), 0);
    const start = offset % PALETTE.length;
    return [...PALETTE.slice(start), ...PALETTE.slice(0, start)];
  }, [reactId]);

  useEffect(() => {
    setMounted(true);
  }, []);

  const parsed = useMemo(
    () => parseChartPayload(source, maxPoints, maxRetries),
    [source, maxPoints, maxRetries]
  );

  useEffect(() => {
    if (!telemetry) return;
    if (recordedSourceRef.current === source) return;
    recordedSourceRef.current = source;

    // [FIX-TELEMETRY-2] Fetch the live action to avoid writing into a stale
    // store instance after HMR / rehydration.
    const store = useGenerativeUIStore.getState();
    if (parsed.ok) {
      store.recordSuccess(0);
    } else {
      store.recordError("chart");
    }
  }, [parsed, source, telemetry]);

  if (!parsed.ok) {
    return (
      <div className="my-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 space-y-2">
        <div className="flex items-start gap-2.5 text-destructive text-xs">
          <AlertCircle size={15} className="shrink-0 mt-0.5" />
          <div className="space-y-0.5">
            <div className="font-semibold">Chart Payload Error</div>
            <div className="font-mono text-[11px] opacity-90">
              {parsed.reason}
            </div>
          </div>
        </div>
        <details className="text-[11px] text-muted-foreground">
          <summary className="cursor-pointer hover:text-foreground transition-colors">
            View source
          </summary>
          <pre className="mt-2 p-2.5 rounded-md bg-background/60 border border-border/50 font-mono whitespace-pre-wrap overflow-auto max-h-48">
            {source}
          </pre>
        </details>
      </div>
    );
  }

  const { payload } = parsed;

  const handleCopyJson = () => {
    navigator.clipboard.writeText(source);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const renderIcon = () => {
    switch (payload.type) {
      case "bar":
        return <BarChart3 size={15} className="text-primary shrink-0" />;
      case "area":
        return <AreaIcon size={15} className="text-primary shrink-0" />;
      case "donut":
      case "pie":
        return <PieIcon size={15} className="text-primary shrink-0" />;
      default:
        return <LineIcon size={15} className="text-primary shrink-0" />;
    }
  };

  return (
    <div className="my-3 rounded-xl border border-border/80 bg-card/60 backdrop-blur-sm overflow-hidden shadow-xs">
      <div className="flex items-center justify-between px-3.5 py-2.5 bg-muted/30 border-b border-border/50 text-xs">
        <div className="flex items-center gap-2 font-medium text-foreground min-w-0">
          {renderIcon()}
          <span className="font-semibold tracking-tight truncate">
            {payload.title}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setShowSource((v) => !v)}
            title={showSource ? "Show chart" : "Show JSON source"}
            className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors flex items-center gap-1 text-[11px]"
          >
            <Code2 size={13} />
            <span>{showSource ? "Chart" : "JSON"}</span>
          </button>
          <button
            type="button"
            onClick={handleCopyJson}
            title="Copy JSON source"
            className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
          >
            {copied ? (
              <Check size={13} className="text-emerald-500" />
            ) : (
              <Copy size={13} />
            )}
          </button>
        </div>
      </div>

      {payload.description && (
        <div className="px-3.5 py-2 text-[11px] text-muted-foreground border-b border-border/40 bg-background/20">
          {payload.description}
        </div>
      )}

      <div className="p-4 bg-background/20">
        {showSource ? (
          <pre className="max-h-[280px] overflow-auto p-3 rounded-lg bg-background/80 border border-border/60 text-[11px] font-mono text-muted-foreground whitespace-pre-wrap">
            {source}
          </pre>
        ) : !mounted ? (
          <div className="h-56 flex items-center justify-center">
            <div className="flex items-center gap-2 text-xs text-muted-foreground animate-pulse">
              <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
              <span>Preparing chart canvas...</span>
            </div>
          </div>
        ) : (
          <div className="h-64 w-full">
            <LazyRecharts payload={payload} colors={colors} />
          </div>
        )}
      </div>
    </div>
  );
}

export default PeldrunChartBlock;
