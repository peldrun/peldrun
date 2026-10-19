// frontend/src/features/source-control/components/CheckResults.tsx

import React from "react";
import { CheckSummaryResponse } from "../api/schemas";

interface CheckResultsProps {
  summary: CheckSummaryResponse | null;
  isLoading?: boolean;
}

/**
 * A component that displays the aggregated status of CI/CD checks and individual run details.
 * It explicitly warns the user if checks are pending or failing before they proceed with an approval.
 */
export function CheckResults({ summary, isLoading = false }: CheckResultsProps) {

  if (isLoading) {
    return (
      <div className="rounded-md border border-gray-200 bg-white p-4">
        <div className="animate-pulse flex space-x-4">
          <div className="flex-1 space-y-3 py-1">
            <div className="h-4 bg-gray-200 rounded w-1/4"></div>
            <div className="space-y-2">
              <div className="h-3 bg-gray-200 rounded"></div>
              <div className="h-3 bg-gray-200 rounded w-5/6"></div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!summary) {
    return (
      <div className="rounded-md border border-gray-200 bg-gray-50 p-4 text-center text-sm text-gray-500">
        No CI/CD checks have been requested or found for this commit.
      </div>
    );
  }

  /**
   * Returns visual configuration for a specific check run based on its status and conclusion.
   */
  const getRunConfig = (status: string, conclusion: string | null | undefined) => {
    const normalizedStatus = status.toLowerCase();
    const normalizedConclusion = (conclusion || "").toLowerCase();

    if (normalizedStatus === "in_progress" || normalizedStatus === "queued") {
      return {
        icon: (
          <svg className="h-4 w-4 animate-spin text-blue-500" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
        ),
        textClass: "text-blue-700",
        bgClass: "bg-blue-50",
      };
    }

    if (normalizedConclusion === "success") {
      return {
        icon: (
          <svg className="h-4 w-4 text-green-500" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
          </svg>
        ),
        textClass: "text-green-700",
        bgClass: "bg-green-50",
      };
    }

    if (normalizedConclusion === "failure" || normalizedConclusion === "timed_out") {
      return {
        icon: (
          <svg className="h-4 w-4 text-red-500" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
          </svg>
        ),
        textClass: "text-red-700",
        bgClass: "bg-red-50",
      };
    }

    return {
      icon: (
        <svg className="h-4 w-4 text-gray-400" fill="currentColor" viewBox="0 0 20 20">
          <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" />
        </svg>
      ),
      textClass: "text-gray-700",
      bgClass: "bg-gray-50",
    };
  };

  return (
    <div className="overflow-hidden rounded-md border border-gray-200 bg-white">
      {/* Summary Header */}
      <div className="border-b border-gray-200 bg-gray-50 px-4 py-3 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-900 flex items-center">
            Checks Status
            {summary.is_passing ? (
              <span className="ml-2 inline-flex items-center rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800">
                Passing
              </span>
            ) : summary.failure_count > 0 ? (
              <span className="ml-2 inline-flex items-center rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-800">
                Failing
              </span>
            ) : (
              <span className="ml-2 inline-flex items-center rounded-full bg-yellow-100 px-2 py-0.5 text-xs font-medium text-yellow-800">
                Pending
              </span>
            )}
          </h3>
          <p className="mt-1 text-xs text-gray-500">{summary.message}</p>
        </div>

        {/* Counts */}
        <div className="flex items-center space-x-2 text-xs font-medium">
          {summary.success_count > 0 && <span className="text-green-600">{summary.success_count} passed</span>}
          {summary.failure_count > 0 && <span className="text-red-600">{summary.failure_count} failed</span>}
          {summary.pending_count > 0 && <span className="text-yellow-600">{summary.pending_count} pending</span>}
        </div>
      </div>

      {/* Individual Runs */}
      {summary.runs && summary.runs.length > 0 ? (
        <ul className="divide-y divide-gray-100 max-h-48 overflow-y-auto">
          {summary.runs.map((run, index) => {
            const config = getRunConfig(run.status, run.conclusion);

            return (
              <li key={`${run.name}-${index}`} className="flex items-center justify-between px-4 py-2 hover:bg-gray-50">
                <div className="flex items-center space-x-3 min-w-0">
                  <div className="flex-shrink-0">{config.icon}</div>
                  <span className="text-sm text-gray-700 truncate" title={run.name}>
                    {run.name}
                  </span>
                </div>

                <div className="flex items-center space-x-4 flex-shrink-0 ml-4">
                  <span className={`text-xs font-medium capitalize ${config.textClass}`}>
                    {run.conclusion || run.status.replace("_", " ")}
                  </span>
                  {run.web_url && (
                    <a
                      href={run.web_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-blue-600 hover:text-blue-800 hover:underline"
                    >
                      Logs ↗
                    </a>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <div className="p-4 text-center text-xs text-gray-500 italic">
          No individual check runs reported.
        </div>
      )}
    </div>
  );
}
