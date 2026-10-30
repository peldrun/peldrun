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
