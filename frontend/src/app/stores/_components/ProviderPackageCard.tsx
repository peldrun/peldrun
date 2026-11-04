// frontend/src/app/stores/_components/ProviderPackageCard.tsx
"use client";

import React from "react";
import {
  CheckCircle2,
  Eye,
  Layers,
  Plug,
  Radio,
  Settings,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Trash2,
  Wrench,
} from "lucide-react";
import type {
  CertificatesStatus,
  ProviderPackageEntry,
  ProviderTransport,
} from "../_types/store";
import { Icon } from "@/components/Icon/Icon";

/**
 * One card in the Providers section grid.
 *
 * Displays a single installed Provider Package with:
 *
 *   • A transport-colored icon (native / compatible / custom).
 *   • Name, display name, and active version.
 *   • A one-or-two-line description.
 *   • Feature chips (Tools, Vision, Reasoning, Streaming).
 *   • Source badge (uploaded vs installed from catalog).
 *   • An enable / disable toggle.
 *   • Footer actions: Details, version selector (when > 1 version
 *     is installed), Delete.
 *
 * The card is DISABLED-VISUAL when the provider is disabled — the
 * whole card fades to 60% opacity. The toggle remains fully
 * interactive so the user can re-enable it without confusion.
 *
 * The card has NO internal state. Every interaction is delegated to
 * the parent (which owns `useProviderActions`). This keeps the card
 * trivially testable and prevents state-drift bugs.
 *
 * Props contract
 * --------------
 *   - `provider`: the entry to render.
 *   - `toggling` / `deleting` / `activating`: per-action loading
 *     flags for THIS card's ID. The parent passes `null` for cards
 *     that are not currently the target of an action, so each card
 *     can independently show the correct spinner.
 *   - `onToggle` / `onDelete` / `onDetails` / `onActivateVersion`:
 *     async handlers. The card does NOT await them — the parent
 *     manages loading state and error toasts.
 */
/**
 * Optional branding overrides from the catalog item, applied when
 * the installed package was originally installed from the catalog.
 *
 * The installed ``ProviderPackageEntry`` does not carry
 * ``brand_color`` / ``icon`` / ``icon_type`` — those live on the
 * catalog item only. When the caller supplies them here, the card
 * renders the branded glyph and the tinted icon container instead
 * of the default transport color. Falls back to the transport
 * color when absent (e.g. catalog is unreachable, or the package
 * was uploaded manually).
 */
export interface ProviderBrandingOverride {
  brand_color?: string | null;
  icon?: string | null;
  icon_type?: "name" | "svg";
}

export interface ProviderPackageCardProps {
  provider: ProviderPackageEntry;

  /**
   * Branding from the matching catalog item, when one exists.
   * See ``ProviderBrandingOverride`` for the shape and semantics.
   * Pass ``null`` or omit it when the package was not installed
   * from the catalog.
   */
  brandingOverride?: ProviderBrandingOverride | null;

  /** True when the toggle is currently in flight for THIS provider. */
  toggling: boolean;
  /** True when a delete is currently in flight for THIS provider. */
  deleting: boolean;
  /** True when a version activation is in flight for THIS provider. */
  activating: boolean;
  /** Called when the enable/disable toggle is clicked. */
  onToggle: (providerId: string) => void;
  /** Called when Delete is confirmed by the caller. */
  onDelete: (provider: ProviderPackageEntry) => void;
  /** Called when the Details action is clicked. */
  onDetails: (providerId: string) => void;
  /** Called when the Configure action is clicked. */
  onConfigure: (providerId: string) => void;
  /**
   * Called when the Certificates action is clicked.
   *
   * Opens the Certificate Manager modal, which shows the TLS
   * certificates the package declares (root CA bundles, mTLS
   * client certificates) and offers a one-click install flow.
   *
   * Only rendered when ``provider.has_certificates === true``.
   */
  onManageCertificates: (providerId: string) => void;
  /** Called when a specific installed version is selected. */
  onActivateVersion: (
    provider: ProviderPackageEntry,
    version: string,
  ) => void;
}


// ---------------------------------------------------------------------------
// Presentation helpers
// ---------------------------------------------------------------------------

/**
 * Tailwind classes for the icon container, keyed by transport.
 * Colored background + tinted icon gives the card a distinct visual
 * identity without requiring an image asset.
 */
const TRANSPORT_ICON_CLASSES: Record<ProviderTransport, string> = {
  native:
    "bg-blue-500/10 text-blue-600 dark:text-blue-400 ring-1 ring-blue-500/20",
  compatible:
    "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 ring-1 ring-emerald-500/20",
  custom:
    "bg-purple-500/10 text-purple-600 dark:text-purple-400 ring-1 ring-purple-500/20",
};

/**
 * Tailwind classes for the transport badge pill. Kept separate from
 * the icon so both can be tuned independently if needed.
 */
const TRANSPORT_BADGE_CLASSES: Record<ProviderTransport, string> = {
  native:
    "bg-blue-500/10 text-blue-700 dark:text-blue-300 border-blue-500/20",
  compatible:
    "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/20",
  custom:
    "bg-purple-500/10 text-purple-700 dark:text-purple-300 border-purple-500/20",
};

/**
 * Human-readable transport labels. `native` is not called "native"
 * in the UI because that word means nothing to a user — "Built-in"
 * is clearer. The other two are already user-facing concepts.
 */
const TRANSPORT_LABELS: Record<ProviderTransport, string> = {
  native: "Built-in",
  compatible: "Compatible",
  custom: "Custom",
};

/**
 * Render a transport badge with the correct label and colors.
 */
function TransportBadge({ transport }: { transport: ProviderTransport }) {
  const classes =
    TRANSPORT_BADGE_CLASSES[transport] ?? TRANSPORT_BADGE_CLASSES.native;
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${classes}`}
    >
      {TRANSPORT_LABELS[transport] ?? transport}
    </span>
  );
}

/**
 * Render the feature chips row. Shows up to 4 chips; anything beyond
 * that is collapsed into a "+N" indicator. Chips are icon-based to
 * minimize width on narrow viewports.
 *
 * Order matters: tools → vision → reasoning → streaming, from most
 * to least commonly relevant for a user making an install decision.
 */
function FeatureChips({ features }: { features: Record<string, boolean> }) {
  interface Chip {
    key: string;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
  }

  const allChips: Chip[] = [
    { key: "tool_calling", label: "Tools", icon: Wrench },
    { key: "vision", label: "Vision", icon: Eye },
    { key: "reasoning", label: "Reasoning", icon: Sparkles },
    { key: "native_streaming", label: "Streaming", icon: Radio },
  ];

  const enabled = allChips.filter((c) => features[c.key] === true);
  if (enabled.length === 0) return null;

  const shown = enabled.slice(0, 4);
  const hidden = enabled.length - shown.length;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {shown.map((chip) => {
        const Icon = chip.icon;
        return (
          <span
            key={chip.key}
            className="inline-flex items-center gap-1 rounded-md bg-muted/60 px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground"
          >
            <Icon className="h-3 w-3" />
            {chip.label}
          </span>
        );
      })}
      {hidden > 0 && (
        <span className="inline-flex items-center rounded-md bg-muted/60 px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground">
          +{hidden}
        </span>
      )}
    </div>
  );
}

/**
 * Compact certificate status badge.
 *
 * Rendered only when the package declares certificates. The color
 * encodes the urgency:
 *
 *   - purple  → installed (all required certificates are present)
 *   - amber   → missing (at least one required certificate is missing)
 *   - gray    → unknown (manifest was readable but status was not
 *                computed — rare)
 *
 * A package that declares no certificates renders nothing here.
 */
function CertificateBadge({
  hasCertificates,
  status,
}: {
  hasCertificates: boolean | null | undefined;
  status: CertificatesStatus | undefined;
}) {
  // No badge for packages that do not need certificates.
  if (!hasCertificates) return null;

  if (status === "all_installed") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full border border-purple-500/20 bg-purple-500/10 px-2 py-0.5 text-[11px] font-medium text-purple-700 dark:text-purple-300"
        title="All required TLS certificates are installed"
      >
        <ShieldCheck className="h-2.5 w-2.5" />
        Certificates
      </span>
    );
  }

  if (status === "some_missing") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300"
        title="Click Certificates to install the required TLS certificates"
      >
        <ShieldAlert className="h-2.5 w-2.5" />
        Certificates missing
      </span>
    );
  }

  // Unknown or unset — render a neutral badge so the user knows the
  // package declares certificates and can click through to inspect.
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-muted-foreground/20 bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
      title="This provider declares TLS certificates; click Certificates to inspect"
    >
      <Shield className="h-2.5 w-2.5" />
      Certificates
    </span>
  );
}

/**
 * Sanitize inline SVG markup from a catalog-supplied icon.
 * Mirrors the pattern used by ``SkillIcon`` and
 * ``CatalogItemDetailsModal``.
 */
function sanitizeBrandSvg(raw: string): string | null {
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
 * Render the branded icon glyph for an installed package.
 *
 * Priority
 * --------
 *   1. ``icon_type === "name"`` — the shared ``Icon`` component,
 *      tinted by ``brand_color`` via CSS ``currentColor``.
 *   2. ``icon_type === "svg"`` — inline sanitized SVG.
 *   3. Fallback — the generic ``Plug`` glyph (default transport
 *      aesthetic).
 */
function BrandedIcon({
  icon,
  iconType,
  brandColor,
  size = 20,
}: {
  icon?: string | null;
  iconType?: "name" | "svg";
  brandColor?: string | null;
  size?: number;
}) {
  const brand = brandColor || "currentColor";
  const value = (icon || "").trim();

  if (value && (iconType || "name") === "name") {
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
        <Icon name={value} size={size} />
      </span>
    );
  }

  if (value && iconType === "svg") {
    const safe = sanitizeBrandSvg(value);
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

  return <Plug className="h-5 w-5" />;
}

/**
 * Small toggle switch. Uses `role="switch"` for accessibility and a
 * per-instance disabled state driven by ``toggling``.
 */
function EnabledToggle({
  enabled,
  busy,
  onToggle,
}: {
  enabled: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label={enabled ? "Disable provider" : "Enable provider"}
      onClick={onToggle}
      disabled={busy}
      className={[
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors",
        enabled
          ? "bg-emerald-500 hover:bg-emerald-600"
          : "bg-muted-foreground/30 hover:bg-muted-foreground/50",
        busy ? "cursor-wait opacity-60" : "cursor-pointer",
      ].join(" ")}
    >
      <span
        className={[
          "inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform",
          enabled ? "translate-x-4" : "translate-x-0.5",
        ].join(" ")}
      />
    </button>
  );
}


// ---------------------------------------------------------------------------
// Main card
// ---------------------------------------------------------------------------

export function ProviderPackageCard({
  provider,
  brandingOverride,
  toggling,
  deleting,
  activating,
  onToggle,
  onDelete,
  onDetails,
  onConfigure,
  onManageCertificates,
  onActivateVersion,
}: ProviderPackageCardProps) {
  const displayName =
    provider.display_name || provider.name || provider.id;
  const transport: ProviderTransport =
    (provider.transport as ProviderTransport) ?? "native";
  const iconClass =
    TRANSPORT_ICON_CLASSES[transport] ?? TRANSPORT_ICON_CLASSES.native;

  // When the package was installed from the catalog, ``brandingOverride``
  // carries the catalog's ``brand_color`` / ``icon`` / ``icon_type``.
  // We use them to tint and icon-render the installed card exactly the
  // way the catalog card does — so the same provider looks identical
  // in both tabs. Falls through to the transport-colored icon
  // container when the override is absent (manual upload, or the
  // catalog is unreachable this session).
  const brandColor = brandingOverride?.brand_color || null;
  const brandIcon = brandingOverride?.icon || null;
  const brandIconType = brandingOverride?.icon_type || "name";
  const hasBranding = Boolean(brandColor || brandIcon);

  const hasMultipleVersions = provider.installed_versions.length > 1;
  const isBusy = toggling || deleting || activating;

  // Build the description preview. Falls back to a short generic
  // string when the manifest declared no description — showing
  // "No description provided." is friendlier than leaving the
  // space blank, which makes the row look broken.
  const description =
    (provider.description && provider.description.trim()) ||
    "No description provided.";

  return (
    <article
      className={[
        // Horizontal list-row layout — matches the visual pattern
        // used by ExtensionCard and SkillCard in this same store.
        // On narrow viewports the actions wrap below the content.
        "group relative flex flex-col sm:flex-row sm:items-center gap-4",
        "p-4 rounded-sm border transition-all hover:shadow-sm",
        provider.enabled
          ? "border-card border-l-[5px] bg-card "
          : "border-muted  bg-muted/20 opacity-70",
        deleting ? "pointer-events-none opacity-40" : "",
      ].join(" ")}

      style={
            hasBranding
              ? {
                  borderLeftColor: brandColor
                    ? `${brandColor}`
                    : undefined,
                }
              : undefined
          }

    >
      {/* ── Left: icon + content ─────────────────────────────── */}
      <div className="flex  gap-3 sm:gap-4 flex-1 min-w-0 items-center">
        {/* Icon container: uses the catalog's brand color when the
            package was installed from the catalog, otherwise falls
            back to the transport-derived classes (blue / green /
            purple) that distinguish native / compatible / custom.
            The two paths are mutually exclusive so there is no
            visual confusion when both are available. */}
        <div
          className={
            hasBranding
              ? "shrink-0 flex h-full w-20 items-center justify-center"
              : `shrink-0 flex h-full w-20 items-center justify-center ${iconClass}`
          }
          aria-hidden="true"
        >
          {hasBranding ? (
            <BrandedIcon
              icon={brandIcon}
              iconType={brandIconType}
              brandColor={brandColor}
              size={70}
            />
          ) : (
            <Plug className="h-20 w-20" />
          )}
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
            <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 text-[11px] font-mono text-muted-foreground">
              v{provider.active_version}
            </span>
            <TransportBadge transport={transport} />
            {hasMultipleVersions && (
              <span
                className="inline-flex items-center gap-1 rounded-md bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground"
                title={`${provider.installed_versions.length} versions installed`}
              >
                <Layers className="h-3 w-3" />
                {provider.installed_versions.length} versions
              </span>
            )}
            {provider.source === "catalog" && (
              <span className="inline-flex items-center rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                From Catalog
              </span>
            )}
            {provider.has_active_connection ? (
              <span
                className="inline-flex items-center gap-1 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300"
                title={
                  provider.active_connection_name
                    ? `Active connection: ${provider.active_connection_name}`
                    : "Active connection set"
                }
              >
                <CheckCircle2 className="h-2.5 w-2.5" />
                <span className="max-w-[120px] truncate">
                  {provider.active_connection_name || "Connected"}
                </span>
              </span>
            ) : (
              <span
                className="inline-flex items-center gap-1 rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300"
                title="Click Configure to add a connection for this provider"
              >
                Not configured
              </span>
            )}
            <CertificateBadge
              hasCertificates={provider.has_certificates}
              status={provider.certificates_status}
            />
          </div>

          {/* Description */}
          <p
            className="line-clamp-2 text-xs leading-relaxed text-muted-foreground"
            title={description}
          >
            {description}
          </p>

          {/* Feature chips */}
          {provider.features && Object.keys(provider.features).length > 0 && (
            <div className="mt-2">
              <FeatureChips features={provider.features} />
            </div>
          )}
        </div>
      </div>

      {/* ── Right: actions ───────────────────────────────────── */}
      <div className="flex items-center gap-1.5 flex-wrap sm:flex-nowrap shrink-0 sm:self-center">
        {hasMultipleVersions && (
          <select
            id={`activate-${provider.id}`}
            value={provider.active_version}
            disabled={isBusy}
            onChange={(e) => {
              const next = e.target.value;
              if (next !== provider.active_version) {
                onActivateVersion(provider, next);
              }
            }}
            className={[
              "rounded-md border bg-background px-1.5 py-1 text-[11px] font-mono",
              "focus:outline-none focus:ring-1 focus:ring-primary/40",
              activating ? "cursor-wait opacity-60" : "cursor-pointer",
            ].join(" ")}
            title="Activate version"
          >
            {provider.installed_versions.map((record) => (
              <option key={record.version} value={record.version}>
                v{record.version}
              </option>
            ))}
          </select>
        )}

        <button
          type="button"
          onClick={() => onConfigure(provider.id)}
          disabled={isBusy}
          className={[
            "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
            provider.has_active_connection
              ? "text-foreground border border-border hover:bg-muted"
              : "bg-primary text-primary-foreground hover:bg-primary/90",
            isBusy ? "cursor-not-allowed opacity-50" : "cursor-pointer",
          ].join(" ")}
          title={
            provider.has_active_connection
              ? "Manage connections for this provider"
              : "Add a connection to start using this provider"
          }
        >
          <Settings className="h-3.5 w-3.5" />
          Configure
        </button>

        {provider.has_certificates && (
          <button
            type="button"
            onClick={() => onManageCertificates(provider.id)}
            disabled={isBusy}
            className={[
              "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors border",
              provider.certificates_status === "some_missing"
                ? "border-amber-500/30 text-amber-700 dark:text-amber-300 hover:bg-amber-500/10"
                : "border-border text-foreground hover:bg-muted",
              isBusy ? "cursor-not-allowed opacity-50" : "cursor-pointer",
            ].join(" ")}
            title={
              provider.certificates_status === "some_missing"
                ? "Install the required TLS certificates"
                : "Manage TLS certificates for this provider"
            }
          >
            <Shield className="h-3.5 w-3.5" />
            Certificates
          </button>
        )}

        <button
          type="button"
          onClick={() => onDetails(provider.id)}
          disabled={isBusy}
          className={[
            "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors border border-border",
            "text-foreground hover:bg-muted",
            isBusy ? "cursor-not-allowed opacity-50" : "cursor-pointer",
          ].join(" ")}
        >
          <CheckCircle2 className="h-3.5 w-3.5" />
          Details
        </button>

        <button
          type="button"
          onClick={() => onDelete(provider)}
          disabled={isBusy}
          aria-label={`Delete ${displayName}`}
          className={[
            "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
            "text-destructive hover:bg-destructive/10",
            isBusy ? "cursor-not-allowed opacity-50" : "cursor-pointer",
          ].join(" ")}
        >
          <Trash2 className="h-3.5 w-3.5" />
          Delete
        </button>

        <div className="w-px h-6 bg-border mx-0.5" />

        <EnabledToggle
          enabled={provider.enabled}
          busy={toggling}
          onToggle={() => onToggle(provider.id)}
        />
      </div>
    </article>
  );
}

export default ProviderPackageCard;
