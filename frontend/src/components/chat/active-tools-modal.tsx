"use client";

import React, { useState, useEffect, useMemo } from "react";
import {
  Wrench,
  Server,
  Terminal,
  Code2,
  Globe,
  FileCode2,
  CheckCircle2,
  X,
  ExternalLink,
  Loader2,
  RefreshCw,
  Settings,
  ShieldCheck,
  Save,
  Search,
  Bot,
  Puzzle,
} from "lucide-react";
import { useChatStore } from "@/stores/chat-store";
import { fetchStoreAgents, fetchStoreTools } from "@/lib/chatsApi";
import { AgentManifest, ToolDefinition } from "@/lib/types";

// ============================================================
// Internal types
// ============================================================
type ToolSource = "builtin" | "mcp";

interface UnifiedTool {
  id: string;
  name: string;
  description: string;
  category: string;
  source: ToolSource;
  is_builtin: boolean;
  is_enabled: boolean;
  safety_level?: string;
  serverName?: string;
}

// ============================================================
// Icon mapper — categorizes tools by id/category, never hardcoded per tool
// ============================================================
function pickToolIcon(tool: UnifiedTool) {
  const id = tool.id.toLowerCase();
  const cat = (tool.category || "").toLowerCase();

  if (tool.source === "mcp") return <Server size={14} className="text-primary shrink-0" />;
  if (id.includes("python") || cat === "execution") return <Code2 size={14} className="text-amber-500 shrink-0" />;
  if (id.includes("bash") || id.includes("shell") || cat === "system") return <Terminal size={14} className="text-emerald-500 shrink-0" />;
  if (id.includes("search") || cat === "search") return <Search size={14} className="text-sky-500 shrink-0" />;
  if (id.includes("editor")) return <FileCode2 size={14} className="text-purple-500 shrink-0" />;
  if (id.includes("saver") || cat === "storage") return <Save size={14} className="text-pink-500 shrink-0" />;
  if (id.includes("browser") || cat === "network") return <Globe size={14} className="text-cyan-500 shrink-0" />;
  if (id.includes("human") || cat === "interaction") return <Bot size={14} className="text-indigo-500 shrink-0" />;
  return <Wrench size={14} className="text-muted-foreground shrink-0" />;
}

// ============================================================
// Main Modal Component
// ============================================================
export function ActiveToolsModal() {
  const { selectedAgentId } = useChatStore();

  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [mcpTools, setMcpTools] = useState<UnifiedTool[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [activeFilter, setActiveFilter] = useState<"all" | "builtin" | "mcp">("all");

  const getApiUrl = (endpoint: string) => {
    if (typeof window !== "undefined" && (window.location.port === "3088" || window.location.port === "3000")) {
      return `http://localhost:8088${endpoint}`;
    }
    return endpoint;
  };

  // ---------------------------------------------------------
  // Fetch agents + tools from the authoritative backend store
  // ---------------------------------------------------------
  const loadData = async () => {
    setLoading(true);
    try {
      const [agentList, toolList] = await Promise.all([
        fetchStoreAgents(),
        fetchStoreTools(),
      ]);
      if (Array.isArray(agentList)) setAgents(agentList);
      if (Array.isArray(toolList)) setTools(toolList);
    } catch (err) {
      console.error("Failed to load store data:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  // ---------------------------------------------------------
  // Fetch live MCP tools from active servers
  // ---------------------------------------------------------
  const fetchActiveMcpTools = async () => {
    try {
      const res = await fetch(getApiUrl("/api/mcp"), { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      const activeServers = (data.servers || []).filter((s: any) => s.status === "active");

      const collected: UnifiedTool[] = [];
      activeServers.forEach((server: any) => {
        const sid = String(server.id || "").toLowerCase();

        if (sid.includes("filesystem")) {
          const entries = [
            { id: "mcp:read_file", desc: "Read content from workspace files via MCP." },
            { id: "mcp:write_file", desc: "Write and update files via MCP filesystem transport." },
            { id: "mcp:list_directory", desc: "Explore folder hierarchies and directory entries." },
          ];
          entries.forEach((e) => collected.push({
            id: e.id,
            name: e.id.replace("mcp:", "MCP: ").replace(/_/g, " "),
            description: e.desc,
            category: "mcp",
            source: "mcp",
            is_builtin: false,
            is_enabled: true,
            serverName: server.name,
          }));
        } else if (sid.includes("github")) {
          const entries = [
            { id: "mcp:get_issue", desc: "Fetch and manage GitHub repository issues." },
            { id: "mcp:search_repositories", desc: "Search public and private GitHub repositories." },
          ];
          entries.forEach((e) => collected.push({
            id: e.id,
            name: e.id.replace("mcp:", "MCP: ").replace(/_/g, " "),
            description: e.desc,
            category: "mcp",
            source: "mcp",
            is_builtin: false,
            is_enabled: true,
            serverName: server.name,
          }));
        } else {
          collected.push({
            id: `mcp:${sid}`,
            name: `MCP: ${server.name}`,
            description: server.description || "Active MCP server tools.",
            category: "mcp",
            source: "mcp",
            is_builtin: false,
            is_enabled: true,
            serverName: server.name,
          });
        }
      });
      setMcpTools(collected);
    } catch (err) {
      console.error("Failed to fetch MCP tools", err);
    }
  };

  useEffect(() => {
    if (isOpen) fetchActiveMcpTools();
  }, [isOpen]);

  // ---------------------------------------------------------
  // Resolve the active agent manifest (falls back safely)
  // ---------------------------------------------------------
  const activeAgent: AgentManifest | null = useMemo(() => {
    if (!selectedAgentId) return null;
    return agents.find((a) => a.id === selectedAgentId) || null;
  }, [agents, selectedAgentId]);

  // ---------------------------------------------------------
  // THE ONLY FILTER: intersect backend tools with agent.tools
  // ---------------------------------------------------------
  const agentScopedTools: UnifiedTool[] = useMemo(() => {
    if (!activeAgent) return [];

    const manifestToolIds = Array.isArray(activeAgent.tools)
      ? activeAgent.tools.map((t: string) => String(t).toLowerCase().trim())
      : [];
    const allowedSet = new Set(manifestToolIds);

    // Built-in tools: exact id match against the registry
    const matchedBuiltins: UnifiedTool[] = tools
      .filter((t) => {
        const idLower = (t.id || "").toLowerCase();
        return allowedSet.has(idLower) && t.is_enabled !== false && t.status !== "disabled";
      })
      .map((t) => ({
        id: t.id,
        name: t.name,
        description: t.description || "",
        category: t.category || "builtin",
        source: "builtin" as ToolSource,
        is_builtin: t.is_builtin ?? true,
        is_enabled: true,
        safety_level: t.safety_level,
      }));

    // MCP tools: only if the manifest explicitly opts in
    const hasMcpPermission = manifestToolIds.some(
      (t) => t === "mcp" || t.startsWith("mcp:")
    );
    const matchedMcp = hasMcpPermission ? mcpTools : [];

    return [...matchedBuiltins, ...matchedMcp];
  }, [activeAgent, tools, mcpTools]);

  // ---------------------------------------------------------
  // Filter for tabs
  // ---------------------------------------------------------
  const filteredTools = useMemo(() => {
    if (activeFilter === "builtin") return agentScopedTools.filter((t) => t.source === "builtin");
    if (activeFilter === "mcp") return agentScopedTools.filter((t) => t.source === "mcp");
    return agentScopedTools;
  }, [agentScopedTools, activeFilter]);

  // ---------------------------------------------------------
  // ESC closes modal
  // ---------------------------------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isOpen) setIsOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen]);

  // ---------------------------------------------------------
  // Render
  // ---------------------------------------------------------
  const toolCount = agentScopedTools.length;
  const displayName = activeAgent?.name || "Unknown Agent";

  return (
    <>
      {/* Trigger Badge */}
      <button
        type="button"
        onClick={() => setIsOpen(true)}
        className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium border border-border bg-card hover:bg-muted/70 text-foreground transition-all cursor-pointer shadow-xs"
        title={`View active tools for ${displayName}`}
      >
        <Wrench size={12} className="text-primary" />
        <span className="font-medium hidden sm:inline">Active Tools</span>
        <span className="px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-primary/10 text-primary border border-primary/20">
          {toolCount}
        </span>
      </button>

      {isOpen && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-100 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4 animate-in fade-in duration-150 font-sans"
          onClick={(e) => { if (e.target === e.currentTarget) setIsOpen(false); }}
        >
          <div className="bg-card border border-border rounded-xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden animate-in zoom-in-95 duration-150">
            {/* Header */}
            <div className="h-14 border-b border-border bg-muted/30 px-5 flex items-center justify-between shrink-0">
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
                  </h3>
                  <p className="text-[11px] text-muted-foreground truncate">
                    Live from backend registry · scoped by agent manifest.
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-1.5 shrink-0">
                <button
                  type="button"
                  onClick={loadData}
                  disabled={loading}
                  className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
                  title="Refresh tool list"
                >
                  <RefreshCw size={14} className={loading ? "animate-spin text-primary" : ""} />
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

            {/* Filter Tabs */}
            <div className="px-5 py-2.5 bg-card border-b border-border/60 flex items-center justify-between text-xs shrink-0">
              <div className="flex items-center gap-1 bg-muted/60 p-0.5 rounded-lg border border-border/60 text-[11px]">
                {(["all", "builtin", "mcp"] as const).map((f) => (
                  <button
                    key={f}
                    type="button"
                    onClick={() => setActiveFilter(f)}
                    className={`px-2.5 py-1 rounded-md transition-all font-medium cursor-pointer ${
                      activeFilter === f
                        ? "bg-card text-foreground shadow-xs font-semibold"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {f === "all" ? "All" : f === "builtin" ? "Built-in" : "MCP"} (
                    {f === "all"
                      ? agentScopedTools.length
                      : agentScopedTools.filter((t) => t.source === f).length}
                    )
                  </button>
                ))}
              </div>

              <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground font-mono hidden sm:flex">
                <ShieldCheck size={13} className="text-emerald-500" />
                <span>Sandbox Scoped</span>
              </div>
            </div>

            {/* Tools List */}
            <div className="flex-1 overflow-y-auto p-5 space-y-2.5 bg-background/50">
              {loading && tools.length === 0 ? (
                <div className="flex items-center justify-center py-12 text-muted-foreground gap-2 text-xs">
                  <Loader2 size={16} className="animate-spin text-primary" />
                  Loading tools from backend registry...
                </div>
              ) : !activeAgent ? (
                <div className="text-center py-10 text-xs text-muted-foreground border border-dashed border-border rounded-lg">
                  No agent selected. Pick an agent first from the Store.
                </div>
              ) : filteredTools.length === 0 ? (
                <div className="text-center py-10 text-xs text-muted-foreground border border-dashed border-border rounded-lg space-y-2">
                  <p className="font-medium text-foreground">No tools resolved for this agent.</p>
                  <p className="text-[10px] opacity-70">
                    Backend registry returned {tools.length} tool(s);
                    agent manifest declares {activeAgent.tools?.length || 0} id(s).
                  </p>
                  <details className="text-left max-w-md mx-auto mt-3">
                    <summary className="cursor-pointer text-primary text-[11px]">Show diagnostic details</summary>
                    <div className="mt-2 p-3 bg-muted/40 rounded text-[10px] font-mono space-y-1">
                      <div><strong>Agent tools:</strong> {JSON.stringify(activeAgent.tools || [])}</div>
                      <div><strong>Registry ids:</strong> {JSON.stringify(tools.map((t) => t.id))}</div>
                    </div>
                  </details>
                </div>
              ) : (
                filteredTools.map((tool) => (
                  <div
                    key={tool.id}
                    className="p-3 rounded-lg border border-border/80 bg-card hover:border-primary/40 transition-colors flex items-start justify-between gap-3 shadow-xs"
                  >
                    <div className="flex items-start gap-2.5 min-w-0">
                      <div className="p-1.5 rounded-md bg-muted/60 border border-border/50 shrink-0 mt-0.5">
                        {pickToolIcon(tool)}
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-xs font-semibold font-mono text-foreground">
                            {tool.name}
                          </span>
                          <span
                            className={`text-[10px] px-1.5 py-0.2 rounded font-mono ${
                              tool.source === "builtin"
                                ? "bg-muted text-muted-foreground"
                                : "bg-primary/10 text-primary border border-primary/20"
                            }`}
                          >
                            {tool.source === "builtin" ? tool.category : tool.serverName || "mcp"}
                          </span>
                          {tool.is_builtin && (
                            <span className="text-[9px] font-mono uppercase text-sky-600 dark:text-sky-400">
                              built-in
                            </span>
                          )}
                        </div>
                        {tool.description && (
                          <p className="text-[11px] text-muted-foreground mt-0.5 leading-relaxed">
                            {tool.description}
                          </p>
                        )}
                      </div>
                    </div>

                    <div className="flex items-center gap-1 text-[10px] text-emerald-500 font-medium shrink-0 pt-0.5">
                      <CheckCircle2 size={12} className="text-emerald-500" />
                      <span>Active</span>
                    </div>
                  </div>
                ))
              )}
            </div>

            {/* Footer */}
            <div className="h-12 border-t border-border bg-card px-5 flex items-center justify-between shrink-0 text-xs">
              <a
                href="/stores"
                className="text-muted-foreground hover:text-primary transition-colors flex items-center gap-1.5 font-medium"
              >
                <Settings size={13} />
                <span>Manage Tools & MCP Servers</span>
                <ExternalLink size={11} className="opacity-70" />
              </a>

              <button
                type="button"
                onClick={() => setIsOpen(false)}
                className="px-3 py-1.5 rounded-md bg-secondary text-secondary-foreground hover:bg-secondary/80 font-medium transition-colors cursor-pointer text-xs"
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
