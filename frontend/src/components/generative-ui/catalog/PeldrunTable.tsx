// frontend/src/components/generative-ui/catalog/PeldrunTable.tsx
"use client";

import React from "react";
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
    <div className="my-2 space-y-1.5">
      {caption && <div className="text-[11px] font-medium text-muted-foreground px-1">{caption}</div>}
      <div className="overflow-x-auto rounded-lg border border-border/70 bg-background/50">
        <table className="w-full text-left text-xs border-collapse">
          <thead>
            <tr className="border-b border-border bg-muted/40 text-muted-foreground font-mono text-[11px]">
              {headers.map((h, idx) => (
                <th key={idx} className="py-2 px-3 font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border/40">
            {displayedRows.map((row, rIdx) => (
              <tr key={rIdx} className="hover:bg-muted/20 transition-colors">
                {row.map((cell, cIdx) => (
                  <td key={cIdx} className="py-2 px-3 text-foreground whitespace-nowrap">
                    {String(cell)}
                  </td>
                ))}
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
