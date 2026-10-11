// frontend/src/components/generative-ui/catalog/PeldrunTestResults.tsx
"use client";

import React from "react";
import { CheckCircle2, XCircle, AlertCircle } from "lucide-react";
import type { PeldrunTestResultsProps } from "../types";

export function PeldrunTestResults({
  suiteName = "Test Execution Suite",
  passedCount = 0,
  failedCount = 0,
  skippedCount = 0,
  tests = [],
}: PeldrunTestResultsProps): React.JSX.Element {
  const total = passedCount + failedCount + skippedCount;
  const passRate = total > 0 ? Math.round((passedCount / total) * 100) : 0;

  return (
    <div className="my-2 p-3.5 rounded-xl border border-border/70 bg-card/50 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-foreground tracking-tight">
          {suiteName}
        </span>
        <span
          className={`text-[10px] font-mono px-2 py-0.5 rounded-md font-bold ${
            failedCount === 0
              ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
              : "bg-red-500/15 text-red-600 dark:text-red-400"
          }`}
        >
          {passRate}% Passing
        </span>
      </div>

      <div className="grid grid-cols-3 gap-2 text-center font-mono">
        <div className="p-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
          <div className="text-[10px] text-emerald-600 dark:text-emerald-400">Passed</div>
          <div className="text-sm font-bold text-emerald-600 dark:text-emerald-400">
            {passedCount}
          </div>
        </div>
        <div className="p-2 rounded-lg bg-red-500/10 border border-red-500/20">
          <div className="text-[10px] text-red-600 dark:text-red-400">Failed</div>
          <div className="text-sm font-bold text-red-600 dark:text-red-400">
            {failedCount}
          </div>
        </div>
        <div className="p-2 rounded-lg bg-muted border border-border">
          <div className="text-[10px] text-muted-foreground">Skipped</div>
          <div className="text-sm font-bold text-muted-foreground">
            {skippedCount}
          </div>
        </div>
      </div>

      {tests.length > 0 && (
        <div className="space-y-1 pt-1 max-h-48 overflow-y-auto">
          {tests.map((test, idx) => (
            <div
              key={`test-${idx}`}
              className="flex items-center justify-between p-1.5 rounded-md text-xs hover:bg-muted/40 transition-colors"
            >
              <div className="flex items-center gap-2 truncate">
                {test.status === "passed" && (
                  <CheckCircle2 size={13} className="text-emerald-500 shrink-0" />
                )}
                {test.status === "failed" && (
                  <XCircle size={13} className="text-red-500 shrink-0" />
                )}
                {test.status === "skipped" && (
                  <AlertCircle size={13} className="text-muted-foreground shrink-0" />
                )}
                <span className="truncate text-foreground font-mono text-[11px]">
                  {test.name}
                </span>
              </div>
              {typeof test.durationMs === "number" ? (
                <span className="text-[10px] text-muted-foreground font-mono shrink-0">
                  {test.durationMs}ms
                </span>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
