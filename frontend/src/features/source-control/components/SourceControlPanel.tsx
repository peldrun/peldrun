// frontend/src/features/source-control/components/SourceControlPanel.tsx

import React, { useEffect, useState } from "react";
import { sourceControlClient } from "../api/source-control-client";
import { useSourceControlStore } from "../state/source-control-store";
import { useOperation } from "../hooks/useOperation";
import { useManualModeStore } from "../state/manual-mode-store";
import { ApprovalHeader } from "./ApprovalHeader";
import { FileChangeList } from "./FileChangeList";
import { DiffViewer } from "./DiffViewer";
import { CheckResults } from "./CheckResults";
import { ApprovalDialog } from "./ApprovalDialog";
import { OperationTimeline } from "./OperationTimeline";
import { ManualModePanel } from "./manual/ManualModePanel";
import { GitBrowserPanel } from "./git/GitBrowserPanel";
import { GitPullRequest, AlertCircle } from "lucide-react";

export interface SourceControlPanelProps {
  chatId?: string;
}

type PanelMode = "agent" | "manual" | "browse";

/**
 * Source Control panel with two modes: Agent and Manual.
 *
 * DESIGN DECISION — both panels are kept MOUNTED at all times:
 *
 *   The Agent panel owns a live `useOperation` polling loop. If we
 *   conditionally rendered only the active mode, switching to
 *   Manual Mode would unmount Agent Mode, which:
 *
 *     - Cancels the polling loop (operation updates stop arriving).
 *     - Resets local view state (active tab, scroll position).
 *     - Forces a full re-fetch when switching back.
 *
 *   By keeping both mounted and hiding the inactive one with the
 *   `hidden` class, switching modes is instant, cheap, and does
 *   not disturb either workflow's state.
 *
 * DESIGN DECISION — Manual Mode state is NOT reset on toggle:
 *
 *   The manual store preserves (chatId, step, workspaceId, diff,
 *   approvalId) across toggles. The user resumes exactly where they
 *   left off when they switch back. Files on disk are never touched
 *   by a mode switch.
 */
export function SourceControlPanel({
  chatId: propChatId,
}: SourceControlPanelProps = {}) {
  const [mode, setMode] = useState<PanelMode>("agent");

  // Resolve chat id: prefer the prop; fall back to URL.
  const resolvedChatId = propChatId ?? deriveChatIdFromUrl();

  // Activate the manual store only when the user first switches to
  // Manual Mode. Subsequent toggles do NOT re-activate, so the
  // workflow state survives.
  useEffect(() => {
    if (mode !== "manual") return;
    if (!resolvedChatId) return;

    const manual = useManualModeStore.getState();
    if (!manual.isActive || manual.chatId !== resolvedChatId) {
      manual.activate(resolvedChatId);

      // Check if a workspace already exists for this chat.
      // If so, skip step 1 & 2 and jump straight to editing.
      void (async () => {
        // Strategy 1: ask backend for the workspace bound to this chat.
        try {
          const ws = await sourceControlClient.getWorkspaceByChat(resolvedChatId);
          if (ws) {
            useManualModeStore.getState().setWorkspace(ws);
            return;
          }
        } catch (err) {
          console.warn("[Manual Mode] getWorkspaceByChat failed:", err);
        }

        // Strategy 2: use the active operation's workspace (if any).
        // When the user clicked "Open Review & Approve" in the chat,
        // the active operation already has a workspace_id.
        const sc = useSourceControlStore.getState();
        const activeWsId = sc.activeOperation?.workspace_id;
        if (activeWsId) {
          try {
            const ws = await sourceControlClient.getWorkspace(activeWsId);
            useManualModeStore.getState().setWorkspace(ws);
          } catch (err) {
            console.warn("[Manual Mode] getWorkspace (active op) failed:", err);
          }
        }
      })();
    }
  }, [mode, resolvedChatId]);

  return (
    <div className="flex flex-col h-full bg-background border-l border-border">
      {/* Mode Toggle */}
      <div className="flex items-center px-4 py-2 border-b border-border bg-card/40 shrink-0">
        <div className="flex rounded-md bg-muted p-0.5" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "agent"}
            onClick={() => setMode("agent")}
            className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
              mode === "agent"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            Agent Mode
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "manual"}
            onClick={() => setMode("manual")}
            className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
              mode === "manual"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            Manual Mode
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "browse"}
            onClick={() => setMode("browse")}
            className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
              mode === "browse"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            Browse
          </button>
        </div>
      </div>

      {/*
        Both panels stay mounted. The `hidden` Tailwind utility sets
        `display: none`, which pauses paint but keeps React state
        and effects intact.
      */}
      <div className="flex-1 overflow-hidden relative">
        <div className={mode === "agent" ? "h-full" : "hidden"}>
          <AgentModePanel />
        </div>
        <div className={mode === "manual" ? "h-full" : "hidden"}>
          <ManualModePanel />
        </div>
        <div className={mode === "browse" ? "h-full" : "hidden"}>
          <BrowsePanel />
        </div>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Browse panel (Repository Browser / History / Branches)
// ----------------------------------------------------------------------

/**
 * Mounted only when the user selects the Browse tab.
 *
 * The wrapper resolves the workspace / repository from the currently
 * attached operation. When no operation is active, it shows a
 * placeholder rather than attempting to guess which workspace to
 * browse; the Agent and Manual flows are the intended paths for
 * attaching an operation.
 */
function BrowsePanel() {
  const activeOperation = useSourceControlStore(
    (s) => s.activeOperation
  );

  if (
    !activeOperation?.workspace_id ||
    !activeOperation?.repository_id
  ) {
    return (
      <div className="flex items-center justify-center h-full p-8 text-center text-muted-foreground">
        <div>
          <h3 className="text-sm font-semibold text-foreground">
            No active workspace
          </h3>
          <p className="text-xs mt-2 max-w-sm leading-relaxed">
            Start a source control operation to browse the
            repository files, history, and branches.
          </p>
        </div>
      </div>
    );
  }

  return (
    <GitBrowserPanel
      workspaceId={activeOperation.workspace_id}
      repositoryId={activeOperation.repository_id}
      provider={activeOperation.provider}
      currentBranch={activeOperation.target_branch || "main"}
    />
  );
}

function deriveChatIdFromUrl(): string | null {
  if (typeof window === "undefined") return null;

  const pathMatch = window.location.pathname.match(
    /\/(?:chats?|c)\/([^/?#]+)/
  );
  if (pathMatch?.[1]) return decodeURIComponent(pathMatch[1]);

  const params = new URLSearchParams(window.location.search);
  return params.get("chat_id") ?? params.get("chat") ?? null;
}

// ----------------------------------------------------------------------
// Agent Mode panel (isolated from Manual Mode)
// ----------------------------------------------------------------------

function AgentModePanel() {
  const { activeOperationId } = useSourceControlStore();
  const [viewMode, setViewMode] = useState<"review" | "timeline">(
    "review"
  );

  const {
    operation,
    diff,
    isLoading,
    isActionLoading,
    approveAndPush,
    rejectOperation,
  } = useOperation(activeOperationId);

  const isStale =
    operation?.current_state === "stale_review_required" ||
    operation?.error_code === "REMOTE_STATE_CHANGED";

  if (!activeOperationId) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-8 text-center text-muted-foreground">
        <GitPullRequest className="w-12 h-12 mb-4 text-muted-foreground/50" />
        <h3 className="text-sm font-semibold text-foreground">
          No Active Operation
        </h3>
        <p className="text-xs mt-2 max-w-sm leading-relaxed">
          When you ask the agent to modify code or create a pull request,
          the review interface will appear here.
        </p>
        <p className="text-[11px] mt-2 text-muted-foreground/70">
          Tip: click "Open Review &amp; Approve" in the chat to attach the
          latest operation.
        </p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <svg
          className="animate-spin h-8 w-8 text-primary"
          viewBox="0 0 24 24"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
            fill="none"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
          />
        </svg>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center px-4 py-2 border-b border-border bg-card/40 shrink-0">
        <div className="flex space-x-1">
          <button
            type="button"
            onClick={() => setViewMode("review")}
            className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              viewMode === "review"
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-muted"
            }`}
          >
            Diff &amp; Review
          </button>
          <button
            type="button"
            onClick={() => setViewMode("timeline")}
            className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              viewMode === "timeline"
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-muted"
            }`}
          >
            Logs / Timeline
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-6">
        {viewMode === "timeline" ? (
          <OperationTimeline events={operation?.events || []} />
        ) : (
          <>
            {isStale && (
              <div className="flex items-start rounded-md bg-red-50 p-3 border border-red-200">
                <AlertCircle className="h-5 w-5 text-red-600 mr-2 flex-shrink-0" />
                <div>
                  <h3 className="text-sm font-medium text-red-800">
                    Remote State Changed
                  </h3>
                  <p className="text-xs text-red-700 mt-1">
                    The remote branch has moved since this review started.
                    You must re-review the latest changes.
                  </p>
                </div>
              </div>
            )}

            {operation && diff && (
              <ApprovalHeader
                operation={operation}
                diff={diff}
                isStale={isStale}
              />
            )}

            {diff && <FileChangeList files={diff.files} />}

            {diff && (
              <div className="rounded-md border border-border bg-card shadow-sm overflow-hidden">
                <div className="border-b border-border bg-muted/50 px-4 py-2">
                  <h3 className="text-sm font-semibold text-foreground">
                    Code Changes
                  </h3>
                </div>
                <DiffViewer
                  diffText={diff.diff_sample}
                  isTruncated={diff.is_truncated}
                />
              </div>
            )}

            <CheckResults summary={null} isLoading={false} />
          </>
        )}
      </div>

      {viewMode === "review" &&
        (operation?.current_state === "review_required" || isStale) && (
          <div className="border-t border-border bg-card p-4 shrink-0">
            <ApprovalDialog
              operationType={operation.operation_type}
              targetBranch={operation.target_branch}
              isActionLoading={isActionLoading}
              isStale={isStale}
              onApprove={approveAndPush}
              onReject={rejectOperation}
            />
          </div>
        )}
    </div>
  );
}
