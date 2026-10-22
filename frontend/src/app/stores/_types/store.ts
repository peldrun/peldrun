// frontend/src/app/stores/_types/store.ts

import type { AgentUpsertPayload } from "@/lib/types";

/**
 * Filter modes for the Agents section.
 */
export type AgentFilter = "all" | "builtin" | "custom" | "active" | "disabled";

/**
 * Filter modes for the Tools section.
 */
export type ToolFilter = "all" | "builtin" | "custom";

/**
 * Result of an MCP extension connectivity test.
 */
export interface McpTestResult {
  ok: boolean;
  message: string;
}

/**
 * Map keyed by extension id, storing the last test result.
 */
export type McpTestResultMap = Record<string, McpTestResult>;

/**
 * Default payload used when creating a new agent via the builder modal.
 * Kept in a single place so both the page and the modal agree on defaults.
 */
export const DEFAULT_AGENT_FORM: AgentUpsertPayload = {
  name: "",
  role: "Specialist",
  icon: "Sparkles",
  description: "",
  system_prompt: "",
  tools: ["bash", "python_execute", "str_replace_editor", "web_search"],
  max_steps: 30,
};

/**
 * Upload category used by the unified upload modal.
 * - "tool"      → custom Python tool (.py) or JSON manifest
 * - "extension" → MCP server manifest (.json)
 */
export type UploadCategory = "tool" | "extension";
