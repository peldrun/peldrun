// Path: frontend/src/components/chat/chat-thread/LiveStatusIndicator.tsx
"use client";

/**
 * Live running indicator shown while the agent is actively executing.
 *
 * Displays a shimmering thinking loader with the current live status
 * message on the left, and the elapsed time in seconds on the right.
 * Purely presentational — all state is owned by the parent container.
 */

import React from "react";
import AIThinkingLoader from "@/components/AIThinkingLoader";

export interface LiveStatusIndicatorProps {
  /** Human-readable status message describing what the agent is doing now. */
  message: string;

  /** Elapsed seconds since the current run began. */
  elapsedSeconds: number;
}

export function LiveStatusIndicator({ message, elapsedSeconds }: LiveStatusIndicatorProps) {
  return (
    <div className="flex items-center justify-between px-3.5 py-2.5 bg-muted/40 text-foreground transition-all">
      <div className="flex items-center gap-2.5 text-xs">
        <AIThinkingLoader
          message={message}
          color="blue"
          speed={5}
          textAnimation="shimmer"
          showDots={false}
        />
      </div>
      <span className="text-[11px] font-mono px-2 py-0.5 rounded-md bg-background border border-border text-muted-foreground">
        {elapsedSeconds}s
      </span>
    </div>
  );
}

export default LiveStatusIndicator;
