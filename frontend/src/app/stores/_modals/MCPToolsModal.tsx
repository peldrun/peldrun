// frontend\src\app\stores\_modals\MCPToolsModal.tsx
"use client";

/**
 * Per-server MCP Tool Management Modal.
 *
 * Renders the cached tool list for a single MCP server (no subprocess
 * is spawned just to open this modal) and lets the user toggle each
 * tool's `user_enabled` flag. Disabled tools are excluded from the
 * LLM's AgentSpec on the next run, which cuts tool-definition tokens
 * substantially on large servers (GitHub: 50+ tools).
 *
 * Actions:
 *   - Toggle individual tool    →  PATCH /tools/{name}
 *   - Enable All / Disable All  →  PATCH /tools
 *   - Force re-discovery        →  POST /tools/refresh
 *
 * The Refresh button is the ONLY path that spawns the MCP server.
 * All toggle operations are pure file writes and complete in <50ms.
 *
 * Optimistic updates: the UI flips the toggle immediately, then
 * reconciles with the server response. On error, the UI reverts the
 * affected row and shows a toast.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  X,
  Search,
  Loader2,
  Wrench,
  RefreshCw,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Sparkles,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";

// ---------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------

interface CachedTool {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  // MCP 2025-11-25 §7.2: tools may declare an output schema describing
  // the shape of their `structuredContent` result. Optional — most
  // servers (GitHub, Filesystem, WordPress) do not emit one.
  output_schema?: Record<string, unknown>;
  user_enabled: boolean;
  first_seen_at: number;
  last_seen_at: number;
}

interface CachedToolsResponse {
  status: string;
  server_id: string;
  cached: boolean;
  fetched_at: number | null;
  tools: CachedTool[];
  total: number;
  enabled: number;
}

export interface MCPToolsModalProps {
  open: boolean;
  serverId: string | null;
  serverName: string;
  onClose: () => void;
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function formatRelativeTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  const ms = Date.now() - ts * 1000;
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

function isRecentlyAdded(first_seen_at: number): boolean {
  return Date.now() - first_seen_at * 1000 < 3_600_000;
}

// ---------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------

export function MCPToolsModal({
  open,
  serverId,
  serverName,
  onClose,
}: MCPToolsModalProps) {
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<CachedToolsResponse | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  // Per-tool in-flight tracking so we can disable the toggle while saving
  const pendingTools = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);

  const fetchTools = useCallback(async (sid: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/mcp/servers/${encodeURIComponent(sid)}/tools/cached`
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const payload: CachedToolsResponse = await res.json();
      setData(payload);
    } catch (err: any) {
      setError(err?.message || "Failed to load cached tools.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Reset state and load when modal opens with a new server
  useEffect(() => {
    if (!open || !serverId) return;
    setData(null);
    setError(null);
    setSearchQuery("");
    setCollapsed({});
    fetchTools(serverId);
  }, [open, serverId, fetchTools]);

  // ESC to close (only when not busy)
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !refreshing && !bulkBusy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, refreshing, bulkBusy, onClose]);

  const handleToggleTool = useCallback(
    async (toolName: string, enabled: boolean) => {
      if (!serverId || !data) return;

      // Optimistic update
      setData((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          tools: prev.tools.map((t) =>
            t.name === toolName ? { ...t, user_enabled: enabled } : t
          ),
          enabled: prev.tools.reduce(
            (sum, t) =>
              sum + ((t.name === toolName ? enabled : t.user_enabled) ? 1 : 0),
            0
          ),
        };
      });

      pendingTools.current.add(toolName);
      forceRender((n) => n + 1);

      try {
        const res = await fetch(
          `/api/mcp/servers/${encodeURIComponent(serverId)}/tools/${encodeURIComponent(toolName)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled }),
          }
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
      } catch (err: any) {
        // Revert on failure
        setData((prev) => {
          if (!prev) return prev;
          return {
            ...prev,
            tools: prev.tools.map((t) =>
              t.name === toolName ? { ...t, user_enabled: !enabled } : t
            ),
            enabled: prev.tools.reduce(
              (sum, t) =>
                sum +
                ((t.name === toolName ? !enabled : t.user_enabled) ? 1 : 0),
              0
            ),
          };
        });
        showToast.error(
          "Toggle Failed",
          err?.message || `Could not ${enabled ? "enable" : "disable"} '${toolName}'.`
        );
      } finally {
        pendingTools.current.delete(toolName);
        forceRender((n) => n + 1);
      }
    },
    [serverId, data]
  );

  const handleBulkToggle = useCallback(
    async (enabled: boolean) => {
      if (!serverId || !data) return;
      setBulkBusy(true);
      try {
        const res = await fetch(
          `/api/mcp/servers/${encodeURIComponent(serverId)}/tools`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled }),
          }
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        // Update local view
        setData((prev) => {
          if (!prev) return prev;
          return {
            ...prev,
            tools: prev.tools.map((t) => ({ ...t, user_enabled: enabled })),
            enabled: enabled ? prev.tools.length : 0,
          };
        });
        showToast.success(
          enabled ? "All Tools Enabled" : "All Tools Disabled",
          `${data.tools.length} tool(s) updated.`
        );
      } catch (err: any) {
        showToast.error(
          "Bulk Toggle Failed",
          err?.message || "Could not update tools."
        );
      } finally {
        setBulkBusy(false);
      }
    },
    [serverId, data]
  );

  const handleRefresh = useCallback(async () => {
    if (!serverId) return;
    setRefreshing(true);
    try {
      const res = await fetch(
        `/api/mcp/servers/${encodeURIComponent(serverId)}/tools/refresh`,
        { method: "POST" }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const payload = await res.json();
      // Reload to get the freshly-merged cache (with first_seen_at etc.)
      await fetchTools(serverId);
      showToast.success(
        "Tools Refreshed",
        `${payload.total} tool(s) discovered (${payload.enabled} enabled).`
      );
    } catch (err: any) {
      const msg = String(err?.message || "");
      // Detect the specific timeout pattern so we can show a
      // helpful hint instead of a raw error. The backend returns
      // a 504 with a structured message; we look for both signals.
      if (msg.includes("timed out") || msg.includes("timeout")) {
        showToast.warning(
          "Tool Discovery Timed Out",
          "The MCP server did not respond in time. If this is the first run, npm may still be downloading the package. Try again in a minute, or increase the startup timeout in Setup → MCP."
        );
      } else {
        showToast.error(
          "Refresh Failed",
          err?.message || "Could not refresh the tool list."
        );
      }
    } finally {
      setRefreshing(false);
    }
  }, [serverId, fetchTools]);

  // Filtered tools (memoized)
  const filteredTools = useMemo(() => {
    if (!data) return [];
    const q = searchQuery.toLowerCase().trim();
    if (!q) return data.tools;
    return data.tools.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        (t.description || "").toLowerCase().includes(q)
    );
  }, [data, searchQuery]);

  const isBusy = refreshing || bulkBusy;

  if (!open || !serverId) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="mcp-tools-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !isBusy) onClose();
      }}
    >
      <div className="bg-card rounded-md shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col border border-border">
        {/* ─── Header ─────────────────────────────────────────── */}
        <div className="px-5 py-3.5 border-b border-border flex items-center justify-between shrink-0 bg-muted/40">
          <div className="min-w-0 flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary shrink-0">
              <Wrench size={16} />
            </div>
            <div className="min-w-0">
              <h2
                id="mcp-tools-title"
                className="text-sm font-bold text-foreground"
              >
                Manage Tools
              </h2>
              <p className="text-xs text-muted-foreground truncate">
                {serverName}{" "}
                <span className="font-mono text-[10px] opacity-70">
                  ({serverId})
                </span>
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isBusy}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-40 disabled:cursor-not-allowed transition"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* ─── Toolbar ────────────────────────────────────────── */}
        <div className="px-5 py-3 border-b border-border flex flex-wrap items-center gap-2 shrink-0">
          <div className="relative flex-1 min-w-[200px]">
            <Search
              size={13}
              className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none"
            />
            <input
              type="text"
              placeholder="Search tools by name or description…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              disabled={!data || data.tools.length === 0}
              className="w-full pl-8 pr-3 py-1.5 bg-background border border-border rounded-md text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary disabled:opacity-50"
            />
          </div>

          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => handleBulkToggle(true)}
              disabled={!data || data.tools.length === 0 || bulkBusy}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 text-[11px] rounded-md border border-border bg-card hover:bg-muted text-foreground transition disabled:opacity-50 font-medium"
              title="Enable every tool"
            >
              <CheckCircle2 size={12} className="text-emerald-500" />
              Enable All
            </button>
            <button
              type="button"
              onClick={() => handleBulkToggle(false)}
              disabled={!data || data.tools.length === 0 || bulkBusy}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 text-[11px] rounded-md border border-border bg-card hover:bg-muted text-foreground transition disabled:opacity-50 font-medium"
              title="Disable every tool"
            >
              <XCircle size={12} className="text-destructive" />
              Disable All
            </button>
            <button
              type="button"
              onClick={handleRefresh}
              disabled={refreshing}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 text-[11px] rounded-md bg-primary text-primary-foreground hover:opacity-90 transition disabled:opacity-50 font-medium"
              title="Re-discover tools from the server"
            >
              {refreshing ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <RefreshCw size={12} />
              )}
              Refresh
            </button>
          </div>
        </div>

        {/* ─── Body ───────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto scrollbar-thin px-5 py-4">
          {loading && (
            <div className="flex items-center justify-center py-16 text-muted-foreground gap-2 text-sm">
              <Loader2 size={18} className="animate-spin text-primary" />
              Loading cached tools…
            </div>
          )}

          {!loading && error && (
            <div className="flex items-start gap-3 p-3 bg-destructive/10 border border-destructive/30 rounded-sm">
              <AlertTriangle
                size={16}
                className="text-destructive shrink-0 mt-0.5"
              />
              <div className="text-xs text-foreground">{error}</div>
            </div>
          )}

          {!loading && !error && data && !data.cached && (
            <div className="py-12 text-center space-y-3">
              <Wrench
                size={32}
                className="mx-auto text-muted-foreground/60"
                aria-hidden="true"
              />
              <div>
                <p className="text-sm font-semibold text-foreground">
                  No cached tools yet
                </p>
                <p className="text-xs text-muted-foreground mt-1 max-w-md mx-auto leading-relaxed">
                  This server's tool list has not been discovered yet.
                  Click <strong>Refresh</strong> to contact the server and
                  cache the result. This will spawn a short-lived MCP
                  process.
                </p>
              </div>
            </div>
          )}

          {!loading && !error && data && data.cached && data.tools.length === 0 && (
            <div className="py-12 text-center text-sm text-muted-foreground">
              The server returned an empty tool list.
            </div>
          )}

          {!loading && !error && data && data.tools.length > 0 && (
            <>
              {/* Stats bar */}
              <div className="mb-3 flex items-center justify-between text-[11px] text-muted-foreground">
                <div className="flex items-center gap-3">
                  <span>
                    <strong className="text-foreground">{data.enabled}</strong>{" "}
                    enabled
                  </span>
                  <span className="opacity-40">•</span>
                  <span>
                    <strong className="text-foreground">
                      {data.tools.length - data.enabled}
                    </strong>{" "}
                    disabled
                  </span>
                  <span className="opacity-40">•</span>
                  <span>
                    Total:{" "}
                    <strong className="text-foreground">
                      {data.tools.length}
                    </strong>
                  </span>
                </div>
                <span>
                  Cached {formatRelativeTime(data.fetched_at)}
                </span>
              </div>

              {/* Tool list */}
              <div className="space-y-1.5">
                {filteredTools.length === 0 && (
                  <div className="py-8 text-center text-xs text-muted-foreground">
                    No tools match "{searchQuery}".
                  </div>
                )}
                {filteredTools.map((tool) => {
                  const isPending = pendingTools.current.has(tool.name);
                  const isCollapsed = collapsed[tool.name] !== false;
                  const recentlyAdded = isRecentlyAdded(tool.first_seen_at);

                  return (
                    <div
                      key={tool.name}
                      className={`rounded-md border transition-colors ${
                        tool.user_enabled
                          ? "border-primary/20 bg-primary/5"
                          : "border-border bg-card"
                      }`}
                    >
                      <div className="flex items-start gap-3 p-3">
                        {/* Toggle */}
                        <button
                          type="button"
                          onClick={() =>
                            handleToggleTool(tool.name, !tool.user_enabled)
                          }
                          disabled={isPending}
                          className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none disabled:cursor-wait mt-0.5 ${
                            tool.user_enabled
                              ? "bg-green-500"
                              : "bg-muted-foreground/40"
                          }`}
                          aria-label={`${tool.user_enabled ? "Disable" : "Enable"} ${tool.name}`}
                        >
                          <span
                            className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-md ring-0 transition duration-200 ease-in-out ${
                              tool.user_enabled
                                ? "translate-x-4"
                                : "translate-x-0"
                            }`}
                          />
                        </button>

                        {/* Info */}
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <code className="text-xs font-mono font-semibold text-foreground break-all">
                              {tool.name}
                            </code>
                            {recentlyAdded && (
                              <span className="inline-flex items-center gap-0.5 text-[9px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-700 dark:text-amber-400 border border-amber-500/20 font-mono">
                                <Sparkles size={9} />
                                new
                              </span>
                            )}
                            {isPending && (
                              <Loader2
                                size={11}
                                className="animate-spin text-muted-foreground"
                              />
                            )}
                          </div>
                          {tool.description && (
                            <p className="text-[11px] text-muted-foreground mt-1 leading-relaxed">
                              {tool.description}
                            </p>
                          )}
                          {/* The "Show schema" toggle appears when EITHER
                              an input schema OR an output schema is
                              present. Previously it appeared only for
                              input, which hid the new output schema on
                              tools that declare one but no input
                              parameters — a rare but real MCP 2025+
                              pattern. */}
                          {((tool.parameters &&
                            typeof tool.parameters === "object" &&
                            Object.keys(tool.parameters as object).length >
                              0) ||
                            (tool.output_schema &&
                              typeof tool.output_schema === "object" &&
                              Object.keys(tool.output_schema as object)
                                .length > 0)) && (
                            <button
                              type="button"
                              onClick={() =>
                                setCollapsed((prev) => ({
                                  ...prev,
                                  [tool.name]: !isCollapsed,
                                }))
                              }
                              className="mt-1 text-[10px] text-muted-foreground hover:text-foreground inline-flex items-center gap-0.5"
                            >
                              {isCollapsed ? (
                                <>
                                  <ChevronDown size={10} />
                                  Show schema
                                </>
                              ) : (
                                <>
                                  <ChevronUp size={10} />
                                  Hide schema
                                </>
                              )}
                            </button>
                          )}
                          {!isCollapsed && (
                            <div className="mt-1.5 space-y-2">
                              {tool.parameters &&
                                typeof tool.parameters === "object" &&
                                Object.keys(tool.parameters as object)
                                  .length > 0 && (
                                  <div>
                                    <div className="text-[9px] uppercase tracking-wider font-bold text-muted-foreground mb-1">
                                      Input Schema
                                    </div>
                                    <pre className="p-2 rounded bg-background border border-border text-[10px] font-mono text-foreground overflow-x-auto max-h-40">
                                      {JSON.stringify(tool.parameters, null, 2)}
                                    </pre>
                                  </div>
                                )}
                              {tool.output_schema &&
                                typeof tool.output_schema === "object" &&
                                Object.keys(tool.output_schema as object)
                                  .length > 0 && (
                                  <div>
                                    <div className="text-[9px] uppercase tracking-wider font-bold text-muted-foreground mb-1">
                                      Output Schema
                                    </div>
                                    <pre className="p-2 rounded bg-background border border-border text-[10px] font-mono text-foreground overflow-x-auto max-h-40">
                                      {JSON.stringify(tool.output_schema, null, 2)}
                                    </pre>
                                  </div>
                                )}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>

        {/* ─── Footer ─────────────────────────────────────────── */}
        <div className="px-5 py-3 border-t border-border flex items-center justify-between gap-2 shrink-0 bg-muted/20">
          <p className="text-[10px] text-muted-foreground">
            Disabled tools are not sent to the LLM. Changes apply to the
            next run.
          </p>
          <button
            type="button"
            onClick={onClose}
            disabled={isBusy}
            className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-50"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}

export default MCPToolsModal;
