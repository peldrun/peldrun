// frontend/src/components/chat/chat-thread/ChatReasoningPanel.tsx
"use client";

import React, { useEffect, useRef, useState } from "react";
import { BrainCircuit, ChevronDown, ChevronRight, Sparkles } from "lucide-react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";

interface ChatReasoningPanelProps {
  /** Concatenated thought text (all accumulated deltas). */
  thoughtContent: string;
  /** True while the run is actively streaming. */
  isRunning: boolean;
}

/**
 * Collapsible reasoning panel for Chat mode.
 *
 * Renders the model's chain-of-thought as it streams, in a dedicated
 * collapsible block distinct from the main answer.
 *
 * Why a dedicated component (not ChatTimeline)
 * --------------------------------------------
 * ChatTimeline is built for the multi-step agent loop: it groups events
 * by step number, shows tool calls, artifacts, observations, and step
 * headers. None of that applies to Chat mode, which is a single
 * reasoning trace followed by a single answer.
 *
 * A dedicated component lets us:
 *   * Auto-expand while streaming, collapse after completion.
 *   * Show "Thinking…" with a pulse indicator while active.
 *   * Truncate at a sensible max-height (reasoning can be 10k+ chars).
 *   * Not pull in the entire agent-timeline visual language.
 */
export function ChatReasoningPanel({
  thoughtContent,
  isRunning,
}: ChatReasoningPanelProps) {
  const [isExpanded, setIsExpanded] = useState(true);
  const hasAutoCollapsedRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Auto-collapse once the run finishes, giving the user one final
  // look at the reasoning before it folds away. Re-opening is a click.
  // Only auto-collapse once per mount.
  useEffect(() => {
    if (!isRunning && !hasAutoCollapsedRef.current && thoughtContent) {
      const timer = setTimeout(() => {
        setIsExpanded(false);
        hasAutoCollapsedRef.current = true;
      }, 1500);
      return () => clearTimeout(timer);
    }
  }, [isRunning, thoughtContent]);

  // Auto-scroll to bottom while streaming so the newest tokens are
  // always visible.
  useEffect(() => {
    if (isRunning && isExpanded && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [thoughtContent, isRunning, isExpanded]);

  if (!thoughtContent || !thoughtContent.trim()) return null;

  const charCount = thoughtContent.length;

  return (
    <div className="border border-transparent rounded-md overflow-hidden bg-card/30 hover:border-muted hover:shadow-sm">
      <button
        type="button"
        onClick={() => {
          setIsExpanded((v) => !v);
          // Manual toggle freezes the auto-collapse forever.
          hasAutoCollapsedRef.current = true;
        }}
        className="w-full flex items-center justify-between px-3.5 py-2 border-muted/60 hover:bg-muted/90 transition-colors cursor-pointer text-xs"
      >
        <div className="flex items-center gap-2 min-w-0">
          <BrainCircuit size={14} className="text-peldrun-accent shrink-0" />
          <span className="font-semibold text-foreground">
            {isRunning ? "Thinking…" : "Reasoning"}
          </span>
          {isRunning && (
            <span className="inline-flex items-center gap-1 text-amber-500">
              <Sparkles size={10} className="animate-pulse" />
            </span>
          )}
          <span className="text-[10px] text-muted-foreground font-mono">
            ({charCount.toLocaleString()} chars)
          </span>
        </div>
        <span className="text-muted-foreground flex-shrink-0 ml-2">
          {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </span>
      </button>

      {isExpanded && (
        <div
          ref={scrollRef}
          className="px-3.5 py-2.5 border-t border-border/40 text-xs text-foreground/80 max-h-64 overflow-y-auto leading-relaxed"
        >
          <MarkdownRenderer content={thoughtContent} />
          {isRunning && (
            <span className="inline-block w-1.5 h-3 bg-primary animate-pulse ml-1 align-middle" />
          )}
        </div>
      )}
    </div>
  );
}

export default ChatReasoningPanel;
