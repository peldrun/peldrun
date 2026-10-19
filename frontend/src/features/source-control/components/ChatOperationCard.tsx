// frontend/src/features/source-control/components/ChatOperationCard.tsx

import React from "react";
import { OperationResponse } from "../api/schemas";

interface ChatOperationCardProps {
  operation: OperationResponse;
  onOpenReview: (operationId: string) => void;
}

/**
 * A compact card rendered inside the chat stream when the agent
 * interacts with source control operations.
 * Acts as a deep-link to open the Right Panel in Review or Timeline mode.
 */
export function ChatOperationCard({
  operation,
  onOpenReview,
}: ChatOperationCardProps) {

  // Determine UI elements based on the operation state
  const getStatusConfig = () => {
    switch (operation.current_state) {
      case "review_required":
        return {
          icon: "⏳",
          title: "Waiting for your review",
          description: `The agent has prepared changes for ${operation.repository_id}. Please review before pushing.`,
          buttonText: "Open Review & Approve",
          colorClass: "border-yellow-200 bg-yellow-50",
          buttonClass: "bg-yellow-600 hover:bg-yellow-700 text-white",
        };
      case "approved":
      case "pushing":
        return {
          icon: "🚀",
          title: "Pushing changes...",
          description: "Applying your approval and pushing to remote.",
          buttonText: "View Progress",
          colorClass: "border-blue-200 bg-blue-50",
          buttonClass: "bg-blue-600 hover:bg-blue-700 text-white",
        };
      case "pushed":
        return {
          icon: "✅",
          title: "Push successful",
          description: "Changes have been pushed to the remote branch.",
          buttonText: "View Details",
          colorClass: "border-green-200 bg-green-50",
          buttonClass: "bg-green-600 hover:bg-green-700 text-white",
        };
      case "change_request_created":
        return {
          icon: "🎉",
          title: "Pull Request created",
          description: "The remote change request is open.",
          buttonText: "View Operation",
          colorClass: "border-green-200 bg-green-50",
          buttonClass: "bg-green-600 hover:bg-green-700 text-white",
        };
      case "rejected":
        return {
          icon: "❌",
          title: "Operation rejected",
          description: "You rejected this change. No remote action was taken.",
          buttonText: "View Details",
          colorClass: "border-red-200 bg-red-50",
          buttonClass: "bg-red-600 hover:bg-red-700 text-white",
        };
      case "failed_final":
        return {
          icon: "⚠️",
          title: "Operation failed",
          description: "An error occurred during execution.",
          buttonText: "View Error",
          colorClass: "border-red-200 bg-red-50",
          buttonClass: "bg-red-600 hover:bg-red-700 text-white",
        };
      default:
        return {
          icon: "ℹ️",
          title: "Source Control Operation",
          description: `Operation type: ${operation.operation_type}`,
          buttonText: "View Details",
          colorClass: "border-gray-200 bg-gray-50",
          buttonClass: "bg-gray-600 hover:bg-gray-700 text-white",
        };
    }
  };

  const config = getStatusConfig();

  return (
    <div className={`my-4 max-w-md rounded-lg border p-4 shadow-sm ${config.colorClass}`}>
      <div className="flex items-start">
        <div className="text-2xl">{config.icon}</div>
        <div className="ml-3 flex-1">
          <h4 className="text-sm font-semibold text-gray-900">
            {config.title}
          </h4>
          <p className="mt-1 text-sm text-gray-600">
            {config.description}
          </p>

          <div className="mt-4 flex items-center justify-between">
            <span className="text-xs text-gray-500 font-mono" title={operation.operation_id}>
              ID: {operation.operation_id.substring(0, 8)}...
            </span>
            <button
              type="button"
              onClick={() => onOpenReview(operation.operation_id)}
              className={`inline-flex items-center rounded-md px-3 py-1.5 text-sm font-medium shadow-sm focus:outline-none focus:ring-2 focus:ring-offset-2 ${config.buttonClass}`}
            >
              {config.buttonText}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
