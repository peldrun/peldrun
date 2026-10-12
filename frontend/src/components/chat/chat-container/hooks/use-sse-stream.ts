// Path: frontend/src/components/chat/chat-container/hooks/use-sse-stream.ts
"use client";

import { useCallback, useEffect, useRef } from "react";
import type { StepEvent } from "@/components/chat/chat-timeline";
import { getFileCategory } from "@/components/workspace/workspace-panel";
import {
  DEBUG_SSE,
  HUMAN_TOOL_NAMES,
  SSE_EVENT_TYPES,
  DELEGATION_EVENT_TYPES,
} from "../constants";
import { safeRender, extractHumanPrompt } from "../utils";
import type {
  ChildRunState,
  DelegationApprovalRequest,
  ChatStatus,
  TokenUsage,
  UsageMetrics,
} from "../types";

interface UseSseStreamParams {
  /** Ref tracking the current step number to avoid stale closures inside handlers. */
  currentStepNumRef: React.MutableRefObject<number>;
  /** Ref tracking the active chat id to avoid stale closures inside handlers. */
  activeChatIdRef: React.MutableRefObject<string | null>;
  setSteps: React.Dispatch<React.SetStateAction<StepEvent[]>>;
  setCurrentStepNum: (n: number) => void;
  setActiveModelName: (m: string) => void;
  setTokensUsed: React.Dispatch<React.SetStateAction<TokenUsage>>;
  setCurrentUsageMetrics: (m: UsageMetrics | null) => void;
  setFinalResult: (r: string | null) => void;
  setStatus: (s: ChatStatus) => void;
  setExpandedSteps: React.Dispatch<React.SetStateAction<Record<string, boolean>>>;
  setHumanQuery: (q: string | null) => void;
  setShowRightPanel: (v: boolean) => void;
  setSelectedFileForEditor: (f: string | null) => void;
  setActiveChildRuns: React.Dispatch<
    React.SetStateAction<Record<string, ChildRunState>>
  >;
  setPendingApprovals: React.Dispatch<
    React.SetStateAction<Record<string, DelegationApprovalRequest>>
  >;
  fetchJobFiles: (jobId: string) => void;
}

/**
 * Encapsulates the entire SSE pipeline for a single job stream.
 *
 * Responsibilities:
 *   - Open / close the EventSource.
 *   - Parse JSON payloads with a safe fallback.
 *   - Route each event type into the appropriate state setters.
 *   - Emit `peldrun:log-entry` / `omweb:chat-status-changed` window events
 *     so neighbouring widgets (log panel, sidebar) stay in sync.
 *
 * Design note:
 *   The incoming `params` object is created inline on every render of the
 *   parent component, which means its identity changes constantly. If we
 *   placed it directly in the `useCallback` dependency array, `connectStream`
 *   would be recreated on every render, causing downstream `useEffect`s to
 *   re-run in a tight loop (the "polling storm" observed in the access logs).
 *   To prevent this, `params` is stored in a ref and `connectStream` is given
 *   an empty dependency array so its identity remains stable for the entire
 *   lifetime of the component.
 */
export function useSseStream(params: UseSseStreamParams) {
  const eventSourceRef = useRef<EventSource | null>(null);

  /**
   * Mirror of the latest `params` object. Handlers read from this ref so they
   * always observe the freshest state setters without forcing the callback
   * itself to change identity.
   */
  const paramsRef = useRef(params);
  useEffect(() => {
    paramsRef.current = params;
  }, [params]);

  const closeStream = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
  }, []);

  const connectStream = useCallback((jobId: string) => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }
    const es = new EventSource(`/api/run/jobs/${jobId}/stream`);
    eventSourceRef.current = es;

    /** Tracks whether we deliberately closed the stream to suppress onerror work. */
    let intentionallyClosed = false;

    /** Convenience accessor for the freshest params snapshot. */
    const p = () => paramsRef.current;

    /** Append a visible step to the timeline, dropping empty entries. */
    const appendStep = (
      type: StepEvent["type"],
      content: any,
      stepNum = 1,
      toolName?: string
    ) => {
      const cleanContent = safeRender(content);
      const cleanTool = toolName ? safeRender(toolName) : undefined;
      if (type === "tool_call" && !cleanContent && !cleanTool) return;
      if (!cleanContent && !cleanTool) return;

      p().setSteps((prev) => [
        ...prev,
        {
          id: `${Date.now()}-${Math.random()}`,
          step: stepNum,
          type,
          content: cleanContent,
          toolName: cleanTool,
          timestamp: new Date().toLocaleTimeString(),
        },
      ]);
    };

    /** Dispatch a structured log entry for the live log panel. */
    const logEntry = (line: string) => {
      if (typeof window === "undefined") return;
      window.dispatchEvent(
        new CustomEvent("peldrun:log-entry", { detail: { line } })
      );
    };

    /** Central event router keyed by the SSE event name. */
    const handleEventPayload = (eventType: string, payload: any) => {
      const ctx = p();

      // Normalize aliases so both "tool_call" and "tool_called" trigger the same logic.
      const normalizedType =
        eventType === "tool_called"
          ? "tool_call"
          : eventType === "tool_retry"
          ? "tool_call"
          : eventType;

      if (DEBUG_SSE) {
        console.log(`[SSE] ▶ ${eventType}`, {
          step: payload?.step,
          tool: payload?.data?.tool_name || payload?.data?.name,
          argsType: typeof payload?.data?.arguments,
          argsPreview:
            typeof payload?.data?.arguments === "string"
              ? payload.data.arguments.slice(0, 200)
              : JSON.stringify(payload?.data?.arguments || {}).slice(0, 200),
        });
      }

      const step =
        payload.step ||
        payload.data?.step ||
        ctx.currentStepNumRef.current ||
        1;
      ctx.setCurrentStepNum(step);
      ctx.currentStepNumRef.current = step;

      if (payload.data?.model) {
        ctx.setActiveModelName(payload.data.model);
      }

      const logTimestamp = new Date().toLocaleTimeString();

      if (eventType === "step_start") {
        logEntry(`[${logTimestamp}] [STEP] Step ${step} started`);
      } else if (eventType === "thought") {
        const raw =
          payload.data?.thought ?? payload.data?.content ?? payload.data;
        if (safeRender(raw).trim() !== "") {
          appendStep("thought", raw, step);
          ctx.setExpandedSteps((prev) => ({
            ...prev,
            [`active-${step}`]: true,
          }));
          logEntry(`[${logTimestamp}] [THOUGHT] ${safeRender(raw)}`);
        }
        if (payload.data?.tokens) ctx.setTokensUsed(payload.data.tokens);
      } else if (normalizedType === "tool_call") {
        const name =
          payload.data?.tool_name ||
          payload.data?.name ||
          payload.data?.toolName ||
          payload.data?.tool ||
          "";
        const rawArgs = payload.data?.arguments ?? payload.data?.args ?? "";

        // Robust extraction that handles object / JSON-string / free-string / empty.
        const { promptText, parsedArgs } = extractHumanPrompt(rawArgs);

        const isHumanTool = HUMAN_TOOL_NAMES.has(name);
        const isHumanLike =
          isHumanTool ||
          (promptText &&
            (promptText.includes("?") || promptText.includes("prefer")));

        if (isHumanTool && !promptText) {
          console.warn(
            "[ASK_HUMAN] Human tool detected but no prompt text extracted.",
            { name, rawArgs }
          );
        }

        if (isHumanLike && promptText) {
          ctx.setHumanQuery(promptText);
        } else if (isHumanTool) {
          // Even without extractable prompt, mark the query so the card renders.
          ctx.setHumanQuery(
            "The agent is awaiting your input. Please respond below."
          );
        }

        // Store the parsed object when available so chat-thread can re-hydrate
        // input_type and options without needing to re-parse.
        appendStep("tool_call", parsedArgs || rawArgs, step, name);
        ctx.setExpandedSteps((prev) => ({ ...prev, [`active-${step}`]: true }));

        logEntry(
          `[${logTimestamp}] [TOOL_CALL] ${name || "tool"}(${
            typeof rawArgs === "string" ? rawArgs : JSON.stringify(rawArgs)
          })`
        );
      } else if (eventType === "ask_human") {
        const promptText =
          payload.data?.prompt ||
          payload.data?.question ||
          payload.data?.query ||
          payload.data?.message ||
          "";
        if (promptText) {
          ctx.setHumanQuery(promptText);
        } else {
          ctx.setHumanQuery(
            "The agent is awaiting your input. Please respond below."
          );
        }
        appendStep("tool_call", payload.data, step, "ask_human");
        ctx.setExpandedSteps((prev) => ({ ...prev, [`active-${step}`]: true }));
      } else if (eventType === "observation") {
        const raw = payload.data?.output ?? "Execution completed.";
        const observationTool =
          payload.data?.tool_name ||
          payload.data?.name ||
          payload.data?.tool ||
          "";

        /**
         * Only clear the pending human query when the observation comes from
         * a NON-human tool. Observations emitted by ask_human/human_input
         * themselves represent the tool's own result and must NOT wipe the
         * awaiting prompt from the UI, otherwise the interaction card would
         * never render.
         */
        if (!HUMAN_TOOL_NAMES.has(observationTool)) {
          ctx.setHumanQuery(null);
        }

        appendStep("observation", raw, step, observationTool);
        logEntry(`[${logTimestamp}] [OBSERVATION] ${safeRender(raw)}`);

        const artName =
          payload.data?.artifact ||
          payload.data?.path ||
          payload.data?.file ||
          "";
        if (payload.data?.event === "artifact_created" || artName) {
          ctx.setShowRightPanel(true);
          if (artName) {
            const cat = getFileCategory(artName);
            if (cat === "preview" || cat === "artifacts") {
              ctx.setSelectedFileForEditor(artName);
            }
            if (typeof window !== "undefined") {
              window.dispatchEvent(
                new CustomEvent("peldrun:artifact-created", {
                  detail: { ...payload.data, file: artName },
                })
              );
            }
          }
        }
        ctx.fetchJobFiles(jobId);
      } else if (eventType === "artifact_created" || eventType === "artifact") {
        const artName =
          payload.data?.artifact ||
          payload.data?.path ||
          payload.data?.file ||
          "";
        if (artName) {
          ctx.setShowRightPanel(true);
          const cat = getFileCategory(artName);
          if (cat === "preview" || cat === "artifacts") {
            ctx.setSelectedFileForEditor(artName);
          }
          if (typeof window !== "undefined") {
            window.dispatchEvent(
              new CustomEvent("peldrun:artifact-created", {
                detail: { ...payload.data, file: artName },
              })
            );
          }
        }
        ctx.fetchJobFiles(jobId);
      } else if (eventType === "final" || eventType === "done") {
        /**
         * Do not unconditionally clear humanQuery here. If the final event
         * was produced because the ask_human tool timed out, we keep the
         * question visible so the user can see what was requested.
         */
        const lastTool = payload.data?.tool_name || payload.data?.last_tool || "";
        const finalError = String(
          payload.data?.error || payload.data?.message || ""
        );
        const isHumanTimeout =
          HUMAN_TOOL_NAMES.has(lastTool) ||
          finalError.includes("ask_human") ||
          finalError.includes("human_input");

        if (!isHumanTimeout) {
          ctx.setHumanQuery(null);
        }

        const resText =
          payload.data?.result ??
          payload.data?.content ??
          "Task completed successfully.";
        if (eventType === "final") ctx.setFinalResult(safeRender(resText));

        if (typeof window !== "undefined") {
          logEntry(`[${logTimestamp}] [FINAL] ${safeRender(resText)}`);
          window.dispatchEvent(new CustomEvent("peldrun:run-completed"));
        }

        if (payload.data?.usage_summary) {
          ctx.setTokensUsed({
            input: payload.data.usage_summary.input_tokens || 0,
            output: payload.data.usage_summary.output_tokens || 0,
            total: payload.data.usage_summary.total_tokens || 0,
          });
          ctx.setCurrentUsageMetrics({
            cost_usd: payload.data.usage_summary.total_cost_usd,
            latency_ms: payload.data.usage_summary.latency_ms,
            tokens_per_second: payload.data.usage_summary.tokens_per_second,
          });
        } else if (payload.data?.usage) {
          const u = payload.data.usage;
          const inTok = u.prompt_tokens ?? u.input_tokens ?? 0;
          const outTok = u.completion_tokens ?? u.output_tokens ?? 0;
          const totTok = u.total_tokens ?? inTok + outTok;
          ctx.setTokensUsed((prev) => ({
            input: inTok > 0 ? inTok : prev.input,
            output: outTok > 0 ? outTok : prev.output,
            total:
              totTok > 0
                ? totTok
                : inTok + outTok > 0
                ? inTok + outTok
                : prev.total,
          }));
          ctx.setCurrentUsageMetrics({
            cost_usd: u.cost_usd,
            latency_ms: u.latency_ms,
            tokens_per_second: u.tokens_per_second,
          });
        }

        ctx.setStatus("completed");
        ctx.setExpandedSteps({});
        ctx.fetchJobFiles(jobId);
        intentionallyClosed = true;
        es.close();

        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("omweb:chat-status-changed", {
              detail: {
                chatId: ctx.activeChatIdRef.current,
                jobId,
                status: "completed",
              },
            })
          );
          window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        }
      } else if (eventType === "error") {
        ctx.setHumanQuery(null);
        const errText = payload.data?.message ?? "Execution error encountered.";
        ctx.setFinalResult(safeRender(errText));
        ctx.setStatus("failed");
        if (typeof window !== "undefined") {
          logEntry(`[${logTimestamp}] [ERROR] ${safeRender(errText)}`);
          window.dispatchEvent(new CustomEvent("peldrun:run-completed"));
        }
        ctx.fetchJobFiles(jobId);
        intentionallyClosed = true;
        es.close();

        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("omweb:chat-status-changed", {
              detail: {
                chatId: ctx.activeChatIdRef.current,
                jobId,
                status: "failed",
              },
            })
          );
          window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        }
      }
    };

    /** Bind a single named SSE event and wire it to the router. */
    const bindEvt = (name: string) => {
      es.addEventListener(name, (e: any) => {
        try {
          handleEventPayload(name, JSON.parse(e.data));
        } catch (parseErr) {
          if (DEBUG_SSE) {
            console.warn(
              `[SSE] Failed to parse JSON for ${name}`,
              parseErr,
              e.data
            );
          }
          handleEventPayload(name, { data: e.data });
        }
      });
    };

    (SSE_EVENT_TYPES as readonly string[]).forEach(bindEvt);

    DELEGATION_EVENT_TYPES.forEach((name) => {
      es.addEventListener(name, (e: any) => {
        try {
          const payload = JSON.parse(e.data);
          const ctx = p();

          if (name === "approval_required") {
            ctx.setPendingApprovals((prev) => ({
              ...prev,
              [payload.data.approval_request_id]: {
                approvalRequestId: payload.data.approval_request_id,
                taskId: payload.data.task_id,
                parentRunId: payload.data.parent_run_id,
                targetAgentId: payload.data.target_agent_id,
                instruction: payload.data.instruction,
                status: "pending",
                timeoutSeconds: payload.data.timeout_seconds || 300,
                createdAt: Date.now(),
              },
            }));
          } else if (name === "approval_resolved") {
            ctx.setPendingApprovals((prev) => {
              const next = { ...prev };
              delete next[payload.data.approval_request_id];
              return next;
            });
          } else if (name === "child_run_started") {
            ctx.setActiveChildRuns((prev) => ({
              ...prev,
              [payload.data.child_run_id]: {
                childRunId: payload.data.child_run_id,
                parentRunId: payload.data.parent_run_id,
                taskId: payload.data.task_id,
                targetAgentId: payload.data.target_agent_id,
                instruction: payload.data.instruction,
                status: "running",
                startedAt: Date.now(),
                steps: [],
                deliverables: [],
              },
            }));
          } else if (
            name === "child_run_completed" ||
            name === "child_run_failed" ||
            name === "child_run_cancelled"
          ) {
            const cStatus =
              name === "child_run_completed"
                ? "completed"
                : name === "child_run_cancelled"
                ? "cancelled"
                : "failed";
            ctx.setActiveChildRuns((prev) => {
              const cur = prev[payload.data.child_run_id];
              if (!cur) return prev;
              return {
                ...prev,
                [payload.data.child_run_id]: {
                  ...cur,
                  status: cStatus,
                  deliverables:
                    payload.data.deliverables || cur.deliverables,
                  tokensUsed: payload.data.tokens_used,
                  durationSeconds: payload.data.duration_seconds,
                  error: payload.data.error,
                },
              };
            });
          }
        } catch (parseErr) {
          console.error("Failed to parse SSE payload for", name, parseErr);
        }
      });
    });

    /**
     * Handle transport errors. The EventSource is closed to prevent the
     * browser's automatic reconnect from spamming the backend. A simple
     * one-second debounce guards against rapid successive errors triggering
     * multiple `fetchJobFiles` calls.
     */
    es.onerror = () => {
      if (intentionallyClosed) return;
      es.close();
      const now = Date.now();
      const last = (es as any).__lastErrorFetch || 0;
      if (now - last < 1000) return;
      (es as any).__lastErrorFetch = now;
      p().fetchJobFiles(jobId);
    };
  }, []); // ← Stable identity: prevents the parent's effects from re-running.

  return { connectStream, closeStream, eventSourceRef };
}
