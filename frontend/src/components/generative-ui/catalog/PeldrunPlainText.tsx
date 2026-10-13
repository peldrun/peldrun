// frontend/src/components/generative-ui/catalog/PeldrunPlainText.tsx
//
// Plain-text block renderer for the Markdown pipeline.
//
// Invoked by MarkdownRenderer.tsx for fenced code blocks tagged with `text`
// or for blocks with no language tag at all.
//
// The component is designed to render human-authored preformatted content
// such as cover pages, ASCII art, and layout mockups. It intentionally does
// not use a dark code-block background, and it preserves the visual layout
// the author wrote.
//
// Two layout modes are detected automatically:
//
//   - "decorated" mode: the content contains one or more lines made entirely
//     of `=`, `-`, or `*` characters. This pattern is typical of cover pages
//     and report headers. In this mode the block is rendered with generous
//     vertical padding and a centered, larger typographic feel.
//
//   - "plain" mode: everything else. The block is rendered as a compact
//     preformatted region.
//
// Users can force plain mode by using a language tag other than `text`
// (for example `plaintext` or `pre`).

"use client";

import React, { useMemo, useState } from "react";
import { Copy, Check, AlignLeft, FileText } from "lucide-react";

// ---------------------------------------------------------------------------
// Public props
// ---------------------------------------------------------------------------

export interface PeldrunPlainTextProps {
  /** Raw content of the code block. */
  source: string;

  /**
   * Language tag declared on the fenced block.
   * Used to influence the header label; not used for rendering.
   */
  language?: string;

  /**
   * When true, wraps long lines instead of allowing horizontal scroll.
   * Defaults to true.
   */
  wrap?: boolean;

  /**
   * When true, shows the header bar with the copy button.
   * Defaults to true.
   */
  showHeader?: boolean;
}

// ---------------------------------------------------------------------------
// Content analysis
// ---------------------------------------------------------------------------

/**
 * Detects whether the content looks like a decorative block (cover page,
 * report header, banner).
 *
 * A block is considered decorative when at least one full line is composed
 * entirely of three or more `=`, `-`, or `*` characters, ignoring trailing
 * whitespace. This heuristic matches the output pattern of the majority of
 * LLMs when they emit a cover page as text.
 */
function isDecoratedBlock(content: string): boolean {
  if (!content) return false;

  const lines = content.split(/\r?\n/);
  const decoratedLine = /^\s*([=\-*])\1{2,}\s*$/;

  return lines.some((line) => decoratedLine.test(line));
}

/**
 * Returns true when the content is visually centered (its longest line is
 * surrounded by significant leading whitespace on multiple lines).
 *
 * Used only to decide whether to apply a centered container style in
 * decorated mode. Plain mode ignores this.
 */
function looksCentered(content: string): boolean {
  const lines = content.split(/\r?\n/).filter((l) => l.trim().length > 0);
  if (lines.length < 3) return false;

  let centeredCount = 0;
  for (const line of lines) {
    const leading = line.length - line.trimStart().length;
    const trailing = line.length - line.trimEnd().length;
    if (leading > 2 && trailing > 2) {
      centeredCount += 1;
    }
  }

  return centeredCount / lines.length >= 0.5;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PeldrunPlainText({
  source,
  language,
  wrap = true,
  showHeader = true,
}: PeldrunPlainTextProps): React.JSX.Element {
  const [copied, setCopied] = useState(false);

  const content = useMemo(() => source.replace(/\r\n/g, "\n"), [source]);
  const isDecorated = useMemo(() => isDecoratedBlock(content), [content]);
  const isCentered = useMemo(
    () => isDecorated && looksCentered(content),
    [isDecorated, content]
  );

  const handleCopy = () => {
    navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // -------------------------------------------------------------------------
  // Empty content
  // -------------------------------------------------------------------------

  if (!content.trim()) {
    return (
      <div className="my-2 px-3 py-2 text-[11px] text-muted-foreground italic rounded-md border border-dashed border-border/60 bg-muted/10">
        Empty {language || "text"} block
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Body content
  // -------------------------------------------------------------------------

  const bodyClasses = [
    "text-foreground/90",
    "font-mono",
    isDecorated ? "text-[12.5px] leading-relaxed" : "text-[12px] leading-normal",
    wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre",
    isCentered ? "text-center" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const containerClasses = [
    "my-3",
    "rounded-xl",
    "border border-border/60",
    "bg-muted/15",
    "overflow-hidden",
    "transition-colors hover:border-border",
  ].join(" ");

  const bodyWrapperClasses = [
    "overflow-x-auto",
    isDecorated ? "px-6 py-5" : "px-4 py-3",
  ].join(" ");

  return (
    <div className={containerClasses}>
      {/* Optional header */}
      {showHeader && (
        <div className="flex items-center justify-between px-3.5 py-2 bg-muted/25 border-b border-border/40 text-[11px]">
          <div className="flex items-center gap-1.5 text-muted-foreground font-medium">
            {isDecorated ? (
              <AlignLeft size={12} className="text-primary/70" />
            ) : (
              <FileText size={12} className="text-muted-foreground" />
            )}
            <span className="font-mono">
              {isDecorated ? "layout" : language || "text"}
            </span>
          </div>
          <button
            type="button"
            onClick={handleCopy}
            title="Copy block content"
            className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
          >
            {copied ? (
              <Check size={12} className="text-emerald-500" />
            ) : (
              <Copy size={12} />
            )}
          </button>
        </div>
      )}

      {/* Body */}
      <div className={bodyWrapperClasses}>
        <pre className={bodyClasses}>{content}</pre>
      </div>
    </div>
  );
}

export default PeldrunPlainText;
