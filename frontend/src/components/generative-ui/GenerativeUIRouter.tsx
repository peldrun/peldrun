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

// Resilient Regex: Matches raw <openui>/<ui> blocks OR blocks enclosed in markdown code fences (```xml ... ```)
const OPENUI_BLOCK_REGEX = /(?:```(?:xml|html|openui|markdown)?\s*)?(<(?:openui(?:-lang)?|ui)\b[\s\S]*?>([\s\S]*?)<\/(?:openui(?:-lang)?|ui)>)(?:\s*```)?/i;

export function GenerativeUIRouter({ content }: GenerativeUIRouterProps): React.JSX.Element {
  const [mounted, setMounted] = useState(false);
  const isEnabled = useGenerativeUIStore((s) => s.enabled);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (mounted && !isEnabled) {
    return <MarkdownRenderer content={content} />;
  }

  const match = content.match(OPENUI_BLOCK_REGEX);
  if (!match) {
    return <MarkdownRenderer content={content} />;
  }

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
