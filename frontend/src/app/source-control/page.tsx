// frontend/src/app/source-control/page.tsx

"use client";

import React, { Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { useOperation } from "../../features/source-control/hooks/useOperation";
import { ApprovalHeader } from "../../features/source-control/components/ApprovalHeader";
import { FileChangeList } from "../../features/source-control/components/FileChangeList";
import { DiffViewer } from "../../features/source-control/components/DiffViewer";
import { CheckResults } from "../../features/source-control/components/CheckResults";
import { OperationTimeline } from "../../features/source-control/components/OperationTimeline";
import { ApprovalDialog } from "../../features/source-control/components/ApprovalDialog";

/**
 * Inner component that uses `useSearchParams`.
 * Separated to allow the parent page to wrap it in a `<Suspense>` boundary,
 * which is required by Next.js App Router for static rendering.
 */
function SourceControlPageContent() {
  const searchParams = useSearchParams();
  const router = useRouter();

  // Extract operationId from the URL (e.g., /source-control?operation_id=op_123)
  const operationId = searchParams.get("operation_id");

  // Load the operation data and actions
  const {
    operation,
    diff,
    isLoading,
    isActionLoading,
    approveAndPush,
    rejectOperation,
  } = useOperation(operationId);

  // Determine if the review is stale (needs re-review)
  const isStale =
    operation?.current_state === "stale_review_required" ||
    operation?.error_code === "REMOTE_STATE_CHANGED";

  // --- Render States ---

  if (!operationId) {
    return (
      <div className="flex h-screen flex-col items-center justify-center bg-gray-50 text-gray-500">
        <svg className="h-12 w-12 text-gray-400 mb-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <h2 className="text-lg font-medium text-gray-900">No Operation Selected</h2>
        <p className="mt-1 text-sm">Please provide an operation_id in the URL to review.</p>
        <button onClick={() => router.push("/")} className="mt-4 text-blue-600 hover:underline text-sm">
          Return to Home
        </button>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-50">
        <div className="flex flex-col items-center">
          <svg className="h-10 w-10 animate-spin text-blue-600 mb-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          <p className="text-sm text-gray-600">Loading operation details...</p>
        </div>
      </div>
    );
  }

  if (!operation) {
    return (
      <div className="flex h-screen flex-col items-center justify-center bg-gray-50 text-red-500">
        <h2 className="text-lg font-medium">Operation Not Found</h2>
        <p className="mt-1 text-sm text-gray-500">The operation ID provided does not exist or has been removed.</p>
      </div>
    );
  }

  // --- Main Layout ---

  return (
    <div className="min-h-screen bg-gray-100 pb-24">
      {/* Top Navigation Bar */}
      <header className="sticky top-0 z-10 bg-white shadow-sm border-b border-gray-200">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6 lg:px-8">
          <div className="flex items-center space-x-4">
            <button
              onClick={() => router.back()}
              className="text-gray-500 hover:text-gray-700 flex items-center text-sm font-medium"
            >
              <svg className="mr-1 h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
              </svg>
              Back
            </button>
            <span className="text-gray-300">|</span>
            <h1 className="text-lg font-semibold text-gray-900">
              Review Operation: <span className="font-mono text-gray-600">{operation.operation_id.substring(0, 8)}</span>
            </h1>
          </div>
          <div className="flex items-center space-x-2">
             <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                isStale ? "bg-red-100 text-red-800" : "bg-blue-100 text-blue-800"
             }`}>
                {isStale ? "Stale Review" : operation.current_state.replace(/_/g, " ").toUpperCase()}
             </span>
          </div>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <div className="grid grid-cols-1 gap-8 lg:grid-cols-3">

          {/* Left Column (Main Review Area - 2/3 width) */}
          <div className="lg:col-span-2 space-y-6">
            {/* Header with Repo/Branches/Warnings */}
            {diff && <ApprovalHeader operation={operation} diff={diff} isStale={isStale} />}

            {/* File List */}
            {diff && <FileChangeList files={diff.files} />}

            {/* Diff Viewer */}
            {diff && (
              <div className="rounded-lg border border-gray-200 bg-white shadow-sm overflow-hidden">
                <div className="border-b border-gray-200 bg-gray-50 px-4 py-2">
                  <h3 className="text-sm font-semibold text-gray-900">Code Changes</h3>
                </div>
                <DiffViewer
                  diffText={diff.diff_sample}
                  isTruncated={diff.is_truncated}
                />
              </div>
            )}
          </div>

          {/* Right Column (Sidebar - 1/3 width) */}
          <div className="lg:col-span-1 space-y-6">
            {/* Checks */}
            <div>
              <h3 className="text-sm font-semibold text-gray-900 mb-2">Status & Checks</h3>
              <CheckResults summary={null} isLoading={false} />
              {/* Note: We would normally pass the real checks here. For now, it's a placeholder. */}
            </div>

            {/* Timeline */}
            <div className="rounded-lg border border-gray-200 bg-white shadow-sm overflow-hidden">
              <div className="border-b border-gray-200 bg-gray-50 px-4 py-2">
                <h3 className="text-sm font-semibold text-gray-900">Operation Timeline</h3>
              </div>
              <OperationTimeline events={operation.events} />
            </div>
          </div>
        </div>
      </main>

      {/* Sticky Footer for Actions */}
      {operation.current_state === "review_required" || isStale ? (
        <div className="fixed bottom-0 left-0 right-0 z-10 bg-white border-t border-gray-200">
          <div className="mx-auto max-w-7xl px-4 py-4 sm:px-6 lg:px-8">
            <ApprovalDialog
              operationType={operation.operation_type}
              isActionLoading={isActionLoading}
              isStale={isStale}
              onApprove={approveAndPush}
              onReject={rejectOperation}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Main Page Component.
 * Wraps the content in a Suspense boundary to satisfy Next.js App Router requirements for `useSearchParams`.
 */
export default function SourceControlPage() {
  return (
    <Suspense fallback={
      <div className="flex h-screen items-center justify-center bg-gray-50">
        <p className="text-sm text-gray-500">Loading review interface...</p>
      </div>
    }>
      <SourceControlPageContent />
    </Suspense>
  );
}
