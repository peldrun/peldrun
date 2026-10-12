// frontend/src/components/chat/code-block.tsx
"use client";

import React, { useState, useMemo } from "react";
import dynamic from "next/dynamic";
import { Highlight, themes } from "prism-react-renderer";
import {
  Check,
  Copy,
  Download,
  FileCode,
  ChevronDown,
  ChevronUp,
  Terminal,
  PanelRightOpen,
  LineChart as ChartIcon,
  Code2,
} from "lucide-react";

// Dynamic import for PeldrunChart to prevent bundling overhead when not charting
const LazyChart = dynamic(
  () =>
    import("@/components/generative-ui/catalog/PeldrunChart").then(
      (m) => m.PeldrunChart || (m as any).default
    ),
  {
    loading: () => (
      <div className="my-2 rounded-xl border border-border/80 bg-card/50 p-6 flex items-center justify-center gap-2 text-xs text-muted-foreground animate-pulse">
        <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span>Rendering Recharts canvas...</span>
      </div>
    ),
    ssr: false,
  }
);

export interface CodeBlockProps {
  code: string;
  language?: string;
  filename?: string;
  className?: string;
}

interface ParsedChartData {
  title: string;
  type: "line" | "bar" | "area" | "donut";
  unit?: string;
  data: Array<{ label: string; value: number }>;
}

const languageAliases: Record<string, string> = {
  py: "python",
  python: "python",
  js: "javascript",
  javascript: "javascript",
  ts: "typescript",
  typescript: "typescript",
  jsx: "jsx",
  tsx: "tsx",
  html: "markup",
  xml: "markup",
  svg: "markup",
  css: "css",
  json: "json",
  recharts: "json",
  chart: "json",
  sh: "bash",
  bash: "bash",
  shell: "bash",
  zsh: "bash",
  powershell: "bash",
  ps1: "bash",
  sql: "sql",
  yaml: "yaml",
  yml: "yaml",
  md: "markdown",
  markdown: "markdown",
  rust: "rust",
  rs: "rust",
  go: "go",
  c: "c",
  cpp: "cpp",
};

const extensionMap: Record<string, string> = {
  python: "py",
  javascript: "js",
  typescript: "ts",
  jsx: "jsx",
  tsx: "tsx",
  markup: "html",
  css: "css",
  json: "json",
  recharts: "json",
  chart: "json",
  bash: "sh",
  sql: "sql",
  yaml: "yaml",
  markdown: "md",
  rust: "rs",
  go: "go",
  c: "c",
  cpp: "cpp",
};

/**
 * Extracts normalized { label, value } pairs from varied engineering data key patterns
 * (e.g. time/voltage in RLC steps, freq/gain in Bode plots, or label/value).
 */
function extractDataPoints(items: any[]): Array<{ label: string; value: number }> {
  if (!Array.isArray(items) || items.length === 0) return [];

  const result: Array<{ label: string; value: number }> = [];

  const xKeyCandidates = [
    "time",
    "t",
    "freq",
    "frequency",
    "date",
    "step",
    "omega",
    "timestamp",
    "x",
    "label",
    "name",
    "phase",
  ];

  const yKeyCandidates = [
    "voltage",
    "v",
    "current",
    "i",
    "amplitude",
    "gain",
    "magnitude",
    "mag",
    "db",
    "val",
    "value",
    "y",
    "power",
    "response",
    "stress",
    "strain",
    "temp",
  ];

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (!item || typeof item !== "object") continue;

    // Direct match: explicit label & value
    if (
      ("label" in item || "name" in item || "x" in item) &&
      ("value" in item || "y" in item)
    ) {
      const label = String(item.label ?? item.name ?? item.x ?? `P${i + 1}`);
      const valNum = Number(item.value ?? item.y);
      if (!isNaN(valNum)) {
        result.push({ label, value: valNum });
        continue;
      }
    }

    const keys = Object.keys(item);
    let labelVal: string | null = null;
    let numVal: number | null = null;

    // Detect known candidates
    for (const k of keys) {
      const lowerK = k.toLowerCase();
      if (yKeyCandidates.includes(lowerK) && typeof item[k] === "number" && !isNaN(item[k])) {
        numVal = item[k];
      }
      if (xKeyCandidates.includes(lowerK) && labelVal === null) {
        labelVal = String(item[k]);
      }
    }

    // Generic fallback: first numerical key is value, first string/non-value key is label
    if (numVal === null) {
      for (const k of keys) {
        if (typeof item[k] === "number" && !isNaN(item[k])) {
          numVal = item[k];
          break;
        }
      }
    }

    if (labelVal === null) {
      for (const k of keys) {
        if (
          typeof item[k] === "string" ||
          (typeof item[k] === "number" && item[k] !== numVal)
        ) {
          labelVal = String(item[k]);
          break;
        }
      }
    }

    if (numVal !== null) {
      result.push({
        label: labelVal ?? `P${i + 1}`,
        value: numVal,
      });
    }
  }

  return result;
}

/**
 * Validates and transforms JSON or recharts blocks into interactive chart datasets.
 */
function parseChartableData(code: string, rawLang: string): ParsedChartData | null {
  const lang = rawLang?.toLowerCase()?.trim() || "";
  const isTargetLanguage =
    lang === "json" ||
    lang === "recharts" ||
    lang === "chart" ||
    lang === "application/json";

  if (!isTargetLanguage || !code) return null;

  try {
    const parsed = JSON.parse(code);
    if (!parsed || typeof parsed !== "object") return null;

    // Pattern 1: Standard Chart envelope { title?, type?, unit?, data: [...] }
    if (!Array.isArray(parsed) && Array.isArray(parsed.data) && parsed.data.length >= 2) {
      const normalizedData = extractDataPoints(parsed.data);
      if (normalizedData.length >= 2) {
        const rawType = (parsed.type || "line").toLowerCase();
        const validTypes = ["line", "bar", "area", "donut"];
        const chartType = validTypes.includes(rawType) ? (rawType as any) : "line";
        return {
          title: parsed.title || "Engineering Analytics",
          type: chartType,
          unit: parsed.unit || "",
          data: normalizedData,
        };
      }
    }

    // Pattern 2: Direct array of points [ { time: ..., voltage: ... }, ... ]
    if (Array.isArray(parsed) && parsed.length >= 2) {
      const normalizedData = extractDataPoints(parsed);
      if (normalizedData.length >= 2) {
        return {
          title: "Engineering Simulation Response",
          type: "line",
          unit: "",
          data: normalizedData,
        };
      }
    }

    // Pattern 3: Envelope with series or points { series: [...] }
    const possibleArray = parsed.series || parsed.points || parsed.measurements;
    if (Array.isArray(possibleArray) && possibleArray.length >= 2) {
      const normalizedData = extractDataPoints(possibleArray);
      if (normalizedData.length >= 2) {
        const rawType = (parsed.type || "line").toLowerCase();
        const validTypes = ["line", "bar", "area", "donut"];
        const chartType = validTypes.includes(rawType) ? (rawType as any) : "line";
        return {
          title: parsed.title || "Engineering Analytics",
          type: chartType,
          unit: parsed.unit || "",
          data: normalizedData,
        };
      }
    }

    return null;
  } catch {
    return null;
  }
}

export function CodeBlock({
  code = "",
  language = "text",
  filename,
  className = "",
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);

  const cleanCode = typeof code === "string" ? code.trim() : String(code || "");
  const lines = cleanCode.split("\n");
  const isLong = lines.length > 20;

  const normalizedLang = languageAliases[language.toLowerCase()] || "javascript";
  const displayLang = (language || "TEXT").toUpperCase();
  const fileExt = extensionMap[normalizedLang] || "txt";
  const defaultFilename = filename || `snippet.${fileExt}`;

  // Evaluate if the snippet can be transformed into an interactive chart
  const chartData = useMemo(() => {
    return parseChartableData(cleanCode, language);
  }, [cleanCode, language]);

  // Default to interactive chart view if chart data is detected
  const [viewMode, setViewMode] = useState<"chart" | "code">("chart");

  const handleCopy = async () => {
    if (!cleanCode) return;
    try {
      if (typeof window !== "undefined" && navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(cleanCode);
      } else {
        const textArea = document.createElement("textarea");
        textArea.value = cleanCode;
        textArea.style.position = "fixed";
        textArea.style.left = "-999999px";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand("copy");
        textArea.remove();
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Clipboard copy failed:", err);
    }
  };

  const handleDownload = () => {
    if (!cleanCode) return;
    const blob = new Blob([cleanCode], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = defaultFilename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const handleOpenInSandbox = () => {
    if (!cleanCode) return;
    if (typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("peldrun:open-in-sandbox", {
          detail: {
            code: cleanCode,
            language: normalizedLang,
            filename: defaultFilename,
          },
        })
      );
    }
  };

  const HighlightComponent = Highlight as any;

  return (
    <div
      dir="ltr"
      className={`my-3 overflow-hidden rounded-sm border border-border bg-[#0d1117] text-slate-100 shadow-peldrun-sm font-mono ${className}`}
    >
      <div className="flex items-center justify-between px-3.5 py-2 border-b border-border/60 bg-[#161b22]">
        <div className="flex items-center gap-2 truncate">
          {normalizedLang === "bash" ? (
            <Terminal size={13} className="text-primary flex-shrink-0" />
          ) : (
            <FileCode size={13} className="text-card flex-shrink-0" />
          )}
          <span className="font-mono text-xs font-semibold text-muted-foreground/90 truncate max-w-[180px] sm:max-w-none">
            {defaultFilename}
          </span>
          <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-muted/10 text-card">
            {displayLang}
          </span>
          {(!chartData || viewMode === "code") && (
            <span className="hidden sm:inline-block text-[11px] font-mono text-muted-foreground">
              ({lines.length} lines)
            </span>
          )}

          {/* Interactive Chart / Raw JSON View Switcher */}
          {chartData && (
            <div className="ml-2 flex items-center bg-muted/20 p-0.5 rounded border border-border/40 text-[11px]">
              <button
                type="button"
                onClick={() => setViewMode("chart")}
                className={`flex items-center gap-1 px-2 py-0.5 rounded transition-all cursor-pointer ${
                  viewMode === "chart"
                    ? "bg-primary text-primary-foreground font-medium shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                }`}
                title="Render Interactive Chart"
              >
                <ChartIcon size={12} />
                <span>Chart</span>
              </button>
              <button
                type="button"
                onClick={() => setViewMode("code")}
                className={`flex items-center gap-1 px-2 py-0.5 rounded transition-all cursor-pointer ${
                  viewMode === "code"
                    ? "bg-primary text-primary-foreground font-medium shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                }`}
                title="View Raw Code"
              >
                <Code2 size={12} />
                <span>JSON</span>
              </button>
            </div>
          )}
        </div>

        <div className="flex items-center gap-1.5 flex-shrink-0">
          <button
            type="button"
            onClick={handleOpenInSandbox}
            className="flex items-center gap-1 px-2 py-1 rounded text-[11px] text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-all cursor-pointer"
            title="Open snippet in Workspace Sandbox"
          >
            <PanelRightOpen size={12} className="text-primary" />
            <span className="hidden sm:inline">Sandbox</span>
          </button>

          <button
            type="button"
            onClick={handleDownload}
            className="flex items-center gap-1 px-2 py-1 rounded text-[11px] text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-all cursor-pointer"
            title="Download Snippet"
          >
            <Download size={12} />
            <span className="hidden sm:inline">Save</span>
          </button>

          <button
            type="button"
            onClick={handleCopy}
            className="flex items-center gap-1 px-2.5 py-1 rounded text-[11px] font-medium bg-mist-950 hover:bg-muted text-muted-foreground hover:text-foreground transition-all cursor-pointer border border-border/50 shadow-peldrun-xs"
            title="Copy Code to Clipboard"
          >
            {copied ? (
              <>
                <Check size={12} className="text-peldrun-success" />
                <span className="text-peldrun-success font-mono">Copied</span>
              </>
            ) : (
              <>
                <Copy size={12} />
                <span className="font-mono">Copy</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Render Chart or Code Block based on current View Mode */}
      {chartData && viewMode === "chart" ? (
        <div className="p-3 bg-card/40 border-t border-border/40">
          <LazyChart
            title={chartData.title}
            type={chartData.type}
            unit={chartData.unit}
            data={chartData.data}
          />
        </div>
      ) : (
        <>
          <div
            className={`relative transition-all ${
              isLong && !isExpanded ? "max-h-[380px] overflow-y-auto" : "overflow-x-auto"
            }`}
          >
            <HighlightComponent
              theme={themes.nightOwl}
              code={cleanCode}
              language={normalizedLang}
            >
              {({
                className: highlightClass,
                style,
                tokens,
                getLineProps,
                getTokenProps,
              }: any) => (
                <pre
                  className={`${highlightClass} p-3.5 text-xs font-mono leading-relaxed bg-transparent m-0 min-w-full`}
                  style={style}
                >
                  {tokens.map((line: any, i: number) => (
                    <div key={i} {...getLineProps({ line })} className="table-row">
                      <span className="table-cell select-none pr-4 text-right text-[11px] font-mono opacity-35 w-8">
                        {i + 1}
                      </span>
                      <span className="table-cell whitespace-pre">
                        {line.map((token: any, key: number) => (
                          <span key={key} {...getTokenProps({ token })} />
                        ))}
                      </span>
                    </div>
                  ))}
                </pre>
              )}
            </HighlightComponent>
          </div>

          {isLong && (
            <div className="flex items-center justify-center p-1.5 border-t border-border/40 bg-[#161b22]/70">
              <button
                type="button"
                onClick={() => setIsExpanded(!isExpanded)}
                className="flex items-center gap-1.5 text-xs font-medium text-primary hover:underline transition-all cursor-pointer py-0.5"
              >
                {isExpanded ? (
                  <>
                    <ChevronUp size={13} />
                    <span>Collapse snippet</span>
                  </>
                ) : (
                  <>
                    <ChevronDown size={13} />
                    <span>Expand full view ({lines.length} lines)</span>
                  </>
                )}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default CodeBlock;
