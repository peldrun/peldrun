// Path: frontend/src/components/chat/markdown-renderer.tsx

"use client";

import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { CodeBlock } from "@/components/chat/code-block";

export interface MarkdownRendererProps {
  content: string;
  className?: string;
}

export function MarkdownRenderer({ content, className = "" }: MarkdownRendererProps) {
  if (!content) return null;

  return (
    <div className={`prose prose-sm dark:prose-invert max-w-none break-words leading-relaxed text-foreground ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={{
          code({ node, inline, className: codeClass, children, ...props }: any) {
            const match = /language-(\w+)/.exec(codeClass || "");
            const rawCode = String(children).replace(/\n$/, "");

            if (!inline && (match || rawCode.includes("\n"))) {
              return (
                <CodeBlock
                  code={rawCode}
                  language={match ? match[1] : "text"}
                />
              );
            }

            return (
              <code
                className="px-1.5 py-0.5 rounded-md bg-muted text-foreground font-mono text-[11px] border border-border/40 font-normal"
                {...props}
              >
                {children}
              </code>
            );
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
            return <thead className="bg-muted/70 text-foreground font-semibold border-b border-border">{children}</thead>;
          },
          th({ children }: any) {
            return <th className="px-3.5 py-2 text-xs font-semibold">{children}</th>;
          },
          td({ children }: any) {
            return <td className="px-3.5 py-2 border-t border-border/50 text-foreground/90">{children}</td>;
          },
          h1({ children }: any) {
            return <h1 className="text-base font-semibold font-heading text-foreground mt-4 mb-2">{children}</h1>;
          },
          h2({ children }: any) {
            return <h2 className="text-sm font-semibold font-heading text-foreground mt-3 mb-1.5">{children}</h2>;
          },
          h3({ children }: any) {
            return <h3 className="text-xs font-semibold font-heading text-foreground mt-2.5 mb-1">{children}</h3>;
          },
          p({ children }: any) {
            return <p className="mb-2 leading-relaxed text-xs">{children}</p>;
          },
          ul({ children }: any) {
            return <ul className="list-disc list-inside mb-2 space-y-1 text-xs">{children}</ul>;
          },
          ol({ children }: any) {
            return <ol className="list-decimal list-inside mb-2 space-y-1 text-xs">{children}</ol>;
          },
          blockquote({ children }: any) {
            return (
              <blockquote className="border-l-2 border-primary/60 pl-3 my-2 text-xs text-muted-foreground italic bg-muted/20 py-1 rounded-r-md">
                {children}
              </blockquote>
            );
          },
          // Smart link shortener with domain Favicon
          a({ href, children, ...props }: any) {
            if (!href) return <a {...props}>{children}</a>;

            let hostname = "";
            let displayLabel = children;
            const textContent = typeof children === "string" ? children : "";

            try {
              const parsedUrl = new URL(href);
              hostname = parsedUrl.hostname;

              // If the inner text is the raw URL itself, shorten it cleanly
              if (
                textContent.startsWith("http://") ||
                textContent.startsWith("https://") ||
                textContent === href
              ) {
                displayLabel = (parsedUrl.hostname + parsedUrl.pathname + parsedUrl.search).replace(/\/$/, "");
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
                className="inline-flex items-center gap-1.5 px-2 py-0.5 my-0.5 rounded-md bg-muted/60 hover:bg-muted text-primary hover:underline font-mono text-xs font-medium transition-all border border-border/50 align-baseline max-w-full"
                title={href}
                {...props}
              >
                <img
                  src={faviconUrl}
                  alt=""
                  width={13}
                  height={13}
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
        {content}
      </ReactMarkdown>
    </div>
  );
}

export default MarkdownRenderer;
