// Path: frontend/src/app/settings/runtime/page.tsx
"use client";

/**
 * Runtime Settings Page — standalone route `/settings/runtime`.
 *
 * Manages the two runtime-configurable settings groups that are NOT
 * already covered by other settings pages:
 *
 *   * Cleanup      — temp artifacts + checkpoint cache sweeper
 *   * Streaming    — per-token flush tuning for Agent mode
 *
 * LLM provider endpoints (LM Studio / Ollama / defaults) are deliberately
 * EXCLUDED here. They are owned by the Models page (see
 * `frontend/src/app/settings/page.tsx`), and duplicating those fields
 * would create two sources of truth for the same values. An info card
 * at the bottom of the page links back to the Models settings.
 *
 * UX pattern
 * ----------
 * Mirrors `SearchTab.tsx`: debounced auto-save (800 ms), manual Save
 * button, per-field source badge (file / env:VAR / default), inline
 * validation, and a "Danger Zone" with Reset + Run Now actions.
 */

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import {
  Save,
  Loader2,
  AlertCircle,
  CheckCircle2,
  XCircle,
  RefreshCw,
  RotateCcw,
  Play,
  Clock,
  Timer,
  Gauge,
  HardDrive,
  Info,
  ExternalLink,
  Activity,
  Zap,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/ToastNotification";

// ---------------------------------------------------------------------------
// Types — mirror the backend response shape
// ---------------------------------------------------------------------------

type SaveState = "idle" | "dirty" | "saving" | "saved" | "error";

interface CleanupValues {
  enabled: boolean;
  interval_hours: number;
  max_age_hours: number;
}

interface StreamingValues {
  flush_interval_seconds: number;
  flush_chars: number;
}

interface TimeoutValues {
  llm_request_timeout_seconds: number;
  step_timeout_seconds: number;
  total_timeout_seconds: number;
}

interface SettingsSchemaField {
  label: string;
  type: "boolean" | "number" | "url" | "password";
  description: string;
  min?: number;
  max?: number;
  step?: number;
}

type SourceTier = "file" | "default" | string; // "env:VAR" also matches

// NOTE: The backend response may still carry an `llm_providers` section
// on legacy deployments. It is intentionally NOT part of this interface:
// LLM provider configuration is owned by the dedicated Models settings
// page, and this page never reads or writes those fields. TypeScript's
// structural typing lets us ignore any extra fields at the boundary.
interface RuntimeSettingsResponse {
  values: {
    timeouts: TimeoutValues;
    cleanup: CleanupValues;
    streaming: StreamingValues;
  };
  sources: {
    timeouts: Record<string, SourceTier>;
    cleanup: Record<string, SourceTier>;
    streaming: Record<string, SourceTier>;
  };
  defaults: {
    timeouts: TimeoutValues;
    cleanup: CleanupValues;
    streaming: StreamingValues;
  };
  schema: {
    timeouts: Record<string, SettingsSchemaField>;
    cleanup: Record<string, SettingsSchemaField>;
    streaming: Record<string, SettingsSchemaField>;
  };
  env_vars: Record<string, Record<string, string>>;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

const ENDPOINT = "/api/config/runtime-settings";

// ---------------------------------------------------------------------------
// Source badge — shows where the effective value came from
// ---------------------------------------------------------------------------

function SourceBadge({ source }: { source: SourceTier }) {
  if (source === "file") {
    return (
      <span
        className="text-[9px] font-mono px-1.5 py-0.5 rounded border border-primary/40 bg-primary/10 text-primary"
        title="Value saved from this page — overrides env var and default"
      >
        saved
      </span>
    );
  }
  if (typeof source === "string" && source.startsWith("env:")) {
    return (
      <span
        className="text-[9px] font-mono px-1.5 py-0.5 rounded border border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
        title={`Environment variable ${source.slice(4)} is currently overriding the default. Save a value here to override the env var.`}
      >
        {source}
      </span>
    );
  }
  return (
    <span
      className="text-[9px] font-mono px-1.5 py-0.5 rounded border border-border bg-muted/60 text-muted-foreground"
      title="Hardcoded default — no file or env override active"
    >
      default
    </span>
  );
}

// ---------------------------------------------------------------------------
// Save indicator — mirrors SearchTab's pattern
// ---------------------------------------------------------------------------

function SaveIndicator({
  state,
  lastSavedAt,
  errorMsg,
}: {
  state: SaveState;
  lastSavedAt: Date | null;
  errorMsg?: string | null;
}) {
  const map: Record<
    SaveState,
    { label: string; cls: string; icon: React.ReactNode }
  > = {
    idle: {
      label: lastSavedAt
        ? `Saved ${lastSavedAt.toLocaleTimeString()}`
        : "Auto-save on",
      cls: "text-muted-foreground bg-muted/40",
      icon: <CheckCircle2 size={11} />,
    },
    dirty: {
      label: "Unsaved changes",
      cls: "text-amber-600 dark:text-amber-400 border-amber-500/40 bg-amber-500/10",
      icon: <AlertCircle size={11} />,
    },
    saving: {
      label: "Saving...",
      cls: "text-primary border-primary/40 bg-primary/10",
      icon: <Loader2 size={11} className="animate-spin" />,
    },
    saved: {
      label: "Confirmed saved",
      cls: "text-emerald-600 dark:text-emerald-400 border-emerald-500/40 bg-emerald-500/10",
      icon: <CheckCircle2 size={11} />,
    },
    error: {
      label: errorMsg || "Save failed",
      cls: "text-rose-600 dark:text-rose-400 border-rose-500/40 bg-rose-500/10",
      icon: <XCircle size={11} />,
    },
  };
  const cfg = map[state];
  return (
    <motion.span
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      className={`hidden sm:flex items-center gap-1.5 text-[10px] font-mono px-2 py-1 rounded-md border border-transparent ${cfg.cls}`}
    >
      {cfg.icon}
      <span>{cfg.label}</span>
    </motion.span>
  );
}

// ---------------------------------------------------------------------------
// Summary card — same visual language as SearchTab's SummaryCard
// ---------------------------------------------------------------------------

function SummaryCard({
  label,
  value,
  icon,
  accent,
  subtitle,
  delay = 0,
}: {
  label: string;
  value: React.ReactNode;
  icon: React.ReactNode;
  accent: string;
  subtitle: React.ReactNode;
  delay?: number;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, delay, ease: [0.16, 1, 0.3, 1] }}
      whileHover={{ y: -4, transition: { duration: 0.2 } }}
      className="group relative p-5 rounded-md border border-card bg-card overflow-hidden shadow-sm hover:shadow-lg hover:border-primary/30 transition-all"
    >
      <div
        className="absolute top-0 left-0 right-0 h-[1px] opacity-30 group-hover:opacity-100 transition-opacity"
        style={{
          background: `linear-gradient(90deg, transparent 0%, ${accent} 50%, transparent 100%)`,
        }}
      />
      <div
        className="absolute -top-10 -right-10 w-32 h-32 rounded-full opacity-[0.08] group-hover:opacity-[0.15] transition-opacity blur-2xl"
        style={{ backgroundColor: accent }}
      />
      <div className="relative flex items-start justify-between">
        <div className="flex-1 space-y-3 min-w-0">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block">
            {label}
          </span>
          <div
            className="text-xl lg:text-2xl font-bold font-mono tabular-nums truncate"
            style={{ color: accent }}
          >
            {value}
          </div>
          <div
            className="text-[11px] text-muted-foreground truncate"
            title={typeof subtitle === "string" ? subtitle : undefined}
          >
            {subtitle}
          </div>
        </div>
        <div className="p-2.5 rounded-sm shrink-0 ml-2" style={{ color: accent }}>
          {icon}
        </div>
      </div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Numeric field with source badge, validation, and min/max hints
// ---------------------------------------------------------------------------

function NumberField({
  id,
  label,
  description,
  value,
  onChange,
  min,
  max,
  step,
  source,
  error,
  warning,
  unit,
}: {
  id: string;
  label: string;
  description: string;
  value: number | "";
  onChange: (v: number | "") => void;
  min?: number;
  max?: number;
  step?: number;
  source: SourceTier;
  error?: string | null;
  warning?: string | null;
  unit?: string;
}) {
  return (
    <div
      className={`p-3 rounded-sm border transition-colors ${
        error
          ? "border-rose-500/60 bg-rose-500/5"
          : "border-border/60 bg-card hover:border-muted/30 hover:shadow-sm"
      }`}
    >
      <div className="flex items-center justify-between mb-1.5 gap-2">
        <label
          htmlFor={id}
          className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground"
        >
          {label}
        </label>
        <SourceBadge source={source} />
      </div>
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="number"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              onChange("");
              return;
            }
            const parsed = parseFloat(raw);
            onChange(Number.isNaN(parsed) ? "" : parsed);
          }}
          aria-invalid={Boolean(error)}
          className="flex-1 bg-transparent border-0 outline-none text-xs text-foreground font-mono focus:ring-0 p-1 hover:bg-muted rounded"
        />
        {unit && (
          <span className="text-[10px] font-mono text-muted-foreground shrink-0">
            {unit}
          </span>
        )}
      </div>
      <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
        {description}
      </p>
      {error && (
        <p
          role="alert"
          className="text-[10px] font-medium text-rose-600 dark:text-rose-400 flex items-center gap-1.5 mt-1"
        >
          <AlertCircle size={11} />
          <span>{error}</span>
        </p>
      )}
      {warning && !error && (
        <p
          role="status"
          className="text-[10px] font-medium text-amber-600 dark:text-amber-400 flex items-start gap-1.5 mt-1"
        >
          <AlertCircle size={11} className="shrink-0 mt-0.5" />
          <span>{warning}</span>
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Boolean toggle
// ---------------------------------------------------------------------------

function BooleanField({
  id,
  label,
  description,
  value,
  onChange,
  source,
}: {
  id: string;
  label: string;
  description: string;
  value: boolean;
  onChange: (v: boolean) => void;
  source: SourceTier;
}) {
  return (
    <div className="p-3 rounded-sm border border-border/60 bg-card hover:border-muted/30 hover:shadow-sm transition-colors">
      <div className="flex items-center justify-between mb-2 gap-2">
        <label
          htmlFor={id}
          className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground"
        >
          {label}
        </label>
        <SourceBadge source={source} />
      </div>
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={value}
        onClick={() => onChange(!value)}
        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors cursor-pointer ${
          value ? "bg-emerald-500" : "bg-muted"
        }`}
      >
        <span
          className={`inline-block h-4 w-4 transform rounded-full bg-white shadow-sm transition-transform ${
            value ? "translate-x-6" : "translate-x-1"
          }`}
        />
      </button>
      <p className="text-[10px] text-muted-foreground mt-2 leading-snug">
        {description}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function RuntimeSettingsPage() {
  const [values, setValues] = useState<{
    timeouts: TimeoutValues;
    cleanup: CleanupValues;
    streaming: StreamingValues;
  } | null>(null);
  const [sources, setSources] = useState<{
    timeouts: Record<string, SourceTier>;
    cleanup: Record<string, SourceTier>;
    streaming: Record<string, SourceTier>;
  } | null>(null);
  const [schema, setSchema] = useState<{
    timeouts: Record<string, SettingsSchemaField>;
    cleanup: Record<string, SettingsSchemaField>;
    streaming: Record<string, SettingsSchemaField>;
  } | null>(null);
  const [defaults, setDefaults] = useState<{
    timeouts: TimeoutValues;
    cleanup: CleanupValues;
    streaming: StreamingValues;
  } | null>(null);

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveErrorMsg, setSaveErrorMsg] = useState<string | null>(null);
  const [lastSavedAt, setLastSavedAt] = useState<Date | null>(null);

  const [cleanupRunning, setCleanupRunning] = useState(false);
  const [cleanupReport, setCleanupReport] = useState<Record<string, unknown> | null>(null);
  const [resetting, setResetting] = useState(false);

  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isFirstRender = useRef(true);
  const lastSnapshotRef = useRef<string>("");

  // -------------------------------------------------------------
  // Load settings on mount
  // -------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(ENDPOINT, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: RuntimeSettingsResponse = await res.json();
        if (cancelled) return;

        const initial = {
          timeouts: {
            llm_request_timeout_seconds: Number(
              data.values.timeouts.llm_request_timeout_seconds
            ),
            step_timeout_seconds: Number(
              data.values.timeouts.step_timeout_seconds
            ),
            total_timeout_seconds: Number(
              data.values.timeouts.total_timeout_seconds
            ),
          },
          cleanup: {
            enabled: Boolean(data.values.cleanup.enabled),
            interval_hours: Number(data.values.cleanup.interval_hours),
            max_age_hours: Number(data.values.cleanup.max_age_hours),
          },
          streaming: {
            flush_interval_seconds: Number(
              data.values.streaming.flush_interval_seconds
            ),
            flush_chars: Number(data.values.streaming.flush_chars),
          },
        };
        setValues(initial);
        setSources({
          timeouts: data.sources.timeouts,
          cleanup: data.sources.cleanup,
          streaming: data.sources.streaming,
        });
        setSchema({
          timeouts: data.schema.timeouts,
          cleanup: data.schema.cleanup,
          streaming: data.schema.streaming,
        });
        setDefaults({
          timeouts: data.defaults.timeouts,
          cleanup: data.defaults.cleanup,
          streaming: data.defaults.streaming,
        });
        lastSnapshotRef.current = JSON.stringify(initial);
        setLoadError(null);
      } catch (err: unknown) {
        if (!cancelled) {
          setLoadError(
            `Failed to load runtime settings (${err instanceof Error ? err.message : "network error"}). The page will reload on next visit.`
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // -------------------------------------------------------------
  // Validation (numeric ranges from schema)
  // -------------------------------------------------------------
  const validationErrors = useMemo<Record<string, string>>(() => {
    if (!values || !schema) return {};
    const errs: Record<string, string> = {};

    const timeoutFields: Array<keyof TimeoutValues> = [
      "llm_request_timeout_seconds",
      "step_timeout_seconds",
      "total_timeout_seconds",
    ];
    for (const field of timeoutFields) {
      const v = values.timeouts[field];
      const s = schema.timeouts[field as string];
      if (typeof v === "number" && s) {
        if (s.min !== undefined && v < s.min) {
          errs[`timeouts.${field}`] = `${s.label} must be ≥ ${s.min}s.`;
        } else if (s.max !== undefined && v > s.max) {
          errs[`timeouts.${field}`] = `${s.label} must be ≤ ${s.max}s.`;
        }
      }
    }

    const cleanupFields: Array<keyof CleanupValues> = [
      "interval_hours",
      "max_age_hours",
    ];
    for (const field of cleanupFields) {
      const v = values.cleanup[field];
      const s = schema.cleanup[field as string];
      if (typeof v === "number" && s) {
        if (s.min !== undefined && v < s.min) {
          errs[`cleanup.${field}`] = `${s.label} must be ≥ ${s.min}.`;
        } else if (s.max !== undefined && v > s.max) {
          errs[`cleanup.${field}`] = `${s.label} must be ≤ ${s.max}.`;
        }
      }
    }

    const streamingFields: Array<keyof StreamingValues> = [
      "flush_interval_seconds",
      "flush_chars",
    ];
    for (const field of streamingFields) {
      const v = values.streaming[field];
      const s = schema.streaming[field as string];
      if (typeof v === "number" && s) {
        if (s.min !== undefined && v < s.min) {
          errs[`streaming.${field}`] = `${s.label} must be ≥ ${s.min}.`;
        } else if (s.max !== undefined && v > s.max) {
          errs[`streaming.${field}`] = `${s.label} must be ≤ ${s.max}.`;
        }
      }
    }
    return errs;
  }, [values, schema]);

  const hasValidationErrors = Object.keys(validationErrors).length > 0;

  // -----------------------------------------------------------------
  // Cross-field timeout warnings (non-blocking)
  // -----------------------------------------------------------------
  //
  // These are HINTS, not errors — the user is allowed to save any
  // combination, but the effective behavior may not match their
  // intent. This is the classic three-tier timeout trap: a lower-tier
  // ceiling that exceeds a higher-tier ceiling is dead in practice.
  //
  //   * llm_request_timeout  >  step_timeout  →  LLM request never
  //     completes because the step timeout fires first in agent mode.
  //   * step_timeout         >  total_timeout →  at most one step can
  //     run before the total ceiling fires, effectively skipping
  //     later steps entirely.
  // -----------------------------------------------------------------
  const timeoutWarnings = useMemo<Record<string, string>>(() => {
    if (!values) return {};
    const warn: Record<string, string> = {};
    const {
      llm_request_timeout_seconds,
      step_timeout_seconds,
      total_timeout_seconds,
    } = values.timeouts;

    // Collect warnings for the Step Timeout field. A Step Timeout is the
    // correct "action" field because it is the *containing* ceiling: the
    // user must either raise it or lower the LLM timeout to reconcile.
    const stepWarnings: string[] = [];

    if (
      typeof llm_request_timeout_seconds === "number" &&
      typeof step_timeout_seconds === "number" &&
      llm_request_timeout_seconds > step_timeout_seconds
    ) {
      stepWarnings.push(
        `The LLM Request Timeout is set to ${llm_request_timeout_seconds}s — higher than this Step Timeout (${step_timeout_seconds}s). In agent mode the step ceiling will fire first, making the LLM timeout effectively ignored. To reconcile: raise this Step Timeout to ≥ ${llm_request_timeout_seconds}s (if you want long LLM responses), OR lower the LLM Request Timeout to ≤ ${step_timeout_seconds}s (for faster fail-fast).`
      );
    }

    if (
      typeof step_timeout_seconds === "number" &&
      typeof total_timeout_seconds === "number" &&
      step_timeout_seconds > total_timeout_seconds
    ) {
      stepWarnings.push(
        `This Step Timeout (${step_timeout_seconds}s) exceeds the Total Run Timeout (${total_timeout_seconds}s). Only one step can complete before the run terminates. Raise Total Run Timeout to ≥ ${step_timeout_seconds}s, or lower this Step Timeout.`
      );
    }

    if (stepWarnings.length > 0) {
      warn["timeouts.step_timeout_seconds"] = stepWarnings.join(" ");
    }

    return warn;
  }, [values]);

  // -------------------------------------------------------------
  // Persist current values to backend
  // -------------------------------------------------------------
  const persist = useCallback(
    async (payload: {
      timeouts: TimeoutValues;
      cleanup: CleanupValues;
      streaming: StreamingValues;
    }) => {
      if (hasValidationErrors) {
        setSaveErrorMsg(Object.values(validationErrors)[0]);
        setSaveState("error");
        return;
      }
      setSaveState("saving");
      setSaveErrorMsg(null);
      try {
        const res = await fetch(ENDPOINT, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            timeouts: payload.timeouts,
            cleanup: payload.cleanup,
            streaming: payload.streaming,
          }),
        });
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}));
          throw new Error(detail?.detail || `HTTP ${res.status}`);
        }
        const fresh: RuntimeSettingsResponse = await res.json();
        setSources({
          timeouts: fresh.sources.timeouts,
          cleanup: fresh.sources.cleanup,
          streaming: fresh.sources.streaming,
        });
        setSaveState("saved");
        setLastSavedAt(new Date());
        // [TOAST] Notify the user that the change has taken effect.
        // Cleanup settings apply on the next sweeper cycle; streaming
        // settings apply on the next think() call — both without a
        // server restart.
        showToast.success(
          "Settings Saved",
          "Runtime settings updated and applied immediately."
        );
        setTimeout(
          () => setSaveState((s) => (s === "saved" ? "idle" : s)),
          4500
        );
      } catch (err: unknown) {
        const msg =
          err instanceof Error ? err.message : "Failed to save settings.";
        setSaveErrorMsg(msg);
        setSaveState("error");
        showToast.error("Save Failed", msg);
      }
    },
    [hasValidationErrors, validationErrors]
  );

  // -------------------------------------------------------------
  // Debounced auto-save on any edit
  // -------------------------------------------------------------
  useEffect(() => {
    if (!values) return;
    const snapshot = JSON.stringify(values);
    if (isFirstRender.current) {
      isFirstRender.current = false;
      lastSnapshotRef.current = snapshot;
      return;
    }
    if (snapshot === lastSnapshotRef.current) return;
    lastSnapshotRef.current = snapshot;

    if (hasValidationErrors) {
      setSaveErrorMsg(Object.values(validationErrors)[0]);
      setSaveState("error");
      return;
    }
    setSaveErrorMsg(null);
    setSaveState("dirty");

    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => persist(values), 800);
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, [values, hasValidationErrors, validationErrors, persist]);

  const handleManualSave = useCallback(async () => {
    if (!values) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    await persist(values);
  }, [values, persist]);

  // -------------------------------------------------------------
  // Trigger cleanup immediately
  // -------------------------------------------------------------
  const handleRunCleanupNow = useCallback(async () => {
    setCleanupRunning(true);
    setCleanupReport(null);
    try {
      const res = await fetch(`${ENDPOINT}/trigger-cleanup`, {
        method: "POST",
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      const report = (data.report || {}) as Record<string, unknown>;
      setCleanupReport(report);

      const scanned = Number(report.scanned ?? 0);
      const deleted = Number(report.deleted_count ?? 0);
      const kept = Number(report.kept_count ?? 0);
      const bytes = Number(report.deleted_bytes ?? 0);
      const maxAge = Number(report.threshold_hours ?? 0);
      const errors = Array.isArray(report.errors) ? report.errors : [];

      // [TOAST] Three distinct outcomes — the user must be told
      // *what actually happened*, not just that the request succeeded.
      if (errors.length > 0 && deleted === 0) {
        // Failure case first — if errors exist, surface them even
        // when nothing else happened.
        showToast.error(
          "Cleanup Encountered Errors",
          `${errors.length} file(s) could not be deleted. Check the report below.`
        );
      } else if (deleted > 0) {
        const mb = (bytes / (1024 * 1024)).toFixed(2);
        const kb = Math.round(bytes / 1024);
        const sizeLabel = bytes > 1024 * 1024 ? `${mb} MB` : `${kb} KB`;
        showToast.success(
          "Cleanup Complete",
          `Removed ${deleted} file(s) · ${sizeLabel} reclaimed · ${kept} kept (within ${maxAge}h window)`
        );
      } else if (scanned === 0) {
        showToast.info(
          "Nothing to Clean",
          "Both the temp store and checkpoint cache are already empty."
        );
      } else {
        // scanned > 0 but deleted = 0 — most common "false failure":
        // the files are all fresh. Explain the retention window.
        showToast.info(
          "Cleanup Complete — Nothing Deleted",
          `All ${scanned} file(s) are within the ${maxAge}h retention window. Increase "Max File Age" or wait for the next sweep.`
        );
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      showToast.error("Cleanup Failed", msg);
      setCleanupReport({ error: msg });
    } finally {
      setCleanupRunning(false);
      setTimeout(() => setCleanupReport(null), 15000);
    }
  }, []);

  // -------------------------------------------------------------
  // Reset all settings to defaults
  // -------------------------------------------------------------
  const handleReset = useCallback(async () => {
    if (
      !window.confirm(
        "Revert all runtime settings to their env/default values? This removes runtime_settings.json and cannot be undone."
      )
    ) {
      return;
    }
    setResetting(true);
    try {
      const res = await fetch(ENDPOINT, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: RuntimeSettingsResponse = await res.json();
      const fresh = {
        timeouts: {
          llm_request_timeout_seconds: Number(
            data.values.timeouts.llm_request_timeout_seconds
          ),
          step_timeout_seconds: Number(
            data.values.timeouts.step_timeout_seconds
          ),
          total_timeout_seconds: Number(
            data.values.timeouts.total_timeout_seconds
          ),
        },
        cleanup: {
          enabled: Boolean(data.values.cleanup.enabled),
          interval_hours: Number(data.values.cleanup.interval_hours),
          max_age_hours: Number(data.values.cleanup.max_age_hours),
        },
        streaming: {
          flush_interval_seconds: Number(
            data.values.streaming.flush_interval_seconds
          ),
          flush_chars: Number(data.values.streaming.flush_chars),
        },
      };
      setValues(fresh);
      setSources({
        timeouts: data.sources.timeouts,
        cleanup: data.sources.cleanup,
        streaming: data.sources.streaming,
      });
      lastSnapshotRef.current = JSON.stringify(fresh);
      setSaveState("idle");
      setLastSavedAt(null);
      showToast.success(
        "Settings Reset",
        "runtime_settings.json was deleted — env vars and hardcoded defaults are now active."
      );
    } catch (err: unknown) {
      const msg =
        err instanceof Error ? err.message : "Failed to reset settings.";
      setSaveErrorMsg(msg);
      setSaveState("error");
      showToast.error("Reset Failed", msg);
    } finally {
      setResetting(false);
    }
  }, []);

  // -------------------------------------------------------------
  // Update helpers
  // -------------------------------------------------------------
  const updateTimeouts = useCallback(
    <K extends keyof TimeoutValues>(key: K, value: TimeoutValues[K]) => {
      setValues((prev) =>
        prev ? { ...prev, timeouts: { ...prev.timeouts, [key]: value } } : prev
      );
    },
    []
  );

  const updateCleanup = useCallback(
    <K extends keyof CleanupValues>(key: K, value: CleanupValues[K]) => {
      setValues((prev) =>
        prev ? { ...prev, cleanup: { ...prev.cleanup, [key]: value } } : prev
      );
    },
    []
  );

  const updateStreaming = useCallback(
    <K extends keyof StreamingValues>(key: K, value: StreamingValues[K]) => {
      setValues((prev) =>
        prev ? { ...prev, streaming: { ...prev.streaming, [key]: value } } : prev
      );
    },
    []
  );

  // -------------------------------------------------------------
  // Render — loading state
  // -------------------------------------------------------------
  if (loading) {
    return (
      <div className="h-full w-full flex items-center justify-center bg-background text-foreground">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 size={16} className="animate-spin" />
          <span>Loading runtime settings...</span>
        </div>
      </div>
    );
  }

  if (loadError || !values) {
    return (
      <div className="h-full w-full flex items-center justify-center bg-background text-foreground p-6">
        <div className="max-w-md text-center space-y-3">
          <AlertCircle size={32} className="text-rose-500 mx-auto" />
          <h2 className="text-sm font-semibold">Could not load settings</h2>
          <p className="text-xs text-muted-foreground">{loadError}</p>
          <Button
            variant="outline"
            onClick={() => window.location.reload()}
            className="text-xs h-8"
          >
            <RefreshCw size={12} />
            <span>Retry</span>
          </Button>
        </div>
      </div>
    );
  }

  const cleanupActive = Boolean(values.cleanup.enabled);

  return (
    <div className="h-full w-full overflow-y-auto bg-background text-foreground font-sans flex flex-col relative">
      {/* Ambient background */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-40 -left-40 w-[500px] h-[500px] rounded-full bg-primary/5 blur-3xl" />
        <div className="absolute top-1/3 -right-40 w-[400px] h-[400px] rounded-full bg-emerald-500/5 blur-3xl" />
        <div className="absolute bottom-0 left-1/3 w-[450px] h-[450px] rounded-full bg-amber-500/5 blur-3xl" />
      </div>

      {/* Header */}
      <motion.header
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="border-b border-border bg-card/60 backdrop-blur-xl px-6 py-4 sticky top-0 z-20"
      >
        <div className="max-w-7xl mx-auto flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-2.5">
            <div className="p-2 rounded bg-primary/10">
              <Gauge size={16} className="text-primary" />
            </div>
            <div>
              <h1 className="text-sm font-semibold font-heading tracking-wide">
                Runtime Settings
              </h1>
              <p className="text-[10px] text-muted-foreground">
                Live-editable configuration · no server restart required
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <SaveIndicator
              state={saveState}
              lastSavedAt={lastSavedAt}
              errorMsg={saveErrorMsg}
            />
            <Button
              variant="save"
              onClick={handleManualSave}
              disabled={saveState === "saving" || hasValidationErrors}
              className="h-8 text-xs gap-1.5 cursor-pointer bg-rb text-[#fff] hover:opacity-90 disabled:opacity-50"
            >
              {saveState === "saving" ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Save size={12} />
              )}
              <span>{saveState === "saving" ? "Saving..." : "Save"}</span>
            </Button>
          </div>
        </div>
      </motion.header>

      <main className="max-w-7xl w-full mx-auto p-6 space-y-6 flex-1 relative">
        {/* Error banner */}
        {saveErrorMsg && (
          <div
            role="alert"
            className="flex items-start gap-3 p-3.5 rounded-md border border-rose-500/40 bg-rose-500/10 text-rose-600 dark:text-rose-400 text-xs"
          >
            <AlertCircle size={15} className="shrink-0 mt-0.5" />
            <div>{saveErrorMsg}</div>
          </div>
        )}

        {/* Summary KPIs */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <SummaryCard
            label="Cleanup Status"
            value={cleanupActive ? "Enabled" : "Disabled"}
            icon={
              cleanupActive ? (
                <Zap size={18} className="text-emerald-500" />
              ) : (
                <XCircle size={18} className="text-rose-500" />
              )
            }
            accent={cleanupActive ? "#10b981" : "#f43f5e"}
            subtitle={
              cleanupActive
                ? "Periodic sweep is active"
                : "No automatic cleanup is running"
            }
            delay={0}
          />
          <SummaryCard
            label="Sweep Interval"
            value={`${values.cleanup.interval_hours}h`}
            icon={<Clock size={18} className="text-primary" />}
            accent="#3b82f6"
            subtitle={
              values.cleanup.interval_hours < 1
                ? "Aggressive · runs sub-hourly"
                : `Runs every ${values.cleanup.interval_hours} hour(s)`
            }
            delay={0.05}
          />
          <SummaryCard
            label="Retention Window"
            value={`${values.cleanup.max_age_hours}h`}
            icon={<HardDrive size={18} className="text-cyan-500" />}
            accent="#06b6d4"
            subtitle="Files older than this are removed on the next sweep"
            delay={0.1}
          />
          <SummaryCard
            label="Streaming Cadence"
            value={`${values.streaming.flush_interval_seconds}s`}
            icon={<Timer size={18} className="text-amber-500" />}
            accent="#f59e0b"
            subtitle={`~${values.streaming.flush_chars} chars per SSE flush`}
            delay={0.15}
          />
        </div>

        {/* Cleanup section */}
        <motion.div
          {...FADE_UP}
          transition={{ ...FADE_UP.transition, delay: 0.2 }}
          className="relative p-5 rounded-md bg-gigbg shadow-sm space-y-5"
        >
          <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md bg-gradient-to-r from-transparent via-primary/60 to-transparent" />

          <div className="flex items-center gap-3">
            <div className="p-2 rounded bg-primary/10">
              <HardDrive size={16} className="text-primary" />
            </div>
            <div>
              <h3 className="text-md text-black dark:text-white font-semibold font-heading uppercase tracking-wider">
                Cleanup
              </h3>
              <p className="text-[11px] text-muted-foreground">
                Automatic sweeper for temp artifacts and checkpoint cache
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <BooleanField
              id="cleanup-enabled"
              label={schema?.cleanup.enabled?.label ?? "Enable Auto Cleanup"}
              description={
                schema?.cleanup.enabled?.description ??
                "Master switch for the periodic sweeper."
              }
              value={Boolean(values.cleanup.enabled)}
              onChange={(v) => updateCleanup("enabled", v)}
              source={sources?.cleanup.enabled ?? "default"}
            />

            <NumberField
              id="cleanup-interval"
              label={schema?.cleanup.interval_hours?.label ?? "Interval (hours)"}
              description={
                schema?.cleanup.interval_hours?.description ??
                "How often the sweeper runs."
              }
              value={values.cleanup.interval_hours}
              onChange={(v) =>
                updateCleanup("interval_hours", v === "" ? 0 : v)
              }
              min={schema?.cleanup.interval_hours?.min}
              max={schema?.cleanup.interval_hours?.max}
              step={schema?.cleanup.interval_hours?.step}
              source={sources?.cleanup.interval_hours ?? "default"}
              error={validationErrors["cleanup.interval_hours"]}
              unit="h"
            />

            <NumberField
              id="cleanup-max-age"
              label={schema?.cleanup.max_age_hours?.label ?? "Max File Age (hours)"}
              description={
                schema?.cleanup.max_age_hours?.description ??
                "Files older than this are removed on the next sweep."
              }
              value={values.cleanup.max_age_hours}
              onChange={(v) => updateCleanup("max_age_hours", v === "" ? 0 : v)}
              min={schema?.cleanup.max_age_hours?.min}
              max={schema?.cleanup.max_age_hours?.max}
              step={schema?.cleanup.max_age_hours?.step}
              source={sources?.cleanup.max_age_hours ?? "default"}
              error={validationErrors["cleanup.max_age_hours"]}
              unit="h"
            />
          </div>

          {/* Run cleanup now + latest report */}
          <div className="flex items-center gap-3 flex-wrap pt-3 border-t border-border/40">
            <Button
              variant="outline"
              onClick={handleRunCleanupNow}
              disabled={cleanupRunning}
              className="h-8 text-xs gap-1.5 cursor-pointer hover:bg-muted"
            >
              {cleanupRunning ? (
                <RefreshCw size={12} className="animate-spin" />
              ) : (
                <Play size={12} />
              )}
              <span>{cleanupRunning ? "Running..." : "Run Cleanup Now"}</span>
            </Button>
            <span className="text-[10px] text-muted-foreground">
              Uses the current retention window · safe to invoke repeatedly
            </span>

            <AnimatePresence>
              {cleanupReport && (
                <motion.div
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  className="w-full flex items-center gap-2 text-[10px] font-mono px-2 py-1 rounded bg-muted/40 border border-border/60 flex-wrap"
                >
                  {(() => {
                    const scanned = Number(cleanupReport.scanned ?? 0);
                    const deleted = Number(cleanupReport.deleted_count ?? 0);
                    const kept = Number(cleanupReport.kept_count ?? 0);
                    const bytes = Number(cleanupReport.deleted_bytes ?? 0);
                    const hasError = Boolean(cleanupReport.error);
                    const Icon = hasError ? XCircle : deleted > 0 ? CheckCircle2 : Info;
                    const color = hasError
                      ? "text-rose-500"
                      : deleted > 0
                      ? "text-emerald-500"
                      : "text-cyan-500";
                    return (
                      <>
                        <Icon size={11} className={color} />
                        {hasError ? (
                          <span className="text-rose-600 dark:text-rose-400">
                            {String(cleanupReport.error)}
                          </span>
                        ) : (
                          <span>
                            scanned {scanned} · deleted{" "}
                            <strong className={deleted > 0 ? "text-emerald-600 dark:text-emerald-400" : ""}>
                              {deleted}
                            </strong>{" "}
                            · kept {kept} · reclaimed{" "}
                            {bytes > 1024 * 1024
                              ? `${(bytes / (1024 * 1024)).toFixed(2)} MB`
                              : `${Math.round(bytes / 1024)} KB`}
                            {deleted === 0 && scanned > 0 && (
                              <span className="ml-1 text-muted-foreground">
                                (all within retention window)
                              </span>
                            )}
                          </span>
                        )}
                      </>
                    );
                  })()}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </motion.div>

        {/* Streaming section */}
        <motion.div
          {...FADE_UP}
          transition={{ ...FADE_UP.transition, delay: 0.3 }}
          className="relative p-5 rounded-md bg-gigbg shadow-sm space-y-5"
        >
          <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md bg-gradient-to-r from-transparent via-amber-500/60 to-transparent" />

          <div className="flex items-center gap-3">
            <div className="p-2 rounded bg-amber-500/10">
              <Activity size={16} className="text-amber-500" />
            </div>
            <div>
              <h3 className="text-md text-black dark:text-white font-semibold font-heading uppercase tracking-wider">
                Streaming
              </h3>
              <p className="text-[11px] text-muted-foreground">
                Per-token flush tuning for Agent mode · effective on the next LLM call
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <NumberField
              id="streaming-flush-interval"
              label={
                schema?.streaming.flush_interval_seconds?.label ??
                "Flush Interval (seconds)"
              }
              description={
                schema?.streaming.flush_interval_seconds?.description ??
                "Minimum delay between SSE flushes."
              }
              value={values.streaming.flush_interval_seconds}
              onChange={(v) =>
                updateStreaming("flush_interval_seconds", v === "" ? 0 : v)
              }
              min={schema?.streaming.flush_interval_seconds?.min}
              max={schema?.streaming.flush_interval_seconds?.max}
              step={schema?.streaming.flush_interval_seconds?.step}
              source={sources?.streaming.flush_interval_seconds ?? "default"}
              error={validationErrors["streaming.flush_interval_seconds"]}
              unit="s"
            />

            <NumberField
              id="streaming-flush-chars"
              label={
                schema?.streaming.flush_chars?.label ?? "Flush Characters"
              }
              description={
                schema?.streaming.flush_chars?.description ??
                "Minimum buffered characters before an SSE flush."
              }
              value={values.streaming.flush_chars}
              onChange={(v) =>
                updateStreaming("flush_chars", v === "" ? 0 : v)
              }
              min={schema?.streaming.flush_chars?.min}
              max={schema?.streaming.flush_chars?.max}
              step={schema?.streaming.flush_chars?.step}
              source={sources?.streaming.flush_chars ?? "default"}
              error={validationErrors["streaming.flush_chars"]}
              unit="chars"
            />
          </div>
        </motion.div>

        {/* Timeouts section — per-request, per-step, and per-run ceilings */}
        <motion.div
          {...FADE_UP}
          transition={{ ...FADE_UP.transition, delay: 0.35 }}
          className="relative p-5 rounded-md bg-gigbg shadow-sm space-y-5"
        >
          <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md bg-gradient-to-r from-transparent via-violet-500/60 to-transparent" />

          <div className="flex items-center gap-3">
            <div className="p-2 rounded bg-violet-500/10">
              <Clock size={16} className="text-violet-500" />
            </div>
            <div>
              <h3 className="text-md text-black dark:text-white font-semibold font-heading uppercase tracking-wider">
                Timeouts
              </h3>
              <p className="text-[11px] text-muted-foreground">
                Wall-clock ceilings for LLM requests, agent steps, and entire
                runs · effective immediately on the next run
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <NumberField
              id="timeout-llm-request"
              label={
                schema?.timeouts.llm_request_timeout_seconds?.label ??
                "LLM Request Timeout"
              }
              description={
                schema?.timeouts.llm_request_timeout_seconds?.description ??
                "HTTP timeout for each LLM provider request."
              }
              value={values.timeouts.llm_request_timeout_seconds}
              onChange={(v) =>
                updateTimeouts(
                  "llm_request_timeout_seconds",
                  v === "" ? 0 : v
                )
              }
              min={schema?.timeouts.llm_request_timeout_seconds?.min}
              max={schema?.timeouts.llm_request_timeout_seconds?.max}
              step={schema?.timeouts.llm_request_timeout_seconds?.step}
              source={
                sources?.timeouts.llm_request_timeout_seconds ?? "default"
              }
              error={
                validationErrors["timeouts.llm_request_timeout_seconds"]
              }
              unit="s"
            />

            <NumberField
              id="timeout-step"
              label={
                schema?.timeouts.step_timeout_seconds?.label ??
                "Step Timeout"
              }
              description={
                schema?.timeouts.step_timeout_seconds?.description ??
                "Timeout for one agent step (LLM + tools)."
              }
              value={values.timeouts.step_timeout_seconds}
              onChange={(v) =>
                updateTimeouts("step_timeout_seconds", v === "" ? 0 : v)
              }
              min={schema?.timeouts.step_timeout_seconds?.min}
              max={schema?.timeouts.step_timeout_seconds?.max}
              step={schema?.timeouts.step_timeout_seconds?.step}
              source={sources?.timeouts.step_timeout_seconds ?? "default"}
              error={validationErrors["timeouts.step_timeout_seconds"]}
              warning={timeoutWarnings["timeouts.step_timeout_seconds"]}
              unit="s"
            />

            <NumberField
              id="timeout-total"
              label={
                schema?.timeouts.total_timeout_seconds?.label ??
                "Total Run Timeout"
              }
              description={
                schema?.timeouts.total_timeout_seconds?.description ??
                "Wall-clock ceiling for the entire run."
              }
              value={values.timeouts.total_timeout_seconds}
              onChange={(v) =>
                updateTimeouts("total_timeout_seconds", v === "" ? 0 : v)
              }
              min={schema?.timeouts.total_timeout_seconds?.min}
              max={schema?.timeouts.total_timeout_seconds?.max}
              step={schema?.timeouts.total_timeout_seconds?.step}
              source={sources?.timeouts.total_timeout_seconds ?? "default"}
              error={validationErrors["timeouts.total_timeout_seconds"]}
              unit="s"
            />
          </div>
        </motion.div>

        {/* Info: LLM providers managed elsewhere */}
        <motion.div
          {...FADE_UP}
          transition={{ ...FADE_UP.transition, delay: 0.4 }}
          className="flex items-start gap-3 p-4 rounded-md border border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300 text-xs"
        >
          <Info size={16} className="shrink-0 mt-0.5" />
          <div className="space-y-1">
            <div className="font-semibold">LLM Provider Settings</div>
            <div className="opacity-90 text-[11px]">
              LM Studio URL, Ollama host, default API endpoint and default API key
              are managed from{" "}
              <Link
                href="/settings"
                className="underline font-medium hover:opacity-80 inline-flex items-center gap-1"
              >
                Models settings
                <ExternalLink size={10} />
              </Link>{" "}
              — not duplicated here to keep a single source of truth.
            </div>
          </div>
        </motion.div>

        {/* Danger zone */}
        <motion.div
          {...FADE_UP}
          transition={{ ...FADE_UP.transition, delay: 0.5 }}
          className="p-5 rounded-md border border-rose-500/30 bg-rose-500/5 space-y-3"
        >
          <div className="flex items-center gap-3">
            <div className="p-2 rounded bg-rose-500/10">
              <RotateCcw size={16} className="text-rose-500" />
            </div>
            <div>
              <h3 className="text-sm font-semibold text-rose-600 dark:text-rose-400 font-heading uppercase tracking-wider">
                Danger Zone
              </h3>
              <p className="text-[11px] text-muted-foreground">
                Irreversible actions — take effect immediately
              </p>
            </div>
          </div>

          <div className="flex items-center gap-3 flex-wrap">
            <Button
              variant="outline"
              onClick={handleReset}
              disabled={resetting}
              className="h-8 text-xs gap-1.5 cursor-pointer border-rose-500/40 text-rose-600 dark:text-rose-400 hover:bg-rose-500/10"
            >
              {resetting ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <RotateCcw size={12} />
              )}
              <span>
                {resetting ? "Resetting..." : "Reset to Defaults"}
              </span>
            </Button>
            <span className="text-[10px] text-muted-foreground">
              Deletes runtime_settings.json — env vars and hardcoded defaults
              take over again.
            </span>
          </div>
        </motion.div>
      </main>
    </div>
  );
}
