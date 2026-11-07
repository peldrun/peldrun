// frontend/src/app/stores/_components/CatalogSkillCard.tsx
"use client";

import React from "react";
import {
  AlertCircle,
  CheckCircle2,
  Download,
  Eye,
  Loader2,
  ShieldCheck,
  Sparkles,
  Tag,
} from "lucide-react";
import type {
  SkillCatalogInstallState,
  SkillCatalogItem,
} from "../_types/store";

// IMPORTANT: The engineering plan requires rendering the catalog
// item's icon by name using the project's central ``Icon`` component
// at ``@/components/Icon/Icon``. If that module does not exist in
// this deployment, replace this import with a Lucide fallback and
// adjust ``CatalogSkillIcon`` below to return ``<Sparkles ...>`` for
// every case. The SVG path (icon_type === "svg") is unaffected.
// Fallback: the project does not ship a central Icon component.
// Render Sparkles for every name-based icon. If you later add
// `@/components/Icon/Icon`, replace this import and revert the
// `Icon` usage in `CatalogSkillIcon` below to use it.
const Icon = ({
  name: _name,
  size = 24,
}: {
  name: string;
  size?: number;
}) => <Sparkles size={size} />;


// ---------------------------------------------------------------------------
// Inline SVG sanitizer
// ---------------------------------------------------------------------------
//
// The backend sanitizes every SVG icon before it lands in the
// catalog (see ``omweb.skills.manifest.sanitize_svg``). We re-sanitize
// at render time as defense-in-depth: a compromised catalog source
// could smuggle raw SVG through a field the backend does not check,
// and ``dangerouslySetInnerHTML`` is an XSS vector if we trust it
// blindly.
//
// The sanitizer is deliberately simple -- it strips the specific
// attack vectors that make SVG dangerous in an HTML context:
//   * <script> blocks
//   * <foreignObject> subtrees (embed arbitrary HTML)
//   * event handler attributes (onclick, onload, ...)
//   * javascript: / data: URLs in href / xlink:href / src
//   * @import directives in CSS
function sanitizeSvg(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed.includes("<svg")) return null;
  return trimmed
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<foreignObject[\s\S]*?<\/foreignObject>/gi, "")
    .replace(/\son\w+\s*=\s*(".*?"|'.*?'|[^\s>]+)/gi, "")
    .replace(
      /(href|xlink:href|src)\s*=\s*("javascript:[^"]*"|'javascript:[^']*'|"data:[^"]*"|'data:[^']*')/gi,
      "",
    )
    .replace(/@import\s+url\s*\([^)]*\)/gi, "");
}


// ---------------------------------------------------------------------------
// Icon renderer
// ---------------------------------------------------------------------------

interface CatalogSkillIconProps {
  item: SkillCatalogItem;
  size?: number;
}

function CatalogSkillIcon({ item, size = 45 }: CatalogSkillIconProps) {
  const brand = item.brand_color || "currentColor";
  const iconValue = (item.icon || "").trim();
  const iconType = item.icon_type || "name";

  // Case 1: name-based icon -- use the project's Icon component.
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
        <Icon name={iconValue} size={size} />
      </span>
    );
  }

  // Case 2: inline SVG -- sanitize before rendering.
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

  // Case 3: no icon declared, or sanitization rejected the payload.
  return (
    <span style={{ color: brand }} aria-hidden="true">
      <Sparkles size={size} />
    </span>
  );
}


// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface CatalogSkillCardProps {
  /** The catalog item to render. */
  item: SkillCatalogItem;

  /** Derived install state, computed by the parent section. */
  installState: SkillCatalogInstallState;

  /** True while this specific item is installing. */
  installing: boolean;

  /** Called when the user clicks Install / Update. */
  onInstall: () => void;

  /** Called when the user clicks Details. */
  onOpenDetails: () => void;

  /**
   * Called when the user clicks "View Installed" (only shown when
   * the installed version matches the catalog version).
   * Optional -- if omitted, no such button is rendered.
   */
  onOpenInstalled?: () => void;
}


// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

export function CatalogSkillCard({
  item,
  installState,
  installing,
  onInstall,
  onOpenDetails,
  onOpenInstalled,
}: CatalogSkillCardProps) {
  const displayName = item.display_name || item.name || item.id;
  const isPreviewOnly = !item.installable;
  const hasVersionMismatch =
    installState.isInstalled &&
    installState.installedVersion !== null &&
    installState.installedVersion !== item.version;

  const brandColor = item.brand_color || null;

  return (
    <article
      className={[
        "group relative flex flex-col sm:flex-row sm:items-center gap-4",
        "p-4 rounded-sm border transition-all hover:shadow-md",
        "border-card border-l-[5px] bg-card",
      ].join(" ")}
      style={{ borderLeftColor: brandColor || undefined }}
    >
      <div className="flex items-center gap-3 sm:gap-4 flex-1 min-w-0">
        <div className="shrink-0 flex h-full w-[50px] items-center justify-center">
          <CatalogSkillIcon item={item} size={45} />
        </div>

        <div className="min-w-0 flex-1">
          {/* Title row: name + version + badges */}
          <div className="flex items-center gap-1.5 flex-wrap mb-1">
            <h3
              className="truncate text-sm font-semibold text-foreground font-mono"
              title={displayName}
            >
              {displayName}
            </h3>
            <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              v{item.version}
            </span>
            {item.has_sub_skills && (
              <span
                className="inline-flex items-center rounded-full bg-violet-500/10 text-violet-700 dark:text-violet-300 border border-violet-500/20 px-2 py-0.5 text-[11px] font-medium"
                title={`Router with ${item.sub_skills_count} sub-skill(s)`}
              >
                ROUTER · {item.sub_skills_count}
              </span>
            )}
            {item.trust === "verified" && (
              <ShieldCheck
                size={13}
                className="text-emerald-500"
                aria-label="Verified publisher"
              />
            )}
            {isPreviewOnly && (
              <span className="inline-flex items-center rounded-full border border-muted-foreground/20 bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                Preview only
              </span>
            )}
            {installState.isInstalled && (
              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
                <CheckCircle2 className="h-2.5 w-2.5" />
                Installed
              </span>
            )}
            {hasVersionMismatch && (
              <span className="inline-flex items-center gap-1 rounded-full border border-blue-500/20 bg-blue-500/10 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:text-blue-300">
                Update available
              </span>
            )}
            {item.stability === "deprecated" && (
              <span className="inline-flex items-center rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                Deprecated
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

          {/* Tags */}
          {item.tags.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {item.tags.slice(0, 6).map((tag, i) => (
                <span
                  key={`${tag}-${i}`}
                  className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground bg-muted border border-border"
                >
                  <Tag size={9} className="opacity-60" />
                  {tag}
                </span>
              ))}
            </div>
          )}

          {/* Install warnings preview -- first one only, plus count. */}
          {item.install_warnings.length > 0 && (
            <div className="mt-2 flex items-start gap-1.5 rounded-md border border-amber-500/20 bg-amber-500/5 px-2 py-1.5">
              <AlertCircle className="mt-0.5 h-3 w-3 shrink-0 text-amber-600 dark:text-amber-400" />
              <p className="text-[10px] leading-relaxed text-amber-900/80 dark:text-amber-200/70">
                {item.install_warnings[0]}
                {item.install_warnings.length > 1 && (
                  <span className="ml-1 font-medium">
                    +{item.install_warnings.length - 1} more
                  </span>
                )}
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Action buttons */}
      <div className="flex items-center gap-1.5 flex-wrap sm:flex-nowrap shrink-0 sm:self-center">
        <button
          type="button"
          onClick={onOpenDetails}
          disabled={installing}
          className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium border border-border text-foreground hover:bg-muted transition-colors disabled:opacity-50 cursor-pointer"
        >
          <Eye className="h-3.5 w-3.5" />
          Details
        </button>

        {installState.isInstalled ? (
          hasVersionMismatch ? (
            // Installed but catalog has a newer version -> Update.
            <button
              type="button"
              onClick={onInstall}
              disabled={!item.installable || installing}
              className={[
                "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
                item.installable && !installing
                  ? "bg-primary text-primary-foreground hover:bg-primary/90 cursor-pointer"
                  : "cursor-not-allowed border bg-muted text-muted-foreground",
              ].join(" ")}
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
          ) : onOpenInstalled ? (
            // Installed at the exact catalog version -> View Installed.
            <button
              type="button"
              onClick={onOpenInstalled}
              className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium border border-emerald-500/30 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/10 transition-colors cursor-pointer"
            >
              <CheckCircle2 className="h-3.5 w-3.5" />
              View Installed
            </button>
          ) : null
        ) : (
          // Not installed -> Install (or disabled Preview if not installable).
          <button
            type="button"
            onClick={onInstall}
            disabled={!item.installable || installing}
            className={[
              "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
              item.installable && !installing
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

export default CatalogSkillCard;
