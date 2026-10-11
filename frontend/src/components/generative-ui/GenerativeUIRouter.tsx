// frontend/src/components/generative-ui/GenerativeUIRouter.tsx
"use client";

import React, { useState, useEffect } from "react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import { useGenerativeUIStore } from "./store";
import { OpenUIRenderer } from "./OpenUIRenderer";
import { GenerativeUIErrorBoundary } from "./ErrorBoundary";

interface GenerativeUIRouterProps {
  content: string;
}

// Resilient Regex: Matches raw or code-fenced <openui>, <ui>, <diagram>, <carousel>, or <mathplot> blocks
const OPENUI_BLOCK_REGEX = /(?:```(?:xml|html|openui|markdown)?\s*)?(<(?:openui(?:-lang)?|ui|diagram|carousel|mathplot)\b[\s\S]*?>([\s\S]*?)<\/(?:openui(?:-lang)?|ui|diagram|carousel|mathplot)>)(?:\s*```)?/i;

// Direct Mermaid Code Block Detection (e.g. ```mermaid ... ```)
const STANDALONE_MERMAID_REGEX = /(?:^|\n)```mermaid\s*\n([\s\S]*?)\n```/i;

export function GenerativeUIRouter({ content }: GenerativeUIRouterProps): React.JSX.Element {
  const [mounted, setMounted] = useState(false);
  const isEnabled = useGenerativeUIStore((s) => s.enabled);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (mounted && !isEnabled) {
    return <MarkdownRenderer content={content} />;
  }

  // 1. Check for standard generative UI container
  const match = content.match(OPENUI_BLOCK_REGEX);
  if (match) {
    const dslContent = (match[2] || "").trim();
    const matchIndex = match.index ?? 0;

    const beforeBlock = content.slice(0, matchIndex).trim();
    const afterBlock = content.slice(matchIndex + match[0].length).trim();

    return (
      <div className="space-y-3">
        {beforeBlock && <MarkdownRenderer content={beforeBlock} />}
        <GenerativeUIErrorBoundary fallbackContent={content}>
          <OpenUIRenderer dslContent={dslContent} />
        </GenerativeUIErrorBoundary>
        {afterBlock && <MarkdownRenderer content={afterBlock} />}
      </div>
    );
  }

  // 2. Intercept standalone Mermaid blocks if emitted outside <openui>
  const mermaidMatch = content.match(STANDALONE_MERMAID_REGEX);
  if (mermaidMatch) {
    const rawMermaid = (mermaidMatch[1] || "").trim();
    const matchIndex = mermaidMatch.index ?? 0;

    const beforeBlock = content.slice(0, matchIndex).trim();
    const afterBlock = content.slice(matchIndex + mermaidMatch[0].length).trim();
    const synthesizedDsl = `Diagram: Workflow Diagram\n${rawMermaid}`;

    return (
      <div className="space-y-3">
        {beforeBlock && <MarkdownRenderer content={beforeBlock} />}
        <GenerativeUIErrorBoundary fallbackContent={content}>
          <OpenUIRenderer dslContent={synthesizedDsl} />
        </GenerativeUIErrorBoundary>
        {afterBlock && <MarkdownRenderer content={afterBlock} />}
      </div>
    );
  }

  return <MarkdownRenderer content={content} />;
}
