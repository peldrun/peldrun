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
  Bot,
  ShieldCheck
} from "lucide-react";
import { useChatStore } from "@/stores/chat-store";
import { fetchStoreAgents } from "@/lib/chatsApi";
import { AgentManifest } from "@/lib/types";

export interface ToolItem {
  id: string;
  name: string;
  description: string;
  source: "builtin" | "mcp";
  serverName?: string;
}

const DEFAULT_BUILTIN_TOOLS: ToolItem[] = [
  {
    id: "python_execute",
    name: "Python Execute",
    description: "Executes Python code in a sandboxed runtime for data analytics, scripting, and figure generation.",
    source: "builtin"
  },
  {
    id: "bash",
    name: "Terminal (Bash)",
    description: "Runs system commands, file transformations, and headless operations in the workspace directory.",
    source: "builtin"
  },
  {
    id: "str_replace_editor",
    name: "File Editor",
    description: "Precise file viewing, creation, and surgical string replacement directly inside workspace files.",
    source: "builtin"
  },
  {
    id: "web_search",
    name: "Web Search",
    description: "Searches Google, DuckDuckGo, and Bing for up-to-date live intelligence and documentation.",
    source: "builtin"
  },
  {
    id: "chrome_browser",
    name: "Browser Automation",
    description: "Controls a live headless/interactive Chrome browser instance for web automation and DOM inspection.",
    source: "builtin"
  },
  {
    id: "ask_human",
    name: "Ask Human",
    description: "Requests interactive feedback and authorization from the user without blocking backend loops.",
    source: "builtin"
  },
  {
    id: "terminate",
    name: "Task Terminator",
    description: "Concludes autonomous agent execution cleanly once deliverables are built and verified.",
    source: "builtin"
  }
];

export function ActiveToolsModal() {
  const { selectedAgentId } = useChatStore();
  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [mcpTools, setMcpTools] = useState<ToolItem[]>([]);
  const [activeFilter, setActiveFilter] = useState<"all" | "builtin" | "mcp">("all");

  const getApiUrl = (endpoint: string) => {
    if (typeof window !== "undefined" && (window.location.port === "3088" || window.location.port === "3000")) {
      return `http://localhost:8088${endpoint}`;
    }
    return endpoint;
  };

  // Load and cache all agent manifests
  useEffect(() => {
    let isMounted = true;
    fetchStoreAgents()
      .then((data) => {
        if (isMounted && data.length > 0) {
          setAgents(data);
        }
      })
      .catch(console.error);

    return () => {
      isMounted = false;
    };
  }, []);

  const fetchActiveMcpTools = async () => {
    setLoading(true);
    try {
      const res = await fetch(getApiUrl("/api/mcp"));
      if (res.ok) {
        const data = await res.json();
        const activeServers = (data.servers || []).filter((s: any) => s.status === "active");

        const tools: ToolItem[] = [];
        activeServers.forEach((server: any) => {
          const sid = server.id || "";
          if (sid.includes("filesystem")) {
            tools.push(
              { id: "read_file", name: "MCP: Read File", description: "Read content from workspace files via MCP.", source: "mcp", serverName: server.name },
              { id: "write_file", name: "MCP: Write File", description: "Write and update files via MCP filesystem transport.", source: "mcp", serverName: server.name },
              { id: "list_directory", name: "MCP: List Directory", description: "Explore folder hierarchies and directory entries.", source: "mcp", serverName: server.name }
            );
          } else if (sid.includes("github")) {
            tools.push(
              { id: "get_issue", name: "MCP: GitHub Issue", description: "Fetch and manage GitHub repository issues.", source: "mcp", serverName: server.name },
              { id: "search_repositories", name: "MCP: Search Repos", description: "Search public and private GitHub repositories.", source: "mcp", serverName: server.name }
            );
          } else {
            tools.push({
              id: sid,
              name: `MCP: ${server.name}`,
              description: server.description || "Active external Model Context Protocol server tools.",
              source: "mcp",
              serverName: server.name
            });
          }
        });
        setMcpTools(tools);
      }
    } catch (err) {
      console.error("Failed to fetch active MCP tools", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchActiveMcpTools();
  }, []);

  // Determine active agent reactively based on selectedAgentId
  const activeAgent = useMemo(() => {
    return (
      agents.find((a) => a.id === selectedAgentId) || {
        id: "peldrun",
        name: "peldrun Generalist",
        role: "General Autonomous Specialist",
        tools: ["python_execute", "bash", "str_replace_editor", "web_search", "chrome_browser", "mcp"]
      }
    );
  }, [agents, selectedAgentId]);

  // Compute allowed tools specifically scoped to the active agent
  const agentScopedTools = useMemo(() => {
    const rawAllowed = (activeAgent.tools || []).map((t: string) => t.toLowerCase());
    const allowedSet = new Set(rawAllowed);
    const hasMcpPermission = rawAllowed.some((t: string) => t.includes("mcp") || t.includes("browser"));

    // Filter built-in tools
    const matchedBuiltins = DEFAULT_BUILTIN_TOOLS.filter((t) => {
      if (t.id === "ask_human" || t.id === "terminate") return true;
      if (allowedSet.has(t.id.toLowerCase())) return true;
      if (t.id === "python_execute" && (allowedSet.has("python") || allowedSet.has("py"))) return true;
      if (t.id === "bash" && (allowedSet.has("terminal") || allowedSet.has("shell"))) return true;
      if (t.id === "str_replace_editor" && allowedSet.has("editor")) return true;
      if (t.id === "web_search" && allowedSet.has("search")) return true;
      if (t.id === "chrome_browser" && allowedSet.has("browser")) return true;
      return false;
    });

    // Filter MCP tools based on active agent's manifest permissions
    const matchedMcp = hasMcpPermission
      ? mcpTools.filter((t) => {
          if (allowedSet.has("mcp") || allowedSet.has("browser")) return true;
          if (t.serverName && allowedSet.has(t.serverName.toLowerCase())) return true;
          if (allowedSet.has(t.id.toLowerCase())) return true;
          return false;
        })
      : [];

    return [...matchedBuiltins, ...matchedMcp];
  }, [activeAgent, mcpTools]);

  const filteredTools = useMemo(() => {
    if (activeFilter === "builtin") return agentScopedTools.filter((t) => t.source === "builtin");
    if (activeFilter === "mcp") return agentScopedTools.filter((t) => t.source === "mcp");
    return agentScopedTools;
  }, [agentScopedTools, activeFilter]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isOpen) {
        setIsOpen(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  const getToolIcon = (tool: ToolItem) => {
    if (tool.source === "mcp") return <Server size={14} className="text-primary shrink-0" />;
    if (tool.id.includes("python")) return <Code2 size={14} className="text-amber-500 shrink-0" />;
    if (tool.id.includes("bash")) return <Terminal size={14} className="text-emerald-500 shrink-0" />;
    if (tool.id.includes("search")) return <Globe size={14} className="text-sky-500 shrink-0" />;
    if (tool.id.includes("editor")) return <FileCode2 size={14} className="text-purple-500 shrink-0" />;
    return <Wrench size={14} className="text-muted-foreground shrink-0" />;
  };

  return (
    <>
      {/* Dynamic Trigger Badge: Shows active agent tool count in real-time */}
      <button
        type="button"
        onClick={() => setIsOpen(true)}
        className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium border border-border bg-card hover:bg-muted/70 text-foreground transition-all cursor-pointer shadow-xs"
        title={`View active tools for ${activeAgent.name}`}
      >
        <Wrench size={12} className="text-primary" />
        <span className="font-medium hidden sm:inline">Active Tools</span>
        <span className="px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-primary/10 text-primary border border-primary/20">
          {agentScopedTools.length}
        </span>
      </button>

      {/* Reactive Modal Dialog */}
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
            {/* Modal Header */}
            <div className="h-14 border-b border-border bg-muted/30 px-5 flex items-center justify-between shrink-0">
              <div className="flex items-center gap-2.5 min-w-0">
                <div className="p-2 rounded-lg bg-primary/10 text-primary border border-primary/20 shrink-0">
                  <Wrench size={16} />
                </div>
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold font-heading text-foreground flex items-center gap-2">
                    <span className="truncate">{activeAgent.name}</span>
                    <span className="px-2 py-0.5 rounded text-[10px] bg-primary/15 text-primary border border-primary/30 font-mono shrink-0">
                      {agentScopedTools.length} Tools
                    </span>
                  </h3>
                  <p className="text-[11px] text-muted-foreground truncate">
                    Active execution tools permitted by this persona manifest.
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-1.5 shrink-0">
                <button
                  type="button"
                  onClick={fetchActiveMcpTools}
                  disabled={loading}
                  className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
                  title="Refresh active tool list"
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

            {/* Filter Tabs Bar */}
            <div className="px-5 py-2.5 bg-card border-b border-border/60 flex items-center justify-between text-xs shrink-0">
              <div className="flex items-center gap-1 bg-muted/60 p-0.5 rounded-lg border border-border/60 text-[11px]">
                <button
                  type="button"
                  onClick={() => setActiveFilter("all")}
                  className={`px-2.5 py-1 rounded-md transition-all font-medium cursor-pointer ${
                    activeFilter === "all" ? "bg-card text-foreground shadow-xs font-semibold" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  All ({agentScopedTools.length})
                </button>
                <button
                  type="button"
                  onClick={() => setActiveFilter("builtin")}
                  className={`px-2.5 py-1 rounded-md transition-all font-medium cursor-pointer ${
                    activeFilter === "builtin" ? "bg-card text-foreground shadow-xs font-semibold" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Built-in ({agentScopedTools.filter((t) => t.source === "builtin").length})
                </button>
                <button
                  type="button"
                  onClick={() => setActiveFilter("mcp")}
                  className={`px-2.5 py-1 rounded-md transition-all font-medium cursor-pointer ${
                    activeFilter === "mcp" ? "bg-card text-foreground shadow-xs font-semibold" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  MCP Protocol ({agentScopedTools.filter((t) => t.source === "mcp").length})
                </button>
              </div>

              <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground font-mono hidden sm:flex">
                <ShieldCheck size={13} className="text-emerald-500" />
                <span>Sandbox Scoped</span>
              </div>
            </div>

            {/* Tools List */}
            <div className="flex-1 overflow-y-auto p-5 space-y-2.5 bg-background/50">
              {loading && mcpTools.length === 0 ? (
                <div className="flex items-center justify-center py-12 text-muted-foreground gap-2 text-xs">
                  <Loader2 size={16} className="animate-spin text-primary" />
                  Synchronizing active persona tools...
                </div>
              ) : filteredTools.length === 0 ? (
                <div className="text-center py-10 text-xs text-muted-foreground border border-dashed border-border rounded-lg">
                  No active tools permitted for this category under the current persona.
                </div>
              ) : (
                filteredTools.map((tool) => (
                  <div
                    key={tool.id}
                    className="p-3 rounded-lg border border-border/80 bg-card hover:border-primary/40 transition-colors flex items-start justify-between gap-3 shadow-xs"
                  >
                    <div className="flex items-start gap-2.5 min-w-0">
                      <div className="p-1.5 rounded-md bg-muted/60 border border-border/50 shrink-0 mt-0.5">
                        {getToolIcon(tool)}
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
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
                            {tool.source === "builtin" ? "builtin" : tool.serverName || "mcp"}
                          </span>
                        </div>
                        <p className="text-[11px] text-muted-foreground mt-0.5 leading-relaxed">
                          {tool.description}
                        </p>
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

            {/* Modal Footer */}
            <div className="h-12 border-t border-border bg-card px-5 flex items-center justify-between shrink-0 text-xs">
              <a
                href="/settings"
                className="text-muted-foreground hover:text-primary transition-colors flex items-center gap-1.5 font-medium"
              >
                <Settings size={13} />
                <span>Manage MCP Servers</span>
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
