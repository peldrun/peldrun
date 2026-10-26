// frontend/src/app/stores/_modals/AgentToolsModal.tsx

"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  Loader2,
  Save,
  Wrench,
  X,
  XCircle,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "../_hooks/useStoreData";
import type { AgentToolInfo, AgentToolsState } from "../_types/store";

/**
 * Props for {@link AgentToolsModal}.
 *
 * The modal owns its own load/save cycle; the parent only supplies
 * the agent id/name and a close callback. It is intentionally opened
 * only for agents whose manifest declares ``settings.tools_editable``
 * (the card hides the trigger otherwise, and the backend enforces the
 * same gate with a 403).
 */
export interface AgentToolsModalProps {
  open: boolean;
  agentId: string | null;
  agentName: string;
  onClose: () => void;
  /** Fired after a successful save so the parent can refresh any caches. */
  onSaved?: () => void;
}

/**
 * Modal for overriding which tools an agent may invoke.
 *
 * Layout:
 *   ┌─ Header: Wrench + "Agent Tools" + agentName ────────────┐
 *   │                                                          │
 *   │  [Info banner — overrides apply to next task run]        │
 *   │                                                          │
 *   │  ── Manifest Tools ──────────────────────────────────    │
 *   │  [x] bash               shell · builtin · safety        │
 *   │  [ ] browser_interactive (not available in store)       │
 *   │  [x] python_execute     python · builtin                │
 *   │                                                          │
 *   │  ── Extra Tools (from store) ────────────────────────    │
 *   │  [ ] source_control     source-control · builtin        │
 *   │  [ ] http_fetch         web · builtin                   │
 *   │                                                          │
 *   │  ── Effective Preview ───────────────────────────────    │
 *   │  6 tools: bash, python_execute, ...                      │
 *   │                                                          │
 *   └─ Cancel · Save Tools ────────────────────────────────────┘
 *
 * Checkbox semantics:
 *   - Manifest tool checked   → active  (not in disabled_tools)
 *   - Manifest tool unchecked → excluded (added to disabled_tools)
 *   - Extra tool checked      → added   (in extra_tools)
 *   - Extra tool unchecked    → ignored (not in extra_tools)
 *
 * Manifest tools that are unavailable in the Store are still shown so
 * the user can see the full intended toolset, but are rendered with a
 * warning badge. They can still be toggled (harmless — the runtime
 * drops them silently when they are not registered).
 */
export function AgentToolsModal({
  open,
  agentId,
  agentName,
  onClose,
  onSaved,
}: AgentToolsModalProps) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [manifestTools, setManifestTools] = useState<string[]>([]);
  const [availableTools, setAvailableTools] = useState<AgentToolInfo[]>([]);
  const [disabled, setDisabled] = useState<Set<string>>(new Set());
  const [extra, setExtra] = useState<Set<string>>(new Set());

  // ── Load tools state on open ───────────────────────────────────
  useEffect(() => {
    if (!open || !agentId) return;
    let cancelled = false;

    setLoading(true);
    setError(null);
    setManifestTools([]);
    setAvailableTools([]);
    setDisabled(new Set());
    setExtra(new Set());

    (async () => {
      try {
        const res = await fetch(
          getApiUrl(`/api/store/agents/${agentId}/tools`)
        );
        if (cancelled) return;

        if (res.status === 403) {
          setError(
            "This agent does not allow tool editing. The manifest has " +
              "tools_editable disabled."
          );
          return;
        }
        if (res.status === 404) {
          setError(`Agent '${agentId}' not found.`);
          return;
        }
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setError(
            body.detail || `Failed to load tools (HTTP ${res.status}).`
          );
          return;
        }

        const data: AgentToolsState = await res.json();
        setManifestTools(data.manifest_tools || []);
        setAvailableTools(data.available_tools || []);
        setDisabled(new Set(data.disabled_tools || []));
        setExtra(new Set(data.extra_tools || []));
      } catch (exc: any) {
        if (cancelled) return;
        setError(exc?.message || "Network error while loading tools.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, agentId]);

  // ── Escape + body scroll lock ─────────────────────────────────
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

  // ── Toggle helpers ────────────────────────────────────────────
  const toggleManifestTool = useCallback((toolId: string) => {
    setDisabled((prev) => {
      const next = new Set(prev);
      if (next.has(toolId)) next.delete(toolId);
      else next.add(toolId);
      return next;
    });
  }, []);

  const toggleExtraTool = useCallback((toolId: string) => {
    setExtra((prev) => {
      const next = new Set(prev);
      if (next.has(toolId)) next.delete(toolId);
      else next.add(toolId);
      return next;
    });
  }, []);

  // ── Derived lists ─────────────────────────────────────────────
  /** Manifest tools enriched with store metadata (or placeholders). */
  const manifestRows = useMemo(() => {
    const byId = new Map(availableTools.map((t) => [t.id, t]));
    return manifestTools.map((id) => {
      const meta = byId.get(id);
      return {
        id,
        name: meta?.name ?? id,
        description: meta?.description ?? "",
        category: meta?.category ?? "",
        is_builtin: meta?.is_builtin ?? false,
        safety_level: meta?.safety_level ?? null,
        availableInStore: !!meta,
      };
    });
  }, [manifestTools, availableTools]);

  /** Available store tools not already declared in the manifest. */
  const extraCandidates = useMemo(
    () => availableTools.filter((t) => !t.in_manifest),
    [availableTools]
  );

  /** Live preview of the effective tool list after pending toggles. */
  const effectiveTools = useMemo(() => {
    const out: string[] = [];
    for (const id of manifestTools) {
      if (!disabled.has(id)) out.push(id);
    }
    for (const id of extra) {
      if (!out.includes(id)) out.push(id);
    }
    return out;
  }, [manifestTools, disabled, extra]);

  const disabledCount = disabled.size;
  const extraCount = extra.size;

  // ── Save ──────────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (!agentId) return;
    setSaving(true);
    setError(null);

    try {
      const res = await fetch(
        getApiUrl(`/api/store/agents/${agentId}/tools`),
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            disabled_tools: Array.from(disabled),
            extra_tools: Array.from(extra),
          }),
        }
      );

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail =
          typeof body.detail === "string"
            ? body.detail
            : "Failed to save tool selection.";
        setError(detail);
        showToast.error("Save Rejected", detail);
        return;
      }

      const data: AgentToolsState = await res.json();
      // Reflect the server's canonical state in case it dropped anything.
      setManifestTools(data.manifest_tools || []);
      setDisabled(new Set(data.disabled_tools || []));
      setExtra(new Set(data.extra_tools || []));

      showToast.success(
        "Agent Tools Saved",
        `${agentName} updated — ${data.effective_tools?.length ?? 0} tool(s) active.`
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

  // ── Render ────────────────────────────────────────────────────
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="agent-tools-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !saving) onClose();
      }}
    >
      <div className="bg-card rounded-md shadow-2xl w-full max-w-3xl max-h-[92vh] flex flex-col">
        {/* ── Header ────────────────────────────────────────────── */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0 bg-card-foreground/50">
          <div className="flex items-center gap-2 min-w-0">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
              <Wrench size={15} />
            </div>
            <div className="min-w-0">
              <h2
                id="agent-tools-title"
                className="text-base font-bold text-primary-foreground font-mono"
              >
                Agent Tools
              </h2>
              <p className="text-xs text-primary-foreground/80 mt-0.5 truncate">
                {agentName}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-50 transition"
            aria-label="Close tools"
          >
            <X size={18} />
          </button>
        </div>

        {/* ── Body ──────────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto px-6 py-5 scrollbar-thin bg-card-foreground/10 space-y-5">
          {loading && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 size={20} className="animate-spin mr-2" />
              <span className="text-sm">Loading tool selection…</span>
            </div>
          )}

          {!loading && error && (
            <div className="flex items-start gap-3 p-4 bg-destructive/10 border border-destructive/30 rounded-sm">
              <AlertTriangle
                size={18}
                className="text-destructive shrink-0 mt-0.5"
              />
              <div className="text-sm text-foreground">{error}</div>
            </div>
          )}

          {!loading && !error && (
            <>
              {/* Info banner */}
              <div className="flex items-start gap-2 p-3 rounded-sm border border-border bg-muted/30 text-[11px] text-muted-foreground">
                <AlertTriangle size={12} className="shrink-0 mt-0.5 text-primary" />
                <span>
                  Tool overrides apply to the <strong>next task run</strong>.
                  The manifest baseline stays unchanged — disabling a tool
                  simply excludes it from this agent's runtime toolset.
                </span>
              </div>

              {/* ── Section 1: Manifest Tools ─────────────────────── */}
              <section>
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-[11px] uppercase tracking-wider font-bold text-foreground">
                    Manifest Tools
                  </h3>
                  <span className="text-[10px] font-mono text-muted-foreground">
                    {manifestRows.length - disabledCount} /{" "}
                    {manifestRows.length} active
                  </span>
                </div>

                {manifestRows.length === 0 && (
                  <p className="text-xs text-muted-foreground italic py-3">
                    This agent declares no manifest tools.
                  </p>
                )}

                <div className="space-y-1.5">
                  {manifestRows.map((row) => {
                    const isActive = !disabled.has(row.id);
                    return (
                      <button
                        key={row.id}
                        type="button"
                        onClick={() => toggleManifestTool(row.id)}
                        className={`w-full flex items-start gap-3 p-2.5 rounded-sm border text-left transition cursor-pointer ${
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
                          aria-hidden="true"
                        >
                          {isActive && <Check size={10} />}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-xs font-mono font-semibold text-foreground truncate">
                              {row.id}
                            </span>
                            {row.category && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-muted border border-border text-muted-foreground">
                                {row.category}
                              </span>
                            )}
                            {row.is_builtin && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-sky-500/10 border border-sky-500/20 text-sky-700 dark:text-sky-300">
                                builtin
                              </span>
                            )}
                            {!row.availableInStore && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-amber-500/10 border border-amber-500/30 text-amber-700 dark:text-amber-300 inline-flex items-center gap-0.5">
                                <XCircle size={8} />
                                not in store
                              </span>
                            )}
                          </div>
                          {row.description && (
                            <p className="text-[10px] text-muted-foreground mt-0.5 line-clamp-1">
                              {row.description}
                            </p>
                          )}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </section>

              {/* ── Section 2: Extra Tools ─────────────────────────── */}
              <section>
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-[11px] uppercase tracking-wider font-bold text-foreground">
                    Extra Tools (from Store)
                  </h3>
                  <span className="text-[10px] font-mono text-muted-foreground">
                    {extraCount} added
                  </span>
                </div>

                {extraCandidates.length === 0 && (
                  <p className="text-xs text-muted-foreground italic py-3">
                    No additional store tools are available to add.
                  </p>
                )}

                <div className="space-y-1.5">
                  {extraCandidates.map((tool) => {
                    const isAdded = extra.has(tool.id);
                    return (
                      <button
                        key={tool.id}
                        type="button"
                        onClick={() => toggleExtraTool(tool.id)}
                        className={`w-full flex items-start gap-3 p-2.5 rounded-sm border text-left transition cursor-pointer ${
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
                          aria-hidden="true"
                        >
                          {isAdded && <Check size={10} />}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-xs font-mono font-semibold text-foreground truncate">
                              {tool.id}
                            </span>
                            {tool.category && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-muted border border-border text-muted-foreground">
                                {tool.category}
                              </span>
                            )}
                            {tool.is_builtin && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-sky-500/10 border border-sky-500/20 text-sky-700 dark:text-sky-300">
                                builtin
                              </span>
                            )}
                          </div>
                          {tool.description && (
                            <p className="text-[10px] text-muted-foreground mt-0.5 line-clamp-1">
                              {tool.description}
                            </p>
                          )}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </section>

              {/* ── Effective Preview ──────────────────────────────── */}
              <section className="pt-2 border-t border-border">
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-[11px] uppercase tracking-wider font-bold text-foreground">
                    Effective Preview
                  </h3>
                  <span className="text-[10px] font-mono text-primary">
                    {effectiveTools.length} tool(s) at runtime
                  </span>
                </div>
                {effectiveTools.length === 0 ? (
                  <p className="text-xs text-muted-foreground italic py-2">
                    No tools active. The agent will not be able to execute
                    actions until at least one tool is enabled.
                  </p>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {effectiveTools.map((id) => (
                      <span
                        key={id}
                        className="px-2 py-0.5 rounded-md text-[10px] font-mono bg-primary/10 border border-primary/30 text-primary"
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

        {/* ── Footer ────────────────────────────────────────────── */}
        <div className="px-6 py-4 border-t border-border flex items-center justify-between gap-2 shrink-0">
          <span className="text-[10px] text-muted-foreground font-mono truncate">
            {manifestTools.length} manifest · {disabledCount} disabled ·{" "}
            {extraCount} extra
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="px-4 py-2 text-sm text-muted-foreground hover:text-foreground transition disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={!canSave}
              className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-sm text-sm font-medium hover:opacity-90 disabled:opacity-50 transition"
            >
              {saving ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  Saving…
                </>
              ) : (
                <>
                  <Save size={14} />
                  Save Tools
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default AgentToolsModal;
