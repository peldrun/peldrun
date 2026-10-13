// frontend/src/components/generative-ui/catalog/PeldrunPlainText.tsx
//
// Plain-text block renderer for the Markdown pipeline.
//
// Invoked by MarkdownRenderer.tsx for fenced code blocks tagged with `text`
// or for blocks with no language tag at all.
//
// The component renders human-authored preformatted content such as cover
// pages, ASCII art, directory trees, layout mockups, and pipe-delimited
// tables. It intentionally avoids a dark code-block background and preserves
// the visual layout the author wrote.
//
// Automatic layout detection (in priority order):
//
//   1. "table"   - the entire block is a pipe-delimited table with a
//                  separator row. Rendered as a real HTML <table>.
//
//   2. "diagram" - the content contains Unicode box-drawing characters
//                  (directory trees, flow charts, progress bars). Rendered
//                  with strict whitespace, no wrapping, and structural
//                  characters visually de-emphasised.
//
//   3. "banner"  - two or more fully decorated lines (`===`, `---`, `***`)
//                  with centered content. Typical of cover pages.
//
//   4. "layout"  - one or more decorated lines. Compact header style with
//                  generous vertical padding.
//
//   5. "plain"   - everything else. Compact preformatted region with
//                  trailing comment highlighting.
//
// Callers can force verbatim rendering by passing `raw`. Note that tabs are
// always expanded to four spaces for display, and line endings are always
// normalised to LF when copying.

"use client";

import React, {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlignCenter,
  AlignLeft,
  Check,
  Copy,
  FileText,
  MoveHorizontal,
  Table as TableIcon,
  Terminal,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Public props
// ---------------------------------------------------------------------------

export interface PeldrunPlainTextProps {
  /** Raw content of the code block. */
  source: string;

  /** Language tag declared on the fenced block. Used for the header label. */
  language?: string;

  /**
   * Wrap long lines. Defaults to true, but is auto-disabled for diagrams
   * where wrapping would destroy the layout.
   */
  wrap?: boolean;

  /** Show the header bar with the copy button. Defaults to true. */
  showHeader?: boolean;

  /** Disable every enhancement and render the block verbatim. */
  raw?: boolean;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Monospace stack chosen for reliable rendering of box-drawing characters.
 * Order matters: earlier fonts are preferred if installed.
 */
const MONO_FONT_STACK = [
  '"JetBrains Mono"',
  '"Fira Code"',
  '"Cascadia Code"',
  '"Cascadia Mono"',
  '"SF Mono"',
  '"Menlo"',
  '"Consolas"',
  '"DejaVu Sans Mono"',
  "monospace",
].join(", ");

/**
 * Unicode box-drawing (U+2500..U+257F) and block-element (U+2580..U+259F)
 * ranges. These cover the characters used by directory trees, flow charts,
 * and progress bars emitted by LLMs.
 */
const BOX_DRAWING_TOKEN = /([\u2500-\u257F\u2580-\u259F]+)/;
const HAS_BOX_DRAWING = /[\u2500-\u257F\u2580-\u259F]/;

/** A line composed entirely of a single decoration character. */
const DECORATED_LINE = /^\s*([=\-*_#~]|[\u2550\u2500\u2501])\1{2,}\s*$/;

/** Trailing line comment (`# ...` or `// ...`) preceded by two spaces. */
const TRAILING_COMMENT = /^(.*?)(\s{2,}(?:#|\/\/)\s.*)$/;

/** A pipe-delimited table row. */
const PIPE_ROW = /^\s*\|.*\|\s*$/;

/** A pipe-delimited separator row (`|---|---|`). */
const PIPE_SEPARATOR = /^\s*\|?[\s:|-]+\|?\s*$/;

// ---------------------------------------------------------------------------
// Content normalisation
// ---------------------------------------------------------------------------

/** Collapses CRLF and CR to a single LF. */
function normaliseLineEndings(value: string): string {
  return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

/** Expands tabs to a fixed number of spaces for stable alignment. */
function expandTabs(value: string, size = 4): string {
  if (!value.includes("\t")) return value;
  return value.replace(/\t/g, " ".repeat(size));
}

// ---------------------------------------------------------------------------
// Content analysis
// ---------------------------------------------------------------------------

/** True when at least one line is a full decoration rule. */
function hasDecoratedLine(content: string): boolean {
  return content.split("\n").some((line) => DECORATED_LINE.test(line));
}

/** True when two or more decoration rules are present (banner layout). */
function isBanner(content: string): boolean {
  let count = 0;
  for (const line of content.split("\n")) {
    if (DECORATED_LINE.test(line) && ++count >= 2) return true;
  }
  return false;
}

/** True when the majority of non-empty lines are visually centred. */
function isCentered(content: string): boolean {
  const lines = content.split("\n").filter((l) => l.trim().length > 0);
  if (lines.length < 3) return false;

  let centered = 0;
  for (const line of lines) {
    const leading = line.length - line.trimStart().length;
    const trailing = line.length - line.trimEnd().length;
    if (leading > 2 && trailing > 2) centered++;
  }
  return centered / lines.length >= 0.5;
}

/** True when the content contains Unicode box-drawing characters. */
function hasDiagram(content: string): boolean {
  return HAS_BOX_DRAWING.test(content);
}

export interface ParsedTable {
  headers: string[];
  rows: string[][];
}

/**
 * Detects a fully pipe-delimited table. Returns null when the content is not
 * a table, or when it is a table mixed with surrounding prose.
 */
function parseTable(content: string): ParsedTable | null {
  const lines = content.split("\n").filter((l) => l.trim().length > 0);
  if (lines.length < 3) return null;
  if (!lines.every((l) => PIPE_ROW.test(l))) return null;

  const separator = lines[1];
  if (!PIPE_SEPARATOR.test(separator) || !separator.includes("-")) return null;

  const splitRow = (row: string): string[] =>
    row
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((cell) => cell.trim());

  return {
    headers: splitRow(lines[0]),
    rows: lines.slice(2).map(splitRow),
  };
}

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

/**
 * Renders a single line with optional box-drawing de-emphasis and trailing
 * comment highlighting. A newline is always appended so the parent <pre>
 * preserves line breaks.
 */
function renderLine(
  line: string,
  key: number,
  colorizeBox: boolean
): React.ReactNode {
  const nodes: React.ReactNode[] = [];

  const commentMatch = line.match(TRAILING_COMMENT);
  const codePart = commentMatch ? commentMatch[1] : line;
  const commentPart = commentMatch ? commentMatch[2] : null;

  if (colorizeBox && HAS_BOX_DRAWING.test(codePart)) {
    const tokens = codePart.split(BOX_DRAWING_TOKEN);
    tokens.forEach((token, index) => {
      if (!token) return;
      if (HAS_BOX_DRAWING.test(token)) {
        nodes.push(
          <span key={`bx-${index}`} className="text-muted-foreground/40">
            {token}
          </span>
        );
      } else {
        nodes.push(<Fragment key={`tx-${index}`}>{token}</Fragment>);
      }
    });
  } else {
    nodes.push(<Fragment key="code">{codePart}</Fragment>);
  }

  if (commentPart) {
    nodes.push(
      <span key="cmt" className="text-muted-foreground/60 italic">
        {commentPart}
      </span>
    );
  }

  return (
    <Fragment key={key}>
      {nodes}
      {"\n"}
    </Fragment>
  );
}

// ---------------------------------------------------------------------------
// Table view
// ---------------------------------------------------------------------------

function AsciiTableView({ table }: { table: ParsedTable }): React.JSX.Element {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-[12px] border-collapse">
        <thead>
          <tr className="border-b border-border/60 bg-muted/20">
            {table.headers.map((header, index) => (
              <th
                key={index}
                className="text-left font-semibold text-foreground/90 px-3 py-1.5"
              >
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, rowIndex) => (
            <tr
              key={rowIndex}
              className="border-b border-border/25 last:border-b-0"
            >
              {row.map((cell, cellIndex) => (
                <td
                  key={cellIndex}
                  className="px-3 py-1.5 text-foreground/80 align-top"
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layout resolution
// ---------------------------------------------------------------------------

type LayoutMode = "table" | "diagram" | "banner" | "layout" | "plain";

interface LayoutInfo {
  mode: LayoutMode;
  wrapEffective: boolean;
  centered: boolean;
}

function resolveLayout(
  content: string,
  requestedWrap: boolean,
  raw: boolean
): LayoutInfo {
  if (raw) {
    return { mode: "plain", wrapEffective: requestedWrap, centered: false };
  }

  if (parseTable(content)) {
    return { mode: "table", wrapEffective: false, centered: false };
  }

  if (hasDiagram(content)) {
    return { mode: "diagram", wrapEffective: false, centered: false };
  }

  const banner = isBanner(content);
  const decorated = hasDecoratedLine(content);
  const centered = decorated && isCentered(content);

  if (banner) {
    return { mode: "banner", wrapEffective: requestedWrap, centered: true };
  }
  if (decorated) {
    return { mode: "layout", wrapEffective: requestedWrap, centered };
  }
  return { mode: "plain", wrapEffective: requestedWrap, centered: false };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PeldrunPlainText({
  source,
  language,
  wrap = true,
  showHeader = true,
  raw = false,
}: PeldrunPlainTextProps): React.JSX.Element {
  const [copied, setCopied] = useState(false);
  const copyTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (copyTimeoutRef.current !== null) {
        window.clearTimeout(copyTimeoutRef.current);
      }
    };
  }, []);

  /** Payload used for copying: line endings normalised, tabs preserved. */
  const copyContent = useMemo(() => normaliseLineEndings(source), [source]);

  /** Payload used for display: line endings normalised and tabs expanded. */
  const displayContent = useMemo(
    () => expandTabs(copyContent),
    [copyContent]
  );

  const layout = useMemo(
    () => resolveLayout(displayContent, wrap, raw),
    [displayContent, wrap, raw]
  );

  const table = useMemo(
    () => (layout.mode === "table" ? parseTable(displayContent) : null),
    [displayContent, layout.mode]
  );

  const handleCopy = useCallback(() => {
    void navigator.clipboard.writeText(copyContent).catch(() => undefined);
    setCopied(true);
    if (copyTimeoutRef.current !== null) {
      window.clearTimeout(copyTimeoutRef.current);
    }
    copyTimeoutRef.current = window.setTimeout(() => {
      setCopied(false);
      copyTimeoutRef.current = null;
    }, 2000);
  }, [copyContent]);

  // -------------------------------------------------------------------------
  // Empty content
  // -------------------------------------------------------------------------

  if (!displayContent.trim()) {
    return (
      <div className="my-2 px-3 py-2 text-[11px] text-muted-foreground italic rounded-md border border-dashed border-border/60 bg-muted/10">
        Empty {language || "text"} block
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Header label and icon
  // -------------------------------------------------------------------------

  const headerLabel = (() => {
    switch (layout.mode) {
      case "table":
        return "table";
      case "diagram":
        return "diagram";
      case "banner":
        return "banner";
      case "layout":
        return "layout";
      default:
        return language || "text";
    }
  })();

  const HeaderIcon = (() => {
    switch (layout.mode) {
      case "table":
        return TableIcon;
      case "diagram":
        return Terminal;
      case "banner":
        return AlignCenter;
      case "layout":
        return AlignLeft;
      default:
        return FileText;
    }
  })();

  // -------------------------------------------------------------------------
  // Body classes
  // -------------------------------------------------------------------------

  const isAiry = layout.mode === "banner" || layout.mode === "layout";

  const bodyClasses = [
    "text-foreground/90",
    "font-mono",
    isAiry ? "text-[12.5px] leading-relaxed" : "text-[12px] leading-normal",
    layout.wrapEffective ? "whitespace-pre-wrap break-words" : "whitespace-pre",
    layout.centered ? "text-center" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const bodyWrapperClasses = [
    "overflow-x-auto",
    isAiry ? "px-6 py-5" : "px-4 py-3",
  ].join(" ");

  const containerClasses = [
    "my-3",
    "rounded-xl",
    "border border-border/60",
    "bg-muted/15",
    "overflow-hidden",
    "transition-colors hover:border-border",
  ].join(" ");

  const preStyle: React.CSSProperties = {
    fontFamily: MONO_FONT_STACK,
    fontVariantLigatures: "none",
    fontKerning: "none",
    tabSize: 4,
  };

  // -------------------------------------------------------------------------
  // Body
  // -------------------------------------------------------------------------

  const renderedBody = (() => {
    if (table) {
      return <AsciiTableView table={table} />;
    }

    if (raw) {
      return (
        <pre className={bodyClasses} style={preStyle}>
          {displayContent}
        </pre>
      );
    }

    const colorizeBox = layout.mode === "diagram";
    const lines = displayContent.split("\n");

    return (
      <pre className={bodyClasses} style={preStyle}>
        {lines.map((line, index) => renderLine(line, index, colorizeBox))}
      </pre>
    );
  })();

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className={containerClasses}>
      {showHeader && (
        <div className="flex items-center justify-between px-3.5 py-2 bg-muted/25 border-b border-border/40 text-[11px]">
          <div className="flex items-center gap-1.5 text-muted-foreground font-medium">
            <HeaderIcon size={12} className="text-primary/70" />
            <span className="font-mono">{headerLabel}</span>
            {!layout.wrapEffective && wrap && !raw && (
              <span
                className="ml-1 inline-flex items-center text-muted-foreground/60"
                title="Horizontal scrolling enabled to preserve layout"
              >
                <MoveHorizontal size={11} />
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={handleCopy}
            aria-label="Copy block content"
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

      <div className={bodyWrapperClasses}>{renderedBody}</div>
    </div>
  );
}

export default PeldrunPlainText;
