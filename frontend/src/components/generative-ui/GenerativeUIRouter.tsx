// frontend/src/components/generative-ui/GenerativeUIRouter.tsx
"use client";

import React, { useState, useEffect, useMemo } from "react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import { useGenerativeUIStore } from "./store";
import { OpenUIRenderer } from "./OpenUIRenderer";
import { GenerativeUIErrorBoundary } from "./ErrorBoundary";

interface GenerativeUIRouterProps {
  content: string;
}

type ContentSegment =
  | { type: "markdown"; content: string }
  | { type: "openui"; dslContent: string; rawContent: string };

const CLOSED_OPENUI_REGEX =
  /(?:```(?:xml|html|openui|markdown)?\s*)?(<(?:openui(?:-lang)?|ui|diagram|carousel|mathplot)\b[^>]*>)([\s\S]*?)<\/(?:openui(?:-lang)?|ui|diagram|carousel|mathplot)>(?:\s*```)?/gi;

const UNCLOSED_TAIL_REGEX =
  /(?:```(?:xml|html|openui|markdown)?\s*)?(<(?:openui(?:-lang)?|ui|diagram|carousel|mathplot)\b[^>]*>)([\s\S]*)$/i;

const STANDALONE_MERMAID_REGEX = /(?:^|\n)```mermaid\s*\n([\s\S]*?)\n```/gi;

function normalizeMarkdownTables(md: string): string {
  if (!md) return "";
  // Ensure a blank line before any markdown table that immediately follows text,
  // preventing CommonMark / remark-gfm from treating it as lazy paragraph text.
  return md.replace(/([^\n])\n(\s*\|[^\n]+\|\s*\n\s*\|[\s\-:|]+\|)/g, "$1\n\n$2");
}

export function GenerativeUIRouter({ content }: GenerativeUIRouterProps): React.JSX.Element {
  const [mounted, setMounted] = useState(false);
  const isEnabled = useGenerativeUIStore((s) => s.enabled);

  useEffect(() => {
    setMounted(true);
  }, []);

  const segments = useMemo<ContentSegment[]>(() => {
    if (!content) return [];
    if (mounted && !isEnabled) {
      return [{ type: "markdown", content: normalizeMarkdownTables(content) }];
    }

    const result: ContentSegment[] = [];
    let lastIndex = 0;

    CLOSED_OPENUI_REGEX.lastIndex = 0;
    let match: RegExpExecArray | null;

    while ((match = CLOSED_OPENUI_REGEX.exec(content)) !== null) {
      const matchStart = match.index;
      const matchEnd = match.index + match[0].length;

      if (matchStart > lastIndex) {
        const textBefore = content.slice(lastIndex, matchStart).trim();
        if (textBefore) {
          result.push({ type: "markdown", content: normalizeMarkdownTables(textBefore) });
        }
      }

      const dslContent = (match[2] || "").trim();
      result.push({
        type: "openui",
        dslContent,
        rawContent: match[0],
      });

      lastIndex = matchEnd;
    }

    if (lastIndex < content.length) {
      const tail = content.slice(lastIndex);
      const unclosedMatch = tail.match(UNCLOSED_TAIL_REGEX);

      if (unclosedMatch && typeof unclosedMatch.index === "number") {
        const textBeforeTail = tail.slice(0, unclosedMatch.index).trim();
        if (textBeforeTail) {
          result.push({ type: "markdown", content: normalizeMarkdownTables(textBeforeTail) });
        }
        const streamingDsl = (unclosedMatch[2] || "").trim();
        result.push({
          type: "openui",
          dslContent: streamingDsl,
          rawContent: unclosedMatch[0],
        });
      } else {
        const remainingText = tail.trim();
        if (remainingText) {
          result.push({ type: "markdown", content: normalizeMarkdownTables(remainingText) });
        }
      }
    }

    const finalSegments: ContentSegment[] = [];
    for (const seg of result) {
      if (seg.type === "markdown" && STANDALONE_MERMAID_REGEX.test(seg.content)) {
        STANDALONE_MERMAID_REGEX.lastIndex = 0;
        let mIdx = 0;
        let mMatch: RegExpExecArray | null;

        while ((mMatch = STANDALONE_MERMAID_REGEX.exec(seg.content)) !== null) {
          const beforeMermaid = seg.content.slice(mIdx, mMatch.index).trim();
          if (beforeMermaid) {
            finalSegments.push({ type: "markdown", content: normalizeMarkdownTables(beforeMermaid) });
          }

          const rawMermaid = (mMatch[1] || "").trim();
          finalSegments.push({
            type: "openui",
            dslContent: `Diagram: Workflow Diagram\n${rawMermaid}`,
            rawContent: mMatch[0],
          });
          mIdx = mMatch.index + mMatch[0].length;
        }

        const afterMermaid = seg.content.slice(mIdx).trim();
        if (afterMermaid) {
          finalSegments.push({ type: "markdown", content: normalizeMarkdownTables(afterMermaid) });
        }
      } else {
        finalSegments.push(seg);
      }
    }

    return finalSegments.length > 0
      ? finalSegments
      : [{ type: "markdown", content: normalizeMarkdownTables(content) }];
  }, [content, mounted, isEnabled]);

  return (
    <div className="space-y-3">
      {segments.map((seg, idx) => {
        if (seg.type === "markdown") {
          return <MarkdownRenderer key={`md-seg-${idx}`} content={seg.content} />;
        }

        return (
          <GenerativeUIErrorBoundary key={`ui-seg-${idx}`} fallbackContent={seg.rawContent}>
            <OpenUIRenderer dslContent={seg.dslContent} />
          </GenerativeUIErrorBoundary>
        );
      })}
    </div>
  );
}

export default GenerativeUIRouter;
