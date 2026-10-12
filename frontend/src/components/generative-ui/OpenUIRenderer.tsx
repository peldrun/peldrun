// frontend/src/components/generative-ui/OpenUIRenderer.tsx
"use client";

import React, { useMemo } from "react";
import { Renderer } from "@openuidev/react-lang";
import { peldrunLibrary } from "./peldrun-library";
import { useGenerativeUIStore } from "./store";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";

interface OpenUIRendererProps {
  dslContent: string;
}



/**
 * Strips outer markdown code fences and redundant tags.
 */
function sanitizeDslSource(source: string): string {
  if (!source) return "";
  return source
    .replace(/^```(?:openui|xml|html)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .replace(/<\/?(?:openui(?:-lang)?|ui)\b[^>]*>/gi, "")
    .trim();
}

/**
 * Defensive auto-recovery: If an LLM defines component variables but omits
 * the mandatory 'root = Stack(...)', synthesize the root assignment automatically.
 */
function ensureRootEntrypoint(dsl: string): string {
  if (!dsl) return "";
  const trimmed = dsl.trim();
  if (/^root\s*=/m.test(trimmed)) {
    return trimmed;
  }

  const assignmentRegex =
    /^([a-zA-Z0-9_]+)\s*=\s*(?:Stack|Grid|Card|Metric|Chart|Table|Diagram|MathPlot|Alert|Badge|Progress|Carousel|Accordion|TestResults|ActionButton|Form)\(/gm;
  const matches = Array.from(trimmed.matchAll(assignmentRegex));

  if (matches.length > 0) {
    const ids = matches.map((m) => m[1]);
    return `root = Stack([${ids.join(", ")}])\n${trimmed}`;
  }

  return trimmed;
}

export function OpenUIRenderer({ dslContent }: OpenUIRendererProps): React.JSX.Element {
  const isEnabled = useGenerativeUIStore(
    (s) => (s as any).enabled ?? (s as any).isEnabled ?? true
  );

  const cleanSource = useMemo(() => {
    return sanitizeDslSource(dslContent);
  }, [dslContent]);

  const preparedSource = useMemo(() => {
    return ensureRootEntrypoint(cleanSource);
  }, [cleanSource]);

  // When Generative UI is disabled in settings, render as markdown prose
  if (isEnabled === false) {
    return (
      <div className="my-2 leading-relaxed">
        <MarkdownRenderer content={cleanSource} />
      </div>
    );
  }

  // Display pulse placeholder during early streaming before code arrives
  if (!cleanSource) {
    return (
      <div className="my-2.5 rounded-xl border border-border/80 bg-card/50 p-6 flex items-center justify-center gap-2 text-xs text-muted-foreground animate-pulse">
        <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span>Synthesizing interface...</span>
      </div>
    );
  }

  return (
    <div className="w-full my-1.5 openui-container">
      <Renderer library={peldrunLibrary} response={preparedSource} />
    </div>
  );
}

export default OpenUIRenderer;
