// frontend/src/app/stores/_modals/ProviderUploadModal.tsx
"use client";

import React, { useEffect, useRef } from "react";
import { AlertCircle, FileArchive, Info, Shield, X } from "lucide-react";

/**
 * Modal for uploading and installing a Provider Package ZIP.
 *
 * The modal is fully controlled — all state lives in
 * ``useProviderActions`` (see the ``UseProviderActionsArgs`` contract).
 * This keeps the modal a pure presentation component: no local
 * validation logic, no file reading, no network calls. Everything is
 * delegated to ``onSubmit``.
 *
 * Sections
 * --------
 *
 *  1. **File picker** — accepts ``.zip`` files only. Shows the
 *     selected filename + size. Clears the previous error whenever
 *     a new file is chosen.
 *
 *  2. **Install dependencies checkbox** — explains the venv
 *     behaviour: ON (default) creates an isolated venv and runs pip;
 *     OFF still creates the venv for isolation but skips pip — useful
 *     for offline installs of a package whose dependencies are
 *     already vendored.
 *
 *  3. **Security notice** — explains that the ZIP will be validated
 *     (path safety, archive bomb, symlink, manifest schema, forbidden
 *     permissions) but that a successfully-installed adapter can
 *     still make network requests to its configured endpoint and read
 *     its own connection's API key.
 *
 *  4. **Inline error area** — displays the structured error message
 *     from the backend (``message [CODE]``) directly under the file
 *     picker. The same error also fires as a toast, but the inline
 *     copy persists so the user can read the full message after the
 *     toast fades.
 *
 *  5. **Submit button** — disabled while ``isUploading`` is true or
 *     when no file is selected. Shows "Installing…" during the
 *     install pipeline.
 *
 * The modal refuses to close while an install is in flight. The
 * user must wait for the operation to finish or fail — closing
 * mid-install would leave the state machine in an unrecoverable
 * state (the backend keeps extracting, but the UI forgets the run).
 *
 * Accessibility
 * -------------
 *  - ``role="dialog"`` + ``aria-modal="true"`` on the container.
 *  - ``aria-labelledby`` points to the title element.
 *  - The close button and the backdrop both call ``onClose``, which
 *    is a no-op while ``isUploading`` is true.
 *  - ESC is handled by the backdrop (native <button> behavior).
 */
export interface ProviderUploadModalProps {
  /** Whether the modal is visible. */
  open: boolean;
  /** The currently-selected file, or ``null``. Owned by the caller. */
  selectedFile: File | null;
  /** Whether ``pip install`` will run inside the isolated venv. */
  installDependencies: boolean;
  /** True while the upload + install request is in flight. */
  isUploading: boolean;
  /** Inline error message from the last failed attempt, or ``null``. */
  errorMessage: string | null;
  /** Called when the file picker selection changes. */
  onSelectFile: (file: File | null) => void;
  /** Called when the install-dependencies checkbox is toggled. */
  onToggleInstallDependencies: (value: boolean) => void;
  /** Called when Submit is pressed. Must be async — the modal awaits it. */
  onSubmit: () => Promise<void>;
  /** Called when the user dismisses the modal. No-op while uploading. */
  onClose: () => void;
}

/**
 * Format a byte count as ``"123 B"``, ``"1.2 KB"``, ``"5.4 MB"``.
 *
 * Kept inline (rather than imported from a shared util) because this
 * is the only file in the Providers section that needs it and
 * extracting it would add a module for one call site.
 */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const formatted =
    unitIndex === 0 ? `${Math.round(value)}` : value.toFixed(1);
  return `${formatted} ${units[unitIndex]}`;
}

export function ProviderUploadModal({
  open,
  selectedFile,
  installDependencies,
  isUploading,
  errorMessage,
  onSelectFile,
  onToggleInstallDependencies,
  onSubmit,
  onClose,
}: ProviderUploadModalProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  // When the modal is closed and reopened, reset the native input
  // so the same file can be selected again (browsers keep the input
  // value across re-opens in the same session).
  useEffect(() => {
    if (!open && fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }, [open]);

  if (!open) return null;

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] ?? null;
    onSelectFile(file);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isUploading) return;
    if (!selectedFile) return;
    await onSubmit();
  };

  const canSubmit = Boolean(selectedFile) && !isUploading;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="provider-upload-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      {/* Backdrop — click to close, unless an install is in flight. */}
      <button
        type="button"
        aria-label="Close upload dialog"
        onClick={() => {
          if (!isUploading) onClose();
        }}
        className="absolute inset-0 cursor-default bg-black/60 backdrop-blur-sm"
        tabIndex={-1}
      />

      {/* Dialog */}
      <div className="relative z-10 w-full max-w-lg overflow-hidden rounded-xl border bg-card shadow-2xl">
        {/* Header */}
        <div className="flex items-start justify-between gap-4 border-b px-5 py-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <FileArchive className="h-4.5 w-4.5" />
            </div>
            <div>
              <h2
                id="provider-upload-title"
                className="text-sm font-semibold text-foreground"
              >
                Install Provider Package
              </h2>
              <p className="text-xs text-muted-foreground">
                Upload a <code className="rounded bg-muted px-1">.zip</code>{" "}
                containing a valid{" "}
                <code className="rounded bg-muted px-1">manifest.json</code>.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isUploading}
            aria-label="Close"
            className={[
              "rounded-md p-1 text-muted-foreground transition-colors",
              isUploading
                ? "cursor-not-allowed opacity-40"
                : "hover:bg-muted hover:text-foreground",
            ].join(" ")}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Body */}
        <form onSubmit={handleSubmit} className="space-y-4 px-5 py-4">
          {/* File picker */}
          <div>
            <label
              htmlFor="provider-upload-file"
              className="mb-1.5 block text-xs font-medium text-foreground"
            >
              Package archive
            </label>
            <input
              ref={fileInputRef}
              id="provider-upload-file"
              type="file"
              accept=".zip,application/zip,application/x-zip-compressed"
              onChange={handleFileChange}
              disabled={isUploading}
              className={[
                "block w-full cursor-pointer rounded-md border bg-background px-3 py-2 text-xs",
                "file:mr-3 file:cursor-pointer file:rounded-md file:border-0",
                "file:bg-primary/10 file:px-2.5 file:py-1 file:text-xs file:font-medium file:text-primary",
                "hover:file:bg-primary/15",
                "focus:outline-none focus:ring-1 focus:ring-primary/40",
                isUploading ? "cursor-wait opacity-60" : "",
              ].join(" ")}
            />
            {selectedFile && (
              <p className="mt-1.5 text-[11px] text-muted-foreground">
                <span className="font-mono">{selectedFile.name}</span>
                {" · "}
                {formatBytes(selectedFile.size)}
              </p>
            )}
          </div>

          {/* Install dependencies toggle */}
          <div className="rounded-lg border bg-muted/30 p-3">
            <label className="flex cursor-pointer items-start gap-2.5">
              <input
                type="checkbox"
                checked={installDependencies}
                onChange={(e) =>
                  onToggleInstallDependencies(e.target.checked)
                }
                disabled={isUploading}
                className="mt-0.5 h-3.5 w-3.5 cursor-pointer rounded border-muted-foreground/40 text-primary focus:ring-1 focus:ring-primary/40"
              />
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium text-foreground">
                  Install dependencies in an isolated venv
                </p>
                <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                  Recommended. Creates a dedicated Python environment
                  for this package and runs{" "}
                  <code className="rounded bg-background px-1">
                    pip install
                  </code>
                  . Disable only when the package ships its dependencies
                  vendored or when the host is offline.
                </p>
              </div>
            </label>
          </div>

          {/* Security notice */}
          <div className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3">
            <Shield className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-medium text-amber-900 dark:text-amber-200">
                Only install packages you trust
              </p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-amber-900/80 dark:text-amber-200/70">
                The archive is validated (path safety, symlinks,
                manifest schema, forbidden permissions), but a
                successfully-installed adapter can still make network
                requests to the endpoint configured for its connection
                and read that connection's API key.
              </p>
            </div>
          </div>

          {/* Inline error — persists across toasts */}
          {errorMessage && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3"
            >
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
              <p className="text-[11px] leading-relaxed text-destructive">
                {errorMessage}
              </p>
            </div>
          )}

          {/* Info: what happens after install */}
          {!errorMessage && (
            <div className="flex items-start gap-2 rounded-lg border bg-muted/20 p-3">
              <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <p className="text-[11px] leading-relaxed text-muted-foreground">
                After install, the package appears in this section. Add
                a connection for it from{" "}
                <span className="font-medium text-foreground">
                  Settings → LLM
                </span>{" "}
                to use it in chats and agents.
              </p>
            </div>
          )}

          {/* Footer */}
          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              disabled={isUploading}
              className={[
                "rounded-md border px-3 py-1.5 text-xs font-medium transition-colors",
                isUploading
                  ? "cursor-not-allowed opacity-50"
                  : "hover:bg-muted",
              ].join(" ")}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSubmit}
              className={[
                "rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors",
                canSubmit
                  ? "hover:bg-primary/90"
                  : "cursor-not-allowed opacity-50",
              ].join(" ")}
            >
              {isUploading ? "Installing…" : "Install Package"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default ProviderUploadModal;
