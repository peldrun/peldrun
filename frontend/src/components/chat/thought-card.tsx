"use client";

import React, { useState } from "react";
import { ChevronRight, ChevronDown, BrainCircuit } from "lucide-react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import { UsageBadgeGroup } from "@/components/chat/UsageBadges";
import type { TokenUsageInfo } from "@/lib/types";

interface ThoughtCardProps {
  thought: string;
  isStreaming?: boolean;
  usage?: TokenUsageInfo | null;
  costUsd?: number | null;
  latencyMs?: number | null;
  ttftMs?: number | null;
  tokensPerSecond?: number | null;
  provider?: string | null;
  model?: string | null;
}

export function ThoughtCard({
  thought,
  isStreaming,
  usage,
  costUsd,
  latencyMs,
  ttftMs,
  tokensPerSecond,
  provider,
  model,
}: ThoughtCardProps) {
  const [isOpen, setIsOpen] = useState(false);

  if (!thought && !isStreaming) return null;

  const hasMetrics = Boolean(
    usage || costUsd !== undefined || latencyMs !== undefined || tokensPerSecond !== undefined
  );

  return (
    <div className="my-1.5 rounded-sm border border-border bg-card/60 text-xs overflow-hidden shadow-peldrun-xs transition-all">
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between px-3.5 py-2.5 text-muted-foreground hover:text-foreground hover:bg-muted/40 transition-colors cursor-pointer select-none font-sans"
      >
        <div className="flex items-center gap-2 truncate">
          <BrainCircuit size={14} className="text-peldrun-accent flex-shrink-0" />
          <span className="text-xs font-semibold text-foreground">Agent Reasoning</span>
          {!isOpen && thought && (
            <span className="text-[11px] text-muted-foreground truncate max-w-sm font-normal">
              — {thought.slice(0, 65).replace(/\n/g, " ")}...
            </span>
          )}
          {isStreaming && (
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-peldrun-accent animate-pulse flex-shrink-0" />
          )}
        </div>
        <div className="text-muted-foreground pl-2 flex-shrink-0">
          {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </div>
      </button>

      {isOpen && (
        <div className="px-4 py-3 border-t border-border/60 bg-background/50 leading-relaxed text-xs space-y-2">
          {thought ? (
            <MarkdownRenderer content={thought} />
          ) : (
            <span className="italic text-muted-foreground">Thinking...</span>
          )}

          {hasMetrics && (
            <div className="pt-2 border-t border-border/40 flex items-center justify-between">
              <UsageBadgeGroup
                usage={usage}
                costUsd={costUsd}
                latencyMs={latencyMs}
                ttftMs={ttftMs}
                tokensPerSecond={tokensPerSecond}
                provider={provider}
                model={model}
                variant="card"
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default ThoughtCard;