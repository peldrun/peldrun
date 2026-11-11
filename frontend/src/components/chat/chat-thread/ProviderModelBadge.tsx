// frontend/src/components/chat/chat-thread/ProviderModelBadge.tsx

"use client";

/**
 * Overlapping provider + vendor badge with hover tooltip.
 *
 * Layout:
 *   ┌─────────┐
 *   │    ┌──┐ │   ← small vendor icon (e.g. "deepseek")
 *   │    └──┘ │
 *   │  ┌───┐  │   ← main provider icon (e.g. "lmstudio")
 *   │  └───┘  │
 *   └─────────┘
 *
 * The small badge only appears when the model identifier can be
 * traced to a known vendor (see lib/modelVendor.ts). When the vendor
 * is unknown, the main provider icon renders alone — the small badge
 * is suppressed entirely so we never show a placeholder that the user
 * cannot dismiss.
 *
 * The tooltip shows the full identity on hover (provider + model +
 * mode + timestamp) and is hidden by default so the chat thread stays
 * visually quiet.
 */

import React from "react";
import { Bot } from "lucide-react";
import { Icon } from "@/components/Icon/Icon";
import { inferModelVendor } from "@/lib/modelVendor";

export interface ProviderModelBadgeProps {
  /**
   * Display name of the provider that ACTUALLY served the request
   * (e.g. "LM Studio (Local)", "GigaChat", "DeepSeek").
   */
  providerName?: string | null;

  /**
   * Full model identifier (e.g. "deepseek-r1-distill-qwen-7b",
   * "@cf/google/gemma-4-26b-a4b-it"). Used to infer the origin vendor
   * for the small badge.
   */
  modelName?: string | null;

  /**
   * Badge identifier for the provider icon (as resolved by
   * `useProviderBadge`). Falls back to a generic `<Bot>` glyph when
   * absent so a provider without a declared badge still renders.
   */
  providerBadge?: string | null;

  /** Size of the MAIN provider icon in pixels. Default: 18. */
  size?: number;

  /** Optional exec mode label rendered in the tooltip. */
  execMode?: "agent" | "chat";

  /** Optional timestamp rendered in the tooltip. */
  timestamp?: string | null;
}

export function ProviderModelBadge({
  providerName,
  modelName,
  providerBadge,
  size = 23,
  execMode,
  timestamp,
}: ProviderModelBadgeProps) {
  // Infer the ORIGIN vendor of the model (DeepSeek, Qwen, Meta, ...).
  // `vendorId` is the lowercase identifier used as the icon name —
  // exactly the same naming convention as the provider badges.
  const vendor = inferModelVendor(modelName);
  const vendorIconName = vendor.vendorId;

  const mainIconSize = size;
  const smallIconSize = Math.max(7, Math.round(size * 0.5));

  return (
    <div
      className="relative group inline-flex items-center cursor-help p-1"
      title="" // suppress native browser tooltip — we render our own
    >
      {/* ─── Overlapping icons ─── */}
      <div className="relative">
        {/* Main provider icon */}
        {providerBadge ? (
          <Icon name={providerBadge} size={mainIconSize} />
        ) : (
          <Bot
            size={mainIconSize - 1}
            className="text-primary shrink-0"
          />
        )}

        {/* Small vendor icon, overlapping the top-right corner of
            the provider icon. `-top-1 -right-1` positions it just
            outside the main icon's bounding box; the `p-1` on the
            outer wrapper guarantees there is room for it without
            being clipped by any surrounding `overflow-hidden`
            container. */}
        {vendorIconName && (
          <span
            className="absolute -top-2 -right-2 flex items-center justify-center rounded-full bg-background/90 ring-1 ring-border shadow-md w-3.5 h-3.5"
            aria-label={vendor.vendorName}
          >
            <Icon name={vendorIconName} size={smallIconSize} />
          </span>
        )}
      </div>

      {/* ─── Hover tooltip ─── */}
      <div
        className="absolute left-1 top-full mt-1 hidden group-hover:flex flex-col items-start gap-1 rounded-md bg-popover px-3 py-2 text-popover-foreground shadow-lg border border-border z-50 whitespace-nowrap font-mono animate-in fade-in zoom-in-95 min-w-max"
        role="tooltip"
      >
        {providerName && (
          <span className="font-semibold text-xs text-primary">
            {providerName}
          </span>
        )}
        {modelName && (
          <span className="font-semibold text-xs text-foreground">
            {modelName}
          </span>
        )}
        {vendor.vendorName &&
          vendor.confidence === "exact" &&
          modelName && (
            <span className="text-[10px] text-muted-foreground">
              Vendor: {vendor.vendorName}
            </span>
          )}
        <div className="flex items-center gap-2 text-[10px] mt-0.5">
          {execMode === "agent" && (
            <span className="px-1.5 py-0.5 rounded bg-primary/10 text-primary border border-primary/20 font-mono">
              Agent
            </span>
          )}
          {timestamp && (
            <span className="text-muted-foreground font-normal">
              {timestamp}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export default ProviderModelBadge;
