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

/**
 * Canonical lifecycle status for chat runs.
 *
 * MUST stay in sync with `ChatStatus` in
 * `components/chat/chat-container/types.ts` (which mirrors
 * `SessionStatus` from `docs/state-contracts.md` §3).
 *
 * Duplication is intentional: importing from `chat-container/types.ts`
 * here would create a build-order dependency from a leaf presentational
 * module (`chat-thread/`) back into a container module
 * (`chat-container/`), which is an anti-pattern. When the two diverge,
 * the container's type is authoritative and this one must be updated
 * to match.
 *
 * Only five values carry rendering semantics inside ChatThread
 * (`idle`, `running`, `paused`, `completed`, `failed`). The remaining
 * values are still accepted so that ChatContainer can pass its full
 * status through without an adapter; each falls through to the
 * nearest equivalent behavior (see comments in ChatThread's
 * conditional blocks).
 */
export type ChatStatus =
  | "idle"
  | "pending"
  | "queued"
  | "starting"
  | "running"
  | "waiting_for_input"
  | "retrying"
  // "paused" intentionally omitted — see chat-container/types.ts
  // for the Phase H rationale.
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed"
  | "interrupted";


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
  /**
   * Provider that produced this turn.
   *
   * Captured at run START time (see ChatContainer's
   * `runStartProviderRef`) so that even if the user changes the
   * active provider later via EngineSelector, historical turns
   * continue to display the provider that actually generated them.
   *
   * `null` / `undefined` on turns that predate this field — the
   * consumer (`HistoricalTurn`) falls back to the currently-active
   * provider for backward compatibility.
   */
  provider?: string | null;
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
  chatId?: string;
  submittedPrompt: string;
  submittedFiles?: AttachedUserFile[];
  sessionTimestamp: string;
  humanQuery: string | null;
  humanAnswer: string;
  setHumanAnswer: (val: string) => void;
  onSendHumanAnswer: (customAnswer?: string) => void;
  execMode: "agent" | "chat";
  activeGroupedSteps: Record<number, StepEvent[]>;

  /**
   * [CHAT-REASONING] Dedicated reasoning text for Chat mode.
   *
   * Chat mode does NOT use the step-based timeline (`activeGroupedSteps`
   * is always `{}` in that mode). Reasoning is delivered as a single
   * continuous string that feeds `ChatReasoningPanel`. Agent mode
   * leaves this `undefined` and uses `activeGroupedSteps` instead.
   */
  chatReasoningText?: string;

  currentStepNum: number;
  /**
   * Canonical run status. ChatThread's conditional rendering only
   * distinguishes `running` / `failed`; all other values behave as
   * "not running, not failed" (which is correct for `pending`,
   * `starting`, `waiting_for_input`, `cancelling`, `cancelled`,
   * `interrupted`, `retrying`, `queued`).
   *
   * Pause was removed in Phase H — see chat-container/types.ts.
   */
  status: ChatStatus;
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
  /**
   * Currently-selected provider display name (from useAppStorage
   * `active_provider`). Used as the FALLBACK provider when rendering
   * a historical turn whose `provider` field is `null` / `undefined`
   * (i.e., a turn created before the provider-capture feature was
   * introduced). The active turn always displays this value directly.
   *
   * Passed down by ChatContainer; consumed by ChatThread and
   * forwarded to HistoricalTurn / ChatDeliverable.
   */
  activeProvider?: string;
  /**
   * Provider snapshot for the CURRENT turn (the run that produced the
   * last submitted prompt).
   *
   * Unlike `activeProvider` (the user's current EngineSelector
   * selection), this value reflects the provider that ACTUALLY served
   * the run. It is captured at run START time by ChatContainer's
   * `runStartProviderRef`, so switching the EngineSelector mid-turn
   * does NOT relabel the current turn.
   *
   * Falls back to `activeProvider` when undefined (external consumers
   * that do not track per-turn snapshots).
   */
  currentTurnProvider?: string;
  /**
   * Model snapshot for the current turn. Same rationale as
   * `currentTurnProvider`.
   */
  currentTurnModel?: string;
  tokensUsed?: { input: number; output: number; total: number };
  costUsd?: number | null;
  latencyMs?: number | null;
  tokensPerSecond?: number | null;
  activeChildRuns?: Record<string, ChildRunState>;
  pendingApprovals?: Record<string, DelegationApprovalRequest>;
  onApproveDelegation?: (approvalId: string, editedInstruction?: string) => Promise<void>;
  onRejectDelegation?: (approvalId: string) => Promise<void>;
  onCancelSubtask?: (childRunId: string) => Promise<void>;

  // Lifecycle control and regeneration callbacks.
  // Pause/Resume hooks were removed in Phase H.
  onRegenerate?: (turnIndex: number, turn?: ChatTurn) => void;
  onOpenReview?: (operationId: string) => void;
}

export const HUMAN_TOOL_NAMES = new Set(["ask_human", "human_input"]);

export type HumanSubmitPhase = "idle" | "sending" | "verifying" | "success" | "fading";

export const SENDING_ANIMATION_MS = 450;
export const SUCCESS_HOLD_MS = 900;
export const FADING_DURATION_MS = 400;
export const VERIFY_FALLBACK_MS = 5000;
