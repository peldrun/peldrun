// frontend/src/components/Icon/ProviderBadgeIcon.tsx
"use client";

import React from "react";
import { Icon } from "./Icon";

/* -------------------------------------------------------------------------- */
/* SVG sanitizer                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Strip potentially dangerous vectors from inline SVG markup before
 * rendering it via `dangerouslySetInnerHTML`.
 *
 * Mirrors the pattern already used by:
 *   - ProviderPackageCard.tsx::sanitizeBrandSvg
 *   - ProvidersSection.tsx::sanitizeSvgInline
 *
 * Unifying the sanitizer here means future security fixes touch one
 * file instead of N divergent copies.
 *
 * Stripped:
 *   - <script> blocks
 *   - <foreignObject> blocks (can embed arbitrary HTML)
 *   - on*="..." event-handler attributes
 *   - javascript: and data: URLs on href/xlink:href/src
 *   - @import url(...) directives inside <style>
 *
 * Returns null when the input does not contain an <svg marker, so the
 * caller can fall through to icon-name rendering.
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

/* -------------------------------------------------------------------------- */
/* Public helpers                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Returns true when the given value is inline SVG markup rather than
 * a bare icon name.
 *
 * Callers use this to decide whether a textual badge pill should be
 * rendered. An SVG badge has no meaningful text representation —
 * showing the raw `<svg>...</svg>` string in a pill would be a bug.
 */
export function isSvgBadge(badge: string | null | undefined): boolean {
  if (!badge) return false;
  return badge.trim().includes("<svg");
}

/* -------------------------------------------------------------------------- */
/* Props                                                                      */
/* -------------------------------------------------------------------------- */

export interface ProviderBadgeIconProps {
  /**
   * The raw `badge` value sourced from `models_vault.json`.
   *
   * Accepts three shapes:
   *   1. Inline SVG markup   → rendered via sanitized `dangerouslySetInnerHTML`.
   *   2. Icon name            → rendered via `<Icon name={badge} />`.
   *   3. Empty / undefined    → falls back to `fallback`.
   */
  badge?: string | null;

  /**
   * Icon base name used when `badge` is empty or SVG sanitization
   * failed. The caller should pass `resolveProviderIcon(id || name)`.
   */
  fallback: string;

  /** Pixel size (renders as a `size × size` box). */
  size: number;

  /** Optional extra class names applied to the wrapper span. */
  className?: string;

  /** Optional accessible title (used as tooltip and aria-label). */
  title?: string;
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Unified provider icon renderer.
 *
 * This is the single source of truth for "how do we draw a provider
 * icon" across the settings surface. It replaces three divergent
 * implementations:
 *
 *   - CloudProviderCard.tsx   — `<Icon name={resolveProviderIcon(...)} />`
 *   - PackageCard.tsx         — `<Icon name={pkg.iconName} />`
 *   - ModelBudgetsTab.tsx     — `resolveLucideIcon()` → string → Icon
 *
 * After migration, every provider surface accepts the same `badge`
 * contract: **either an icon name or inline SVG**.
 *
 * Layout invariant
 * ----------------
 * The wrapper span carries an explicit `width`/`height` and forces
 * any child `<svg>` (from either the SVG-badge path or the `<Icon>`
 * component) to fill that box via the Tailwind arbitrary variant
 * `[&>svg]:w-full [&>svg]:h-full`. This prevents catalog-supplied
 * SVGs — which commonly carry their own `width`/`height` attributes
 * — from overflowing or clipping inside fixed-size cards.
 *
 * The wrapper also sets `color: currentColor`, so callers tint the
 * badge by placing a `text-*` class on any ancestor.
 */
export function ProviderBadgeIcon({
  badge,
  fallback,
  size,
  className,
  title,
}: ProviderBadgeIconProps) {
  const raw = (badge || "").trim();
  const wrapperClass = [
    "inline-flex items-center justify-center [&>svg]:w-full [&>svg]:h-full",
    className || "",
  ]
    .filter(Boolean)
    .join(" ");

  const wrapperStyle: React.CSSProperties = {
    width: size,
    height: size,
    color: "currentColor",
  };

  // ---- Case 1: inline SVG badge -----------------------------------
  if (raw && isSvgBadge(raw)) {
    const safe = sanitizeSvg(raw);
    if (safe) {
      return (
        <span
          className={wrapperClass}
          style={wrapperStyle}
          title={title}
          aria-hidden={title ? undefined : "true"}
          dangerouslySetInnerHTML={{ __html: safe }}
        />
      );
    }
    // Sanitization failed → fall through to icon-name rendering.
  }

  // ---- Case 2: icon name badge ------------------------------------
  if (raw) {
    return (
      <span className={wrapperClass} style={wrapperStyle}>
        <Icon name={raw} size={size} title={title} />
      </span>
    );
  }

  // ---- Case 3: fallback -------------------------------------------
  return (
    <span className={wrapperClass} style={wrapperStyle}>
      <Icon name={fallback} size={size} title={title} />
    </span>
  );
}

export default ProviderBadgeIcon;
