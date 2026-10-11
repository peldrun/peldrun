// frontend/src/components/generative-ui/catalog/PeldrunDiagram.tsx
"use client";

import React, { useEffect, useRef, useState, useId } from "react";
import mermaid from "mermaid";
import { GitFork, AlertCircle, Copy, Check, Maximize2 } from "lucide-react";
import { PeldrunMotionWrapper } from "./PeldrunMotionWrapper";

interface PeldrunDiagramProps {
  code: string;
  title?: string;
}

export function PeldrunDiagram({ code, title }: PeldrunDiagramProps): React.JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  const [svgHtml, setSvgHtml] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const uniqueId = useId().replace(/:/g, "_");

  useEffect(() => {
    let isMounted = true;

    async function renderMermaid() {
      if (!code.trim()) return;

      try {
        const isDark = document.documentElement.classList.contains("dark");
        mermaid.initialize({
          startOnLoad: false,
          theme: isDark ? "dark" : "neutral",
          securityLevel: "loose",
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          themeVariables: {
            darkMode: isDark,
            background: isDark ? "#0f172a" : "#ffffff",
            primaryColor: isDark ? "#3b82f6" : "#2563eb",
            primaryTextColor: isDark ? "#f8fafc" : "#0f172a",
            lineColor: isDark ? "#64748b" : "#94a3b8",
          },
        });

        const cleanCode = code
          .replace(/^```(?:mermaid)?/i, "")
          .replace(/```$/i, "")
          .trim();

        const renderId = `mermaid_svg_${uniqueId}_${Date.now().toString(36)}`;
        const { svg } = await mermaid.render(renderId, cleanCode);

        if (isMounted) {
          setSvgHtml(svg);
          setError(null);
        }
      } catch (err: any) {
        if (isMounted) {
          console.warn("[PeldrunDiagram] Mermaid parse failure:", err);
          setError(err?.message || "Failed to render diagram syntax.");
        }
      }
    }

    renderMermaid();

    return () => {
      isMounted = false;
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
