// frontend/src/app/stores/_components/ExtensionCard.tsx

"use client";

import React from "react";
import {
  CheckCircle2,
  Cpu,
  Loader2,
  Play,
  XCircle,
} from "lucide-react";
import type { ExtensionDefinition } from "@/lib/types";
import type { McpTestResult } from "../_types/store";

/**
 * Props for {@link ExtensionCard}.
 * All actions and state are injected so the card stays purely
 * presentational.
 */
export interface ExtensionCardProps {
  extension: ExtensionDefinition;
  /** True while a toggle request for this specific extension is in flight. */
  isToggling: boolean;
  /** True while a connectivity test for this specific extension is running. */
  isTesting: boolean;
  /** Last stored test result, if any. */
  testResult?: McpTestResult;
  onToggle: (extId: string) => void;
  onTest: (extId: string) => void;
}

/**
 * Card representing a single MCP extension / server.
 *
 * Visual states:
 *  - active   → primary accent border + tinted background
 *  - inactive → neutral border, muted status dot
 *  - testing  → spinner inside the "Test Connection" button
 *  - tested   → inline success/failure box below the command preview
 */
export function ExtensionCard({
  extension,
  isToggling,
  isTesting,
  testResult,
  onToggle,
  onTest,
}: ExtensionCardProps) {
  const isActive = extension.status === "active";

  return (
    <div
      className={`p-5 rounded-sm border bg-card shadow-xs hover:shadow-md transition-all flex flex-col justify-between ${
        isActive ? "border-primary/40 bg-primary/5" : "border-border"
      }`}
    >
      <div>
        {/* Header: icon + name + live toggle */}
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="p-2 rounded-lg bg-primary/10 text-primary border border-primary/20 shrink-0">
              <Cpu size={16} />
            </div>
            <h3 className="text-sm font-bold text-foreground truncate">
              {extension.name}
            </h3>
          </div>

          <button
            type="button"
            onClick={() => onToggle(extension.id)}
            disabled={isToggling}
            className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none disabled:opacity-60 ${
              isActive ? "bg-cgood" : "bg-[#777171]"
            }`}
            title={isActive ? "Disable extension" : "Activate extension"}
            aria-label={isActive ? "Disable extension" : "Activate extension"}
          >
            <span
              className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-background shadow-md ring-0 transition duration-200 ease-in-out ${
                isActive ? "translate-x-4" : "translate-x-0"
              }`}
            />
          </button>
        </div>

        {/* Description */}
        <p className="text-xs text-muted-foreground mb-4 leading-relaxed">
          {extension.description}
        </p>

        {/* Command preview */}
        {extension.command && (
          <div className="p-2.5 rounded-lg bg-muted/50 border border-border text-[11px] font-mono text-muted-foreground truncate select-all mb-3">
            <span className="text-primary mr-1.5">$</span>
            {extension.command}
          </div>
        )}

        {/* Inline test result */}
        {testResult && (
          <div
            className={`p-2.5 rounded-lg text-xs flex items-start gap-2 border mb-3 ${
              testResult.ok
                ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-600 dark:text-emerald-400"
                : "bg-destructive/10 border-destructive/20 text-destructive"
            }`}
          >
            {testResult.ok ? (
              <CheckCircle2 size={14} className="shrink-0 mt-0.5" />
            ) : (
              <XCircle size={14} className="shrink-0 mt-0.5" />
            )}
            <span className="leading-tight">{testResult.message}</span>
          </div>
        )}
      </div>

      {/* Footer: status dot + test button */}
      <div className="pt-3 border-t border-border flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              isActive ? "bg-emerald-500 animate-pulse" : "bg-muted-foreground"
            }`}
          />
          <span className="capitalize">{extension.status}</span>
        </div>

        <button
          type="button"
          onClick={() => onTest(extension.id)}
          disabled={isTesting}
          className="flex items-center gap-1 text-[11px] px-2.5 py-1 rounded bg-secondary hover:bg-secondary/80 text-secondary-foreground font-medium transition cursor-pointer disabled:opacity-60"
        >
          {isTesting ? (
            <Loader2 size={11} className="animate-spin text-primary" />
          ) : (
            <Play size={10} className="text-primary fill-primary" />
          )}
          <span>Test Connection</span>
        </button>
      </div>
    </div>
  );
}
