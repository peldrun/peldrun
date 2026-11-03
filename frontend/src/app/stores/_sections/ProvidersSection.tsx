// frontend/src/app/stores/_sections/ProvidersSection.tsx
"use client";

import React, { useEffect, useMemo, useState } from "react";
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
  ProviderCatalogItem,
  ProviderFilter,
  ProviderPackageEntry,
  ProviderTransport,
} from "../_types/store";
import type { SectionHeaderConfig } from "../_config/sections";
import { ProviderPackageCard } from "../_components/ProviderPackageCard";


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

interface CatalogItemCardProps {
  item: ProviderCatalogItem;
  installing: boolean;
  onInstall: () => void;
}

function CatalogItemCard({
  item,
  installing,
  onInstall,
}: CatalogItemCardProps) {
  const displayName = item.display_name || item.name || item.id;
  const transport =
    (item.transport as ProviderTransport) ?? "native";
  const badgeClass =
    TRANSPORT_BADGE_CLASSES[transport] ?? TRANSPORT_BADGE_CLASSES.native;

  const canInstall = item.installable && !installing;

  return (
    <article className="group relative flex h-full flex-col rounded-xl border bg-card transition-all hover:border-primary/40 hover:shadow-sm">
      {/* Header */}
      <div className="flex items-start gap-3 p-4">
        <div
          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${badgeClass}`}
          aria-hidden="true"
        >
          <Plug className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <h3
            className="truncate text-sm font-semibold text-foreground"
            title={displayName}
          >
            {displayName}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              v{item.version}
            </span>
            <span
              className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${badgeClass}`}
            >
              {TRANSPORT_LABELS[transport] ?? transport}
            </span>
            {!item.installable && (
              <span className="inline-flex items-center rounded-full border border-muted-foreground/20 bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                Preview only
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Description */}
      <p
        className="line-clamp-2 px-4 text-xs leading-relaxed text-muted-foreground"
        title={item.description}
      >
        {item.description?.trim() || "No description provided."}
      </p>

      {/* Permission warnings (if any) */}
      {item.permission_warnings && item.permission_warnings.length > 0 && (
        <div className="px-4 pt-3">
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

      {/* Footer */}
      <div className="mt-auto flex items-center justify-between gap-2 border-t px-3 py-2.5">
        <p
          className="truncate text-[11px] text-muted-foreground"
          title={item.installability_reason}
        >
          {item.installable ? "Ready to install" : item.installability_reason}
        </p>
        <button
          type="button"
          onClick={onInstall}
          disabled={!canInstall}
          className={[
            "inline-flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
            canInstall
              ? "bg-primary text-primary-foreground hover:bg-primary/90"
              : "cursor-not-allowed border bg-muted text-muted-foreground",
          ].join(" ")}
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
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {filteredProviders.map((provider) => (
                <ProviderPackageCard
                  key={provider.id}
                  provider={provider}
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
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {filteredCatalog.map((item) => (
                <CatalogItemCard
                  key={item.id}
                  item={item}
                  installing={installingCatalogId === item.id}
                  onInstall={() => void onInstallFromCatalog(item.id)}
                />
              ))}
            </div>
          )}
        </>
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
