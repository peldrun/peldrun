// frontend/src/app/stores/_sections/AgentsSection.tsx

"use client";

import React, { useMemo } from "react";
import { Bot, Plus } from "lucide-react";
import type { AgentManifest } from "@/lib/types";
import { AgentCard } from "../_components/AgentCard";
import type { SectionHeaderConfig } from "../_config/sections";
import type { AgentFilter } from "../_types/store";

/**
 * Props for {@link AgentsSection}.
 * All action handlers are forwarded from the page orchestrator,
 * which owns the corresponding hooks (`useAgentActions`).
 */
export interface AgentsSectionProps {
  agents: AgentManifest[];
  searchQuery: string;
  agentFilter: AgentFilter;
  /** Currently toggling agent id (or null). Used to disable per-card toggle. */
  togglingAgentId: string | null;
  onToggleAgent: (agentId: string) => void;
  onEditAgent: (agent: AgentManifest) => void;
  onDeleteAgent: (agentId: string) => void;
  onStartChat: (agent: AgentManifest) => void;
}

/**
 * Build the section-specific header configuration for the Agents Hub.
 *
 * Kept as a standalone factory so the page can compose the header
 * without rendering the section content (also easier to unit-test).
 */
export function getAgentsHeaderConfig(ctx: {
  onNewAgent: () => void;
}): SectionHeaderConfig {
  return {
    title: "Agents Hub",
    subtitle: "Autonomous personas and specialist agents",
    badge: "AGENTS",
    actions: [
      {
        id: "new-agent",
        label: "New Agent",
        icon: Plus,
        variant: "primary",
        onClick: ctx.onNewAgent,
      },
    ],
  };
}

/**
 * Agents Hub section content.
 *
 * Responsibilities:
 *  - Filter the agents collection by `searchQuery` + `agentFilter`.
 *  - Render a responsive grid of {@link AgentCard}.
 *  - Render a friendly empty state when no agents match.
 *
 * The section is stateless: every mutation is delegated to the
 * handlers supplied by the parent (backed by `useAgentActions`).
 */
export function AgentsSection({
  agents,
  searchQuery,
  agentFilter,
  togglingAgentId,
  onToggleAgent,
  onEditAgent,
  onDeleteAgent,
  onStartChat,
}: AgentsSectionProps) {
  const filteredAgents = useMemo(() => {
    return agents.filter((ag) => {
      const q = searchQuery.toLowerCase().trim();
      const matchesQuery =
        !q ||
        ag.name.toLowerCase().includes(q) ||
        (ag.description || "").toLowerCase().includes(q) ||
        (ag.role || "").toLowerCase().includes(q);

      if (!matchesQuery) return false;
      if (agentFilter === "builtin") return ag.is_builtin;
      if (agentFilter === "custom") return !ag.is_builtin;
      if (agentFilter === "active") return ag.status !== "disabled";
      if (agentFilter === "disabled") return ag.status === "disabled";
      return true;
    });
  }, [agents, searchQuery, agentFilter]);

  if (filteredAgents.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
        <Bot className="h-10 w-10 text-muted-foreground mb-3" />
        <h3 className="text-sm font-semibold text-foreground">
          No agents match your criteria
        </h3>
        <p className="text-xs text-muted-foreground max-w-sm mt-1">
          Try adjusting your search query or create a new custom agent.
        </p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
      {filteredAgents.map((ag) => (
        <AgentCard
          key={ag.id}
          agent={ag}
          isToggling={togglingAgentId === ag.id}
          onToggle={onToggleAgent}
          onEdit={onEditAgent}
          onDelete={onDeleteAgent}
          onStartChat={onStartChat}
        />
      ))}
    </div>
  );
}
