export type ServerEventName =
  | "status"
  | "run_accepted"
  | "run_started"
  | "step_start"
  | "thought"
  | "tool_call"
  | "tool_called"
  | "tool_retry"
  | "observation"
  | "step_end"
  | "ask_human"
  | "artifact_created"
  | "artifact_updated"
  | "artifact_deleted"
  | "artifact_moved"
  | "final"
  | "error"
  | "cancelled"
  | "done"
  | "ping";

export type RunStatus =
  | "pending"
  | "queued"
  | "starting"
  | "running"
  | "waiting_for_input"
  | "retrying"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed";

export interface Step {
  id: string;
  step_number: number;
  type: string;
  content: string;
  timestamp: string;
  tool_name?: string;
  tool_args?: Record<string, any>;
  attempt?: number;
  status?: string;
}

export interface ToolPair {
  kind: "tool";
  id: string;
  tool: string;
  args: Record<string, unknown>;
  startedAt: number;
  output: string | null;
  durationMs: number | null;
  attempt?: number;
  maxAttempts?: number;
  isError?: boolean;
  status?: "running" | "retrying" | "completed" | "failed" | "waiting_for_input";
  error?: string | null;
  retryDelay?: number;
  requiresInput?: boolean;
  requestId?: string;
}

export interface ThoughtItem {
  kind: "thought";
  id: string;
  text: string;
  at: number;
}

export interface HumanInputItem {
  kind: "human_input";
  id: string;
  requestId: string;
  question: string;
  options: string[];
  inputType: string;
  status: "pending" | "answered" | "cancelled";
  answer?: string | null;
  at: number;
}

export interface ArtifactItem {
  kind: "artifact";
  id: string;
  name: string;
  path: string;
  operation: "created" | "updated" | "deleted" | "moved";
  sizeBytes?: number;
  revision?: number;
  at: number;
}

export type StepItem = ThoughtItem | ToolPair | HumanInputItem | ArtifactItem;

export interface StepGroup {
  id: string;
  step: number;
  step_id?: string;
  startedAt: number | null;
  finishedAt: number | null;
  durationMs: number | null;
  items: StepItem[];
}

export interface RunState {
  jobId: string;
  prompt: string;
  status: RunStatus;
  startedAt: number;
  steps: StepGroup[];
  finalAnswer: string | null;
  error: string | null;
  lastEventAt: number;
  droppedEvents: number;
  lastSequence: number;
  seenEventIds: string[];
}

export interface SSEEnvelope {
  jobId?: string;
  runId?: string;
  id?: string;
  event_id?: string;
  type: ServerEventName;
  step: number;
  step_id?: string;
  seq?: number;
  sequence?: number;
  data: Record<string, any>;
}

// ==========================================
// Sovereign Store & Agent Manifest Contracts
// ==========================================
export interface AgentManifest {
  id: string;
  name: string;
  role?: string;
  icon?: string;
  description?: string;
  system_prompt: string;
  tools: string[];
  max_steps: number;
  is_builtin?: boolean;
  created_at?: string;
  updated_at?: string;
  status?: "active" | "disabled" | string;
}

export interface ToolDefinition {
  id: string;
  name: string;
  description?: string;
  category?: string;
  safety_level?: string;
  is_builtin?: boolean;
  is_enabled?: boolean;
  status?: "active" | "disabled" | string;
  parameters?: Record<string, any>;
}

export interface ExtensionDefinition {
  id: string;
  name: string;
  type: string;
  status: string;
  command?: string;
  description: string;
}

export interface AgentUpsertPayload {
  name: string;
  role?: string;
  icon?: string;
  description?: string;
  system_prompt: string;
  tools: string[];
  max_steps?: number;
}

// ==========================================
// Model Metadata Interfaces
// ==========================================
export interface ModelQuantization {
  name: string | null;
  bits_per_weight: number | null;
}
export interface ModelInstanceConfig {
  context_length: number;
  eval_batch_size?: number;
  flash_attention?: boolean;
  num_experts?: number;
  offload_kv_cache_to_gpu?: boolean;
}
export interface ModelCapabilities {
  vision: boolean;
  trained_for_tool_use: boolean;
  reasoning?: boolean;
}
export interface ModelMetadata {
  type: string;
  publisher?: string | null;
  key: string;
  display_name?: string | null;
  architecture?: string | null;
  quantization?: ModelQuantization | null;
  size_bytes?: number | null;
  params_string?: string | null;
  loaded_instances?: Array<{ id: string; config: ModelInstanceConfig }>;
  max_context_length?: number | null;
  format?: string | null;
  capabilities?: ModelCapabilities | null;
  description?: string | null;
}

// ==========================================
// Token Accounting & Telemetry Domain Models (P1-03)
// ==========================================
export type TokenUsageSource = "provider" | "estimated" | "unknown";

export interface TokenUsageInfo {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  cached_input_tokens?: number | null;
  reasoning_output_tokens?: number | null;
  source?: TokenUsageSource;
  estimated?: boolean;
  tokenizer_id?: string | null;
  tokenizer_version?: string | null;
  estimation_method?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
}

export interface TurnUsage {
  turn_id: string;
  timestamp: number;
  usage?: TokenUsageInfo;
  cost_usd?: number | null;
  cost_nano_usd?: number | null;
  latency_ms?: number | null;
  ttft_ms?: number | null;
  tokens_per_second?: number | null;
  model?: string | null;
  provider?: string | null;
}

export interface ChatUsageSummary {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens?: number;
  reasoning_output_tokens?: number;
  estimated_tokens?: number;
  total_cost_usd: number;
  cost_nano_usd?: number;
  llm_call_count?: number;
  turn_count?: number;
}