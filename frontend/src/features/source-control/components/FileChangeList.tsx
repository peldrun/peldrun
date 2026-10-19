// frontend/src/features/source-control/components/FileChangeList.tsx

import React from "react";
import { FileDiffResponse } from "../api/schemas";
import { formatChangeType } from "../utils/diff-format";

interface FileChangeListProps {
  files: FileDiffResponse[];
  onFileClick?: (path: string) => void;
}

/**
 * A component displaying a list of modified files with their addition/deletion stats.
 * Highlights high-risk files (workflow files, binary files) with visual warning badges.
 */
export function FileChangeList({ files, onFileClick }: FileChangeListProps) {
  if (!files || files.length === 0) {
    return (
      <div className="rounded-md border border-gray-200 bg-gray-50 p-4 text-center text-sm text-gray-500">
        No file changes detected.
      </div>
    );
  }

  /**
   * Returns a color class based on the change type.
   */
  const getChangeTypeColor = (type: string) => {
    switch (type.toLowerCase()) {
      case "added":
        return "bg-green-100 text-green-800 border-green-200";
      case "deleted":
        return "bg-red-100 text-red-800 border-red-200";
      case "renamed":
        return "bg-purple-100 text-purple-800 border-purple-200";
      case "modified":
      default:
        return "bg-gray-100 text-gray-800 border-gray-200";
    }
  };

  return (
    <div className="overflow-hidden rounded-md border border-gray-200 bg-white">
      <div className="border-b border-gray-200 bg-gray-50 px-4 py-2">
        <h3 className="text-sm font-semibold text-gray-900">
          Changed Files ({files.length})
        </h3>
      </div>

      <ul className="divide-y divide-gray-100 max-h-64 overflow-y-auto">
        {files.map((file) => (
          <li
            key={file.path}
            className={`flex items-center justify-between p-2 px-4 text-sm transition-colors ${
              onFileClick ? "cursor-pointer hover:bg-blue-50" : ""
            }`}
            onClick={() => onFileClick?.(file.path)}
          >
            {/* Left Side: File Path & Badges */}
            <div className="flex min-w-0 flex-1 items-center space-x-2 pr-4">
              <span
                className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium border ${getChangeTypeColor(
                  file.change_type
                )}`}
              >
                {formatChangeType(file.change_type)}
              </span>

              <span className="truncate font-mono text-gray-700" title={file.path}>
                {file.path}
              </span>

              {/* Warning Badges */}
              {file.is_workflow_file && (
                <span
                  className="inline-flex items-center rounded border border-orange-200 bg-orange-100 px-1.5 py-0.5 text-xs font-medium text-orange-800"
                  title="Modifying CI/CD workflows requires extra scrutiny."
                >
                  <svg className="mr-1 h-3 w-3" fill="currentColor" viewBox="0 0 20 20">
                    <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
                  </svg>
                  Workflow
                </span>
              )}

              {file.is_binary && (
                <span
                  className="inline-flex items-center rounded border border-gray-300 bg-gray-100 px-1.5 py-0.5 text-xs font-medium text-gray-600"
                  title="Binary files cannot be previewed as text."
                >
                  Binary
                </span>
              )}
            </div>

            {/* Right Side: Stats */}
            <div className="flex items-center space-x-3 whitespace-nowrap font-mono text-xs">
              {file.insertions > 0 && (
                <span className="text-green-600 font-medium">
                  +{file.insertions}
                </span>
              )}
              {file.deletions > 0 && (
                <span className="text-red-600 font-medium">
                  -{file.deletions}
                </span>
              )}
              {file.insertions === 0 && file.deletions === 0 && (
                <span className="text-gray-400">0</span>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
