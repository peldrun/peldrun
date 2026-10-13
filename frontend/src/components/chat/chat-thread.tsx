// Path: frontend/src/components/chat/chat-thread.tsx
"use client";

/**
 * Backward-compatibility shim for the chat-thread module.
 *
 * The original monolithic implementation has been split into the
 * `./chat-thread/` sub-folder for maintainability. This file re-exports the
 * public surface so existing imports keep working without changes:
 *
 *   import { ChatThread } from "@/components/chat/chat-thread";
 *   import type { ChatTurn, AttachedUserFile } from "@/components/chat/chat-thread";
 *
 * New code should import directly from the folder root:
 *
 *   import { ChatThread } from "@/components/chat/chat-thread";
 *
 * (The folder and this file share the same module specifier base; the
 * bundler resolves the explicit `.tsx` file first, so this shim remains the
 * entry point until it is removed.)
 */

export { ChatThread, default } from "./chat-thread/ChatThread";

export type {
  ChatTurn,
  ChatThreadProps,
  AttachedUserFile,
  HumanSubmitPhase,
} from "./chat-thread/types";

export {
  HUMAN_TOOL_NAMES,
  SENDING_ANIMATION_MS,
  SUCCESS_HOLD_MS,
  FADING_DURATION_MS,
  VERIFY_FALLBACK_MS,
} from "./chat-thread/types";
