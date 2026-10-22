// frontend\src\app\stores\_modals\ToolSettingsModal.tsx
"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Save,
  X,
  XCircle,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "../_hooks/useStoreData";

// ── Schema types (subset used by current manifests) ───────────────

interface JSONSchemaProperty {
  type?: "string" | "integer" | "number" | "boolean" | "object" | "array";
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  enumNames?: string[];
  minimum?: number;
  maximum?: number;
  properties?: Record<string, JSONSchemaProperty>;
  items?: JSONSchemaProperty;
  additionalProperties?: boolean;
}

interface JSONSchema {
  type?: string;
  properties?: Record<string, JSONSchemaProperty>;
  required?: string[];
  additionalProperties?: boolean;
}

type FormValues = Record<string, unknown>;

interface EnvironmentCheck {
  name: string;
  label?: string;
  passed: boolean;
  message: string;
  hint?: string;
  note?: string;
  path?: string;
  resolved_example?: string;
}

interface EnvironmentReport {
  ready: boolean;
  checks: EnvironmentCheck[];
  tool_id?: string;
}

// ── Helpers ────────────────────────────────────────────────────────

function humanizeKey(key: string): string {
  return key.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Return the effective default for a property, recursing into nested
 * object schemas so nested defaults are pre-filled in the form.
 */
function getDefaultFor(prop: JSONSchemaProperty | undefined): unknown {
  if (!prop) return undefined;
  if (prop.default !== undefined) return prop.default;
  if (prop.type === "object" && prop.properties) {
    const obj: Record<string, unknown> = {};
    for (const [k, sub] of Object.entries(prop.properties)) {
      const d = getDefaultFor(sub);
      if (d !== undefined) obj[k] = d;
    }
    return Object.keys(obj).length > 0 ? obj : undefined;
  }
  return undefined;
}

/**
 * Coerce a raw form value into the type declared by the schema.
 * Arrays may arrive as newline-separated strings (from textareas).
 */
function coerceValue(prop: JSONSchemaProperty | undefined, raw: unknown): unknown {
  if (!prop) return raw;

  if (prop.type === "object" && prop.properties) {
    const obj = (raw || {}) as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const [k, sub] of Object.entries(prop.properties)) {
      if (k in obj) out[k] = coerceValue(sub, obj[k]);
    }
    return out;
  }

  if (prop.type === "array") {
    if (typeof raw === "string") {
      const lines = raw.split("\n").map((s) => s.trim()).filter(Boolean);
      const itemType = prop.items?.type;
      if (itemType === "integer")
        return lines
          .map((s) => Number.parseInt(s, 10))
          .filter((n) => Number.isFinite(n));
      if (itemType === "number")
        return lines
          .map((s) => Number.parseFloat(s))
          .filter((n) => Number.isFinite(n));
      return lines;
    }
    return Array.isArray(raw) ? raw : [];
  }

  if (prop.type === "integer") {
    const n = Number(raw);
    return Number.isFinite(n) ? Math.trunc(n) : raw;
  }
  if (prop.type === "number") {
    const n = Number(raw);
    return Number.isFinite(n) ? n : raw;
  }
  if (prop.type === "boolean") return Boolean(raw);
  return raw;
}

// ── Props ──────────────────────────────────────────────────────────

interface ToolSettingsModalProps {
  open: boolean;
  toolId: string | null;
  toolName: string;
  onClose: () => void;
  onSaved?: (values: Record<string, unknown>) => void;
}

// ── Component ──────────────────────────────────────────────────────

export function ToolSettingsModal({
  open,
  toolId,
  toolName,
  onClose,
  onSaved,
}: ToolSettingsModalProps) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [schema, setSchema] = useState<JSONSchema | null>(null);
  const [values, setValues] = useState<FormValues>({});
  const [error, setError] = useState<string | null>(null);

  // Environment test state (currently meaningful for browser_interactive).
  const [testing, setTesting] = useState(false);
  const [testReport, setTestReport] = useState<EnvironmentReport | null>(null);

  // Fetch schema + values on open
  useEffect(() => {
    if (!open || !toolId) return;
    let cancelled = false;

    setLoading(true);
    setError(null);
    setSchema(null);
    setValues({});
    setTestReport(null);
    setTesting(false);

    (async () => {
      try {
        const res = await fetch(
          getApiUrl(`/api/store/tools/${toolId}/settings`)
        );
        if (cancelled) return;

        if (res.status === 404) {
          setError("This tool has no configurable settings.");
          return;
        }
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          setError(
            err.detail || `Failed to load settings (HTTP ${res.status}).`
          );
          return;
        }

        const data = await res.json();
        const loadedSchema: JSONSchema | null = data.schema || null;
        const loadedValues: FormValues = data.values || {};

        // Pre-fill any missing top-level defaults (esp. nested objects).
        const merged: FormValues = { ...loadedValues };
        if (loadedSchema?.properties) {
          for (const [k, prop] of Object.entries(loadedSchema.properties)) {
            if (!(k in merged)) {
              const d = getDefaultFor(prop);
              if (d !== undefined) merged[k] = d;
            }
          }
        }

        setSchema(loadedSchema);
        setValues(merged);
      } catch (exc: any) {
        if (cancelled) return;
        setError(exc?.message || "Network error while loading settings.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, toolId]);

  // Escape key closes the modal
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, saving, onClose]);

  // Prevent body scroll while open
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  const handleFieldChange = useCallback((key: string, raw: unknown) => {
    setValues((prev) => ({ ...prev, [key]: raw }));
  }, []);

  /**
   * Probe the tool's runtime environment and surface the result inside
   * the modal. Currently the backend implements a meaningful probe for
   * ``browser_interactive`` (Playwright / Chromium / screenshot dir);
   * other tools return a benign "no checks" report.
   */
  const handleTestEnvironment = useCallback(async () => {
    if (!toolId) return;
    setTesting(true);
    setTestReport(null);
    try {
      const res = await fetch(
        getApiUrl(`/api/store/tools/${toolId}/test-environment`)
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setTestReport({
          ready: false,
          checks: [
            {
              name: "http_error",
              label: "Environment probe",
              passed: false,
              message:
                typeof body.detail === "string"
                  ? body.detail
                  : `HTTP ${res.status}`,
            },
          ],
        });
        return;
      }
      const data: EnvironmentReport = await res.json();
      setTestReport(data);
    } catch (exc: any) {
      setTestReport({
        ready: false,
        checks: [
          {
            name: "network_error",
            label: "Environment probe",
            passed: false,
            message: exc?.message || "Network error while testing.",
          },
        ],
      });
    } finally {
      setTesting(false);
    }
  }, [toolId]);

  const handleSubmit = useCallback(async () => {
    if (!toolId) return;
    setSaving(true);
    setError(null);

    try {
      const payload: FormValues = {};
      for (const [key, prop] of Object.entries(schema?.properties || {})) {
        if (key in values) payload[key] = coerceValue(prop, values[key]);
      }

      const res = await fetch(
        getApiUrl(`/api/store/tools/${toolId}/settings`),
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }
      );

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const detail =
          typeof err.detail === "string"
            ? err.detail
            : "Validation failed. Check the values and try again.";
        setError(detail);
        showToast.error("Settings Rejected", detail);
        return;
      }

      const data = await res.json();
      showToast.success("Settings Saved", `${toolName} updated.`);
      onSaved?.(data.values || payload);
      onClose();
    } catch (exc: any) {
      const msg = exc?.message || "Network error while saving.";
      setError(msg);
      showToast.error("Save Failed", msg);
    } finally {
      setSaving(false);
    }
  }, [toolId, schema, values, toolName, onSaved, onClose]);

  if (!open) return null;

  const hasFields = !!(
    schema?.properties && Object.keys(schema.properties).length > 0
  );
  const canSave = !saving && !loading && hasFields;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="tool-settings-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !saving) onClose();
      }}
    >
      <div className="bg-card border border-border rounded-lg shadow-2xl w-full max-w-2xl max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0">
          <div className="min-w-0">
            <h2
              id="tool-settings-title"
              className="text-base font-bold text-foreground font-mono"
            >
              Tool Settings
            </h2>
            <p className="text-xs text-muted-foreground mt-0.5 truncate">
              {toolName}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-50 transition"
            aria-label="Close settings"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 scrollbar-thin">
          {loading && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 size={20} className="animate-spin mr-2" />
              <span className="text-sm">Loading settings…</span>
            </div>
          )}

          {!loading && error && !schema && (
            <div className="flex items-start gap-3 p-4 bg-destructive/10 border border-destructive/30 rounded-sm">
              <AlertTriangle
                size={18}
                className="text-destructive shrink-0 mt-0.5"
              />
              <div className="text-sm text-foreground">{error}</div>
            </div>
          )}

          {!loading && schema && !hasFields && (
            <p className="text-sm text-muted-foreground py-8 text-center">
              This tool has no configurable settings.
            </p>
          )}

          {!loading && schema && hasFields && (
            <form
              className="space-y-5"
              onSubmit={(e) => {
                e.preventDefault();
                if (canSave) handleSubmit();
              }}
            >
              {Object.entries(schema.properties!).map(([key, prop]) => (
                <FieldRow
                  key={key}
                  fieldKey={key}
                  prop={prop}
                  value={values[key]}
                  onChange={handleFieldChange}
                />
              ))}

              {error && (
                <div className="flex items-start gap-3 p-3 bg-destructive/10 border border-destructive/30 rounded-sm">
                  <AlertTriangle
                    size={14}
                    className="text-destructive shrink-0 mt-0.5"
                  />
                  <div className="text-xs text-foreground">{error}</div>
                </div>
              )}
            </form>
          )}

          {/* Environment test result */}
          {testReport && (
            <div className="mt-5 border border-border rounded-sm overflow-hidden">
              <div
                className={`px-4 py-2 flex items-center gap-2 text-xs font-semibold ${
                  testReport.ready
                    ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-b border-emerald-500/30"
                    : "bg-destructive/10 text-destructive border-b border-destructive/30"
                }`}
              >
                {testReport.ready ? (
                  <>
                    <CheckCircle2 size={14} />
                    Environment ready
                  </>
                ) : (
                  <>
                    <XCircle size={14} />
                    Environment has issues
                  </>
                )}
              </div>
              <ul className="divide-y divide-border">
                {testReport.checks.map((c) => (
                  <li
                    key={c.name}
                    className="px-4 py-3 flex items-start gap-2"
                  >
                    {c.passed ? (
                      <CheckCircle2
                        size={14}
                        className="text-emerald-600 dark:text-emerald-400 shrink-0 mt-0.5"
                      />
                    ) : (
                      <XCircle
                        size={14}
                        className="text-destructive shrink-0 mt-0.5"
                      />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-medium text-foreground">
                        {c.label || c.name}
                      </div>
                      <div className="text-[11px] text-muted-foreground mt-0.5">
                        {c.message}
                      </div>
                      {c.note && (
                        <div className="text-[11px] text-muted-foreground/80 mt-1 italic">
                          {c.note}
                        </div>
                      )}
                      {!c.passed && c.hint && (
                        <pre className="text-[11px] text-foreground bg-muted/40 border border-border rounded-sm p-2 mt-1.5 whitespace-pre-wrap font-mono">
                          {c.hint}
                        </pre>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-border flex items-center justify-between gap-2 shrink-0">
          <button
            type="button"
            onClick={handleTestEnvironment}
            disabled={testing || loading || !toolId}
            className="inline-flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground hover:text-foreground border border-border rounded-sm hover:bg-muted/40 disabled:opacity-50 transition"
          >
            {testing ? (
              <>
                <Loader2 size={14} className="animate-spin" />
                Testing…
              </>
            ) : (
              <>
                <Activity size={14} />
                Test Environment
              </>
            )}
          </button>
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
            onClick={handleSubmit}
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
                Save Settings
              </>
            )}
          </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Field renderer ─────────────────────────────────────────────────

function FieldRow({
  fieldKey,
  prop,
  value,
  onChange,
}: {
  fieldKey: string;
  prop: JSONSchemaProperty;
  value: unknown;
  onChange: (key: string, raw: unknown) => void;
}) {
  const label = prop.title || humanizeKey(fieldKey);
  const description = prop.description;
  const baseInputClass =
    "w-full px-3 py-2 bg-background border border-border rounded-sm text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary transition";

  // Nested object
  if (prop.type === "object" && prop.properties) {
    const nested = (value as Record<string, unknown>) || {};
    return (
      <fieldset className="border border-border rounded-sm p-3 space-y-3">
        <legend className="text-xs font-semibold text-foreground px-1">
          {label}
        </legend>
        {description && (
          <p className="text-[11px] text-muted-foreground -mt-1">
            {description}
          </p>
        )}
        {Object.entries(prop.properties).map(([k, sub]) => (
          <FieldRow
            key={k}
            fieldKey={k}
            prop={sub}
            value={nested[k]}
            onChange={(nestedKey, raw) => {
              onChange(fieldKey, { ...nested, [nestedKey]: raw });
            }}
          />
        ))}
      </fieldset>
    );
  }

  // Enum (select)
  if (Array.isArray(prop.enum)) {
    return (
      <div>
        <label className="block text-xs font-semibold text-foreground mb-1">
          {label}
        </label>
        <select
          className={baseInputClass}
          value={String(value ?? prop.default ?? "")}
          onChange={(e) => onChange(fieldKey, e.target.value)}
        >
          {prop.enum.map((opt, i) => (
            <option key={`${String(opt)}-${i}`} value={String(opt)}>
              {prop.enumNames?.[i] ?? String(opt)}
            </option>
          ))}
        </select>
        {description && (
          <p className="text-[11px] text-muted-foreground mt-1">
            {description}
          </p>
        )}
      </div>
    );
  }

  // Boolean
  if (prop.type === "boolean") {
    return (
      <div>
        <label className="flex items-center gap-3 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={Boolean(value ?? prop.default ?? false)}
            onChange={(e) => onChange(fieldKey, e.target.checked)}
            className="h-4 w-4 rounded border-border text-primary focus:ring-primary cursor-pointer"
          />
          <span className="text-sm font-medium text-foreground">{label}</span>
        </label>
        {description && (
          <p className="text-[11px] text-muted-foreground mt-1 ml-7">
            {description}
          </p>
        )}
      </div>
    );
  }

  // Integer / Number
  if (prop.type === "integer" || prop.type === "number") {
    const rangeHint =
      prop.minimum !== undefined || prop.maximum !== undefined
        ? ` (${prop.minimum ?? "−∞"} – ${prop.maximum ?? "∞"})`
        : "";
    return (
      <div>
        <label className="block text-xs font-semibold text-foreground mb-1">
          {label}
          <span className="ml-1 text-muted-foreground font-normal">
            {rangeHint}
          </span>
        </label>
        <input
          type="number"
          className={baseInputClass}
          value={String(value ?? prop.default ?? "")}
          min={prop.minimum}
          max={prop.maximum}
          step={prop.type === "integer" ? 1 : "any"}
          onChange={(e) => onChange(fieldKey, e.target.value)}
        />
        {description && (
          <p className="text-[11px] text-muted-foreground mt-1">
            {description}
          </p>
        )}
      </div>
    );
  }

  // Array (one item per line)
  if (prop.type === "array") {
    const strValue =
      typeof value === "string"
        ? value
        : Array.isArray(value)
          ? value.map((x) => String(x)).join("\n")
          : Array.isArray(prop.default)
            ? (prop.default as unknown[]).map((x) => String(x)).join("\n")
            : "";
    return (
      <div>
        <label className="block text-xs font-semibold text-foreground mb-1">
          {label}
        </label>
        <textarea
          rows={3}
          className={baseInputClass}
          value={strValue}
          onChange={(e) => onChange(fieldKey, e.target.value)}
          placeholder="One item per line"
        />
        <p className="text-[11px] text-muted-foreground mt-1">
          {description || "One item per line."}
        </p>
      </div>
    );
  }

  // String (default)
  return (
    <div>
      <label className="block text-xs font-semibold text-foreground mb-1">
        {label}
      </label>
      <input
        type="text"
        className={baseInputClass}
        value={String(value ?? prop.default ?? "")}
        onChange={(e) => onChange(fieldKey, e.target.value)}
      />
      {description && (
        <p className="text-[11px] text-muted-foreground mt-1">
          {description}
        </p>
      )}
    </div>
  );
}

export default ToolSettingsModal;
