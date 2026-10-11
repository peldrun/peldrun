// frontend/src/components/generative-ui/catalog/PeldrunTable.tsx
"use client";

import React from "react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import type { PeldrunTableProps } from "../types";

const MAX_DISPLAY_ROWS = 15;

export function PeldrunTable({
  headers,
  rows = [],
  caption,
}: PeldrunTableProps): React.JSX.Element {
  const displayedRows = rows.slice(0, MAX_DISPLAY_ROWS);
  const isTruncated = rows.length > MAX_DISPLAY_ROWS;

  return (
    <div className="my-2.5 space-y-1.5 font-sans">
      {caption && (
        <div className="text-[11px] font-semibold text-muted-foreground px-1 tracking-tight">
          {caption}
        </div>
      )}
      <div className="overflow-x-auto rounded-xl border border-border/80 bg-card/50 shadow-xs">
        <table className="w-full text-left text-xs border-collapse">
          <thead>
            <tr className="border-b border-border/80 bg-muted/50 text-foreground font-mono text-[11px]">
              {headers.map((h, idx) => (
                <th key={`th-${idx}`} className="py-2.5 px-3 font-semibold tracking-tight">
                  <MarkdownRenderer
                    content={h}
                    className="[&_p]:mb-0 [&_p]:text-xs [&_p]:font-semibold [&_p]:text-foreground"
                  />
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border/40">
            {displayedRows.map((row, rIdx) => (
              <tr key={`tr-${rIdx}`} className="hover:bg-muted/20 transition-colors">
                {headers.map((_, cIdx) => {
                  const cell = row[cIdx] !== undefined ? String(row[cIdx]) : "";
                  return (
                    <td
                      key={`td-${rIdx}-${cIdx}`}
                      className="py-2.5 px-3 text-foreground align-top text-xs leading-relaxed break-words"
                    >
                      <MarkdownRenderer
                        content={cell}
                        className="[&_p]:mb-0 [&_p]:text-xs [&_p]:leading-relaxed"
                      />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {isTruncated && (
        <div className="text-[10px] text-muted-foreground font-mono px-1">
          Showing top {MAX_DISPLAY_ROWS} of {rows.length} rows. Inspect artifacts for full dataset.
        </div>
      )}
    </div>
  );
}

export default PeldrunTable;
