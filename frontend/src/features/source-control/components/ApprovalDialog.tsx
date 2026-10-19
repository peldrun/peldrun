// frontend/src/features/source-control/components/ApprovalDialog.tsx

import React, { useState } from "react";

interface ApprovalDialogProps {
  operationType: string;
  isActionLoading: boolean;
  isStale: boolean;
  onApprove: () => void;
  onReject: (reason: string) => void;
}

/**
 * The final action block for approving or rejecting a remote write.
 * Enforces explicit naming of side effects and requires a reason for rejection.
 */
export function ApprovalDialog({
  operationType,
  isActionLoading,
  isStale,
  onApprove,
  onReject,
}: ApprovalDialogProps) {
  const [showRejectInput, setShowRejectInput] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [rejectError, setRejectError] = useState<string | null>(null);

  // Determine the exact label based on the operation type
  const getApproveLabel = () => {
    if (operationType === "create_change_request") {
      return "Approve & Create Pull Request";
    }
    return "Approve & Push Branch";
  };

  const handleRejectClick = () => {
    if (!showRejectInput) {
      setShowRejectInput(true);
      return;
    }

    // Validate that a reason is provided
    if (rejectReason.trim().length < 5) {
      setRejectError("Please provide a valid reason (at least 5 characters).");
      return;
    }

    setRejectError(null);
    onReject(rejectReason.trim());
  };

  return (
    <div className="border-t border-gray-200 bg-white p-4 shadow-[0_-4px_6px_-1px_rgba(0,0,0,0.05)]">

      {/* Stale Warning Overlay */}
      {isStale && (
        <div className="mb-4 rounded-md bg-red-50 p-3 text-sm text-red-800 border border-red-200 flex items-start">
          <svg className="h-5 w-5 text-red-600 mr-2 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
          </svg>
          <div>
            <strong className="font-semibold">Review Required:</strong> The remote branch has changed. You cannot approve this until you re-review the latest changes.
          </div>
        </div>
      )}

      {/* Rejection Input Area */}
      {showRejectInput && (
        <div className="mb-4 rounded-md bg-gray-50 p-3 border border-gray-200">
          <label htmlFor="reject-reason" className="block text-sm font-medium text-gray-700 mb-1">
            Reason for rejection (required)
          </label>
          <textarea
            id="reject-reason"
            rows={2}
            className="w-full rounded-md border border-gray-300 p-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            placeholder="E.g., The changes to the authentication flow are not secure."
            value={rejectReason}
            onChange={(e) => {
              setRejectReason(e.target.value);
              if (rejectError) setRejectError(null);
            }}
            disabled={isActionLoading}
          />
          {rejectError && <p className="mt-1 text-xs text-red-600">{rejectError}</p>}
        </div>
      )}

      {/* Action Buttons */}
      <div className="flex flex-col space-y-3 sm:flex-row sm:space-y-0 sm:space-x-3 sm:justify-end">

        {/* Keep Local (Optional/Cancel action) */}
        <button
          type="button"
          onClick={() => setShowRejectInput(false)}
          disabled={isActionLoading || (!showRejectInput && isStale)}
          className={`w-full sm:w-auto inline-flex justify-center items-center rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 ${
            showRejectInput ? "hidden sm:inline-flex" : ""
          }`}
        >
          Cancel
        </button>

        {/* Reject Button */}
        <button
          type="button"
          onClick={handleRejectClick}
          disabled={isActionLoading}
          className={`w-full sm:w-auto inline-flex justify-center items-center rounded-md border border-transparent px-4 py-2 text-sm font-medium shadow-sm focus:outline-none focus:ring-2 focus:ring-offset-2 ${
            showRejectInput
              ? "bg-red-600 text-white hover:bg-red-700 focus:ring-red-500"
              : "bg-white text-red-700 border-red-300 hover:bg-red-50 focus:ring-red-500"
          } disabled:opacity-50`}
        >
          {isActionLoading && showRejectInput ? (
            <svg className="mr-2 h-4 w-4 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
          ) : null}
          {showRejectInput ? "Confirm Rejection" : "Reject"}
        </button>

        {/* Approve Button */}
        <button
          type="button"
          onClick={onApprove}
          disabled={isActionLoading || isStale}
          className="w-full sm:w-auto inline-flex justify-center items-center rounded-md border border-transparent bg-blue-600 px-6 py-2 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isActionLoading && !showRejectInput ? (
            <>
              <svg className="mr-2 h-4 w-4 animate-spin text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              Processing...
            </>
          ) : (
            getApproveLabel()
          )}
        </button>
      </div>
    </div>
  );
}
