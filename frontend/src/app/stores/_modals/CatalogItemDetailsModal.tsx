// frontend/src/app/stores/_modals/CatalogItemDetailsModal.tsx
"use client";

import React, { useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowUpCircle,
  CheckCircle2,
  Download,
  ExternalLink,
  FileArchive,
  GitCommitHorizontal,
  Globe,
  Loader2,
  Package,
  Plug,
  Shield,
  ShieldCheck,
  Sparkles,
  X,
  XCircle,
} from "lucide-react";
import type {
  ProviderCatalogInstallState,
  ProviderCatalogItem,
  ProviderTransport,
} from "../_types/store";
import { Icon } from "@/components/Icon/Icon";


// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface CatalogItemDetailsModalProps {
  /** Whether the modal is visible. */
  open: boolean;

  /** The catalog item being inspected, or ``null`` when closed. */
  item: ProviderCatalogItem | null;

  /**
   * Derived install state for this item. Computed by the parent
   * (``ProvidersSection``) by cross-referencing the item against the
   * installed ``providers`` list. ``null`` when ``item`` is null.
   */
  installState: ProviderCatalogInstallState | null;

  /**
   * True while an install / reinstall is in flight for this item.
   * The Install button shows a spinner and is disabled.
   */
  isInstalling: boolean;

  /**
   * Error from the last install attempt, or ``null``. Rendered as a
   * red banner above the Install button. Cleared when the user
   * reopens the modal.
   */
  installError: string | null;

  /** Called when the user clicks "Install" (not yet installed). */
  onInstall: () => Promise<void>;

  /**
   * Called when the user clicks "Reinstall" (installed, same
   * version) or "Update" (installed, different version). Same code
   * path as install on the backend — the catalog item's
   * ``download_url`` decides which bytes are fetched.
   */
  onReinstall: () => Promise<void>;

  /** Called when the user toggles enable/disable on an installed item. */
  onToggleProvider: (providerId: string) => void;

  /** Called when the user clicks "Configure" on an installed item. */
  onConfigure: (providerId: string) => void;

  /** Called when the user closes the modal. */
  onClose: () => void;
}


// ---------------------------------------------------------------------------
// Presentation helpers
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

const FEATURE_LABELS: Record<string, string> = {
  model_discovery: "Model discovery",
  native_streaming: "Streaming",
  buffered_streaming: "Buffered streaming",
  tool_calling: "Tool calling",
  parallel_tool_calls: "Parallel tools",
  forced_tool_choice: "Forced tool choice",
  vision: "Vision",
  audio: "Audio",
  structured_output: "Structured output",
  usage_reporting: "Usage reporting",
  reasoning: "Reasoning",
};

/**
 * Minimal client-side SVG sanitizer.
 *
 * The catalog's ``icon`` field can carry inline SVG markup. The
 * backend does not sanitize it (a catalog is a text file, not an
 * endpoint that returns HTML). We strip the obvious vectors here so
 * a malicious catalog author cannot run JS via a crafted icon.
 *
 * Mirrors the pattern used by ``SkillIcon.tsx``.
 */
function sanitizeSvg(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed.includes("<svg")) return null;
  return trimmed
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<foreignObject[\s\S]*?<\/foreignObject>/gi, "")
    .replace(/\son\w+\s*=\s*(".*?"|'.*?'|[^\s>]+)/gi, "")
    .replace(
      /(href|xlink:href|src)\s*=\s*("javascript:[^"]*"|'javascript:[^']*'|"data:[^"]*"|'data:[^']*')/gi,
      ""
    )
    .replace(/@import\s+url\s*\([^)]*\)/gi, "");
}

/**
 * Render the catalog item's icon, respecting its declared branding.
 *
 * Priority
 * --------
 *   1. ``icon_type === "name"`` — delegate to the shared ``Icon``
 *      component from ``@/components/Icon/Icon``. That component
 *      renders the SVG inline and honors ``currentColor``, so the
 *      wrapper ``<span>`` sets ``color: brand_color`` to tint the
 *      glyph. (A plain ``<img>`` tag cannot tint an external SVG —
 *      hence the switch from ``<img>`` to ``Icon``.)
 *
 *   2. ``icon_type === "svg"`` — inline sanitized SVG markup.
 *
 *   3. Fallback — neutral ``Plug`` glyph in ``brand_color``.
 */
function CatalogItemIcon({
  item,
  size = 40,
}: {
  item: ProviderCatalogItem;
  size?: number;
}) {
  const brand = item.brand_color || "currentColor";
  const iconValue = (item.icon || "").trim();
  const iconType = item.icon_type || "name";

  // Priority 1 — named icon via the shared Icon component.
  if (iconValue && iconType === "name") {
    return (
      <span
        style={{
          color: brand,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: size,
          height: size,
        }}
        aria-hidden="true"
      >
        <Icon
          name={iconValue}
          size={size}
          title={item.display_name || item.name || item.id}
        />
      </span>
    );
  }

  // Priority 2 — inline SVG supplied by the catalog author.
  if (iconValue && iconType === "svg") {
    const safe = sanitizeSvg(iconValue);
    if (safe) {
      return (
        <span
          style={{
            color: brand,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: size,
            height: size,
          }}
          aria-hidden="true"
          dangerouslySetInnerHTML={{ __html: safe }}
        />
      );
    }
  }

  // Priority 3 — neutral fallback in brand color.
  return (
    <span
      style={{
        color: brand,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: size,
        height: size,
      }}
      aria-hidden="true"
    >
      <Plug size={size * 0.6} />
    </span>
  );
}

/**
 * Section wrapper with a small uppercase label. Used throughout the
 * modal body so the visual rhythm matches ``ProviderDetailsDrawer``.
 */
function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground/80">
        {title}
      </h4>
      {children}
    </section>
  );
}


// ---------------------------------------------------------------------------
// Main modal
// ---------------------------------------------------------------------------

export function CatalogItemDetailsModal({
  open,
  item,
  installState,
  isInstalling,
  installError,
  onInstall,
  onReinstall,
  onToggleProvider,
  onConfigure,
  onClose,
}: CatalogItemDetailsModalProps) {
  // Track which screenshots are being previewed at full size, so a
  // simple click on a thumbnail can open a lightbox overlay without
  // another modal component.
  const [activeScreenshot, setActiveScreenshot] = useState<string | null>(
    null,
  );

  // Sorted feature entries — stable order across renders so the
  // chips do not shuffle when unrelated state updates.
  const featureEntries = useMemo<Array<[string, boolean]>>(() => {
    if (!item?.features) return [];
    return Object.entries(item.features)
      .filter(([, v]) => v === true)
      .sort(([a], [b]) => a.localeCompare(b));
  }, [item?.features]);

  // ESC closes the modal (or the lightbox if one is open).
  React.useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (activeScreenshot) {
        setActiveScreenshot(null);
      } else if (!isInstalling) {
        onClose();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, isInstalling, activeScreenshot, onClose]);

  // Reset the lightbox when the modal closes so reopening it does
  // not show a stale screenshot.
  React.useEffect(() => {
    if (!open) setActiveScreenshot(null);
  }, [open]);

  if (!open || !item || !installState) return null;

  const displayName =
    item.display_name || item.name || item.id;
  const transport =
    (item.transport as ProviderTransport) ?? "native";
  const badgeClass =
    TRANSPORT_BADGE_CLASSES[transport] ?? TRANSPORT_BADGE_CLASSES.native;

  const developer = item.developer;
  const legacyAuthor = item.author;
  const developerName = developer?.name || legacyAuthor || null;
  const developerUrl = developer?.url || item.author_url || null;
  const developerVerified = Boolean(developer?.verified);

  const canInstallNow = item.installable && !isInstalling;
  const isVersionMismatch =
    installState.isInstalled &&
    installState.installedVersion !== null &&
    installState.installedVersion !== item.version;

  // ── Install-state label + primary action ─────────────────────
  const renderPrimaryAction = () => {
    if (installState.isInstalled) {
      // Installed: split into "Update / Reinstall" (secondary) and
      // "Configure" (primary), matching the installed card pattern
      // elsewhere in the store.
      return (
        <div className="flex flex-col gap-2">
          {isVersionMismatch && (
            <button
              type="button"
              onClick={() => void onReinstall()}
              disabled={!item.installable || isInstalling}
              className={[
                "inline-flex items-center justify-center gap-1.5 rounded-md px-3.5 py-2 text-sm font-medium transition-colors",
                !item.installable || isInstalling
                  ? "cursor-not-allowed border bg-muted text-muted-foreground"
                  : "bg-primary text-primary-foreground hover:bg-primary/90",
              ].join(" ")}
              title={
                item.installable
                  ? `Update from v${installState.installedVersion} to v${item.version}`
                  : item.installability_reason
              }
            >
              {isInstalling ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Updating…
                </>
              ) : (
                <>
                  <ArrowUpCircle className="h-4 w-4" />
                  Update to v{item.version}
                </>
              )}
            </button>
          )}

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => onConfigure(item.id)}
              disabled={isInstalling}
              className="inline-flex items-center gap-1.5 rounded-md border bg-background px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Plug className="h-4 w-4" />
              Configure Connection
            </button>

            {!isVersionMismatch && (
              <button
                type="button"
                onClick={() => void onReinstall()}
                disabled={!item.installable || isInstalling}
                className={[
                  "inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm font-medium transition-colors",
                  !item.installable || isInstalling
                    ? "cursor-not-allowed opacity-50"
                    : "text-foreground hover:bg-muted",
                ].join(" ")}
                title="Re-download and reinstall this exact version"
              >
                {isInstalling ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Reinstalling…
                  </>
                ) : (
                  <>
                    <Download className="h-4 w-4" />
                    Reinstall
                  </>
                )}
              </button>
            )}
          </div>
        </div>
      );
    }

    // Not installed: primary "Install" button.
    return (
      <button
        type="button"
        onClick={() => void onInstall()}
        disabled={!canInstallNow}
        className={[
          "inline-flex items-center justify-center gap-1.5 rounded-md px-3.5 py-2 text-sm font-medium transition-colors",
          canInstallNow
            ? "bg-primary text-primary-foreground hover:bg-primary/90"
            : "cursor-not-allowed border bg-muted text-muted-foreground",
        ].join(" ")}
        title={item.installability_reason}
      >
        {isInstalling ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            Installing…
          </>
        ) : item.installable ? (
          <>
            <Download className="h-4 w-4" />
            Download &amp; Install
          </>
        ) : (
          <>
            <Package className="h-4 w-4" />
            Preview only
          </>
        )}
      </button>
    );
  };

  // ── Install-state badge ──────────────────────────────────────
  const renderInstallBadge = () => {
    if (!installState.isInstalled) {
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-muted-foreground/20 bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
          <Package className="h-2.5 w-2.5" />
          Not installed
        </span>
      );
    }

    if (installState.isEnabled) {
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
          <CheckCircle2 className="h-2.5 w-2.5" />
          Installed &amp; enabled
        </span>
      );
    }

    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
        <XCircle className="h-2.5 w-2.5" />
        Installed but disabled
      </span>
    );
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="catalog-details-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      {/* Backdrop */}
      <button
        type="button"
        aria-label="Close catalog details"
        onClick={() => {
          if (!isInstalling) onClose();
        }}
        className="absolute inset-0 cursor-default bg-black/60 backdrop-blur-sm"
        tabIndex={-1}
      />

      {/* Dialog */}
      <div className="relative z-10 flex w-full max-w-3xl flex-col overflow-hidden rounded-xl border bg-card shadow-2xl max-h-[90vh]">
        {/* ── Header ────────────────────────────────────────────── */}
        <div className="flex items-start justify-between gap-4 border-b px-5 py-4">
          <div className="flex items-start gap-3 min-w-0 flex-1">
            {/* Icon container: tinted with the item's brand color.
                Falls back to the neutral muted surface when the
                catalog author did not declare ``brand_color``. */}
            <div
              className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg border"
              style={{
                backgroundColor: item.brand_color
                  ? `${item.brand_color}1a` /* ~10% opacity */
                  : undefined,
                borderColor: item.brand_color
                  ? `${item.brand_color}33` /* ~20% opacity */
                  : undefined,
              }}
              aria-hidden="true"
            >
              <CatalogItemIcon item={item} size={28} />
            </div>
            <div className="min-w-0 flex-1">
              <h2
                id="catalog-details-title"
                className="truncate text-base font-semibold text-foreground"
                title={displayName}
              >
                {displayName}
              </h2>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                  v{item.version}
                </span>
                <span
                  className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${badgeClass}`}
                >
                  {TRANSPORT_LABELS[transport] ?? transport}
                </span>
                {renderInstallBadge()}
                {item.license && (
                  <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 text-[11px] font-mono text-muted-foreground">
                    {item.license}
                  </span>
                )}
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={onClose}
            disabled={isInstalling}
            aria-label="Close"
            className={[
              "rounded-md p-1 text-muted-foreground transition-colors",
              isInstalling
                ? "cursor-not-allowed opacity-40"
                : "hover:bg-muted hover:text-foreground",
            ].join(" ")}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* ── Body ──────────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto scrollbar-thin px-5 py-5 space-y-5">
          {/* Description */}
          <p className="text-sm leading-relaxed text-muted-foreground">
            {item.description?.trim() || "No description provided."}
          </p>

          {/* Feature chips */}
          {featureEntries.length > 0 && (
            <Section title="Capabilities">
              <div className="flex flex-wrap gap-1.5">
                {featureEntries.map(([key]) => {
                  const label = FEATURE_LABELS[key] ?? key.replace(/_/g, " ");
                  return (
                    <span
                      key={key}
                      className="inline-flex items-center gap-1 rounded-md bg-muted/60 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
                    >
                      <Sparkles className="h-3 w-3" />
                      {label}
                    </span>
                  );
                })}
              </div>
            </Section>
          )}

          {/* Developer card */}
          {developerName && (
            <Section title="Published by">
              <div className="flex items-start gap-3 rounded-lg border bg-muted/20 p-3">
                {developer?.avatar_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={developer.avatar_url}
                    alt=""
                    aria-hidden="true"
                    className="h-10 w-10 shrink-0 rounded-full object-cover ring-1 ring-border"
                  />
                ) : (
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
                    {developerName.charAt(0).toUpperCase()}
                  </div>
                )}

                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate text-sm font-semibold text-foreground">
                      {developerName}
                    </span>
                    {developerVerified && (
                      <span title="Verified publisher">
                        <ShieldCheck className="h-4 w-4 text-emerald-500" />
                      </span>
                    )}
                  </div>

                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px]">
                    {developerUrl && (
                      <a
                        href={developerUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-primary hover:underline"
                      >
                        <Globe className="h-3 w-3" />
                        Website
                      </a>
                    )}
                    {developer?.github_username && (
                      <a
                        href={`https://github.com/${developer.github_username}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-primary hover:underline"
                      >
                        <GitCommitHorizontal className="h-3 w-3" />
                        {developer.github_username}
                      </a>
                    )}
                    {item.repository_url && (
                      <a
                        href={item.repository_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-primary hover:underline"
                      >
                        <GitCommitHorizontal className="h-3 w-3" />
                        Repository
                      </a>
                    )}
                  </div>
                </div>
              </div>
            </Section>
          )}

          {/* Project links */}
          {(item.homepage ||
            item.docs_url ||
            item.changelog_url ||
            item.repository_url) && (
            <Section title="Links">
              <div className="flex flex-wrap gap-2">
                {item.homepage && (
                  <a
                    href={item.homepage}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 rounded-md border bg-background px-2.5 py-1 text-[11px] font-medium text-foreground transition-colors hover:bg-muted"
                  >
                    <Globe className="h-3 w-3" />
                    Homepage
                    <ExternalLink className="h-2.5 w-2.5 text-muted-foreground" />
                  </a>
                )}
                {item.docs_url && (
                  <a
                    href={item.docs_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 rounded-md border bg-background px-2.5 py-1 text-[11px] font-medium text-foreground transition-colors hover:bg-muted"
                  >
                    Documentation
                    <ExternalLink className="h-2.5 w-2.5 text-muted-foreground" />
                  </a>
                )}
                {item.changelog_url && (
                  <a
                    href={item.changelog_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 rounded-md border bg-background px-2.5 py-1 text-[11px] font-medium text-foreground transition-colors hover:bg-muted"
                  >
                    Changelog
                    <ExternalLink className="h-2.5 w-2.5 text-muted-foreground" />
                  </a>
                )}
              </div>
            </Section>
          )}

          {/* Screenshots */}
          {item.screenshots && item.screenshots.length > 0 && (
            <Section title="Screenshots">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {item.screenshots.map((url, idx) => (
                  <button
                    key={`${url}-${idx}`}
                    type="button"
                    onClick={() => setActiveScreenshot(url)}
                    className="group relative overflow-hidden rounded-md border bg-muted/30 transition-colors hover:border-primary/40"
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={url}
                      alt={`Screenshot ${idx + 1}`}
                      className="h-24 w-full object-cover transition-transform group-hover:scale-105"
                    />
                  </button>
                ))}
              </div>
            </Section>
          )}

          {/* Permission warnings */}
          {item.permission_warnings && item.permission_warnings.length > 0 && (
            <Section title="Runtime access">
              <div className="space-y-1.5">
                {item.permission_warnings.map((w, idx) => (
                  <div
                    key={idx}
                    className="flex items-start gap-2 rounded-md border border-amber-500/20 bg-amber-500/5 px-2.5 py-1.5"
                  >
                    <Shield className="mt-0.5 h-3 w-3 shrink-0 text-amber-600 dark:text-amber-400" />
                    <p className="text-[11px] leading-relaxed text-amber-900/80 dark:text-amber-200/70">
                      {w}
                    </p>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {/* Certificates */}
          {item.certificates && item.certificates.length > 0 && (
            <Section title="Required TLS certificates">
              <div className="space-y-1.5">
                {item.certificates.map((cert) => (
                  <div
                    key={cert.id}
                    className="rounded-md border bg-muted/20 px-2.5 py-1.5"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-medium text-foreground">
                        {cert.display_name}
                      </span>
                      <span className="text-[10px] font-mono uppercase text-muted-foreground">
                        {cert.certificate_type}
                      </span>
                    </div>
                    {cert.description && (
                      <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
                        {cert.description}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </Section>
          )}

          {/* Connection fields */}
          {item.connection_fields && item.connection_fields.length > 0 && (
            <Section title="Connection setup">
              <div className="space-y-1">
                {item.connection_fields.map((f) => (
                  <div
                    key={f.key}
                    className="flex items-center justify-between gap-2 rounded-md bg-muted/30 px-2.5 py-1"
                  >
                    <span className="text-[11px] font-mono text-muted-foreground">
                      {f.key}
                    </span>
                    <span className="text-[11px] text-foreground">
                      {f.label}
                      {f.required && (
                        <span className="ml-1 text-destructive">*</span>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {/* Install error */}
          {installError && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3"
            >
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
              <p className="text-[11px] leading-relaxed text-destructive">
                {installError}
              </p>
            </div>
          )}

          {/* Install-state summary */}
          {installState.isInstalled && (
            <Section title="Installed state">
              <div className="space-y-1 rounded-md border bg-muted/20 p-2.5 text-[11px]">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">Version</span>
                  <span className="font-mono text-foreground">
                    v{installState.installedVersion}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">Enabled</span>
                  <span
                    className={
                      installState.isEnabled
                        ? "font-medium text-emerald-600 dark:text-emerald-400"
                        : "font-medium text-amber-600 dark:text-amber-400"
                    }
                  >
                    {installState.isEnabled ? "Yes" : "No"}
                  </span>
                </div>
                {installState.installedVersions.length > 1 && (
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-muted-foreground">
                      All versions
                    </span>
                    <span className="font-mono text-foreground">
                      {installState.installedVersions
                        .map((v) => `v${v}`)
                        .join(", ")}
                    </span>
                  </div>
                )}
                {installState.installedPackage?.has_active_connection ? (
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-muted-foreground">Connection</span>
                    <span className="font-medium text-emerald-600 dark:text-emerald-400">
                      {installState.installedPackage
                        .active_connection_name || "Connected"}
                    </span>
                  </div>
                ) : (
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-muted-foreground">Connection</span>
                    <span className="font-medium text-amber-600 dark:text-amber-400">
                      Not configured
                    </span>
                  </div>
                )}

                <div className="mt-2 border-t pt-2">
                  <label className="flex cursor-pointer items-center justify-between gap-2">
                    <span className="text-muted-foreground">
                      {installState.isEnabled ? "Disable provider" : "Enable provider"}
                    </span>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={installState.isEnabled}
                      onClick={() =>
                        installState.installedPackage &&
                        onToggleProvider(installState.installedPackage.id)
                      }
                      disabled={isInstalling}
                      className={[
                        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors",
                        installState.isEnabled
                          ? "bg-emerald-500 hover:bg-emerald-600"
                          : "bg-muted-foreground/30 hover:bg-muted-foreground/50",
                        isInstalling
                          ? "cursor-not-allowed opacity-60"
                          : "cursor-pointer",
                      ].join(" ")}
                    >
                      <span
                        className={[
                          "inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform",
                          installState.isEnabled
                            ? "translate-x-4"
                            : "translate-x-0.5",
                        ].join(" ")}
                      />
                    </button>
                  </label>
                </div>
              </div>
            </Section>
          )}
        </div>

        {/* ── Footer ────────────────────────────────────────────── */}
        <div className="flex flex-col gap-2 border-t px-5 py-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[10px] text-muted-foreground">
            {installState.isInstalled
              ? "Installed on this PELDRUN instance."
              : item.installable
              ? "Ready to install. The ZIP will be verified against its SHA-256 before extraction."
              : item.installability_reason}
          </p>

          <div className="flex items-center gap-2 sm:shrink-0">
            <button
              type="button"
              onClick={onClose}
              disabled={isInstalling}
              className="rounded-md border px-3 py-1.5 text-xs font-medium transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
            >
              Close
            </button>
            {renderPrimaryAction()}
          </div>
        </div>
      </div>

      {/* Screenshot lightbox */}
      {activeScreenshot && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Screenshot preview"
          className="absolute inset-0 z-20 flex items-center justify-center bg-black/80 p-6"
          onClick={() => setActiveScreenshot(null)}
        >
          <button
            type="button"
            onClick={() => setActiveScreenshot(null)}
            aria-label="Close preview"
            className="absolute top-4 right-4 rounded-md bg-white/10 p-2 text-white hover:bg-white/20"
          >
            <X className="h-5 w-5" />
          </button>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={activeScreenshot}
            alt="Screenshot preview"
            className="max-h-full max-w-full rounded-lg shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  );
}

export default CatalogItemDetailsModal;
