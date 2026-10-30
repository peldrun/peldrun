// frontend\src\app\stores\_components\ExtensionCard.tsx
"use client";

import React from "react";
import {
  CheckCircle2,
  Cpu,
  Loader2,
  Play,
  Settings,
  Trash2,
  Wrench,
  XCircle,
  ExternalLink,
  ShieldCheck,
} from "lucide-react";
import type { ExtensionDefinition } from "@/lib/types";
import type { McpTestResult } from "../_types/store";

export interface ExtensionCardProps {
  extension: ExtensionDefinition;
  isToggling: boolean;
  isTesting: boolean;
  isDeleting?: boolean;
  testResult?: McpTestResult;
  onToggle: (extId: string) => void;
  onTest: (extId: string) => void;
  onConfigure?: (extension: ExtensionDefinition) => void;
  onManageTools?: (extension: ExtensionDefinition) => void;
  onOpenDetails: (extension: ExtensionDefinition) => void;
  onDelete?: (extId: string) => void;
  isGloballyDisabled?: boolean; // ← New prop for global MCP state
}

export function ExtensionCard({
  extension,
  isToggling,
  isTesting,
  isDeleting = false,
  testResult,
  onToggle,
  onTest,
  onConfigure,
  onManageTools,
  onOpenDetails,
  onDelete,
  isGloballyDisabled = false,
}: ExtensionCardProps) {
  // Treat extension as disabled in UI if either server is inactive OR global MCP is off
  const isServerActive = extension.status === "active";
  const isActive = isServerActive && !isGloballyDisabled;

  const isBuiltin = Boolean((extension as any).is_builtin);
  const iconSvg = (extension as any).icon_svg;
  const shortDesc = (extension as any).short_description || extension.description;
  const trust = (extension as any).trust;

  // Render SVG icon if provided, else fallback to Cpu
  const renderIcon = () => {
    if (iconSvg) {
      return (
        <div
          className="w-full h-full text-black"
          dangerouslySetInnerHTML={{ __html: iconSvg }}
        />
      );
    }
    return <Cpu size={20} className="text-primary" />;
  };

  return (
    <div
      className={`p-4 rounded-sm border hover:shadow-sm transition-all flex flex-col sm:flex-row sm:items-center justify-between gap-4 ${
        isActive ? "border-card bg-card" : "border-border bg-muted opacity-80"
      }`}
    >
      {/* ─── Left: Icon + Info ────────────────────────────────── */}
      <div className="flex items-center gap-4 min-w-0 flex-1">
        <div className="w-9 max-w-[60px] p-0 shrink-0">
          {renderIcon()}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <h3 className="text-sm font-bold text-foreground truncate">
              {extension.name}
            </h3>
            {trust === "verified" && (
              <span title="Verified Publisher" className="inline-flex items-center">
                <ShieldCheck size={14} className="text-emerald-500" />
              </span>
            )}
            {!isBuiltin && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground font-mono">
                custom
              </span>
            )}
          </div>
          <p className="text-xs text-muted-foreground line-clamp-2 leading-relaxed">
            {shortDesc}
          </p>
        </div>
      </div>

      {/* ─── Right: Actions ───────────────────────────────────── */}
      <div className="flex items-center gap-2 shrink-0 sm:justify-end">
        {/* Test Result Badge */}
        {testResult && (
          <div
            className={`mr-2 px-2 py-1 rounded text-[10px] flex items-center gap-1 border font-medium ${
              testResult.ok
                ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-600"
                : "bg-destructive/10 border-destructive/20 text-destructive"
            }`}
            title={testResult.message}
          >
            {testResult.ok ? <CheckCircle2 size={12} /> : <XCircle size={12} />}
            <span className="hidden md:inline truncate max-w-[100px]">
              {testResult.ok ? "Connected" : "Failed"}
            </span>
          </div>
        )}

        {/* Details Button (Google Drive Style) */}
        <button
          type="button"
          onClick={() => onOpenDetails(extension)}
          className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-sm border border-border bg-background hover:bg-muted text-foreground transition-colors font-medium"
        >
          Details
          <ExternalLink size={12} className="text-muted-foreground" />
        </button>

        {/* Test Connection */}
        <button
          type="button"
          onClick={() => onTest(extension.id)}
          disabled={isTesting || isGloballyDisabled}
          className="flex items-center justify-center w-8 h-8 rounded-md bg-secondary hover:bg-secondary/80 text-secondary-foreground transition-colors disabled:opacity-50"
          title="Test Connection"
        >
          {isTesting ? (
            <Loader2 size={14} className="animate-spin text-primary" />
          ) : (
            <Play size={12} className="text-primary fill-primary" />
          )}
        </button>

        {/* Settings (If not built-in, or if it has env vars) */}
        {onConfigure && (
          <button
            type="button"
            onClick={() => onConfigure(extension)}
            className="flex items-center justify-center w-8 h-8 rounded-md border border-border bg-card hover:bg-muted text-muted-foreground transition-colors"
            title="Settings"
          >
            <Settings size={14} />
          </button>
        )}

        {/* Manage Tools (Only for MCP) */}
        {onManageTools && (
          <button
            type="button"
            onClick={() => onManageTools(extension)}
            className="flex items-center justify-center w-8 h-8 rounded-md border border-border bg-card hover:bg-muted text-muted-foreground transition-colors"
            title="Manage Tools"
          >
            <Wrench size={14} />
          </button>
        )}

        {/* Delete (Only if NOT built-in) */}
        {!isBuiltin && onDelete && (
          <button
            type="button"
            onClick={() => onDelete(extension.id)}
            disabled={isDeleting}
            className="flex items-center justify-center w-8 h-8 rounded-md border border-transparent hover:border-destructive/30 hover:bg-destructive/10 text-muted-foreground hover:text-destructive transition-colors disabled:opacity-50"
            title="Delete"
          >
            {isDeleting ? (
              <Loader2 size={14} className="animate-spin text-destructive" />
            ) : (
              <Trash2 size={14} />
            )}
          </button>
        )}

        {/* Divider + Toggle */}
        <div className="w-px h-6 bg-border mx-1" />

        <button
          type="button"
          onClick={() => onToggle(extension.id)}
          disabled={isToggling || isGloballyDisabled}
          className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none disabled:opacity-50 ${
            isServerActive ? "bg-emerald-500" : "bg-muted-foreground/30"
          }`}
          title={isServerActive ? "Deactivate Server" : "Activate Server"}
        >
          <span
            className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-md ring-0 transition duration-200 ease-in-out ${
              isServerActive ? "translate-x-4" : "translate-x-0"
            }`}
          />
        </button>
      </div>
    </div>
  );
}
