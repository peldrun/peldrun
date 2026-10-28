// frontend/src/app/settings/setup/tabs/llm/components/ContextBudgetPanel.tsx
"use client";

import React, { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Gauge, Sliders, Sparkles, Info } from "lucide-react";
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
  // Toggles the inline explanation under the Global Default Temperature
  // slider. Collapsed by default so the panel stays compact; expanded on
  // demand for users who want to understand the precedence rules.
  const [showTempInfo, setShowTempInfo] = useState(false);

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

      {/* Main controls.
          NOTE: "Context Window" and "Max Output Tokens" were removed from
          this panel. Both are per-model settings and their single source of
          truth is the "Model Budgets & Context" tab. Editing them here
          previously showed the global default (1500) while the runtime
          actually used the per-model budget (4096), which made the two
          panels contradict each other and confused the user.
          Temperature is truly global and therefore belongs here — but it is
          intentionally placed in its OWN box (below the read-only summary)
          so it does not read as "part of the active model config". A small
          (i) button toggles an explanation of the precedence chain. */}
      <div className="space-y-3">
        {/* Active model summary (read-only) */}
        <div className="p-3 rounded-sm border border-border/60 bg-background/60">
          <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5">
            Active Model Budgets
          </label>
          <div className="text-xs text-foreground font-mono space-y-0.5">
            <div>
              Context:{" "}
              <span className="font-bold">
                {effectiveContextWindow.toLocaleString()}
              </span>{" "}
              tokens
            </div>
            <div>
              Max Output:{" "}
              <span className="font-bold">
                {effectiveMaxOutput.toLocaleString()}
              </span>{" "}
              tokens
            </div>
          </div>
          <span className="text-[10px] text-muted-foreground mt-1 block">
            These values are resolved per-model. Edit them in{" "}
            <strong>Model Budgets &amp; Context</strong>.
          </span>
        </div>

        {/* Global Default Temperature (its own box) */}
        <div className="p-3 rounded-sm border border-border/60 bg-background/60 hover:border-primary/30 transition-colors space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5">
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                Global Default Temperature
              </label>
              <button
                type="button"
                onClick={() => setShowTempInfo((v) => !v)}
                className="text-muted-foreground hover:text-primary cursor-pointer transition-colors"
                aria-label="How temperature is resolved"
                title="How temperature is resolved"
              >
                <Info size={12} />
              </button>
            </div>
            <span className="text-xs font-mono font-bold text-primary tabular-nums">
              {Number(config.llm.temperature).toFixed(2)}
            </span>
          </div>

          {/* Collapsible explanation */}
          <AnimatePresence initial={false}>
            {showTempInfo && (
              <motion.div
                key="temp-info"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
                className="overflow-hidden"
              >
                <div className="p-3 rounded-md border border-primary/20 bg-primary/5 text-[11px] text-foreground/90 leading-relaxed space-y-1.5">
                  <p className="font-semibold text-primary">
                    How temperature is resolved
                  </p>
                  <ol className="list-decimal list-inside space-y-1">
                    <li>
                      <strong>Per-model override</strong> (highest priority):
                      if you set a temperature for this model in{" "}
                      <strong>Model Budgets &amp; Context</strong>, that value
                      wins — regardless of what is shown here.
                    </li>
                    <li>
                      <strong>Global default</strong> (this slider): used for
                      any model that has no per-model override.
                    </li>
                    <li>
                      <strong>Built-in fallback</strong>: if neither is
                      configured, the runtime uses its historical default
                      (0.7 for chat, 0.2 for agent mode).
                    </li>
                  </ol>
                  <p className="text-muted-foreground">
                    Range: 0.00 = fully deterministic · 1.50 = most creative.
                    This value is saved via <strong>Save Config</strong>.
                  </p>
                </div>
              </motion.div>
            )}
          </AnimatePresence>

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
            className="w-full accent-primary cursor-pointer mt-1"
          />
          <div className="flex items-center justify-between text-[10px] text-muted-foreground font-mono">
            <span>Deterministic (0.00)</span>
            <span>Balanced (0.70)</span>
            <span>Creative (1.50)</span>
          </div>
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
