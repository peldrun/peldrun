"use client";

/**
 * ChatThread container component.
 *
 * Owns the high-level orchestration of a chat session's rendering:
 *  - Iterates historical turns and renders each via `HistoricalTurn`.
 *  - Renders the current active turn (prompt + stepper + subtasks).
 *  - Displays delegation approval cards, live running indicator, and the
 *    final deliverable when the current run completes.
 *  - Detects a pending human-in-the-loop tool call and mounts the
 *    interactive `HumanInputCard` accordingly.
 *  - Exposes lifecycle actions (Regenerate, Pause, Resume).
 *
 * All state is passed in from the parent; this component is a pure
 * presentational shell that delegates to focused sub-components.
 */

import React, { useEffect, useRef } from "react";
import { ChatTimeline, StepEvent } from "../chat-timeline";
import { ChatDeliverable } from "../chat-deliverable";
import { DelegationApprovalCard } from "../DelegationApprovalCard";
import { SubtaskTree } from "../SubtaskTree";
import { UserMessageBubble } from "./UserMessageBubble";
import { HistoricalTurn } from "./HistoricalTurn";
import { HumanInputCard } from "./HumanInputCard";
import { LiveStatusIndicator } from "./LiveStatusIndicator";
import { useHumanToolDetection } from "./useHumanToolDetection";
import type { ChatThreadProps } from "./types";

import { ChatOperationCard } from "@/features/source-control/components/ChatOperationCard";
import { extractOperationFromSteps, filterApprovalSteps } from "./utils";

/**
 * Groups visible step events by their numeric step index.
 *
 * Events without content or a tool name are dropped to avoid rendering
 * empty placeholders. The returned record preserves insertion order.
 */
function groupStepEvents(evts: StepEvent[]): Record<number, StepEvent[]> {
  return evts.reduce((acc, s) => {
    const isVisible = (s.content && s.content.trim() !== "") || Boolean(s.toolName);
    if (!isVisible) return acc;
    if (!acc[s.step]) acc[s.step] = [];
    acc[s.step].push(s);
    return acc;
  }, {} as Record<number, StepEvent[]>);
}

export function ChatThread({
  historyTurns,
  submittedPrompt,
  submittedFiles = [],
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
  tokensUsed,
  costUsd,
  latencyMs,
  tokensPerSecond,
  activeChildRuns = {},
  pendingApprovals = {},
  onApproveDelegation,
  onRejectDelegation,
  onCancelSubtask,
  onPauseJob,
  onResumeJob,
  onRegenerate,
  onOpenReview,
}: ChatThreadProps) {
  const activePromptAnchorRef = useRef<HTMLDivElement | null>(null);




  // Auto-scroll to the active prompt anchor when a new prompt is submitted.
  useEffect(() => {
    if (submittedPrompt) {
      activePromptAnchorRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    }
  }, [submittedPrompt]);

  // Detect whether the agent is currently paused waiting for human input.
  const activeStepsList = Object.values(activeGroupedSteps || {}).flat();
  const { isHumanWaiting, detectedPrompt, detectedOptions } = useHumanToolDetection(
    activeStepsList,
    humanQuery,
    status
  );

  // Determine whether the current final result looks like a raw trace dump.
  const isCurrentRawTrace = Boolean(
    finalResult &&
      (finalResult.includes("Observed output of cmd") || finalResult.startsWith("Step 1:"))
  );

  // Most recent meaningful thought from the active step stream.
  const currentLastThought = [...activeStepsList]
    .reverse()
    .find(
      (s) =>
        s.type === "thought" &&
        s.content &&
        !s.content.startsWith("Step ") &&
        !s.content.startsWith("terminate(")
    );

  const activeApprovalList = Object.values(pendingApprovals || {});

  const activeOperation = extractOperationFromSteps(activeStepsList);
  const filteredGroupedSteps = Object.fromEntries(
    Object.entries(activeGroupedSteps || {})
      .map(([stepNum, steps]) => [stepNum, filterApprovalSteps(steps)])
      .filter(([_, steps]) => (steps as StepEvent[]).length > 0)
  );

  return (
    <div className="flex-1 overflow-y-auto p-4 sm:p-5 font-sans">
      <div className="w-full max-w-[900px] mx-auto space-y-6">
        {/* Historical Turns */}
        {historyTurns.map((turn, tIdx) => (
          <div key={turn.id || `turn-${tIdx}`}>
            <HistoricalTurn
              turn={turn}
              tIdx={tIdx}
              copiedSection={copiedSection}
              copyText={copyText}
              expandedSteps={expandedSteps}
              toggleStep={toggleStep}
              onSelectFile={onSelectFile}
              showRawTrace={showRawTrace}
              setShowRawTrace={setShowRawTrace}
              activeModelName={activeModelName}
              onRegenerate={onRegenerate}
            />
          </div>
        ))}

        {/* Current Active Turn Prompt */}
        {submittedPrompt && (
          <div ref={activePromptAnchorRef} className="space-y-4">
            <UserMessageBubble
              prompt={submittedPrompt}
              files={submittedFiles}
              timestamp={sessionTimestamp}
              copyId="user-prompt"
              copiedSection={copiedSection}
              onCopy={copyText}
              onSelectFile={onSelectFile}
            />
          </div>
        )}

        {/* Active Stepper (Agent Mode) */}
        {execMode === "agent" &&
          Object.keys(filteredGroupedSteps).length > 0 && (
            <ChatTimeline
              prefix="active"
              groupedSteps={filteredGroupedSteps}
              isCurrentActive={true}
              currentStepNum={currentStepNum}
              isRunning={status === "running"}
              expandedSteps={expandedSteps}
              toggleStep={toggleStep}
            />
          )}

        {activeOperation && onOpenReview && (
          <ChatOperationCard
            operation={activeOperation}
            onOpenReview={onOpenReview}
          />
        )}

        {/* Active Delegation Approval Cards */}
        {activeApprovalList.length > 0 && onApproveDelegation && onRejectDelegation && (
          <div className="space-y-2">
            {activeApprovalList.map((approval) => (
              <DelegationApprovalCard
                key={approval.approvalRequestId}
                approval={approval}
                onApprove={onApproveDelegation}
                onReject={onRejectDelegation}
                disabled={status !== "running"}
              />
            ))}
          </div>
        )}

        {/* Active Multi-Agent Subtask Observability Tree */}
        {Object.keys(activeChildRuns || {}).length > 0 && (
          <SubtaskTree
            childRuns={activeChildRuns}
            onCancelSubtask={onCancelSubtask}
            onSelectFile={onSelectFile}
          />
        )}

        {/* Interactive Human-in-the-Loop Dialog Suspension Card */}
        {isHumanWaiting && (
          <HumanInputCard
            isHumanWaiting={isHumanWaiting}
            detectedPrompt={detectedPrompt || ""}
            detectedOptions={detectedOptions}
            humanAnswer={humanAnswer}
            setHumanAnswer={setHumanAnswer}
            onSendHumanAnswer={onSendHumanAnswer}
          />
        )}

        {/* Live Running / Paused Indicator */}
        {(status === "running" || status === "paused") && !isHumanWaiting && activeApprovalList.length === 0 && (
          <LiveStatusIndicator
            message={status === "paused" ? "Execution paused by operator" : getLiveStatusMessage()}
            elapsedSeconds={elapsedSeconds}
            isPaused={status === "paused"}
            onPause={onPauseJob}
            onResume={onResumeJob}
          />
        )}

        {/* Deliverable for Completed / Current Turn */}
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
            tokensUsed={tokensUsed}
            costUsd={costUsd}
            latencyMs={latencyMs}
            tokensPerSecond={tokensPerSecond}
            onRegenerate={status !== "running" && status !== "paused" && onRegenerate ? () => onRegenerate(-1) : undefined}
          />
        )}

        <div ref={chatScrollBottomRef} />
      </div>
    </div>
  );
}

export default ChatThread;
