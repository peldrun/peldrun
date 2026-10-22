// frontend/src/app/stores/_sections/ExtensionsSection.tsx

"use client";

import React, { useMemo } from "react";
import { Puzzle, Upload } from "lucide-react";
import type { ExtensionDefinition } from "@/lib/types";
import { ExtensionCard } from "../_components/ExtensionCard";
import type { SectionHeaderConfig } from "../_config/sections";
import type { McpTestResultMap } from "../_types/store";

/**
 * Props for {@link ExtensionsSection}.
 * All action handlers are forwarded from the page orchestrator,
 * which owns `useExtensionActions`.
 */
export interface ExtensionsSectionProps {
  extensions: ExtensionDefinition[];
  searchQuery: string;
  /** Currently toggling extension id (or null). */
  togglingExtId: string | null;
  /** Currently testing extension id (or null). */
  testingExtId: string | null;
  /** Per-extension last test result. */
  testResults: McpTestResultMap;
  onToggleExtension: (extId: string) => void;
  onTestExtension: (extId: string) => void;
}

/**
 * Build the section-specific header configuration for the Extensions
 * & MCP section.
 *
 * A single action is exposed: uploading a new MCP server manifest.
 */
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
        label: "Upload MCP (.json)",
        icon: Upload,
        variant: "primary",
        onClick: ctx.onUploadMCP,
      },
    ],
  };
}

/**
 * Extensions & MCP section content.
 *
 * Responsibilities:
 *  - Filter the extensions collection by the free-text search query.
 *    (No segmented filter is used in this section — MCP servers do not
 *    have a built-in/custom distinction.)
 *  - Render a two-column responsive grid of {@link ExtensionCard}.
 *  - Render a friendly empty state when no extensions match.
 *
 * The section is stateless: every mutation is delegated to the parent.
 */
export function ExtensionsSection({
  extensions,
  searchQuery,
  togglingExtId,
  testingExtId,
  testResults,
  onToggleExtension,
  onTestExtension,
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
          Upload an MCP server manifest (.json) to connect external tool
          protocols.
        </p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
      {filteredExtensions.map((ex) => (
        <ExtensionCard
          key={ex.id}
          extension={ex}
          isToggling={togglingExtId === ex.id}
          isTesting={testingExtId === ex.id}
          testResult={testResults[ex.id]}
          onToggle={onToggleExtension}
          onTest={onTestExtension}
        />
      ))}
    </div>
  );
}
