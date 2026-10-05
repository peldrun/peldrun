"use client";

import React from "react";
import { Copy, Check, HelpCircle, CheckCircle2, MessageSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ChatTimeline, StepEvent } from "./chat-timeline";
import { ChatDeliverable } from "./chat-deliverable";
import AIThinkingLoader from "@/components/AIThinkingLoader";

export interface ChatTurn {
  id: string;
  jobId: string;
  prompt: string;
  timestamp: string;
  steps: StepEvent[];
  finalResult: string | null;
  status: "completed" | "failed";
  tokensUsed?: { input: number; output: number; total: number };
  producedFiles?: { name: string; path: string }[];
  model?: string;
  mode?: "agent" | "chat";
}

interface ChatThreadProps {
  historyTurns: ChatTurn[];
  submittedPrompt: string;
  sessionTimestamp: string;
  humanQuery: string | null;
  humanAnswer: string;
  setHumanAnswer: (val: string) => void;
  onSendHumanAnswer: (customAnswer?: string) => void;
  execMode: "agent" | "chat";
  activeGroupedSteps: Record<number, StepEvent[]>;
  currentStepNum: number;
  status: "idle" | "running" | "completed" | "failed";
  elapsedSeconds: number;
  finalResult: string | null;
  producedFiles: { name: string; path: string }[];
  copiedSection: string | null;
  copyText: (text: string, id: string) => void;
  expandedSteps: Record<string, boolean>;
  toggleStep: (key: string) => void;
  onSelectFile: (fileName: string) => void;
  showRawTrace: boolean;
  setShowRawTrace: (val: boolean) => void;
  getLiveStatusMessage: () => string;
  chatScrollBottomRef: React.RefObject<HTMLDivElement | null>;
  activeModelName?: string;
}

export function ChatThread({
  historyTurns,
  submittedPrompt,
  sessionTimestamp,
  humanQuery,
  humanAnswer,
  setHumanAnswer,
  onSendHumanAnswer,
  execMode,
  activeGroupedSteps,
  currentStepNum,
  status,
  elapsedSeconds,
  finalResult,
  producedFiles,
  copiedSection,
  copyText,
  expandedSteps,
  toggleStep,
  onSelectFile,
  showRawTrace,
  setShowRawTrace,
  getLiveStatusMessage,
  chatScrollBottomRef,
  activeModelName = "Assistant",
}: ChatThreadProps) {
  const groupStepEvents = (evts: StepEvent[]) => {
    return evts.reduce((acc, s) => {
      const isVisible = (s.content && s.content.trim() !== "") || Boolean(s.toolName);
      if (!isVisible) return acc;
      if (!acc[s.step]) acc[s.step] = [];
      acc[s.step].push(s);
      return acc;
    }, {} as Record<number, StepEvent[]>);
  };

  const isCurrentRawTrace = Boolean(
    finalResult &&
    (finalResult.includes("Observed output of cmd") || finalResult.startsWith("Step 1:"))
  );

  const activeStepsList = Object.values(activeGroupedSteps).flat();
  const currentLastThought = [...activeStepsList]
    .reverse()
    .find((s) => s.type === "thought" && s.content && !s.content.startsWith("Step ") && !s.content.startsWith("terminate("));

  // Inspect active steps to detect interactive human inquiry parameters (prompt, options, input_type)
  const pendingHumanCall = [...activeStepsList].reverse().find((s) => {
    return (
      (s.toolName === "ask_human" || s.toolName === "human_input") &&
      s.type === "tool_call"
    );
  });

  let detectedPrompt = humanQuery;
  let detectedType = "text";
  let detectedOptions: string[] = [];

  if (pendingHumanCall && pendingHumanCall.content) {
    try {
      const parsedArgs = JSON.parse(pendingHumanCall.content);
      if (parsedArgs && typeof parsedArgs === "object") {
        detectedPrompt = parsedArgs.prompt || parsedArgs.query || detectedPrompt;
        detectedType = (parsedArgs.input_type || "text").toLowerCase();
        if (Array.isArray(parsedArgs.options) && parsedArgs.options.length > 0) {
          detectedOptions = parsedArgs.options.map(String);
        } else if (detectedType === "confirm" || detectedType === "boolean") {
          detectedOptions = ["Yes", "No"];
        }
      }
    } catch {
      // Content may be a raw string prompt
      if (!detectedPrompt) {
        detectedPrompt = pendingHumanCall.content;
      }
    }
  }

  const isHumanWaiting = Boolean(detectedPrompt && status === "running");

  return (
    <div className="flex-1 overflow-y-auto p-4 sm:p-5 font-sans">
      <div className="w-full max-w-[900px] mx-auto space-y-6">
        {/* Historical Turns */}
        {historyTurns.map((turn, tIdx) => {
          const turnMode = turn.mode || (turn.steps && turn.steps.length > 0 ? "agent" : "chat");
          const isAgentTurn = turnMode === "agent";
          const isTurnRawTrace = Boolean(
            turn.finalResult &&
            (turn.finalResult.includes("Observed output of cmd") || turn.finalResult.startsWith("Step 1:"))
          );
          const turnLastThought = [...turn.steps]
            .reverse()
            .find((s) => s.type === "thought" && s.content && !s.content.startsWith("Step ") && !s.content.startsWith("terminate("));

          const turnGrouped = groupStepEvents(turn.steps);

          return (
            <div key={turn.id || `turn-${tIdx}`} className="space-y-4 pb-6 border-b border-border/40">
              {/* User Prompt */}
              <div className="flex flex-col items-end space-y-1">
                <div className="max-w-[90%] bg-transparent text-foreground px-4 py-2.5 text-sm sm:text-base leading-relaxed whitespace-pre-wrap font-sans">
                  {turn.prompt}
                </div>
                <div className="flex items-center gap-2 px-1 text-[10px] text-muted-foreground font-mono">
                  {turn.timestamp && <span>{turn.timestamp}</span>}
                  <button
                    type="button"
                    onClick={() => copyText(turn.prompt, `turn-prompt-${tIdx}`)}
                    className="p-1 hover:text-foreground text-muted-foreground transition-all cursor-pointer rounded"
                    title="Copy prompt"
                  >
                    {copiedSection === `turn-prompt-${tIdx}` ? (
                      <Check size={11} className="text-emerald-500" />
                    ) : (
                      <Copy size={11} />
                    )}
                  </button>
                </div>
              </div>

              {/* Execution Steps */}
              {isAgentTurn && turnGrouped && Object.keys(turnGrouped).length > 0 && (
                <ChatTimeline
                  prefix={`turn-${tIdx}`}
                  groupedSteps={turnGrouped}
                  isCurrentActive={false}
                  currentStepNum={0}
                  isRunning={false}
                  expandedSteps={expandedSteps}
                  toggleStep={toggleStep}
                />
              )}

              {/* Final Result */}
              {turn.finalResult && (
                <ChatDeliverable
                  finalResult={turn.finalResult}
                  status={turn.status}
                  turnIndex={tIdx}
                  isTurnRawTrace={isTurnRawTrace}
                  lastThoughtContent={turnLastThought ? turnLastThought.content : undefined}
                  producedFiles={turn.producedFiles}
                  onSelectFile={onSelectFile}
                  copiedSection={copiedSection}
                  onCopy={copyText}
                  execMode={isAgentTurn ? "agent" : "chat"}
                  showRawTrace={showRawTrace}
                  setShowRawTrace={setShowRawTrace}
                  modelName={turn.model || activeModelName}
                  timestamp={turn.timestamp}
                />
              )}
            </div>
          );
        })}

        {/* Current Active Turn Prompt */}
        {submittedPrompt && (
          <div className="flex flex-col items-end space-y-1">
            <div className="max-w-[90%] bg-transparent text-foreground px-4 py-2.5 text-sm sm:text-base leading-relaxed whitespace-pre-wrap font-sans">
              {submittedPrompt}
            </div>
            <div className="flex items-center gap-2 px-1 text-[10px] text-muted-foreground font-mono">
              {sessionTimestamp && <span>{sessionTimestamp}</span>}
              <button
                type="button"
                onClick={() => copyText(submittedPrompt, "user-prompt")}
                className="p-1 hover:text-foreground text-muted-foreground transition-all cursor-pointer rounded"
                title="Copy prompt"
              >
                {copiedSection === "user-prompt" ? (
                  <Check size={11} className="text-emerald-500" />
                ) : (
                  <Copy size={11} />
                )}
              </button>
            </div>
          </div>
        )}

        {/* Active Stepper (Agent Mode) */}
        {execMode === "agent" && activeGroupedSteps && Object.keys(activeGroupedSteps).length > 0 && (
          <ChatTimeline
            prefix="active"
            groupedSteps={activeGroupedSteps}
            isCurrentActive={true}
            currentStepNum={currentStepNum}
            isRunning={status === "running"}
            expandedSteps={expandedSteps}
            toggleStep={toggleStep}
          />
        )}

        {/* Interactive Human-in-the-Loop Dialog Suspension Card */}
        {isHumanWaiting && (
          <div className="p-4 sm:p-5 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs space-y-3.5 shadow-sm transition-all">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-amber-600 dark:text-amber-400 font-semibold text-xs">
                <HelpCircle size={15} className="shrink-0 animate-bounce" />
                <span>Agent Paused & Waiting for Your Decision:</span>
              </div>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/20 font-bold">
                Action Required
              </span>
            </div>

            <div className="p-3 rounded-lg bg-background border border-border text-foreground text-sm font-medium leading-relaxed whitespace-pre-wrap">
              {detectedPrompt}
            </div>

            {/* Clickable Quick Action Option Buttons */}
            {detectedOptions.length > 0 && (
              <div className="space-y-1.5">
                <span className="text-[11px] font-medium text-muted-foreground block">
                  Select an option to proceed:
                </span>
                <div className="flex flex-wrap items-center gap-2">
                  {detectedOptions.map((opt) => (
                    <button
                      key={opt}
                      type="button"
                      onClick={() => onSendHumanAnswer(opt)}
                      className="px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-primary text-primary-foreground hover:bg-primary/90 transition-all shadow-xs cursor-pointer flex items-center gap-1.5 active:scale-95"
                    >
                      <CheckCircle2 size={13} />
                      <span>{opt}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* Freeform Text Input Response */}
            <div className="flex items-center gap-2 pt-1">
              <div className="relative flex-1">
                <input
                  type="text"
                  value={humanAnswer}
                  onChange={(e) => setHumanAnswer(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && onSendHumanAnswer()}
                  placeholder={
                    detectedOptions.length > 0
                      ? "Or type a custom response to the agent..."
                      : "Type your response to the agent..."
                  }
                  className="w-full bg-background border border-border rounded-lg pl-3 pr-8 py-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-primary shadow-xs"
                />
                <MessageSquare size={13} className="absolute right-2.5 top-2.5 text-muted-foreground/60 pointer-events-none" />
              </div>
              <Button
                variant="primary"
                onClick={() => onSendHumanAnswer()}
                disabled={!humanAnswer.trim()}
                className="text-xs px-4 h-8.5 rounded-lg cursor-pointer font-medium"
              >
                Send
              </Button>
            </div>
          </div>
        )}

        {/* Live Running Indicator */}
        {status === "running" && !isHumanWaiting && (
          <div className="flex items-center justify-between px-3.5 py-2.5 bg-muted/40 text-foreground transition-all">
            <div className="flex items-center gap-2.5 text-xs">
              <AIThinkingLoader
                message={getLiveStatusMessage()}
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
        )}

        {/* Current Turn Final Deliverable */}
        {finalResult && (
          <ChatDeliverable
            finalResult={finalResult}
            status={status === "failed" ? "failed" : "completed"}
            isTurnRawTrace={isCurrentRawTrace}
            lastThoughtContent={currentLastThought ? currentLastThought.content : undefined}
            producedFiles={producedFiles}
            onSelectFile={onSelectFile}
            copiedSection={copiedSection}
            onCopy={copyText}
            execMode={execMode}
            showRawTrace={showRawTrace}
            setShowRawTrace={setShowRawTrace}
            modelName={activeModelName}
            timestamp={sessionTimestamp}
          />
        )}

        <div ref={chatScrollBottomRef} />
      </div>
    </div>
  );
}

export default ChatThread;