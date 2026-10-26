// frontend/src/app/stores/_components/AgentIcon.tsx

"use client";

import React, { useState } from "react";
import {
  BarChart3,
  Bot,
  Code2,
  FileCode2,
  Puzzle,
  Search,
  Sparkles,
  User,
} from "lucide-react";
import { getApiUrl } from "../_hooks/useStoreData";

/**
 * Props for {@link AgentIcon}.
 *
 * The component is a pure renderer:
 *   - Size comes from `size` (required, exact pixels).
 *   - Color comes from ``currentColor`` — i.e. from the caller's CSS
 *     ``color`` property. Whatever color the parent's text is, the
 *     icon inherits it automatically via the CSS mask technique.
 *
 * The component never inspects ``core_color``, never overrides an
 * inherited color, never picks a size. All visual decisions belong
 * to the caller.
 */
export interface AgentIconProps {
  /** Agent id — used to build the SVG URL. */
  agentId: string;
  /** Manifest icon hint — used ONLY for the Lucide fallback path. */
  iconName?: string;
  /** Exact pixel size (both width and height). Required. */
  size: number;
  className?: string;
}

type LucideComponent = React.ComponentType<{
  size?: number;
  className?: string;
}>;

/* ────────────────────────────────────────────────────────────────
 * Lucide fallback registry
 * ──────────────────────────────────────────────────────────────── */

const ICON_REGISTRY: Record<string, LucideComponent> = {
  bot: Bot,
  robot: Bot,
  user: User,
  sparkles: Sparkles,
  puzzle: Puzzle,
  code: Code2,
  code2: Code2,
  code_architect: Code2,
  search: Search,
  deep_researcher: Search,
  barchart: BarChart3,
  barchart3: BarChart3,
  data_scientist: BarChart3,
  file: FileCode2,
  file_code: FileCode2,
  editor: FileCode2,
  peldrun: Bot,
  generalist: Bot,
};

function extractIconBasename(raw: string | undefined): string {
  if (!raw || typeof raw !== "string") return "";
  const trimmed = raw.trim();
  if (!trimmed) return "";
  const base = trimmed.split("/").pop() ?? "";
  return base.replace(/\.svg$/i, "").toLowerCase().trim();
}

function resolveLucideIcon(
  iconName: string | undefined,
  size: number
): React.ReactNode {
  const base = extractIconBasename(iconName);
  if (base && ICON_REGISTRY[base]) {
    const Icon = ICON_REGISTRY[base];
    return <Icon size={size} />;
  }
  if (base) {
    const sortedKeys = Object.keys(ICON_REGISTRY).sort(
      (a, b) => b.length - a.length
    );
    for (const key of sortedKeys) {
      if (base.includes(key)) {
        const Icon = ICON_REGISTRY[key];
        return <Icon size={size} />;
      }
    }
  }
  return <Bot size={size} />;
}

/* ────────────────────────────────────────────────────────────────
 * Main component
 * ──────────────────────────────────────────────────────────────── */

export function AgentIcon({
  agentId,
  iconName,
  size,
  className,
}: AgentIconProps) {
  const [failed, setFailed] = useState(false);

  const url = getApiUrl(
    `/api/store/agents/${encodeURIComponent(agentId)}/icon.svg`
  );

  // ── Fallback: Lucide — inherits color via currentColor ──────
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
          // No explicit color — inherits from parent naturally.
        }}
      >
        {resolveLucideIcon(iconName, size)}
      </span>
    );
  }

  // ── Primary: SVG loaded via <img>, tinted via CSS mask ──────
  return (
    <>
      {/* 1. Hidden probe: existence check + HTTP cache warming.
          Not for display. */}
      <img
        src={url}
        alt=""
        style={{ display: "none" }}
        onError={() => setFailed(true)}
        aria-hidden="true"
      />

      {/* 2. Visible icon: mask carves the shape, backgroundColor
          applies `currentColor` (which inherits the parent's CSS
          `color`). No color prop, no inline tint decision — the
          caller's text color IS the icon color. */}
      <span
        className={className}
        aria-hidden="true"
        style={{
          width: size,
          height: size,
          display: "inline-block",
          backgroundColor: "currentColor",
          maskImage: `url("${url}")`,
          maskSize: "contain",
          maskRepeat: "no-repeat",
          maskPosition: "center",
          WebkitMaskImage: `url("${url}")`,
          WebkitMaskSize: "contain",
          WebkitMaskRepeat: "no-repeat",
          WebkitMaskPosition: "center",
        }}
      />
    </>
  );
}
