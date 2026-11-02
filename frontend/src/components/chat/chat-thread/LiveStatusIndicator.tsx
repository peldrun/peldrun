"use client";

/**
 * Live running indicator shown while the agent is executing.
 *
 * Displays a shimmering thinking loader alongside the elapsed
 * execution timer. Stop is the only lifecycle control — the "Pause"
 * button was removed in Phase H because no LLM provider supports
 * true streaming pause semantics (LM Studio, GigaChat, OpenAI,
 * Anthropic, Ollama all keep generating internally even when the
 * client stops consuming chunks). The button was misleading and has
 * been removed in favour of a single, honest, universal control:
 * Stop (which closes the HTTP connection and genuinely terminates
 * generation).
 *
 * Purely presentational — state and actions are owned by the parent
 * container.
 */

import React from "react";
import AIThinkingLoader from "@/components/AIThinkingLoader";

export interface LiveStatusIndicatorProps {
  /** Human-readable status message describing what the agent is doing now. */
  message: string;

  /** Elapsed seconds since the current run began. */
  elapsedSeconds: number;
}

export function LiveStatusIndicator({
  message,
  elapsedSeconds,
}: LiveStatusIndicatorProps) {
  return (
    <div className="flex items-center justify-between px-3.5 py-2.5 bg-muted/40 text-foreground transition-all rounded-md border border-border/40">
      <div className="flex items-center gap-2.5 text-xs">
        <AIThinkingLoader
          message={message}
          color="blue"
          speed={5}
          textAnimation="shimmer"
          showDots={false}
        />
      </div>

      <div className="flex items-center gap-2">
        <span className="text-[11px] font-mono px-2 py-0.5 rounded-md bg-background border border-border text-muted-foreground">
          {elapsedSeconds}s
        </span>
      </div>
    </div>
  );
}

export default LiveStatusIndicator;
