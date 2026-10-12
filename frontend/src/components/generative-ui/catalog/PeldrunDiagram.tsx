// frontend/src/components/generative-ui/catalog/PeldrunDiagram.tsx
"use client";

import React, { useEffect, useRef, useState, useId } from "react";
import mermaid from "mermaid";
import { GitFork, AlertCircle, Copy, Check } from "lucide-react";
import { PeldrunMotionWrapper } from "./PeldrunMotionWrapper";

interface PeldrunDiagramProps {
  code: string;
  title?: string;
}

/**
 * Sanitizes SVG markup to prevent script execution and cross-site scripting (XSS).
 */
function sanitizeSvg(rawSvg: string): string {
  if (typeof window === "undefined" || !rawSvg) {
    return rawSvg;
  }

  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(rawSvg, "image/svg+xml");

    // Strip dangerous active elements
    const blockedTags = ["script", "foreignobject", "iframe", "object", "embed"];
    blockedTags.forEach((tagName) => {
      const elements = doc.querySelectorAll(tagName);
      elements.forEach((el) => el.remove());
    });

    // Strip inline event listeners and dangerous URI protocols
    const allElements = doc.querySelectorAll("*");
    allElements.forEach((el) => {
      const attributes = Array.from(el.attributes);
      for (const attr of attributes) {
        const attrName = attr.name.toLowerCase();
        const attrValue = attr.value.toLowerCase().trim();

        if (attrName.startsWith("on") || attrValue.startsWith("javascript:") || attrValue.startsWith("data:text/html")) {
          el.removeAttribute(attr.name);
        }
      }
    });

    return new XMLSerializer().serializeToString(doc.documentElement);
  } catch (err) {
    console.warn("[PeldrunDiagram] SVG sanitization fallback applied:", err);
    return "";
  }
}

export function PeldrunDiagram({ code, title }: PeldrunDiagramProps): React.JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  const [svgHtml, setSvgHtml] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const uniqueId = useId().replace(/:/g, "_");

  useEffect(() => {
    let isMounted = true;
    const originalParseError = mermaid.parseError;

    // Remove leftover error artifacts injected globally by mermaid
    const cleanupGlobalMermaidErrors = () => {
      if (typeof document === "undefined") return;
      const danglingErrors = document.querySelectorAll(
        `div[id^="dmermaid_svg_"], div.mermaid-error, svg[id^="mermaid-svg-"][aria-roledescription="error"]`
      );
      danglingErrors.forEach((el) => {
        if (el.parentNode === document.body) {
          el.remove();
        }
      });
    };

    async function renderMermaid() {
      if (!code.trim()) return;

      try {
        const isDark = document.documentElement.classList.contains("dark");

        mermaid.initialize({
          startOnLoad: false,
          suppressErrorRendering: true,
          theme: isDark ? "dark" : "neutral",
          securityLevel: "strict",
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          themeVariables: {
            darkMode: isDark,
            background: isDark ? "#0f172a" : "#ffffff",
            primaryColor: isDark ? "#3b82f6" : "#2563eb",
            primaryTextColor: isDark ? "#f8fafc" : "#0f172a",
            lineColor: isDark ? "#64748b" : "#94a3b8",
          },
        });

        // Intercept parser errors locally without polluting global callbacks permanently
        mermaid.parseError = (err: any) => {
          if (isMounted) {
            setError(typeof err === "string" ? err : err?.message || "Invalid diagram syntax.");
          }
          cleanupGlobalMermaidErrors();
        };

        const cleanCode = code
          .replace(/^```(?:mermaid)?/i, "")
          .replace(/```$/i, "")
          .trim();

        const renderId = `mermaid_svg_${uniqueId}_${Date.now().toString(36)}`;
        const { svg } = await mermaid.render(renderId, cleanCode);

        cleanupGlobalMermaidErrors();

        if (isMounted) {
          const sanitized = sanitizeSvg(svg);
          setSvgHtml(sanitized);
          setError(null);
        }
      } catch (err: any) {
        cleanupGlobalMermaidErrors();
        if (isMounted) {
          setError(err?.message || "Failed to render diagram syntax.");
        }
      }
    }

    renderMermaid();

    return () => {
      isMounted = false;
      mermaid.parseError = originalParseError;
      cleanupGlobalMermaidErrors();
    };
  }, [code, uniqueId]);

  const handleCopyCode = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <PeldrunMotionWrapper className="my-2.5">
      <div className="rounded-xl border border-border/80 bg-card/60 backdrop-blur-sm overflow-hidden shadow-xs transition-all hover:border-border">
        {/* Header Bar */}
        <div className="flex items-center justify-between px-3.5 py-2.5 bg-muted/30 border-b border-border/50 text-xs">
          <div className="flex items-center gap-2 font-medium text-foreground">
            <GitFork size={14} className="text-primary shrink-0" />
            <span className="font-semibold tracking-tight">
              {title || "Workflow & Logic Diagram"}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={handleCopyCode}
              title="Copy Diagram DSL"
              className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
            >
              {copied ? <Check size={13} className="text-emerald-500" /> : <Copy size={13} />}
            </button>
          </div>
        </div>

        {/* Content Canvas */}
        <div className="p-4 overflow-x-auto flex items-center justify-center min-h-[140px] bg-background/30">
          {error ? (
            <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive text-xs flex items-start gap-2.5 max-w-md w-full">
              <AlertCircle size={15} className="shrink-0 mt-0.5" />
              <div className="space-y-1">
                <span className="font-semibold block">Diagram Syntax Error</span>
                <p className="text-[11px] opacity-90 font-mono whitespace-pre-wrap">{error}</p>
              </div>
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
              <span>Rendering visual diagram...</span>
            </div>
          )}
        </div>
      </div>
    </PeldrunMotionWrapper>
  );
}

export default PeldrunDiagram;
