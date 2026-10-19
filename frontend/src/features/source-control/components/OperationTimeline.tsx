// frontend/src/features/source-control/components/OperationTimeline.tsx

import React from "react";
import { OperationEventResponse } from "../api/schemas";

interface OperationTimelineProps {
  events: OperationEventResponse[];
}

/**
 * A vertical timeline component rendering the durable event history of a source control operation.
 * Each event is displayed with a state-specific icon, timestamp, and transition label.
 */
export function OperationTimeline({ events }: OperationTimelineProps) {

  if (!events || events.length === 0) {
    return (
      <div className="p-4 text-center text-sm text-gray-500">
        No events recorded for this operation yet.
      </div>
    );
  }

  /**
   * Returns a visual configuration (icon, colors) based on the target state.
   */
  const getEventConfig = (toState: string) => {
    const normalized = toState.toLowerCase();

    // Success / Completed states
    if (["pushed", "change_request_created", "approved"].includes(normalized)) {
      return {
        icon: "✓",
        bgClass: "bg-green-500",
        textClass: "text-green-700",
        bgLightClass: "bg-green-50",
      };
    }

    // Failure / Rejection states
    if (["rejected", "failed_final", "cancelled"].includes(normalized)) {
      return {
        icon: "✕",
        bgClass: "bg-red-500",
        textClass: "text-red-700",
        bgLightClass: "bg-red-50",
      };
    }

    // Warning / Requires attention states
    if (["stale_review_required"].includes(normalized)) {
      return {
        icon: "!",
        bgClass: "bg-yellow-500",
        textClass: "text-yellow-700",
        bgLightClass: "bg-yellow-50",
      };
    }

    // In-progress / Waiting states
    if (["review_required", "pushing", "checking"].includes(normalized)) {
      return {
        icon: "⏳",
        bgClass: "bg-blue-500",
        textClass: "text-blue-700",
        bgLightClass: "bg-blue-50",
      };
    }

    // Default / Neutral states
    return {
      icon: "•",
      bgClass: "bg-gray-400",
      textClass: "text-gray-700",
      bgLightClass: "bg-gray-50",
    };
  };

  /**
   * Formats a state name for display (e.g., "review_required" -> "Review Required").
   */
  const formatStateName = (state: string) => {
    return state
      .replace(/_/g, " ")
      .replace(/\b\w/g, (char) => char.toUpperCase());
  };

  return (
    <div className="flow-root p-4">
      <ul role="list" className="-mb-8">
        {events.map((event, eventIdx) => {
          const config = getEventConfig(event.to_state);
          const isLast = eventIdx === events.length - 1;

          return (
            <li key={event.event_id}>
              <div className="relative pb-8">
                {/* Vertical connector line (except for the last item) */}
                {!isLast && (
                  <span
                    className="absolute left-4 top-4 -ml-px h-full w-0.5 bg-gray-200"
                    aria-hidden="true"
                  />
                )}

                <div className="relative flex space-x-3">
                  {/* Icon / Dot */}
                  <div>
                    <span
                      className={`h-8 w-8 rounded-full flex items-center justify-center ring-4 ring-white ${config.bgClass} text-white text-sm font-bold`}
                    >
                      {config.icon}
                    </span>
                  </div>

                  {/* Event Content */}
                  <div className="flex min-w-0 flex-1 justify-between space-x-4 pt-1.5">
                    <div>
                      <p className={`text-sm font-medium ${config.textClass}`}>
                        {formatStateName(event.to_state)}
                      </p>
                      {event.from_state && (
                        <p className="text-xs text-gray-500 mt-0.5">
                          from: {formatStateName(event.from_state)}
                        </p>
                      )}
                      {/* Render metadata summary if available */}
                      {event.metadata && Object.keys(event.metadata).length > 0 && (
                        <div className={`mt-2 rounded-md p-2 text-xs ${config.bgLightClass}`}>
                          {Object.entries(event.metadata).map(([key, value]) => (
                            <div key={key} className="flex justify-between">
                              <span className="font-medium text-gray-600">{key}:</span>
                              <span className="text-gray-800 ml-2 truncate">
                                {typeof value === "object"
                                  ? JSON.stringify(value)
                                  : String(value)}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Timestamp */}
                    <div className="whitespace-nowrap text-right text-xs text-gray-500">
                      <time dateTime={event.timestamp}>
                        {new Date(event.timestamp).toLocaleString()}
                      </time>
                    </div>
                  </div>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
