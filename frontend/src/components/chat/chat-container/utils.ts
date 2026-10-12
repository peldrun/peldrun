// Path: frontend/src/components/chat/chat-container/utils.ts

import type { StepEvent } from "@/components/chat/chat-timeline";
import { DEBUG_SSE } from "./constants";

/**
 * Safely stringify any value for display. Guards against empty objects/arrays
 * and returns an empty string when the value carries no useful information.
 */
export function safeRender(val: any): string {
  if (val === null || val === undefined) return "";
  if (typeof val === "string") {
    const trimmed = val.trim();
    if (trimmed === "{}" || trimmed === "null" || trimmed === "undefined") return "";
    return val;
  }
  if (typeof val === "number" || typeof val === "boolean") return String(val);
  try {
    const str = JSON.stringify(val, null, 2);
    if (str === "{}" || str === "[]") return "";
    return str;
  } catch {
    return String(val);
  }
}

/**
 * Robustly extracts a human-readable prompt string from tool-call arguments.
 *
 * Arguments may arrive in several shapes depending on the SSE bridge:
 *   1. Plain object:                  { prompt: "...", input_type: "text" }
 *   2. JSON-encoded string:           "{\"prompt\":\"...\"}"
 *   3. Free-form string (fallback):   "What is your name?"
 *   4. Empty / malformed:             "" or "{}"
 *
 * Returns both the extracted prompt text and the parsed object (when available)
 * so the caller can reuse the object for step recording without re-parsing.
 */
export function extractHumanPrompt(rawArgs: any): {
  promptText: string;
  parsedArgs: any | null;
} {
  let parsedArgs: any | null = null;

  if (rawArgs && typeof rawArgs === "object" && !Array.isArray(rawArgs)) {
    parsedArgs = rawArgs;
  } else if (typeof rawArgs === "string") {
    const trimmed = rawArgs.trim();
    if (trimmed.startsWith("{")) {
      try {
        parsedArgs = JSON.parse(trimmed);
      } catch (e) {
        if (DEBUG_SSE) {
          console.warn(
            "[extractHumanPrompt] JSON.parse failed for string args:",
            e,
            trimmed.slice(0, 200)
          );
        }
      }
    }
  }

  const promptText =
    (parsedArgs &&
      (parsedArgs.prompt ||
        parsedArgs.question ||
        parsedArgs.query ||
        parsedArgs.message)) ||
    (typeof rawArgs === "string" &&
    rawArgs.length > 0 &&
    !rawArgs.trim().startsWith("{")
      ? rawArgs
      : "") ||
    "";

  return { promptText, parsedArgs };
}

/** Human-readable byte size formatter used in the landing drop-zone. */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Groups visible step events by their step number, dropping any event that
 * carries neither content nor a tool name (i.e. invisible noise).
 */
export function groupStepEvents(evts: StepEvent[]): Record<number, StepEvent[]> {
  return evts.reduce((acc, s) => {
    const isVisible =
      (s.content && s.content.trim() !== "") || Boolean(s.toolName);
    if (!isVisible) return acc;
    if (!acc[s.step]) acc[s.step] = [];
    acc[s.step].push(s);
    return acc;
  }, {} as Record<number, StepEvent[]>);
}
