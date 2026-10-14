// frontend/src/app/settings/setup/tabs/llm/components/ContextBudgetPanel.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import { Gauge, Sliders, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ContextBudgetPanelProps } from "../types";

/* -------------------------------------------------------------------------- */
/* Budget segment descriptor                                                  */
/* -------------------------------------------------------------------------- */

interface BudgetSegment {
  key: string;
  label: string;
  ratio: number;
  color: string;
}

/**
 * Canonical budget distribution (must sum to 1.0).
 * Mirrors the original proportional bar shown in the LLM tab.
 */
const BUDGET_SEGMENTS: readonly BudgetSegment[] = [
  { key: "system", label: "System", ratio: 0.1, color: "bg-sky-500" },
  { key: "summary", label: "Summary", ratio: 0.15, color: "bg-amber-500" },
  { key: "recent", label: "Recent", ratio: 0.5, color: "bg-emerald-500" },
  { key: "exec", label: "Exec", ratio: 0.15, color: "bg-violet-500" },
  { key: "current", label: "Current", ratio: 0.1, color: "bg-rose-500" },
];

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Context window & token budget architecture panel.
 *
 * Hosts:
 *   - Context Window / Max Output / Temperature controls,
 *   - "Auto-detect from Model" action,
 *   - the effective input budget visualization (proportional bar + legend).
 */
export function ContextBudgetPanel({
  config,
  setConfig,
  effectiveContextWindow,
  effectiveMaxOutput,
  effectiveInputBudget,
  sanitizeTokenInput,
  onAutoDetect,
}: ContextBudgetPanelProps) {
  return (
    <motion.div
      {...FADE_UP}
      className="relative p-5 rounded-md border border-border bg-card shadow-sm space-y-4"
    >
      {/* Top accent line, consistent with the SearchTab reference design */}
      <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md bg-gradient-to-r from-transparent via-primary/60 to-transparent" />

      {/* Header */}
      <div className="flex items-center justify-between border-b border-border/60 pb-3 flex-wrap gap-2">
        <div className="flex items-center gap-2.5">
          <div className="p-2 icon">
            <Gauge size={16} className="text-primary" />
          </div>
          <span className="text-xs font-bold text-foreground uppercase tracking-wider">
            Context Window &amp; Token Budget Architecture
          </span>
        </div>

        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onAutoDetect}
          className="h-7 text-[11px] border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform flex items-center gap-1.5"
        >
          <Sparkles size={11} className="text-amber-500" />
          <span>Auto-detect from Model</span>
        </Button>
      </div>

      {/* Main controls */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Context window */}
        <div className="p-3 rounded-sm border border-border/60 bg-background/60 hover:border-primary/30 transition-colors">
          <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5">
            Context Window (Total Capacity)
          </label>
          <input
            type="number"
            value={effectiveContextWindow}
            onChange={(e) => {
              const val = sanitizeTokenInput(e.target.value, 8192);
              setConfig({
                ...config,
                llm: { ...config.llm, context_window: val, max_tokens: val },
              });
            }}
            className="w-full bg-transparent border-0 outline-none text-xs text-foreground font-mono focus:ring-0"
          />
          <span className="text-[10px] text-muted-foreground mt-1 block">
            Total available token window (inputs + outputs)
          </span>
        </div>

        {/* Max output tokens */}
        <div className="p-3 rounded-sm border border-border/60 bg-background/60 hover:border-primary/30 transition-colors">
          <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5">
            Max Output Tokens (Completion Budget)
          </label>
          <input
            type="number"
            value={effectiveMaxOutput}
            onChange={(e) => {
              const val = sanitizeTokenInput(e.target.value, 1500);
              setConfig({
                ...config,
                llm: { ...config.llm, max_output_tokens: val },
              });
            }}
            className="w-full bg-transparent border-0 outline-none text-xs text-foreground font-mono focus:ring-0"
          />
          <span className="text-[10px] text-muted-foreground mt-1 block">
            Reserved token buffer strictly for LLM generation
          </span>
        </div>

        {/* Temperature */}
        <div className="p-3 rounded-sm border border-border/60 bg-background/60 hover:border-primary/30 transition-colors">
          <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5">
            Temperature: {Number(config.llm.temperature).toFixed(2)}
          </label>
          <input
            type="range"
            min="0.0"
            max="1.5"
            step="0.05"
            value={config.llm.temperature}
            onChange={(e) =>
              setConfig({
                ...config,
                llm: {
                  ...config.llm,
                  temperature: parseFloat(e.target.value) || 0,
                },
              })
            }
            className="w-full accent-primary cursor-pointer mt-2"
          />
          <span className="text-[10px] text-muted-foreground mt-1 block">
            Deterministic (0.0) to Creative (1.5)
          </span>
        </div>
      </div>

      {/* Effective input budget visualization */}
      <div className="p-3.5 rounded-md border border-border/80 bg-background/50 space-y-2.5">
        <div className="flex items-center justify-between text-xs">
          <span className="font-semibold text-foreground flex items-center gap-1.5">
            <Sliders size={13} className="text-primary" />
            Effective Context Input Budget:
          </span>
          <span className="font-mono font-bold text-primary">
            {effectiveInputBudget.toLocaleString()} tokens
          </span>
        </div>

        {/* Proportional budget bar */}
        <div className="w-full h-3 rounded-sm bg-muted overflow-hidden flex shadow-inner">
          {BUDGET_SEGMENTS.map((segment) => (
            <div
              key={segment.key}
              style={{ width: `${segment.ratio * 100}%` }}
              className={segment.color}
              title={`${segment.label}: ${Math.round(segment.ratio * 100)}%`}
            />
          ))}
        </div>

        {/* Legend */}
        <div className="flex flex-wrap items-center justify-between text-[10px] text-muted-foreground font-mono gap-1 pt-0.5">
          {BUDGET_SEGMENTS.map((segment) => (
            <span key={segment.key} className="flex items-center gap-1">
              <span
                className={`h-2 w-2 rounded-full ${segment.color}`}
                aria-hidden="true"
              />
              {segment.label} ({Math.round(segment.ratio * 100)}%):{" "}
              {Math.round(effectiveInputBudget * segment.ratio)}
            </span>
          ))}
        </div>
      </div>
    </motion.div>
  );
}
