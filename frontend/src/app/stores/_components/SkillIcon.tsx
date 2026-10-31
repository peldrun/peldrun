// frontend/src/app/stores/_components/SkillIcon.tsx

"use client";

import React, { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import type { SkillSummary } from "../_types/store";
import { getApiUrl } from "../_hooks/useStoreData";

export interface SkillIconProps {
  skill: SkillSummary;
  size?: number;
  className?: string;
}

/**
 * Module-level SVG cache keyed by ``<id>@<version>``.
 *
 * Including the version in the key ensures a skill upgrade refreshes
 * its icon instead of showing the previous one from the cache. The
 * value is either the sanitized SVG text or ``null`` when the fetch
 * failed (so we skip the network round-trip on the next render and
 * fall back to Lucide immediately).
 */
const svgCache = new Map<string, string | null>();

/**
 * Minimal client-side SVG sanitizer.
 *
 * The backend has ALREADY stripped dangerous elements and attributes
 * when serving ``/api/skills/{id}/icon.svg`` (see
 * ``omweb.skills.manifest.sanitize_svg``). This second pass is a
 * defense-in-depth measure that costs nothing and protects against:
 *   * a misconfigured deployment serving a different backend, or
 *   * an operator manually injecting an SVG into a static host.
 *
 * Returns ``null`` when the input is not recognizably an SVG.
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
 * Render a skill's icon.
 *
 * Resolution order:
 *   1. Fetch ``GET /api/skills/{id}/icon.svg`` (which serves either
 *      the manifest's ``icon_svg`` or the manifest's ``icon`` file).
 *   2. On success, cache and render the sanitized SVG inline.
 *   3. On 404 or parse failure, fall back to a Lucide ``Sparkles``
 *      glyph (matching the chat composer's chip icon) so the card
 *      never shows a broken placeholder.
 *
 * Size is controlled by the caller (default 40px, matching the
 * pattern used by ToolIcon / AgentIcon). The component never
 * overrides the parent's CSS ``color`` — SVG content inherits via
 * ``currentColor``.
 */
export function SkillIcon({
  skill,
  size = 40,
  className,
}: SkillIconProps) {
  const [svgText, setSvgText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    setSvgText(null);

    const cacheKey = `${skill.id}@${skill.version || "0.0.0"}`;

    // Cache hit — resolve immediately without a network round-trip.
    if (svgCache.has(cacheKey)) {
      const cached = svgCache.get(cacheKey) ?? null;
      if (cached === null) setFailed(true);
      else setSvgText(cached);
      return;
    }

    // A skill with NO declared icon (neither ``icon_svg`` nor
    // ``icon``) does not need a network request at all — we know
    // the endpoint will 404. Short-circuit to the Lucide fallback
    // so the card renders instantly with no layout shift.
    if (!skill.has_icon_svg && !skill.has_icon_path) {
      svgCache.set(cacheKey, null);
      setFailed(true);
      return;
    }

    const url = getApiUrl(
      `/api/skills/${encodeURIComponent(skill.id)}/icon.svg`
    );
    const ctrl = new AbortController();

    fetch(url, { signal: ctrl.signal, credentials: "same-origin" })
      .then((r) =>
        r.ok ? r.text() : Promise.reject(new Error(String(r.status)))
      )
      .then((text) => {
        const safe = sanitizeSvg(text);
        svgCache.set(cacheKey, safe);
        if (cancelled) return;
        if (safe === null) setFailed(true);
        else setSvgText(safe);
      })
      .catch((e) => {
        if (cancelled || e?.name === "AbortError") return;
        svgCache.set(cacheKey, null);
        setFailed(true);
      });

    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [skill.id, skill.version, skill.has_icon_svg, skill.has_icon_path]);

  // ── Fallback: Lucide ─────────────────────────────────────────
  if (failed) {
    return (
      <span
        className={className}
        aria-hidden="true"
        style={{
          width: size,
          height: size,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Sparkles size={size} />
      </span>
    );
  }

  // ── Loading placeholder (same box size → no layout shift) ───
  if (!svgText) {
    return (
      <span
        className={className}
        aria-hidden="true"
        style={{ width: size, height: size, display: "inline-block" }}
      />
    );
  }

  // ── Inline SVG ───────────────────────────────────────────────
  return (
    <span
      className={className}
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        color: "currentColor",
      }}
      dangerouslySetInnerHTML={{ __html: svgText }}
    />
  );
}
