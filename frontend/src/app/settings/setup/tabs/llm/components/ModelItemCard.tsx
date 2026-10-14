// frontend/src/app/settings/setup/tabs/llm/components/ModelItemCard.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import {
  Brain,
  Eye,
  Info,
  Trash2,
  Wrench,
} from "lucide-react";
import type { ModelItemCardProps } from "../types";

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Reusable model card used by both LM Studio and Ollama sections.
 *
 * Visual language aligned with the SearchTab reference design:
 *   - `motion.div` with fade-up on mount,
 *   - card border tinted with the accent color when active,
 *   - capability chips rendered with the same "colored text + tinted bg +
 *     bordered" pattern used across the settings surface,
 *   - compact action cluster (Primary / Vision / Remove) with hover scale.
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
          <button
            type="button"
            onClick={onOpenInfo}
            className="p-1 rounded-sm text-muted-foreground hover:text-primary hover:bg-muted transition-colors cursor-pointer shrink-0"
            title="View model technical specs & capabilities"
          >
            <Info size={12} />
          </button>
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

      {/* Capability badges */}
      <div className="flex flex-wrap items-center gap-1 font-sans">
        {capabilities.isReasoning && (
          <span className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.5 rounded-sm bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
            <Brain size={9} /> Reasoning
          </span>
        )}

        {capabilities.isVision && (
          <span className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.5 rounded-sm bg-violet-500/10 text-violet-600 dark:text-violet-400 border border-violet-500/20">
            <Eye size={9} /> Vision
          </span>
        )}

        {capabilities.isTools && (
          <span className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.5 rounded-sm bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-500/20">
            <Wrench size={9} /> Tools
          </span>
        )}

        {capabilities.paramsDisplay && (
          <span className="text-[9px] font-bold px-1.5 py-0.5 rounded-sm bg-primary/15 text-primary border border-primary/30 font-mono">
            {capabilities.paramsDisplay}
          </span>
        )}

        {capabilities.quantDisplay && (
          <span className="text-[9px] px-1.5 py-0.5 rounded-sm bg-muted text-muted-foreground border border-border font-mono">
            {capabilities.quantDisplay}
          </span>
        )}

        {capabilities.contextDisplay && (
          <span className="text-[9px] px-1.5 py-0.5 rounded-sm bg-muted text-muted-foreground border border-border font-mono">
            {capabilities.contextDisplay}
          </span>
        )}
      </div>
    </motion.div>
  );
}
