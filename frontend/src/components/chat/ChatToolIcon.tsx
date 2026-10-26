// frontend/src/components/chat/ChatToolIcon.tsx

"use client";

import React from "react";
import { Server } from "lucide-react";
import { ToolIcon } from "@/app/stores/_components/ToolIcon";
import type { ToolWithMeta } from "@/app/stores/_components/ToolCard";

/**
 * Thin wrapper around the Store's ``ToolIcon`` component, adapted for
 * the Chat surface.
 *
 * The Store's ``ToolIcon`` already does everything we need: it fetches
 * the SVG from ``/api/store/tools/{id}/icon.svg``, sanitizes it, caches
 * it, and falls back to Lucide when the file is missing. The only
 * reason we do not use it directly in chat is that it expects the full
 * ``ToolWithMeta`` shape (which chat does not carry) — this wrapper
 * builds a minimal object that satisfies the type contract without
 * requiring the caller to know about it.
 *
 * MCP tools are handled separately: they have no per-tool SVG file on
 * disk, so we short-circuit to the Server icon instead of letting the
 * fetch fail (which would spam the network and the console).
 */
export interface ChatToolIconProps {
  toolId: string;
  /** Manifest-declared icon filename, if available. */
  iconHint?: string;
  /** Category for icon-resolution fallback. */
  category?: string;
  /** ``true`` when this is an MCP-protocol tool (not a builtin). */
  isMcp?: boolean;
  size?: number;
  className?: string;
}

export function ChatToolIcon({
  toolId,
  iconHint,
  category,
  isMcp = false,
  size = 14,
  className,
}: ChatToolIconProps) {
  // MCP tools do not have a per-tool SVG on the backend; use a stable
  // Lucide icon and skip the fetch entirely.
  if (isMcp) {
    return (
      <Server
        size={size}
        className={className ?? "text-primary shrink-0"}
        aria-hidden="true"
      />
    );
  }

  // Builtin tool — delegate to the Store's ToolIcon so the same SVG
  // (or Lucide fallback) is used here as on the Store page.
  const meta = {
    id: toolId,
    name: toolId,
    description: "",
    category: category || "",
    icon: iconHint,
    is_builtin: false,
  } as ToolWithMeta;

  return <ToolIcon tool={meta} size={size} className={className} />;
}

export default ChatToolIcon;
