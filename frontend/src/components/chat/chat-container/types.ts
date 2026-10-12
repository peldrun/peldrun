// Path: frontend/src/components/chat/chat-container/types.ts

/**
 * Shared type definitions for the chat container and its extracted hooks.
 *
 * Centralising these types keeps the hook interfaces small and stable,
 * and prevents accidental divergence between producer/consumer signatures.
 */

/** Live state of a child (delegated) run spawned by a parent agent. */
export interface ChildRunState {
  childRunId: string;
  parentRunId: string;
  taskId: string;
  targetAgentId: string;
  instruction: string;
  status: "running" | "completed" | "failed" | "cancelled";
  startedAt: number;
  steps: any[];
  deliverables: any[];
  tokensUsed?: any;
  durationSeconds?: number;
  error?: string;
}

/**
 * Pending delegation approval request awaiting a human decision.
 * Resolved requests are removed from the pending map by the SSE handler.
 */
export interface DelegationApprovalRequest {
  approvalRequestId: string;
  taskId: string;
  parentRunId: string;
  targetAgentId: string;
  instruction: string;
  status: "pending" | "resolved";
  timeoutSeconds: number;
  createdAt: number;
}

/** Props accepted by the top-level ChatContainer component. */
export interface ChatContainerProps {
  initialJobId?: string | null;
}

/** Lifecycle status of the current chat run. */
export type ChatStatus = "idle" | "running" | "completed" | "failed";

/** Aggregated token usage counters for the current or completed run. */
export interface TokenUsage {
  input: number;
  output: number;
  total: number;
}

/** Optional cost/latency metrics emitted alongside terminal SSE events. */
export interface UsageMetrics {
  cost_usd?: number;
  latency_ms?: number;
  tokens_per_second?: number;
}

/** A file produced by an agent run and shown in the workspace panel. */
export interface ProducedFile {
  name: string;
  path: string;
}
