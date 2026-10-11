// frontend/src/components/chat/SubtaskTree.tsx
"use client";

import React, { useState } from "react";
import {
  GitFork,
  ChevronDown,
  ChevronRight,
  Bot,
  CheckCircle2,
  XCircle,
  AlertCircle,
  Loader2,
  StopCircle,
  FileCheck,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ChildRunState } from "@/lib/types";

interface SubtaskTreeProps {
  childRuns: Record<string, ChildRunState>;
  onCancelSubtask?: (childRunId: string) => Promise<void>;
  onSelectFile?: (filePath: string) => void;
}

export function SubtaskTree({
  childRuns,
  onCancelSubtask,
  onSelectFile,
}: SubtaskTreeProps) {
  const [expandedMap, setExpandedMap] = useState<Record<string, boolean>>({});

  const runsList = Object.values(childRuns || {});
  if (runsList.length === 0) {
    return null;
  }

  const toggleExpand = (childId: string) => {
    setExpandedMap((prev) => ({
      ...prev,
      [childId]: !prev[childId],
    }));
  };

  const renderStatusBadge = (status: ChildRunState["status"]) => {
    switch (status) {
      case "running":
        return (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono font-medium bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30">
            <Loader2 size={10} className="animate-spin" />
            <span>Running</span>
          </span>
        );
      case "completed":
        return (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono font-medium bg-primary/15 text-primary border border-primary/25">
            <CheckCircle2 size={10} />
            <span>Completed</span>
          </span>
        );
      case "cancelled":
        return (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono font-medium bg-muted text-muted-foreground border border-border">
            <XCircle size={10} />
            <span>Cancelled</span>
          </span>
        );
      case "failed":
      default:
        return (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono font-medium bg-rose-500/15 text-rose-600 dark:text-rose-400 border border-rose-500/30">
            <AlertCircle size={10} />
            <span>Failed</span>
          </span>
        );
    }
  };

  return (
    <div className="rounded-xl border border-border bg-card/60 backdrop-blur-sm overflow-hidden my-3 shadow-xs">
      <div className="px-3.5 py-2.5 bg-muted/40 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-semibold text-foreground">
          <GitFork size={14} className="text-primary" />
          <span>Delegated Specialist Subtasks ({runsList.length})</span>
        </div>
      </div>

      <div className="divide-y divide-border/60">
        {runsList.map((child) => {
          const isExpanded = Boolean(expandedMap[child.childRunId]);
          return (
            <div key={child.childRunId} className="p-3 text-xs space-y-2">
              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  onClick={() => toggleExpand(child.childRunId)}
                  className="flex items-center gap-2 text-left hover:text-foreground cursor-pointer flex-1 min-w-0"
                >
                  {isExpanded ? (
                    <ChevronDown size={14} className="text-muted-foreground shrink-0" />
                  ) : (
                    <ChevronRight size={14} className="text-muted-foreground shrink-0" />
                  )}
                  <span className="p-1 rounded-md bg-muted text-primary shrink-0">
                    <Bot size={13} />
                  </span>
                  <span className="font-semibold text-foreground truncate">
                    {child.targetAgentId}
                  </span>
                  <span className="text-[10px] text-muted-foreground font-mono truncate hidden sm:inline">
                    {child.instruction.slice(0, 60)}...
                  </span>
                </button>

                <div className="flex items-center gap-2 shrink-0">
                  {renderStatusBadge(child.status)}

                  {child.status === "running" && onCancelSubtask && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => onCancelSubtask(child.childRunId)}
                      className="h-6 px-2 text-[10px] text-rose-500 hover:text-rose-600 hover:bg-rose-500/10 cursor-pointer flex items-center gap-1"
                      title="Cancel this subtask only"
                    >
                      <StopCircle size={11} />
                      <span>Cancel</span>
                    </Button>
                  )}
                </div>
              </div>

              {isExpanded && (
                <div className="pl-6 pt-1 space-y-2 animate-in fade-in duration-100">
                  <div className="p-2.5 rounded-lg bg-muted/30 border border-border/50 text-[11px] font-mono text-muted-foreground whitespace-pre-wrap">
                    <span className="font-semibold text-foreground/80 block mb-1">Directive:</span>
                    {child.instruction}
                  </div>

                  <div className="flex flex-wrap items-center gap-3 text-[10px] font-mono text-muted-foreground">
                    {child.durationSeconds !== undefined && (
                      <span>Duration: {child.durationSeconds.toFixed(1)}s</span>
                    )}
                    {child.tokensUsed !== undefined && child.tokensUsed > 0 && (
                      <span className="flex items-center gap-1">
                        <Zap size={10} className="text-amber-500" />
                        <span>Tokens: {child.tokensUsed.toLocaleString()}</span>
                      </span>
                    )}
                    <span>Steps: {child.steps.length}</span>
                  </div>

                  {child.deliverables && child.deliverables.length > 0 && (
                    <div className="pt-1 space-y-1">
                      <span className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider block">
                        Produced Deliverables:
                      </span>
                      <div className="flex flex-wrap gap-1.5">
                        {child.deliverables.map((d, dIdx) => (
                          <button
                            key={`${d}-${dIdx}`}
                            type="button"
                            onClick={() => onSelectFile && onSelectFile(d)}
                            className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-mono bg-card border border-border hover:border-primary text-foreground cursor-pointer transition-colors"
                          >
                            <FileCheck size={11} className="text-emerald-500" />
                            <span>{d}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {child.error && (
                    <div className="p-2 rounded bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-[11px] font-mono">
                      {child.error}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default SubtaskTree;
