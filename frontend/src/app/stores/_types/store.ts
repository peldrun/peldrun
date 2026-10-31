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


// ─────────────────────────────────────────────────────────────────
// Skills
// ─────────────────────────────────────────────────────────────────
//
// Types mirror the HTTP responses from ``omweb.routers.skills``:
//
//   GET  /api/skills                      → SkillSummary[]  (list)
//   GET  /api/skills/{id}                 → SkillDetail     (single)
//   POST /api/skills/install              → SkillInstallResponse
//   POST /api/skills/{id}/toggle          → SkillToggleResponse
//   DELETE /api/skills/{id}               → { deleted: true }
//   GET  /api/skills/{id}/preview         → SkillPreview
//
// These are duplicated locally rather than fetched from a shared
// module because the store page is the only consumer. If a second
// consumer appears (e.g. a mobile client), extract to ``@/lib/types``.

/**
 * Filter modes for the Skills section.
 *
 *   "all"      → every installed skill
 *   "builtin"  → shipped skills (``is_builtin: true``)
 *   "custom"   → user-installed skills (``is_builtin: false``)
 */
export type SkillFilter = "all" | "builtin" | "custom";

/**
 * Author block as it appears in a skill manifest. All fields
 * optional — a skill may be installed by a user who never filled
 * them in.
 */
export interface SkillAuthor {
  name?: string;
  url?: string;
  email?: string;
}

/**
 * Summary of one skill as returned by the list endpoint.
 *
 * Deliberately excludes the full ``manifest`` and ``frontmatter``
 * (which can each be several KB) and the sanitized ``icon_svg``
 * (up to 32 KB). Those are fetched on demand via the detail
 * endpoint and the icon endpoint respectively.
 */
export interface SkillSummary {
  id: string;
  version: string;
  name: string;
  display_name: string;
  description: string;
  short_description: string;
  status: string;
  enabled: boolean;
  is_builtin: boolean;
  is_deprecated: boolean;
  chat_invocable: boolean;
  agent_assignable: boolean;
  workflow_node: boolean;
  has_icon_svg: boolean;
  has_icon_path: boolean;
  icon_path: string | null;
  tags: string[];
  author: SkillAuthor;
  publisher_id: string | null;
  trust: string;
  license: string | null;
  homepage: string | null;
  docs_url: string | null;
  release_date: string | null;
  stability: string;
  capabilities: Record<string, unknown>;
  execution: Record<string, unknown>;
  package_dir: string;
}

/**
 * Full skill detail — includes the raw manifest and SKILL.md frontmatter.
 */
export interface SkillDetail extends SkillSummary {
  manifest: Record<string, unknown>;
  frontmatter: Record<string, unknown>;
}

/**
 * Response from ``POST /api/skills/install``.
 */
export interface SkillInstallResponse {
  status: string;
  skill: SkillSummary;
  was_upgrade: boolean;
  warnings: string[];
}

/**
 * Response from ``POST /api/skills/{id}/toggle``.
 */
export interface SkillToggleResponse {
  status: string;
  skill: SkillSummary;
}

/**
 * Response from ``GET /api/skills/{id}/preview`` — the full
 * instructions text for the details drawer.
 */
export interface SkillPreview {
  status: string;
  skill_id: string;
  version: string;
  instructions: string;
  instructions_length: number;
  frontmatter: Record<string, unknown>;
}
