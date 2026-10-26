// frontend/src/app/stores/_components/ToolIcon.tsx

"use client";

import React, { useEffect, useState } from "react";
import {
  Code2,
  Database,
  FileCode2,
  FileText,
  GitBranch,
  Globe,
  MessageCircleQuestion,
  Monitor,
  Puzzle,
  Search,
  Square,
  Terminal,
  Users,
  Wrench,
} from "lucide-react";
import type { ToolWithMeta } from "./ToolCard";
import { getApiUrl } from "../_hooks/useStoreData";

export interface ToolIconProps {
  tool: ToolWithMeta;
  size?: number;
  className?: string;
}

/** Module-level cache: toolId → sanitized SVG text (or null = failed) */
const svgCache = new Map<string, string | null>();

type LucideComponent = React.ComponentType<{ size?: number; className?: string }>;

/* ────────────────────────────────────────────────────────────────
 * Icon resolution registry (data-driven)
 * ──────────────────────────────────────────────────────────────── */

/**
 * Manifest-driven icon registry.
 *
 * Keys are lowercase identifiers. Lookup happens against three sources:
 *   1. ``tool.icon`` — basename of a filename like "terminal.svg" → "terminal"
 *   2. ``tool.id`` — the exact tool identifier
 *   3. substrings of ``tool.id``
 *
 * Adding a new built-in tool with an icon name that is already a key
 * here requires **zero frontend changes**. Adding a new icon requires
 * a single line below.
 */
const ICON_REGISTRY: Record<string, LucideComponent> = {
  // ── Shell / Terminal ────────────────────────────────────────
  bash: Terminal,
  sh: Terminal,
  shell: Terminal,
  terminal: Terminal,

  // ── Python / Code execution ─────────────────────────────────
  python: Code2,
  python_execute: Code2,
  python3: Code2,
  code: Code2,
  code2: Code2,
  execute: Code2,

  // ── File editor ─────────────────────────────────────────────
  editor: FileCode2,
  str_replace_editor: FileCode2,
  file_code: FileCode2,
  file_code2: FileCode2,
  edit: FileCode2,

  // ── File writer / saver ─────────────────────────────────────
  file: FileText,
  file_saver: FileText,
  file_text: FileText,
  file_write: FileText,
  save: FileText,

  // ── Web search ──────────────────────────────────────────────
  search: Search,
  web_search: Search,
  websearch: Search,

  // ── HTTP / Network ──────────────────────────────────────────
  globe: Globe,
  http: Globe,
  http_fetch: Globe,
  fetch: Globe,
  request: Globe,
  network: Globe,

  // ── Browser automation ──────────────────────────────────────
  browser: Monitor,
  browser_interactive: Monitor,
  monitor: Monitor,
  playwright: Monitor,

  // ── Human interaction ───────────────────────────────────────
  ask_human: MessageCircleQuestion,
  human: MessageCircleQuestion,
  human_input: MessageCircleQuestion,
  message: MessageCircleQuestion,
  question: MessageCircleQuestion,

  // ── Source control ──────────────────────────────────────────
  git: GitBranch,
  git_branch: GitBranch,
  source_control: GitBranch,
  version_control: GitBranch,

  // ── Delegation ──────────────────────────────────────────────
  delegate: Users,
  delegate_task: Users,
  delegate_subtask: Users,
  users: Users,
  handover: Users,
  team: Users,

  // ── MCP ─────────────────────────────────────────────────────
  mcp: Puzzle,
  puzzle: Puzzle,
  plugin: Puzzle,

  // ── Database ────────────────────────────────────────────────
  database: Database,
  db: Database,
  sql: Database,

  // ── Lifecycle ───────────────────────────────────────────────
  terminate: Square,
  square: Square,
  done: Square,
  stop: Square,
};

/**
 * Category-driven icon fallback.
 *
 * When a tool's ``icon`` and ``id`` do not match an entry in
 * ``ICON_REGISTRY``, we fall back to its ``category``. This makes the
 * system self-extending: a new tool with ``"category": "web"`` gets a
 * Globe icon without any registry edit.
 */
const CATEGORY_ICONS: Record<string, LucideComponent> = {
  // Web family
  web: Globe,
  network: Globe,
  http: Globe,
  search: Search,

  // Code / execution family
  code: Code2,
  execution: Code2,
  development: Code2,
  python: Code2,

  // File family
  file: FileText,
  files: FileText,
  filesystem: FileText,
  editor: FileCode2,

  // System family
  system: Terminal,
  shell: Terminal,
  os: Terminal,

  // Browser family
  browser: Monitor,
  automation: Monitor,

  // Interaction family
  interaction: MessageCircleQuestion,
  human: MessageCircleQuestion,

  // Version control
  vcs: GitBranch,
  scm: GitBranch,

  // Delegation
  delegation: Users,
  agent: Users,

  // MCP
  mcp: Puzzle,
  protocol: Puzzle,

  // Data
  data: Database,
  analytics: Database,
};

/* ────────────────────────────────────────────────────────────────
 * Resolution pipeline
 * ──────────────────────────────────────────────────────────────── */

/**
 * Extract the bare basename of a manifest ``icon`` field.
 *
 * Handles three shapes uniformly:
 *   "bash.svg"             → "bash"
 *   "icons/bash.svg"       → "bash"
 *   "Terminal"             → "terminal"
 *   "  Code2  "            → "code2"
 */
function extractIconBasename(raw: string | undefined): string {
  if (!raw || typeof raw !== "string") return "";
  const trimmed = raw.trim();
  if (!trimmed) return "";
  const base = trimmed.split("/").pop() ?? "";
  return base.replace(/\.svg$/i, "").toLowerCase().trim();
}

/**
 * Resolve a Lucide icon for a tool using a manifest-driven cascade.
 *
 * Resolution order (first match wins):
 *   1. ``tool.icon`` basename → ICON_REGISTRY
 *   2. ``tool.category`` exact → CATEGORY_ICONS
 *   3. ``tool.id`` exact → ICON_REGISTRY
 *   4. ``tool.id`` substring → ICON_REGISTRY (longest key first)
 *   5. ``tool.category`` substring → CATEGORY_ICONS
 *   6. Generic Wrench
 *
 * Every step is data-driven. Adding a tool with a known icon name or
 * a known category requires no code changes here.
 */
function resolveLucideIcon(tool: ToolWithMeta, size: number): React.ReactNode {
  // 1. Manifest-declared icon basename
  const iconName = extractIconBasename(tool.icon);
  if (iconName && ICON_REGISTRY[iconName]) {
    const Icon = ICON_REGISTRY[iconName];
    return <Icon size={size} />;
  }

  // 2. Category exact match
  const category = (tool.category || "").toLowerCase().trim();
  if (category && CATEGORY_ICONS[category]) {
    const Icon = CATEGORY_ICONS[category];
    return <Icon size={size} />;
  }

  // 3. Tool ID exact match
  const id = (tool.id || "").toLowerCase().trim();
  if (id && ICON_REGISTRY[id]) {
    const Icon = ICON_REGISTRY[id];
    return <Icon size={size} />;
  }

  // 4. Tool ID substring match — longest keys first to avoid
  //    "http" matching before "http_fetch" would, ensuring the more
  //    specific icon wins.
  if (id) {
    const sortedKeys = Object.keys(ICON_REGISTRY).sort(
      (a, b) => b.length - a.length
    );
    for (const key of sortedKeys) {
      if (id.includes(key)) {
        const Icon = ICON_REGISTRY[key];
        return <Icon size={size} />;
      }
    }
  }

  // 5. Category substring match (last-chance category fallback)
  if (category) {
    for (const [key, Icon] of Object.entries(CATEGORY_ICONS)) {
      if (category.includes(key)) {
        return <Icon size={size} />;
      }
    }
  }

  // 6. Universal fallback
  return <Wrench size={size} />;
}

/* ────────────────────────────────────────────────────────────────
 * SVG sanitization (unchanged)
 * ──────────────────────────────────────────────────────────────── */

/** Minimal hardening: kill <script>, event handlers, and javascript: URLs. */
function sanitizeSvg(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed.includes("<svg")) return null;
  return trimmed
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/\son\w+\s*=\s*(".*?"|'.*?'|[^\s>]+)/gi, "")
    .replace(/(href|src)\s*=\s*("javascript:[^"]*"|'javascript:[^']*')/gi, "");
}

/* ────────────────────────────────────────────────────────────────
 * Main component (unchanged structure)
 * ──────────────────────────────────────────────────────────────── */

export function ToolIcon({ tool, size = 18, className }: ToolIconProps) {
  const [svgText, setSvgText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    setSvgText(null);

    // Cache hit
    if (svgCache.has(tool.id)) {
      const cached = svgCache.get(tool.id) ?? null;
      if (cached === null) setFailed(true);
      else setSvgText(cached);
      return;
    }

    const url = getApiUrl(
      `/api/store/tools/${encodeURIComponent(tool.id)}/icon.svg`
    );
    const ctrl = new AbortController();

    fetch(url, { signal: ctrl.signal, credentials: "same-origin" })
      .then((r) =>
        r.ok ? r.text() : Promise.reject(new Error(String(r.status)))
      )
      .then((text) => {
        const safe = sanitizeSvg(text);
        svgCache.set(tool.id, safe);
        if (cancelled) return;
        if (safe === null) setFailed(true);
        else setSvgText(safe);
      })
      .catch((e) => {
        if (cancelled || e?.name === "AbortError") return;
        svgCache.set(tool.id, null);
        setFailed(true);
      });

    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [tool.id]);

  // ── Fallback: Lucide ─────────────────────────────────────────
  if (failed) {
    return (
      <span
        className={className}
        aria-hidden="true"
        style={{
          width: size,
          height: size,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {resolveLucideIcon(tool, size)}
      </span>
    );
  }

  // ── Loading placeholder (same box size → no layout shift) ───
  if (!svgText) {
    return (
      <span
        className={className}
        aria-hidden="true"
        style={{ width: size, height: size, display: "inline-block" }}
      />
    );
  }

  // ── Inline SVG ───────────────────────────────────────────────
  return (
    <span
      className={className}
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        color: "currentColor", // resolves fill="currentColor" inside the SVG
      }}
      dangerouslySetInnerHTML={{ __html: svgText }}
    />
  );
}
