// frontend/src/app/stores/_modals/ProviderConfigureModal.tsx
"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  ChevronRight,
  Eye,
  EyeOff,
  FileArchive,
  Loader2,
  Plug,
  Plus,
  Save,
  Shield,
  Star,
  Trash2,
  X,
  XCircle,
  Zap,
} from "lucide-react";
import type {
  ConnectionFieldSpec,
  ProviderConnection,
  ProviderPackageEntry,
  ProviderTransport,
} from "../_types/store";
import type { SaveConnectionPayload } from "@/lib/providerApi";


// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ProviderConfigureModalProps {
  /** Whether the modal is visible. */
  open: boolean;
  /** The provider package being configured, or ``null`` when closed. */
  provider: ProviderPackageEntry | null;
  /** The saved connections for this provider. */
  connections: ProviderConnection[];
  /** True while loading the connection list. */
  isConnectionsLoading: boolean;
  /** Error from the last connection-list load, if any. */
  connectionsError: string | null;
  /**
   * ID of the connection currently being acted on. Used to show a
   * spinner on the exact row. ``"__new__"`` indicates a new connection
   * is being saved.
   */
  connectionActionId: string | null;
  /** Close the modal. No-op while an action is in flight. */
  onClose: () => void;
  /** Create a new connection. */
  onCreateConnection: (
    providerId: string,
    payload: SaveConnectionPayload,
  ) => Promise<ProviderConnection | null>;
  /** Update an existing connection. */
  onUpdateConnection: (
    providerId: string,
    connectionId: string,
    payload: SaveConnectionPayload,
  ) => Promise<ProviderConnection | null>;
  /** Delete a connection. */
  onDeleteConnection: (
    providerId: string,
    connectionId: string,
  ) => Promise<boolean>;
  /** Enable a connection. */
  onEnableConnection: (providerId: string, connectionId: string) => Promise<void>;
  /** Disable a connection. */
  onDisableConnection: (providerId: string, connectionId: string) => Promise<void>;
  /** Activate a connection (becomes the primary for the package). */
  onActivateConnection: (providerId: string, connectionId: string) => Promise<void>;
  /** Test a connection by calling the provider's discovery endpoint. */
  onTestConnection: (
    providerId: string,
    connectionId: string,
  ) => Promise<{
    ok: boolean;
    message: string;
    latency_ms: number;
  } | null>;
}


// ---------------------------------------------------------------------------
// Shared styles
// ---------------------------------------------------------------------------

const TRANSPORT_LABELS: Record<ProviderTransport, string> = {
  native: "Built-in",
  compatible: "Compatible",
  custom: "Custom",
};

const TRANSPORT_BADGE_CLASSES: Record<ProviderTransport, string> = {
  native:
    "bg-blue-500/10 text-blue-700 dark:text-blue-300 border-blue-500/20",
  compatible:
    "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/20",
  custom:
    "bg-purple-500/10 text-purple-700 dark:text-purple-300 border-purple-500/20",
};


// ---------------------------------------------------------------------------
// Field value helpers
// ---------------------------------------------------------------------------

/**
 * Normalize a raw field value into the shape the form inputs expect.
 *
 * The backend stores values as-is (raw JSON), so a boolean comes back
 * as ``true``, a number as ``123``, a string as ``"..."``. This helper
 * coerces missing values to sensible defaults for each field type so
 * the form inputs never see ``undefined``.
 */
function coerceFieldValue(
  spec: ConnectionFieldSpec,
  rawValue: unknown,
): unknown {
  if (rawValue !== undefined && rawValue !== null) return rawValue;
  if (spec.default !== undefined && spec.default !== null) return spec.default;
  switch (spec.type) {
    case "boolean":
      return false;
    case "number":
      return "";
    case "select":
      return spec.options && spec.options.length > 0
        ? spec.options[0].value
        : "";
    default:
      return "";
  }
}

/**
 * Compute the two default fields (``base_url``, ``api_key``) that
 * every provider needs, plus the provider's declared extra fields.
 *
 * The two defaults are ALWAYS rendered unless the manifest explicitly
 * declares them. This avoids duplicating spec entries for the most
 * common case.
 */
function buildEffectiveFieldList(
  manifestFields: ConnectionFieldSpec[] | undefined,
): ConnectionFieldSpec[] {
  const result: ConnectionFieldSpec[] = [];
  const seen = new Set<string>();

  // Default base_url.
  const baseUrlOverride = (manifestFields || []).find(
    (f) => f.key === "base_url",
  );
  result.push(
    baseUrlOverride || {
      key: "base_url",
      label: "Endpoint URL",
      type: "url",
      required: true,
      placeholder: "https://api.example.com/v1",
      description:
        "The base URL of the provider's API. The adapter appends its own path.",
    },
  );
  seen.add("base_url");

  // Default api_key.
  const apiKeyOverride = (manifestFields || []).find(
    (f) => f.key === "api_key",
  );
  result.push(
    apiKeyOverride || {
      key: "api_key",
      label: "API Key",
      type: "password",
      required: false,
      placeholder: "sk-...",
      description:
        "Sent as a Bearer token by default. Leave empty for local providers that do not require authentication.",
    },
  );
  seen.add("api_key");

  // Manifest-declared extras.
  for (const field of manifestFields || []) {
    if (!seen.has(field.key)) {
      result.push(field);
      seen.add(field.key);
    }
  }

  return result;
}

/**
 * Evaluate a ``visible_if`` condition against the current field values.
 *
 * ``visible_if`` is a dict of ``{otherFieldKey: expectedValue}``. The
 * field is shown only when EVERY referenced field equals its expected
 * value.
 *
 * An empty ``visible_if`` (``undefined`` or ``{}``) means "always
 * visible".
 */
function isFieldVisible(
  spec: ConnectionFieldSpec,
  values: Record<string, unknown>,
): boolean {
  const cond = spec.visible_if;
  if (!cond || typeof cond !== "object") return true;
  for (const [key, expected] of Object.entries(cond)) {
    if (values[key] !== expected) return false;
  }
  return true;
}


// ---------------------------------------------------------------------------
// Dynamic field renderer
// ---------------------------------------------------------------------------

interface FieldRendererProps {
  spec: ConnectionFieldSpec;
  value: unknown;
  onChange: (nextValue: unknown) => void;
  disabled: boolean;
}

function FieldRenderer({
  spec,
  value,
  onChange,
  disabled,
}: FieldRendererProps) {
  const [showSecret, setShowSecret] = useState(false);
  const commonInputClass = [
    "w-full bg-background border border-border rounded-md px-3 py-1.5",
    "text-xs text-foreground font-mono shadow-sm",
    "focus:outline-none focus:ring-1 focus:ring-primary/40",
    disabled ? "cursor-not-allowed opacity-60" : "",
  ].join(" ");

  const labelBlock = (
    <div className="flex items-center justify-between gap-2">
      <label
        htmlFor={`field-${spec.key}`}
        className="text-[11px] font-semibold text-foreground"
      >
        {spec.label}
        {spec.required && (
          <span className="ml-1 text-destructive" aria-hidden="true">
            *
          </span>
        )}
      </label>
      {spec.type === "password" && (
        <button
          type="button"
          onClick={() => setShowSecret((s) => !s)}
          className="text-[10px] text-muted-foreground hover:text-foreground flex items-center gap-0.5"
        >
          {showSecret ? <EyeOff size={10} /> : <Eye size={10} />}
          <span>{showSecret ? "Hide" : "Show"}</span>
        </button>
      )}
    </div>
  );

  const helperText = spec.description ? (
    <p className="mt-0.5 text-[10px] text-muted-foreground leading-relaxed">
      {spec.description}
    </p>
  ) : null;

  // ── Boolean ──────────────────────────────────────────────────────
  if (spec.type === "boolean") {
    const checked = Boolean(value);
    return (
      <div>
        {labelBlock}
        <label className="mt-1 flex cursor-pointer items-center gap-2">
          <input
            id={`field-${spec.key}`}
            type="checkbox"
            checked={checked}
            disabled={disabled}
            onChange={(e) => onChange(e.target.checked)}
            className="h-3.5 w-3.5 cursor-pointer rounded border-muted-foreground/40 text-primary focus:ring-1 focus:ring-primary/40"
          />
          <span className="text-xs text-foreground">
            {checked ? "Enabled" : "Disabled"}
          </span>
        </label>
        {helperText}
      </div>
    );
  }

  // ── Select ───────────────────────────────────────────────────────
  if (spec.type === "select") {
    const options = spec.options || [];
    return (
      <div>
        {labelBlock}
        <select
          id={`field-${spec.key}`}
          value={String(value ?? "")}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          className={`${commonInputClass} cursor-pointer`}
        >
          {options.length === 0 && (
            <option value="">(No options declared)</option>
          )}
          {options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        {helperText}
      </div>
    );
  }

  // ── Number ───────────────────────────────────────────────────────
  if (spec.type === "number") {
    return (
      <div>
        {labelBlock}
        <input
          id={`field-${spec.key}`}
          type="number"
          value={value === "" || value === null || value === undefined
            ? ""
            : String(value)}
          disabled={disabled}
          placeholder={spec.placeholder || ""}
          onChange={(e) => {
            const raw = e.target.value;
            onChange(raw === "" ? "" : Number(raw));
          }}
          className={commonInputClass}
        />
        {helperText}
      </div>
    );
  }

  // ── Textarea ─────────────────────────────────────────────────────
  if (spec.type === "textarea") {
    return (
      <div>
        {labelBlock}
        <textarea
          id={`field-${spec.key}`}
          value={String(value ?? "")}
          disabled={disabled}
          placeholder={spec.placeholder || ""}
          rows={4}
          onChange={(e) => onChange(e.target.value)}
          className={`${commonInputClass} resize-y`}
        />
        {helperText}
      </div>
    );
  }

  // ── File / Secret File ───────────────────────────────────────────
  //
  // The form cannot read the user's local filesystem. The value is a
  // path on the PELDRUN host — either absolute or relative to the
  // package's storage directory. The adapter's code opens the file
  // at request time. This is the same model used by PELDRUN's own
  // TLS configuration fields elsewhere.
  if (spec.type === "file" || spec.type === "secret_file") {
    return (
      <div>
        {labelBlock}
        <input
          id={`field-${spec.key}`}
          type={spec.type === "secret_file" && !showSecret ? "password" : "text"}
          value={String(value ?? "")}
          disabled={disabled}
          placeholder={
            spec.placeholder || "/absolute/path/on/peldrun/host"
          }
          onChange={(e) => onChange(e.target.value)}
          className={commonInputClass}
        />
        <p className="mt-0.5 text-[10px] text-muted-foreground italic">
          Absolute path to a file on the PELDRUN backend host
          {spec.accept ? ` (accepted: ${spec.accept})` : ""}.
        </p>
        {helperText}
      </div>
    );
  }

  // ── URL ──────────────────────────────────────────────────────────
  if (spec.type === "url") {
    return (
      <div>
        {labelBlock}
        <input
          id={`field-${spec.key}`}
          type="url"
          value={String(value ?? "")}
          disabled={disabled}
          placeholder={spec.placeholder || "https://..."}
          onChange={(e) => onChange(e.target.value)}
          className={commonInputClass}
        />
        {helperText}
      </div>
    );
  }

  // ── Password ─────────────────────────────────────────────────────
  if (spec.type === "password") {
    return (
      <div>
        {labelBlock}
        <input
          id={`field-${spec.key}`}
          type={showSecret ? "text" : "password"}
          value={String(value ?? "")}
          disabled={disabled}
          placeholder={spec.placeholder || ""}
          onChange={(e) => onChange(e.target.value)}
          className={commonInputClass}
        />
        {helperText}
      </div>
    );
  }

  // ── String (default) ─────────────────────────────────────────────
  return (
    <div>
      {labelBlock}
      <input
        id={`field-${spec.key}`}
        type="text"
        value={String(value ?? "")}
        disabled={disabled}
        placeholder={spec.placeholder || ""}
        onChange={(e) => onChange(e.target.value)}
        className={commonInputClass}
      />
      {helperText}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Connection row (in the list view)
// ---------------------------------------------------------------------------

interface ConnectionRowProps {
  connection: ProviderConnection;
  isBusy: boolean;
  onEdit: () => void;
  onEnable: () => void;
  onDisable: () => void;
  onActivate: () => void;
  onDelete: () => void;
  onTest: () => void;
  testResult?: { ok: boolean; message: string; latency_ms: number };
}

function ConnectionRow({
  connection,
  isBusy,
  onEdit,
  onEnable,
  onDisable,
  onActivate,
  onDelete,
  onTest,
  testResult,
}: ConnectionRowProps) {
  return (
    <div
      className={[
        "rounded-lg border p-3 transition-colors",
        connection.is_active
          ? "border-emerald-500/40 bg-emerald-500/5"
          : connection.enabled
          ? "border-border bg-card"
          : "border-border bg-muted/30 opacity-70",
      ].join(" ")}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="truncate text-sm font-semibold text-foreground">
              {connection.name}
            </span>
            {connection.is_active && (
              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-300">
                <Star className="h-2.5 w-2.5 fill-current" />
                Active
              </span>
            )}
            {!connection.enabled && (
              <span className="inline-flex items-center rounded-full border border-muted-foreground/20 bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
                Disabled
              </span>
            )}
          </div>

          {testResult && (
            <div className="mt-1.5 flex items-center gap-1.5">
              {testResult.ok ? (
                <CheckCircle2 className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
              ) : (
                <XCircle className="h-3 w-3 text-destructive" />
              )}
              <span
                className={`text-[11px] font-mono ${
                  testResult.ok
                    ? "text-emerald-600 dark:text-emerald-400"
                    : "text-destructive"
                }`}
              >
                {testResult.ok
                  ? `Connected — ${testResult.latency_ms.toFixed(0)} ms`
                  : testResult.message || "Probe failed"}
              </span>
            </div>
          )}
        </div>

        {/* Actions */}
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={onTest}
            disabled={isBusy}
            title="Test connection"
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
          >
            {isBusy ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Zap className="h-3.5 w-3.5" />
            )}
          </button>

          {!connection.is_active && (
            <button
              type="button"
              onClick={onActivate}
              disabled={isBusy}
              title="Make active"
              className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-emerald-500/10 hover:text-emerald-600 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Star className="h-3.5 w-3.5" />
            </button>
          )}

          <button
            type="button"
            onClick={connection.enabled ? onDisable : onEnable}
            disabled={isBusy}
            title={connection.enabled ? "Disable" : "Enable"}
            className={[
              "rounded-md px-2 py-1 text-[11px] font-medium transition-colors",
              connection.enabled
                ? "text-muted-foreground hover:bg-muted hover:text-foreground"
                : "bg-emerald-500/10 text-emerald-600 hover:bg-emerald-500/20",
              isBusy ? "cursor-not-allowed opacity-40" : "",
            ].join(" ")}
          >
            {connection.enabled ? "Disable" : "Enable"}
          </button>

          <button
            type="button"
            onClick={onEdit}
            disabled={isBusy}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            title="Edit"
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </button>

          <button
            type="button"
            onClick={onDelete}
            disabled={isBusy}
            title="Delete"
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Form mode
// ---------------------------------------------------------------------------

interface FormState {
  /** ``"create"`` or the ID of the connection being edited. */
  mode: "create" | "edit" | string;
  name: string;
  isActive: boolean;
  /** Current field values — includes all edits made by the user. */
  values: Record<string, unknown>;
  /** Original field values — used to detect which fields changed. */
  originalValues: Record<string, unknown>;
}

interface FormViewProps {
  provider: ProviderPackageEntry;
  form: FormState;
  onChange: (patch: Partial<FormState>) => void;
  onFieldChange: (key: string, value: unknown) => void;
  onSave: () => Promise<void>;
  onCancel: () => void;
  onDelete?: () => void;
  isSaving: boolean;
  error: string | null;
}

function FormView({
  provider,
  form,
  onChange,
  onFieldChange,
  onSave,
  onCancel,
  onDelete,
  isSaving,
  error,
}: FormViewProps) {
  const fields = useMemo(
    () => buildEffectiveFieldList(provider.connection_fields),
    [provider.connection_fields],
  );

  const isEdit = form.mode !== "create";

  // Validate required fields.
  const missingRequired = fields.some((spec) => {
    if (!spec.required) return false;
    if (!isFieldVisible(spec, form.values)) return false;
    const v = form.values[spec.key];
    return v === undefined || v === null || v === "";
  });

  const canSave = !isSaving && !missingRequired && form.name.trim().length > 0;

  return (
    <div className="flex flex-col gap-4">
      {/* Back button */}
      <div>
        <button
          type="button"
          onClick={onCancel}
          disabled={isSaving}
          className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ArrowLeft className="h-3 w-3" />
          Back to connections
        </button>
      </div>

      {/* Error */}
      {error && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3"
        >
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
          <p className="text-[11px] leading-relaxed text-destructive">
            {error}
          </p>
        </div>
      )}

      {/* Connection name */}
      <div>
        <label
          htmlFor="connection-name"
          className="mb-1 block text-[11px] font-semibold text-foreground"
        >
          Connection Name
          <span className="ml-1 text-destructive" aria-hidden="true">*</span>
        </label>
        <input
          id="connection-name"
          type="text"
          value={form.name}
          disabled={isSaving}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="e.g. Personal, Work, Sandbox"
          className="w-full rounded-md border bg-background px-3 py-1.5 text-xs font-mono text-foreground shadow-sm focus:outline-none focus:ring-1 focus:ring-primary/40"
        />
      </div>

      {/* Dynamic fields */}
      <div className="space-y-3">
        {fields.map((spec) => {
          if (!isFieldVisible(spec, form.values)) return null;
          return (
            <FieldRenderer
              key={spec.key}
              spec={spec}
              value={form.values[spec.key]}
              onChange={(v) => onFieldChange(spec.key, v)}
              disabled={isSaving}
            />
          );
        })}
      </div>

      {/* Set as active checkbox */}
      <label className="flex cursor-pointer items-start gap-2 rounded-md border bg-muted/20 p-3">
        <input
          type="checkbox"
          checked={form.isActive}
          disabled={isSaving}
          onChange={(e) => onChange({ isActive: e.target.checked })}
          className="mt-0.5 h-3.5 w-3.5 cursor-pointer rounded border-muted-foreground/40 text-primary focus:ring-1 focus:ring-primary/40"
        />
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-semibold text-foreground">
            Set as active connection
          </p>
          <p className="mt-0.5 text-[10px] text-muted-foreground leading-relaxed">
            When enabled, this connection becomes the one the runtime uses
            for chat and agent requests. Other connections of the same
            package will be deactivated.
          </p>
        </div>
      </label>

      {/* Actions */}
      <div className="flex items-center justify-between gap-2 border-t pt-3">
        <div>
          {isEdit && onDelete && (
            <button
              type="button"
              onClick={onDelete}
              disabled={isSaving}
              className="inline-flex items-center gap-1.5 rounded-md border border-destructive/30 px-3 py-1.5 text-xs font-medium text-destructive transition-colors hover:bg-destructive/10 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete
            </button>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={isSaving}
            className="rounded-md border px-3 py-1.5 text-xs font-medium transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onSave}
            disabled={!canSave}
            className={[
              "inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors",
              canSave ? "hover:bg-primary/90" : "cursor-not-allowed opacity-50",
            ].join(" ")}
          >
            {isSaving ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Saving…
              </>
            ) : (
              <>
                <Save className="h-3.5 w-3.5" />
                {isEdit ? "Save Changes" : "Create Connection"}
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Main modal
// ---------------------------------------------------------------------------

export function ProviderConfigureModal({
  open,
  provider,
  connections,
  isConnectionsLoading,
  connectionsError,
  connectionActionId,
  onClose,
  onCreateConnection,
  onUpdateConnection,
  onDeleteConnection,
  onEnableConnection,
  onDisableConnection,
  onActivateConnection,
  onTestConnection,
}: ProviderConfigureModalProps) {
  const [form, setForm] = useState<FormState | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<
    Record<string, { ok: boolean; message: string; latency_ms: number }>
  >({});

  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── Reset state when the provider changes or modal opens ─────────
  useEffect(() => {
    if (!open || !provider) {
      setForm(null);
      setFormError(null);
      setTestResults({});
      return;
    }
    // If there are no saved connections, start in create mode.
    // Otherwise show the list.
    if (!isConnectionsLoading && connections.length === 0) {
      setForm(newCreateFormState(provider));
    } else {
      setForm(null);
    }
    setFormError(null);
    setTestResults({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, provider?.id]);

  // ── Close on ESC (unless a save is in flight) ────────────────────
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && connectionActionId === null) {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, connectionActionId, onClose]);

  if (!open || !provider) return null;

  const displayName =
    provider.display_name || provider.name || provider.id;
  const transport =
    (provider.transport as ProviderTransport) ?? "native";
  const badgeClass =
    TRANSPORT_BADGE_CLASSES[transport] ?? TRANSPORT_BADGE_CLASSES.native;

  const isBusy = connectionActionId !== null;

  // ── Actions ──────────────────────────────────────────────────────

  const handleCreateNew = () => {
    setForm(newCreateFormState(provider));
    setFormError(null);
  };

  const handleEdit = (conn: ProviderConnection) => {
    setForm(editFormState(conn));
    setFormError(null);
  };

  const handleCancelForm = () => {
    setForm(null);
    setFormError(null);
  };

  const handleFieldChange = (key: string, value: unknown) => {
    setForm((prev) =>
      prev ? { ...prev, values: { ...prev.values, [key]: value } } : prev,
    );
  };

  const handleFormPatch = (patch: Partial<FormState>) => {
    setForm((prev) => (prev ? { ...prev, ...patch } : prev));
  };

  const handleSave = async () => {
    if (!form) return;
    setFormError(null);

    // Only submit fields that changed since load — this preserves
    // masked secret values on the backend (the merge keeps the
    // existing value when the field is absent from the payload).
    const changedFields: Record<string, unknown> = {};
    for (const [key, current] of Object.entries(form.values)) {
      const original = form.originalValues[key];
      if (current !== original) {
        changedFields[key] = current;
      }
    }
    // Include all fields when creating a new connection — there is no
    // "original" to preserve.
    const fieldsToSubmit =
      form.mode === "create" ? form.values : changedFields;

    const payload: SaveConnectionPayload = {
      name: form.name.trim() || provider.id,
      fields: fieldsToSubmit,
      is_active: form.isActive,
    };

    let saved: ProviderConnection | null = null;
    if (form.mode === "create") {
      saved = await onCreateConnection(provider.id, payload);
    } else {
      saved = await onUpdateConnection(provider.id, form.mode, payload);
    }

    if (saved) {
      // Return to list view.
      setForm(null);
    } else {
      setFormError("Save failed. See the toast for details.");
    }
  };

  const handleDelete = async () => {
    if (!form || form.mode === "create") return;
    const name = form.name || "this connection";
    if (
      !window.confirm(
        `Delete connection "${name}"? This cannot be undone.`,
      )
    ) {
      return;
    }
    const ok = await onDeleteConnection(provider.id, form.mode);
    if (ok) {
      setForm(null);
    }
  };

  const handleRowDelete = async (conn: ProviderConnection) => {
    if (
      !window.confirm(
        `Delete connection "${conn.name}"? This cannot be undone.`,
      )
    ) {
      return;
    }
    await onDeleteConnection(provider.id, conn.id);
  };

  const handleRowTest = async (conn: ProviderConnection) => {
    const result = await onTestConnection(provider.id, conn.id);
    if (result) {
      setTestResults((prev) => ({ ...prev, [conn.id]: result }));
    }
  };

  // ── Render ──────────────────────────────────────────────────────

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="provider-configure-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      {/* Backdrop */}
      <button
        type="button"
        aria-label="Close configure dialog"
        onClick={() => {
          if (!isBusy) onClose();
        }}
        className="absolute inset-0 cursor-default bg-black/60 backdrop-blur-sm"
        tabIndex={-1}
      />

      {/* Dialog */}
      <div className="relative z-10 flex w-full max-w-2xl flex-col overflow-hidden rounded-xl border bg-card shadow-2xl max-h-[90vh]">
        {/* Header */}
        <div className="flex items-start justify-between gap-4 border-b px-5 py-4">
          <div className="flex items-start gap-3">
            <div
              className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${badgeClass}`}
              aria-hidden="true"
            >
              <Plug className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h2
                id="provider-configure-title"
                className="truncate text-sm font-semibold text-foreground"
                title={displayName}
              >
                Configure {displayName}
              </h2>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                <span
                  className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${badgeClass}`}
                >
                  {TRANSPORT_LABELS[transport] ?? transport}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  {connections.length} saved{" "}
                  {connections.length === 1 ? "connection" : "connections"}
                </span>
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isBusy}
            aria-label="Close"
            className={[
              "rounded-md p-1 text-muted-foreground transition-colors",
              isBusy
                ? "cursor-not-allowed opacity-40"
                : "hover:bg-muted hover:text-foreground",
            ].join(" ")}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Runtime hints banner */}
        {provider.runtime_hints?.requires_custom_tls && (
          <div className="border-b bg-amber-500/5 px-5 py-2.5">
            <div className="flex items-start gap-2">
              <Shield className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
              <div className="min-w-0 flex-1">
                <p className="text-[11px] font-medium text-amber-900 dark:text-amber-200">
                  This provider requires a custom TLS certificate
                </p>
                <p className="mt-0.5 text-[10px] text-amber-900/80 dark:text-amber-200/70">
                  The package declares that certifi does not include the
                  root CA this provider uses. Provide the CA bundle file
                  path in the connection fields.
                </p>
              </div>
            </div>
          </div>
        )}

        {/* Body */}
        <div className="flex-1 overflow-y-auto scrollbar-thin px-5 py-4">
          {/* Loading state */}
          {isConnectionsLoading && connections.length === 0 && !form && (
            <div className="flex flex-col items-center justify-center gap-2 py-12">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
              <p className="text-xs text-muted-foreground">
                Loading connections…
              </p>
            </div>
          )}

          {/* List error */}
          {connectionsError && !form && (
            <div
              role="alert"
              className="mb-4 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3"
            >
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
              <p className="text-[11px] leading-relaxed text-destructive">
                {connectionsError}
              </p>
            </div>
          )}

          {/* FORM MODE */}
          {form && (
            <FormView
              provider={provider}
              form={form}
              onChange={handleFormPatch}
              onFieldChange={handleFieldChange}
              onSave={handleSave}
              onCancel={handleCancelForm}
              onDelete={form.mode !== "create" ? handleDelete : undefined}
              isSaving={connectionActionId !== null}
              error={formError}
            />
          )}

          {/* LIST MODE */}
          {!form && !isConnectionsLoading && (
            <div className="space-y-3">
              {/* Add button */}
              <button
                type="button"
                onClick={handleCreateNew}
                disabled={isBusy}
                className={[
                  "inline-flex w-full items-center justify-center gap-2 rounded-lg border-2 border-dashed border-primary/30 bg-primary/5 px-3 py-3 text-xs font-medium text-primary transition-colors",
                  isBusy
                    ? "cursor-not-allowed opacity-50"
                    : "hover:border-primary/60 hover:bg-primary/10",
                ].join(" ")}
              >
                <Plus className="h-4 w-4" />
                Add New Connection
              </button>

              {/* Empty state */}
              {connections.length === 0 && (
                <div className="rounded-lg border border-dashed p-6 text-center">
                  <p className="text-sm font-medium text-foreground">
                    No connections yet
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Click "Add New Connection" above to get started.
                  </p>
                </div>
              )}

              {/* Connection rows */}
              {connections.map((conn) => (
                <ConnectionRow
                  key={conn.id}
                  connection={conn}
                  isBusy={connectionActionId === conn.id}
                  onEdit={() => handleEdit(conn)}
                  onEnable={() =>
                    onEnableConnection(provider.id, conn.id)
                  }
                  onDisable={() =>
                    onDisableConnection(provider.id, conn.id)
                  }
                  onActivate={() =>
                    onActivateConnection(provider.id, conn.id)
                  }
                  onDelete={() => handleRowDelete(conn)}
                  onTest={() => handleRowTest(conn)}
                  testResult={testResults[conn.id]}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Form state constructors
// ---------------------------------------------------------------------------

function newCreateFormState(provider: ProviderPackageEntry): FormState {
  const fields = buildEffectiveFieldList(provider.connection_fields);
  const values: Record<string, unknown> = {};
  for (const spec of fields) {
    values[spec.key] = coerceFieldValue(spec, undefined);
  }
  return {
    mode: "create",
    name: `${provider.display_name || provider.name || provider.id} — Default`,
    isActive: true,
    values,
    originalValues: {},
  };
}

function editFormState(conn: ProviderConnection): FormState {
  return {
    mode: conn.id,
    name: conn.name,
    isActive: conn.is_active,
    values: { ...conn.fields },
    originalValues: { ...conn.fields },
  };
}

export default ProviderConfigureModal;
