"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { Step } from "@/lib/types";

export function useJobStream(jobId: string | null) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const lastSequenceRef = useRef<number>(0);

  const stopStream = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    setIsStreaming(false);
  }, []);

  useEffect(() => {
    if (!jobId) {
      setSteps([]);
      setIsStreaming(false);
      lastSequenceRef.current = 0;
      return;
    }

    stopStream();
    setSteps([]);
    setError(null);
    setIsStreaming(true);

    // Connect with after_sequence support to enable crash recovery and deduplication
    const sseUrl = `/api/run/jobs/${encodeURIComponent(jobId)}/stream${
      lastSequenceRef.current > 0 ? `?after_sequence=${lastSequenceRef.current}` : ""
    }`;

    const es = new EventSource(sseUrl);
    eventSourceRef.current = es;

    es.onmessage = (event) => {
      try {
        if (!event.data) return;
        const payload = JSON.parse(event.data);

        if (payload.type === "ping") return;

        // Monotonic sequence tracking
        const currentSeq = payload.seq || payload.sequence || payload.data?.seq;
        if (typeof currentSeq === "number" && currentSeq > 0) {
          lastSequenceRef.current = Math.max(lastSequenceRef.current, currentSeq);
        }

        let contentStr = "";
        if (typeof payload.data === "string") {
          contentStr = payload.data;
        } else if (payload.data && typeof payload.data === "object") {
          contentStr =
            payload.data.thought ||
            payload.data.content ||
            payload.data.result ||
            payload.data.output ||
            JSON.stringify(payload.data, null, 2);
        } else {
          contentStr = JSON.stringify(payload, null, 2);
        }

        const stepType = (payload.type || "agent_step").toUpperCase();

        const newStep: Step = {
          id: payload.id || `step_${Date.now()}_${Math.random().toString(36).substring(2, 6)}`,
          step_number: payload.step || steps.length + 1,
          type: stepType,
          content: contentStr,
          timestamp: new Date().toISOString(),
          tool_name: payload.toolName || payload.data?.tool_name || payload.data?.tool,
          tool_args: payload.data?.arguments,
          attempt: payload.data?.attempt,
        };

        setSteps((prev) => {
          // Avoid duplicate steps with matching IDs
          if (prev.some((s) => s.id === newStep.id)) {
            return prev;
          }
          return [...prev, newStep];
        });

        // Close stream upon reaching any terminal lifecycle state
        const terminalStates = ["final", "complete", "completed", "done", "error", "cancelled"];
        if (
          terminalStates.includes(String(payload.type).toLowerCase()) ||
          terminalStates.includes(String(payload.status).toLowerCase())
        ) {
          stopStream();
        }
      } catch {
        setSteps((prev) => [
          ...prev,
          {
            id: `step_${Date.now()}`,
            step_number: prev.length + 1,
            type: "LOG",
            content: event.data,
            timestamp: new Date().toISOString(),
          },
        ]);
      }
    };

    es.onerror = () => {
      stopStream();
    };

    return () => {
      stopStream();
    };
  }, [jobId, stopStream]);

  return { steps, isStreaming, error, stopStream };
}