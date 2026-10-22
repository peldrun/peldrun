// frontend/src/app/stores/_components/ToolCard.tsx

"use client";

import React from "react";
import {
  Eye,
  Info,
  Lock,
  Tag,
  Trash2,
  WifiOff,
} from "lucide-react";
import type { ToolDefinition } from "@/lib/types";
import { ToolIcon } from "./ToolIcon";

/**
 * Full manifest surface accepted by the tool UI.
 *
 * `ToolDefinition` covers only the fields the store page needs for the
 * basic grid, but real manifests sent by the backend carry much richer
 * metadata (author, license, links, runtime, examples, ...). Those
 * fields are declared here once so every consumer (card, modal) can
 * access them with full type safety instead of `(tool as any)`.
 *
 * Every field is optional because sparse payloads (e.g. custom tools
 * uploaded by a user) may omit most of them.
 */
export type ToolWithMeta = ToolDefinition & {
  // ── Identity ─────────────────────────────────────────────────
  display_name?: string;
  subcategory?: string;
  icon?: string;

  // ── Localised copy ───────────────────────────────────────────
  short_description?: string;
  store_description?: string;
  usage_notes?: string;
  supported_languages?: string[];
  i18n?: Record<
    string,
    {
      display_name?: string;
      short_description?: string;
      store_description?: string;
    }
  >;

  // ── Author & Publisher ───────────────────────────────────────
  author?: {
    name?: string;
    url?: string;
    email?: string;
  };
  publisher_id?: string;
  trust?: "verified" | "community" | "unverified" | string;
  signature?: string;

  // ── Version & License ────────────────────────────────────────
  version?: string;
  release_date?: string;
  stability?: string;
  min_app_version?: string;
  changelog_url?: string;
  license?: string;
  license_url?: string;
  pricing?: string;

  // ── Links & Media ────────────────────────────────────────────
  homepage?: string;
  docs_url?: string;
  demo_url?: string | null;
  screenshots?: string[];

  // ── Classification ───────────────────────────────────────────
  tags?: string[];
  capabilities?: string[];

  // ── Security & Permissions ───────────────────────────────────
  permissions?: string[];
  network_access?: boolean;
  requires_api_key?: boolean;
  required_env_vars?: string[];
  sandbox?: boolean;
  filesystem_access?: string;
  read_only?: boolean;
  rate_limit?: {
    requests?: number;
    window_seconds?: number;
  };

  // ── Runtime ──────────────────────────────────────────────────
  timeout_seconds?: number;
  concurrency?: number;
  runtime?: string;
  entry_point?: string;
  dependencies?: string[];

  // ── Configuration ────────────────────────────────────────────
  settings?: {
    enabled?: boolean;
    schema?: unknown;
    defaults?: unknown;
    required?: string[];
  };
  parameters?: unknown;

  // ── Examples ─────────────────────────────────────────────────
  examples?: Array<{
    input?: unknown;
    output_preview?: string;
  }>;

  // ── Lifecycle ────────────────────────────────────────────────
  deprecated?: boolean;
  deprecation_message?: string | null;
  replaces?: string[];
  replaced_by?: string | null;
};

export interface ToolCardProps {
  tool: ToolDefinition;
  /** True while a toggle request for this specific tool is in flight. */
  isToggling: boolean;
  onToggle: (toolId: string) => void;
  onDelete: (toolId: string) => void;
  /** Opens the details modal showing the full JSON manifest. */
  onViewDetails: (tool: ToolWithMeta) => void;
}

/**
 * Small inline chip used for the meta footer (safety, sandbox, ...).
 * Renders only when the caller has already verified the value exists.
 */
function MetaChip({
  icon: Icon,
  label,
}: {
  icon: React.ComponentType<{ size?: number; className?: string }>;
  label: string;
}) {
  return (
    <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
      <Icon size={11} className="opacity-70 shrink-0" />
      <span className="capitalize">{label}</span>
    </span>
  );
}

/**
 * Single-row representation of a tool inside the Sovereign Tools list.
 *
 * Layout:
 *  ┌───────────────────────────────────────────────────────────────┐
 *  │ [icon]  name  [category] [origin] [version] [disabled] ⓘ ● 🗑 │
 *  │         description (line-clamped)                           │
 *  │         [tag] [tag] [tag] ...                                │
 *  │         ⛨ safety · ⬚ sandbox · ⌁ no network · 👁 read only  │
 *  └───────────────────────────────────────────────────────────────┘
 *
 * The component never assumes a field exists — each block is guarded
 * by an explicit presence check so sparse payloads still look clean.
 * The info button opens the full details modal handled by the parent.
 */
export function ToolCard({
  tool,
  isToggling,
  onToggle,
  onDelete,
  onViewDetails,
}: ToolCardProps) {
  const meta = tool as ToolWithMeta;
  const isEnabled = tool.is_enabled !== false && tool.status !== "disabled";
  const isBuiltin = !!tool.is_builtin;

  // Prefer the human-friendly display_name when available; fall back to
  // the raw manifest `name` otherwise. Used for both the visible label
  // and the hover tooltip so they always agree.
  const displayLabel = meta.display_name?.trim() || tool.name;

  const tags = Array.isArray(meta.tags) ? meta.tags : [];
  const hasTags = tags.length > 0;

  const hasMetaRow =
    !!tool.safety_level ||
    meta.sandbox === true ||
    meta.network_access === false ||
    meta.read_only === true;

  return (
    <div
      className={`flex items-start gap-4 p-4 rounded-sm border transition-all shadow-md ${
        !isEnabled
          ? "bg-muted/20 border-border/60 opacity-80"
          : "bg-card border-card hover:border-primary/1 hover:shadow-sm"
      }`}
    >
      {/* Leading icon */}
      <div className="shrink-0 p-0 flex items-center justify-center m-auto w-[50px] text-foreground">
        <ToolIcon tool={meta} size={45} />
      </div>

      {/* Main content */}
      <div className="flex-1 min-w-0">
        {/* Header row */}
        <div className="flex items-start justify-between gap-3 mb-1.5">
          <div className="flex items-center gap-2 flex-wrap min-w-0">
            <h3
              className="text-sm font-bold text-foreground font-mono truncate"
              title={displayLabel}
            >
              {displayLabel}
            </h3>

            {tool.category && (
              <span className="px-1.5 py-0.5 rounded-sm text-[10px] font-mono uppercase bg-rby/20 text-black border border-rby">
                {tool.category}
              </span>
            )}

            {isBuiltin ? (
              <span className="px-1.5 py-0.5 text-[10px] font-mono uppercase bg-sky-500/10 text-sky-700 dark:text-sky-300 border border-sky-500/20">
                BUILT-IN
              </span>
            ) : (
              <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-primary/10 text-primary border border-primary/20">
                CUSTOM
              </span>
            )}

            {meta.version && (
              <span className="px-1.5 py-0.5 rounded text-[10px] font-mono text-muted-foreground border border-border bg-muted/50">
                v{meta.version}
              </span>
            )}

            {!isEnabled && (
              <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-muted text-muted-foreground border border-border">
                DISABLED
              </span>
            )}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={() => onViewDetails(meta)}
              className="p-1.5 rounded text-muted-foreground hover:text-primary hover:bg-primary/10 transition cursor-pointer"
              title="View details"
              aria-label="View tool details"
            >
              <Info size={14} />
            </button>

            <button
              type="button"
              onClick={() => onToggle(tool.id)}
              disabled={isToggling}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none disabled:opacity-60 ${
                isEnabled ? "bg-cgood" : "bg-[#777171]"
              }`}
              title={isEnabled ? "Disable tool" : "Enable tool"}
              aria-label={isEnabled ? "Disable tool" : "Enable tool"}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-background shadow-md ring-0 transition duration-200 ease-in-out ${
                  isEnabled ? "translate-x-4" : "translate-x-0"
                }`}
              />
            </button>

            {!isBuiltin && (
              <button
                type="button"
                onClick={() => onDelete(tool.id)}
                className="p-1.5 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition cursor-pointer"
                title="Delete custom tool"
                aria-label="Delete custom tool"
              >
                <Trash2 size={14} />
              </button>
            )}
          </div>
        </div>

        {/* Description */}
        {tool.description && (
          <p className="text-xs text-muted-foreground leading-relaxed mb-2 line-clamp-2">
            {tool.description}
          </p>
        )}

        {/* Tags */}
        {hasTags && (
          <div className="flex flex-wrap gap-1.5 mb-2">
            {tags.slice(0, 8).map((tag, idx) => (
              <span
                key={`${tag}-${idx}`}
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono text-muted-foreground bg-muted border border-border"
              >
                <Tag size={9} className="opacity-60" />
                {tag}
              </span>
            ))}
            {tags.length > 8 && (
              <span className="text-[10px] text-muted-foreground self-center">
                +{tags.length - 8}
              </span>
            )}
          </div>
        )}

        {/* Meta footer */}
        {hasMetaRow && (
          <div className="flex items-center gap-3 flex-wrap">
            {tool.safety_level && (
              <MetaChip icon={Lock} label={tool.safety_level} />
            )}
            {meta.sandbox === true && (
              <MetaChip icon={Lock} label="sandbox" />
            )}
            {meta.network_access === false && (
              <MetaChip icon={WifiOff} label="no network" />
            )}
            {meta.read_only === true && (
              <MetaChip icon={Eye} label="read only" />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
