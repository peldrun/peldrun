"use client";

import React from "react";
import { FileText, ExternalLink, Activity, Check, Copy, Bot, AlertCircle, RotateCcw } from "lucide-react";
import { UsageBadgeGroup } from "@/components/chat/UsageBadges";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";

interface ChatDeliverableProps {
  finalResult: string;
  status?: "completed" | "failed" | "stopped" | "paused";
  turnIndex?: number;
  isTurnRawTrace?: boolean;
  lastThoughtContent?: string;
  producedFiles?: { name: string; path: string; action?: string; status?: string }[];
  onSelectFile: (fileName: string) => void;
  copiedSection: string | null;
  onCopy: (text: string, identifier: string) => void;
  execMode?: "agent" | "chat";
  showRawTrace: boolean;
  setShowRawTrace: (show: boolean) => void;
  modelName?: string;
  timestamp?: string;
  tokensUsed?: { input?: number; output?: number; total?: number };
  costUsd?: number | null;
  latencyMs?: number | null;
  tokensPerSecond?: number | null;
  onRegenerate?: (turnIndex?: number) => void;
}

export function ChatDeliverable({
  finalResult,
  status = "completed",
  turnIndex,
  isTurnRawTrace = false,
  lastThoughtContent,
  producedFiles = [],
  onSelectFile,
  copiedSection,
  onCopy,
  execMode = "agent",
  showRawTrace,
  setShowRawTrace,
  modelName = "Assistant",
  timestamp,
  tokensUsed,
  costUsd,
  latencyMs,
  tokensPerSecond,
  onRegenerate,
}: ChatDeliverableProps) {
  const copyId = typeof turnIndex === "number" ? `turn-res-${turnIndex}` : "final-result";

  //: Hide dot-prefixed paths (`.git/…`, `.peldrun/…`, `.env`,
  //: `.gitignore`, …) from the user-facing deliverables list.
  //: These paths are implementation details of the workspace — they
  //: are not what the agent "produced" for the user, and they pollute
  //: the deliverable card with dozens of internal Git files (hooks,
  //: objects, refs, …) that bury the actual deliverables.
  //:
  //: The workspace panel (Files tab) still shows them in full, so the
  //: user can inspect the Git internals when needed.
  //:
  //: The check splits on both POSIX and Windows separators so that
  //: paths from either side of the stack are treated uniformly.
  const visibleProducedFiles = (producedFiles || []).filter((f) => {
    const raw = f.path || f.name || "";
    if (!raw) return false;
    const segments = raw.split(/[/\\]/);
    return !segments.some((segment) => segment.startsWith("."));
  });

  const hasFiles = visibleProducedFiles.length > 0;

  let displayContent = finalResult;
  if (isTurnRawTrace) {
    if (lastThoughtContent && lastThoughtContent.trim()) {
      displayContent = lastThoughtContent;
    } else if (hasFiles) {
      displayContent = "Task deliverables and files have been generated successfully in your workspace.";
    } else {
      displayContent = "Task completed successfully.";
    }
  }

  const hasUsage = Boolean(tokensUsed && tokensUsed.total && tokensUsed.total > 0);

  return (
    <div className="space-y-3 font-sans">
      {/* 1. Assistant Identity Header */}
      <div className="flex items-center justify-between text-xs text-muted-foreground pt-1">
        <div className="flex items-center gap-1.5 font-medium text-foreground">
          <Bot size={15} className="text-primary shrink-0" />
          <span className="font-semibold text-xs font-mono">{modelName}</span>
          {execMode === "agent" && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary border border-primary/20 font-mono">
              Agent
            </span>
          )}
          {timestamp && <span className="text-[10px] text-muted-foreground font-normal">({timestamp})</span>}
        </div>
      </div>

      {/* 2. Primary Response rendered via GenerativeUIRouter */}
      <div className="p-4 rounded-md bg-card/40 text-foreground text-sm leading-relaxed">
        <MarkdownRenderer content={displayContent} />
      </div>

      {/* 3. Diagnostic Trace Toggle */}
      {isTurnRawTrace && (
        <div className="border border-border/60 rounded-lg overflow-hidden bg-background/40">
          <button
            type="button"
            onClick={() => setShowRawTrace(!showRawTrace)}
            className="w-full flex items-center justify-between px-3.5 py-1.5 text-muted-foreground hover:text-foreground text-[11px] font-mono cursor-pointer"
          >
            <span className="flex items-center gap-1.5">
              <Activity size={13} className="shrink-0" />
              <span>Execution Trace & Observations</span>
            </span>
            <span>{showRawTrace ? "Hide diagnostic trace" : "View diagnostic trace"}</span>
          </button>
          {showRawTrace && (
            <div className="p-2.5 border-t border-border/40 font-mono text-[11px] text-muted-foreground bg-muted/30 whitespace-pre-wrap max-h-48 overflow-y-auto">
              {finalResult}
            </div>
          )}
        </div>
      )}

      {/* 4. Adaptive Deliverable Box: Error or Stop Alert */}
      {(status === "failed" || status === "stopped") && (
        <div className="p-3.5 rounded-lg bg-destructive/10 border border-destructive/30 text-destructive text-xs flex items-start gap-2.5">
          <AlertCircle size={15} className="shrink-0 mt-0.5" />
          <div className="space-y-1">
            <span className="font-semibold block">
              {status === "stopped" ? "Execution Stopped by Operator" : "Execution Terminated with Error"}
            </span>
            <p className="text-[11px] opacity-90 leading-relaxed font-mono whitespace-pre-wrap">
              {finalResult || (status === "stopped" ? "Task was stopped by operator." : "Execution was aborted or encountered an error.")}
            </p>
          </div>
        </div>
      )}

      {/* 5. Adaptive Deliverable Box: Generated Files */}
      {execMode === "agent" && status === "completed" && hasFiles && (
        <div className="p-3.5 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-xs space-y-2.5">
          <div className="flex items-center justify-between">
            <span className="text-emerald-500 font-semibold tracking-wide text-xs">
              TASK DELIVERABLES & OUTPUT FILES ({visibleProducedFiles.length})
            </span>
          </div>
          <div className="flex flex-wrap gap-2">
            {visibleProducedFiles.map((f, idx) => {
              const isModified = f.action === "modified" || f.status === "modified";
              const targetPath = f.path || f.name;
              return (
                <button
                  key={`${targetPath}-${idx}`}
                  type="button"
                  onClick={() => onSelectFile(targetPath)}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-card hover:bg-muted text-foreground border border-card text-xs cursor-pointer shadow-sm hover:shadow-xl transition-all"
                  title={`Preview ${targetPath} in Workspace`}
                >
                  <FileText size={13} className="text-primary shrink-0" />
                  <span className="font-medium font-mono truncate max-w-[200px]">{targetPath}</span>
                  {isModified ? (
                    <span className="text-[9px] px-1 py-0.2 rounded bg-amber-500/20 text-amber-600 dark:text-amber-400 font-mono font-semibold">
                      Modified
                    </span>
                  ) : (
                    <span className="text-[9px] px-1 py-0.2 rounded bg-emerald-500/20 text-emerald-600 dark:text-emerald-400 font-mono font-semibold">
                      New
                    </span>
                  )}
                  <ExternalLink size={10} className="opacity-60 shrink-0" />
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* 6. Bottom Action Toolbar */}
      <div className="flex items-center justify-between gap-2 pt-2 text-xs">
        <div className="flex items-center">
          {hasUsage && (
            <UsageBadgeGroup
              tokensUsed={tokensUsed}
              costUsd={costUsd ?? 0}
              latencyMs={latencyMs}
              tokensPerSecond={tokensPerSecond}
              model={modelName}
              variant="card"
            />
          )}
        </div>

        <div className="flex items-center gap-1.5">
          {onRegenerate && (
            <button
              type="button"
              onClick={() => onRegenerate(turnIndex)}
              className="flex items-center gap-1.5 px-2.5 py-1 text-black/50 hover:text-foreground cursor-pointer transition-all text-[11px]"
              title="Regenerate Response"
            >
              <RotateCcw size={12} className="shrink-0" />

            </button>
          )}

          <button
            type="button"
            onClick={() => onCopy(displayContent, copyId)}
            className="flex items-center gap-1.5 px-2.5 py-1 text-black/50 hover:text-foreground cursor-pointer transition-all text-[11px]"
            title="Copy Response"
          >
            {copiedSection === copyId ? (
              <>
                <Check size={12} className="text-emerald-500 shrink-0" />
                <span className="text-emerald-500 font-medium font-mono">Copied!</span>
              </>
            ) : (
              <>
                <Copy size={12} className="shrink-0" />
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ChatDeliverable;
