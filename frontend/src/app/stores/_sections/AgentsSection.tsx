// frontend/src/app/stores/_sections/AgentsSection.tsx

"use client";

import React, { useCallback, useMemo, useState } from "react";
import { Bot, Plus } from "lucide-react";
import type { AgentManifest } from "@/lib/types";
import { AgentCard } from "../_components/AgentCard";
import {
  AgentDetailsModal,
  type AgentWithMeta,
} from "../_modals/AgentDetailsModal";
import { AgentSettingsModal } from "../_modals/AgentSettingsModal";
import { AgentToolsModal } from "../_modals/AgentToolsModal";
import { AgentMcpModal } from "../_modals/AgentMcpModal";
import { AgentSkillsModal } from "../_modals/AgentSkillsModal";
import { getApiUrl } from "../_hooks/useStoreData";
import type { SectionHeaderConfig } from "../_config/sections";
import type { AgentFilter } from "../_types/store";

export interface AgentsSectionProps {
  agents: AgentManifest[];
  searchQuery: string;
  agentFilter: AgentFilter;
  togglingAgentId: string | null;
  onToggleAgent: (agentId: string) => void;
  onEditAgent: (agent: AgentManifest) => void;
  onDeleteAgent: (agentId: string) => void;
  onStartChat: (agent: AgentManifest) => void;
  onReloadAgents?: () => void | Promise<void>;
}

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

async function fetchAgentManifest(
  agentId: string
): Promise<AgentWithMeta | null> {
  try {
    const res = await fetch(
      getApiUrl(`/api/store/agents/${encodeURIComponent(agentId)}`)
    );
    if (!res.ok) return null;
    const data = await res.json();
    const agent = data?.agent;
    return agent && typeof agent === "object"
      ? (agent as AgentWithMeta)
      : null;
  } catch {
    return null;
  }
}

export function AgentsSection({
  agents,
  searchQuery,
  agentFilter,
  togglingAgentId,
  onToggleAgent,
  onEditAgent,
  onDeleteAgent,
  onStartChat,
  onReloadAgents,
}: AgentsSectionProps) {
  const [detailsAgent, setDetailsAgent] = useState<AgentWithMeta | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [settingsAgent, setSettingsAgent] = useState<AgentWithMeta | null>(
    null
  );
  const [toolsAgent, setToolsAgent] = useState<AgentWithMeta | null>(null);
  const [skillsAgent, setSkillsAgent] = useState<AgentWithMeta | null>(null);
  const [mcpAgent, setMcpAgent] = useState<AgentWithMeta | null>(null);

  const openDetails = useCallback(async (agent: AgentManifest) => {
    const shortData = agent as AgentWithMeta;
    setDetailsAgent(shortData);
    setDetailsLoading(true);
    try {
      const full = await fetchAgentManifest(agent.id);
      setDetailsAgent((prev) =>
        prev && prev.id === agent.id && full
          ? ({ ...prev, ...full } as AgentWithMeta)
          : prev
      );
    } finally {
      setDetailsLoading(false);
    }
  }, []);

  const openSettings = useCallback((agent: AgentManifest) => {
    setSettingsAgent(agent as AgentWithMeta);
  }, []);

  const openTools = useCallback((agent: AgentManifest) => {
    setToolsAgent(agent as AgentWithMeta);
  }, []);

  const openSkills = useCallback((agent: AgentManifest) => {
    setSkillsAgent(agent as AgentWithMeta);
  }, []);

  const openMcp = useCallback((agent: AgentManifest) => {
    setMcpAgent(agent as AgentWithMeta);
  }, []);

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
    <div className="space-y-6">
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
            onViewDetails={openDetails}
            onOpenSettings={openSettings}
            onOpenTools={openTools}
            onOpenSkills={openSkills}
            onOpenMcp={openMcp}
          />
        ))}
      </div>

      {/* Details modal */}
      <AgentDetailsModal
        open={detailsAgent !== null}
        agent={detailsAgent}
        loading={detailsLoading}
        onClose={() => {
          setDetailsAgent(null);
          setDetailsLoading(false);
        }}
      />

      {/* Settings modal */}
      <AgentSettingsModal
        open={settingsAgent !== null}
        agentId={settingsAgent?.id ?? null}
        agentName={settingsAgent?.name ?? ""}
        onClose={() => setSettingsAgent(null)}
      />

      {/* Tools modal */}
      <AgentToolsModal
        open={toolsAgent !== null}
        agentId={toolsAgent?.id ?? null}
        agentName={toolsAgent?.name ?? ""}
        onClose={() => setToolsAgent(null)}
      />

      {/* Skills modal */}
      <AgentSkillsModal
        open={skillsAgent !== null}
        agentId={skillsAgent?.id ?? null}
        agentName={skillsAgent?.name ?? ""}
        onClose={() => setSkillsAgent(null)}
        onSaved={onReloadAgents}
      />

      {/* MCP modal */}
      <AgentMcpModal
        open={mcpAgent !== null}
        agentId={mcpAgent?.id ?? null}
        agentName={mcpAgent?.name ?? ""}
        onClose={() => setMcpAgent(null)}
        onSaved={onReloadAgents}
      />
    </div>
  );
}
