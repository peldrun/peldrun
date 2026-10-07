"use client";

import React from "react";
import { FileText, ExternalLink, Activity, Check, Copy, Bot, AlertCircle } from "lucide-react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import { UsageBadgeGroup } from "@/components/chat/UsageBadges";

interface ChatDeliverableProps {
  finalResult: string;
  status: "completed" | "failed";
  turnIndex?: number;
  isTurnRawTrace?: boolean;
  lastThoughtContent?: string;
  producedFiles?: { name: string; path: string }[];
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
}

export function ChatDeliverable({
  finalResult,
  status,
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
}: ChatDeliverableProps) {
  const copyId = typeof turnIndex === "number" ? `turn-res-${turnIndex}` : "final-result";
  const hasFiles = producedFiles && producedFiles.length > 0;

  // Synthesize clean presentation if finalResult contains raw diagnostic execution traces
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

      {/* 2. Primary Markdown Response */}
      <div className="p-4 rounded-md bg-card/40 text-foreground text-sm leading-relaxed">
        <MarkdownRenderer content={displayContent} />
      </div>

      {/* 3. Diagnostic Trace Toggle (Keeps raw command logs isolated) */}
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

      {/* 4. Adaptive Deliverable Box: Error Alert */}
      {status === "failed" && (
        <div className="p-3.5 rounded-lg bg-destructive/10 border border-destructive/30 text-destructive text-xs flex items-start gap-2.5">
          <AlertCircle size={15} className="shrink-0 mt-0.5" />
          <div className="space-y-1">
            <span className="font-semibold block">Execution Terminated with Error</span>
            <p className="text-[11px] opacity-90 leading-relaxed font-mono whitespace-pre-wrap">
              {finalResult || "Execution was aborted or encountered an error."}
            </p>
          </div>
        </div>
      )}

      {/* 5. Adaptive Deliverable Box: Generated Files */}
      {execMode === "agent" && status === "completed" && hasFiles && (
        <div className="p-3.5 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-xs space-y-2.5">
          <div className="flex items-center justify-between">
            <span className="text-emerald-500 font-semibold tracking-wide text-xs">
              TASK DELIVERABLES & OUTPUT FILES ({producedFiles.length})
            </span>
          </div>
          <div className="flex flex-wrap gap-2">
            {producedFiles.map((f) => (
              <button
                key={f.path || f.name}
                type="button"
                onClick={() => onSelectFile(f.name)}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-card hover:bg-muted text-foreground border border-border text-xs cursor-pointer shadow-xs transition-all"
                title="Preview file in Workspace"
              >
                <FileText size={13} className="text-primary shrink-0" />
                <span className="font-medium font-mono">{f.name}</span>
                <ExternalLink size={10} className="opacity-60 shrink-0" />
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 6. Bottom Action Toolbar with Usage Badges & Copy Button */}
      <div className="flex items-center justify-between gap-2 pt-2 border-t border-border/40 text-xs">
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

        <button
          type="button"
          onClick={() => onCopy(displayContent, copyId)}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-muted/50 hover:bg-muted text-muted-foreground hover:text-foreground cursor-pointer transition-all border border-border/40 text-[11px]"
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
              <span className="font-mono">Copy</span>
            </>
          )}
        </button>
      </div>
    </div>
  );
}

export default ChatDeliverable;