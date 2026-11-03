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
  Sparkles,
  Trash2,
  Wrench,
} from "lucide-react";
import type {
  ProviderPackageEntry,
  ProviderTransport,
} from "../_types/store";

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
export interface ProviderPackageCardProps {
  provider: ProviderPackageEntry;
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
  toggling,
  deleting,
  activating,
  onToggle,
  onDelete,
  onDetails,
  onConfigure,
  onActivateVersion,
}: ProviderPackageCardProps) {
  const displayName =
    provider.display_name || provider.name || provider.id;
  const transport: ProviderTransport =
    (provider.transport as ProviderTransport) ?? "native";
  const iconClass =
    TRANSPORT_ICON_CLASSES[transport] ?? TRANSPORT_ICON_CLASSES.native;

  const hasMultipleVersions = provider.installed_versions.length > 1;
  const isBusy = toggling || deleting || activating;

  // Build the description preview. Falls back to a short generic
  // string when the manifest declared no description — showing
  // "No description provided." is friendlier than leaving the
  // space blank, which makes the card look broken.
  const description =
    (provider.description && provider.description.trim()) ||
    "No description provided.";

  return (
    <article
      className={[
        "group relative flex h-full flex-col rounded-xl border bg-card transition-all",
        "hover:border-primary/40 hover:shadow-sm",
        provider.enabled ? "" : "opacity-60",
        deleting ? "pointer-events-none opacity-40" : "",
      ].join(" ")}
    >
      {/* ── Header: icon + title + toggle ─────────────────────── */}
      <div className="flex items-start gap-3 p-4">
        <div
          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${iconClass}`}
          aria-hidden="true"
        >
          <Plug className="h-5 w-5" />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <h3
              className="truncate text-sm font-semibold text-foreground"
              title={displayName}
            >
              {displayName}
            </h3>
            <EnabledToggle
              enabled={provider.enabled}
              busy={toggling}
              onToggle={() => onToggle(provider.id)}
            />
          </div>

          <div className="mt-1 flex flex-wrap items-center gap-1.5">
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
            {/* Connection status — the single most important piece of
                information for a plugin provider. "Not configured" is
                rendered in amber because the user cannot use the
                provider until they add a connection. */}
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
          </div>
        </div>
      </div>

      {/* ── Description ───────────────────────────────────────── */}
      <p
        className="line-clamp-2 px-4 text-xs leading-relaxed text-muted-foreground"
        title={description}
      >
        {description}
      </p>

      {/* ── Feature chips ─────────────────────────────────────── */}
      {provider.features && Object.keys(provider.features).length > 0 && (
        <div className="px-4 pt-3">
          <FeatureChips features={provider.features} />
        </div>
      )}

      {/* ── Footer actions ────────────────────────────────────── */}
      <div className="mt-auto flex items-center justify-between gap-2 border-t px-3 py-2.5">
        <div className="flex items-center gap-1">
          {/* Configure is the primary action for a plugin provider —
              without a connection, the provider cannot be used. We
              place it FIRST so users find it immediately. When there
              is no active connection, we emphasize it with a primary
              style so it stands out. */}
          <button
            type="button"
            onClick={() => onConfigure(provider.id)}
            disabled={isBusy}
            className={[
              "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
              provider.has_active_connection
                ? "text-foreground hover:bg-muted"
                : "bg-primary text-primary-foreground hover:bg-primary/90",
              isBusy ? "cursor-not-allowed opacity-50" : "",
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

          <button
            type="button"
            onClick={() => onDetails(provider.id)}
            disabled={isBusy}
            className={[
              "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
              "text-foreground hover:bg-muted",
              isBusy ? "cursor-not-allowed opacity-50" : "",
            ].join(" ")}
          >
            <CheckCircle2 className="h-3.5 w-3.5" />
            Details
          </button>
        </div>

        <div className="flex items-center gap-1">
          {hasMultipleVersions && (
            <div className="flex items-center gap-1">
              <label
                htmlFor={`activate-${provider.id}`}
                className="text-[11px] text-muted-foreground"
              >
                Activate:
              </label>
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
                  "rounded-md border bg-background px-1.5 py-0.5 text-[11px] font-mono",
                  "focus:outline-none focus:ring-1 focus:ring-primary/40",
                  activating ? "cursor-wait opacity-60" : "",
                ].join(" ")}
              >
                {provider.installed_versions.map((record) => (
                  <option key={record.version} value={record.version}>
                    v{record.version}
                  </option>
                ))}
              </select>
            </div>
          )}

          <button
            type="button"
            onClick={() => onDelete(provider)}
            disabled={isBusy}
            aria-label={`Delete ${displayName}`}
            className={[
              "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
              "text-destructive hover:bg-destructive/10",
              isBusy ? "cursor-not-allowed opacity-50" : "",
            ].join(" ")}
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </button>
        </div>
      </div>
    </article>
  );
}

export default ProviderPackageCard;
