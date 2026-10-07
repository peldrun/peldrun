"use client";

import React from "react";
import { Plus, History, Square, PanelRightClose, PanelRightOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EngineOption, EngineSelector } from "./engine-selector";
import { UsageBadgeGroup } from "@/components/chat/UsageBadges";
import type { ChatUsageSummary } from "@/lib/types";

interface ChatSubHeaderProps {
  isFreshSession: boolean;
  submittedPrompt: string;
  activeChatId: string | null;
  activeJobId: string | null;
  historyTurnsCount: number;
  tokensUsed: { input: number; output: number; total: number };
  currentStepNum: number;
  maxSteps?: number;
  execMode: "agent" | "chat";
  selectedEngineId?: string;
  onSelectEngine?: (engine: EngineOption) => void;
  onNewSession: () => void;
  onStopTask: () => void;
  showRightPanel: boolean;
  onToggleRightPanel: () => void;

  status?: "idle" | "running" | "ready" | "completed" | "error";
  sessionTitle?: string;
  stepCount?: number;

  // Optional telemetry overrides
  usageSummary?: ChatUsageSummary | null;
  activeProvider?: string | null;
  activeModel?: string | null;
  totalCostUsd?: number | null;
}

export function ChatSubHeader({
  isFreshSession,
  historyTurnsCount,
  tokensUsed,
  currentStepNum,
  maxSteps = 30,
  execMode,
  onNewSession,
  onStopTask,
  showRightPanel,
  onToggleRightPanel,
  status = "idle",
  stepCount,
  usageSummary,
  activeProvider,
  activeModel,
  totalCostUsd,
}: ChatSubHeaderProps) {
  // Safe aggregation: fallback gracefully to tokensUsed if usageSummary is absent
  const effectiveSummary: ChatUsageSummary | null =
    usageSummary ||
    (tokensUsed && tokensUsed.total > 0
      ? {
          input_tokens: tokensUsed.input || 0,
          output_tokens: tokensUsed.output || 0,
          total_tokens: tokensUsed.total || 0,
          total_cost_usd: totalCostUsd ?? 0,
        }
      : null);

  return (
    <div className="h-12 flex items-center justify-between px-5 bg-transparent backdrop-blur-sm shrink-0 font-sans">
      <div className="flex items-center gap-2.5">
        {isFreshSession ? (
          <EngineSelector />
        ) : (
          <button
            type="button"
            onClick={onNewSession}
            className="inline-flex items-center gap-1.5 h-7 px-2.5 text-xs font-medium rounded-md border border-border bg-card hover:bg-muted text-foreground transition-all shadow-peldrun-xs cursor-pointer"
            title="Start a fresh autonomous session"
          >
            <Plus size={13} />
            <span>New Session</span>
          </button>
        )}
      </div>

      <div className="flex items-center gap-2.5">

        {/* Token & Telemetry Badges Cluster */}
        {/* Single Unified Header Badge */}
                {tokensUsed && tokensUsed.total > 0 && (
                  <UsageBadgeGroup
                    tokensUsed={tokensUsed}
                    costUsd={totalCostUsd ?? 0}
                    provider={activeProvider}
                    model={activeModel}
                    variant="compact"
                    showBreakdown={true}
                  />
                )}


        {/* STEP COUNTER: Strict Isolation - Visible ONLY when execMode === 'agent' */}
        {execMode === "agent" && (status === "running" || currentStepNum > 0) && (
          <span className="text-xs font-mono text-foreground font-semibold px-2 py-0.5 bg-muted rounded-md border border-border/60 animate-in fade-in">
            Step {currentStepNum} / {maxSteps}
          </span>
        )}

        {historyTurnsCount > 0 && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-mono bg-muted border border-border text-muted-foreground">
            <History size={10} />
            <span>Turn {historyTurnsCount + 1}</span>
          </span>
        )}

        {/* Semantic Status Badge - Only visible when actively RUNNING */}
        {status === "running" && (
          <div className="hidden sm:flex items-center gap-1.5">
            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-sm text-[11px] font-mono font-medium bg-amber-500/15 text-amber-500 border border-amber-500/30">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
              <span>RUNNING</span>
            </span>

            {typeof stepCount === "number" && (
              <span className="text-[11px] font-mono text-muted-foreground px-1.5 py-0.5 rounded-sm bg-background border border-border">
                Step {stepCount}
              </span>
            )}
          </div>
        )}

        {status === "running" && (
          <Button
            variant="destructive"
            size="sm"
            onClick={onStopTask}
            className="active:bg-red-800 bg-red-600 hover:bg-red-700 text-white h-7 px-2.5 text-xs font-sans rounded-md cursor-pointer shadow-sm transition-colors"
          >
            <Square size={11} className="fill-current mr-1" />
            <span>Stop</span>
          </Button>
        )}

        <button
          type="button"
          onClick={onToggleRightPanel}
          className="p-1.5 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-all cursor-pointer"
          title={showRightPanel ? "Hide Right Workspace Panel" : "Show Right Workspace Panel"}
        >
          {showRightPanel ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
        </button>
      </div>
    </div>
  );
}

export default ChatSubHeader;