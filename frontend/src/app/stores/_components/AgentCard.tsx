// frontend/src/app/stores/_components/AgentCard.tsx

"use client";

import React from "react";
import {
  ArrowRight,
  Edit2,
  Info,
  Server,
  Settings,
  Trash2,
  Wrench,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { AgentManifest } from "@/lib/types";
import { AgentIcon } from "./AgentIcon";

/* ────────────────────────────────────────────────────────────────
 * Props
 * ──────────────────────────────────────────────────────────────── */

export interface AgentCardProps {
  agent: AgentManifest;
  /** True while a toggle request for this specific agent is in flight. */
  isToggling: boolean;
  onToggle: (agentId: string) => void;
  onEdit: (agent: AgentManifest) => void;
  onDelete: (agentId: string) => void;
  onStartChat: (agent: AgentManifest) => void;
  onViewDetails?: (agent: AgentManifest) => void;
  onOpenSettings?: (agent: AgentManifest) => void;
  onOpenTools?: (agent: AgentManifest) => void;
  onOpenMcp?: (agent: AgentManifest) => void;
}

/* ────────────────────────────────────────────────────────────────
 * Visual constants — the single source of truth for the card layout
 * ──────────────────────────────────────────────────────────────── */

/**
 * Icon slot dimensions. The slot is the outer square that reserves
 * space (prevents layout shift while the SVG loads); the icon itself
 * is drawn at ICON_SIZE inside it.
 *
 * Change these to resize every agent icon in the app — no other file
 * needs editing.
 */
const ICON_SLOT_SIZE = 66; // px — outer box
const ICON_SIZE = 66; // px — actual icon rendering size

/* ────────────────────────────────────────────────────────────────
 * Manifest helpers
 * ──────────────────────────────────────────────────────────────── */

interface AgentExtraFields {
  core_color?: string;
  settings?: {
    tools_editable?: boolean;
    schema?: {
      properties?: Record<string, unknown>;
    };
  };
}

function hasConfigurableSettings(agent: AgentManifest): boolean {
  const meta = agent as AgentManifest & AgentExtraFields;
  const schema = meta.settings?.schema;
  return !!(
    schema?.properties &&
    Object.keys(schema.properties).length > 0
  );
}

function hasEditableTools(agent: AgentManifest): boolean {
  const meta = agent as AgentManifest & AgentExtraFields;
  return !!meta.settings?.tools_editable;
}

function getCoreColor(agent: AgentManifest): string | undefined {
  const meta = agent as AgentManifest & AgentExtraFields;
  const c = meta.core_color;
  return typeof c === "string" && c.trim() ? c.trim() : undefined;
}

/* ────────────────────────────────────────────────────────────────
 * Component
 * ──────────────────────────────────────────────────────────────── */

/**
 * Card representing a single agent in the Agents Hub.
 *
 * Icon rendering contract:
 *   - ``core_color`` (from the manifest) is used ONLY for the subtle
 *     background wash behind the card content. It is never applied to
 *     the icon itself.
 *   - The icon inherits the card's text color via ``currentColor``
 *     (see ``text-foreground`` on the icon slot wrapper). Changing the
 *     surrounding text color changes the icon color automatically —
 *     no per-icon overrides anywhere.
 *   - Icon size is controlled by ``ICON_SIZE`` and ``ICON_SLOT_SIZE``
 *     above. AgentIcon is a pure renderer with no visual decisions of
 *     its own.
 */
export function AgentCard({
  agent,
  isToggling,
  onToggle,
  onEdit,
  onDelete,
  onStartChat,
  onViewDetails,
  onOpenSettings,
  onOpenTools,
  onOpenMcp,
}: AgentCardProps) {
  const isDisabled = agent.status === "disabled";
  const isProtected = agent.id === "peldrun";
  const coreColor = getCoreColor(agent);
  const showSettings = hasConfigurableSettings(agent) && !!onOpenSettings;
  const showTools = hasEditableTools(agent) && !!onOpenTools;
  const showMcp = !!onOpenMcp;
  // Read the EFFECTIVE MCP server list — this is what the runtime
  // will actually bind to the agent after applying the user's
  // ``disabled_mcp_servers`` / ``extra_mcp_servers`` overrides from
  // ``agent_settings.json``.
  //
  // Fallback to the raw ``mcp_servers`` field for defence in depth:
  // a backend that has not yet emitted ``effective_mcp_servers``
  // would otherwise make the badge always show 0 even when the
  // manifest declares servers. This fallback also keeps pre-Phase-8
  // agents (custom, older backend, cached client payloads) correct.
  const effectiveMcpServers = Array.isArray(agent.effective_mcp_servers)
    ? agent.effective_mcp_servers
    : Array.isArray(agent.mcp_servers)
    ? agent.mcp_servers
    : [];
  const mcpServersCount = effectiveMcpServers.length;

  return (
    <div
      className={`group relative text-black hover:text-[#fff] flex flex-col justify-between p-5 rounded-sm
        border transition-all duration-200 shadow-md hover:shadow-sm overflow-hidden ${
        isDisabled
          ? "bg-muted/30 border-border/60 opacity-80"
          : "bg-card border-card hover:border-card/50"
      }`}
    >
      {/* ── core_color wash (decorative background) ────────────── */}
      {!isDisabled && coreColor && (
        <div
          className="absolute inset-0 pointer-events-none opacity-[0.08] group-hover:opacity-[1.0] transition-opacity"
          style={{ backgroundColor: coreColor}}
          aria-hidden="true"
        />
      )}

      <div className="relative">
        {/* ── Header row: icon + name + toggle ─────────────────── */}
        <div className="flex items-start justify-between mb-3.5">
          <div className="flex items-center gap-3 min-w-0  text-inherit ">
            {/* Icon slot — controls size AND text color.
                `text-foreground` is what the icon inherits via
                currentColor. To tint the icon differently per-theme
                (or per-state), change this class — no other file
                needs editing. */}
            <div
              className="shrink-0 flex items-center justify-center text-inherit group-hover:scale-105 transition-transform"
              style={{
                width: ICON_SLOT_SIZE,
                height: ICON_SLOT_SIZE,
              }}
            >
              <AgentIcon
                agentId={agent.id}
                iconName={agent.icon}
                size={ICON_SIZE}
              />
            </div>

            <div className="min-w-0">
              <h3 className="text-md font-extrabold uppercase text-inherit group-hover:text-inherit transition-colors truncate">
                {agent.name}
              </h3>
              <span className="text-[11px] font-mono text-inherit truncate block">
                {agent.role || "Autonomous Agent"}
              </span>
            </div>
          </div>

          {!isProtected && (
            <button
              type="button"
              onClick={() => onToggle(agent.id)}
              disabled={isToggling}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none disabled:opacity-60 ${
                !isDisabled ? "bg-cgood" : "bg-[#777171]"
              }`}
              title={!isDisabled ? "Disable agent" : "Enable agent"}
              aria-label={!isDisabled ? "Disable agent" : "Enable agent"}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-background shadow-md ring-0 transition duration-200 ease-in-out ${
                  !isDisabled ? "translate-x-4" : "translate-x-0"
                }`}
              />
            </button>
          )}
        </div>

        {/* ── Badges ───────────────────────────────────────────── */}
        <div className="flex items-center gap-2 mb-3">
          {agent.is_builtin ? (
            <span className="px-2 py-0.5 rounded-sm text-[10px] font-mono uppercase font-semibold bg-card border border-sky-500/30 text-sky-700 dark:text-sky-300">
              BUILT-IN
            </span>
          ) : (
            <span className="px-2 py-0.5 rounded-sm text-[10px] font-mono uppercase font-semibold bg-card border border-primary/30 text-primary">
              CUSTOM
            </span>
          )}
          <span
            className={`px-2 py-0.5 rounded-sm text-[10px] font-mono uppercase font-semibold ${
              !isDisabled
                ? "bg-card border border-emerald-500/30 text-emerald-600 dark:text-emerald-400"
                : "bg-muted text-muted-foreground border border-border"
            }`}
          >
            {!isDisabled ? "ACTIVE" : "DISABLED"}
          </span>
        </div>

        {/* ── Description ──────────────────────────────────────── */}
        <p className="text-xs text-inherit line-clamp-2 mb-4 leading-relaxed">
          {agent.description || "Custom configured autonomous specialist."}
        </p>

        {/* ── Scoped tools ─────────────────────────────────────── */}
        <div className="mb-4">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[10px] uppercase font-semibold text-inherit tracking-wider block">
              Scoped Tools ({agent.tools?.length || 0})
            </span>
            {mcpServersCount > 0 && (
              <span className="text-[10px] font-mono text-primary flex items-center gap-1 font-semibold">
                <Server size={10} />
                {mcpServersCount} MCP
              </span>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5 max-h-16 overflow-y-auto scrollbar-none">
            {agent.tools && agent.tools.length > 0 ? (
              agent.tools.map((t) => (
                <span
                  key={t}
                  className="px-2 py-0.5 rounded-sm bg-card text-[10px] text-muted-foreground font-mono shadow-sm"
                >
                  {t}
                </span>
              ))
            ) : (
              <span className="text-[11px] text-muted-foreground italic">
                No tools bound
              </span>
            )}
          </div>
        </div>
      </div>

      {/* ── Footer actions ─────────────────────────────────────── */}
      <div className="relative pt-3.5 border-t border-border flex items-center justify-between">
        <div className="flex items-center gap-1">
          {onViewDetails && (
            <button
              type="button"
              onClick={() => onViewDetails(agent)}
              className="p-1.5 rounded-lg hover:bg-muted text-inherit hover:text-primary transition cursor-pointer"
              title="View details"
              aria-label="View agent details"
            >
              <Info size={13} />
            </button>
          )}

          {showTools && (
            <button
              type="button"
              onClick={() => onOpenTools!(agent)}
              className="p-1.5 rounded-lg hover:bg-muted text-inherit hover:text-primary transition cursor-pointer"
              title="Configure agent tools"
              aria-label="Configure agent tools"
            >
              <Wrench size={13} />
            </button>
          )}

          {showMcp && (
            <button
              type="button"
              onClick={() => onOpenMcp!(agent)}
              className="p-1.5 rounded-lg hover:bg-muted text-inherit hover:text-primary transition cursor-pointer"
              title="Configure agent MCP servers"
              aria-label="Configure agent MCP servers"
            >
              <Server size={13} />
            </button>
          )}

          {showSettings && (
            <button
              type="button"
              onClick={() => onOpenSettings!(agent)}
              className="p-1.5 rounded-lg hover:bg-muted text-inherit hover:text-primary transition cursor-pointer"
              title="Configure agent settings"
              aria-label="Configure agent settings"
            >
              <Settings size={13} />
            </button>
          )}

          {!agent.is_builtin && (
            <>
              <button
                type="button"
                onClick={() => onEdit(agent)}
                className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
                title="Edit Agent"
                aria-label="Edit Agent"
              >
                <Edit2 size={13} />
              </button>
              <button
                type="button"
                onClick={() => onDelete(agent.id)}
                className="p-1.5 rounded-lg hover:bg-destructive/10 text-muted-foreground hover:text-destructive transition cursor-pointer"
                title="Delete Agent"
                aria-label="Delete Agent"
              >
                <Trash2 size={13} />
              </button>
            </>
          )}
        </div>

        <Button
          size="sm"
          disabled={isDisabled}
          onClick={() => onStartChat(agent)}
          className="bg-primary hover:bg-rb/90 hover:text-primary-foreground text-primary text-xs px-3.5 h-8 font-medium shadow-xs transition group/btn cursor-pointer disabled:opacity-40"
        >
          <span>Start Chat</span>
          <ArrowRight
            size={12}
            className="ml-1 group-hover/btn:translate-x-0.5 transition-transform"
          />
        </Button>
      </div>
    </div>
  );
}
