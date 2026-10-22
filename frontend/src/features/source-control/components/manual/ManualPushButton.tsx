// frontend/src/features/source-control/components/manual/ManualPushButton.tsx

import React, { useState } from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { useManualMode } from "../../hooks/useManualMode";
import { AlertCircle, CheckCircle2, XCircle } from "lucide-react";

/**
 * Step 5 of Manual Mode: approve and push (or reject).
 *
 * State ownership — read carefully:
 *
 *   - `step === "approving"` — this component is rendered.
 *   - `step === "pushing"`   — the push call is in flight (set by
 *                              the hook immediately after the user
 *                              clicks Approve). When this happens,
 *                              ManualModePanel replaces this
 *                              component with its own "Pushing…"
 *                              spinner, so we unmount.
 *   - `step === "pushed"`    — push succeeded; the panel shows the
 *                              success screen.
 *   - `step === "editing"`   — the push was refused with 412 (stale
 *                              review) OR the user rejected; the
 *                              panel shows ManualEditingGuide again.
 *   - `step === "error"`     — some other failure. The panel's
 *                              error banner is visible; this
 *                              component is unmounted.
 *
 * Because the component unmounts on every terminal transition, the
 * local `isPushing` state is only a short-lived guard against rapid
 * double-clicks (before React flushes the store update). It is
 * intentionally NOT used for the long-form loading UI — that is the
 * panel's job.
 */
export function ManualPushButton() {
  const { operationId, approvalId, workspaceId, step, error } =
    useManualModeStore();
  const { approveAndPush, rejectApproval } = useManualMode();

  const [isPushing, setIsPushing] = useState(false);
  const [showReject, setShowReject] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [isRejecting, setIsRejecting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const handleApprove = async () => {
    if (isPushing || step === "pushing") return;
    setIsPushing(true);
    setLocalError(null);
    try {
      await approveAndPush();
      // On success, the hook advances step to "pushed" and this
      // component unmounts. On failure, the hook either advances to
      // "editing" (412) or leaves the step at "error"; in both cases
      // the store's `error` field carries the message.
    } finally {
      setIsPushing(false);
    }
  };

  const handleReject = async () => {
    if (isRejecting || rejectReason.trim().length < 5) return;
    setIsRejecting(true);
    setLocalError(null);
    try {
      const ok = await rejectApproval(rejectReason.trim());
      if (ok) {
        // The hook's rejectApproval() already called
        // clearApproval(), which resets operationId / approvalId /
        // diff AND advances step to "editing". No extra setStep
        // call is needed here.
        setShowReject(false);
        setRejectReason("");
      } else {
        setLocalError("Rejection failed. See the error banner above.");
      }
    } finally {
      setIsRejecting(false);
    }
  };

  const handleBackToReview = () => {
    if (isPushing || isRejecting) return;
    useManualModeStore.getState().setStep("reviewing");
  };

  const missingData = !operationId || !approvalId || !workspaceId;
  const approveDisabled =
    missingData || isPushing || isRejecting || step === "pushing";
  const showBusy = isPushing || isRejecting || step === "pushing";

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">
          Approve &amp; Push
        </h3>
        <p className="text-xs text-muted-foreground mb-4">
          You are about to push your changes to the remote repository. This is
          your final confirmation.
        </p>
      </div>

      {/* Local error (complementary to the store-level banner) */}
      {(localError || error) && (
        <div className="flex items-start rounded-md bg-red-50 p-3 border border-red-200">
          <AlertCircle className="h-4 w-4 text-red-600 mr-2 flex-shrink-0 mt-0.5" />
          <p className="text-xs text-red-700 flex-1">
            {localError || error}
          </p>
        </div>
      )}

      {/* Data missing guard */}
      {missingData && (
        <div className="rounded-md bg-yellow-50 border border-yellow-200 p-3 text-xs text-yellow-800">
          This screen is missing approval data. Please return to review.
        </div>
      )}

      {/* Reject input */}
      {showReject && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3">
          <label className="block text-xs font-medium text-red-800 mb-1">
            Reason for rejection (required)
          </label>
          <textarea
            rows={3}
            value={rejectReason}
            onChange={(e) => setRejectReason(e.target.value)}
            disabled={isRejecting}
            className="w-full rounded-md border border-red-300 px-3 py-2 text-xs bg-white disabled:opacity-50"
            placeholder="Explain why you are rejecting these changes…"
          />
          <div className="flex gap-2 mt-2">
            <button
              type="button"
              onClick={handleReject}
              disabled={
                isRejecting || rejectReason.trim().length < 5
              }
              className="rounded-md bg-red-600 text-white px-3 py-1.5 text-xs font-medium hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isRejecting ? "Rejecting…" : "Confirm Rejection"}
            </button>
            <button
              type="button"
              onClick={() => {
                if (isRejecting) return;
                setShowReject(false);
                setRejectReason("");
              }}
              disabled={isRejecting}
              className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Action buttons */}
      {!showReject && (
        <div className="space-y-2">
          <button
            type="button"
            onClick={handleApprove}
            disabled={approveDisabled}
            className="w-full flex items-center justify-center gap-2 rounded-md bg-green-600 text-white px-4 py-3 text-sm font-semibold hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {showBusy ? (
              <>
                <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-r-transparent" />
                Pushing…
              </>
            ) : (
              <>
                <CheckCircle2 size={18} />
                Approve &amp; Push Branch
              </>
            )}
          </button>

          <button
            type="button"
            onClick={() => setShowReject(true)}
            disabled={showBusy || missingData}
            className="w-full flex items-center justify-center gap-2 rounded-md border border-red-300 text-red-700 bg-white px-4 py-2 text-sm font-medium hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <XCircle size={16} />
            Reject Changes
          </button>

          <button
            type="button"
            onClick={handleBackToReview}
            disabled={showBusy}
            className="w-full text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            ← Back to review
          </button>
        </div>
      )}
    </div>
  );
}
