// frontend/src/components/generative-ui/catalog/PeldrunDiagram.tsx
//
// Mermaid diagram renderer for the Markdown pipeline.
//
// ============================================================================
// ARCHITECTURAL NOTES
// ============================================================================
//
// 1. Hidden render container
//    -----------------------
//    Mermaid's `render(id, text, container?)` API accepts an optional third
//    parameter. When provided, Mermaid renders into that container instead
//    of creating a temporary div in document.body. This is the only reliable
//    way to prevent the bottom-left loading flash that otherwise appears
//    during every render cycle.
//
//    We allocate one hidden container per component instance, attach it to
//    document.body (Mermaid needs it in the live document to measure text),
//    and remove it on unmount.
//
// 2. Node label rendering
//    --------------------
//    Mermaid v10+ renders node labels inside <foreignObject> elements by
//    default. Our SVG sanitizer must preserve <foreignObject> while removing
//    its executable content, otherwise every node would appear as an empty
//    box. The sanitizer below:
//
//      - Removes <script>, <iframe>, <object>, <embed> entirely.
//      - Strips event-handler attributes (onclick, onload, ...).
//      - Removes javascript:, data:text/html, and vbscript: URIs.
//      - Keeps <foreignObject> and its non-executable children intact.
//
//    We do NOT set htmlLabels on any diagram type. Mermaid v11+ removed
//    that option from several configs (sequence, er, journey) and
//    TypeScript rejects it on those types. The sanitizer handles both
//    native <text> rendering and <foreignObject> rendering correctly, so
//    there is no need to force either mode.
//
// 3. Theme customization
//    -------------------
//    Only the palettes that Mermaid's built-in themes render poorly are
//    customized (xyChart, gantt). All other variables fall through to the
//    built-in "neutral" (light) or "dark" theme, which produces readable
//    output by default. Overriding mainBkg, nodeBorder, primaryTextColor,
//    or textColor caused text to blend into node backgrounds in earlier
//    versions.
//
// 4. Telemetry
//    ---------
//    Store actions are read through refs so the telemetry effect never
//    re-runs due to action identity changes. A "recorded source" ref
//    ensures a single diagram instance reports at most one event per
//    unique source string, even across re-renders.
//
// 5. Retry strategy
//    --------------
//    Two attempts are made:
//      1. The normalized source.
//      2. A simplified variant (graph -> flowchart, style lines removed).
//    If both fail, the error is surfaced with a collapsible source preview.

"use client";

import React, { useEffect, useRef, useState, useId, useMemo } from "react";
import mermaid from "mermaid";
import { GitFork, AlertCircle, Copy, Check, Code2 } from "lucide-react";

import { useGenerativeUIStore } from "../store";

// ---------------------------------------------------------------------------
// Public props
// ---------------------------------------------------------------------------

export interface PeldrunDiagramProps {
  /** Raw Mermaid source, already stripped of fences by the caller. */
  code: string;

  /** Optional title shown in the header. */
  title?: string;

  /**
   * When true, records a telemetry event on render failure or success.
   * Defaults to true. Set to false for dry-run rendering.
   */
  telemetry?: boolean;
}

// ---------------------------------------------------------------------------
// Source normalization
// ---------------------------------------------------------------------------

/**
 * Reverses escape sequences that may have been introduced when the raw
 * diagram source crossed a JSON, OpenUI, or streaming boundary.
 *
 * Mermaid requires real newlines; a literal backslash-n collapses the
 * entire graph into a single line and triggers "Lexical error on line 1".
 */
function normalizeMermaidSource(raw: string): string {
  if (!raw) return "";

  return raw
    .replace(/^```(?:mermaid)?\s*/i, "")
    .replace(/\s*```\s*$/i, "")
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "  ")
    .replace(/\\"/g, '"')
    .replace(/\\'/g, "'")
    .replace(/\\\\/g, "\\")
    .replace(/\r\n/g, "\n")
    .trim();
}

/**
 * Produces a simplified variant of a diagram for retry attempts.
 *
 * Common strict-parser failures:
 *   - `graph` directive rejected in favor of `flowchart`.
 *   - Custom `style` / `classDef` / `linkStyle` lines.
 *   - A trailing blank line inside the source.
 */
function simplifyMermaidSource(source: string): string {
  if (!source) return "";

  return source
    .replace(/^\s*graph\s+(LR|RL|TD|TB|BT)\b/m, "flowchart $1")
    .replace(/^\s*style\s+.+$/gm, "")
    .replace(/^\s*classDef\s+.+$/gm, "")
    .replace(/^\s*class\s+.+$/gm, "")
    .replace(/^\s*linkStyle\s+.+$/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/**
 * Level 2 fallback: builds on `simplifyMermaidSource` by removing constructs
 * that frequently break Mermaid's strict parser:
 *
 *   - `subgraph ... end` blocks (nesting is fragile)
 *   - Edge labels (`-->|label|`, `-- label -->`) — kept the edges, dropped labels
 *
 * Node labels and shapes are preserved, so the diagram remains readable.
 */
function aggressivelySimplifyMermaidSource(source: string): string {
  if (!source) return "";

  let out = simplifyMermaidSource(source);

  // Drop single-level subgraph blocks (nested subgraphs are rare in LLM output
  // and any nesting left behind will simply fail into the next strategy).
  out = out.replace(/^\s*subgraph\s+[^\n]*$/gm, "");
  out = out.replace(/^\s*end\s*$/gm, "");

  // Strip edge labels but keep the edges:
  //   A -->|label| B      => A --> B
  //   A -- label --> B    => A --> B
  //   A -. label .-> B    => A -.-> B
  out = out.replace(/(-->|---|-\.->|==>)\s*\|[^|\n]*\|/g, "$1");
  out = out.replace(/\s--\s[^-][^\n]*?\s+-->/g, " -->");

  return out.replace(/\n{2,}/g, "\n").trim();
}

/**
 * Level 3 fallback: last-resort structural extraction. Keeps only the
 * diagram directive and the edges between node IDs. Every node label, shape,
 * and style is removed.
 *
 * The result renders a "bare" topology (nodes as raw IDs, edges connected).
 * This sacrifices readability for a chance to at least display the structure
 * when the original source is malformed beyond repair.
 */
function minimalMermaidSource(source: string): string {
  if (!source) return "";

  let out = aggressivelySimplifyMermaidSource(source);

  // Collapse node shapes to bare IDs — order matters, longer forms first.
  //   A([Label])    => A
  //   A[[Label]]    => A
  //   A[(Label)]    => A
  //   A((Label))    => A
  //   A[Label]      => A
  //   A(Label)      => A
  //   A{Label}      => A
  out = out.replace(/([A-Za-z0-9_-]+)\s*\(\[.*?\]\)/g, "$1");
  out = out.replace(/([A-Za-z0-9_-]+)\s*\[\[.*?\]\]/g, "$1");
  out = out.replace(/([A-Za-z0-9_-]+)\s*\[\(.*?\)\]/g, "$1");
  out = out.replace(/([A-Za-z0-9_-]+)\s*\(\(.*?\)\)/g, "$1");
  out = out.replace(/([A-Za-z0-9_-]+)\s*\[.*?\]/g, "$1");
  out = out.replace(/([A-Za-z0-9_-]+)\s*\(.*?\)/g, "$1");
  out = out.replace(/([A-Za-z0-9_-]+)\s*\{.*?\}/g, "$1");

  return out.trim();
}

/**
 * Ordered fallback strategies for Mermaid rendering. The renderer runs the
 * first `min(max_retries + 1, length)` of them, skipping strategies whose
 * output matches the previous candidate (avoids duplicate work).
 */
const MERMAID_FALLBACK_STRATEGIES: ReadonlyArray<(src: string) => string> = [
  (src) => src,
  simplifyMermaidSource,
  aggressivelySimplifyMermaidSource,
  minimalMermaidSource,
];

// ---------------------------------------------------------------------------
// Hidden render container
// ---------------------------------------------------------------------------

/**
 * Creates a hidden, off-screen container attached to document.body.
 *
 * Mermaid requires the container to be part of the live document so it can
 * measure text and compute layout. The container is visually hidden by
 * being positioned far outside the viewport with zero size.
 */
function createHiddenContainer(): HTMLDivElement {
  const container = document.createElement("div");
  container.setAttribute("data-peldrun-mermaid-host", "true");
  container.setAttribute("aria-hidden", "true");
  container.style.position = "fixed";
  container.style.left = "-100000px";
  container.style.top = "-100000px";
  container.style.width = "0";
  container.style.height = "0";
  container.style.overflow = "hidden";
  container.style.pointerEvents = "none";
  container.style.visibility = "hidden";
  container.style.zIndex = "-2147483647";
  document.body.appendChild(container);
  return container;
}

// ---------------------------------------------------------------------------
// SVG sanitization
// ---------------------------------------------------------------------------

/**
 * Sanitizes generated SVG markup to prevent script execution and XSS.
 *
 * Design rationale:
 *   Mermaid v10+ renders node labels inside <foreignObject> elements by
 *   default. A naive sanitizer that removes foreignObject entirely would
 *   strip all node text and leave the diagram showing only empty boxes.
 *
 *   This sanitizer keeps <foreignObject> while removing its executable
 *   content:
 *
 *     - Removes <script>, <iframe>, <object>, <embed> entirely.
 *     - Strips event-handler attributes (onclick, onload, ...).
 *     - Removes javascript:, data:text/html, and vbscript: URIs.
 *     - Keeps <foreignObject> and its non-executable children intact.
 *
 *   The same attribute-level checks are applied to every element in the
 *   tree, so no path into the DOM can carry executable content.
 */
function sanitizeSvg(rawSvg: string): string {
  if (typeof window === "undefined" || !rawSvg) {
    return rawSvg;
  }

  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(rawSvg, "image/svg+xml");

    // Elements that are always removed, wherever they appear.
    const alwaysBlocked = ["script", "iframe", "object", "embed"];
    alwaysBlocked.forEach((tagName) => {
      doc.querySelectorAll(tagName).forEach((el) => el.remove());
    });

    /**
     * Removes dangerous attributes from a single element.
     * Applied to every element in the SVG tree.
     */
    const sanitizeElementAttributes = (el: Element) => {
      const attributes = Array.from(el.attributes);
      for (const attr of attributes) {
        const attrName = attr.name.toLowerCase();
        const attrValue = attr.value.trim().toLowerCase();

        const isEventHandler = attrName.startsWith("on");
        const isDangerousUri =
          attrValue.startsWith("javascript:") ||
          attrValue.startsWith("data:text/html") ||
          attrValue.startsWith("vbscript:");

        if (isEventHandler || isDangerousUri) {
          el.removeAttribute(attr.name);
        }
      }
    };

    doc.querySelectorAll("*").forEach(sanitizeElementAttributes);

    return new XMLSerializer().serializeToString(doc.documentElement);
  } catch (err) {
    console.warn("[PeldrunDiagram] SVG sanitization failed:", err);
    return "";
  }
}

// ---------------------------------------------------------------------------
// Theme configuration
// ---------------------------------------------------------------------------

/**
 * Builds the Mermaid themeVariables object for the current color scheme.
 *
 * Only the palettes that Mermaid's built-in themes render poorly are
 * customized:
 *
 *   - xyChart: the default line color is nearly invisible on a light
 *     background, so explicit axis, tick, and title colors are set.
 *
 *   - gantt: default section and task colors clash with our palette, so
 *     explicit task colors are provided.
 *
 * Everything else falls back to Mermaid's built-in "neutral" (light) or
 * "dark" theme. Overriding mainBkg, nodeBorder, primaryTextColor,
 * textColor, or clusterBkg caused text to blend into node backgrounds in
 * earlier versions of this file.
 */
function buildThemeVariables(isDark: boolean): Record<string, unknown> {
  return {
    darkMode: isDark,

    xyChart: {
      backgroundColor: isDark ? "#0f172a" : "#ffffff",
      titleColor: isDark ? "#f8fafc" : "#0f172a",
      xAxisLabelColor: isDark ? "#cbd5e1" : "#334155",
      xAxisTitleColor: isDark ? "#cbd5e1" : "#334155",
      xAxisTickColor: isDark ? "#94a3b8" : "#475569",
      xAxisLineColor: isDark ? "#64748b" : "#94a3b8",
      yAxisLabelColor: isDark ? "#cbd5e1" : "#334155",
      yAxisTitleColor: isDark ? "#cbd5e1" : "#334155",
      yAxisTickColor: isDark ? "#94a3b8" : "#475569",
      yAxisLineColor: isDark ? "#64748b" : "#94a3b8",
      plotBorderColor: isDark ? "#475569" : "#cbd5e1",
    },

    gantt: {
      primaryColor: isDark ? "#1e293b" : "#f1f5f9",
      sectionBkgColor: isDark ? "#1e293b" : "#e2e8f0",
      sectionBkgColor2: isDark ? "#334155" : "#f1f5f9",
      altSectionBkgColor: isDark ? "#0f172a" : "#f8fafc",
      taskBkgColor: isDark ? "#3b82f6" : "#2563eb",
      taskTextColor: isDark ? "#f8fafc" : "#ffffff",
      taskTextLightColor: isDark ? "#e2e8f0" : "#1e293b",
      taskBorderColor: isDark ? "#60a5fa" : "#1d4ed8",
      doneTaskBkgColor: isDark ? "#475569" : "#94a3b8",
      activeTaskBkgColor: isDark ? "#10b981" : "#059669",
      activeTaskBorderColor: isDark ? "#34d399" : "#047857",
      critBkgColor: isDark ? "#ef4444" : "#dc2626",
      critBorderColor: isDark ? "#f87171" : "#b91c1c",
      gridColor: isDark ? "#334155" : "#cbd5e1",
    },
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PeldrunDiagram({
  code,
  title,
  telemetry = true,
}: PeldrunDiagramProps): React.JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [svgHtml, setSvgHtml] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [showSource, setShowSource] = useState(false);

  // [FIX-TELEMETRY-2] Previously captured actions via useRef at mount time.
  // If the store module is re-instantiated (HMR, or persist rehydration),
  // the captured reference kept mutating the OLD store instance, so counters
  // incremented there were invisible to components reading the NEW store.
  // Reading `getState()` at call time always hits the current instance.
  // (The refs are removed entirely — no need to hold references.)

  // Ensures a single diagram instance reports at most one telemetry event
  // per unique source string, even across re-renders.
  const recordedSourceRef = useRef<string | null>(null);

  const reactId = useId().replace(/:/g, "_");

  // [FIX-MAX-RETRIES] Subscribe to the user's retry budget so the number of
  // render attempts reflects the setting in the Settings panel. Previously
  // the renderer always attempted exactly two strategies regardless of the
  // configured value.
  const maxRetries = useGenerativeUIStore((s) => s.max_retries);

  const normalizedSource = useMemo(() => normalizeMermaidSource(code), [code]);

  // -------------------------------------------------------------------------
  // Hidden host container lifecycle
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (typeof document === "undefined") return;

    if (!hostRef.current) {
      hostRef.current = createHiddenContainer();
    }

    return () => {
      if (hostRef.current && hostRef.current.parentNode) {
        hostRef.current.parentNode.removeChild(hostRef.current);
      }
      hostRef.current = null;
    };
  }, []);

  // -------------------------------------------------------------------------
  // Render effect
  // -------------------------------------------------------------------------
  useEffect(() => {
    let isMounted = true;

    async function renderDiagram(): Promise<void> {
      if (!normalizedSource) {
        if (isMounted) {
          setSvgHtml("");
          setError(null);
        }
        return;
      }

      const host = hostRef.current;
      if (!host) {
        // The host container has not been created yet. This can happen on
        // the very first render pass before the layout effect fires.
        return;
      }

      const reportSuccess = (latencyMs: number = 0) => {
        if (!telemetry) return;
        if (recordedSourceRef.current === normalizedSource) return;
        recordedSourceRef.current = normalizedSource;
        useGenerativeUIStore.getState().recordSuccess(latencyMs);
      };

      const reportError = () => {
        if (!telemetry) return;
        if (recordedSourceRef.current === normalizedSource) return;
        recordedSourceRef.current = normalizedSource;
        useGenerativeUIStore.getState().recordError("mermaid");
      };

      try {
        const isDark =
          typeof document !== "undefined" &&
          document.documentElement.classList.contains("dark");

        // Mermaid v11+ removed `htmlLabels` from several diagram configs
        // (sequence, er, journey) and TypeScript rejects the option on those
        // types. The sanitizer in this file preserves <foreignObject>
        // elements and only strips executable content, so node labels remain
        // visible regardless of which rendering mode Mermaid chooses.
        mermaid.initialize({
          startOnLoad: false,
          suppressErrorRendering: true,
          // [FIX-DIAGRAM-COLORS] "neutral" is Mermaid's deliberately
          // desaturated theme (grayscale nodes, muted borders) intended for
          // print. It made every diagram look like a wireframe.
          //
          // "default" (light) / "dark" (dark) use proper pastel palettes
          // for node backgrounds, contrasting borders, and colored edges.
          //
          // The custom themeVariables below only override `xyChart` and
          // `gantt` — flowchart / sequence / state / class / ER diagrams
          // inherit the full built-in palette and render with real colors.
          theme: isDark ? "dark" : "default",
          securityLevel: "strict",
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          themeVariables: buildThemeVariables(isDark),
          gantt: { useMaxWidth: true },
          journey: { useMaxWidth: true },
        });

        // [FIX-MAX-RETRIES] Build the attempt list from `max_retries`.
        //
        //   max_retries = 0  →  [original]                       (1 attempt)
        //   max_retries = 1  →  [original, simplified]           (2 attempts)
        //   max_retries = 2  →  [original, simp, aggressive]     (3 attempts)
        //   max_retries ≥ 3  →  all 4 strategies                 (4 attempts)
        //
        // Values above 3 saturate at the number of available strategies —
        // beyond that there is nothing left to simplify, so extra retries
        // would just repeat the previous candidate.
        const attemptCap = Math.min(
          Math.max(0, maxRetries) + 1,
          MERMAID_FALLBACK_STRATEGIES.length
        );
        const attempts: string[] = [];
        let previousCandidate: string | null = null;
        for (let i = 0; i < attemptCap; i += 1) {
          const candidate = MERMAID_FALLBACK_STRATEGIES[i](normalizedSource);
          if (!candidate || candidate === previousCandidate) continue;
          attempts.push(candidate);
          previousCandidate = candidate;
        }

        let lastError: unknown = null;

        for (let i = 0; i < attempts.length; i += 1) {
          const candidate = attempts[i];
          const renderId = `peldrun_mermaid_${reactId}_${i}`;

          // Clear the host before each attempt. We never let Mermaid
          // create its own container in document.body.
          while (host.firstChild) {
            host.removeChild(host.firstChild);
          }

          try {
            const _renderStartedAt =
              typeof performance !== "undefined" ? performance.now() : 0;
            const { svg } = await mermaid.render(renderId, candidate, host);
            const _renderDurationMs =
              typeof performance !== "undefined"
                ? Math.round(performance.now() - _renderStartedAt)
                : 0;

            if (isMounted) {
              const sanitized = sanitizeSvg(svg);
              setSvgHtml(sanitized);
              setError(null);
            }
            reportSuccess(_renderDurationMs);
            return;
          } catch (attemptErr) {
            lastError = attemptErr;
            while (host.firstChild) {
              host.removeChild(host.firstChild);
            }
          }
        }

        if (isMounted) {
          const message =
            (lastError as { message?: string } | null)?.message ||
            (typeof lastError === "string" ? lastError : null) ||
            "Failed to render diagram syntax.";
          setError(message);
          setSvgHtml("");
        }
        reportError();
      } catch (err) {
        if (isMounted) {
          const message =
            (err as { message?: string } | null)?.message ||
            "Unexpected error while rendering diagram.";
          setError(message);
          setSvgHtml("");
        }
        reportError();
      }
    }

    renderDiagram();

    return () => {
      isMounted = false;
    };
  }, [normalizedSource, maxRetries, reactId, telemetry]);

  const handleCopyCode = () => {
    navigator.clipboard.writeText(normalizedSource);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="my-3 rounded-xl border border-border/80 bg-card/60 backdrop-blur-sm overflow-hidden shadow-xs transition-colors hover:border-border">
      {/* Header */}
      <div className="flex items-center justify-between px-3.5 py-2.5 bg-muted/30 border-b border-border/50 text-xs">
        <div className="flex items-center gap-2 font-medium text-foreground">
          <GitFork size={14} className="text-primary shrink-0" />
          <span className="font-semibold tracking-tight">
            {title || "Diagram"}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setShowSource((v) => !v)}
            title={showSource ? "Show rendered diagram" : "Show Mermaid source"}
            className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors flex items-center gap-1 text-[11px]"
          >
            <Code2 size={13} />
            <span>{showSource ? "Diagram" : "Source"}</span>
          </button>
          <button
            type="button"
            onClick={handleCopyCode}
            title="Copy Mermaid source"
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

      {/* Body */}
      <div className="p-4 overflow-x-auto flex items-center justify-center min-h-[140px] bg-background/30">
        {showSource ? (
          <pre className="w-full max-w-full text-[11px] font-mono text-muted-foreground bg-background/60 border border-border/50 rounded-lg p-3 overflow-auto whitespace-pre-wrap">
            {normalizedSource}
          </pre>
        ) : error ? (
          <div className="w-full max-w-md space-y-2">
            <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive text-xs flex items-start gap-2.5">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              <div className="space-y-1">
                <span className="font-semibold block">Diagram Syntax Error</span>
                <p className="text-[11px] opacity-90 font-mono whitespace-pre-wrap">
                  {error}
                </p>
              </div>
            </div>
            <details className="text-[11px] text-muted-foreground">
              <summary className="cursor-pointer hover:text-foreground transition-colors">
                View Mermaid source
              </summary>
              <pre className="mt-2 p-2.5 rounded-md bg-muted/40 border border-border/50 font-mono whitespace-pre-wrap overflow-auto max-h-48">
                {normalizedSource}
              </pre>
            </details>
          </div>
        ) : svgHtml ? (
          <div
            ref={containerRef}
            className="w-full flex justify-center [&_svg]:max-w-full [&_svg]:h-auto"
            dangerouslySetInnerHTML={{ __html: svgHtml }}
          />
        ) : (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="animate-spin w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full" />
            <span>Rendering diagram...</span>
          </div>
        )}
      </div>
    </div>
  );
}

export default PeldrunDiagram;
