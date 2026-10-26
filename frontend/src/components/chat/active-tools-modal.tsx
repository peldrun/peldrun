// frontend/src/components/chat/ActiveToolsModal.tsx

"use client";

import React, { useState, useEffect, useMemo, useCallback } from "react";
import {
  Wrench,
  CheckCircle2,
  EyeOff,
  X,
  ExternalLink,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Settings2,
} from "lucide-react";
import { useChatStore } from "@/stores/chat-store";
import { fetchStoreAgents } from "@/lib/chatsApi";
import type { AgentManifest } from "@/lib/types";
import { ChatToolIcon } from "./ChatToolIcon";

/* ────────────────────────────────────────────────────────────────
 * Backend shape — mirrors GET /api/store/agents/{id}/tools
 * ──────────────────────────────────────────────────────────────── */

interface AgentToolInfo {
  id: string;
  name: string;
  description: string;
  category: string;
  is_builtin: boolean;
  safety_level?: string | null;
  in_manifest: boolean;
}

interface AgentToolsState {
  manifest_tools: string[];
  disabled_tools: string[];
  extra_tools: string[];
  effective_tools: string[];
  tools_editable: boolean;
  available_tools: AgentToolInfo[];
}

interface MCPServerInfo {
  id: string;
  name: string;
  status: string;
  description?: string;
}

/* ────────────────────────────────────────────────────────────────
 * Unified display type
 * ──────────────────────────────────────────────────────────────── */

type ToolSource = "builtin" | "mcp";

interface DisplayTool {
  id: string;
  name: string;
  description: string;
  category: string;
  source: ToolSource;
  is_builtin: boolean;
  safety_level?: string | null;
  serverName?: string;
}

/* ────────────────────────────────────────────────────────────────
 * Modal
 * ──────────────────────────────────────────────────────────────── */

/**
 * Read-only viewer of the tools the currently-selected agent will
 * actually use at runtime.
 *
 * Data flow:
 *   - ``GET /api/store/agents/{id}/tools`` → effective tool list
 *     (already accounts for disabled_tools / extra_tools overrides
 *     saved from the Store tools panel).
 *   - ``GET /api/mcp`` → the real MCP tool ids exposed by active MCP
 *     servers. The ``mcp`` pseudo-tool in a manifest expands to these.
 *
 * The modal is **not** an editor — it reflects the effective state and
 * links to ``/stores`` where the editor lives. This keeps the mental
 * model clean: chat shows what the agent has, Store lets you change it.
 */
export function ActiveToolsModal() {
  const { selectedAgentId } = useChatStore();

  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [toolsState, setToolsState] = useState<AgentToolsState | null>(null);
  const [mcpActiveTools, setMcpActiveTools] = useState<string[]>([]);
  const [mcpServers, setMcpServers] = useState<MCPServerInfo[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeFilter, setActiveFilter] = useState<"all" | "builtin" | "mcp">(
    "all"
  );

  const getApiUrl = useCallback((endpoint: string): string => {
    if (
      typeof window !== "undefined" &&
      (window.location.port === "3088" || window.location.port === "3000")
    ) {
      return `http://localhost:8088${endpoint}`;
    }
    return endpoint;
  }, []);

  /* ── Data loading ────────────────────────────────────────────── */

  const loadAgents = useCallback(async () => {
    try {
      const list = await fetchStoreAgents();
      if (Array.isArray(list)) setAgents(list);
    } catch (err) {
      console.error("Failed to load agents:", err);
    }
  }, []);

  const loadToolsState = useCallback(async () => {
    if (!selectedAgentId) {
      setToolsState(null);
      return;
    }
    try {
      const res = await fetch(
        getApiUrl(`/api/store/agents/${encodeURIComponent(selectedAgentId)}/tools`),
        { cache: "no-store" }
      );
      if (res.status === 403 || res.status === 404) {
        // Locked agent or missing — fall back to manifest-only view.
        setToolsState(null);
        return;
      }
      if (!res.ok) {
        setToolsState(null);
        return;
      }
      const data: AgentToolsState = await res.json();
      setToolsState(data);
    } catch {
      setToolsState(null);
    }
  }, [selectedAgentId, getApiUrl]);

  const loadMcpData = useCallback(async () => {
    try {
      const res = await fetch(getApiUrl("/api/mcp"), { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      setMcpActiveTools(
        Array.isArray(data.active_tools) ? data.active_tools : []
      );
      setMcpServers(Array.isArray(data.servers) ? data.servers : []);
    } catch (err) {
      console.error("Failed to load MCP tools:", err);
    }
  }, [getApiUrl]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([loadAgents(), loadToolsState(), loadMcpData()]);
    } catch (err: any) {
      setError(err?.message || "Failed to load tools.");
    } finally {
      setLoading(false);
    }
  }, [loadAgents, loadToolsState, loadMcpData]);

  // Load agents once on mount so the trigger badge can render a count.
  useEffect(() => {
    loadAgents();
  }, [loadAgents]);

  // Reload everything when modal opens or selected agent changes.
  useEffect(() => {
    if (isOpen) loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, selectedAgentId]);

  /* ── Resolve active agent ────────────────────────────────────── */

  const activeAgent: AgentManifest | null = useMemo(() => {
    if (!selectedAgentId) return null;
    return agents.find((a) => a.id === selectedAgentId) || null;
  }, [agents, selectedAgentId]);

  /* ── Build the display list ──────────────────────────────────── */

  const displayTools: DisplayTool[] = useMemo(() => {
    if (!activeAgent) return [];

    // Preferred path: use the effective tool list from the backend.
    if (toolsState) {
      const metaMap = new Map(
        toolsState.available_tools.map((t) => [t.id, t])
      );
      const result: DisplayTool[] = [];

      for (const id of toolsState.effective_tools) {
        const lowerId = id.toLowerCase();

        // Expand the "mcp" pseudo-tool into real MCP tool ids.
        if (lowerId === "mcp") {
          for (const mcpId of mcpActiveTools) {
            const server = mcpServers.find((s) =>
              String(mcpId).toLowerCase().includes(String(s.id).toLowerCase())
            );
            result.push({
              id: mcpId,
              name: mcpId,
              description:
                "MCP protocol tool exposed by an active server.",
              category: "mcp",
              source: "mcp",
              is_builtin: false,
              serverName: server?.name,
            });
          }
          continue;
        }

        // Expand explicit mcp:<tool> references.
        if (lowerId.startsWith("mcp:")) {
          result.push({
            id,
            name: id,
            description: "MCP protocol tool.",
            category: "mcp",
            source: "mcp",
            is_builtin: false,
          });
          continue;
        }

        // Standard built-in tool — use metadata when available.
        const meta = metaMap.get(id);
        result.push({
          id,
          name: meta?.name ?? id,
          description: meta?.description ?? "",
          category: meta?.category ?? "",
          source: "builtin",
          is_builtin: meta?.is_builtin ?? false,
          safety_level: meta?.safety_level ?? null,
        });
      }

      return result;
    }

    // Fallback path: agent is locked (tools_editable = false) or the
    // tools endpoint failed. Show the manifest tools verbatim so the
    // user still sees what the agent declares — without metadata.
    const manifestTools = Array.isArray(activeAgent.tools)
      ? activeAgent.tools
      : [];
    return manifestTools.map<DisplayTool>((id) => ({
      id,
      name: id,
      description: "",
      category: id === "mcp" ? "mcp" : "",
      source: id.toLowerCase() === "mcp" ? "mcp" : "builtin",
      is_builtin: false,
    }));
  }, [activeAgent, toolsState, mcpActiveTools, mcpServers]);

  /* ── Filter ─────────────────────────────────────────────────── */

  const filteredTools = useMemo(() => {
    if (activeFilter === "builtin")
      return displayTools.filter((t) => t.source === "builtin");
    if (activeFilter === "mcp")
      return displayTools.filter((t) => t.source === "mcp");
    return displayTools;
  }, [displayTools, activeFilter]);

  const disabledCount = toolsState?.disabled_tools.length ?? 0;
  const toolCount = displayTools.length;
  const displayName = activeAgent?.name || "Unknown Agent";

  /* ── Keyboard handler ────────────────────────────────────────── */

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isOpen) setIsOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen]);

  /* ── Icon helper ─────────────────────────────────────────────── */

  const renderIcon = (tool: DisplayTool) => (
    <ChatToolIcon
      toolId={tool.id}
      category={tool.category}
      isMcp={tool.source === "mcp"}
      size={14}
      className="shrink-0"
    />
  );

  /* ── Render ──────────────────────────────────────────────────── */

  return (
    <>
      {/* ── Trigger badge ─────────────────────────────────────── */}
      <button
        type="button"
        onClick={() => setIsOpen(true)}
        className="flex items-center mr-[5px] text-[11px] font-medium bg-transparent hover:bg-muted/70 text-foreground transition-all cursor-pointer"
        title={`View [ ${toolCount} ] active tools for ${displayName} `}
      >
        <Wrench size={13} className="text-primary" />

        {disabledCount > 0 && (
          <span className="px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-muted text-muted-foreground border border-border">
            {disabledCount} off
          </span>
        )}
      </button>

      {/* ── Modal ─────────────────────────────────────────────── */}
      {isOpen && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-100 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4 animate-in fade-in duration-150 font-sans"
          onClick={(e) => {
            if (e.target === e.currentTarget) setIsOpen(false);
          }}
        >
          <div className="bg-card border border-border rounded-xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden animate-in zoom-in-95 duration-150">
            {/* Header */}
            <div className="h-14 border-b border-border bg-gigbg px-5 flex items-center justify-between shrink-0">
              <div className="flex items-center gap-2.5 min-w-0">
                <div className="p-2 rounded-lg bg-primary/10 text-primary border border-primary/20 shrink-0">
                  <Wrench size={16} />
                </div>
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold font-heading text-foreground flex items-center gap-2">
                    <span className="truncate">{displayName}</span>
                    <span className="px-2 py-0.5 rounded text-[10px] bg-primary/15 text-primary border border-primary/30 font-mono shrink-0">
                      {toolCount} Tools
                    </span>
                    {disabledCount > 0 && (
                      <span className="px-2 py-0.5 rounded text-[10px] bg-muted text-muted-foreground border border-border font-mono shrink-0">
                        {disabledCount} disabled
                      </span>
                    )}
                  </h3>
                  <p className="text-[11px] text-muted-foreground truncate">
                    Effective tool set · resolves disabled/extra overrides
                    in real time.
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-1.5 shrink-0">
                <button
                  type="button"
                  onClick={loadAll}
                  disabled={loading}
                  className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
                  title="Refresh tool list"
                >
                  <RefreshCw
                    size={14}
                    className={loading ? "animate-spin text-primary" : ""}
                  />
                </button>
                <button
                  type="button"
                  onClick={() => setIsOpen(false)}
                  className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
                  title="Close modal (Esc)"
                >
                  <X size={16} />
                </button>
              </div>
            </div>

            {/* Filter tabs */}
            <div className="px-5 py-2.5 bg-muted border-b border-border/60 flex items-center justify-between text-xs shrink-0">
              <div className="flex items-center gap-1 bg-card p-0.5 rounded-lg border border-border/60 text-[11px] shadow-sm">
                {(["all", "builtin", "mcp"] as const).map((f) => (
                  <button
                    key={f}
                    type="button"
                    onClick={() => setActiveFilter(f)}
                    className={`px-2.5 py-1 rounded-md transition-all font-medium cursor-pointer ${
                      activeFilter === f
                        ? "bg-muted/90 text-foreground shadow-xs font-semibold"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {f === "all" ? "All" : f === "builtin" ? "Built-in" : "MCP"} (
                    {f === "all"
                      ? displayTools.length
                      : displayTools.filter((t) => t.source === f).length}
                    )
                  </button>
                ))}
              </div>

              <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground font-mono hidden sm:flex">
                <ShieldCheck size={13} className="text-emerald-500" />
                <span>Scoped to agent manifest</span>
              </div>
            </div>

            {/* Tools list */}
            <div className="flex-1 overflow-y-auto p-5 space-y-2.5 bg-custom">
              {error && (
                <div className="p-3 rounded-lg border border-destructive/30 bg-destructive/10 text-destructive text-xs">
                  {error}
                </div>
              )}

              {loading && displayTools.length === 0 ? (
                <div className="flex items-center justify-center py-12 text-muted-foreground gap-2 text-xs">
                  <Loader2 size={16} className="animate-spin text-primary" />
                  Loading tools from backend registry…
                </div>
              ) : !activeAgent ? (
                <div className="text-center py-10 text-xs text-muted-foreground border border-dashed border-border rounded-lg">
                  No agent selected. Pick an agent first from the Store.
                </div>
              ) : filteredTools.length === 0 ? (
                <div className="text-center py-10 text-xs text-muted-foreground border border-dashed border-border rounded-lg space-y-2">
                  <p className="font-medium text-foreground">
                    No tools match this filter.
                  </p>
                  {disabledCount > 0 && (
                    <p className="text-[10px] opacity-70">
                      {disabledCount} tool(s) disabled for this agent. Open
                      the Store to re-enable them.
                    </p>
                  )}
                </div>
              ) : (
                filteredTools.map((tool) => (
                  <div
                    key={`${tool.source}:${tool.id}`}
                    className="p-3 rounded-lg border border-border/80 bg-card hover:border-primary/40 transition-colors flex items-start justify-between gap-3 shadow-xs"
                  >
                    <div className="flex items-start gap-2.5 min-w-0">
                      <div className="p-1.5 rounded-md bg-muted/60 border border-border/50 shrink-0 mt-0.5">
                        {renderIcon(tool)}
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-xs font-semibold font-mono text-foreground truncate">
                            {tool.id}
                          </span>
                          {tool.category && (
                            <span className="text-[10px] px-1.5 py-0.2 rounded font-mono bg-muted text-muted-foreground border border-border/60">
                              {tool.category}
                            </span>
                          )}
                          {tool.is_builtin && (
                            <span className="text-[9px] font-mono uppercase px-1 py-0.2 rounded bg-sky-500/10 text-sky-700 dark:text-sky-300 border border-sky-500/20">
                              built-in
                            </span>
                          )}
                          {tool.source === "mcp" && tool.serverName && (
                            <span className="text-[10px] px-1.5 py-0.2 rounded font-mono bg-primary/10 text-primary border border-primary/20">
                              {tool.serverName}
                            </span>
                          )}
                        </div>
                        {tool.description && (
                          <p className="text-[11px] text-muted-foreground mt-0.5 leading-relaxed line-clamp-2">
                            {tool.description}
                          </p>
                        )}
                      </div>
                    </div>

                    <div className="flex items-center gap-1 text-[10px] text-emerald-500 font-medium shrink-0 pt-0.5">
                      <CheckCircle2 size={12} />
                      <span>Active</span>
                    </div>
                  </div>
                ))
              )}

              {/* Disabled tools notice */}
              {disabledCount > 0 && toolsState && (
                <div className="mt-3 p-3 rounded-lg border border-amber-500/30 bg-amber-500/5 text-[11px] text-amber-700 dark:text-amber-300 flex items-start gap-2">
                  <EyeOff size={13} className="shrink-0 mt-0.5" />
                  <div className="min-w-0">
                    <div className="font-semibold mb-0.5">
                      {disabledCount} tool(s) disabled for this agent
                    </div>
                    <div className="font-mono text-[10px] opacity-80 break-all">
                      {toolsState.disabled_tools.join(", ")}
                    </div>
                    <div className="mt-1 opacity-90">
                      Open the agent's tools panel in the Store to re-enable
                      them.
                    </div>
                  </div>
                </div>
              )}

              {/* Locked agent notice */}
              {!toolsState && activeAgent && (
                <div className="mt-3 p-3 rounded-lg border border-border bg-muted/30 text-[11px] text-muted-foreground">
                  This agent's tools are locked by its manifest — showing
                  the baseline tool list only.
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="h-12 border-t border-border bg-gigbg px-5 flex items-center justify-between shrink-0 text-xs">
              <a
                href="/stores"
                className="text-muted-foreground hover:text-primary transition-colors flex items-center gap-1.5 font-medium"
              >
                <Settings2 size={13} />
                <span>Manage Tools & MCP Servers</span>
                <ExternalLink size={11} className="opacity-70" />
              </a>

              <button
                type="button"
                onClick={() => setIsOpen(false)}
                className="px-3 py-1.5 rounded-xs bg-black/70 text-card hover:bg-black/90 font-medium transition-colors cursor-pointer text-xs"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export default ActiveToolsModal;
