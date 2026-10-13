// Path: frontend/src/components/chat/chat-thread/types.ts
/**
 * Shared type definitions and constants for the chat thread module.
 *
 * Keeping types and constants isolated allows both the container component
 * and the presentational sub-components to share a single source of truth
 * without pulling in the full rendering tree.
 */

import type { StepEvent } from "../chat-timeline";
import type { ChildRunState, DelegationApprovalRequest } from "@/lib/types";

/**
 * File attached to a user message.
 * Either `path` (workspace-relative) or `url` (external/preview) may be set.
 */
export interface AttachedUserFile {
  name: string;
  path?: string;
  url?: string;
  type?: string;
}

/**
 * A single conversational turn: the user prompt, the agent trace,
 * and the final deliverable produced for that turn.
 */
export interface ChatTurn {
  id: string;
  jobId: string;
  prompt: string;
  timestamp: string;
  steps: StepEvent[];
  childRuns?: Record<string, ChildRunState>;
  finalResult: string | null;
  status: "completed" | "failed";
  tokensUsed?: { input: number; output: number; total: number };
  producedFiles?: { name: string; path: string }[];
  attachedFiles?: AttachedUserFile[];
  model?: string;
  mode?: "agent" | "chat";
  usage?: {
    cost_usd: number;
    latency_ms: number;
    tokens_per_second: number;
    provider: string;
    model: string;
  };
}

/**
 * Props accepted by the top-level ChatThread container.
 */
export interface ChatThreadProps {
  historyTurns: ChatTurn[];
  submittedPrompt: string;
  submittedFiles?: AttachedUserFile[];
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
  tokensUsed?: { input: number; output: number; total: number };
  costUsd?: number | null;
  latencyMs?: number | null;
  tokensPerSecond?: number | null;
  activeChildRuns?: Record<string, ChildRunState>;
  pendingApprovals?: Record<string, DelegationApprovalRequest>;
  onApproveDelegation?: (approvalId: string, editedInstruction?: string) => Promise<void>;
  onRejectDelegation?: (approvalId: string) => Promise<void>;
  onCancelSubtask?: (childRunId: string) => Promise<void>;
}

/**
 * Human-interactive tool names recognized by the chat thread renderer.
 * Kept as a shared constant so extraction and resolution logic remain
 * consistent across the component tree.
 */
export const HUMAN_TOOL_NAMES = new Set(["ask_human", "human_input"]);

/**
 * Visual phases for the human-in-the-loop submission flow.
 *
 * idle       -> card is displayed and awaiting user input
 * sending    -> card flies away (letter-send animation) after submission
 * verifying  -> compact loader shown while confirming delivery
 * success    -> green check confirmation is briefly displayed
 * fading     -> confirmation fades out with a subtle scale-down
 */
export type HumanSubmitPhase = "idle" | "sending" | "verifying" | "success" | "fading";

/** Duration (ms) of the letter-fly animation when submitting an answer. */
export const SENDING_ANIMATION_MS = 450;

/** Duration (ms) the success confirmation is held before fading out. */
export const SUCCESS_HOLD_MS = 900;

/** Duration (ms) of the fade-out transition on the success confirmation. */
export const FADING_DURATION_MS = 400;

/** Fallback (ms) used to leave the verifying phase if no upstream signal arrives. */
export const VERIFY_FALLBACK_MS = 5000;
