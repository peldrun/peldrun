// frontend/src/components/source-control/source-control-workspace-panel.tsx

/**
 * frontend/src/components/source-control/source-control-workspace-panel.tsx
 *
 * Standalone side-panel that mirrors the visual frame of
 * `WorkspacePanel` (fixed height header, tab bar, action buttons)
 * but whose body is entirely Source Control.
 *
 * The header shows the current repository, the current branch, and
 * two actions:
 *
 *   - Refresh   : triggers the `peldrun:artifact-updated` event so
 *                 every mounted Source Control view (Files, History,
 *                 Branches, PRs, ...) re-fetches its data.
 *   - Export ZIP: hands the download off to the same endpoint the
 *                 main WorkspacePanel uses. This is handy because
 *                 the side panel typically hosts the branch review
 *                 and the user often wants to save the workspace
 *                 right after a review.
 *
 * The wrapper does NOT re-implement any of the tabs. The inner
 * `SourceControlPanel` owns:
 *
 *   - Agent / Manual / Browse mode switch,
 *   - The entire repository browser (Files, History, Branches),
 *   - Pull Requests, Issues, Tags, Releases.
 *
 * The wrapper adds two props that `SourceControlPanel` does not
 * need when embedded in the main WorkspacePanel:
 *
 *   - `activeChatId` is required, because the side panel is
 *     meaningless without a chat context.
 *   - `onClose` is optional, so the panel can be dismissed from
 *     whichever layout hosts it (drawer, split view, modal).
 */

"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  Download,
  GitBranch,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";
import { SourceControlPanel } from "@/features/source-control/components/SourceControlPanel";
import { Button } from "@/components/ui/button";
import { sourceControlClient } from "@/features/source-control/api/source-control-client";
import type { WorkspaceResponse } from "@/features/source-control/api/schemas";

export interface SourceControlWorkspacePanelProps {
  /**
   * The chat whose workspace the panel is bound to. This is the
   * same value `WorkspacePanel` receives as `activeChatId`. When
   * `null`, the panel renders an empty state so the layout can
   * show it unconditionally without crashing on the first render.
   */
  activeChatId: string | null;
  /**
   * Initial mode for the inner `SourceControlPanel`.
   *
   * - `"browse"` (default): open the repository browser directly.
   *   This is the right choice when the user clicks the GitBranch
   *   icon in the chat header without any specific operation in
   *   mind.
   * - `"agent"`: open in Agent mode. Used when `handleOpenReview`
   *   fires from a "Waiting for your review" card in the chat.
   * - `"manual"`: open in Manual mode. Supported for completeness,
   *   though currently no caller uses it directly.
   *
   * The value is honored at first render only. Once the inner
   * panel is mounted, it keeps its own `mode` state; changing this
   * prop later has no effect. Callers that need to force a mode
   * switch must change the React `key` (as `ChatContainer` does
   * via `sourceControlMountKey`).
   */
  defaultMode?: "agent" | "manual" | "browse";
  /**
   * Optional dismiss handler. When provided, a close button is
   * rendered in the header. Omit it for embedded layouts that do
   * not want the panel to disappear.
   */
  onClose?: () => void;
}

export function SourceControlWorkspacePanel({
  activeChatId,
  defaultMode = "browse",
  onClose,
}: SourceControlWorkspacePanelProps) {
  const [workspace, setWorkspace] = useState<WorkspaceResponse | null>(
    null
  );
  const [exporting, setExporting] = useState<boolean>(false);
  const [refreshing, setRefreshing] = useState<boolean>(false);

  // --- Fetch the workspace metadata so the header can show the
  //     branch name. ---
  //
  // This is best-effort: the panel still works without it. It only
  // affects the chip in the header. A failure is silently ignored
  // so a transient 404 (workspace not yet created) does not spam
  // the console.
  const fetchWorkspace = useCallback(async () => {
    if (!activeChatId) {
      setWorkspace(null);
      return;
    }
    try {
      const ws = await sourceControlClient.getWorkspaceByChat(
        activeChatId
      );
      setWorkspace(ws);
    } catch {
      setWorkspace(null);
    }
  }, [activeChatId]);

  useEffect(() => {
    void fetchWorkspace();
  }, [fetchWorkspace]);

  // --- Refresh: fire the same event that `workspace-panel.tsx`
  //     listens for, so every mounted tab in SourceControlPanel
  //     re-fetches without us having to know its internals. ---
  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      // Refresh the header data (branch chip) …
      await fetchWorkspace();
      // … and ask every source-control view to re-fetch.
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("peldrun:artifact-updated", {
            detail: { source: "source-control-workspace-panel" },
          })
        );
      }
    } finally {
      // A short delay gives the spinner a visible "worked" feel;
      // the underlying re-fetches are asynchronous anyway.
      window.setTimeout(() => setRefreshing(false), 400);
    }
  };

  // --- Export ZIP: identical endpoint to the main workspace panel,
  //     so the exported artifact matches what the user would get
  //     from the Files tab of the main panel. ---
  const handleExportZip = async () => {
    if (!activeChatId) return;
    setExporting(true);
    try {
      const res = await fetch(
        `/api/run/jobs/${activeChatId}/download-zip`
      );
      if (!res.ok) {
        throw new Error(
          `Failed to export ZIP (HTTP ${res.status})`
        );
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `workspace_${activeChatId}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      console.error(
        "[SourceControlWorkspacePanel] Export failed:",
        err
      );
    } finally {
      setExporting(false);
    }
  };

  // --- Empty state when no chat is selected ---
  if (!activeChatId) {
    return (
      <div className="flex flex-col h-full bg-background border-l border-border">
        <div className="h-12 border-b border-border bg-card/40 px-4 flex items-center shrink-0">
          <span className="text-xs font-semibold text-foreground">
            Source Control
          </span>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="ml-auto p-1 rounded hover:bg-muted"
              title="Close"
              aria-label="Close"
            >
              <X className="w-3.5 h-3.5 text-muted-foreground" />
            </button>
          )}
        </div>
        <div className="flex-1 flex items-center justify-center p-6 text-center text-xs text-muted-foreground">
          Open a chat to view its source control.
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-background border-l border-border font-sans">
      {/* ---------------- Header ---------------- */}
      <div className="h-12 border-b border-border bg-card/40 backdrop-blur-sm px-4 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <GitBranch className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
          <span className="text-xs font-semibold text-foreground shrink-0">
            Source Control
          </span>
          {workspace && (
            <>
              <span className="text-[10px] text-muted-foreground truncate max-w-[160px] hidden md:inline">
                {workspace.repository_id}
              </span>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary font-mono shrink-0">
                {workspace.current_branch}
              </span>
            </>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          <Button
            variant="ghost"
            size="sm"
            onClick={handleRefresh}
            disabled={refreshing}
            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground"
            title="Refresh source control data"
          >
            <RefreshCw
              size={12}
              className={refreshing ? "animate-spin text-primary" : ""}
            />
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleExportZip}
            disabled={exporting}
            className="h-7 px-2.5 text-xs gap-1.5 text-muted-foreground hover:text-foreground border-border/60 hover:bg-muted/60"
            title="Export workspace as ZIP"
          >
            {exporting ? (
              <>
                <Loader2
                  size={12}
                  className="animate-spin text-primary"
                />
                <span>Exporting…</span>
              </>
            ) : (
              <>
                <Download size={12} className="text-primary" />
                <span>Export ZIP</span>
              </>
            )}
          </Button>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="p-1 rounded hover:bg-muted"
              title="Close"
              aria-label="Close"
            >
              <X className="w-3.5 h-3.5 text-muted-foreground" />
            </button>
          )}
        </div>
      </div>

      {/* ---------------- Body ---------------- */}
      <div className="flex-1 min-h-0 overflow-hidden bg-background">
        <SourceControlPanel
          chatId={activeChatId}
          defaultMode={defaultMode}
        />
      </div>
    </div>
  );
}

export default SourceControlWorkspacePanel;
