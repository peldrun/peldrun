// frontend/src/app/stores/_sections/ExtensionsSection.tsx
"use client";

import React, { useMemo } from "react";
import { Puzzle, Upload, AlertTriangle } from "lucide-react";
import type { ExtensionDefinition } from "@/lib/types";
import { ExtensionCard } from "../_components/ExtensionCard";
import type { SectionHeaderConfig } from "../_config/sections";
import type { McpTestResultMap } from "../_types/store";

export interface ExtensionsSectionProps {
  extensions: ExtensionDefinition[];
  searchQuery: string;
  togglingExtId: string | null;
  testingExtId: string | null;
  deletingExtId?: string | null;
  testResults: McpTestResultMap;
  onToggleExtension: (extId: string) => void;
  onTestExtension: (extId: string) => void;
  onConfigureExtension?: (extension: ExtensionDefinition) => void;
  onManageToolsExtension?: (extension: ExtensionDefinition) => void;
  onOpenDetailsExtension: (extension: ExtensionDefinition) => void;
  onDeleteExtension?: (extId: string) => void;
  isMcpGloballyDisabled?: boolean;
}

export function getExtensionsHeaderConfig(ctx: {
  onUploadMCP: () => void;
}): SectionHeaderConfig {
  return {
    title: "Extensions & MCP",
    subtitle: "MCP protocol servers and extensions",
    badge: "MCP",
    actions: [
      {
        id: "upload-mcp",
        label: "Upload MCP (.json / .zip)",
        icon: Upload,
        variant: "primary",
        onClick: ctx.onUploadMCP,
      },
    ],
  };
}

export function ExtensionsSection({
  extensions,
  searchQuery,
  togglingExtId,
  testingExtId,
  deletingExtId,
  testResults,
  onToggleExtension,
  onTestExtension,
  onConfigureExtension,
  onManageToolsExtension,
  onOpenDetailsExtension,
  onDeleteExtension,
  isMcpGloballyDisabled = false,
}: ExtensionsSectionProps) {
  const filteredExtensions = useMemo(() => {
    const q = searchQuery.toLowerCase().trim();
    return extensions.filter(
      (ex) =>
        !q ||
        ex.name.toLowerCase().includes(q) ||
        (ex.description || "").toLowerCase().includes(q)
    );
  }, [extensions, searchQuery]);

  if (filteredExtensions.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
        <Puzzle className="h-10 w-10 text-muted-foreground mb-3" />
        <h3 className="text-sm font-semibold text-foreground">
          No extensions found
        </h3>
        <p className="text-xs text-muted-foreground mt-1">
          Upload an MCP server manifest (.json) or bundle (.zip) to connect external tool protocols.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* ─── Global Disabled Banner ─── */}
      {isMcpGloballyDisabled && (
        <div className="p-4 rounded-s-sm border border-destructive/30 bg-destructive/10 flex items-start gap-3">
          <AlertTriangle className="h-5 w-5 text-destructive shrink-0 mt-0.5" />
          <div>
            <h4 className="text-sm font-bold text-destructive">
              MCP Subsystem is Disabled Globally
            </h4>
            <p className="text-xs text-destructive/80 mt-1">
              All MCP servers are currently paused. You cannot activate servers or test connections until you re-enable the subsystem from <strong>Settings → MCP</strong>.
            </p>
          </div>
        </div>
      )}

      {/* ─── List View ─── */}
      <div className="flex flex-col gap-3">
        {filteredExtensions.map((ex) => (
          <ExtensionCard
            key={ex.id}
            extension={ex}
            isToggling={togglingExtId === ex.id}
            isTesting={testingExtId === ex.id}
            isDeleting={deletingExtId === ex.id}
            testResult={testResults[ex.id]}
            onToggle={onToggleExtension}
            onTest={onTestExtension}
            onConfigure={onConfigureExtension}
            onManageTools={onManageToolsExtension}
            onOpenDetails={onOpenDetailsExtension}
            onDelete={onDeleteExtension}
            isGloballyDisabled={isMcpGloballyDisabled}
          />
        ))}
      </div>
    </div>
  );
}
