// frontend/src/app/stores/_modals/CertificateManagerModal.tsx
"use client";

import React, { useEffect } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Download,
  ExternalLink,
  FileText,
  Loader2,
  RefreshCw,
  Shield,
  Trash2,
  X,
  XCircle,
  Zap,
} from "lucide-react";
import type {
  CertificateStatusReport,
  ProviderPackageEntry,
} from "../_types/store";


// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

/**
 * Action identifier format used by ``actionId``.
 *
 * The parent hook (``useProviderActions``) sets this to track which
 * certificate has an operation in flight, so the correct row can show
 * a spinner while the others stay interactive.
 *
 * Format: ``"<action>:<certificate_id>"``
 *   - "install:russian_trusted_root_ca"
 *   - "test:russian_trusted_root_ca"
 *   - "delete:russian_trusted_root_ca"
 */
export interface CertificateManagerModalProps {
  /** Whether the modal is visible. */
  open: boolean;

  /** The provider package being inspected, or ``null`` when closed. */
  provider: ProviderPackageEntry | null;

  /**
   * Every declared certificate with its reconciled status.
   *
   * Populated by ``useProviderActions`` via ``listCertificates()``.
   * An empty array with ``isLoading === false`` means the package
   * declares no certificates (shows the empty state).
   */
  certificates: CertificateStatusReport[];

  /** True while loading the certificate list. */
  isLoading: boolean;

  /** Error from the last list load, if any. */
  error: string | null;

  /**
   * Which action is currently in flight, in ``"<action>:<cert_id>"``
   * format. ``null`` means no action is running.
   */
  actionId: string | null;

  /** Close the modal. No-op while an action is in flight. */
  onClose: () => void;

  /** Re-fetch the certificate list from the backend. */
  onRefresh: () => Promise<void>;

  /**
   * Download, verify, and install a certificate.
   *
   * Returns ``true`` on success, ``false`` on failure. The parent
   * displays the toast — this modal only reflects the final state.
   */
  onInstall: (certificateId: string) => Promise<boolean>;

  /** Re-check the status of an installed certificate. */
  onTest: (certificateId: string) => Promise<boolean>;

  /** Delete a certificate record and (optionally) its file. */
  onDelete: (certificateId: string, deleteFile: boolean) => Promise<boolean>;
}


// ---------------------------------------------------------------------------
// Status presentation
// ---------------------------------------------------------------------------

interface StatusVisual {
  /** Short label shown in the badge. */
  label: string;
  /** Tailwind color family (used to build badge + icon classes). */
  color: "emerald" | "amber" | "red" | "orange" | "gray";
  /** Icon shown next to the display name. */
  Icon: React.ComponentType<{ className?: string }>;
}

const STATUS_VISUALS: Record<string, StatusVisual> = {
  installed: {
    label: "Installed",
    color: "emerald",
    Icon: CheckCircle2,
  },
  missing: {
    label: "Missing",
    color: "amber",
    Icon: AlertCircle,
  },
  invalid: {
    label: "Invalid",
    color: "red",
    Icon: XCircle,
  },
  expired: {
    label: "Expired",
    color: "orange",
    Icon: AlertCircle,
  },
  pending: {
    label: "Not Installed",
    color: "gray",
    Icon: AlertCircle,
  },
};

/**
 * Build the Tailwind class strings for a given status.
 *
 * Kept as a function (rather than a static map) so the two class
 * strings — badge and icon container — stay in sync as the palette
 * evolves.
 */
function statusClasses(color: StatusVisual["color"]): {
  badge: string;
  icon: string;
} {
  switch (color) {
    case "emerald":
      return {
        badge:
          "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        icon: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 ring-1 ring-emerald-500/20",
      };
    case "amber":
      return {
        badge:
          "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
        icon: "bg-amber-500/10 text-amber-600 dark:text-amber-400 ring-1 ring-amber-500/20",
      };
    case "red":
      return {
        badge:
          "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300",
        icon: "bg-red-500/10 text-red-600 dark:text-red-400 ring-1 ring-red-500/20",
      };
    case "orange":
      return {
        badge:
          "border-orange-500/30 bg-orange-500/10 text-orange-700 dark:text-orange-300",
        icon: "bg-orange-500/10 text-orange-600 dark:text-orange-400 ring-1 ring-orange-500/20",
      };
    default:
      return {
        badge:
          "border-muted-foreground/20 bg-muted text-muted-foreground",
        icon: "bg-muted text-muted-foreground ring-1 ring-border",
      };
  }
}


// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function truncateHash(hash: string | null | undefined): string {
  if (!hash) return "—";
  if (hash.length <= 20) return hash;
  return `${hash.slice(0, 12)}…${hash.slice(-6)}`;
}


// ---------------------------------------------------------------------------
// Certificate card
// ---------------------------------------------------------------------------

interface CertificateCardProps {
  certificate: CertificateStatusReport;
  /** "install" | "test" | "delete" | null — which action is in flight */
  busyAction: "install" | "test" | "delete" | null;
  /** True when any action is in flight (to disable all buttons). */
  isBusy: boolean;
  onInstall: () => void;
  onTest: () => void;
  onDelete: () => void;
}

function CertificateCard({
  certificate,
  busyAction,
  isBusy,
  onInstall,
  onTest,
  onDelete,
}: CertificateCardProps) {
  const visual = STATUS_VISUALS[certificate.status] ?? STATUS_VISUALS.pending;
  const classes = statusClasses(visual.color);
  const StatusIcon = visual.Icon;

  const isInstalled = certificate.status === "installed";
  const isInvalid = certificate.status === "invalid";
  const isPending = certificate.status === "pending";

  const handleDelete = () => {
    if (
      !window.confirm(
        `Delete certificate "${certificate.display_name}"?\n\n` +
          `The file will be removed from disk and the vault record ` +
          `will be cleared. The provider will stop working until you ` +
          `install the certificate again.`,
      )
    ) {
      return;
    }
    onDelete();
  };

  return (
    <article className="rounded-lg border bg-card p-4">
      {/* Header row: icon + name + status badge */}
      <div className="flex items-start gap-3">
        <div
          className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${classes.icon}`}
          aria-hidden="true"
        >
          <Shield className="h-4 w-4" />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-sm font-semibold text-foreground">
              {certificate.display_name}
            </h3>
            <span
              className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${classes.badge}`}
            >
              <StatusIcon className="h-2.5 w-2.5" />
              {visual.label}
            </span>
            {certificate.required && !isInstalled && (
              <span className="inline-flex items-center rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300">
                Required
              </span>
            )}
          </div>

          {/* Description only shown when not installed — once
              installed, the user cares about the path, not the prose. */}
          {!isInstalled && (
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              A TLS certificate required by this provider. Install it
              once; the adapter will load it on every request.
            </p>
          )}
        </div>
      </div>

      {/* Detail grid: path, size, hash, installed at */}
      {isInstalled && (
        <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-1.5 text-[11px] sm:grid-cols-2">
          {certificate.installed_path && (
            <>
              <dt className="flex items-center gap-1 text-muted-foreground">
                <FileText className="h-3 w-3" />
                Installed at
              </dt>
              <dd
                className="truncate font-mono text-foreground"
                title={certificate.installed_path}
              >
                {certificate.installed_path}
              </dd>
            </>
          )}
          {certificate.size_bytes > 0 && (
            <>
              <dt className="text-muted-foreground">File size</dt>
              <dd className="font-mono text-foreground">
                {formatBytes(certificate.size_bytes)}
              </dd>
            </>
          )}
          {certificate.actual_sha256 && (
            <>
              <dt className="text-muted-foreground">SHA-256</dt>
              <dd
                className="truncate font-mono text-foreground"
                title={certificate.actual_sha256}
              >
                {truncateHash(certificate.actual_sha256)}
              </dd>
            </>
          )}
          {certificate.installed_at && (
            <>
              <dt className="text-muted-foreground">Installed at</dt>
              <dd className="text-foreground">
                {formatTimestamp(certificate.installed_at)}
              </dd>
            </>
          )}
        </dl>
      )}

      {/* Invalid state: show expected vs actual hash */}
      {isInvalid && certificate.expected_sha256 && (
        <div className="mt-3 rounded-md border border-red-500/20 bg-red-500/5 p-2.5">
          <p className="text-[10px] font-semibold text-red-900 dark:text-red-200">
            Hash mismatch
          </p>
          <dl className="mt-1 grid grid-cols-1 gap-x-3 gap-y-0.5 text-[10px]">
            <dt className="text-red-900/70 dark:text-red-200/70">
              Expected
            </dt>
            <dd
              className="font-mono text-red-900 dark:text-red-200"
              title={certificate.expected_sha256}
            >
              {truncateHash(certificate.expected_sha256)}
            </dd>
            <dt className="text-red-900/70 dark:text-red-200/70">
              Actual
            </dt>
            <dd
              className="font-mono text-red-900 dark:text-red-200"
              title={certificate.actual_sha256 || ""}
            >
              {truncateHash(certificate.actual_sha256)}
            </dd>
          </dl>
        </div>
      )}

      {/* Error message from the last attempt */}
      {certificate.error_message && (
        <div className="mt-2 flex items-start gap-1.5">
          <AlertCircle className="mt-0.5 h-3 w-3 shrink-0 text-destructive" />
          <p className="text-[11px] leading-relaxed text-destructive">
            {certificate.error_message}
          </p>
        </div>
      )}

      {/* Action row */}
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t pt-3">
        {/* Install / Reinstall */}
        <button
          type="button"
          onClick={onInstall}
          disabled={isBusy}
          className={[
            "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
            !isBusy && (isPending || !isInstalled)
              ? "bg-primary text-primary-foreground hover:bg-primary/90"
              : "border bg-muted text-foreground hover:bg-muted/70",
            isBusy ? "cursor-not-allowed opacity-50" : "",
          ].join(" ")}
          title={
            isInstalled
              ? "Re-download and re-install the certificate (overwrites the current file)"
              : "Download the certificate and install it on this host"
          }
        >
          {busyAction === "install" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Download className="h-3.5 w-3.5" />
          )}
          {isInstalled ? "Reinstall" : isInvalid ? "Reinstall" : "Download & Install"}
        </button>

        {/* Test — only meaningful when the file exists */}
        {!isPending && (
          <button
            type="button"
            onClick={onTest}
            disabled={isBusy}
            className={[
              "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
              "border text-foreground hover:bg-muted",
              isBusy ? "cursor-not-allowed opacity-50" : "",
            ].join(" ")}
            title="Re-check the certificate file (existence, hash, format)"
          >
            {busyAction === "test" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Zap className="h-3.5 w-3.5" />
            )}
            Test
          </button>
        )}

        {/* Delete — only when there is something to delete */}
        {!isPending && (
          <button
            type="button"
            onClick={handleDelete}
            disabled={isBusy}
            className={[
              "ml-auto inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
              "text-destructive hover:bg-destructive/10",
              isBusy ? "cursor-not-allowed opacity-50" : "",
            ].join(" ")}
            title="Remove the certificate file from disk and clear the record"
          >
            {busyAction === "delete" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Trash2 className="h-3.5 w-3.5" />
            )}
            Delete
          </button>
        )}

        {/* Docs link */}
        {certificate.docs_url && (
          <a
            href={certificate.docs_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
          >
            <ExternalLink className="h-3 w-3" />
            Learn more
          </a>
        )}
      </div>
    </article>
  );
}


// ---------------------------------------------------------------------------
// Empty + loading + error states
// ---------------------------------------------------------------------------

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed p-10 text-center">
      <Shield className="h-10 w-10 text-muted-foreground/40" />
      <p className="text-sm font-medium text-foreground">
        No certificates required
      </p>
      <p className="max-w-md text-xs text-muted-foreground leading-relaxed">
        This provider does not declare any TLS certificates that need
        to be installed on the host. If you see SSL errors when using
        this provider, check the backend logs.
      </p>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12">
      <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      <p className="text-xs text-muted-foreground">
        Loading certificates…
      </p>
    </div>
  );
}

function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-8 text-center"
    >
      <AlertCircle className="h-8 w-8 text-destructive" />
      <p className="text-sm font-medium text-destructive">
        Failed to load certificates
      </p>
      <p className="max-w-md text-xs text-muted-foreground">
        {message}
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-2 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium hover:bg-muted"
      >
        <RefreshCw className="h-3 w-3" />
        Retry
      </button>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Main modal
// ---------------------------------------------------------------------------

export function CertificateManagerModal({
  open,
  provider,
  certificates,
  isLoading,
  error,
  actionId,
  onClose,
  onRefresh,
  onInstall,
  onTest,
  onDelete,
}: CertificateManagerModalProps) {
  // Close on ESC (unless an action is in flight).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && actionId === null) {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, actionId, onClose]);

  if (!open || !provider) return null;

  const displayName =
    provider.display_name || provider.name || provider.id;
  const isBusy = actionId !== null;

  // Compute how many certificates are installed vs required, for
  // the summary line in the header.
  const requiredCerts = certificates.filter((c) => c.required);
  const installedRequired = requiredCerts.filter(
    (c) => c.status === "installed",
  );
  const totalRequired = requiredCerts.length;
  const totalInstalled = installedRequired.length;

  // Determine which action is in flight and for which certificate.
  const [busyActionForCert, busyActionKind] = (() => {
    if (!actionId) return [null, null] as const;
    const [kind, certId] = actionId.split(":", 2);
    return [certId, kind as "install" | "test" | "delete"] as const;
  })();

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="certificate-manager-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      {/* Backdrop */}
      <button
        type="button"
        aria-label="Close certificate manager"
        onClick={() => {
          if (!isBusy) onClose();
        }}
        className="absolute inset-0 cursor-default bg-black/60 backdrop-blur-sm"
        tabIndex={-1}
      />

      {/* Dialog */}
      <div className="relative z-10 flex w-full max-w-3xl flex-col overflow-hidden rounded-xl border bg-card shadow-2xl max-h-[90vh]">
        {/* Header */}
        <div className="flex items-start justify-between gap-4 border-b px-5 py-4">
          <div className="flex items-start gap-3">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary ring-1 ring-primary/20"
              aria-hidden="true"
            >
              <Shield className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h2
                id="certificate-manager-title"
                className="truncate text-sm font-semibold text-foreground"
                title={`Certificates — ${displayName}`}
              >
                Certificates — {displayName}
              </h2>
              <p className="mt-1 text-[11px] text-muted-foreground">
                {totalRequired > 0 ? (
                  <>
                    {totalInstalled} of {totalRequired} required{" "}
                    {totalRequired === 1
                      ? "certificate"
                      : "certificates"}{" "}
                    installed
                  </>
                ) : certificates.length > 0 ? (
                  <>No required certificates; {certificates.length} optional</>
                ) : (
                  <>No certificates declared by this package</>
                )}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => void onRefresh()}
              disabled={isLoading || isBusy}
              aria-label="Refresh certificate status"
              className={[
                "rounded-md p-1.5 text-muted-foreground transition-colors",
                isLoading || isBusy
                  ? "cursor-wait opacity-40"
                  : "hover:bg-muted hover:text-foreground",
              ].join(" ")}
              title="Refresh status from the filesystem"
            >
              <RefreshCw
                className={[
                  "h-4 w-4",
                  isLoading ? "animate-spin" : "",
                ].join(" ")}
              />
            </button>
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
        </div>

        {/* Info banner */}
        <div className="border-b bg-muted/30 px-5 py-2.5">
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            Certificates are downloaded from their official source,
            verified against a SHA-256 hash, and installed on the
            PELDRUN backend host. The provider adapter reads them at
            request time to establish trusted TLS connections.
          </p>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto scrollbar-thin px-5 py-4">
          {isLoading && certificates.length === 0 && (
            <LoadingState />
          )}

          {error && certificates.length === 0 && !isLoading && (
            <ErrorState
              message={error}
              onRetry={() => void onRefresh()}
            />
          )}

          {!isLoading && !error && certificates.length === 0 && (
            <EmptyState />
          )}

          {certificates.length > 0 && (
            <div className="space-y-3">
              {certificates.map((cert) => (
                <CertificateCard
                  key={cert.certificate_id}
                  certificate={cert}
                  busyAction={
                    busyActionForCert === cert.certificate_id
                      ? busyActionKind
                      : null
                  }
                  isBusy={isBusy}
                  onInstall={() => void onInstall(cert.certificate_id)}
                  onTest={() => void onTest(cert.certificate_id)}
                  onDelete={() =>
                    void onDelete(cert.certificate_id, true)
                  }
                />
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="border-t px-5 py-3">
          <div className="flex items-center justify-between gap-2">
            <p className="text-[10px] text-muted-foreground">
              Managed by the PELDRUN Certificate Manager. Files are
              written atomically and verified before use.
            </p>
            <button
              type="button"
              onClick={onClose}
              disabled={isBusy}
              className="rounded-md border px-3 py-1.5 text-xs font-medium transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default CertificateManagerModal;
