// frontend/src/features/source-control/components/SourceControlPanel.tsx

import React, { useState } from "react";
import { useSourceControlStore } from "../state/source-control-store";
import { useOperation } from "../hooks/useOperation";
import { ApprovalHeader } from "./ApprovalHeader";
import { FileChangeList } from "./FileChangeList";
import { DiffViewer } from "./DiffViewer";
import { CheckResults } from "./CheckResults";
import { ApprovalDialog } from "./ApprovalDialog";
import { OperationTimeline } from "./OperationTimeline";
import { GitPullRequest, AlertCircle } from "lucide-react";

/**
 * The main container for the Source Control tab inside the Workspace Panel.
 * It manages internal tabs for "Review" (Diff) and "Timeline" (Logs).
 */
export function SourceControlPanel() {
  const { activeOperationId } = useSourceControlStore();
  const [viewMode, setViewMode] = useState<"review" | "timeline">("review");

  const {
    operation,
    diff,
    isLoading,
    isActionLoading,
    approveAndPush,
    rejectOperation,
  } = useOperation(activeOperationId);

  const isStale = operation?.current_state === "stale_review_required" || operation?.error_code === "REMOTE_STATE_CHANGED";

  if (!activeOperationId) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-8 text-center text-muted-foreground bg-background">
        <GitPullRequest className="w-12 h-12 mb-4 text-muted-foreground/50" />
        <h3 className="text-sm font-semibold text-foreground">No Active Operation</h3>
        <p className="text-xs mt-2 max-w-sm leading-relaxed">
          When you ask the agent to modify code or create a pull request, the review interface will appear here.
        </p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full bg-background">
        <svg className="animate-spin h-8 w-8 text-primary" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
        </svg>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-background border-l border-border">
      {/* Internal Tabs (Review vs Timeline) */}
      <div className="flex items-center px-4 py-2 border-b border-border bg-card/40 shrink-0">
        <div className="flex space-x-1">
          <button
            onClick={() => setViewMode("review")}
            className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              viewMode === "review" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"
            }`}
          >
            Diff & Review
          </button>
          <button
            onClick={() => setViewMode("timeline")}
            className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              viewMode === "timeline" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"
            }`}
          >
            Logs / Timeline
          </button>
        </div>
      </div>

      {/* Scrollable Content Area */}
      <div className="flex-1 overflow-y-auto p-4 space-y-6">
        {viewMode === "timeline" ? (
          <OperationTimeline events={operation?.events || []} />
        ) : (
          <>
            {isStale && (
              <div className="flex items-start rounded-md bg-red-50 p-3 border border-red-200">
                <AlertCircle className="h-5 w-5 text-red-600 mr-2 flex-shrink-0" />
                <div>
                  <h3 className="text-sm font-medium text-red-800">Remote State Changed</h3>
                  <p className="text-xs text-red-700 mt-1">
                    The remote branch has moved since this review started. You must re-review the latest changes.
                  </p>
                </div>
              </div>
            )}

            {operation && diff && (
              <ApprovalHeader operation={operation} diff={diff} isStale={isStale} />
            )}

            {diff && (
              <FileChangeList files={diff.files} />
            )}

            {diff && (
              <div className="rounded-md border border-border bg-card shadow-sm overflow-hidden">
                <div className="border-b border-border bg-muted/50 px-4 py-2">
                  <h3 className="text-sm font-semibold text-foreground">Code Changes</h3>
                </div>
                <DiffViewer diffText={diff.diff_sample} isTruncated={diff.is_truncated} />
              </div>
            )}

            <CheckResults summary={null} isLoading={false} />
          </>
        )}
      </div>

      {/* Sticky Footer for Approvals (Only in Review Mode) */}
      {viewMode === "review" && (operation?.current_state === "review_required" || isStale) && (
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
