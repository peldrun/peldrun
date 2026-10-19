// frontend/src/features/source-control/components/ApprovalHeader.tsx

import React from "react";
import { OperationResponse, DiffSummaryResponse } from "../api/schemas";

interface ApprovalHeaderProps {
  operation: OperationResponse;
  diff: DiffSummaryResponse;
  isStale?: boolean;
}

/**
 * A header component for the approval screen.
 * Summarizes the repository, branches, commit SHAs, and surfaces critical security warnings.
 */
export function ApprovalHeader({ operation, diff, isStale = false }: ApprovalHeaderProps) {

  // Check if the target branch is a known protected branch
  const protectedBranches = ["main", "master", "develop", "release", "prod", "production"];
  const isProtectedTarget = protectedBranches.includes(operation.target_branch.toLowerCase());

  return (
    <div className="rounded-md border border-gray-200 bg-white shadow-sm overflow-hidden">
      {/* Context Section */}
      <div className="p-4 bg-gray-50 border-b border-gray-200">
        <div className="flex items-center space-x-2 text-sm text-gray-600 mb-3">
          <svg className="h-5 w-5 text-gray-400" fill="currentColor" viewBox="0 0 24 24">
            <path d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.166 6.839 9.489.5.092.682-.217.682-.482 0-.237-.008-.866-.013-1.7-2.782.604-3.369-1.34-3.369-1.34-.454-1.156-1.11-1.464-1.11-1.464-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.831.092-.646.35-1.086.636-1.336-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0112 6.836c.85.004 1.705.114 2.504.336 1.909-1.294 2.747-1.025 2.747-1.025.546 1.377.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.161 22 16.418 22 12c0-5.523-4.477-10-10-10z" />
          </svg>
          <span className="font-medium text-gray-900">{operation.provider.toUpperCase()}</span>
          <span className="text-gray-300">/</span>
          <span className="font-mono">{operation.repository_id}</span>
        </div>

        {/* Branch Flow */}
        <div className="flex items-center space-x-3 text-sm">
          <div className="flex items-center space-x-1 bg-gray-200 rounded px-2 py-1">
            <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
            </svg>
            <span className="font-mono text-gray-800">{operation.target_branch}</span>
          </div>

          <svg className="h-4 w-4 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M14 5l7 7m0 0l-7 7m7-7H3" />
          </svg>

          <div className="flex items-center space-x-1 bg-blue-100 rounded px-2 py-1 border border-blue-200">
            <svg className="h-4 w-4 text-blue-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" />
            </svg>
            <span className="font-mono text-blue-800">{operation.target_branch}</span>
          </div>
        </div>

        {/* SHAs */}
        <div className="mt-4 grid grid-cols-2 gap-4 text-xs font-mono text-gray-500">
          <div>
            <span className="block font-sans font-medium text-gray-700">Base SHA</span>
            <span title={diff.base_sha}>{diff.base_sha.substring(0, 10)}</span>
          </div>
          <div>
            <span className="block font-sans font-medium text-gray-700">Head SHA</span>
            <span title={diff.head_sha}>{diff.head_sha.substring(0, 10)}</span>
          </div>
        </div>
      </div>

      {/* Warnings Section */}
      <div className="flex flex-col space-y-2 p-4">

        {/* Stale Review Warning (412) */}
        {isStale && (
          <div className="flex items-start rounded-md bg-red-50 p-3 border border-red-200">
            <svg className="h-5 w-5 text-red-600 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
            </svg>
            <div className="ml-3">
              <h3 className="text-sm font-medium text-red-800">Remote State Changed</h3>
              <p className="mt-1 text-xs text-red-700">
                The remote branch has moved since this review started. You must re-review the latest changes before pushing.
              </p>
            </div>
          </div>
        )}

        {/* Protected Branch Warning */}
        {isProtectedTarget && (
          <div className="flex items-start rounded-md bg-yellow-50 p-3 border border-yellow-200">
            <svg className="h-5 w-5 text-yellow-600 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clipRule="evenodd" />
            </svg>
            <div className="ml-3">
              <h3 className="text-sm font-medium text-yellow-800">Protected Branch Target</h3>
              <p className="mt-1 text-xs text-yellow-700">
                You are pushing directly to <strong>{operation.target_branch}</strong>. This is normally restricted. Ensure you have explicit permission to override.
              </p>
            </div>
          </div>
        )}

        {/* Workflow Files Warning */}
        {diff.touches_workflow_files && (
          <div className="flex items-start rounded-md bg-orange-50 p-3 border border-orange-200">
            <svg className="h-5 w-5 text-orange-600 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M2 5a2 2 0 012-2h12a2 2 0 012 2v10a2 2 0 01-2 2H4a2 2 0 01-2-2V5zm3.293 1.293a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 01-1.414-1.414L7.586 10 5.293 7.707a1 1 0 010-1.414zM11 12a1 1 0 100 2h3a1 1 0 100-2h-3z" clipRule="evenodd" />
            </svg>
            <div className="ml-3">
              <h3 className="text-sm font-medium text-orange-800">CI/CD Workflow Files Modified</h3>
              <p className="mt-1 text-xs text-orange-700">
                This change modifies pipeline configuration files:
              </p>
              <ul className="mt-1 list-disc list-inside text-xs text-orange-700 font-mono">
                {diff.workflow_files.map((file) => (
                  <li key={file}>{file}</li>
                ))}
              </ul>
            </div>
          </div>
        )}

        {/* Binary Files Warning */}
        {diff.has_binary_files && (
          <div className="flex items-start rounded-md bg-gray-50 p-3 border border-gray-200">
            <svg className="h-5 w-5 text-gray-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 6a1 1 0 011-1h6a1 1 0 110 2H7a1 1 0 01-1-1zm1 3a1 1 0 100 2h6a1 1 0 100-2H7z" clipRule="evenodd" />
            </svg>
            <div className="ml-3">
              <h3 className="text-sm font-medium text-gray-800">Binary Files Detected</h3>
              <p className="mt-1 text-xs text-gray-600">
                This diff contains binary files (e.g., images or compiled assets) which cannot be previewed as text.
              </p>
            </div>
          </div>
        )}

      </div>
    </div>
  );
}
