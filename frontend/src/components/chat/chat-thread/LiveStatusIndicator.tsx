"use client";

/**
 * Live running/paused indicator shown while the agent is executing or suspended.
 *
 * Displays a shimmering thinking loader when active, or a paused state indicator,
 * alongside the elapsed execution timer and operator lifecycle control buttons (Pause/Resume).
 * Purely presentational — state and actions are owned by the parent container.
 */

import React from "react";
import { Pause, Play } from "lucide-react";
import AIThinkingLoader from "@/components/AIThinkingLoader";

export interface LiveStatusIndicatorProps {
  /** Human-readable status message describing what the agent is doing now. */
  message: string;

  /** Elapsed seconds since the current run began. */
  elapsedSeconds: number;

  /** Whether the agent execution is currently paused. */
  isPaused?: boolean;

  /** Callback invoked when the operator pauses execution. */
  onPause?: () => void;

  /** Callback invoked when the operator resumes execution. */
  onResume?: () => void;
}

export function LiveStatusIndicator({
  message,
  elapsedSeconds,
  isPaused = false,
  onPause,
  onResume,
}: LiveStatusIndicatorProps) {
  return (
    <div className="flex items-center justify-between px-3.5 py-2.5 bg-muted/40 text-foreground transition-all rounded-md border border-border/40">
      <div className="flex items-center gap-2.5 text-xs">
        {isPaused ? (
          <div className="flex items-center gap-2 text-xs text-amber-500 font-medium">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500" />
            </span>
            <span className="font-mono text-[11px] text-foreground">
              {message || "Execution paused at step boundary"}
            </span>
          </div>
        ) : (
          <AIThinkingLoader
            message={message}
            color="blue"
            speed={5}
            textAnimation="shimmer"
            showDots={false}
          />
        )}
      </div>

      <div className="flex items-center gap-2">
        <span className="text-[11px] font-mono px-2 py-0.5 rounded-md bg-background border border-border text-muted-foreground">
          {elapsedSeconds}s
        </span>

        {isPaused ? (
          onResume && (
            <button
              type="button"
              onClick={onResume}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 text-[11px] font-mono font-medium cursor-pointer transition-colors shadow-sm"
              title="Resume Agent Execution"
            >
              <Play size={11} className="shrink-0 fill-current" />
              <span>Resume</span>
            </button>
          )
        ) : (
          onPause && (
            <button
              type="button"
              onClick={onPause}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-background hover:bg-muted text-muted-foreground hover:text-foreground border border-border text-[11px] font-mono font-medium cursor-pointer transition-colors shadow-sm"
              title="Pause Agent Execution"
            >
              <Pause size={11} className="shrink-0 text-amber-500" />
              <span>Pause</span>
            </button>
          )
        )}
      </div>
    </div>
  );
}

export default LiveStatusIndicator;
