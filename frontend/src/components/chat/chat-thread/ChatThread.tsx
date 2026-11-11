//frontend\src\components\chat\chat-thread\ChatThread.tsx

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
 *  - Exposes lifecycle actions (Regenerate).
 *
 * All state is passed in from the parent; this component is a pure
 * presentational shell that delegates to focused sub-components.
 */

import React, { useEffect, useRef } from "react";
import { Bot } from "lucide-react";
import { Icon } from "@/components/Icon/Icon";
import { useProviderBadge } from "@/lib/providerBadge";
import { ChatTimeline, StepEvent } from "../chat-timeline";
import { ChatDeliverable } from "../chat-deliverable";
import { DelegationApprovalCard } from "../DelegationApprovalCard";
import { SubtaskTree } from "../SubtaskTree";
import { UserMessageBubble } from "./UserMessageBubble";
import { HistoricalTurn } from "./HistoricalTurn";
import { HumanInputCard } from "./HumanInputCard";
import { LiveStatusIndicator } from "./LiveStatusIndicator";
import { ChatReasoningPanel } from "./ChatReasoningPanel";
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
  const seen = new Set<string>();
  return evts.reduce((acc, s) => {
    const contentKey =
      typeof s.content === "string" ? s.content.trim() : "";
    const dedupKey = `${s.step}|${s.type}|${s.toolName || ""}|${contentKey}`;
    if (seen.has(dedupKey)) return acc;
    seen.add(dedupKey);

    const isVisible = contentKey !== "" || Boolean(s.toolName);
    if (!isVisible) return acc;
    if (!acc[s.step]) acc[s.step] = [];
    acc[s.step].push(s);
    return acc;
  }, {} as Record<number, StepEvent[]>);
}

export function ChatThread({
  historyTurns,
  chatId,
  submittedPrompt,
  submittedFiles = [],
  sessionTimestamp,
  humanQuery,
  humanAnswer,
  setHumanAnswer,
  onSendHumanAnswer,
  execMode,
  activeGroupedSteps,
  chatReasoningText: chatReasoningTextProp,
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
  activeProvider,
  tokensUsed,
  costUsd,
  latencyMs,
  tokensPerSecond,
  activeChildRuns = {},
  pendingApprovals = {},
  onApproveDelegation,
  onRejectDelegation,
  onCancelSubtask,
  onRegenerate,
  onOpenReview,
}: ChatThreadProps) {
  const activePromptAnchorRef = useRef<HTMLDivElement | null>(null);

  // `activeProvider` is provided as a prop by ChatContainer (which
  // reads it from useAppStorage("active_provider") at the container
  // level). Centralizing the read there guarantees that every
  // consumer in the tree — ChatThread's header, HistoricalTurn, and
  // ChatDeliverable — sees the same value within a single render
  // pass. Previously this component read the value independently,
  // which meant ChatContainer and ChatThread could momentarily see
  // different values during a selector change.
  const providerBadge = useProviderBadge(activeProvider);

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

  // [CHAT-REASONING] The reasoning stream is provided directly by
  // ChatContainer as a dedicated prop. Chat mode does NOT use the
  // step-based timeline at all — `activeGroupedSteps` is `{}` in
  // chat mode (see ChatContainer's `activeGroupedSteps` prop), so
  // deriving the reasoning from `activeStepsList` would always yield
  // an empty string.
  //
  // The state lives in ChatContainer (populated by use-sse-stream's
  // `setChatReasoningText`), which isolates it from the agent-mode
  // `steps` array and keeps the two modes completely independent.
  const chatReasoningText = chatReasoningTextProp || "";

  const activeApprovalList = Object.values(pendingApprovals || {});

  const activeOperation = extractOperationFromSteps(activeStepsList);

  // The final result is surfaced separately as `<ChatDeliverable>`.
  // Some backends also emit the same content as an observation
  // event in the last step, which causes the final message to
  // render twice: once inside the timeline, once inside the
  // deliverable. Filter that observation out of the timeline.
  const finalResultTrimmed =
    typeof finalResult === "string" ? finalResult.trim() : "";

  const filteredGroupedSteps = Object.fromEntries(
    Object.entries(activeGroupedSteps || {})
      .map(([stepNum, steps]) => {
        const cleaned = (steps as StepEvent[]).filter((s) => {
          // image_results steps are rendered separately by ChatThread
          // above the timeline, so exclude them from the accordion
          // content to prevent visual duplication.
          if (s.type === "image_results") return false;

          if (!finalResultTrimmed) return true;
          if (s.type !== "observation") return true;
          const content =
            typeof s.content === "string" ? s.content.trim() : "";
          if (!content) return true;
          return !(
            content === finalResultTrimmed ||
            content.startsWith(finalResultTrimmed) ||
            finalResultTrimmed.startsWith(content)
          );
        });
        return [stepNum, filterApprovalSteps(cleaned)];
      })
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
              chatId={chatId}
              copiedSection={copiedSection}
              copyText={copyText}
              expandedSteps={expandedSteps}
              toggleStep={toggleStep}
              onSelectFile={onSelectFile}
              showRawTrace={showRawTrace}
              setShowRawTrace={setShowRawTrace}
              activeModelName={activeModelName}
              activeProvider={activeProvider}
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

        {/* Active Turn Identity Header (Model Name & Metadata) */}
        {submittedPrompt && (
          <div className="flex items-center justify-between text-xs text-muted-foreground pt-1 pb-2">
            <div className="flex items-center gap-1.5 font-medium text-foreground">
              {providerBadge ? (
                <Icon name={providerBadge} size={16} />
              ) : (
                <Bot size={15} className="text-primary shrink-0" />
              )}
              {activeProvider && (
                <span className="font-semibold text-xs font-mono text-primary truncate max-w-[140px]">
                  {activeProvider}:
                </span>
              )}
              <span className="font-semibold text-xs font-mono theAgent truncate max-w-[180px]">{activeModelName}</span>
              {execMode === "agent" && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary border border-primary/20 font-mono">
                  Agent
                </span>
              )}
              {sessionTimestamp && (
                <span className="text-[10px] text-muted-foreground font-normal">
                  ({sessionTimestamp})
                </span>
              )}
            </div>
          </div>
        )}

        {/* Active Stepper (Agent Mode) — rendered below the header */}
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

        {/* [CHAT-REASONING] Reasoning trace in Chat mode.
            Rendered above the deliverable bubble so the user sees the
            model "thinking out loud" while it works, then the answer
            below. Auto-expands during streaming, auto-collapses 1.5s
            after completion (one final look), and is manually
            re-openable. Only rendered in chat mode — agent mode uses
            ChatTimeline instead. */}
        {execMode === "chat" && chatReasoningText.trim() && (
          <ChatReasoningPanel
            thoughtContent={chatReasoningText}
            isRunning={status === "running"}
          />
        )}

        {/* Image galleries are injected into the final answer's
            `result_text` as an ``image_gallery`` markdown block by
            the backend (peldrun_engine.py for agent mode,
            dispatcher.py for chat mode). They render inside
            <ChatDeliverable> via MarkdownRenderer, not as a
            standalone block here. This keeps the gallery visually
            part of the model's answer, matching chat mode. */}

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

        {/* Deliverable for Completed / Current Turn */}
        {finalResult && (
          <ChatDeliverable
            chatId={chatId}
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
            hideHeader={true}
            onRegenerate={
              status !== "running" && onRegenerate
                ? () => onRegenerate(-1)
                : undefined
            }
          />
        )}

        {/* Live Running Indicator — pinned to the bottom of the active thread.
            Pause was removed in Phase H — no LLM provider supports
            true streaming pause, so the button was misleading. Stop
            (available in ChatSubHeader) is the only lifecycle control. */}
        {status === "running" && !isHumanWaiting && activeApprovalList.length === 0 && (
          <LiveStatusIndicator
            message={getLiveStatusMessage()}
            elapsedSeconds={elapsedSeconds}
          />
        )}

        <div ref={chatScrollBottomRef} />
      </div>
    </div>
  );
}

export default ChatThread;
