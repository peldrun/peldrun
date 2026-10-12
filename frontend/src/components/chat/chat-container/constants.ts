// Path: frontend/src/components/chat/chat-container/constants.ts

/**
 * Human-interactive tool names recognized by the container's SSE handlers.
 * Kept as a Set to allow fast lookups both in event routing and in the
 * observation-clearing guard.
 */
export const HUMAN_TOOL_NAMES = new Set(["ask_human", "human_input"]);

/**
 * Diagnostic flag: enable verbose SSE event logging in browser console.
 * Set to false once the ask_human rendering issue is confirmed resolved.
 */
export const DEBUG_SSE = false;

/**
 * Complete list of all SSE event types the backend may emit.
 * The frontend must listen for every type; otherwise events like
 * tool_called / tool_retry / status are silently dropped.
 */
export const SSE_EVENT_TYPES = [
  // Control plane
  "status",
  "run_accepted",
  "run_started",

  // Execution plane
  "step_start",
  "step_end",
  "thought",
  "tool_call",
  "tool_called",
  "tool_retry",
  "observation",

  // Artifact lifecycle
  "artifact_created",
  "artifact_updated",
  "artifact_deleted",
  "artifact_moved",
  "artifact",

  // Terminal
  "final",
  "done",
  "error",
  "cancelled",

  // Transport / heartbeat
  "ping",
] as const;

/**
 * Delegation / child-run lifecycle events. These are dispatched by the
 * multi-agent orchestrator and carry the payload needed to render
 * approval cards and child-run status widgets.
 */
export const DELEGATION_EVENT_TYPES = [
  "delegation_requested",
  "approval_required",
  "approval_resolved",
  "child_run_started",
  "child_run_step",
  "child_run_completed",
  "child_run_failed",
  "child_run_cancelled",
] as const;
