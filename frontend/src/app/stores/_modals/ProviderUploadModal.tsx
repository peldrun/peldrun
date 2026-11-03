// frontend/src/app/stores/_modals/ProviderUploadModal.tsx
"use client";

import React, { useEffect, useRef } from "react";
import {
  AlertCircle,
  CheckCircle2,
  FileArchive,
  Loader2,
  Package,
  Upload,
  X,
  XCircle,
} from "lucide-react";
import type { InstallJobStatus } from "@/lib/providerApi";


// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ProviderUploadModalProps {
  /** Whether the modal is visible. */
  open: boolean;

  /** The .zip file selected by the user, or ``null``. */
  selectedFile: File | null;

  /** Whether "Install dependencies in an isolated venv" is checked. */
  installDependencies: boolean;

  /**
   * True from the moment the user clicks Install until the job
   * reaches a terminal state (success or failure).
   *
   * While ``true``:
   *   - The file picker is hidden (replaced by the progress view).
   *   - The Close button is disabled.
   *   - The Install button is disabled.
   */
  isUploading: boolean;

  /**
   * Error message to display when the install fails.
   *
   * Populated from ``installJobStatus.error.error`` once the polling
   * loop sees a ``failed`` status. Cleared when the user reopens the
   * modal or retries.
   */
  errorMessage: string | null;

  /**
   * Live state of the current install job, or ``null`` when no job is
   * active.
   *
   * While ``running``, the modal shows a progress bar (derived from
   * the log contents) and a live console. Once ``success`` or
   * ``failed``, the modal shows the final result and (on failure)
   * keeps the logs visible so the user can diagnose the issue.
   */
  installJobStatus: InstallJobStatus | null;

  /** Called when the user selects or clears a file. */
  onSelectFile: (file: File | null) => void;

  /** Called when the venv checkbox is toggled. */
  onToggleInstallDependencies: (value: boolean) => void;

  /** Called when the user clicks "Install Package". */
  onSubmit: () => Promise<void>;

  /** Called when the user closes the modal (or presses Escape). */
  onClose: () => void;
}


// ---------------------------------------------------------------------------
// Progress derivation
// ---------------------------------------------------------------------------

/**
 * High-level phases of the install pipeline, in order.
 *
 * The UI uses the current phase to drive the progress bar and the
 * step indicators. Phase transitions are detected by substring
 * matching against the log lines emitted by the backend.
 *
 * Keeping this derivation in the modal (not in the hook) means the
 * hook stays presentation-agnostic — it just exposes raw logs, and
 * each consumer can decide how to interpret them.
 */
const PHASE_LABELS = [
  "Validating archive",
  "Extracting files",
  "Preparing environment",
  "Installing dependencies",
  "Registering package",
] as const;

/**
 * Derive the current phase index from the accumulated log lines.
 *
 * Returns 0..PHASE_LABELS.length when still running, or
 * PHASE_LABELS.length + 1 when complete.
 *
 * The heuristics here are conservative — each check looks for a
 * distinctive marker that the backend emits at a well-defined point
 * in the pipeline. If no marker has appeared yet, we stay at phase 0.
 */
function derivePhase(logs: string[]): number {
  const joined = logs.join("\n");

  // Terminal success / failure markers.
  if (
    joined.includes("Installation completed") ||
    joined.includes("completed successfully")
  ) {
    return PHASE_LABELS.length + 1;
  }
  if (joined.includes("[error]")) {
    return PHASE_LABELS.length + 1;
  }

  // Phase markers, from most specific to least.
  if (joined.includes("register") || joined.includes("Registering")) {
    return 4;
  }
  if (joined.includes("[pip]") || joined.includes("pip install")) {
    return 3;
  }
  if (joined.includes("Resolving dependency source")) {
    return 2;
  }
  if (joined.includes("extract") || joined.includes("Extracting")) {
    return 1;
  }
  return 0;
}


// ---------------------------------------------------------------------------
// Log line rendering
// ---------------------------------------------------------------------------

/**
 * Parse a log line into its tag (``[system]``, ``[pip]``, ``[error]``)
 * and body, so the UI can color-code by tag.
 */
function parseLogLine(line: string): { tag: string; text: string } {
  const match = line.match(/^(\[(?:system|pip|error)\])?\s*(.*)$/);
  return {
    tag: match?.[1] || "",
    text: match?.[2] || line,
  };
}

function colorForTag(tag: string): string {
  switch (tag) {
    case "[error]":
      return "text-destructive";
    case "[system]":
      return "text-blue-600 dark:text-blue-400";
    case "[pip]":
      return "text-muted-foreground";
    default:
      return "text-foreground";
  }
}


// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ProviderUploadModal({
  open,
  selectedFile,
  installDependencies,
  isUploading,
  errorMessage,
  installJobStatus,
  onSelectFile,
  onToggleInstallDependencies,
  onSubmit,
  onClose,
}: ProviderUploadModalProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const logsEndRef = useRef<HTMLDivElement>(null);

  const isRunning = installJobStatus?.status === "running" || isUploading;
  const isSuccess = installJobStatus?.status === "success";
  const isFailed = installJobStatus?.status === "failed";
  const showProgress = installJobStatus !== null || isUploading;

  // Auto-scroll the log console as new lines arrive.
  const logCount = installJobStatus?.logs.length ?? 0;
  useEffect(() => {
    if (logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [logCount]);

  // Close on Escape, unless a job is running.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !isRunning) onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, isRunning, onClose]);

  if (!open) return null;

  const currentPhase = installJobStatus
    ? derivePhase(installJobStatus.logs)
    : 0;

  const completed = isSuccess ? PHASE_LABELS.length : currentPhase;
  const progressPct = Math.min(
    100,
    Math.round((completed / PHASE_LABELS.length) * 100),
  );

  const canSubmit = selectedFile !== null && !isRunning;

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] || null;
    onSelectFile(file);
    // Reset the input so selecting the same file again re-fires onChange.
    e.target.value = "";
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="upload-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      {/* Backdrop */}
      <button
        type="button"
        aria-label="Close upload modal"
        onClick={() => {
          if (!isRunning) onClose();
        }}
        className="absolute inset-0 cursor-default bg-black/60 backdrop-blur-sm"
        tabIndex={-1}
      />

      {/* Dialog */}
      <div
        className={[
          "relative z-10 flex w-full flex-col overflow-hidden rounded-xl border bg-card shadow-2xl max-h-[90vh]",
          showProgress ? "max-w-3xl" : "max-w-xl",
        ].join(" ")}
      >
        {/* ── Header ────────────────────────────────────────────── */}
        <div className="flex items-start justify-between gap-4 border-b px-5 py-4">
          <div className="flex items-start gap-3">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary ring-1 ring-primary/20"
              aria-hidden="true"
            >
              <FileArchive className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h2
                id="upload-modal-title"
                className="text-sm font-semibold text-foreground"
              >
                Install Provider Package
              </h2>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                Upload a .zip containing a valid manifest.json.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isRunning}
            aria-label="Close"
            className={[
              "rounded-md p-1 text-muted-foreground transition-colors",
              isRunning
                ? "cursor-not-allowed opacity-40"
                : "hover:bg-muted hover:text-foreground",
            ].join(" ")}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* ── Body ──────────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto scrollbar-thin px-5 py-4">
          {/* Pre-flight view: file picker + options + errors */}
          {!showProgress && (
            <>
              <div className="mb-4">
                <label className="mb-1 block text-[11px] font-semibold text-foreground">
                  Package archive
                </label>
                <div className="flex items-center gap-2">
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".zip"
                    onChange={handleFileChange}
                    className="hidden"
                  />
                  <button
                    type="button"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={isRunning}
                    className="inline-flex items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-xs font-medium transition-colors hover:bg-muted"
                  >
                    <Upload className="h-3.5 w-3.5" />
                    Choose File
                  </button>
                  <span className="truncate text-xs text-muted-foreground">
                    {selectedFile?.name || "No file selected"}
                  </span>
                </div>
                {selectedFile && (
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    {(selectedFile.size / 1024).toFixed(1)} KB
                  </p>
                )}
              </div>

              <label className="flex cursor-pointer items-start gap-2 rounded-md border bg-muted/20 p-3">
                <input
                  type="checkbox"
                  checked={installDependencies}
                  disabled={isRunning}
                  onChange={(e) =>
                    onToggleInstallDependencies(e.target.checked)
                  }
                  className="mt-0.5 h-3.5 w-3.5 cursor-pointer rounded border-muted-foreground/40 text-primary focus:ring-1 focus:ring-primary/40"
                />
                <div className="min-w-0">
                  <p className="text-xs font-semibold text-foreground">
                    Install dependencies in an isolated venv
                  </p>
                  <p className="mt-0.5 text-[11px] text-muted-foreground leading-relaxed">
                    Recommended. Creates a dedicated Python environment
                    for this package and runs <code>pip install</code>.
                    Disable only when the package ships its dependencies
                    vendored or when the host is offline.
                  </p>
                </div>
              </label>

              <div className="mt-4 flex items-start gap-2 rounded-md border border-amber-500/20 bg-amber-500/5 p-3">
                <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
                <p className="text-[11px] leading-relaxed text-amber-900/80 dark:text-amber-200/70">
                  Only install packages you trust. The archive is
                  validated (path safety, symlinks, manifest schema,
                  forbidden permissions), but a successfully-installed
                  adapter can still make network requests to the
                  endpoint configured for its connection and read that
                  connection's API key.
                </p>
              </div>

              {errorMessage && (
                <div className="mt-4 flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3">
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
                  <p className="text-[11px] leading-relaxed text-destructive">
                    {errorMessage}
                  </p>
                </div>
              )}
            </>
          )}

          {/* Progress view */}
          {showProgress && (
            <div className="space-y-4">
              {/* Progress bar + phase chips */}
              <div>
                <div className="mb-2 flex items-center justify-between text-xs">
                  <span className="font-medium text-foreground">
                    {isSuccess
                      ? "Installation complete"
                      : isFailed
                      ? "Installation failed"
                      : PHASE_LABELS[
                          Math.min(currentPhase, PHASE_LABELS.length - 1)
                        ]}
                  </span>
                  <span className="font-mono text-muted-foreground">
                    {progressPct}%
                  </span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    className={[
                      "h-full transition-all duration-500",
                      isFailed
                        ? "bg-destructive"
                        : isSuccess
                        ? "bg-emerald-500"
                        : "bg-primary",
                    ].join(" ")}
                    style={{ width: `${progressPct}%` }}
                  />
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {PHASE_LABELS.map((label, idx) => {
                    const done = idx < completed;
                    const active = idx === completed && !isSuccess && !isFailed;
                    return (
                      <span
                        key={label}
                        className={[
                          "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium",
                          done
                            ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                            : active
                            ? "bg-primary/10 text-primary"
                            : "bg-muted text-muted-foreground",
                        ].join(" ")}
                      >
                        {done && (
                          <CheckCircle2 className="h-2.5 w-2.5" />
                        )}
                        {active && (
                          <Loader2 className="h-2.5 w-2.5 animate-spin" />
                        )}
                        {label}
                      </span>
                    );
                  })}
                </div>
              </div>

              {/* Log console */}
              <div>
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-[11px] font-semibold text-foreground">
                    Install log
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    {logCount} line{logCount === 1 ? "" : "s"}
                  </span>
                </div>
                <div className="max-h-64 overflow-y-auto rounded-md border bg-muted/30 p-2 font-mono text-[10px] leading-relaxed scrollbar-thin">
                  {logCount === 0 && (
                    <p className="italic text-muted-foreground">
                      Waiting for output…
                    </p>
                  )}
                  {installJobStatus?.logs.map((line, idx) => {
                    const { tag, text } = parseLogLine(line);
                    return (
                      <div
                        key={idx}
                        className={colorForTag(tag)}
                      >
                        {tag && (
                          <span className="mr-1 opacity-70">{tag}</span>
                        )}
                        {text}
                      </div>
                    );
                  })}
                  <div ref={logsEndRef} />
                </div>
              </div>

              {/* Success banner */}
              {isSuccess && installJobStatus?.result && (
                <div className="flex items-start gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3">
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                  <div className="min-w-0 flex-1">
                    <p className="text-[11px] font-semibold text-emerald-900 dark:text-emerald-200">
                      {installJobStatus.result.display_name ||
                        installJobStatus.result.name}{" "}
                      v{installJobStatus.result.version}
                    </p>
                    <p className="mt-0.5 text-[10px] text-emerald-900/70 dark:text-emerald-200/70">
                      Installed in{" "}
                      {installJobStatus.result.install_report.duration_ms}
                      ms.
                    </p>
                  </div>
                </div>
              )}

              {/* Failure banner */}
              {isFailed && (
                <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3">
                  <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
                  <div className="min-w-0 flex-1">
                    <p className="text-[11px] font-semibold text-destructive">
                      {installJobStatus?.error?.error ||
                        errorMessage ||
                        "Install failed"}
                    </p>
                    {installJobStatus?.error?.code && (
                      <p className="mt-0.5 font-mono text-[10px] text-destructive/80">
                        {installJobStatus.error.code}
                      </p>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* ── Footer ────────────────────────────────────────────── */}
        <div className="flex items-center justify-between gap-2 border-t px-5 py-3">
          <div className="text-[10px] text-muted-foreground">
            {showProgress
              ? "The install runs on the backend; you can close this window once it finishes."
              : "Only install packages you trust."}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={isRunning}
              className="rounded-md border px-3 py-1.5 text-xs font-medium transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
            >
              {isRunning
                ? "Installing…"
                : isFailed
                ? "Close"
                : "Cancel"}
            </button>
            {!showProgress && (
              <button
                type="button"
                onClick={() => void onSubmit()}
                disabled={!canSubmit}
                className={[
                  "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                  canSubmit
                    ? "bg-primary text-primary-foreground hover:bg-primary/90"
                    : "cursor-not-allowed opacity-50",
                ].join(" ")}
              >
                <Package className="h-3.5 w-3.5" />
                Install Package
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default ProviderUploadModal;
