// frontend/src/features/source-control/components/manual/ManualDiffReview.tsx

import React from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { DiffViewer } from "../DiffViewer";
import { FileChangeList } from "../FileChangeList";
import { AlertCircle, ArrowRight, ChevronLeft } from "lucide-react";

/**
 * Step 4 of Manual Mode: review the diff before approving.
 *
 * Preconditions (guaranteed by the flow):
 *   - step === "reviewing"
 *   - operationId, approvalId, and diff are all set
 *
 * The component does NOT itself request approval — that already
 * happened in the previous step (ManualEditingGuide), which called
 * `inspectAndRequestApproval()` and wrote all three values to the
 * store. See `useManualMode.ts` for details.
 *
 * The only forward action is "Proceed to Approve & Push", which
 * advances `step` to "approving" so ManualPushButton renders.
 * The only backward action is "Back to editing", which clears the
 * approval data and returns to the editing guide.
 */
export function ManualDiffReview() {
  const {
    diff,
    operationId,
    approvalId,
    repository,
    branch,
    approvalScope,
  } = useManualModeStore();

  // Defensive: the flow should never reach here without these
  // values, but if it does (e.g. a stale render after store reset),
  // we show a clear message instead of an infinite spinner.
  if (!diff || !operationId || !approvalId) {
    return (
      <div className="space-y-4">
        <div className="flex items-start rounded-md bg-red-50 p-3 border border-red-200">
          <AlertCircle className="h-4 w-4 text-red-600 mr-2 flex-shrink-0 mt-0.5" />
          <div className="text-xs text-red-700 flex-1">
            <p className="font-medium">Review data is missing.</p>
            <p className="mt-1">
              This usually happens after a page refresh or a workflow reset.
              Please return to the editing step and request approval again.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => useManualModeStore.getState().clearApproval()}
          className="w-full flex items-center justify-center gap-2 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90"
        >
          <ChevronLeft size={16} />
          Back to editing
        </button>
      </div>
    );
  }

  // No changes detected — the backend normally refuses to create an
  // approval ticket when the diff is empty, so this path is only
  // reachable in unusual situations (race condition, manual store
  // mutation). Kept as a safety net.
  if (diff.total_files === 0) {
    return (
      <div className="space-y-4">
        <div>
          <h3 className="text-sm font-semibold text-foreground mb-1">
            No Changes Detected
          </h3>
          <p className="text-xs text-muted-foreground">
            The workspace has no changes relative to its base commit. Edit
            some files in the Files tab, then return here.
          </p>
        </div>
        <button
          type="button"
          onClick={() => useManualModeStore.getState().clearApproval()}
          className="w-full flex items-center justify-center gap-2 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90"
        >
          <ChevronLeft size={16} />
          Back to editing
        </button>
      </div>
    );
  }

  // The schema allows null/undefined here; DiffViewer expects a
  // string. Normalize once so both the disabled check and the
  // rendered text use the same value.
  const diffText = diff.diff_sample ?? "";

  const handleProceed = () => {
    useManualModeStore.getState().setStep("approving");
  };

  const handleBack = () => {
    useManualModeStore.getState().clearApproval();
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">
          Review Your Changes
        </h3>
        <p className="text-xs text-muted-foreground mb-4">
          Approval has been requested. Review the diff below, then approve or
          reject.
        </p>
      </div>

      {/* Protected-branch warning (Task 1.3) — shown ONLY when the
          approval ticket's scope is "push_to_protected". This means
          the target branch matches the protected set (main, master,
          etc.) and the user is being asked to explicitly consent to
          a push that could normally be refused. The warning is
          deliberately prominent and action-oriented so a distracted
          user does not click through it by mistake. */}
      {approvalScope === "push_to_protected" && (
        <div className="rounded-md bg-amber-50 border-2 border-amber-400 p-3">
          <div className="flex items-start gap-2">
            <AlertCircle className="h-5 w-5 text-amber-600 flex-shrink-0 mt-0.5" />
            <div className="text-xs text-amber-900 flex-1">
              <p className="font-semibold">
                You are about to approve a push to a protected branch.
              </p>
              <p className="mt-1">
                The target branch{" "}
                <span className="font-mono font-medium">
                  {branch || "(unknown)"}
                </span>{" "}
                matches the protected pattern set. Approving this
                ticket gives PELDRUN permission to push directly to
                it, bypassing the usual feature-branch workflow.
              </p>
              <p className="mt-1">
                Only proceed if you have explicitly configured this
                via <span className="font-mono">allow_push_to_protected</span>{" "}
                in settings and understand that GitHub's own branch
                protection rules may still reject the push.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Ticket info */}
      <div className="rounded-md bg-blue-50 border border-blue-200 p-3 text-xs space-y-1">
        <div>
          <span className="font-medium text-blue-800">Approval ID:</span>{" "}
          <span className="font-mono break-all">{approvalId}</span>
        </div>
        <div>
          <span className="font-medium text-blue-800">Operation ID:</span>{" "}
          <span className="font-mono break-all">{operationId}</span>
        </div>
        <div>
          <span className="font-medium text-blue-800">Repository:</span>{" "}
          {repository?.full_name ?? "—"}{" "}
          <span className="text-muted-foreground">→</span> {branch || "—"}
        </div>
        <div>
          <span className="font-medium text-blue-800">Diff SHA-256:</span>{" "}
          <span className="font-mono">
            {diff.diff_sha256 ? `${diff.diff_sha256.slice(0, 16)}…` : "—"}
          </span>
        </div>
        <div>
          <span className="font-medium text-blue-800">Files changed:</span>{" "}
          {diff.total_files} (+{diff.insertions} / −{diff.deletions})
        </div>
        {diff.touches_workflow_files && (
          <div className="text-yellow-800 bg-yellow-100 rounded px-2 py-1 mt-1">
            ⚠️ This diff touches workflow files:{" "}
            <span className="font-mono">
              {diff.workflow_files.join(", ")}
            </span>
          </div>
        )}
      </div>

      {/* File list */}
      <div className="rounded-md border border-border overflow-hidden">
        <div className="border-b border-border bg-muted/50 px-3 py-2">
          <h4 className="text-xs font-semibold">
            Files ({diff.files.length})
          </h4>
        </div>
        <FileChangeList files={diff.files} />
      </div>

      {/* Diff viewer */}
      <div className="rounded-md border border-border overflow-hidden">
        <div className="border-b border-border bg-muted/50 px-3 py-2 flex items-center justify-between">
          <h4 className="text-xs font-semibold">Diff Preview</h4>
          {diff.is_truncated && (
            <span className="text-[10px] text-muted-foreground">
              truncated for size
            </span>
          )}
        </div>
        <DiffViewer diffText={diffText} isTruncated={diff.is_truncated} />
      </div>

      {/* Actions */}
      <div className="space-y-2">
        <button
          type="button"
          onClick={handleProceed}
          disabled={!diffText}
          className="w-full flex items-center justify-center gap-2 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Proceed to Approve &amp; Push
          <ArrowRight size={16} />
        </button>

        <button
          type="button"
          onClick={handleBack}
          className="w-full text-xs text-muted-foreground hover:text-foreground"
        >
          ← Back to editing
        </button>
      </div>

      <p className="text-[11px] text-center text-muted-foreground">
        Any change to the workspace after this point will invalidate the
        current approval, and the push will be refused with a stale-review
        error. You can safely return here and request a fresh approval.
      </p>
    </div>
  );
}
