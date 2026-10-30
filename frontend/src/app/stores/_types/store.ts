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
  // Optional three-state classification used by the UI to pick the
  // badge color and label.
  //
  //   "success"  → green  "Connected"
  //   "failure"  → red    "Failed"
  //   "deferred" → amber  "OAuth server"
  //
  // "deferred" is set by the OAuth-server path in
  // useExtensionActions: verification requires a Bearer token that
  // only exists inside a chat session, so the Store UI cannot test
  // the connection. A green "Connected" badge would claim a success
  // that never happened; a red "Failed" badge would claim a failure
  // that also never happened. Amber honestly communicates "not
  // verified here — verify in chat".
  //
  // When omitted, the UI falls back to `ok` for backward
  // compatibility with any existing payloads.
  kind?: "success" | "failure" | "deferred";
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
  core_color: "#10b981",
  description: "",
  system_prompt: "",
  tools: ["bash", "python_execute", "str_replace_editor", "web_search"],
  // Always initialise ``mcp_servers`` to an empty array — never
  // ``undefined`` — so a create/update that intentionally leaves
  // MCP servers unselected sends ``[]`` rather than omitting the
  // field. The backend treats both identically today, but this
  // keeps the form payload shape stable for future validation.
  mcp_servers: [],
  max_steps: 30,
};

/**
 * Upload category used by the unified upload modal.
 * - "tool"      → custom Python tool (.py) or JSON manifest
 * - "extension" → MCP server manifest (.json)
 */
export type UploadCategory = "tool" | "extension";

/**
 * Metadata for a single tool as seen by the Agent Tools modal.
 *
 * ``in_manifest`` marks tools that already appear in the agent's
 * baseline manifest (shown in the "Manifest Tools" section). Tools
 * with ``in_manifest = false`` are eligible for the "Extra Tools"
 * section.
 */
export interface AgentToolInfo {
  id: string;
  name: string;
  description: string;
  category: string;
  is_builtin: boolean;
  safety_level?: string | null;
  in_manifest: boolean;
}

/**
 * Full tool state for an agent, returned by ``GET /agents/{id}/tools``.
 *
 * The three user-facing lists follow the model:
 *   - ``manifest_tools``: developer-authored baseline (never mutated)
 *   - ``disabled_tools``: subset of manifest_tools to exclude at runtime
 *   - ``extra_tools``:    additions from the Store catalog
 *   - ``effective_tools``: what the runtime will actually use
 *
 * ``tools_editable`` reflects ``settings.tools_editable`` in the manifest
 * (locked agents cannot open the tools modal).
 */
export interface AgentToolsState {
  manifest_tools: string[];
  disabled_tools: string[];
  extra_tools: string[];
  effective_tools: string[];
  tools_editable: boolean;
  available_tools: AgentToolInfo[];
}
