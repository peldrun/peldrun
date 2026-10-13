// Path: frontend/src/components/chat/chat-thread/index.ts
/**
 * Barrel export for the chat-thread module.
 *
 * Consumers should import from the folder root rather than reaching into
 * individual files. This keeps the public surface stable while allowing
 * internal organization to evolve.
 *
 * Usage:
 *   import { ChatThread } from "@/components/chat/chat-thread";
 *   import type { ChatTurn, AttachedUserFile } from "@/components/chat/chat-thread";
 */

// Primary container
export { ChatThread, default } from "./ChatThread";

// Sub-components (exposed for reuse/testing)
export { HistoricalTurn } from "./HistoricalTurn";
export { UserMessageBubble } from "./UserMessageBubble";
export { AttachedFileCard } from "./AttachedFileCard";
export { HumanInputCard } from "./HumanInputCard";
export { LiveStatusIndicator } from "./LiveStatusIndicator";

// Hooks
export { useHumanToolDetection } from "./useHumanToolDetection";

// Utilities
export { parsePromptAndAttachments, resolveFileCategory } from "./utils";

// Types and constants
export {
  HUMAN_TOOL_NAMES,
  SENDING_ANIMATION_MS,
  SUCCESS_HOLD_MS,
  FADING_DURATION_MS,
  VERIFY_FALLBACK_MS,
} from "./types";

export type {
  AttachedUserFile,
  ChatTurn,
  ChatThreadProps,
  HumanSubmitPhase,
} from "./types";

export type { AttachedCardProps } from "./AttachedFileCard";
export type { UserMessageBubbleProps } from "./UserMessageBubble";
export type { HistoricalTurnProps } from "./HistoricalTurn";
export type { HumanInputCardProps } from "./HumanInputCard";
export type { LiveStatusIndicatorProps } from "./LiveStatusIndicator";
export type { HumanToolDetectionResult } from "./useHumanToolDetection";
