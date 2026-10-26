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


export function truncateText(value: unknown, max = 80): string {
  if (typeof value !== "string") return "";
  const s = value.replace(/\s+/g, " ").trim();
  if (s.length <= max) return s;
  return s.slice(0, max - 1) + "…";
}

/**
 * Parses raw JSON string arguments from the backend and returns a
 * human-readable summary instead of the raw `{ "key": "value" }` block.
 */
export function formatToolArgs(toolName: string, rawContent: string): string {
  let args: Record<string, unknown>;
  try {
    args = JSON.parse(rawContent);
  } catch {
    // If it's not valid JSON, just return the raw string
    return rawContent;
  }

  const key = (toolName || "").toLowerCase().trim();

  const pick = (...names: string[]): string => {
    for (const n of names) {
      const v = args[n];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    return "";
  };

  if (key === "bash" || key === "shell" || key === "sh") {
    const cmd = pick("command", "cmd");
    return cmd ? `$ ${truncateText(cmd, 90)}` : "";
  }
  if (key.startsWith("python")) {
    const code = pick("code", "script", "command");
    if (!code) return "";
    return truncateText(code.split(/\r?\n/)[0].trim(), 90);
  }
  if (key.includes("search")) {
    const q = pick("query", "q", "search_query");
    return q ? `"${truncateText(q, 80)}"` : "";
  }
  if (key.includes("http") || key.includes("fetch")) {
    const url = pick("url", "endpoint");
    return url ? truncateText(url, 90) : "";
  }

  // Handles file creation/editing (str_replace_editor)
  if (key.includes("editor")) {
    const cmd = pick("command");
    const path = pick("path", "file", "filename");
    const text = pick("file_text", "text", "content");

    if (cmd && path) return `${cmd} ${truncateText(path, 60)}`;
    if (cmd && text) return `${cmd} "${truncateText(text.split("\n")[0], 60)}"`;
    return cmd || (path ? truncateText(path, 90) : "");
  }

  if (key.includes("file")) {
    const f = pick("path", "file", "filename");
    return f ? truncateText(f, 90) : "";
  }
  if (key.includes("browser")) {
    return pick("action", "operation");
  }
  if (key.includes("human")) {
    const q = pick("prompt", "question", "query");
    return q ? `"${truncateText(q, 80)}"` : "";
  }
  if (key.includes("source_control")) {
    const action = pick("action");
    const repo = pick("repo", "repository", "workspace");
    if (action && repo) return `${action} · ${truncateText(repo, 60)}`;
    return action || (repo ? truncateText(repo, 90) : "");
  }
  if (key.includes("delegate")) {
    const target = pick("target_agent_id", "agent_id");
    const prompt = pick("subtask_prompt", "prompt", "task");
    if (target && prompt) return `${target} · "${truncateText(prompt, 60)}"`;
    return target || (prompt ? `"${truncateText(prompt, 80)}"` : "");
  }

  // Fallback: show the first available string value
  for (const v of Object.values(args)) {
    if (typeof v === "string" && v.trim()) return truncateText(v, 90);
  }
  return rawContent;
}
