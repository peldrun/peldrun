// frontend/src/app/settings/setup/tabs/MCPTab.tsx
"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  Save,
  Loader2,
  RefreshCw,
  Power,
  Clock,
  Timer,
  Database,
  Gauge,
  Zap,
  AlertTriangle,
  Info,
  Settings2,
  RotateCcw,
  PackagePlus,
  Trash,
  ShieldAlert,
  X,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";

interface MCPSettings {
  enabled: boolean;
  tool_cache_ttl_hours: number;
  startup_timeout_seconds: number;
  call_timeout_seconds: number;
  max_tools_per_server: number;
  auto_cleanup_idle_minutes: number;
  lazy_start_on_demand: boolean;
  send_all_tools_by_default: boolean;
}

interface MCPTabProps {
  config: any;
  setConfig: React.Dispatch<React.SetStateAction<any>>;
}

const DEFAULT_SETTINGS: MCPSettings = {
  enabled: true,
  tool_cache_ttl_hours: 24,
  startup_timeout_seconds: 30,
  call_timeout_seconds: 120,
  max_tools_per_server: 100,
  auto_cleanup_idle_minutes: 30,
  lazy_start_on_demand: false,
  send_all_tools_by_default: true,
};

export function MCPTab({ config, setConfig }: MCPTabProps) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [settings, setSettings] = useState<MCPSettings>(DEFAULT_SETTINGS);
  const [defaults, setDefaults] = useState<MCPSettings>(DEFAULT_SETTINGS);
  const initialRef = useRef<MCPSettings>(DEFAULT_SETTINGS);

  // ── Maintenance state ──────────────────────────────────────
  const [warmingCache, setWarmingCache] = useState(false);
  const [cleaningSessions, setCleaningSessions] = useState(false);
  const [restarting, setRestarting] = useState(false);

  // ── Custom Confirm Dialog State ─────────────────────────────
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    message: React.ReactNode;
    confirmLabel: string;
    variant: "primary" | "destructive";
    onConfirm: () => void | Promise<void>;
  } | null>(null);

  const fetchSettings = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/mcp/settings");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const s = data.settings || DEFAULT_SETTINGS;
      const d = data.defaults || DEFAULT_SETTINGS;
      setSettings(s);
      setDefaults(d);
      initialRef.current = s;
    } catch (err: any) {
      console.error("Failed to load MCP settings:", err);
      showToast.error("Load Failed", err.message || "Could not load MCP settings.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSettings();
  }, [fetchSettings]);

  const isDirty = JSON.stringify(settings) !== JSON.stringify(initialRef.current);

  const setField = <K extends keyof MCPSettings>(key: K, value: MCPSettings[K]) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
  };

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const res = await fetch("/api/mcp/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      const saved = data.settings || settings;
      setSettings(saved);
      initialRef.current = saved;
      showToast.success("MCP Settings Saved", "Subsystem configuration updated.");
    } catch (err: any) {
      console.error("Failed to save MCP settings:", err);
      showToast.error("Save Failed", err.message || "Could not save MCP settings.");
    } finally {
      setSaving(false);
    }
  }, [settings]);

  const handleReset = useCallback(() => {
    setSettings(defaults);
    showToast.info("Reset to Defaults", "Click Save to apply the default values.");
  }, [defaults]);

  // Ctrl/Cmd+S shortcut
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        if (isDirty && !saving) handleSave();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isDirty, saving, handleSave]);

  // ── Maintenance handlers ───────────────────────────────────

  const handleWarmCache = useCallback(async () => {
    setWarmingCache(true);
    try {
      showToast.info(
        "Warming npm Cache",
        "Downloading packages for all npx-based MCP servers. This may take 30-120 seconds."
      );
      const res = await fetch("/api/mcp/maintenance/warm-npm-cache", {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (data.failed > 0) {
        showToast.warning(
          `Cache Warmed (${data.succeeded}/${data.total})`,
          `${data.failed} package(s) failed. Check server logs for details.`
        );
      } else {
        showToast.success(
          "Cache Warmed Successfully",
          `${data.succeeded} package(s) downloaded and cached.`
        );
      }
    } catch (err: any) {
      showToast.error("Cache Warm Failed", err?.message || "Unknown error");
    } finally {
      setWarmingCache(false);
    }
  }, []);

  const handleCleanupSessions = useCallback(() => {
    setConfirmAction({
      title: "Cleanup MCP Sessions",
      message: (
        <div className="space-y-3">
          <p className="text-sm text-foreground">
            Close all active MCP sessions and invalidate caches?
          </p>
          <p className="text-xs text-muted-foreground leading-relaxed">
            Active chats will re-discover tools on the next tool call. This
            operation is safe and idempotent.
          </p>
        </div>
      ),
      confirmLabel: "Cleanup Now",
      variant: "primary",
      onConfirm: async () => {
        setCleaningSessions(true);
        try {
          const res = await fetch("/api/mcp/maintenance/cleanup-sessions", {
            method: "POST",
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
          }
          const data = await res.json();
          showToast.success(
            "Sessions Cleaned",
            `${data.sessions_closed} session(s) closed, ${data.caches_invalidated} cache(s) invalidated.`
          );
        } catch (err: any) {
          showToast.error("Cleanup Failed", err?.message || "Unknown error");
        } finally {
          setCleaningSessions(false);
          setConfirmAction(null);
        }
      },
    });
  }, []);

  const handleRestartBackend = useCallback(() => {
    setConfirmAction({
      title: "Restart Backend",
      message: (
        <div className="space-y-3">
          <p className="text-sm text-foreground">
            Are you sure you want to restart the backend?
          </p>
          <ul className="list-disc pl-5 text-xs text-muted-foreground space-y-1 leading-relaxed">
            <li>All active chats will reconnect.</li>
            <li>The page will reload automatically once the backend is back up.</li>
            <li>This takes approximately 5–10 seconds.</li>
          </ul>
        </div>
      ),
      confirmLabel: "Restart Now",
      variant: "destructive",
      onConfirm: async () => {
        setRestarting(true);
        try {
          await fetch("/api/mcp/maintenance/restart", { method: "POST" });
          showToast.info(
            "Restarting Backend",
            "The backend is shutting down. Waiting for it to come back online…"
          );

          await new Promise((r) => setTimeout(r, 3000));

          const start = Date.now();
          while (Date.now() - start < 30_000) {
            try {
              const res = await fetch("/api/health", { cache: "no-store" });
              if (res.ok) {
                showToast.success(
                  "Backend Restarted",
                  "Reloading the page now…"
                );
                setTimeout(() => window.location.reload(), 500);
                return;
              }
            } catch {
              // Expected while backend is down.
            }
            await new Promise((r) => setTimeout(r, 800));
          }

          showToast.error(
            "Restart Timeout",
            "Backend did not respond within 30s. Check the terminal."
          );
        } catch (err) {
          console.debug("Restart fetch error (expected during shutdown):", err);
        } finally {
          setRestarting(false);
          setConfirmAction(null);
        }
      },
    });
  }, []);

  if (loading) {
    return (
      <div className="llm-tab w-full space-y-6 font-sans pb-6">
        <div className="flex items-center justify-center py-16 text-muted-foreground gap-2 text-sm">
          <Loader2 size={18} className="animate-spin text-primary" />
          Loading MCP subsystem settings…
        </div>
      </div>
    );
  }

  const isDisabled = !settings.enabled;

  return (
    <div className="llm-tab w-full space-y-6 font-sans pb-24">
      {/* ── Header ─────────────────────────────────────────────── */}
      <div className="border-b border-border w-full pb-4 pt-4 bg-custom">
        <div className="flex items-center justify-between pr-4 pl-4">
          <div className="flex items-center gap-2">
            <Settings2 size={16} className="text-primary" />
            <h2 className="text-sm font-semibold font-heading text-foreground uppercase tracking-wide">
              Model Context Protocol — Subsystem Settings
            </h2>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={fetchSettings}
              disabled={loading || saving}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-md border border-border bg-card hover:bg-muted text-foreground transition-colors disabled:opacity-50"
            >
              <RefreshCw size={12} className={loading ? "animate-spin text-primary" : ""} />
              Reload
            </button>
            <button
              type="button"
              onClick={handleReset}
              disabled={loading || saving}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-md border border-border bg-card hover:bg-muted text-foreground transition-colors disabled:opacity-50"
              title="Reset all fields to defaults (must Save to apply)"
            >
              <RotateCcw size={12} />
              Reset
            </button>
          </div>
        </div>
        <p className="text-xs text-muted-foreground mt-0.5 pr-4 pl-4">
          Global configuration for the MCP subsystem. Server-level settings live in the Store; per-chat credentials are managed via the authentication modal inside each chat.
        </p>
      </div>

      <div className="w-full max-w-5xl space-y-6 m-auto px-4">
        {/* ── Disabled banner ─────────────────────────────────── */}
        {isDisabled && (
          <div className="p-4 rounded-md border border-amber-500/30 bg-amber-500/10 flex items-start gap-3">
            <AlertTriangle size={18} className="text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
            <div className="text-xs">
              <p className="font-semibold text-amber-700 dark:text-amber-300">
                MCP Subsystem is Disabled
              </p>
              <p className="text-amber-700/80 dark:text-amber-400/80 mt-1 leading-relaxed">
                No MCP server will be contacted. All active sessions have been closed. Agents cannot see or invoke any MCP tool until the subsystem is re-enabled and saved.
              </p>
            </div>
          </div>
        )}

        {/* ── Master toggle ──────────────────────────────────── */}
        <section className="p-5 rounded-md border border-border bg-gigbg shadow-sm">
          <div className="flex items-center justify-between gap-4">
            <div className="flex items-start gap-3">
              <div className={`p-2 rounded-lg ${settings.enabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>
                <Power size={18} />
              </div>
              <div>
                <h3 className="text-sm font-semibold text-foreground">
                  Master Enable
                </h3>
                <p className="text-xs text-muted-foreground mt-0.5 leading-relaxed max-w-lg">
                  When disabled, the entire MCP subsystem is stopped — no subprocesses, no discovery, no tool injection. Existing chat sessions are unaffected after the next save.
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => setField("enabled", !settings.enabled)}
              className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-primary/30 ${
                settings.enabled ? "bg-green-500" : "bg-muted-foreground/40"
              }`}
              aria-label={settings.enabled ? "Disable MCP subsystem" : "Enable MCP subsystem"}
            >
              <span
                className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow-lg ring-0 transition duration-200 ease-in-out ${
                  settings.enabled ? "translate-x-5" : "translate-x-0"
                }`}
              />
            </button>
          </div>
        </section>

              {/* ── Custom Confirm Dialog ─────────────────────────────── */}
      {confirmAction && (
        <ConfirmDialog
          open={!!confirmAction}
          title={confirmAction.title}
          message={confirmAction.message}
          confirmLabel={confirmAction.confirmLabel}
          variant={confirmAction.variant}
          loading={restarting || cleaningSessions}
          onConfirm={confirmAction.onConfirm}
          onCancel={() => {
            if (!restarting && !cleaningSessions) {
              setConfirmAction(null);
            }
          }}
        />
      )}

        {/* ── Cache & Performance ─────────────────────────────── */}
        <section className="p-5 rounded-md border border-border bg-gigbg shadow-sm space-y-4">
          <header className="flex items-center gap-2 pb-2 border-b border-border/60">
            <Database size={15} className="text-primary" />
            <h3 className="text-xs font-semibold text-foreground uppercase tracking-wider">
              Tool Cache & Discovery
            </h3>
          </header>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <NumberField
              icon={<Clock size={14} />}
              label="Tool Cache TTL (hours)"
              hint="How long a cached tool list is considered authoritative before re-discovery. Range: 1–168."
              value={settings.tool_cache_ttl_hours}
              min={1}
              max={168}
              onChange={(v) => setField("tool_cache_ttl_hours", v)}
              disabled={isDisabled}
            />
            <NumberField
              icon={<Gauge size={14} />}
              label="Max Tools per Server"
              hint="Hard cap on tools accepted from one server. Protects against malformed servers. Range: 10–1000."
              value={settings.max_tools_per_server}
              min={10}
              max={1000}
              onChange={(v) => setField("max_tools_per_server", v)}
              disabled={isDisabled}
            />
          </div>

          <ToggleRow
            icon={<Zap size={14} />}
            label="Lazy Start on Demand"
            hint="Spawn subprocess only when a tool is invoked, not during discovery. Recommended for costly-to-start servers with sporadic usage."
            checked={settings.lazy_start_on_demand}
            onChange={(v) => setField("lazy_start_on_demand", v)}
            disabled={isDisabled}
          />

          <ToggleRow
            icon={<Info size={14} />}
            label="Send All Tools by Default"
            hint="When enabled, newly discovered tools are auto-enabled. When disabled, new tools start disabled and must be opted in via the Manage Tools modal."
            checked={settings.send_all_tools_by_default}
            onChange={(v) => setField("send_all_tools_by_default", v)}
            disabled={isDisabled}
          />
        </section>

        {/* ── Timeouts ────────────────────────────────────────── */}
        <section className="p-5 rounded-md border border-border bg-gigbg shadow-sm space-y-4">
          <header className="flex items-center gap-2 pb-2 border-b border-border/60">
            <Timer size={15} className="text-primary" />
            <h3 className="text-xs font-semibold text-foreground uppercase tracking-wider">
              Timeouts
            </h3>
          </header>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <NumberField
              icon={<Timer size={14} />}
              label="Startup Timeout (s)"
              hint="Maximum time to spawn a stdio subprocess or establish HTTP/SSE. Range: 5–300."
              value={settings.startup_timeout_seconds}
              min={5}
              max={300}
              onChange={(v) => setField("startup_timeout_seconds", v)}
              disabled={isDisabled}
            />
            <NumberField
              icon={<Timer size={14} />}
              label="Call Timeout (s)"
              hint="Maximum duration of a single tool invocation. Range: 10–1800."
              value={settings.call_timeout_seconds}
              min={10}
              max={1800}
              onChange={(v) => setField("call_timeout_seconds", v)}
              disabled={isDisabled}
            />
            <NumberField
              icon={<Clock size={14} />}
              label="Idle Cleanup (min)"
              hint="Close idle stdio subprocesses after this many minutes of inactivity. Range: 1–1440."
              value={settings.auto_cleanup_idle_minutes}
              min={1}
              max={1440}
              onChange={(v) => setField("auto_cleanup_idle_minutes", v)}
              disabled={isDisabled}
            />
          </div>
        </section>

        {/* ── Where to do what ────────────────────────────────── */}
        <section className="p-4 rounded-md border border-border/60 bg-card/50">
          <p className="text-[11px] text-muted-foreground leading-relaxed">
            <strong className="text-foreground">Looking for something else?</strong>{" "}
            Server registration and per-server credentials are managed in the{" "}
            <a href="/stores" className="text-primary hover:underline">Store → Extensions &amp; MCP</a>{" "}
            section. Per-chat credentials are managed inside each chat via the authentication modal.
            Individual tool enable/disable lives in the server card in the Store.
          </p>
        </section>

        {/* ── Maintenance ─────────────────────────────────────── */}
        <section className="p-5 rounded-md border border-border bg-gigbg shadow-sm space-y-4">
          <header className="flex items-center gap-2 pb-2 border-b border-border/60">
            <Settings2 size={15} className="text-primary" />
            <h3 className="text-xs font-semibold text-foreground uppercase tracking-wider">
              Maintenance
            </h3>
          </header>
          <p className="text-xs text-muted-foreground leading-relaxed">
            Recovery actions for common operational issues. Safe to run
            at any time — none of them touch existing chat history or
            saved credentials.
          </p>

          <div className="space-y-2.5">
            <MaintenanceRow
              icon={<PackagePlus size={15} />}
              label="Warm npm Cache"
              description="Re-download packages for all npx-based MCP servers. Fixes Test Connection timeouts after `npm cache clean` or network outages."
              actionLabel={warmingCache ? "Downloading…" : "Warm Cache"}
              loading={warmingCache}
              onClick={handleWarmCache}
              variant="primary"
            />
            <MaintenanceRow
              icon={<Trash size={15} />}
              label="Cleanup Sessions"
              description="Close all active MCP subprocesses and invalidate caches. Use when Test Connection hangs or Tools Refresh stalls."
              actionLabel={cleaningSessions ? "Cleaning…" : "Cleanup"}
              loading={cleaningSessions}
              onClick={handleCleanupSessions}
              variant="secondary"
            />
            <MaintenanceRow
              icon={<ShieldAlert size={15} />}
              label="Restart Backend"
              description="Re-execute the backend process to load newly-edited Python modules. Active chats will reconnect automatically."
              actionLabel={restarting ? "Restarting…" : "Restart"}
              loading={restarting}
              onClick={handleRestartBackend}
              variant="destructive"
            />
          </div>
        </section>
      </div>

      {/* ── Sticky save bar ────────────────────────────────────── */}
      {isDirty && (
        <div className="fixed bottom-0 left-0 right-0 z-40 border-t border-border bg-background/95 backdrop-blur-sm shadow-lg">
          <div className="max-w-5xl mx-auto px-4 py-3 flex items-center justify-between gap-4">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <div className="h-1.5 w-1.5 rounded-full bg-amber-500 animate-pulse" />
              <span>You have unsaved changes.</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setSettings(initialRef.current)}
                disabled={saving}
                className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-50"
              >
                Discard
              </button>
              <button
                type="button"
                onClick={handleSave}
                disabled={saving}
                className="inline-flex items-center gap-1.5 px-4 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:opacity-90 disabled:opacity-50 transition"
              >
                {saving ? (
                  <>
                    <Loader2 size={13} className="animate-spin" />
                    Saving…
                  </>
                ) : (
                  <>
                    <Save size={13} />
                    Save Changes
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Helper components
// ─────────────────────────────────────────────────────────────────
function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  variant = "primary",
  loading = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: React.ReactNode;
  confirmLabel: string;
  variant?: "primary" | "destructive";
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !loading) onCancel();
      }}
    >
      <div className="bg-card rounded-lg shadow-2xl w-full max-w-md border border-border overflow-hidden animate-in fade-in zoom-in-95 duration-150">
        {/* Header */}
        <div className="px-5 py-3.5 border-b border-border bg-muted/30 flex items-center justify-between">
          <h3 className="text-sm font-bold text-foreground">{title}</h3>
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 transition disabled:opacity-40"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        {/* Body */}
        <div className="px-5 py-4">{message}</div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-border bg-muted/20 flex items-center justify-end gap-2 shrink-0">
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="px-4 py-1.5 rounded-md text-xs font-medium text-muted-foreground hover:text-foreground transition disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={loading}
            className={`inline-flex items-center gap-1.5 px-4 py-1.5 rounded-md text-xs font-medium transition disabled:opacity-50 ${
              variant === "destructive"
                ? "bg-destructive text-destructive-foreground hover:opacity-90"
                : "bg-primary text-primary-foreground hover:opacity-90"
            }`}
          >
            {loading && <Loader2 size={13} className="animate-spin" />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

function MaintenanceRow({
  icon,
  label,
  description,
  actionLabel,
  loading,
  onClick,
  variant,
}: {
  icon: React.ReactNode;
  label: string;
  description: string;
  actionLabel: string;
  loading: boolean;
  onClick: () => void;
  variant: "primary" | "secondary" | "destructive";
}) {
  const buttonClass =
    variant === "destructive"
      ? "bg-destructive text-destructive-foreground hover:opacity-90"
      : variant === "primary"
      ? "bg-primary text-primary-foreground hover:opacity-90"
      : "border border-border bg-card hover:bg-muted text-foreground";

  return (
    <div className="flex items-start justify-between gap-4 p-3 rounded-md border border-border/60 bg-background/50">
      <div className="flex items-start gap-2.5 min-w-0 flex-1">
        <span className="text-muted-foreground mt-0.5 shrink-0">{icon}</span>
        <div className="min-w-0">
          <div className="text-xs font-semibold text-foreground">{label}</div>
          <p className="text-[10px] text-muted-foreground mt-0.5 leading-tight">
            {description}
          </p>
        </div>
      </div>
      <button
        type="button"
        onClick={onClick}
        disabled={loading}
        className={`shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition disabled:opacity-50 disabled:cursor-not-allowed ${buttonClass}`}
      >
        {loading && <Loader2 size={12} className="animate-spin" />}
        {actionLabel}
      </button>
    </div>
  );
}

function NumberField({
  icon,
  label,
  hint,
  value,
  min,
  max,
  onChange,
  disabled,
}: {
  icon: React.ReactNode;
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <div className={disabled ? "opacity-50 pointer-events-none" : ""}>
      <label className="flex items-center gap-1.5 text-xs font-semibold text-foreground mb-1.5">
        <span className="text-muted-foreground">{icon}</span>
        {label}
      </label>
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (Number.isFinite(n)) onChange(n);
        }}
        disabled={disabled}
        className="w-full px-3 py-1.5 bg-background border border-border rounded-md text-xs text-foreground font-mono focus:outline-none focus:border-primary transition"
      />
      <p className="text-[10px] text-muted-foreground mt-1 leading-tight">{hint}</p>
    </div>
  );
}

function ToggleRow({
  icon,
  label,
  hint,
  checked,
  onChange,
  disabled,
}: {
  icon: React.ReactNode;
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div
      className={`flex items-start justify-between gap-4 p-3 rounded-md border border-border/60 bg-background/50 ${
        disabled ? "opacity-50 pointer-events-none" : ""
      }`}
    >
      <div className="flex items-start gap-2.5 min-w-0">
        <span className="text-muted-foreground mt-0.5 shrink-0">{icon}</span>
        <div className="min-w-0">
          <div className="text-xs font-semibold text-foreground">{label}</div>
          <p className="text-[10px] text-muted-foreground mt-0.5 leading-tight">{hint}</p>
        </div>
      </div>
      <button
        type="button"
        onClick={() => onChange(!checked)}
        disabled={disabled}
        className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none disabled:cursor-not-allowed ${
          checked ? "bg-green-500" : "bg-muted-foreground/40"
        }`}
      >
        <span
          className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-md ring-0 transition duration-200 ease-in-out ${
            checked ? "translate-x-4" : "translate-x-0"
          }`}
        />
      </button>
    </div>
  );
}

export default MCPTab;
