// frontend/src/components/chat/ActiveToolsModal.tsx

"use client";

import React, { useState, useEffect, useMemo, useCallback } from "react";
import {
  Wrench,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  EyeOff,
  X,
  ExternalLink,
  Loader2,
  RefreshCw,
  Server,
  ShieldCheck,
  Settings2,
  AlertCircle,
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
 * Unified display types
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

interface McpGroup {
  serverId: string;
  serverName: string;
  serverStatus: string;
  tools: DisplayTool[];
  /** True once the per-server tool probe has finished (success or error). */
  loaded: boolean;
  /** Non-null when the probe failed. */
  error: string | null;
}

/* ────────────────────────────────────────────────────────────────
 * Modal
 * ──────────────────────────────────────────────────────────────── */

export function ActiveToolsModal() {
  const { selectedAgentId } = useChatStore();

  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [toolsState, setToolsState] = useState<AgentToolsState | null>(null);
  const [effectiveMcpServers, setEffectiveMcpServers] = useState<string[]>([]);
  const [mcpToolsByServer, setMcpToolsByServer] = useState<
    Record<string, Array<{ name: string; description: string }>>
  >({});
  const [mcpServerErrors, setMcpServerErrors] = useState<
    Record<string, string>
  >({});
  const [mcpServers, setMcpServers] = useState<MCPServerInfo[]>([]);
  const [mcpToolsLoading, setMcpToolsLoading] = useState(false);
  // Expanded state per server. Missing entry = collapsed (accordion default).
  const [expandedServers, setExpandedServers] = useState<Set<string>>(
    new Set(),
  );
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeFilter, setActiveFilter] = useState<"all" | "builtin" | "mcp">(
    "all",
  );

  // Relative path only - SSR/CSR parity. See next.config.mjs.
  // Kept as useCallback for stable identity across dependent callbacks.
  const getApiUrl = useCallback((endpoint: string): string => {
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
        getApiUrl(
          `/api/store/agents/${encodeURIComponent(selectedAgentId)}/tools`,
        ),
        { cache: "no-store" },
      );
      if (res.status === 403 || res.status === 404) {
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

  const loadMcpMeta = useCallback(async () => {
    if (!selectedAgentId) {
      setEffectiveMcpServers([]);
      setMcpServers([]);
      return;
    }
    try {
      const [agentMcpRes, allMcpRes] = await Promise.all([
        fetch(
          getApiUrl(
            `/api/store/agents/${encodeURIComponent(selectedAgentId)}/mcp`,
          ),
          { cache: "no-store" },
        ),
        fetch(getApiUrl("/api/mcp"), { cache: "no-store" }),
      ]);

      if (agentMcpRes.ok) {
        const data = await agentMcpRes.json();
        setEffectiveMcpServers(
          Array.isArray(data.effective_mcp_servers)
            ? data.effective_mcp_servers
            : [],
        );
      } else {
        setEffectiveMcpServers([]);
      }

      if (allMcpRes.ok) {
        const data = await allMcpRes.json();
        setMcpServers(Array.isArray(data.servers) ? data.servers : []);
      }
    } catch (err) {
      console.error("Failed to load MCP metadata:", err);
    }
  }, [selectedAgentId, getApiUrl]);

  /**
   * Probe each effective MCP server for its tool list.
   *
   * Two performance optimisations:
   *   1. Skip servers already loaded (unless they failed last time),
   *      so refreshes do not re-probe every server.
   *   2. Update ``mcpToolsByServer`` incrementally as each server's
   *      probe completes. The UI shows tools as they arrive instead
   *      of waiting for the slowest server.
   *
   * Backend note: ``GET /api/mcp/servers/{id}/tools`` currently
   * spawns a fresh stdio subprocess per call (no cache at the route
   * level). This is the dominant source of latency for complex
   * servers (github ~3s, filesystem ~0.5s). A follow-up backend
   * change should route this endpoint through ``get_tools`` for
   * cache reuse.
   */
  const loadMcpTools = useCallback(async () => {
    if (effectiveMcpServers.length === 0) {
      setMcpToolsByServer({});
      setMcpServerErrors({});
      setMcpToolsLoading(false);
      return;
    }

    // Skip servers that have already been successfully loaded. Failed
    // servers are retried (their error is cleared so the UI shows the
    // spinner again during the retry).
    const toLoad = effectiveMcpServers.filter((sid) => {
      const isLoaded = sid in mcpToolsByServer;
      const hasError = Boolean(mcpServerErrors[sid]);
      return !isLoaded || hasError;
    });

    if (toLoad.length === 0) {
      setMcpToolsLoading(false);
      return;
    }

    setMcpToolsLoading(true);

    // Clear previous errors for servers being retried so the UI shows
    // a clean "loading" state rather than a stale error badge.
    setMcpServerErrors((prev) => {
      const next = { ...prev };
      for (const sid of toLoad) delete next[sid];
      return next;
    });

    await Promise.all(
      toLoad.map(async (serverId) => {
        try {
          const res = await fetch(
            getApiUrl(
              `/api/mcp/servers/${encodeURIComponent(serverId)}/tools`,
            ),
            { cache: "no-store" },
          );
          if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            const msg =
              typeof body?.detail === "string"
                ? body.detail
                : `HTTP ${res.status}`;
            setMcpServerErrors((prev) => ({ ...prev, [serverId]: msg }));
            setMcpToolsByServer((prev) => ({ ...prev, [serverId]: [] }));
            return;
          }
          const data = await res.json();
          if (data?.status === "error") {
            setMcpServerErrors((prev) => ({
              ...prev,
              [serverId]: String(data.message || "Probe failed"),
            }));
            setMcpToolsByServer((prev) => ({ ...prev, [serverId]: [] }));
            return;
          }
          const tools = Array.isArray(data.tools) ? data.tools : [];
          setMcpToolsByServer((prev) => ({
            ...prev,
            [serverId]: tools.map((t: any) => ({
              name: String(t?.name || ""),
              description: String(t?.description || ""),
            })),
          }));
        } catch (err: any) {
          setMcpServerErrors((prev) => ({
            ...prev,
            [serverId]: err?.message || "Network error",
          }));
          setMcpToolsByServer((prev) => ({ ...prev, [serverId]: [] }));
        }
      }),
    );

    setMcpToolsLoading(false);
  }, [
    effectiveMcpServers,
    mcpToolsByServer,
    mcpServerErrors,
    getApiUrl,
  ]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([loadAgents(), loadToolsState(), loadMcpMeta()]);
    } catch (err: any) {
      setError(err?.message || "Failed to load tools.");
    } finally {
      setLoading(false);
    }
  }, [loadAgents, loadToolsState, loadMcpMeta]);

  /* ── Effects ─────────────────────────────────────────────────── */

  // Load agents once on mount so the trigger badge can render a count.
  useEffect(() => {
    loadAgents();
  }, [loadAgents]);

  // Fetch per-server tools whenever the effective server list changes.
  // loadMcpTools reads the current mcpToolsByServer/mcpServerErrors to
  // decide which servers to (re)probe — see its docstring.
  useEffect(() => {
    if (isOpen) {
      loadMcpTools();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveMcpServers, isOpen]);

  // Reload everything when modal opens or selected agent changes.
  // Reset MCP state to prevent cross-agent leakage while loading.
  useEffect(() => {
    if (!isOpen) return;
    setEffectiveMcpServers([]);
    setMcpToolsByServer({});
    setMcpServerErrors({});
    setMcpToolsLoading(false);
    loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, selectedAgentId]);

  /* ── Resolve active agent ────────────────────────────────────── */

  const activeAgent: AgentManifest | null = useMemo(() => {
    if (!selectedAgentId) return null;
    return agents.find((a) => a.id === selectedAgentId) || null;
  }, [agents, selectedAgentId]);

  /* ── Build display lists ─────────────────────────────────────── */

  const builtinTools = useMemo<DisplayTool[]>(() => {
    if (!activeAgent) return [];

    if (toolsState) {
      const metaMap = new Map(
        toolsState.available_tools.map((t) => [t.id, t]),
      );
      const result: DisplayTool[] = [];

      for (const id of toolsState.effective_tools) {
        const lowerId = id.toLowerCase();
        if (lowerId === "mcp") continue;
        if (lowerId.startsWith("mcp:") || lowerId.startsWith("mcp__")) continue;

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

    const manifestTools = Array.isArray(activeAgent.tools)
      ? activeAgent.tools
      : [];
    return manifestTools
      .filter(
        (id) =>
          id.toLowerCase() !== "mcp" &&
          !id.toLowerCase().startsWith("mcp:") &&
          !id.toLowerCase().startsWith("mcp__"),
      )
      .map<DisplayTool>((id) => ({
        id,
        name: id,
        description: "",
        category: "",
        source: "builtin",
        is_builtin: false,
      }));
  }, [activeAgent, toolsState]);

  const mcpGroups = useMemo<McpGroup[]>(() => {
    if (!activeAgent) return [];

    const serverById = new Map(mcpServers.map((s) => [s.id, s]));

    return effectiveMcpServers.map((serverId) => {
      const srvInfo = serverById.get(serverId);
      const tools = (mcpToolsByServer[serverId] || []).map<DisplayTool>((t) => ({
        id: `mcp__${serverId}__${t.name}`,
        name: t.name,
        description: t.description,
        category: "mcp",
        source: "mcp",
        is_builtin: false,
        serverName: srvInfo?.name || serverId,
      }));

      return {
        serverId,
        serverName: srvInfo?.name || serverId,
        serverStatus: srvInfo?.status || "unknown",
        tools,
        loaded: serverId in mcpToolsByServer,
        error: mcpServerErrors[serverId] || null,
      };
    });
  }, [
    activeAgent,
    effectiveMcpServers,
    mcpToolsByServer,
    mcpServers,
    mcpServerErrors,
  ]);

  const totalToolCount = useMemo(
    () =>
      builtinTools.length + mcpGroups.reduce((acc, g) => acc + g.tools.length, 0),
    [builtinTools, mcpGroups],
  );

  const mcpToolCount = totalToolCount - builtinTools.length;
  const disabledCount = toolsState?.disabled_tools.length ?? 0;
  const displayName = activeAgent?.name || "Unknown Agent";

  /* ── Accordion toggle ────────────────────────────────────────── */

  const toggleServerExpanded = useCallback((serverId: string) => {
    setExpandedServers((prev) => {
      const next = new Set(prev);
      if (next.has(serverId)) next.delete(serverId);
      else next.add(serverId);
      return next;
    });
  }, []);

  /* ── Keyboard handler ────────────────────────────────────────── */

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isOpen) setIsOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen]);

  /* ── Render helpers ──────────────────────────────────────────── */

  const renderIcon = (tool: DisplayTool) => (
    <ChatToolIcon
      toolId={tool.id}
      category={tool.category}
      isMcp={tool.source === "mcp"}
      size={18}
      className="shrink-0"
    />
  );

  const renderToolCard = (tool: DisplayTool) => (
    <div
      key={`${tool.source}:${tool.id}`}
      className="p-3 rounded-md border border-border/80 bg-muted hover:border-primary/40 transition-colors flex items-start justify-between gap-3 shadow-xs"
    >
      <div className="flex items-center gap-2.5 min-w-0">
        <div className="toolcallcrdIcon shrink-0 mt-0.5 flex items-center justify-center">
          {renderIcon(tool)}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs font-semibold font-mono text-foreground truncate">
              {tool.name}
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
  );

  const renderMcpGroup = (group: McpGroup) => {
    const isExpanded = expandedServers.has(group.serverId);
    const isLoading = !group.loaded;
    const hasError = group.error !== null;
    const isNotActive = group.serverStatus !== "active";

    return (
      <div
        key={group.serverId}
        className="rounded-md border border-border/80 bg-muted/20 overflow-hidden"
      >
        <button
          type="button"
          onClick={() => toggleServerExpanded(group.serverId)}
          className="w-full flex items-center justify-between gap-2 px-3 py-2.5 bg-muted/40 hover:bg-muted/70 transition-colors cursor-pointer"
          aria-expanded={isExpanded}
        >
          <div className="flex items-center gap-2 min-w-0">
            {isExpanded ? (
              <ChevronDown
                size={14}
                className="shrink-0 text-muted-foreground"
              />
            ) : (
              <ChevronRight
                size={14}
                className="shrink-0 text-muted-foreground"
              />
            )}
            <Server size={13} className="shrink-0 text-primary" />
            <span className="text-xs font-semibold text-foreground truncate">
              {group.serverName}
            </span>
            {isNotActive && (
              <span className="text-[9px] px-1 py-0.2 rounded font-mono uppercase bg-amber-500/10 text-amber-600 border border-amber-500/20 shrink-0">
                inactive
              </span>
            )}
            {isLoading ? (
              <span className="flex items-center gap-1 text-[10px] font-mono text-primary shrink-0">
                <Loader2 size={10} className="animate-spin" />
                loading
              </span>
            ) : hasError ? (
              <span className="flex items-center gap-1 text-[10px] font-mono text-red-500 shrink-0">
                <AlertCircle size={10} />
                error
              </span>
            ) : (
              <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-primary/10 text-primary border border-primary/20 shrink-0">
                {group.tools.length}{" "}
                {group.tools.length === 1 ? "tool" : "tools"}
              </span>
            )}
          </div>
        </button>

        {isExpanded && (
          <div className="p-2 space-y-2 border-t border-border/60">
            {isLoading && (
              <div className="flex items-center justify-center gap-2 py-4 text-xs text-muted-foreground">
                <Loader2 size={12} className="animate-spin text-primary" />
                Discovering tools from {group.serverName}…
              </div>
            )}

            {!isLoading && hasError && (
              <div className="p-3 rounded bg-red-500/5 border border-red-500/20 text-[11px] text-red-600 dark:text-red-400">
                <div className="font-semibold mb-1 flex items-center gap-1">
                  <AlertCircle size={11} />
                  Failed to load tools
                </div>
                <div className="font-mono text-[10px] opacity-90 break-all">
                  {group.error}
                </div>
              </div>
            )}

            {!isLoading && !hasError && group.tools.length === 0 && (
              <div className="py-3 text-center text-[11px] text-muted-foreground italic">
                This server exposes no tools.
              </div>
            )}

            {!isLoading &&
              !hasError &&
              group.tools.map((tool) => renderToolCard(tool))}
          </div>
        )}
      </div>
    );
  };

  /* ── Derived empty / visibility flags ────────────────────────── */

  const showBuiltinSection =
    activeFilter === "all" || activeFilter === "builtin";
  const showMcpSection = activeFilter === "all" || activeFilter === "mcp";

  const filterHasResults =
    (activeFilter === "builtin" && builtinTools.length > 0) ||
    (activeFilter === "mcp" && effectiveMcpServers.length > 0) ||
    (activeFilter === "all" &&
      (builtinTools.length > 0 || effectiveMcpServers.length > 0));

  /* ── Render ──────────────────────────────────────────────────── */

  return (
    <>
      {/* ── Trigger badge ─────────────────────────────────────── */}
      <button
        type="button"
        onClick={() => setIsOpen(true)}
        className="flex items-center mr-[5px] text-[11px] font-medium bg-transparent hover:bg-muted/70 text-foreground transition-all cursor-pointer"
        title={`View [ ${totalToolCount} ] active tools for ${displayName} `}
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
          <div className="bg-card border border-card rounded-sm shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden animate-in zoom-in-95 duration-150">
            {/* Header */}
            <div className="h-14 border-b border-border bg-gigbg px-5 flex items-center justify-between shrink-0">
              <div className="flex items-center gap-2.5 min-w-0">
                <div className="WrenchIcon shrink-0">
                  <Wrench size={16} />
                </div>
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold font-heading text-foreground flex items-center gap-2">
                    <span className="truncate">{displayName}</span>
                    <span className="px-2 py-0.5 rounded text-[10px] bg-primary/15 text-primary border border-primary/30 font-mono shrink-0">
                      {totalToolCount} Tools
                    </span>
                    {disabledCount > 0 && (
                      <span className="px-2 py-0.5 rounded text-[10px] bg-muted text-muted-foreground border border-border font-mono shrink-0">
                        {disabledCount} disabled
                      </span>
                    )}
                    {mcpToolsLoading && (
                      <span className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-primary/10 text-primary border border-primary/20 font-mono shrink-0">
                        <Loader2 size={9} className="animate-spin" />
                        loading
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
            <div className="px-5 py-2.5 bg-custom border-b border-border/60 flex items-center justify-between text-xs shrink-0">
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
                      ? totalToolCount
                      : f === "builtin"
                        ? builtinTools.length
                        : mcpToolCount}
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
            <div className="flex-1 overflow-y-auto p-5 space-y-2.5 bg-background">
              {error && (
                <div className="p-3 rounded-lg border border-destructive/30 bg-destructive/10 text-destructive text-xs">
                  {error}
                </div>
              )}

              {loading && totalToolCount === 0 ? (
                <div className="flex items-center justify-center py-12 text-muted-foreground gap-2 text-xs">
                  <Loader2 size={16} className="animate-spin text-primary" />
                  Loading tools from backend registry…
                </div>
              ) : !activeAgent ? (
                <div className="text-center py-10 text-xs text-muted-foreground border border-dashed border-border rounded-lg">
                  No agent selected. Pick an agent first from the Store.
                </div>
              ) : !filterHasResults ? (
                <div className="text-center py-10 text-xs text-muted-foreground border border-dashed border-border rounded-lg space-y-2">
                  <p className="font-medium text-foreground">
                    {activeFilter === "builtin"
                      ? "No built-in tools available."
                      : activeFilter === "mcp"
                        ? "No MCP servers configured for this agent."
                        : "No tools available for this agent."}
                  </p>
                  {disabledCount > 0 && (
                    <p className="text-[10px] opacity-70">
                      {disabledCount} tool(s) disabled for this agent. Open
                      the Store to re-enable them.
                    </p>
                  )}
                </div>
              ) : (
                <>
                  {/* ── Built-in Tools Section ────────────────── */}
                  {showBuiltinSection && builtinTools.length > 0 && (
                    <div className="space-y-2">
                      {activeFilter === "all" && (
                        <div className="flex items-center gap-2 pt-1">
                          <span className="text-[10px] uppercase tracking-wider font-bold text-muted-foreground">
                            Built-in Tools
                          </span>
                          <span className="text-[10px] font-mono text-muted-foreground">
                            ({builtinTools.length})
                          </span>
                          <div className="flex-1 h-px bg-border/40" />
                        </div>
                      )}
                      {builtinTools.map((tool) => renderToolCard(tool))}
                    </div>
                  )}

                  {/* ── MCP Tools Section (Accordions) ────────── */}
                  {showMcpSection && effectiveMcpServers.length > 0 && (
                    <div className="space-y-2">
                      {activeFilter === "all" && (
                        <div className="flex items-center gap-2 pt-3">
                          <span className="text-[10px] uppercase tracking-wider font-bold text-muted-foreground">
                            MCP Tools
                          </span>
                          <span className="text-[10px] font-mono text-muted-foreground">
                            ({mcpToolCount})
                          </span>
                          <div className="flex-1 h-px bg-border/40" />
                          {mcpToolsLoading && (
                            <span className="flex items-center gap-1 text-[10px] text-primary font-mono">
                              <Loader2 size={9} className="animate-spin" />
                              discovering
                            </span>
                          )}
                        </div>
                      )}

                      {mcpGroups.map((group) => renderMcpGroup(group))}
                    </div>
                  )}

                  {/* ── Disabled tools notice ─────────────────── */}
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
                          Open the agent's tools panel in the Store to
                          re-enable them.
                        </div>
                      </div>
                    </div>
                  )}

                  {/* ── Locked agent notice ───────────────────── */}
                  {!toolsState && activeAgent && (
                    <div className="mt-3 p-3 rounded-lg border border-border bg-muted/30 text-[11px] text-muted-foreground">
                      This agent's tools are locked by its manifest — showing
                      the baseline tool list only.
                    </div>
                  )}
                </>
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
