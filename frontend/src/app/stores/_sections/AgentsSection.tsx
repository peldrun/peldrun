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
import { getApiUrl } from "../_hooks/useStoreData";
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
  /**
   * Reload the agents collection from the backend.
   *
   * Invoked by:
   *   - ``AgentMcpModal.onSaved`` — after a successful MCP server
   *     save, so the ``N MCP`` badge on the card refreshes with the
   *     new ``effective_mcp_servers`` value.
   *   - (future) any other modal that mutates an agent's manifest
   *     without going through the builder form.
   *
   * Optional to keep this section usable in isolation (tests,
   * storybook). When omitted, the modal still saves successfully —
   * the card just will not reflect the change until the next full
   * page reload. That matches the pre-Phase-10 behaviour, so this
   * addition is strictly non-breaking.
   */
  onReloadAgents?: () => void | Promise<void>;
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
 * Fetch the full manifest for a single agent.
 *
 * The list endpoint (`GET /agents`) returns the same payload as the
 * detail endpoint, but the row already has the short data available.
 * This helper exists so the details modal can lazily pull the full
 * manifest without forcing the section to re-fetch the whole catalog.
 */
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

/**
 * Agents Hub section content.
 *
 * Responsibilities:
 *  - Filter the agents collection by `searchQuery` + `agentFilter`.
 *  - Render a responsive grid of {@link AgentCard}.
 *  - Render a friendly empty state when no agents match.
 *  - Own the modal state for Details and Settings (local to this
 *    section because it does not need to be coordinated elsewhere).
 *
 * The section remains otherwise stateless: every mutation is
 * delegated to the parent (backed by `useAgentActions`).
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
  onReloadAgents,
}: AgentsSectionProps) {
  // Modal state — which agent (if any) is being viewed / configured.
  const [detailsAgent, setDetailsAgent] = useState<AgentWithMeta | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [settingsAgent, setSettingsAgent] = useState<AgentWithMeta | null>(
    null
  );
  const [toolsAgent, setToolsAgent] = useState<AgentWithMeta | null>(null);
  const [mcpAgent, setMcpAgent] = useState<AgentWithMeta | null>(null);

  /**
   * Open the details modal for an agent.
   *
   * Strategy (same as the Tools section):
   *  1. Display the row's already-loaded short data immediately so the
   *     user sees content without any delay.
   *  2. Fetch the full manifest from `/api/store/agents/{id}` in the
   *     background.
   *  3. Merge the response into the currently-open agent — but only if
   *     the modal is still showing the same agent (guards against a
   *     race when the user closes and opens a different agent quickly).
   */
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

  /**
   * Open the settings modal for an agent. The card only triggers this
   * when `hasConfigurableSettings(agent)` is true, so we do not need
   * to re-check the schema here — the modal itself handles the
   * "no settings" case with a friendly 404 message if it happens.
   */
  const openSettings = useCallback((agent: AgentManifest) => {
    setSettingsAgent(agent as AgentWithMeta);
  }, []);

  /**
   * Open the tools modal for an agent. The card only triggers this
   * when `settings.tools_editable = true`, and the backend enforces
   * the same gate with a 403 (defense in depth).
   */
  const openTools = useCallback((agent: AgentManifest) => {
    setToolsAgent(agent as AgentWithMeta);
  }, []);

  /**
   * Open the MCP protocol servers modal for an agent to configure
   * which external tool servers are bound to its execution runtime.
   */
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
            onOpenMcp={openMcp}
          />
        ))}
      </div>

      {/* ── Agent details modal ─────────────────────────────────── */}
      <AgentDetailsModal
        open={detailsAgent !== null}
        agent={detailsAgent}
        loading={detailsLoading}
        onClose={() => {
          setDetailsAgent(null);
          setDetailsLoading(false);
        }}
      />

      {/* ── Agent settings modal ────────────────────────────────── */}
      <AgentSettingsModal
        open={settingsAgent !== null}
        agentId={settingsAgent?.id ?? null}
        agentName={settingsAgent?.name ?? ""}
        onClose={() => setSettingsAgent(null)}
      />

      {/* ── Agent tools modal ──────────────────────────────────── */}
      <AgentToolsModal
        open={toolsAgent !== null}
        agentId={toolsAgent?.id ?? null}
        agentName={toolsAgent?.name ?? ""}
        onClose={() => setToolsAgent(null)}
      />

      {/* ── Agent MCP modal ────────────────────────────────────── */}
      {/*
       * ``onSaved`` must trigger a full reload of the agents list so
       * the parent re-fetches ``effective_mcp_servers`` (which is
       * computed from agent_settings.json on the backend). Without
       * this, the ``N MCP`` badge on the card stays stuck at its
       * pre-save value until the user refreshes the page — exactly
       * the UX bug documented in the audit report.
       *
       * If ``onReloadAgents`` is not provided, the modal still saves
       * successfully (the backend update is not gated on this); the
       * card just will not reflect the change. This preserves
       * backward compatibility with any parent that has not yet
       * wired up a reload handler.
       */}
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
