"use client";

/**
 * Renders a single historical turn from the conversation history.
 *
 * A historical turn is composed of:
 *  - The user message bubble (prompt + attachments).
 *  - An optional agent timeline (only when the turn ran in agent mode
 *    and produced visible step events).
 *  - An optional subtask tree when the turn spawned child runs.
 *  - The final deliverable card when a final result was produced.
 *
 * The component is intentionally stateless; expansion/collapse state is
 * delegated to the parent container via the `expandedSteps` / `toggleStep`
 * props so a single source of truth is preserved across turns.
 */

import React from "react";
import { ChatTimeline, StepEvent } from "../chat-timeline";
import { ChatDeliverable } from "../chat-deliverable";
import { SubtaskTree } from "../SubtaskTree";
import { UserMessageBubble } from "./UserMessageBubble";
import type { ChatTurn } from "./types";

export interface HistoricalTurnProps {
  turn: ChatTurn;
  tIdx: number;
  chatId?: string;
  copiedSection: string | null;
  copyText: (text: string, id: string) => void;
  expandedSteps: Record<string, boolean>;
  toggleStep: (key: string) => void;
  onSelectFile: (fileName: string) => void;
  showRawTrace: boolean;
  setShowRawTrace: (val: boolean) => void;
  activeModelName: string;
  /**
   * Currently-active provider display name (from ChatContainer's
   * `useAppStorage("active_provider")`).
   *
   * Used ONLY as a fallback when `turn.provider` is null/undefined —
   * i.e., for historical turns created before the provider-snapshot
   * feature was introduced. Newer turns carry their own `provider`
   * field captured at run START time, so we always display the
   * provider that actually produced the turn regardless of any later
   * EngineSelector change.
   */
  activeProvider?: string;
  onRegenerate?: (turnIndex: number, turn: ChatTurn) => void;
}

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

export function HistoricalTurn({
  turn,
  tIdx,
  chatId,
  copiedSection,
  copyText,
  expandedSteps,
  toggleStep,
  onSelectFile,
  showRawTrace,
  setShowRawTrace,
  activeModelName,
  activeProvider,
  onRegenerate,
}: HistoricalTurnProps) {
  const turnMode = turn.mode || (turn.steps && turn.steps.length > 0 ? "agent" : "chat");
  const isAgentTurn = turnMode === "agent";

  // Resolve the provider/model snapshots for THIS turn.
  //
  // `turn.provider` / `turn.model` are captured at run START time in
  // ChatContainer (see `runStartProviderRef` / `runStartModelRef`), so
  // they reflect the selection that ACTUALLY produced this turn —
  // even if the user has since changed the active provider/model via
  // EngineSelector. Falling back to the current active values only
  // when the snapshot is missing covers:
  //
  //   * Turns created before the snapshot feature existed (pre-upgrade
  //     sessions loaded from disk).
  //   * The very first turn of a session, before any archive has
  //     populated the refs.
  //
  // Using `||` (not `??`) preserves the existing behavior for
  // `turn.model`: an empty string is treated the same as `null` /
  // `undefined`, so a genuinely-empty model name never wins over the
  // current selection.
  const turnProvider = turn.provider || activeProvider;
  const turnModel = turn.model || activeModelName;

  const isTurnRawTrace = Boolean(
    turn.finalResult &&
    (turn.finalResult.includes("Observed output of cmd") || turn.finalResult.startsWith("Step 1:"))
  );

  const turnLastThought = [...turn.steps]
    .reverse()
    .find(
      (s) =>
        s.type === "thought" &&
        s.content &&
        !s.content.startsWith("Step ") &&
        !s.content.startsWith("terminate(")
    );

  const turnGrouped = groupStepEvents(turn.steps);

  return (
    <div className="space-y-4 pb-4">
      <UserMessageBubble
        prompt={turn.prompt}
        files={turn.attachedFiles}
        timestamp={turn.timestamp}
        copyId={`turn-prompt-${tIdx}`}
        copiedSection={copiedSection}
        onCopy={copyText}
        onSelectFile={onSelectFile}
      />

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

      {/* Historical Subtask Tree */}
      {turn.childRuns && Object.keys(turn.childRuns).length > 0 && (
        <SubtaskTree childRuns={turn.childRuns} onSelectFile={onSelectFile} />
      )}

      {turn.finalResult && (
        <ChatDeliverable
          chatId={chatId}
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
          modelName={turnModel}
          // Explicit provider for THIS turn. ChatDeliverable uses it
          // instead of the currently-active provider (which it would
          // otherwise read from useAppStorage). Without this prop,
          // historical turns would keep re-labelling themselves with
          // whatever provider the user picks in EngineSelector.
          providerName={turnProvider}
          timestamp={turn.timestamp}
          tokensUsed={turn.tokensUsed}
          costUsd={turn.usage?.cost_usd}
          latencyMs={turn.usage?.latency_ms}
          tokensPerSecond={turn.usage?.tokens_per_second}
          onRegenerate={onRegenerate ? () => onRegenerate(tIdx, turn) : undefined}
        />
      )}
    </div>
  );
}

export default HistoricalTurn;
