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

// Resilient regex that matches <openui>, <openui-lang>, or <ui>, even when wrapped in markdown code blocks
const OPENUI_BLOCK_REGEX = /(?:```(?:xml|html|openui|markdown)?\s*)?<(?:openui(?:-lang)?|ui)[\s\S]*?>([\s\S]*?)<\/(?:openui(?:-lang)?|ui)>(?:\s*```)?/i;

export function GenerativeUIRouter({ content }: GenerativeUIRouterProps): React.JSX.Element {
  const [mounted, setMounted] = useState(false);
  const isEnabled = useGenerativeUIStore((s) => s.enabled);

  useEffect(() => {
    setMounted(true);
  }, []);

  // Only disable if explicitly mounted and toggled off by user in Settings
  if (mounted && !isEnabled) {
    return <MarkdownRenderer content={content} />;
  }

  // Intercept generative UI block
  const match = content.match(OPENUI_BLOCK_REGEX);
  if (!match) {
    return <MarkdownRenderer content={content} />;
  }

  const dslContent = match[1].trim();
  const matchIndex = match.index ?? 0;

  // Clean surrounding markdown text
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
