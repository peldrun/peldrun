// frontend/src/components/chat/markdown-renderer.tsx
//
// Unified Markdown renderer for chat and agent messages.
//
// Performance:
//   The exported component is wrapped in React.memo so a parent re-render
//   (for example, a 1-second elapsed timer in the chat container) does not
//   re-run KaTeX or Mermaid when the message content is unchanged. Without
//   this wrapper, every timer tick would re-process every message and flood
//   the console with spurious KaTeX warnings.
//
// Dispatch:
//   ```mermaid    -> PeldrunDiagram
//   ```chart      -> PeldrunChartBlock
//   ```text       -> PeldrunPlainText
//   ```<language> -> CodeBlock
//   no tag        -> PeldrunPlainText
//
// LaTeX:
//   \[...\] and \(...\) delimiters are normalized to the $$...$$ and $...$
//   forms that remark-math v5+ recognizes. KaTeX is configured with
//   strict: "ignore" to suppress false-positive warnings on patterns such
//   as "12.4\%" at the end of inline math.

"use client";

import React, { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

import { CodeBlock } from "@/components/chat/code-block";
import { useGenerativeUIStore } from "@/components/generative-ui/store";
import { PeldrunDiagram } from "@/components/generative-ui/catalog/PeldrunDiagram";
import { PeldrunChartBlock } from "@/components/generative-ui/catalog/PeldrunChartBlock";
import { PeldrunPlainText } from "@/components/generative-ui/catalog/PeldrunPlainText";

// ---------------------------------------------------------------------------
// Public props
// ---------------------------------------------------------------------------

export interface MarkdownRendererProps {
  content: string;
  className?: string;
}

// ---------------------------------------------------------------------------
// Content preprocessing
// ---------------------------------------------------------------------------

/**
 * Normalizes LaTeX delimiters emitted by LLMs.
 *
 * remark-math v5+ only recognizes $...$ (inline) and $$...$$ (display).
 * Legacy LaTeX delimiters \(...\) and \[...\] are translated into the
 * dollar-based forms so KaTeX can render them.
 */
function normalizeMathDelimiters(source: string): string {
  if (!source) return source;

  return source
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, body: string) => `$$${body}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, body: string) => `$${body}$`);
}

/**
 * Ensures that a GFM table is preceded by a blank line.
 */
function normalizeMarkdownTables(source: string): string {
  if (!source) return source;
  return source.replace(
    /([^\n])\n(\s*\|[^\n]+\|\s*\n\s*\|[\s\-:|]+\|)/g,
    "$1\n\n$2"
  );
}

// ---------------------------------------------------------------------------
// Code block language detection
// ---------------------------------------------------------------------------

function canonicalizeLanguage(raw: string | undefined): string {
  if (!raw) return "";
  const lower = raw.toLowerCase().trim();
  const aliases: Record<string, string> = {
    mermaid: "mermaid",
    mmd: "mermaid",
    chart: "chart",
    json_chart: "chart",
    text: "text",
    plaintext: "text",
    plain: "text",
    pre: "text",
    sh: "bash",
    shell: "bash",
    zsh: "bash",
    ts: "typescript",
    tsx: "tsx",
    js: "javascript",
    jsx: "jsx",
    py: "python",
    rb: "ruby",
    yml: "yaml",
  };
  return aliases[lower] ?? lower;
}

// ---------------------------------------------------------------------------
// Inner component (unexported, memoized below)
// ---------------------------------------------------------------------------

function MarkdownRendererInner({
  content,
  className = "",
}: MarkdownRendererProps): React.JSX.Element | null {
  const [mounted, setMounted] = useState(false);
  const features = useGenerativeUIStore((s) => s.features);
  const enabled = useGenerativeUIStore((s) => s.enabled);

  useEffect(() => {
    setMounted(true);
  }, []);

  const effectiveFeatures = mounted
    ? features
    : { mermaid: true, latex: true, charts: true, code_highlighting: true };

  const effectiveEnabled = mounted ? enabled : true;

  const processed = useMemo(() => {
    if (!content) return "";
    let out = content;
    if (effectiveEnabled && effectiveFeatures.latex) {
      out = normalizeMathDelimiters(out);
    }
    out = normalizeMarkdownTables(out);
    return out;
  }, [content, effectiveEnabled, effectiveFeatures.latex]);

  const remarkPlugins = useMemo(() => {
    const plugins: any[] = [remarkGfm];
    if (effectiveEnabled && effectiveFeatures.latex) {
      plugins.push(remarkMath);
    }
    return plugins;
  }, [effectiveEnabled, effectiveFeatures.latex]);

  const rehypePlugins = useMemo(() => {
    const plugins: any[] = [];
    if (effectiveEnabled && effectiveFeatures.latex) {
      // Pass KaTeX options explicitly.
      //   strict: "ignore"   -> suppress the "% has no terminating newline"
      //                         false positives on patterns like "12.4\%".
      //   throwOnError: false -> render invalid LaTeX as red text instead
      //                          of throwing an exception.
      //   output: "html"     -> skip MathML generation, roughly halving
      //                          the produced DOM size.
      plugins.push([
        rehypeKatex,
        {
          strict: "ignore",
          throwOnError: false,
          output: "html",
        },
      ]);
    }
    return plugins;
  }, [effectiveEnabled, effectiveFeatures.latex]);

  if (!processed) return null;

  return (
    <div
      className={`prose prose-sm dark:prose-invert max-w-none break-words leading-relaxed text-foreground ${className}`}
    >
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={rehypePlugins}
        components={{
          code({ node, className: codeClass, children, ...props }: any) {
            const match = /language-(\w+)/.exec(codeClass || "");
            const language = canonicalizeLanguage(match?.[1]);
            const rawCode = String(children).replace(/\n$/, "");
            const parentTag = (node as any)?.tagName;
            const isBlock =
              Boolean(match) ||
              rawCode.includes("\n") ||
              parentTag === "pre";

            if (!isBlock) {
              return (
                <code
                  className="px-1.5 py-0.5 rounded-md bg-muted text-foreground font-mono text-[11px] border border-border/40 font-normal"
                  {...props}
                >
                  {children}
                </code>
              );
            }

            if (language === "mermaid") {
              if (effectiveEnabled && effectiveFeatures.mermaid) {
                return <PeldrunDiagram code={rawCode} />;
              }
              return <CodeBlock code={rawCode} language="text" />;
            }

            if (language === "chart") {
              if (effectiveEnabled && effectiveFeatures.charts) {
                return <PeldrunChartBlock source={rawCode} />;
              }
              return <CodeBlock code={rawCode} language="json" />;
            }

            if (language === "text" || !language) {
              return <PeldrunPlainText source={rawCode} language={language} />;
            }

            if (effectiveEnabled && effectiveFeatures.code_highlighting) {
              return <CodeBlock code={rawCode} language={language} />;
            }
            return <CodeBlock code={rawCode} language="text" />;
          },

          pre({ children }: any) {
            return <>{children}</>;
          },

          table({ children }: any) {
            return (
              <div className="my-3 overflow-x-auto rounded-sm border border-border bg-card/60 shadow-peldrun-xs">
                <table className="w-full border-collapse text-xs text-left">
                  {children}
                </table>
              </div>
            );
          },

          thead({ children }: any) {
            return (
              <thead className="bg-muted/70 text-foreground font-semibold border-b border-border">
                {children}
              </thead>
            );
          },

          th({ children }: any) {
            return (
              <th className="px-3.5 py-2 text-sm font-semibold">{children}</th>
            );
          },

          td({ children }: any) {
            return (
              <td className="px-3.5 py-2 border-t border-border/50 text-foreground/90 text-sm">
                {children}
              </td>
            );
          },

          h1({ children }: any) {
            return (
              <h1 className="text-2xl font-semibold font-heading text-foreground mt-4 mb-2">
                {children}
              </h1>
            );
          },

          h2({ children }: any) {
            return (
              <h2 className="text-xl font-semibold font-heading text-foreground mt-3 mb-1.5">
                {children}
              </h2>
            );
          },

          h3({ children }: any) {
            return (
              <h3 className="text-lg font-semibold font-heading text-foreground mt-2.5 mb-1">
                {children}
              </h3>
            );
          },

          p({ children }: any) {
            return <p className="mb-2 leading-relaxed text-sm">{children}</p>;
          },

          ul({ children }: any) {
            return (
              <ul className="list-disc list-inside mb-2 space-y-1 text-sm">
                {children}
              </ul>
            );
          },

          ol({ children }: any) {
            return (
              <ol className="list-decimal list-inside mb-2 space-y-1 text-sm">
                {children}
              </ol>
            );
          },

          blockquote({ children }: any) {
            return (
              <blockquote className="border-l-2 border-primary/60 pl-3 my-2 text-sm text-muted-foreground italic bg-muted/20 py-1 rounded-r-md">
                {children}
              </blockquote>
            );
          },

          a({ href, children, ...props }: any) {
            if (!href) return <a {...props}>{children}</a>;

            let hostname = "";
            let displayLabel = children;
            const textContent = typeof children === "string" ? children : "";

            try {
              const parsedUrl = new URL(href);
              hostname = parsedUrl.hostname;

              if (
                textContent.startsWith("http://") ||
                textContent.startsWith("https://") ||
                textContent === href
              ) {
                displayLabel = (
                  parsedUrl.hostname +
                  parsedUrl.pathname +
                  parsedUrl.search
                ).replace(/\/$/, "");
              }
            } catch {
              return (
                <a
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary hover:underline font-medium transition-all"
                  {...props}
                >
                  {children}
                </a>
              );
            }

            const faviconUrl = `https://www.google.com/s2/favicons?domain=${hostname}&sz=32`;

            return (
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                referrerPolicy="no-referrer"
                className="inline-flex items-center gap-1.5 px-2 py-0.5 my-0.5 rounded-md bg-muted/60 hover:bg-muted text-primary hover:underline font-mono text-xs font-medium transition-all border border-border/50 align-baseline max-w-full"
                title={href}
                {...props}
              >
                <img
                  src={faviconUrl}
                  alt=""
                  width={13}
                  height={13}
                  referrerPolicy="no-referrer"
                  className="w-3.5 h-3.5 shrink-0 rounded-xs inline-block"
                  onError={(e) => {
                    (e.currentTarget as HTMLElement).style.display = "none";
                  }}
                />
                <span className="truncate max-w-[280px]">{displayLabel}</span>
              </a>
            );
          },
        }}
      >
        {processed}
      </ReactMarkdown>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Memoized export
// ---------------------------------------------------------------------------

/**
 * Custom comparator.
 *
 * Only re-render when the message content or the layout class changes.
 * Feature flags are read from the store *inside* the component, so an
 * external change to them will still reach the component via its own
 * subscription — the comparator does not need to inspect them.
 *
 * This is the single most important performance guard in the chat view:
 * without it, the elapsed-timer tick that fires once per second re-runs
 * KaTeX on every message in the conversation.
 */
function markdownRendererPropsAreEqual(
  prev: MarkdownRendererProps,
  next: MarkdownRendererProps
): boolean {
  return (
    prev.content === next.content && prev.className === next.className
  );
}

export const MarkdownRenderer = React.memo(
  MarkdownRendererInner,
  markdownRendererPropsAreEqual
);

MarkdownRenderer.displayName = "MarkdownRenderer";

export default MarkdownRenderer;
