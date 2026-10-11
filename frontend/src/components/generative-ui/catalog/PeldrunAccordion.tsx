// frontend/src/components/generative-ui/catalog/PeldrunAccordion.tsx
"use client";

import React, { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import type { PeldrunAccordionProps } from "../types";

export function PeldrunAccordion({
  items = [],
}: PeldrunAccordionProps): React.JSX.Element {
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);

  const toggle = (idx: number) => {
    setExpandedIndex(expandedIndex === idx ? null : idx);
  };

  return (
    <div className="my-2 rounded-xl border border-border/70 overflow-hidden divide-y divide-border/60">
      {items.map((item, idx) => {
        const isExpanded = expandedIndex === idx;
        return (
          <div key={`acc-${idx}`} className="bg-card/40">
            <button
              type="button"
              onClick={() => toggle(idx)}
              className="w-full flex items-center justify-between p-3 text-left text-xs font-medium text-foreground hover:bg-muted/30 transition-colors cursor-pointer"
            >
              <span>{item.title}</span>
              {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </button>
            {isExpanded && (
              <div className="p-3 pt-0 text-xs text-muted-foreground leading-relaxed">
                <MarkdownRenderer content={item.content} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default PeldrunAccordion;
