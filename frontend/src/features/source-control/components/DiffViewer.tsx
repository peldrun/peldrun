// frontend/src/features/source-control/components/DiffViewer.tsx

import React, { useMemo } from "react";
import { parseUnifiedDiff, ParsedDiffLine } from "../utils/diff-format";

interface DiffViewerProps {
  diffText: string | null | undefined;
  isTruncated?: boolean;
  fileName?: string; // Optional: used when displaying a single file's diff
}

/**
 * A component that renders a unified diff string with syntax highlighting,
 * line numbers, and horizontal scrolling.
 * Mimics the standard diff viewing experience found in modern Git providers.
 */
export function DiffViewer({ diffText, isTruncated = false, fileName }: DiffViewerProps) {

  // Parse the raw diff string into structured lines (memoized for performance)
  const parsedLines = useMemo(() => {
    if (!diffText) return [];
    return parseUnifiedDiff(diffText);
  }, [diffText]);

  if (!diffText || parsedLines.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center rounded-md border border-gray-200 bg-gray-50 text-sm text-gray-500">
        No diff content available to display.
      </div>
    );
  }

  /**
   * Determines the row background color based on the line type.
   */
  const getRowClass = (type: ParsedDiffLine["type"]) => {
    switch (type) {
      case "addition":
        return "bg-green-50";
      case "deletion":
        return "bg-red-50";
      case "hunk":
        return "bg-blue-50 border-y border-blue-100";
      case "header":
        return "bg-gray-100";
      default:
        return "bg-white";
    }
  };

  /**
   * Determines the content text color and prefix based on the line type.
   */
  const getContentConfig = (type: ParsedDiffLine["type"]) => {
    switch (type) {
      case "addition":
        return { textClass: "text-green-800", prefix: "+" };
      case "deletion":
        return { textClass: "text-red-800", prefix: "-" };
      case "hunk":
        return { textClass: "text-blue-700 font-medium", prefix: "" };
      case "header":
        return { textClass: "text-gray-500 font-semibold", prefix: "" };
      default:
        return { textClass: "text-gray-800", prefix: " " };
    }
  };

  return (
    <div className="overflow-hidden rounded-md border border-gray-200 bg-white shadow-sm">
      {/* Optional File Header (if viewing a single file) */}
      {fileName && (
        <div className="border-b border-gray-200 bg-gray-50 px-4 py-2 text-sm font-medium text-gray-700">
          {fileName}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left font-mono text-xs leading-5">
          <tbody>
            {parsedLines.map((line, index) => {
              const rowClass = getRowClass(line.type);
              const contentConfig = getContentConfig(line.type);

              return (
                <tr key={index} className={`${rowClass} hover:bg-opacity-70`}>
                  {/* Old Line Number */}
                  <td className="w-10 min-w-[2.5rem] select-none border-r border-gray-200 bg-gray-50 px-2 text-right text-gray-400">
                    {line.oldLineNumber || ""}
                  </td>

                  {/* New Line Number */}
                  <td className="w-10 min-w-[2.5rem] select-none border-r border-gray-200 bg-gray-50 px-2 text-right text-gray-400">
                    {line.newLineNumber || ""}
                  </td>

                  {/* Line Content */}
                  <td className={`whitespace-pre px-3 py-0.5 ${contentConfig.textClass}`}>
                    {contentConfig.prefix && (
                      <span className="mr-1 inline-block w-3 select-none opacity-50">
                        {contentConfig.prefix}
                      </span>
                    )}
                    {line.content}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Truncation Warning */}
      {isTruncated && (
        <div className="border-t border-yellow-200 bg-yellow-50 px-4 py-2 text-xs text-yellow-800">
          <span className="font-semibold">Note:</span> This diff has been truncated due to its size.
          Not all changes are displayed here. Please review the full changes locally.
        </div>
      )}
    </div>
  );
}
