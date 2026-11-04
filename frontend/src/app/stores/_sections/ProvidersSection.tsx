// frontend/src/app/stores/_sections/ProvidersSection.tsx
"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Download,
  Eye,
  FileArchive,
  Loader2,
  Plug,
  RefreshCw,
  Shield,
  Sparkles,
  Wrench,
  XCircle,
} from "lucide-react";
import type {
  ProviderCatalogInstallState,
  ProviderCatalogItem,
  ProviderFilter,
  ProviderPackageEntry,
  ProviderTransport,
} from "../_types/store";
import type { SectionHeaderConfig } from "../_config/sections";
import { ProviderPackageCard } from "../_components/ProviderPackageCard";
import { CatalogItemDetailsModal } from "../_modals/CatalogItemDetailsModal";
import { Icon } from "@/components/Icon/Icon";


// ---------------------------------------------------------------------------
// Props contract
// ---------------------------------------------------------------------------

export interface ProvidersSectionProps {
  // ── Installed packages ────────────────────────────────────────
  providers: ProviderPackageEntry[];
  searchQuery: string;
  providerFilter?: ProviderFilter; // optional; defaults to "all"

  // ── Catalog ───────────────────────────────────────────────────
  catalogItems: ProviderCatalogItem[];
  isCatalogLoading: boolean;
  isRefreshingCatalog: boolean;
  catalogError: string | null;
  onLoadCatalog: () => Promise<void>;
  onRefreshCatalog: () => Promise<void>;
  onInstallFromCatalog: (itemId: string) => Promise<void>;
  installingCatalogId: string | null;

  // ── Per-item action states (targeted at specific provider IDs) ─
  togglingProviderId: string | null;
  deletingProviderId: string | null;
  activatingProviderId: string | null;

  // ── List actions ──────────────────────────────────────────────
  onToggleProvider: (providerId: string) => void;
  onDeleteProvider: (provider: ProviderPackageEntry) => void;
  onActivateVersion: (
    provider: ProviderPackageEntry,
    version: string,
  ) => void;
  onOpenDetails: (providerId: string) => void;

  // ── Configure modal ───────────────────────────────────────────
  /**
   * Called when the user clicks "Configure" on a provider card.
   * Opens the ProviderConfigureModal with the package's connection
   * list. The connection state is managed by useProviderActions,
   * not by this section — this prop is just the trigger.
   */
  onOpenConfigure: (providerId: string) => void;

  // ── Certificate Manager modal ─────────────────────────────────
  /**
   * Called when the user clicks "Certificates" on a provider card.
   * Opens the CertificateManagerModal, which shows the TLS
   * certificates the package declares and offers a one-click
   * install flow.
   *
   * Only rendered by cards whose ``has_certificates`` field is true
   * — packages that do not declare certificates never trigger this.
   */
  onManageCertificates: (providerId: string) => void;

  // ── Upload modal trigger ──────────────────────────────────────
  onOpenUpload: () => void;
}


// ---------------------------------------------------------------------------
// Tab types
// ---------------------------------------------------------------------------

type TabId = "installed" | "catalog";


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


// ---------------------------------------------------------------------------
// Catalog item card (simpler than the installed-package card)
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Inline SVG sanitizer (defense-in-depth for catalog-authored icons)
// ---------------------------------------------------------------------------

/**
 * Strip dangerous vectors from an inline SVG string before it is
 * rendered with ``dangerouslySetInnerHTML``. The backend does not
 * sanitize catalog-supplied SVG (a catalog is a text file, not an
 * HTML response), so the frontend must guard against script tags,
 * event handlers, and javascript: hrefs.
 *
 * Mirrors the pattern used by ``SkillIcon.tsx``.
 */
function sanitizeSvgInline(raw: string): string | null {
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
 * Render a catalog item's icon, respecting its declared branding.
 *
 * Priority
 * --------
 *   1. ``icon_type === "name"`` — delegate to the shared ``Icon``
 *      component from ``@/components/Icon/Icon``. That component
 *      renders the SVG inline and honors ``currentColor``, which
 *      lets us tint it with ``brand_color`` by setting ``color`` on
 *      the wrapper ``<span>``. (A plain ``<img>`` tag cannot tint an
 *      external SVG — hence the switch from ``<img>`` to ``Icon``.)
 *
 *   2. ``icon_type === "svg"`` — inline sanitized SVG markup. Used
 *      when the catalog author wants to ship a custom icon that is
 *      not part of PELDRUN's bundled ``public/icon/`` inventory.
 *
 *   3. Fallback — the neutral ``Plug`` glyph in ``brand_color``.
 *
 * The wrapper is always a fixed-size ``<span>`` so the surrounding
 * row layout never shifts when an icon is missing or slow to load.
 */
function CatalogItemIconInline({
  item,
  size = 20,
}: {
  item: ProviderCatalogItem;
  size?: number;
}) {
  const brand = item.brand_color || "currentColor";
  const iconValue = (item.icon || "").trim();
  const iconType = item.icon_type || "name";

  // Priority 1 — named icon via the shared Icon component.
  // This is the recommended path: the icon name is resolved against
  // the bundled inventory, and `currentColor` inherits `brand`.
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
    const safe = sanitizeSvgInline(iconValue);
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
      <Plug style={{ width: size * 0.8, height: size * 0.8 }} />
    </span>
  );
}


// ---------------------------------------------------------------------------
// Catalog item card
// ---------------------------------------------------------------------------

interface CatalogItemCardProps {
  item: ProviderCatalogItem;

  /**
   * Derived install state for this item, computed by the parent by
   * joining against the installed ``providers`` list. Drives the
   * badges and the primary action ("Install" vs "Update" vs
   * "Configure").
   */
  installState: ProviderCatalogInstallState;

  /** True while an install / update is in flight for this item. */
  installing: boolean;

  /** Trigger install (or update) for this item. */
  onInstall: () => void;

  /** Open the details modal for this item. */
  onOpenDetails: () => void;

  /** Open the Configure modal (only meaningful when installed). */
  onConfigure: (providerId: string) => void;

  /** Toggle enable/disable (only meaningful when installed). */
  onToggleProvider: (providerId: string) => void;
}

function CatalogItemCard({
  item,
  installState,
  installing,
  onInstall,
  onOpenDetails,
  onConfigure,
  onToggleProvider,
}: CatalogItemCardProps) {
  const displayName = item.display_name || item.name || item.id;
  const transport =
    (item.transport as ProviderTransport) ?? "native";
  const badgeClass =
    TRANSPORT_BADGE_CLASSES[transport] ?? TRANSPORT_BADGE_CLASSES.native;

  const hasVersionMismatch =
    installState.isInstalled &&
    installState.installedVersion !== null &&
    installState.installedVersion !== item.version;

  const canInstall = item.installable && !installing;

  // ---- Install-state badge --------------------------------------
  const renderStateBadge = () => {
    if (!installState.isInstalled) return null;
    if (!installState.isEnabled) {
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
          <XCircle className="h-2.5 w-2.5" />
          Disabled
        </span>
      );
    }
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
        <CheckCircle2 className="h-2.5 w-2.5" />
        Installed
      </span>
    );
  };

  return (
    <article
      className={[
        "group relative flex flex-col sm:flex-row  items-center gap-4 ",
        "p-4 rounded-sm border transition-all hover:shadow-md",
        "border-card border-l-[5px] bg-card hover:border-card",
      ].join(" ")}

      style={{
            borderLeftColor: item.brand_color
              ? `${item.brand_color}` /* ~20% opacity */
              : undefined,
          }}

    >
      {/* ── Left: icon + content ─────────────────────────────── */}
      <div className="flex items-center gap-3 sm:gap-4 flex-1 min-w-0">
        {/* Icon container: tinted with the item's brand color.
            Falls back to the neutral muted surface when the
            catalog author did not declare ``brand_color``.
            The 8-digit hex form (``#RRGGBBAA``) is CSS-native and
            works in every modern browser — no runtime color
            parsing needed on the client. */}
        <div
          className="shrink-0 flex h-full w-20 items-center justify-center"

          aria-hidden="true"
        >
          <CatalogItemIconInline item={item} size={55} />
        </div>

        <div className="min-w-0 flex-1">
          {/* Title + badges row */}
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <h3
              className="truncate text-sm font-semibold text-foreground"
              title={displayName}
            >
              {displayName}
            </h3>
            <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              v{item.version}
            </span>
            <span
              className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${badgeClass}`}
            >
              {TRANSPORT_LABELS[transport] ?? transport}
            </span>
            {renderStateBadge()}
            {hasVersionMismatch && (
              <span className="inline-flex items-center gap-1 rounded-full border border-blue-500/20 bg-blue-500/10 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:text-blue-300">
                Update available
              </span>
            )}
            {!item.installable && (
              <span className="inline-flex items-center rounded-full border border-muted-foreground/20 bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                Preview only
              </span>
            )}
          </div>

          {/* Description */}
          <p
            className="line-clamp-2 text-xs leading-relaxed text-muted-foreground"
            title={item.description}
          >
            {item.description?.trim() || "No description provided."}
          </p>

          {/* Permission warnings (if any) */}
          {item.permission_warnings && item.permission_warnings.length > 0 && (
            <div className="mt-2">
              <div className="flex items-start gap-1.5 rounded-md border border-amber-500/20 bg-amber-500/5 px-2 py-1.5">
                <Shield className="mt-0.5 h-3 w-3 shrink-0 text-amber-600 dark:text-amber-400" />
                <p className="text-[10px] leading-relaxed text-amber-900/80 dark:text-amber-200/70">
                  {item.permission_warnings[0]}
                  {item.permission_warnings.length > 1 && (
                    <span className="ml-1 font-medium">
                      +{item.permission_warnings.length - 1} more
                    </span>
                  )}
                </p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ── Right: actions ───────────────────────────────────── */}
      <div className="flex items-center gap-1.5 flex-wrap sm:flex-nowrap shrink-0 sm:self-center">
        <button
          type="button"
          onClick={onOpenDetails}
          disabled={installing}
          className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium border border-border text-foreground hover:bg-muted transition-colors disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer"
        >
          <Eye className="h-3.5 w-3.5" />
          Details
        </button>

        {installState.isInstalled ? (
          <>
            <button
              type="button"
              onClick={() => onConfigure(item.id)}
              disabled={installing}
              className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium border border-border text-foreground hover:bg-muted transition-colors disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer"
            >
              <Plug className="h-3.5 w-3.5" />
              Configure
            </button>

            {hasVersionMismatch && (
              <button
                type="button"
                onClick={onInstall}
                disabled={!canInstall}
                className={[
                  "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
                  canInstall
                    ? "bg-primary text-primary-foreground hover:bg-primary/90 cursor-pointer"
                    : "cursor-not-allowed border bg-muted text-muted-foreground",
                ].join(" ")}
                title={`Update from v${installState.installedVersion} to v${item.version}`}
              >
                {installing ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Updating…
                  </>
                ) : (
                  <>
                    <Download className="h-3.5 w-3.5" />
                    Update
                  </>
                )}
              </button>
            )}

            <div className="w-px h-6 bg-border mx-0.5" />

            <button
              type="button"
              role="switch"
              aria-checked={installState.isEnabled}
              aria-label={
                installState.isEnabled
                  ? "Disable provider"
                  : "Enable provider"
              }
              onClick={() => onToggleProvider(item.id)}
              disabled={installing}
              className={[
                "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors",
                installState.isEnabled
                  ? "bg-emerald-500 hover:bg-emerald-600"
                  : "bg-muted-foreground/30 hover:bg-muted-foreground/50",
                installing
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
          </>
        ) : (
          <button
            type="button"
            onClick={onInstall}
            disabled={!canInstall}
            className={[
              "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
              canInstall
                ? "bg-primary text-primary-foreground hover:bg-primary/90 cursor-pointer"
                : "cursor-not-allowed border bg-muted text-muted-foreground",
            ].join(" ")}
            title={item.installability_reason}
          >
            {installing ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Installing…
              </>
            ) : item.installable ? (
              <>
                <Download className="h-3.5 w-3.5" />
                Install
              </>
            ) : (
              <>
                <Eye className="h-3.5 w-3.5" />
                Preview
              </>
            )}
          </button>
        )}
      </div>
    </article>
  );
}


// ---------------------------------------------------------------------------
// Empty states
// ---------------------------------------------------------------------------

function EmptyInstalledState({
  hasSearch,
  onOpenUpload,
}: {
  hasSearch: boolean;
  onOpenUpload: () => void;
}) {
  if (hasSearch) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed p-12 text-center">
        <XCircle className="h-8 w-8 text-muted-foreground/50" />
        <p className="text-sm font-medium text-foreground">
          No matching providers
        </p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Try adjusting your search or filter, or upload a new package.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed p-12 text-center">
      <Plug className="h-10 w-10 text-muted-foreground/40" />
      <div>
        <p className="text-sm font-medium text-foreground">
          No provider packages installed
        </p>
        <p className="mt-1 max-w-sm text-xs text-muted-foreground">
          Install an LLM provider from the catalog, or upload a
          provider package you trust.
        </p>
      </div>
      <button
        type="button"
        onClick={onOpenUpload}
        className="mt-2 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
      >
        <FileArchive className="h-3.5 w-3.5" />
        Upload Package
      </button>
    </div>
  );
}

function EmptyCatalogState({
  hasSearch,
  isLoading,
  error,
}: {
  hasSearch: boolean;
  isLoading: boolean;
  error: string | null;
}) {
  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed p-12 text-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground/60" />
        <p className="text-xs text-muted-foreground">Loading catalog…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-destructive/30 bg-destructive/5 p-12 text-center">
        <AlertCircle className="h-8 w-8 text-destructive" />
        <p className="text-sm font-medium text-destructive">
          Failed to load catalog
        </p>
        <p className="max-w-md text-xs text-muted-foreground">{error}</p>
      </div>
    );
  }

  if (hasSearch) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed p-12 text-center">
        <XCircle className="h-8 w-8 text-muted-foreground/50" />
        <p className="text-sm font-medium text-foreground">
          No matching catalog items
        </p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Try adjusting your search.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed p-12 text-center">
      <Sparkles className="h-8 w-8 text-muted-foreground/40" />
      <p className="text-sm font-medium text-foreground">
        Catalog is empty
      </p>
      <p className="max-w-sm text-xs text-muted-foreground">
        No installable provider packages are currently listed. You can
        still upload a package manually.
      </p>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Main section
// ---------------------------------------------------------------------------

export function ProvidersSection({
  providers,
  searchQuery,
  providerFilter = "all",
  catalogItems,
  isCatalogLoading,
  isRefreshingCatalog,
  catalogError,
  onLoadCatalog,
  onRefreshCatalog,
  onInstallFromCatalog,
  installingCatalogId,
  togglingProviderId,
  deletingProviderId,
  activatingProviderId,
  onToggleProvider,
  onDeleteProvider,
  onActivateVersion,
  onOpenDetails,
  onOpenConfigure,
  onManageCertificates,
  onOpenUpload,
}: ProvidersSectionProps) {
  const [activeTab, setActiveTab] = useState<TabId>("installed");

  /**
   * The catalog item whose details modal is currently open, or
   * ``null`` when closed. Kept separate from ``activeTab`` — the
   * modal is orthogonal to which list the user is browsing.
   */
  const [detailsItem, setDetailsItem] = useState<ProviderCatalogItem | null>(
    null,
  );

  /**
   * Map of installed packages keyed by provider id. Used by
   * ``computeInstallState`` to answer "is this catalog item
   * installed on this instance?" in O(1) per item.
   */
  const installedById = useMemo(() => {
    const map = new Map<string, ProviderPackageEntry>();
    for (const p of providers) map.set(p.id, p);
    return map;
  }, [providers]);

  /**
   * Map of provider id → catalog branding override.
   *
   * Built from the catalog items currently loaded. Used to tint the
   * installed package cards with the same brand color + icon that
   * the catalog cards use. When the catalog is unavailable (remote
   * fetch failed, or the package was uploaded manually), the map is
   * empty and the installed card falls back to its transport color
   * — a graceful degradation rather than a visual break.
   */
  const brandingByProviderId = useMemo(() => {
    const map = new Map<
      string,
      {
        brand_color?: string | null;
        icon?: string | null;
        icon_type?: "name" | "svg";
      }
    >();
    for (const c of catalogItems) {
      if (c.brand_color || c.icon) {
        map.set(c.id, {
          brand_color: c.brand_color ?? null,
          icon: c.icon ?? null,
          icon_type: c.icon_type,
        });
      }
    }
    return map;
  }, [catalogItems]);

  /**
   * Compute the derived install state for a catalog item by joining
   * against the installed registry. This is the single source of
   * truth for "is it installed? enabled? at what version?" used by
   * both the card badges and the details modal.
   */
  const computeInstallState = useCallback(
    (item: ProviderCatalogItem): ProviderCatalogInstallState => {
      const installed = installedById.get(item.id) ?? null;
      if (!installed) {
        return {
          isInstalled: false,
          isEnabled: false,
          installedVersion: null,
          installedVersions: [],
          installedPackage: null,
        };
      }
      return {
        isInstalled: true,
        isEnabled: installed.enabled,
        installedVersion: installed.active_version,
        installedVersions: [
          ...installed.installed_versions.map((v) => v.version),
        ].sort().reverse(),
        installedPackage: installed,
      };
    },
    [installedById],
  );

  // --------------------------------------------------------------
  // Lazy catalog load — fetch once when the section first renders.
  //
  // The catalog is only relevant here (no other section uses it),
  // so loading it globally in useStoreData would waste bandwidth
  // and inflate state on every page visit. Loading it here means
  // it appears the instant the user opens the Providers tab.
  //
  // `onLoadCatalog` is idempotent — subsequent mounts (e.g. after
  // navigating away and back) fetch again, but the backend serves
  // from an in-memory cache so the round-trip is negligible.
  // --------------------------------------------------------------
  useEffect(() => {
    void onLoadCatalog();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --------------------------------------------------------------
  // Filter + search over installed providers.
  // --------------------------------------------------------------
  const filteredProviders = useMemo(() => {
    let result = providers;

    if (providerFilter !== "all") {
      if (providerFilter === "enabled") {
        result = result.filter((p) => p.enabled);
      } else if (providerFilter === "disabled") {
        result = result.filter((p) => !p.enabled);
      } else {
        // native | compatible | custom
        result = result.filter((p) => p.transport === providerFilter);
      }
    }

    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim();
      result = result.filter((p) => {
        return (
          p.id.toLowerCase().includes(q) ||
          (p.name || "").toLowerCase().includes(q) ||
          (p.display_name || "").toLowerCase().includes(q) ||
          (p.description || "").toLowerCase().includes(q) ||
          (p.tags || []).some((t) => t.toLowerCase().includes(q))
        );
      });
    }

    return result;
  }, [providers, providerFilter, searchQuery]);

  // --------------------------------------------------------------
  // Filter + search over catalog items.
  //
  // No transport filter applies here (the catalog is not filtered
  // by enabled state — that concept only exists for installed
  // packages). Search works over the same fields.
  // --------------------------------------------------------------
  const filteredCatalog = useMemo(() => {
    if (!searchQuery.trim()) return catalogItems;
    const q = searchQuery.toLowerCase().trim();
    return catalogItems.filter((c) => {
      return (
        c.id.toLowerCase().includes(q) ||
        (c.name || "").toLowerCase().includes(q) ||
        (c.display_name || "").toLowerCase().includes(q) ||
        (c.description || "").toLowerCase().includes(q) ||
        (c.tags || []).some((t) => t.toLowerCase().includes(q))
      );
    });
  }, [catalogItems, searchQuery]);

  // --------------------------------------------------------------
  // Tab counts for the navigation buttons.
  // --------------------------------------------------------------
  const installedCount = providers.length;
  const catalogCount = catalogItems.length;

  return (
    <div className="space-y-4">
      {/* ── Tab navigation ──────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b">
        <div className="flex items-center gap-1">
          <TabButton
            active={activeTab === "installed"}
            onClick={() => setActiveTab("installed")}
            label="Installed"
            count={installedCount}
          />
          <TabButton
            active={activeTab === "catalog"}
            onClick={() => setActiveTab("catalog")}
            label="Catalog"
            count={catalogCount}
            loading={isCatalogLoading && catalogCount === 0}
          />
        </div>

        {/* Refresh button for the catalog tab. Rendered only when
            the user is on the catalog tab — otherwise it would
            just add visual noise for installed-package browsing. */}
        {activeTab === "catalog" && (
          <button
            type="button"
            onClick={() => void onRefreshCatalog()}
            disabled={isRefreshingCatalog || isCatalogLoading}
            className={[
              "mb-1 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
              isRefreshingCatalog || isCatalogLoading
                ? "cursor-wait opacity-60"
                : "hover:bg-muted hover:border-primary/40",
            ].join(" ")}
          >
            <RefreshCw
              className={[
                "h-3 w-3",
                isRefreshingCatalog ? "animate-spin" : "",
              ].join(" ")}
            />
            {isRefreshingCatalog ? "Refreshing…" : "Refresh"}
          </button>
        )}
      </div>

      {/* ── Installed tab ────────────────────────────────────────── */}
      {activeTab === "installed" && (
        <>
          {filteredProviders.length === 0 ? (
            <EmptyInstalledState
              hasSearch={searchQuery.trim().length > 0 || providerFilter !== "all"}
              onOpenUpload={onOpenUpload}
            />
          ) : (
            <div className="flex flex-col gap-3">
              {filteredProviders.map((provider) => (
                <ProviderPackageCard
                  key={provider.id}
                  provider={provider}
                  brandingOverride={brandingByProviderId.get(provider.id)}
                  toggling={togglingProviderId === provider.id}
                  deleting={deletingProviderId === provider.id}
                  activating={activatingProviderId === provider.id}
                  onToggle={onToggleProvider}
                  onDelete={onDeleteProvider}
                  onDetails={onOpenDetails}
                  onConfigure={onOpenConfigure}
                  onManageCertificates={onManageCertificates}
                  onActivateVersion={onActivateVersion}
                />
              ))}
            </div>
          )}
        </>
      )}

      {/* ── Catalog tab ──────────────────────────────────────────── */}
      {activeTab === "catalog" && (
        <>
          {filteredCatalog.length === 0 ? (
            <EmptyCatalogState
              hasSearch={searchQuery.trim().length > 0}
              isLoading={isCatalogLoading}
              error={catalogError}
            />
          ) : (
            <div className="flex flex-col gap-3">
              {filteredCatalog.map((item) => (
                <CatalogItemCard
                  key={item.id}
                  item={item}
                  installState={computeInstallState(item)}
                  installing={installingCatalogId === item.id}
                  onInstall={() => void onInstallFromCatalog(item.id)}
                  onOpenDetails={() => setDetailsItem(item)}
                  onConfigure={onOpenConfigure}
                  onToggleProvider={onToggleProvider}
                />
              ))}
            </div>
          )}
        </>
      )}

      {/* ── Catalog item details modal ────────────────────────────── */}
      {detailsItem && (
        <CatalogItemDetailsModal
          open={true}
          item={detailsItem}
          installState={computeInstallState(detailsItem)}
          isInstalling={installingCatalogId === detailsItem.id}
          installError={null}
          onInstall={() => onInstallFromCatalog(detailsItem.id)}
          onReinstall={() => onInstallFromCatalog(detailsItem.id)}
          onToggleProvider={onToggleProvider}
          onConfigure={onOpenConfigure}
          onClose={() => setDetailsItem(null)}
        />
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Tab button
// ---------------------------------------------------------------------------

function TabButton({
  active,
  onClick,
  label,
  count,
  loading,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count?: number;
  loading?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "relative -mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium transition-colors",
        active
          ? "border-primary text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground",
      ].join(" ")}
    >
      {label}
      {loading ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : typeof count === "number" ? (
        <span
          className={[
            "inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
            active
              ? "bg-primary/10 text-primary"
              : "bg-muted text-muted-foreground",
          ].join(" ")}
        >
          {count}
        </span>
      ) : null}
    </button>
  );
}


// ---------------------------------------------------------------------------
// Header config factory (for StoreHeader in page.tsx)
// ---------------------------------------------------------------------------

/**
 * Build the section header configuration for the Providers tab.
 *
 * Exposed as a standalone factory (rather than a static object) so
 * the "Upload Package" button's ``onClick`` closure is fresh on each
 * render — otherwise a stale reference could point to a closed-over
 * modal state from a previous render.
 *
 * The ``Refresh`` action is deliberately NOT included here — catalog
 * refresh is scoped to the catalog tab and lives inside the tab
 * navigation. Adding it to the page-level header would mean the
 * button exists but does nothing when the user is on the Installed
 * tab, which is confusing.
 */
export function getProvidersHeaderConfig(args: {
  onUploadProvider: () => void;
}): SectionHeaderConfig {
  return {
    title: "AI Providers",
    subtitle:
      "Install LLM provider packages — built-in, compatible, or custom.",
    actions: [
      {
        id: "upload-provider",
        label: "Upload Package",
        icon: FileArchive,
        variant: "primary",
        onClick: args.onUploadProvider,
        iconClassName: "h-4 w-4",
      },
    ],
  };
}


export default ProvidersSection;
