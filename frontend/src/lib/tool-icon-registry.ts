// frontend/src/lib/tool-icon-registry.ts

import type { ComponentType } from "react";
import {
  Code2, Database, FileCode2, FileText, GitBranch, Globe,
  MessageCircleQuestion, Monitor, Puzzle, Search, Square, Terminal, Users, Wrench,
} from "lucide-react";

export type LucideIconComponent = ComponentType<{ size?: number; className?: string; }>;

export interface ToolManifest {
  id: string;
  name: string;
  display_name?: string;
  category?: string;
  icon?: string;
}

export const TOOL_ICON_REGISTRY: Record<string, LucideIconComponent> = {
  bash: Terminal, sh: Terminal, shell: Terminal, terminal: Terminal,
  python: Code2, python_execute: Code2, python3: Code2, code: Code2, code2: Code2, execute: Code2,
  editor: FileCode2, str_replace_editor: FileCode2, file_code: FileCode2, file_code2: FileCode2, edit: FileCode2,
  file: FileText, file_saver: FileText, file_text: FileText, file_write: FileText, save: FileText,
  search: Search, web_search: Search, websearch: Search,
  globe: Globe, http: Globe, http_fetch: Globe, fetch: Globe, request: Globe, network: Globe,
  browser: Monitor, browser_interactive: Monitor, monitor: Monitor, playwright: Monitor,
  ask_human: MessageCircleQuestion, human: MessageCircleQuestion, human_input: MessageCircleQuestion,
  message: MessageCircleQuestion, question: MessageCircleQuestion,
  git: GitBranch, git_branch: GitBranch, source_control: GitBranch, version_control: GitBranch,
  delegate: Users, delegate_task: Users, delegate_subtask: Users, users: Users, handover: Users, team: Users,
  mcp: Puzzle, puzzle: Puzzle, plugin: Puzzle, database: Database, db: Database, sql: Database,
  terminate: Square, square: Square, done: Square, stop: Square,
};

export const CATEGORY_ICONS: Record<string, LucideIconComponent> = {
  web: Globe, network: Globe, http: Globe, search: Search,
  code: Code2, execution: Code2, development: Code2, python: Code2,
  file: FileText, files: FileText, filesystem: FileText, editor: FileCode2,
  system: Terminal, shell: Terminal, os: Terminal, browser: Monitor, automation: Monitor,
  interaction: MessageCircleQuestion, human: MessageCircleQuestion,
  vcs: GitBranch, scm: GitBranch, delegation: Users, agent: Users,
  mcp: Puzzle, protocol: Puzzle, data: Database, analytics: Database,
};

export function extractIconBasename(raw: string | undefined): string {
  if (!raw || typeof raw !== "string") return "";
  const trimmed = raw.trim();
  if (!trimmed) return "";
  const base = trimmed.split("/").pop() ?? "";
  return base.replace(/\.svg$/i, "").toLowerCase().trim();
}

export function resolveToolIconComponent(
  id: string | undefined,
  iconBasename?: string,
  category?: string,
): LucideIconComponent | null {
  const iconName = (iconBasename || "").toLowerCase().trim();
  const cat = (category || "").toLowerCase().trim();
  const tid = (id || "").toLowerCase().trim();

  if (iconName && TOOL_ICON_REGISTRY[iconName]) return TOOL_ICON_REGISTRY[iconName];
  if (cat && CATEGORY_ICONS[cat]) return CATEGORY_ICONS[cat];
  if (tid && TOOL_ICON_REGISTRY[tid]) return TOOL_ICON_REGISTRY[tid];

  if (tid) {
    const sortedKeys = Object.keys(TOOL_ICON_REGISTRY).sort((a, b) => b.length - a.length);
    for (const key of sortedKeys) {
      if (tid.includes(key)) return TOOL_ICON_REGISTRY[key];
    }
  }
  if (cat) {
    for (const [key, Icon] of Object.entries(CATEGORY_ICONS)) {
      if (cat.includes(key)) return Icon;
    }
  }
  return null;
}

export function resolveToolIconOrFallback(
  id: string | undefined,
  iconBasename?: string,
  category?: string,
): LucideIconComponent {
  return resolveToolIconComponent(id, iconBasename, category) ?? Wrench;
}

/**
 * Fallback label generator used ONLY when the tool manifest cannot be fetched
 * (e.g., for core tools like `terminate` that are not in the store).
 */
export function resolveToolLabel(toolName: string): string {
  const key = (toolName || "").toLowerCase().trim();
  const LABELS: Record<string, string> = {
    bash: "Shell", sh: "Shell", shell: "Shell", terminal: "Shell",
    python_execute: "Python", python: "Python", python3: "Python", code: "Code", code2: "Code",
    web_search: "Web Search", websearch: "Web Search", search: "Web Search",
    http_fetch: "HTTP Fetch", http: "HTTP Fetch", fetch: "HTTP Fetch", globe: "HTTP Fetch",
    browser_interactive: "Browser", browser: "Browser", monitor: "Browser",
    ask_human: "Ask Human", human_input: "Ask Human", human: "Ask Human", message: "Ask Human",
    str_replace_editor: "Editor", editor: "Editor", file_code: "Editor",
    file_saver: "File Saver", file: "File Saver", file_text: "File Saver",
    source_control: "Source Control", git: "Source Control", git_branch: "Source Control",
    delegate_task: "Delegate", delegate_subtask: "Delegate", delegate: "Delegate",
    mcp: "MCP", puzzle: "MCP", terminate: "Terminate", square: "Terminate",
  };
  if (LABELS[key]) return LABELS[key];
  return key.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) || "Tool";
}

/* ────────────────────────────────────────────────────────────────
 * Manifest Metadata Cache
 * Caches both successful responses and failures (null) to prevent
 * repeated 404 network requests for core tools.
 * ──────────────────────────────────────────────────────────────── */

const toolMetadataCache: Record<string, ToolManifest | null> = {};

export async function fetchToolManifest(toolId: string): Promise<ToolManifest | null> {
  // Cache hit (including null for known failures)
  if (toolId in toolMetadataCache) {
    return toolMetadataCache[toolId];
  }

  try {
    const base = typeof window !== "undefined" && (window.location.port === "3088" || window.location.port === "3000") ? "http://localhost:8088" : "";
    const res = await fetch(`${base}/api/store/tools/${toolId}`);

    if (res.ok) {
      const data = await res.json();
      if (data.tool) {
        toolMetadataCache[toolId] = data.tool;
        return data.tool;
      }
    }

    // If 404 or invalid response, cache null to prevent future requests
    toolMetadataCache[toolId] = null;
    return null;
  } catch (e) {
    // Network error - cache null
    toolMetadataCache[toolId] = null;
    return null;
  }
}
