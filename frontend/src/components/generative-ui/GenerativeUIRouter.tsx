// frontend/src/components/generative-ui/GenerativeUIRouter.tsx
"use client";

import React from "react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import { useGenerativeUIStore } from "./store";
import { OpenUIRenderer } from "./OpenUIRenderer";
import { GenerativeUIErrorBoundary } from "./ErrorBoundary";

interface GenerativeUIRouterProps {
  content: string;
}

const OPENUI_BLOCK_REGEX = /<openui(?:-lang)?[\s\S]*?>([\s\S]*?)<\/openui(?:-lang)?>/i;

export const GenerativeUIRouter: React.FC<GenerativeUIRouterProps> = ({ content }) => {
  const isEnabled = useGenerativeUIStore((s) => s.enabled);

  // Invariant 1: If disabled in settings, return MarkdownRenderer directly (Zero Overhead)
  if (!isEnabled) {
    return <MarkdownRenderer content={content} />;
  }

  // Invariant 2: If no OpenUI tags found, return standard MarkdownRenderer
  const match = content.match(OPENUI_BLOCK_REGEX);
  if (!match) {
    return <MarkdownRenderer content={content} />;
  }

  const dslContent = match[1].trim();
  const beforeBlock = content.slice(0, match.index).trim();
  const afterBlock = content.slice((match.index || 0) + match[0].length).trim();

  return (
    <div className="space-y-2">
      {beforeBlock && <MarkdownRenderer content={beforeBlock} />}
      <GenerativeUIErrorBoundary fallbackContent={content}>
        <OpenUIRenderer dslContent={dslContent} />
      </GenerativeUIErrorBoundary>
      {afterBlock && <MarkdownRenderer content={afterBlock} />}
    </div>
  );
};
