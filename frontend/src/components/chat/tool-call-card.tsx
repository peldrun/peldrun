"use client";

import React, { useState } from "react";
import {
  Terminal,
  ChevronRight,
  ChevronDown,
  Clock,
  CheckCircle2,
  RotateCw,
  AlertCircle,
  HelpCircle,
} from "lucide-react";
import { ToolPair } from "@/lib/types";

export function ToolCallCard({ tool }: { tool: ToolPair }) {
  const [isOpen, setIsOpen] = useState(false);

  const isRetrying = tool.status === "retrying";
  const isFailed = tool.isError || tool.status === "failed";
  const isWaitingInput = tool.status === "waiting_for_input" || tool.requiresInput;

  return (
    <div
      className={`my-1.5 rounded-lg border text-xs overflow-hidden shadow-peldrun-xs transition-colors ${
        isRetrying
          ? "border-amber-500/40 bg-amber-500/5"
          : isFailed
          ? "border-red-500/40 bg-red-500/5"
          : isWaitingInput
          ? "border-blue-500/40 bg-blue-500/5"
          : "border-border bg-card/60"
      }`}
    >
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between px-3 py-2 hover:bg-muted/40 transition-colors cursor-pointer select-none font-mono"
      >
        <div className="flex items-center gap-2 truncate">
          <Terminal size={13} className="text-primary flex-shrink-0" />
          <span className="font-semibold text-[11px] text-foreground">{tool.tool}</span>

          {/* Retry attempt badge */}
          {tool.attempt && tool.attempt > 1 && (
            <span className="inline-flex items-center gap-1 px-1.5 py-0.2 rounded text-[10px] bg-amber-500/15 text-amber-500 font-mono">
              <RotateCw size={10} className={isRetrying ? "animate-spin" : ""} />
              <span>
                Attempt {tool.attempt}
                {tool.maxAttempts ? `/${tool.maxAttempts}` : ""}
              </span>
            </span>
          )}

          {/* Status indicators */}
          {!isOpen && isRetrying && (
            <span className="inline-flex items-center gap-1 text-[10px] text-amber-500 truncate max-w-xs font-sans">
              <span>retrying...</span>
            </span>
          )}

          {!isOpen && isWaitingInput && (
            <span className="inline-flex items-center gap-1 text-[10px] text-blue-400 truncate max-w-xs font-sans">
              <HelpCircle size={11} />
              <span>waiting for user</span>
            </span>
          )}

          {!isOpen && isFailed && (
            <span className="inline-flex items-center gap-1 text-[10px] text-red-500 truncate max-w-xs font-sans">
              <AlertCircle size={11} />
              <span>failed</span>
            </span>
          )}

          {!isOpen && tool.output && !isFailed && !isRetrying && (
            <span className="inline-flex items-center gap-1 text-[10px] text-peldrun-success truncate max-w-xs font-sans">
              <CheckCircle2 size={11} />
              <span>executed</span>
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 text-muted-foreground flex-shrink-0">
          {tool.durationMs !== null && tool.durationMs !== undefined && (
            <span className="flex items-center gap-1 text-[10px] font-mono">
              <Clock size={11} />
              {tool.durationMs}ms
            </span>
          )}
          {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </div>
      </button>

      {isOpen && (
        <div className="p-3 space-y-2 border-t border-border/60 bg-background/60">
          {/* Tool Parameters */}
          {tool.args && Object.keys(tool.args).length > 0 && (
            <div>
              <div className="text-[9px] font-mono text-muted-foreground uppercase mb-1">Parameters</div>
              <pre className="p-2 rounded-md bg-muted/50 border border-border/50 text-foreground font-mono text-[10px] overflow-x-auto max-h-48">
                {JSON.stringify(tool.args, null, 2)}
              </pre>
            </div>
          )}

          {/* Retry Error Notification */}
          {tool.error && (
            <div>
              <div className="text-[9px] font-mono text-amber-500 uppercase mb-1">
                {isRetrying ? "Retry Diagnostic" : "Execution Error"}
              </div>
              <pre className="p-2 rounded-md bg-amber-500/10 border border-amber-500/20 text-amber-300 font-mono text-[10px] whitespace-pre-wrap leading-relaxed max-h-36">
                {tool.error}
              </pre>
            </div>
          )}

          {/* Execution Output */}
          {tool.output !== null && tool.output !== undefined && (
            <div>
              <div
                className={`text-[9px] font-mono uppercase mb-1 ${
                  isFailed ? "text-red-500" : "text-peldrun-success"
                }`}
              >
                {isFailed ? "Failure Output" : "Result"}
              </div>
              <pre
                className={`p-2 rounded-md border font-mono text-[10px] whitespace-pre-wrap leading-relaxed overflow-x-auto max-h-56 ${
                  isFailed
                    ? "bg-red-500/10 border-red-500/20 text-red-300"
                    : "bg-muted/40 border-border/50 text-foreground"
                }`}
              >
                {tool.output}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default ToolCallCard;