// frontend/src/app/settings/setup/tabs/llm/components/ModelItemCard.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import {
  Brain,
  Eye,
  HelpCircle,
  Info,
  Trash2,
  Wrench,
} from "lucide-react";
import type { ModelItemCardProps } from "../types";

/* -------------------------------------------------------------------------- */
/* Source label helper                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Render a human-readable label for a capability source. Used only in
 * tooltips — never displayed inline (would clutter the compact card).
 *
 * These labels mirror the backend `CapabilitySource` enum. The frontend
 * never infers a source; it only displays what the metadata carries.
 */
function sourceLabel(source: string): string {
  switch (source) {
    case "manual":
      return "Set manually by the user";
    case "model_metadata":
      return "Declared in model metadata";
    case "provider_catalog":
      return "Reported by the provider's catalog";
    case "provider_metadata":
      return "Reported by the provider's metadata endpoint";
    case "capability_probe":
      return "Confirmed by a capability probe";
    case "runtime_observation":
      return "Observed at runtime";
    default:
      return "Source unknown";
  }
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Reusable model card used by both LM Studio and Ollama sections.
 *
 * Design principles (aligned with the project's "no hardcoded model
 * names" rule):
 *
 *   1. Capability badges are shown ONLY when the corresponding boolean
 *      is `true` — and those booleans come exclusively from the
 *      provider-returned metadata (or a manual user override), never
 *      from a name-pattern heuristic.
 *
 *   2. When `matchedMetadataKey === null` (the model has no metadata
 *      entry), a small amber "?" indicator is shown next to the info
 *      button. Clicking the info button opens the metadata modal so
 *      the user can define capabilities manually.
 *
 *   3. Each capability badge carries a `title` tooltip explaining
 *      where the value came from ("Set manually", "Reported by the
 *      provider's catalog", etc.), so the user can trust the badge
 *      or correct it.
 *
 *   4. Informational badges (`paramsDisplay`, `quantDisplay`,
 *      `contextDisplay`) are shown ONLY when the provider returned
 *      the underlying field — the frontend does not fabricate them.
 *
 * The component remains purely presentational. All decision logic
 * lives in `inferModelCapabilities` (metadata reader) and the backend
 * `capability_resolver` + `capabilities.py` (intersection).
 */
export function ModelItemCard({
  model,
  isActive,
  accent,
  capabilities,
  onSetPrimary,
  onSetVision,
  onRemove,
  onOpenInfo,
}: ModelItemCardProps) {
  const hasMetadata = capabilities.matchedMetadataKey !== null;
  const hasAnyCapability =
    capabilities.isVision || capabilities.isTools || capabilities.isReasoning;
  const hasAnyInfoBadge =
    Boolean(capabilities.paramsDisplay) ||
    Boolean(capabilities.quantDisplay) ||
    Boolean(capabilities.contextDisplay) ||
    Boolean(capabilities.publisherDisplay) ||
    Boolean(capabilities.archDisplay) ||
    Boolean(capabilities.formatDisplay);

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
      whileHover={{ y: -2, transition: { duration: 0.15 } }}
      className={`group relative p-2.5 rounded-md border text-xs font-mono flex flex-col gap-1.5 transition-all shadow-sm ${
        isActive
          ? "bg-primary/10 border-primary/40"
          : "bg-card border-border/70 hover:border-primary/30"
      }`}
    >
      {/* Active accent bar */}
      {isActive && (
        <span
          aria-hidden="true"
          className="absolute left-0 top-1 bottom-1 w-[2px] rounded-sm"
          style={{ backgroundColor: accent }}
        />
      )}

      {/* Header row: model id + actions */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0 flex-1">
          <span
            className="truncate font-semibold text-foreground"
            title={model}
          >
            {model}
          </span>

          {/* Info button — opens the metadata modal.
              The amber tint when `!hasMetadata` signals "no metadata;
              click to define capabilities" without adding a noisy
              badge. The tooltip is the only place this hint lives. */}
          <button
            type="button"
            onClick={onOpenInfo}
            className={`p-1 rounded-sm transition-colors cursor-pointer shrink-0 ${
              hasMetadata
                ? "text-muted-foreground hover:text-primary hover:bg-muted"
                : "text-amber-600 dark:text-amber-400 hover:text-primary hover:bg-muted"
            }`}
            title={
              hasMetadata
                ? "View model technical specs & capabilities"
                : "No metadata — click to define capabilities manually"
            }
          >
            <Info size={12} />
          </button>

          {/* Explicit "no metadata" indicator — only shown when the
              model has no entry at all, so the user knows the absence
              of capability badges is not "we checked and it does not
              support them" but "we have no data to check". */}
          {!hasMetadata && (
            <span
              className="flex items-center gap-0.5 text-[9px] font-sans px-1 py-0.5 rounded-sm bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20"
              title="This model has no metadata entry. Click the info icon to define its capabilities."
            >
              <HelpCircle size={9} /> unknown
            </span>
          )}
        </div>

        <div className="flex items-center gap-1 shrink-0 font-sans">
          <button
            type="button"
            onClick={onSetPrimary}
            className={`px-1.5 py-0.5 rounded-sm text-[10px] font-semibold cursor-pointer transition-all hover:scale-105 ${
              isActive
                ? "bg-emerald-500 text-white shadow-sm"
                : "bg-primary/15 text-primary hover:bg-primary/25"
            }`}
          >
            {isActive ? "Active" : "Primary"}
          </button>

          {onSetVision && (
            <button
              type="button"
              onClick={onSetVision}
              className="px-1.5 py-0.5 rounded-sm text-[10px] font-semibold bg-violet-500/15 text-violet-500 hover:bg-violet-500/25 cursor-pointer transition-all hover:scale-105"
            >
              Vision
            </button>
          )}

          <button
            type="button"
            onClick={onRemove}
            className="p-1 rounded-sm text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 transition-colors cursor-pointer"
            title="Remove model from list"
          >
            <Trash2 size={11} />
          </button>
        </div>
      </div>

      {/* Capability badges — only shown when the metadata explicitly
          says the capability is supported. Each badge's tooltip shows
          the source, so the user knows whether to trust it. */}
      {(hasAnyCapability || hasAnyInfoBadge) && (
        <div className="flex flex-wrap items-center gap-1 font-sans">
          {capabilities.isReasoning && (
            <span
              className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.5 rounded-sm bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20"
              title={sourceLabel(capabilities.reasoningSource)}
            >
              <Brain size={9} /> Reasoning
            </span>
          )}

          {capabilities.isVision && (
            <span
              className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.5 rounded-sm bg-violet-500/10 text-violet-600 dark:text-violet-400 border border-violet-500/20"
              title={sourceLabel(capabilities.visionSource)}
            >
              <Eye size={9} /> Vision
            </span>
          )}

          {capabilities.isTools && (
            <span
              className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.5 rounded-sm bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-500/20"
              title={sourceLabel(capabilities.toolsSource)}
            >
              <Wrench size={9} /> Tools
            </span>
          )}

          {capabilities.paramsDisplay && (
            <span
              className="text-[9px] font-bold px-1.5 py-0.5 rounded-sm bg-primary/15 text-primary border border-primary/30 font-mono"
              title="Parameter count — reported by the provider"
            >
              {capabilities.paramsDisplay}
            </span>
          )}

          {capabilities.quantDisplay && (
            <span
              className="text-[9px] px-1.5 py-0.5 rounded-sm bg-muted text-muted-foreground border border-border font-mono"
              title="Quantization — reported by the provider"
            >
              {capabilities.quantDisplay}
            </span>
          )}

          {capabilities.contextDisplay && (
            <span
              className="text-[9px] px-1.5 py-0.5 rounded-sm bg-muted text-muted-foreground border border-border font-mono"
              title="Context window — reported by the provider"
            >
              {capabilities.contextDisplay}
            </span>
          )}

          {capabilities.publisherDisplay && (
            <span
              className="text-[9px] px-1.5 py-0.5 rounded-sm bg-muted text-muted-foreground border border-border font-mono"
              title="Publisher — reported by the provider"
            >
              {capabilities.publisherDisplay}
            </span>
          )}
        </div>
      )}
    </motion.div>
  );
}
