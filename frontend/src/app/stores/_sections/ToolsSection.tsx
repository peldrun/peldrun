// frontend/src/app/stores/_sections/ToolsSection.tsx

"use client";

import React, { useCallback, useMemo, useState } from "react";
import { FileArchive, Upload, Wrench } from "lucide-react";
import type { ToolDefinition } from "@/lib/types";
import type { BundleEntry } from "@/lib/bundleApi";
import { fetchToolManifest } from "@/lib/chatsApi";
import { BundleCard } from "@/components/store/BundleCard";
import { ToolCard, type ToolWithMeta } from "../_components/ToolCard";
import { ToolDetailsModal } from "../_modals/ToolDetailsModal";
import { ToolSettingsModal } from "../_modals/ToolSettingsModal";
import type { SectionHeaderConfig } from "../_config/sections";
import type { ToolFilter } from "../_types/store";

/**
 * Props for {@link ToolsSection}.
 * All action handlers are forwarded from the page orchestrator,
 * which owns `useToolActions` and `useBundleActions`.
 */
export interface ToolsSectionProps {
  tools: ToolDefinition[];
  bundles: BundleEntry[];
  searchQuery: string;
  toolFilter: ToolFilter;
  togglingToolId: string | null;
  onToggleTool: (toolId: string) => void;
  onDeleteTool: (toolId: string) => void;
  onOpenBundleUpload: () => void;
  onOpenToolUpload: () => void;
  onBundleActivate: (bundle: BundleEntry, version: string) => void;
  onBundleDelete: (bundle: BundleEntry) => void;
  onBundleDetails: (bundle: BundleEntry) => void;
}

/**
 * Build the section-specific header configuration for the Tools section.
 *
 * Two entry points are exposed:
 *  - Install Bundle (.zip)  → atomic, content-addressed bundles
 *  - Upload Single File     → legacy single-file tools (.py / .json)
 */
export function getToolsHeaderConfig(ctx: {
  onInstallBundle: () => void;
  onUploadSingleFile: () => void;
}): SectionHeaderConfig {
  return {
    title: "Sovereign Tools",
    subtitle: "Local Python tools and installed bundles",
    badge: "TOOLS",
    actions: [
      {
        id: "install-bundle",
        label: "Install Bundle (.zip)",
        icon: FileArchive,
        variant: "primary",
        onClick: ctx.onInstallBundle,
      },
      {
        id: "upload-single-file",
        label: "Upload Single File",
        icon: Upload,
        variant: "outline",
        onClick: ctx.onUploadSingleFile,
      },
    ],
  };
}

/**
 * Tools section content.
 *
 * Layout:
 *  1. Installed Bundles   → responsive grid (3 columns on lg).
 *                           Bundles are visual, versioned artifacts so a
 *                           grid layout surfaces them better.
 *  2. Single-File Tools   → vertical list (one row per tool).
 *                           Tools expose dense metadata (tags, safety,
 *                           permissions) which reads better in a row.
 *
 * Details:
 *  - Clicking the info icon on any tool opens a modal showing the full
 *    manifest. The modal state is local to this section because it does
 *    not need to be coordinated with any other part of the page.
 *
 * The section is otherwise stateless: all mutations are delegated to
 * the parent.
 */
export function ToolsSection({
  tools,
  bundles,
  searchQuery,
  toolFilter,
  togglingToolId,
  onToggleTool,
  onDeleteTool,
  onBundleActivate,
  onBundleDelete,
  onBundleDetails,
}: ToolsSectionProps) {
  const [detailsTool, setDetailsTool] = useState<ToolWithMeta | null>(null);
  // Settings modal: which tool (if any) is being configured.
  const [settingsTool, setSettingsTool] = useState<ToolWithMeta | null>(null);

  /**
   * Open the details modal for a tool.
   *
   * Strategy:
   *  1. Display the row's already-loaded (short) data immediately so the
   *     user sees content without any delay.
   *  2. Fetch the full manifest from `/api/store/tools/{id}` in the
   *     background.
   *  3. Merge the response into the currently-open tool — but only if
   *     the modal is still showing the same tool (guards against a race
   *     when the user closes and opens a different tool quickly).
   */
  const openDetails = useCallback(async (tool: ToolWithMeta) => {
    setDetailsTool(tool);
    try {
      const full = await fetchToolManifest(tool.id);
      if (full) {
        setDetailsTool((prev) =>
          prev && prev.id === tool.id
            ? ({ ...prev, ...(full as Partial<ToolWithMeta>) } as ToolWithMeta)
            : prev
        );
      }
    } catch (err) {
      console.error("Failed to load full tool manifest:", err);
    }
  }, []);

  const filteredTools = useMemo(() => {
    return tools.filter((tl) => {
      const q = searchQuery.toLowerCase().trim();
      const matchesQuery =
        !q ||
        tl.name.toLowerCase().includes(q) ||
        (tl.description || "").toLowerCase().includes(q) ||
        (tl.category || "").toLowerCase().includes(q);

      if (!matchesQuery) return false;
      if (toolFilter === "builtin") return tl.is_builtin;
      if (toolFilter === "custom") return !tl.is_builtin;
      return true;
    });
  }, [tools, searchQuery, toolFilter]);

  const hasBundles = bundles.length > 0;

  return (
    <div className="space-y-6">
      {/* ── Installed Bundles (grid) ────────────────────────────── */}
      {hasBundles && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <FileArchive size={14} className="text-primary" />
              <h3 className="text-sm font-bold text-foreground">
                Installed Bundles
              </h3>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground border border-border">
                {bundles.length}
              </span>
            </div>
            <span className="text-[10px] text-muted-foreground font-mono">
              Content-addressed · Atomic install · Rollback ready
            </span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
            {bundles.map((bd) => (
              <BundleCard
                key={bd.id}
                bundle={bd}
                onActivate={(version) => onBundleActivate(bd, version)}
                onDelete={() => onBundleDelete(bd)}
                onViewDetails={() => onBundleDetails(bd)}
              />
            ))}
          </div>
        </div>
      )}

      {/* ── Single-File Tools (list) ────────────────────────────── */}
      <div>
        {hasBundles && (
          <div className="flex items-center gap-2 mb-3">
            <Wrench size={14} className="text-muted-foreground" />
            <h3 className="text-sm font-bold text-foreground">
              Single-File Tools
            </h3>
            <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground border border-border">
              {filteredTools.length}
            </span>
          </div>
        )}

        {filteredTools.length === 0 ? (
          <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
            <Wrench className="h-10 w-10 text-muted-foreground mb-3" />
            <h3 className="text-sm font-semibold text-foreground">
              No tools found
            </h3>
            <p className="text-xs text-muted-foreground mt-1">
              Upload a custom Python tool (.py) or JSON manifest to extend
              capabilities.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {filteredTools.map((tl) => (
              <ToolCard
                key={tl.id}
                tool={tl}
                isToggling={togglingToolId === tl.id}
                onToggle={onToggleTool}
                onDelete={onDeleteTool}
                onViewDetails={openDetails}
                onOpenSettings={(tool) => setSettingsTool(tool)}
              />
            ))}
          </div>
        )}
      </div>

      {/* ── Tool details modal ─────────────────────────────────── */}
      <ToolDetailsModal
        open={detailsTool !== null}
        tool={detailsTool}
        onClose={() => {
          setDetailsTool(null);
        }}
      />

      {/* ── Tool settings modal ────────────────────────────────── */}
      <ToolSettingsModal
        open={settingsTool !== null}
        toolId={settingsTool?.id ?? null}
        toolName={settingsTool?.name ?? ""}
        onClose={() => setSettingsTool(null)}
      />
    </div>
  );
}
