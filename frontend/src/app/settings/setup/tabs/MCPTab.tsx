// frontend\src\app\settings\setup\tabs\MCPTab.tsx
"use client";

import React, { useState, useEffect } from "react";
import {
  Share2,
  Bot,
  Server,
  CheckCircle2,
  XCircle,
  RefreshCw,
  Terminal,
  Play,
  Loader2,
  Settings,
  Trash2,
  Wrench,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";
import { MCPSettingsModal } from "@/app/stores/_modals/MCPSettingsModal";

interface MCPTabProps {
  config: any;
  setConfig: React.Dispatch<React.SetStateAction<any>>;
}

interface MCPServerInfo {
  id: string;
  name: string;
  type: string;
  status: "active" | "ready" | string;
  command?: string;
  url?: string;
  transport?: string;
  description?: string;
  is_builtin?: boolean;
  is_bundle?: boolean;
}

export function MCPTab({ config, setConfig }: MCPTabProps) {
  const [servers, setServers] = useState<MCPServerInfo[]>([]);
  const [activeTools, setActiveTools] = useState<string[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [configuringServer, setConfiguringServer] = useState<MCPServerInfo | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message: string }>>({});

  const handleDeleteServer = async (serverId: string) => {
    if (!confirm(`Are you sure you want to delete MCP server '${serverId}' and purge all its bundle files?`)) {
      return;
    }
    setDeletingId(serverId);
    try {
      const res = await fetch(getApiUrl(`/api/mcp/servers/${serverId}`), {
        method: "DELETE",
      });
      if (res.ok) {
        showToast.success("MCP Server Removed", `${serverId} purged successfully.`);
        fetchMCPServers();
      } else {
        const err = await res.json().catch(() => ({}));
        showToast.error("Delete Failed", err.detail || "Failed to delete server.");
      }
    } catch (err: any) {
      showToast.error("Network Error", err.message || "Failed to reach backend.");
    } finally {
      setDeletingId(null);
    }
  };

  const getApiUrl = (endpoint: string) => {
    if (typeof window !== "undefined" && (window.location.port === "3088" || window.location.port === "3000")) {
      return `http://localhost:8088${endpoint}`;
    }
    return endpoint;
  };

  const fetchMCPServers = async () => {
    setLoading(true);
    try {
      const res = await fetch(getApiUrl("/api/mcp"));
      if (res.ok) {
        const data = await res.json();
        setServers(data.servers || []);
        setActiveTools(data.active_tools || []);
      } else {
        showToast.error("MCP Fetch Error", "Failed to retrieve registered MCP servers.");
      }
    } catch (err: any) {
      console.error("Failed to fetch MCP servers:", err);
      showToast.error("Connection Error", err.message || "Could not connect to backend MCP registry.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMCPServers();
  }, []);

  const handleToggle = async (serverId: string) => {
    setTogglingId(serverId);
    try {
      const res = await fetch(getApiUrl(`/api/mcp/servers/${serverId}/toggle`), {
        method: "POST"
      });
      if (res.ok) {
        const data = await res.json();
        const newStatus = data.server?.status;
        setServers((prev) =>
          prev.map((s) => (s.id === serverId ? { ...s, status: newStatus } : s))
        );
        showToast.success("MCP Server Updated", `${serverId} is now ${newStatus}`);
        fetchMCPServers();
      } else {
        const err = await res.json();
        showToast.error("Toggle Failed", err.detail || "Could not toggle server status.");
      }
    } catch (err: any) {
      showToast.error("Network Error", err.message);
    } finally {
      setTogglingId(null);
    }
  };

  const handleTestConnection = async (serverId: string) => {
    setTestingId(serverId);
    try {
      const res = await fetch(getApiUrl("/api/mcp/test"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ server_id: serverId })
      });
      const data = await res.json();
      const isOk = data.status === "ok";
      setTestResults((prev) => ({
        ...prev,
        [serverId]: { ok: isOk, message: data.message }
      }));
      if (isOk) {
        showToast.success("MCP Test Succeeded", data.message);
      } else {
        showToast.warning("MCP Test Warning", data.message);
      }
    } catch (err: any) {
      setTestResults((prev) => ({
        ...prev,
        [serverId]: { ok: false, message: err.message || "Failed to reach backend." }
      }));
      showToast.error("Test Error", err.message);
    } finally {
      setTestingId(null);
    }
  };

  return (
    <div className="llm-tab w-full space-y-6 max-auto font-sans pb-6">
      {/* Header */}
      <div className="border-b border-border w-full pb-4 pt-4 bg-custom">
        <div className="flex items-center justify-between pr-4 pl-4">
          <div className="flex items-center gap-2">
            <Share2 size={16} className="text-primary" />
            <h2 className="text-sm font-semibold font-heading text-foreground uppercase tracking-wide">
              Model Context Protocol (MCP) & Multi-Agent Workflows
            </h2>
          </div>
          <button
            type="button"
            onClick={fetchMCPServers}
            disabled={loading}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-md border border-border bg-card hover:bg-rby text-black transition-colors"
          >
            <RefreshCw size={12} className={loading ? "animate-spin text-primary" : ""} />
            Refresh
          </button>
        </div>
        <p className="text-xs text-muted-foreground mt-0.5 pr-4 pl-4">
          Seamlessly manage external tool servers, standard stdio execution bridges, and automated multi-agent delegations.
        </p>
      </div>

      <div className="w-full max-w-7xl space-y-8 pt-6 m-auto px-4">
        {/* Section 1: Registered MCP Servers */}
        <div className="p-5 rounded-md border border-border bg-gigbg space-y-5 shadow-sm">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Server size={15} className="text-primary" />
              <span className="text-xs font-semibold text-foreground uppercase tracking-wider block">
                Registered MCP Tool Servers
              </span>
            </div>
            <span className="text-[11px] text-muted-foreground font-mono">
              {servers.filter((s) => s.status === "active").length} of {servers.length} active
            </span>
          </div>

          {loading ? (
            <div className="flex items-center justify-center py-8 text-muted-foreground gap-2 text-xs">
              <Loader2 size={16} className="animate-spin text-primary" />
              Loading registered MCP servers...
            </div>
          ) : servers.length === 0 ? (
            <div className="text-center py-8 text-xs text-muted-foreground border border-dashed border-border rounded-md">
              No MCP servers registered in the extensions manifest directory.
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {servers.map((server) => {
                const isActive = server.status === "active";
                const isToggling = togglingId === server.id;
                const isTesting = testingId === server.id;
                const testResult = testResults[server.id];

                return (
                  <div
                    key={server.id}
                    className={`rounded-lg border p-4 space-y-3 transition-all ${
                      isActive
                        ? "border-rb/20 bg-rb/5 text-black shadow-sm"
                        : "border-border bg-background"
                    }`}
                  >
                    {/* Card Header */}
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-semibold text-foreground">
                            {server.name}
                          </span>
                          {server.is_builtin && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-inherit font-mono">
                              builtin
                            </span>
                          )}
                          {server.is_bundle && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary border border-primary/20 font-mono">
                              bundle
                            </span>
                          )}
                        </div>
                        <p className="text-[11px] text-inherit mt-0.5 line-clamp-2">
                          {server.description || "No description provided."}
                        </p>
                      </div>

                      {/* Status Toggle Switch */}
                      <button
                        type="button"
                        onClick={() => handleToggle(server.id)}
                        disabled={isToggling}
                        className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                          isActive ? "bg-green-300" : "bg-black/50"
                        }`}
                        title={isActive ? "Click to disable" : "Click to activate"}
                      >
                        <span
                          className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-background shadow-lg ring-0 transition duration-200 ease-in-out ${
                            isActive ? "translate-x-4" : "translate-x-0"
                          }`}
                        />
                      </button>
                    </div>

                    {/* Command / Target Display */}
                    <div className="p-2 rounded bg-muted border border-border/50 text-[11px] font-mono text-black flex items-center gap-2 overflow-x-auto">
                      <Terminal size={12} className="shrink-0 text-primary" />
                      <span className="truncate select-all">
                        {server.command || server.url || "No execution target defined"}
                      </span>
                    </div>

                    {/* Test Result Notice */}
                    {testResult && (
                      <div
                        className={`p-2 rounded text-[11px] flex items-start gap-1.5 ${
                          testResult.ok
                            ? "bg-green-500/10 text-green-600 dark:text-green-400 border border-green-500/20"
                            : "bg-destructive/10 text-destructive border border-destructive/20"
                        }`}
                      >
                        {testResult.ok ? (
                          <CheckCircle2 size={13} className="shrink-0 mt-0.5" />
                        ) : (
                          <XCircle size={13} className="shrink-0 mt-0.5" />
                        )}
                        <span className="leading-tight">{testResult.message}</span>
                      </div>
                    )}

                    {/* Card Actions Footer */}
                    <div className="flex items-center justify-between pt-1 border-t border-border/40">
                      <div className="flex items-center gap-1.5 text-[10px] text-card">
                        <span
                          className={`h-1.5 w-1.5 rounded-full ${
                            isActive ? "bg-green-500 animate-pulse" : "bg-muted-foreground"
                          }`}
                        />
                        <span className="capitalize text-rby">{server.status}</span>
                      </div>

                      <div className="flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => setConfiguringServer(server)}
                          className="flex items-center gap-1 text-[11px] px-2.5 py-1 rounded border border-border bg-card hover:bg-muted text-foreground transition font-medium cursor-pointer"
                          title="Configure Server Settings & Environment"
                        >
                          <Settings size={11} className="text-muted-foreground" />
                          <span>Settings</span>
                        </button>

                        <button
                          type="button"
                          onClick={() => handleTestConnection(server.id)}
                          disabled={isTesting}
                          className="flex items-center gap-1 text-[11px] px-2.5 py-1 rounded bg-secondary hover:bg-secondary/80 text-secondary-foreground font-medium transition-colors cursor-pointer"
                        >
                          {isTesting ? (
                            <Loader2 size={11} className="animate-spin text-primary" />
                          ) : (
                            <Play size={10} className="text-primary fill-primary" />
                          )}
                          Test Connection
                        </button>

                        {!server.is_builtin && (
                          <button
                            type="button"
                            onClick={() => handleDeleteServer(server.id)}
                            disabled={deletingId === server.id}
                            className="p-1 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition cursor-pointer disabled:opacity-50"
                            title="Delete Server / Purge Bundle"
                            aria-label="Delete Server"
                          >
                            {deletingId === server.id ? (
                              <Loader2 size={12} className="animate-spin text-destructive" />
                            ) : (
                              <Trash2 size={12} />
                            )}
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* Active Tools Summary */}
          {activeTools.length > 0 && (
            <div className="pt-3 border-t border-border">
              <span className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider block mb-2">
                Active Protocol Tools Available to Agents:
              </span>
              <div className="flex flex-wrap gap-1.5">
                {activeTools.map((tool) => (
                  <span
                    key={tool}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-mono bg-rby/20 text-black border border-rby"
                  >
                    <Wrench size={10} className="text-primary" />
                    {tool}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Section 2: Agent Delegation [runflow] */}
        <div className="p-5 rounded-md border border-border bg-card space-y-4 shadow-sm text-black">
          <span className="text-xs font-semibold text-inherit uppercase tracking-wider block">
            Agent Delegation [runflow]
          </span>

          <label className="flex items-center gap-2.5 cursor-pointer">
            <input
              type="checkbox"
              checked={Boolean(config?.runflow?.use_data_analysis_agent)}
              onChange={(e) =>
                setConfig((prev: any) => ({
                  ...prev,
                  runflow: { ...(prev?.runflow || {}), use_data_analysis_agent: e.target.checked }
                }))
              }
              className="rounded border-border text-primary focus:ring-primary h-4 w-4"
            />
            <div className="flex items-center gap-1.5">
              <Bot size={14} className="text-primary" />
              <span className="text-xs text-foreground font-medium">
                Use Data Analysis Specialist Agent
              </span>
            </div>
          </label>
        </div>

        {/* Section 3: Model Context Protocol [mcp] */}
        <div className="p-5 rounded-md border border-border bg-card space-y-4 shadow-sm">
          <span className="text-xs font-semibold uppercase tracking-wider block text-inherit">
            Core Reference Module [mcp]
          </span>

          <div>
            <label className="text-[11px] font-medium text-muted-foreground block mb-1">
              MCP Server Reference Module
            </label>
            <input
              type="text"
              value={config?.mcp?.server_reference || ""}
              onChange={(e) =>
                setConfig((prev: any) => ({
                  ...prev,
                  mcp: { ...(prev?.mcp || {}), server_reference: e.target.value }
                }))
              }
              placeholder="app.mcp.server"
              className="w-full bg-background border border-border rounded-md px-3 py-1.5 text-xs text-foreground shadow-peldrun-xs font-mono"
            />
            <span className="text-[10px] text-muted-foreground mt-1.5 block">
              Points to the backend Python module implementing native MCP server tools for external integrations.
            </span>
          </div>
        </div>
      </div>

      {/* Standalone MCP Server Settings Modal */}
      <MCPSettingsModal
        open={configuringServer !== null}
        serverId={configuringServer?.id ?? null}
        serverName={configuringServer?.name ?? ""}
        onClose={() => setConfiguringServer(null)}
        onSaved={fetchMCPServers}
      />
    </div>
  );
}
