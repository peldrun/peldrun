// Path: frontend/src/components/chat/chat-thread/useHumanToolDetection.ts
"use client";

/**
 * Custom hook that inspects the currently active step sequence to detect
 * whether the agent is paused waiting on a human-interactive tool call.
 *
 * Detection strategy:
 *  1. Walk the active steps in reverse to locate the most recent call to a
 *     tool in `HUMAN_TOOL_NAMES` (e.g. `ask_human`, `human_input`).
 *  2. A call is considered "resolved" only when a subsequent observation was
 *     emitted by a NON-human tool. Observations produced by the human tool
 *     itself are ignored, otherwise the awaiting card would immediately
 *     disappear.
 *  3. If the call is unresolved, attempt to parse its content as JSON and
 *     extract the prompt, input type, and available options.
 *
 * The hook does not own any state; it derives its result purely from the
 * inputs, so it re-evaluates on every render and stays consistent with the
 * upstream `StepEvent` stream.
 */

import { HUMAN_TOOL_NAMES } from "./types";
import type { StepEvent } from "../chat-timeline";

export interface HumanToolDetectionResult {
  /** Whether the agent is currently waiting for human input. */
  isHumanWaiting: boolean;

  /** The resolved prompt text to display to the user. */
  detectedPrompt: string | null;

  /** The resolved input type (e.g. "text", "confirm", "boolean"). */
  detectedType: string;

  /** Quick-pick options supplied by the agent (may be empty). */
  detectedOptions: string[];
}

/**
 * Compute the human tool detection result from the active step list and the
 * current run status.
 *
 * @param activeStepsList  Flattened list of all active step events.
 * @param humanQuery       Upstream-provided prompt override (takes priority).
 * @param status           Current run status (idle | running | completed | failed).
 * @returns                Detection result describing the waiting state.
 */
export function useHumanToolDetection(
  activeStepsList: StepEvent[],
  humanQuery: string | null,
  status: string
): HumanToolDetectionResult {
  // Locate the most recent human tool invocation in the active steps sequence.
  const humanCallIndex = [...activeStepsList]
    .map((s, idx) => ({ s, idx }))
    .reverse()
    .find(({ s }) => HUMAN_TOOL_NAMES.has(s.toolName || "") && s.type === "tool_call");

  /**
   * An observation is only considered as "resolving" the human call if it
   * was produced by a NON-human tool. Observations emitted by the human tool
   * itself (reflecting its own result) must be ignored, otherwise the pending
   * human prompt would immediately be marked as resolved and the awaiting
   * card would never render.
   */
  const isCallResolved =
    humanCallIndex !== undefined &&
    activeStepsList.slice(humanCallIndex.idx + 1).some((s) => {
      if (s.type !== "observation") return false;
      if (HUMAN_TOOL_NAMES.has(s.toolName || "")) return false;
      return true;
    });

  const pendingHumanCall = humanCallIndex && !isCallResolved ? humanCallIndex.s : undefined;

  let detectedPrompt: string | null = humanQuery;
  let detectedType = "text";
  let detectedOptions: string[] = [];

  if (pendingHumanCall && pendingHumanCall.content) {
    try {
      const parsedArgs =
        typeof pendingHumanCall.content === "object"
          ? pendingHumanCall.content
          : JSON.parse(pendingHumanCall.content);

      if (parsedArgs && typeof parsedArgs === "object") {
        detectedPrompt =
          humanQuery ||
          parsedArgs.prompt ||
          parsedArgs.question ||
          parsedArgs.query ||
          parsedArgs.message ||
          "";
        detectedType = (parsedArgs.input_type || "text").toLowerCase();
        if (Array.isArray(parsedArgs.options) && parsedArgs.options.length > 0) {
          detectedOptions = parsedArgs.options.map(String);
        } else if (detectedType === "confirm" || detectedType === "boolean") {
          detectedOptions = ["Yes", "No"];
        }
      }
    } catch {
      if (!detectedPrompt) {
        detectedPrompt = pendingHumanCall.content;
      }
    }
  }

  /**
   * The waiting card should render whenever there is a pending human call
   * OR a detected prompt, and the agent is still running. Previously this
   * required both `detectedPrompt` AND the call to be unresolved, which failed
   * whenever the observation from ask_human cleared the humanQuery upstream.
   */
  const isHumanWaiting = Boolean(
    (detectedPrompt || pendingHumanCall) &&
      !isCallResolved &&
      (status === "running" || status === "waiting_for_input")
  );

  return {
    isHumanWaiting,
    detectedPrompt,
    detectedType,
    detectedOptions,
  };
}

export default useHumanToolDetection;
