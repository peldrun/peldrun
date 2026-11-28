// frontend/src/app/stores/_modals/CatalogAgentDetailsModal.tsx
"use client";

import React, { useMemo } from "react";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  Download,
  ExternalLink,
  Eye,
  FileText,
  GitBranch,
  Globe,
  Scale,
  Loader2,
  Mail,
  Shield,
  Sparkles,
  Tag,
  User,
  X,
} from "lucide-react";
import type {
  AgentCatalogItem,
  AgentCatalogInstallState,
  DeveloperInfo,
} from "../_types/store";

// ---------------------------------------------------------------------------
// Inline SVG sanitizer (defense-in-depth, mirrors backend _sanitize_svg)
// ---------------------------------------------------------------------------

function sanitizeSvg(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed.toLowerCase().includes("<svg")) return null;
  return trimmed
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<foreignObject[\s\S]*?<\/foreignObject>/gi, "")
    .replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "")
    .replace(
      /(href|xlink:href|src)\s*=\s*("javascript:[^"]*"|'javascript:[^']*'|"data:[^"]*"|'data:[^']*')/gi,
      "",
    )
    .replace(/@import\s+url\s*\([^)]*\)/gi, "");
}

// ---------------------------------------------------------------------------
// Icon renderer (supports both inline SVG and Lucide name fallback)
// ---------------------------------------------------------------------------

interface AgentIconProps {
  item: AgentCatalogItem;
  size?: number;
}

function AgentIcon({ item, size = 48 }: AgentIconProps) {
  const brand = item.brand_color || "currentColor";
  const iconValue = (item.icon || "").trim();
  const iconType = item.icon_type || "name";

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

  // Lucide name fallback or default Bot
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
      <Bot size={size * 0.65} />
    </span>
  );
}

// ---------------------------------------------------------------------------
// Developer block (rich publisher info)
// ---------------------------------------------------------------------------

interface DeveloperBlockProps {
  developer: DeveloperInfo | null | undefined;
  fallbackAuthor: string | null;
  fallbackAuthorUrl: string | null | undefined;
}

function DeveloperBlock({
  developer,
  fallbackAuthor,
  fallbackAuthorUrl,
}: DeveloperBlockProps) {
  const name = developer?.name || fallbackAuthor;
  if (!name) return null;

  return (
    <div className="flex items-center gap-3 rounded-md border border-border bg-muted/30 p-3">
      {developer?.avatar_url ? (
        <img
          src={developer.avatar_url}
          alt={name}
          className="h-10 w-10 rounded-full border border-border object-cover"
          onError={(e) => {
            (e.target as HTMLImageElement).style.display = "none";
          }}
        />
      ) : (
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted">
          <User className="h-5 w-5 text-muted-foreground" />
        </div>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-sm font-semibold text-foreground">
            {name}
          </span>
          {developer?.verified && (
            <Shield
              className="h-3.5 w-3.5 shrink-0 text-emerald-500"
              aria-label="Verified publisher"
            />
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
          {developer?.url && (
            <a
              href={developer.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 hover:text-foreground"
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
              className="inline-flex items-center gap-1 hover:text-foreground"
            >
              <GitBranch className="h-3 w-3" />
              @{developer.github_username}
            </a>
          )}
          {developer?.email && (
            <a
              href={`mailto:${developer.email}`}
              className="inline-flex items-center gap-1 hover:text-foreground"
            >
              <Mail className="h-3 w-3" />
              Email
            </a>
          )}
          {!developer && fallbackAuthorUrl && (
            <a
              href={fallbackAuthorUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 hover:text-foreground"
            >
              <ExternalLink className="h-3 w-3" />
              Profile
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stability badge
// ---------------------------------------------------------------------------

function StabilityBadge({ stability }: { stability: AgentCatalogItem["stability"] }) {
  const config = {
    stable: { label: "Stable", className: "bg-emerald-500/10 text-emerald-700 border-emerald-500/20 dark:text-emerald-300" },
    beta: { label: "Beta", className: "bg-blue-500/10 text-blue-700 border-blue-500/20 dark:text-blue-300" },
    experimental: { label: "Experimental", className: "bg-amber-500/10 text-amber-700 border-amber-500/20 dark:text-amber-300" },
    deprecated: { label: "Deprecated", className: "bg-red-500/10 text-red-700 border-red-500/20 dark:text-red-300" },
  } as const;

  const { label, className } = config[stability] || config.stable;

  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${className}`}
    >
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Trust badge
// ---------------------------------------------------------------------------

function TrustBadge({ trust }: { trust: AgentCatalogItem["trust"] }) {
  if (trust === "verified") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
        <Shield className="h-3 w-3" />
        Verified
      </span>
    );
  }
  if (trust === "community") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-blue-500/20 bg-blue-500/10 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:text-blue-300">
        <User className="h-3 w-3" />
        Community
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
      Unverified
    </span>
  );
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface CatalogAgentDetailsModalProps {
  open: boolean;
  item: AgentCatalogItem | null;
  installState: AgentCatalogInstallState;
  isInstalling: boolean;
  onInstall: () => void;
  onClose: () => void;
  onOpenInstalled?: () => void;
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------

export function CatalogAgentDetailsModal({
  open,
  item,
  installState,
  isInstalling,
  onInstall,
  onClose,
  onOpenInstalled,
}: CatalogAgentDetailsModalProps) {
  // Derive the primary action button based on install state
  const primaryAction = useMemo(() => {
    if (!item) return null;

    if (!item.installable) {
      return {
        label: "Preview Only",
        icon: Eye,
        onClick: null,
        disabled: true,
        variant: "muted" as const,
        title: item.installability_reason,
      };
    }

    if (installState.isInstalled) {
      const hasUpdate =
        installState.installedVersion !== null &&
        installState.installedVersion !== item.version;

      if (hasUpdate) {
        return {
          label: `Update to v${item.version}`,
          icon: Download,
          onClick: onInstall,
          disabled: isInstalling,
          variant: "primary" as const,
          title: undefined,
        };
      }

      if (onOpenInstalled) {
        return {
          label: "View Installed",
          icon: CheckCircle2,
          onClick: onOpenInstalled,
          disabled: false,
          variant: "success" as const,
          title: undefined,
        };
      }

      return {
        label: "Installed",
        icon: CheckCircle2,
        onClick: null,
        disabled: true,
        variant: "success" as const,
        title: undefined,
      };
    }

    return {
      label: "Install",
      icon: Download,
      onClick: onInstall,
      disabled: isInstalling,
      variant: "primary" as const,
      title: undefined,
    };
  }, [item, installState, isInstalling, onInstall, onOpenInstalled]);

  if (!open || !item) return null;

  const displayName = item.display_name || item.name || item.id;
  const hasUpdate =
    installState.isInstalled &&
    installState.installedVersion !== null &&
    installState.installedVersion !== item.version;

  const variantClasses: Record<string, string> = {
    primary:
      "bg-primary text-primary-foreground hover:bg-primary/90 cursor-pointer",
    success:
      "border border-emerald-500/30 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/10 cursor-pointer",
    muted: "cursor-not-allowed border bg-muted text-muted-foreground",
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
    >
      <div
        className="relative flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ── Header ─────────────────────────────────────────────── */}
        <div className="flex items-start gap-4 border-b border-border p-6">
          <AgentIcon item={item} size={56} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="truncate text-lg font-semibold text-foreground">
                {displayName}
              </h2>
              <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                v{item.version}
              </span>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <TrustBadge trust={item.trust} />
              <StabilityBadge stability={item.stability} />
              {installState.isInstalled && (
                <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
                  <CheckCircle2 className="h-2.5 w-2.5" />
                  Installed
                  {hasUpdate && (
                    <span className="ml-1 text-blue-600 dark:text-blue-400">
                      · update available
                    </span>
                  )}
                </span>
              )}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* ── Scrollable body ────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          {/* Description */}
          <p className="text-sm leading-relaxed text-foreground">
            {item.description || "No description provided."}
          </p>

          {/* Developer */}
          <DeveloperBlock
            developer={item.developer}
            fallbackAuthor={item.author}
            fallbackAuthorUrl={item.author_url}
          />

          {/* Tags */}
          {item.tags && item.tags.length > 0 && (
            <div>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Tags
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {item.tags.map((tag) => (
                  <span
                    key={tag}
                    className="inline-flex items-center gap-1 rounded-md border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                  >
                    <Tag className="h-3 w-3 opacity-60" />
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Install warnings */}
          {item.install_warnings && item.install_warnings.length > 0 && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
                <div>
                  <h3 className="text-xs font-semibold text-amber-900 dark:text-amber-200">
                    Before installing
                  </h3>
                  <ul className="mt-1.5 space-y-1 text-xs text-amber-800 dark:text-amber-300/80">
                    {item.install_warnings.map((warning, idx) => (
                      <li key={idx} className="leading-relaxed">
                        • {warning}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          )}

          {/* Metadata grid */}
          <div className="grid grid-cols-2 gap-3 text-xs">
            {item.license && (
              <div className="flex items-center gap-2 rounded-md border border-border bg-muted/30 p-2.5">
                <FileText className="h-3.5 w-3.5 text-muted-foreground" />
                <div className="min-w-0">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    License
                  </div>
                  <div className="truncate font-medium text-foreground">
                    {item.license}
                  </div>
                </div>
              </div>
            )}
            {item.release_date && (
              <div className="flex items-center gap-2 rounded-md border border-border bg-muted/30 p-2.5">
                <Sparkles className="h-3.5 w-3.5 text-muted-foreground" />
                <div className="min-w-0">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    Released
                  </div>
                  <div className="truncate font-medium text-foreground">
                    {item.release_date}
                  </div>
                </div>
              </div>
            )}
            {item.homepage && (
              <a
                href={item.homepage}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 rounded-md border border-border bg-muted/30 p-2.5 hover:bg-muted"
              >
                <Globe className="h-3.5 w-3.5 text-muted-foreground" />
                <div className="min-w-0">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    Homepage
                  </div>
                  <div className="truncate font-medium text-foreground">
                    Visit site
                  </div>
                </div>
              </a>
            )}
            {item.docs_url && (
              <a
                href={item.docs_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 rounded-md border border-border bg-muted/30 p-2.5 hover:bg-muted"
              >
                <FileText className="h-3.5 w-3.5 text-muted-foreground" />
                <div className="min-w-0">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    Documentation
                  </div>
                  <div className="truncate font-medium text-foreground">
                    Read docs
                  </div>
                </div>
              </a>
            )}
            {item.repository_url && (
              <a
                href={item.repository_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 rounded-md border border-border bg-muted/30 p-2.5 hover:bg-muted"
              >
                <GitBranch className="h-3.5 w-3.5 text-muted-foreground" />
                <div className="min-w-0">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    Repository
                  </div>
                  <div className="truncate font-medium text-foreground">
                    View source
                  </div>
                </div>
              </a>
            )}
          </div>

          {/* Deprecation notice */}
          {item.stability === "deprecated" && item.deprecation_message && (
            <div className="rounded-md border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-800 dark:text-red-300/80">
              <strong>Deprecated:</strong> {item.deprecation_message}
            </div>
          )}

          {/* Installed version info */}
          {installState.isInstalled && installState.installedVersion && (
            <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Installed version</span>
                <span className="font-mono font-medium text-foreground">
                  v{installState.installedVersion}
                </span>
              </div>
              {hasUpdate && (
                <div className="mt-1 flex items-center justify-between">
                  <span className="text-muted-foreground">Available</span>
                  <span className="font-mono font-medium text-blue-600 dark:text-blue-400">
                    v{item.version}
                  </span>
                </div>
              )}
            </div>
          )}
        </div>

        {/* ── Footer with action button ──────────────────────────── */}
        <div className="flex items-center justify-end gap-2 border-t border-border bg-muted/20 p-4">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border bg-background px-4 py-2 text-sm font-medium text-foreground hover:bg-muted"
          >
            Close
          </button>
          {primaryAction && (
            <button
              type="button"
              onClick={primaryAction.onClick || undefined}
              disabled={primaryAction.disabled}
              title={primaryAction.title}
              className={`inline-flex items-center gap-1.5 rounded-md px-4 py-2 text-sm font-medium transition-colors ${
                variantClasses[primaryAction.variant]
              }`}
            >
              {isInstalling ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Installing…
                </>
              ) : (
                <>
                  <primaryAction.icon className="h-4 w-4" />
                  {primaryAction.label}
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default CatalogAgentDetailsModal;
