// frontend/src/app/stores/_modals/AgentMcpModal.tsx
"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  Cpu,
  Loader2,
  Save,
  Server,
  X,
  XCircle,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "../_hooks/useStoreData";

export interface AgentMcpServerInfo {
  id: string;
  name: string;
  description: string;
  transport: string;
  status: string;
  is_builtin: boolean;
  is_bundle: boolean;
  in_manifest: boolean;
}

export interface AgentMcpState {
  manifest_mcp_servers: string[];
  disabled_mcp_servers: string[];
  extra_mcp_servers: string[];
  effective_mcp_servers: string[];
  available_mcp_servers: AgentMcpServerInfo[];
}

export interface AgentMcpModalProps {
  open: boolean;
  agentId: string | null;
  agentName: string;
  onClose: () => void;
  onSaved?: () => void;
}

export function AgentMcpModal({
  open,
  agentId,
  agentName,
  onClose,
  onSaved,
}: AgentMcpModalProps) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [manifestServers, setManifestServers] = useState<string[]>([]);
  const [availableServers, setAvailableServers] = useState<AgentMcpServerInfo[]>([]);
  const [disabled, setDisabled] = useState<Set<string>>(new Set());
  const [extra, setExtra] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!open || !agentId) return;
    let cancelled = false;

    setLoading(true);
    setError(null);
    setManifestServers([]);
    setAvailableServers([]);
    setDisabled(new Set());
    setExtra(new Set());

    (async () => {
      try {
        const res = await fetch(getApiUrl(`/api/store/agents/${agentId}/mcp`));
        if (cancelled) return;

        if (res.status === 404) {
          setError(`Agent '${agentId}' not found.`);
          return;
        }
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setError(body.detail || `Failed to load MCP server state (HTTP ${res.status}).`);
          return;
        }

        const data: AgentMcpState = await res.json();
        setManifestServers(data.manifest_mcp_servers || []);
        setAvailableServers(data.available_mcp_servers || []);
        setDisabled(new Set(data.disabled_mcp_servers || []));
        setExtra(new Set(data.extra_mcp_servers || []));
      } catch (exc: any) {
        if (cancelled) return;
        setError(exc?.message || "Network error while loading MCP servers.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, agentId]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, saving, onClose]);

  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  const toggleManifestServer = useCallback((serverId: string) => {
    setDisabled((prev) => {
      const next = new Set(prev);
      if (next.has(serverId)) next.delete(serverId);
      else next.add(serverId);
      return next;
    });
  }, []);

  const toggleExtraServer = useCallback((serverId: string) => {
    setExtra((prev) => {
      const next = new Set(prev);
      if (next.has(serverId)) next.delete(serverId);
      else next.add(serverId);
      return next;
    });
  }, []);

  const manifestRows = useMemo(() => {
    const byId = new Map(availableServers.map((s) => [s.id, s]));
    return manifestServers.map((id) => {
      const meta = byId.get(id);
      return {
        id,
        name: meta?.name ?? id,
        description: meta?.description ?? "",
        transport: meta?.transport ?? "stdio",
        status: meta?.status ?? "ready",
        is_builtin: meta?.is_builtin ?? false,
        is_bundle: meta?.is_bundle ?? false,
        availableInStore: Boolean(meta),
      };
    });
  }, [manifestServers, availableServers]);

  const extraCandidates = useMemo(
    () => availableServers.filter((s) => !s.in_manifest),
    [availableServers]
  );

  const effectiveServers = useMemo(() => {
    const out: string[] = [];
    for (const id of manifestServers) {
      if (!disabled.has(id)) out.push(id);
    }
    for (const id of extra) {
      if (!out.includes(id)) out.push(id);
    }
    return out;
  }, [manifestServers, disabled, extra]);

  const handleSave = useCallback(async () => {
    if (!agentId) return;
    setSaving(true);
    setError(null);

    try {
      const res = await fetch(getApiUrl(`/api/store/agents/${agentId}/mcp`), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          disabled_mcp_servers: Array.from(disabled),
          extra_mcp_servers: Array.from(extra),
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail =
          typeof body.detail === "string" ? body.detail : "Failed to save MCP server selection.";
        setError(detail);
        showToast.error("Save Rejected", detail);
        return;
      }

      const data = await res.json();
      setManifestServers(data.manifest_mcp_servers || []);
      setDisabled(new Set(data.disabled_mcp_servers || []));
      setExtra(new Set(data.extra_mcp_servers || []));

      showToast.success(
        "Agent MCP Servers Saved",
        `${agentName} updated — ${data.effective_mcp_servers?.length ?? 0} server(s) active.`
      );
      onSaved?.();
      onClose();
    } catch (exc: any) {
      const msg = exc?.message || "Network error while saving.";
      setError(msg);
      showToast.error("Save Failed", msg);
    } finally {
      setSaving(false);
    }
  }, [agentId, disabled, extra, agentName, onSaved, onClose]);

  if (!open) return null;

  const canSave = !saving && !loading && !error;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      onClick={(e) => {
        if (e.target === e.currentTarget && !saving) onClose();
      }}
    >
      <div className="bg-card rounded-md shadow-2xl w-full max-w-3xl max-h-[92vh] flex flex-col border border-border">
        {/* Header */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0 bg-muted/40">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
              <Server size={16} />
            </div>
            <div className="min-w-0">
              <h2 className="text-sm font-bold text-foreground uppercase tracking-wide">
                Agent MCP Servers
              </h2>
              <p className="text-xs text-muted-foreground truncate">{agentName}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-50 transition"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 scrollbar-thin space-y-5">
          {loading && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 size={20} className="animate-spin mr-2" />
              <span className="text-sm">Loading MCP server configuration…</span>
            </div>
          )}

          {!loading && error && (
            <div className="flex items-start gap-3 p-3 bg-destructive/10 border border-destructive/30 rounded-sm">
              <AlertTriangle size={16} className="text-destructive shrink-0 mt-0.5" />
              <div className="text-xs text-foreground">{error}</div>
            </div>
          )}

          {!loading && !error && (
            <>
              {/* Informational Notice */}
              <div className="flex items-start gap-2.5 p-3 rounded-md border border-border bg-muted/30 text-xs text-muted-foreground">
                <Cpu size={14} className="shrink-0 mt-0.5 text-primary" />
                <span>
                  Designate external <strong>MCP protocol servers</strong> available to this agent.
                  The agent will automatically discover tools exposed by active servers during task execution.
                </span>
              </div>

              {/* Section 1: Manifest MCP Servers */}
              <section className="space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="text-xs uppercase tracking-wider font-bold text-foreground">
                    Manifest MCP Servers
                  </h3>
                  <span className="text-[11px] font-mono text-muted-foreground">
                    {manifestRows.length - disabled.size} / {manifestRows.length} active
                  </span>
                </div>

                {manifestRows.length === 0 ? (
                  <p className="text-xs text-muted-foreground italic py-2">
                    This agent declares no default baseline MCP servers in its manifest.
                  </p>
                ) : (
                  <div className="space-y-2">
                    {manifestRows.map((row) => {
                      const isActive = !disabled.has(row.id);
                      return (
                        <button
                          key={row.id}
                          type="button"
                          onClick={() => toggleManifestServer(row.id)}
                          className={`w-full flex items-start gap-3 p-3 rounded-md border text-left transition cursor-pointer ${
                            isActive
                              ? "bg-primary/5 border-primary/30 hover:bg-primary/10"
                              : "bg-muted/20 border-border hover:bg-muted/40"
                          }`}
                        >
                          <span
                            className={`mt-0.5 flex items-center justify-center shrink-0 w-4 h-4 rounded border ${
                              isActive
                                ? "bg-primary border-primary text-primary-foreground"
                                : "bg-background border-border"
                            }`}
                          >
                            {isActive && <Check size={10} />}
                          </span>

                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-1.5 flex-wrap">
                              <span className="text-xs font-semibold text-foreground font-mono">
                                {row.name}
                              </span>
                              <span className="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase bg-muted text-muted-foreground">
                                {row.transport}
                              </span>
                              {row.is_builtin && (
                                <span className="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase bg-sky-500/10 text-sky-600 dark:text-sky-400">
                                  builtin
                                </span>
                              )}
                              {row.is_bundle && (
                                <span className="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
                                  bundle
                                </span>
                              )}
                              {!row.availableInStore && (
                                <span className="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase bg-amber-500/10 text-amber-600 inline-flex items-center gap-1">
                                  <XCircle size={9} />
                                  not in registry
                                </span>
                              )}
                            </div>
                            {row.description && (
                              <p className="text-[11px] text-muted-foreground mt-0.5 line-clamp-1">
                                {row.description}
                              </p>
                            )}
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </section>

              {/* Section 2: Extra MCP Servers from Store */}
              <section className="space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="text-xs uppercase tracking-wider font-bold text-foreground">
                    Available Store MCP Servers
                  </h3>
                  <span className="text-[11px] font-mono text-muted-foreground">
                    {extra.size} added
                  </span>
                </div>

                {extraCandidates.length === 0 ? (
                  <p className="text-xs text-muted-foreground italic py-2">
                    No additional MCP servers available in registry.
                  </p>
                ) : (
                  <div className="space-y-2">
                    {extraCandidates.map((server) => {
                      const isAdded = extra.has(server.id);
                      return (
                        <button
                          key={server.id}
                          type="button"
                          onClick={() => toggleExtraServer(server.id)}
                          className={`w-full flex items-start gap-3 p-3 rounded-md border text-left transition cursor-pointer ${
                            isAdded
                              ? "bg-emerald-500/5 border-emerald-500/30 hover:bg-emerald-500/10"
                              : "bg-muted/20 border-border hover:bg-muted/40"
                          }`}
                        >
                          <span
                            className={`mt-0.5 flex items-center justify-center shrink-0 w-4 h-4 rounded border ${
                              isAdded
                                ? "bg-emerald-500 border-emerald-500 text-white"
                                : "bg-background border-border"
                            }`}
                          >
                            {isAdded && <Check size={10} />}
                          </span>

                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-1.5 flex-wrap">
                              <span className="text-xs font-semibold text-foreground font-mono">
                                {server.name}
                              </span>
                              <span className="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase bg-muted text-muted-foreground">
                                {server.transport}
                              </span>
                              {server.is_builtin && (
                                <span className="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase bg-sky-500/10 text-sky-600 dark:text-sky-400">
                                  builtin
                                </span>
                              )}
                              {server.is_bundle && (
                                <span className="text-[10px] px-1.5 py-0.2 rounded font-mono uppercase bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
                                  bundle
                                </span>
                              )}
                            </div>
                            {server.description && (
                              <p className="text-[11px] text-muted-foreground mt-0.5 line-clamp-1">
                                {server.description}
                              </p>
                            )}
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </section>

              {/* Section 3: Effective Preview */}
              <section className="pt-3 border-t border-border space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="text-xs uppercase tracking-wider font-bold text-foreground">
                    Effective MCP Servers
                  </h3>
                  <span className="text-[11px] font-mono text-primary font-semibold">
                    {effectiveServers.length} server(s) authorized
                  </span>
                </div>

                {effectiveServers.length === 0 ? (
                  <p className="text-xs text-muted-foreground italic">
                    No MCP servers authorized. The agent will run exclusively with standard tools.
                  </p>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {effectiveServers.map((id) => (
                      <span
                        key={id}
                        className="px-2 py-0.5 rounded-md text-[11px] font-mono bg-primary/10 border border-primary/30 text-primary"
                      >
                        {id}
                      </span>
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-border flex items-center justify-between gap-2 shrink-0 bg-muted/20">
          <span className="text-[11px] text-muted-foreground font-mono">
            {manifestServers.length} manifest · {disabled.size} disabled · {extra.size} extra
          </span>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="px-3.5 py-1.5 text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-50 cursor-pointer"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={!canSave}
              className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:opacity-90 disabled:opacity-50 transition cursor-pointer"
            >
              {saving ? (
                <>
                  <Loader2 size={13} className="animate-spin" />
                  Saving…
                </>
              ) : (
                <>
                  <Save size={13} />
                  Save MCP Servers
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default AgentMcpModal;
