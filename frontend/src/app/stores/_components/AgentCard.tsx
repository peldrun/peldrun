// frontend/src/app/stores/_components/AgentCard.tsx

"use client";

import React from "react";
import {
  ArrowRight,
  BarChart3,
  Bot,
  Code2,
  Edit2,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { AgentManifest } from "@/lib/types";

/**
 * Props for {@link AgentCard}.
 * All actions are injected so the card stays purely presentational.
 */
export interface AgentCardProps {
  agent: AgentManifest;
  /** True while a toggle request for this specific agent is in flight. */
  isToggling: boolean;
  onToggle: (agentId: string) => void;
  onEdit: (agent: AgentManifest) => void;
  onDelete: (agentId: string) => void;
  onStartChat: (agent: AgentManifest) => void;
}

/**
 * Map a manifest icon identifier to its Lucide representation.
 * Falls back to a generic bot icon for unknown values so a missing
 * icon never renders a blank slot.
 */
function renderAgentIcon(iconName?: string) {
  switch ((iconName || "").toLowerCase()) {
    case "code2":
      return <Code2 className="h-5 w-5 text-cyan-600 dark:text-cyan-400" />;
    case "search":
      return <Search className="h-5 w-5 text-amber-600 dark:text-amber-400" />;
    case "barchart3":
      return <BarChart3 className="h-5 w-5 text-purple-600 dark:text-purple-400" />;
    case "sparkles":
      return <Sparkles className="h-5 w-5 text-primary" />;
    default:
      return <Bot className="h-5 w-5 text-primary" />;
  }
}

/**
 * Card representing a single agent in the Agents Hub.
 *
 * Visual states:
 *  - enabled  → full colour, "Start Chat" enabled
 *  - disabled → muted background, "Start Chat" disabled
 *  - protected (`peldrun`) → no toggle switch, cannot be disabled
 */
export function AgentCard({
  agent,
  isToggling,
  onToggle,
  onEdit,
  onDelete,
  onStartChat,
}: AgentCardProps) {
  const isDisabled = agent.status === "disabled";
  const isProtected = agent.id === "peldrun";

  return (
    <div
      className={`group relative flex flex-col justify-between p-5 rounded-sm border transition-all duration-200 shadow-sm hover:shadow-md ${
        isDisabled
          ? "bg-muted/30 border-border/60 opacity-80"
          : "bg-card border-transparent hover:border-muted/40 shadow-md"
      }`}
    >
      <div>
        {/* Card header & status toggle */}
        <div className="flex items-start justify-between mb-3.5">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-sm bg-muted border border-border shadow-xs group-hover:scale-105 transition-transform">
              {renderAgentIcon(agent.icon)}
            </div>
            <div>
              <h3 className="text-sm font-bold text-foreground group-hover:text-primary transition-colors flex items-center gap-2">
                {agent.name}
              </h3>
              <span className="text-[11px] font-mono text-primary">
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

        {/* Origin + status badges */}
        <div className="flex items-center gap-2 mb-3">
          {agent.is_builtin ? (
            <span className="px-2 py-0.5 rounded-md text-[10px] font-mono uppercase font-semibold bg-sky-500/10 border border-sky-500/30 text-sky-700 dark:text-sky-300">
              BUILT-IN
            </span>
          ) : (
            <span className="px-2 py-0.5 rounded-md text-[10px] font-mono uppercase font-semibold bg-primary/10 border border-primary/30 text-primary">
              CUSTOM
            </span>
          )}
          <span
            className={`px-2 py-0.5 rounded-md text-[10px] font-mono uppercase font-semibold ${
              !isDisabled
                ? "bg-emerald-500/10 border border-emerald-500/30 text-emerald-600 dark:text-emerald-400"
                : "bg-muted text-muted-foreground border border-border"
            }`}
          >
            {!isDisabled ? "ACTIVE" : "DISABLED"}
          </span>
        </div>

        {/* Description */}
        <p className="text-xs text-muted-foreground line-clamp-2 mb-4 leading-relaxed">
          {agent.description || "Custom configured autonomous specialist."}
        </p>

        {/* Scoped tools */}
        <div className="mb-4">
          <span className="text-[10px] uppercase font-semibold text-muted-foreground tracking-wider block mb-1.5">
            Scoped Tools ({agent.tools?.length || 0})
          </span>
          <div className="flex flex-wrap gap-1.5 max-h-16 overflow-y-auto scrollbar-none">
            {agent.tools && agent.tools.length > 0 ? (
              agent.tools.map((t) => (
                <span
                  key={t}
                  className="px-2 py-0.5 rounded-md bg-muted text-[10px] text-muted-foreground font-mono border border-border"
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

      {/* Footer actions */}
      <div className="pt-3.5 border-t border-border flex items-center justify-between">
        <div className="flex items-center gap-1">
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
