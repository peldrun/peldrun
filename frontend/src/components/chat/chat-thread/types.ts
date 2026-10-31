/**
 * Shared type definitions and constants for the chat thread module.
 */

import type { StepEvent } from "../chat-timeline";
import type { ChildRunState, DelegationApprovalRequest } from "@/lib/types";

export interface AttachedUserFile {
  name: string;
  path?: string;
  url?: string;
  type?: string;
  /**
   * Multimodal image reference.
   *
   * Populated for images that were uploaded to the temporary artifact
   * store (``POST /api/files/upload-temp``). When present, the card
   * renders a preview from ``/api/files/temp/{fileId}`` instead of the
   * legacy ``/api/files/raw/{name}`` fallback, so the thumbnail works
   * even after a page reload (unlike ``URL.createObjectURL`` handles,
   * which die with the tab).
   *
   * Always ``undefined`` for non-image attachments and for files that
   * arrived via the legacy ``POST /api/files/upload`` pipeline.
   */
  fileId?: string;
  /** MIME type of the file, when known (e.g. ``"image/png"``). */
  mimeType?: string;
  /** Image width in pixels, when known. Advisory only. */
  width?: number;
  /** Image height in pixels, when known. Advisory only. */
  height?: number;
}

export type ChatStatus = "idle" | "running" | "paused" | "completed" | "failed";


export interface ChatTurn {
  id: string;
  jobId: string;
  prompt: string;
  timestamp: string;
  steps: StepEvent[];
  childRuns?: Record<string, ChildRunState>;
  finalResult: string | null;
  status: "completed" | "failed" | "stopped" | "paused";
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
  status: "idle" | "running" | "paused" | "completed" | "failed";
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

  // Lifecycle control and regeneration callbacks
  onPauseJob?: () => void;
  onResumeJob?: () => void;
  onRegenerate?: (turnIndex: number, turn?: ChatTurn) => void;
  onOpenReview?: (operationId: string) => void;
}

export const HUMAN_TOOL_NAMES = new Set(["ask_human", "human_input"]);

export type HumanSubmitPhase = "idle" | "sending" | "verifying" | "success" | "fading";

export const SENDING_ANIMATION_MS = 450;
export const SUCCESS_HOLD_MS = 900;
export const FADING_DURATION_MS = 400;
export const VERIFY_FALLBACK_MS = 5000;
