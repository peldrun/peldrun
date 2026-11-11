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

// [Phase F.3] SSE connection ownership moved to the shared
// runStreamManager (see lib/run-stream-manager.ts). This hook no
// longer opens its own EventSource — it subscribes to the manager
// and processes the events that arrive. The manager keeps the
// stores (stream-store + session-store) in sync on its own; this
// hook's job is now purely to project events into ChatContainer's
// local UI state (steps, finalResult, humanQuery, ...).
import { runStreamManager } from "@/lib/run-stream-manager";
import { useToolStreamBuffer } from "@/lib/use-tool-stream-buffer";

/**
 * Metadata for an MCP server that requires credentials before it can
 * be used in the current chat.
 *
 * Emitted by the backend's pre-flight auth gate as an SSE
 * `mcp_auth_required` event. The corresponding job is already in a
 * failed state server-side, so the stream is closed immediately after
 * this payload reaches the container.
 *
 * The container is responsible for:
 *   1. Opening the auth modal with this payload.
 *   2. Recording the user's choice via the appropriate /mcp-auth
 *      endpoint.
 *   3. Re-issuing POST /api/run with the same prompt so the fresh
 *      job picks up the newly-recorded credentials.
 */
export interface McpAuthRequiredPayload {
  /** Registry id of the MCP server (e.g. "mcp_github"). */
  serverId: string;

  /** Human-friendly display name for the modal heading. */
  serverName: string;

  /** Declared mechanism: "oauth" | "env" | "apikey" | "bearer" | etc. */
  authMethod: string;

  /** Raw JSON Schema for env vars (used to render the form), or null. */
  envSchema: Record<string, unknown> | null;

  /** Whether the server exposes an HTTP URL, i.e. can do OAuth. */
  oauthSupported: boolean;
}

interface UseSseStreamParams {
  currentStepNumRef: React.MutableRefObject<number>;
  activeChatIdRef: React.MutableRefObject<string | null>;
  /**
   * Current run mode. Used to decide whether image_results should be
   * appended as a timeline step (agent mode) or injected into
   * finalResult as a special markdown block (chat mode, which does
   * not render the timeline).
   */
  execMode?: "agent" | "chat";
  setSteps: React.Dispatch<React.SetStateAction<StepEvent[]>>;
  setCurrentStepNum: (n: number) => void;
  setActiveModelName: (m: string) => void;
  setTokensUsed: React.Dispatch<React.SetStateAction<TokenUsage>>;
  setCurrentUsageMetrics: (m: UsageMetrics | null) => void;
  setFinalResult: React.Dispatch<React.SetStateAction<string | null>>;
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
  /**
   * [CHAT-REASONING] Dedicated reasoning-text setter for Chat mode.
   *
   * Chat mode does NOT use the step-based timeline: reasoning is a
   * single continuous stream rendered in a dedicated collapsible
   * panel (like DeepSeek-R1's "Thinking…" block). This setter
   * receives every thought delta verbatim; the consumer feeds it into
   * `ChatThread` as a single string.
   *
   * Optional so agent-only consumers remain unaffected.
   */
  setChatReasoningText?: React.Dispatch<React.SetStateAction<string>>;
  fetchJobFiles: (jobId: string) => void;

  /**
   * Invoked when the backend emits `mcp_auth_required`. The container
   * uses this to open the per-chat authentication modal. Optional so
   * existing call sites that do not need MCP auth are unaffected —
   * the handler path simply becomes a no-op.
   */
  onMcpAuthRequired?: (payload: McpAuthRequiredPayload) => void;
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
  // [Phase F.3] The manager now owns the EventSource. This hook holds:
  //   * unsubscribeRef  — cleanup function returned by subscribe().
  //   * currentJobIdRef — tracks the job the subscription belongs to.
  //   * eventSourceRef  — kept as a null stub for backwards compatibility
  //                       with callers that still destructure it from the
  //                       return value (currently only ChatContainer).
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const currentJobIdRef = useRef<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null); // always null

  /**
   * Image results accumulated during the current run. Reset on every
   * `connectStream()` call. In chat mode, these are injected as a
   * markdown code block into finalResult on the `final` event, which
   * MarkdownRenderer converts into an inline gallery. In agent mode,
   * each image_results event is appended as its own timeline step.
   */
  const collectedImagesRef = useRef<
    Array<{
      title: string;
      image_url: string;
      thumbnail_url?: string | null;
      source_url?: string | null;
      width?: number | null;
      height?: number | null;
      source?: string | null;
    }>
  >([]);
  const collectedImagesQueryRef = useRef<string>("");
  const collectedImagesProviderRef = useRef<string>("");

  /**
   * [TOOL_CALL_STARTING] Tracks tool_call_ids for which a "pending"
   * placeholder has already been appended to the timeline. When the
   * real `tool_call` event arrives with the same id, we replace the
   * pending step in place instead of appending a duplicate.
   *
   * Cleared on every fresh connection (see connectStream). A new run
   * may reuse the same step indices but never the same tool_call_ids
   * (they are UUID-derived on the backend).
   */
  const pendingToolCallsRef = useRef<Set<string>>(new Set());

  /**
   * Mirror of the latest `params` object. Handlers read from this ref so they
   * always observe the freshest state setters without forcing the callback
   * itself to change identity.
   */
  const paramsRef = useRef(params);
  useEffect(() => {
    paramsRef.current = params;
  }, [params]);

  // -----------------------------------------------------------------
  // Tool Output Streaming: per-chunk batching.
  //
  // `useToolStreamBuffer` accumulates raw chunks in a ref and flushes
  // them ONCE per animation frame (~60 Hz cap). This prevents a full
  // `setSteps` re-render for every chunk (30-60/s per tool) while
  // keeping the visual experience fluid. See the hook's docstring for
  // the full rationale.
  //
  // The callback appends the flushed text to the matching
  // `tool_call` step's `liveOutput` and `liveStreams`. The stream
  // value is narrowed to the three backend-legal channels so the
  // indexed assignment is type-safe; an unexpected value falls back
  // to "stdout".
  //
  // Chunks arriving for a `toolCallId` that has no matching step are
  // silently dropped. In practice this cannot happen because the
  // backend emits TOOL_CALL before any TOOL_OUTPUT, and the manager
  // delivers events in order — but the guard prevents a latent crash
  // if that invariant is ever violated.
  // -----------------------------------------------------------------
  const { push: pushToolOutputStream, flush: flushToolOutputStream } =
    useToolStreamBuffer((toolCallId, text, stream) => {
      const validStream: "stdout" | "stderr" | "output" =
        stream === "stderr"
          ? "stderr"
          : stream === "output"
          ? "output"
          : "stdout";
      paramsRef.current.setSteps((prev) => {
        const idx = prev.findIndex(
          (s) => s.type === "tool_call" && s.toolCallId === toolCallId,
        );
        if (idx === -1) return prev;
        const target = prev[idx];
        const prevStreams = target.liveStreams || {};
        const next = [...prev];
        next[idx] = {
          ...target,
          liveOutput: (target.liveOutput || "") + text,
          liveStreams: {
            ...prevStreams,
            [validStream]: (prevStreams[validStream] || "") + text,
          },
        };
        return next;
      });
    });

  const closeStream = useCallback(() => {
    // Drop the local subscription. This does NOT disconnect the
    // manager from the SSE stream — the manager owns the connection
    // lifecycle independently and may be tracking other jobs. To
    // force-close the manager's connection for this job, call
    // runStreamManager.disconnect(jobId) directly (currently used
    // only by ChatContainer.handleNewSession).
    if (unsubscribeRef.current) {
      try {
        unsubscribeRef.current();
      } catch (err) {
        if (DEBUG_SSE) {
          console.warn("[SSE] unsubscribe threw during closeStream:", err);
        }
      }
      unsubscribeRef.current = null;
    }
    currentJobIdRef.current = null;
  }, []);

  const connectStream = useCallback(
    (
      jobId: string,
      chatId?: string,
      options?: {
        prompt?: string;
        title?: string;
        agentId?: string;
        projectId?: string;
      },
    ) => {
      // [Phase F.3] Drop the previous subscription before creating a
      // new one. This is local cleanup only; the manager's connection
      // lifetime is independent.
      if (unsubscribeRef.current) {
        try {
          unsubscribeRef.current();
        } catch (err) {
          if (DEBUG_SSE) {
            console.warn("[SSE] unsubscribe threw:", err);
          }
        }
        unsubscribeRef.current = null;
      }

      currentJobIdRef.current = jobId;
      const resolvedChatId = chatId || jobId;

      // Reset accumulated image state on every fresh connection.
      collectedImagesRef.current = [];
      collectedImagesQueryRef.current = "";
      collectedImagesProviderRef.current = "";
      // [TOOL_CALL_STARTING] Clear pending tool-call ids so a fresh
      // run does not inherit stale ids from the previous run.
      pendingToolCallsRef.current.clear();

      /** Convenience accessor for the freshest params snapshot. */
      const p = () => paramsRef.current;

    /** Append a visible step to the timeline, dropping empty entries. */
    const appendStep = (
      type: StepEvent["type"],
      content: any,
      stepNum = 1,
      toolName?: string,
      toolCallId?: string
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
          toolCallId,
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
      } else if (eventType === "chunk") {
        // [FIX-CHUNK-RESET] Discard the previous attempt's text before
        // processing a new attempt's first chunk. The backend sends a
        // reset event (`{reset: true, attempt_id, reason}`) before
        // each retry. Without this branch, the retry's content would
        // be APPENDED to the failed attempt's text — producing
        // duplicate or corrupted output in the deliverable bubble.
        //
        // Resetting to `null` (not `""`) is deliberate: the
        // `ChatDeliverable` component renders nothing when
        // `finalResult` is `null`, so the user sees a clean slate
        // until the retry starts streaming again. An empty string
        // would render an empty styled block.
        if (payload.data?.reset === true) {
          ctx.setFinalResult(null);
          return;
        }

        const delta =
          payload.data?.delta ??
          payload.data?.text ??
          payload.data?.content ??
          (typeof payload.data === "string" ? payload.data : "");
        if (delta) {
          ctx.setFinalResult((prev) => (prev ? prev + delta : delta));
        }
      } else if (eventType === "thought") {
        // [CHAT-REASONING] Chat mode uses a dedicated reasoning
        // channel — NOT the step-based timeline. Chat mode has a
        // single continuous thought stream followed by a single
        // answer; representing it via the agent's `steps[]` array
        // (which is rendered only by ChatTimeline in agent mode) is
        // the wrong model and silently loses the reasoning in the
        // UI because `activeGroupedSteps` is `{}` in chat mode.
        //
        // In chat mode we route every thought delta to
        // `setChatReasoningText` and RETURN EARLY — never touching
        // `steps[]`. This gives us:
        //   * Completely independent reasoning display
        //     (ChatReasoningPanel) that mirrors DeepSeek-R1's
        //     collapsible "Thinking…" block.
        //   * No per-token step allocation, no groupStepEvents
        //     reducer work, no agent-mode artifacts.
        //
        // The `reset` sentinel (emitted before each retry)
        // clears the accumulated reasoning, exactly like the
        // agent-mode reset clears the trailing thought steps.
        if (ctx.execMode === "chat" && ctx.setChatReasoningText) {
          if (payload.data?.reset === true) {
            ctx.setChatReasoningText("");
            return;
          }
          const isDelta = Boolean(payload.data?.is_delta);
          const raw =
            payload.data?.delta ??
            payload.data?.thought ??
            payload.data?.content ??
            payload.data;
          const rendered = safeRender(raw);
          if (!rendered) return;
          if (isDelta) {
            ctx.setChatReasoningText((prev) => prev + rendered);
          } else {
            // A non-delta emission carries the complete text — replace
            // rather than append.
            ctx.setChatReasoningText(rendered);
          }
          return;
        }

        // [FIX-THOUGHT-RESET] Discard the failed attempt's reasoning
        // before processing a new attempt's first thought delta. The
        // backend sends a reset event with no `thought` field before
        // each retry. The historical code fell through to the
        // `shouldProcess` gate, where `rendered.trim() === ""` caused
        // the event to be silently dropped — leaving the failed
        // attempt's thought steps visible in the timeline. When the
        // retry then streamed new reasoning, the historical
        // `prev[prev.length - 1].type === "thought"` branch appended
        // to the FAILED attempt's content instead of starting a new
        // step.
        //
        // The fix removes the trailing thought steps in the CURRENT
        // step. Thought steps from earlier completed steps are kept —
        // only the current step's reasoning is being restarted.
        if (payload.data?.reset === true) {
          ctx.setSteps((prev) => {
            const next = [...prev];
            // Pop trailing thought steps belonging to THIS step.
            // A thought from a previous step (different `step`
            // number) or a non-thought step (tool_call, observation,
            // …) acts as a fence — we stop popping there so we do
            // not corrupt completed history.
            while (
              next.length > 0 &&
              next[next.length - 1].type === "thought" &&
              next[next.length - 1].step === step
            ) {
              next.pop();
            }
            return next;
          });
          return;
        }

        const isDelta = Boolean(payload.data?.is_delta);
        const raw =
          payload.data?.delta ??
          payload.data?.thought ??
          payload.data?.content ??
          payload.data;
        const rendered = safeRender(raw);

        // [STREAMING-AGENT] Whitespace handling is asymmetric:
        //   * Deltas MUST be preserved verbatim, including pure
        //     whitespace tokens (" ", "\n"), because concatenating
        //     deltas without them corrupts word boundaries
        //     ("hello" + "world" → "helloworld"). We accept any
        //     non-empty string.
        //   * Non-deltas are typically complete thoughts; we skip
        //     whitespace-only noise to avoid empty timeline entries.
        const shouldProcess = isDelta
          ? rendered.length > 0
          : rendered.trim() !== "";

        if (shouldProcess) {
          if (isDelta) {
            ctx.setSteps((prev) => {
              const lastIdx = prev.length - 1;
              if (
                lastIdx >= 0 &&
                prev[lastIdx].type === "thought" &&
                prev[lastIdx].step === step
              ) {
                const updated = [...prev];
                updated[lastIdx] = {
                  ...updated[lastIdx],
                  content: updated[lastIdx].content + rendered,
                };
                return updated;
              }
              return [
                ...prev,
                {
                  id: `thought-${step}-${Date.now()}`,
                  step,
                  type: "thought",
                  content: rendered,
                  timestamp: new Date().toLocaleTimeString(),
                },
              ];
            });
          } else {
            appendStep("thought", raw, step);
          }
          ctx.setExpandedSteps((prev) => ({
            ...prev,
            [`active-${step}`]: true,
          }));
          if (!isDelta) {
            logEntry(`[${logTimestamp}] [THOUGHT] ${rendered}`);
          }
        }
        if (payload.data?.tokens) ctx.setTokensUsed(payload.data.tokens);
      } else if (eventType === "tool_call_starting") {
        // ─── Early tool-call signal ────────────────────────────────
        //
        // Fired the moment the model begins emitting a tool call.
        // The JSON arguments are still streaming at this point
        // (5-20 seconds for a typical file-creation payload). We
        // append a PLACEHOLDER step with `isPending: true` so the
        // timeline renders "⏳ preparing <tool>…" immediately.
        //
        // When the real `tool_call` event arrives with the same
        // `tool_call_id`, the placeholder is REPLACED in place —
        // no duplicate, no re-mount, no flicker.
        const startingToolName = String(payload.data?.tool_name || "");
        const startingToolCallId = String(payload.data?.tool_call_id || "");
        if (!startingToolName || !startingToolCallId) {
          return;
        }

        // Idempotency: skip if we already appended a pending
        // placeholder for this id. `pendingToolCallsRef` is cleared
        // when the real tool_call arrives, so a repeated starting
        // event after that point cannot happen for the same id.
        if (pendingToolCallsRef.current.has(startingToolCallId)) {
          return;
        }
        pendingToolCallsRef.current.add(startingToolCallId);

        p().setSteps((prev) => [
          ...prev,
          {
            id: `pending-${startingToolCallId}-${Date.now()}`,
            step,
            type: "tool_call",
            content: "",
            toolName: startingToolName,
            toolCallId: startingToolCallId,
            isPending: true,
            timestamp: new Date().toLocaleTimeString(),
          },
        ]);
        ctx.setExpandedSteps((prev) => ({
          ...prev,
          [`active-${step}`]: true,
        }));
        logEntry(
          `[${logTimestamp}] [TOOL_CALL_STARTING] ${startingToolName}…`
        );
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

        // Correlate the local StepEvent with the backend's tool_call_id
        // so that streamed TOOL_OUTPUT chunks (which arrive keyed by
        // tool_call_id) can be appended to THIS step. Without this,
        // the live output would have no step to land on and would be
        // silently dropped by the buffer's flush callback.
        const toolCallId =
          payload.data?.tool_call_id ||
          payload.data?.toolCallId ||
          undefined;

        // [TOOL_CALL_STARTING] Match the real tool_call to its pending
        // placeholder. Two-step matching strategy:
        //
        //   1. Exact id match — the clean case where the backend used
        //      the same tool_call_id for both the starting signal and
        //      the completed event.
        //
        //   2. Name fallback — required because the LLM provider often
        //      generates a fresh id per attempt. When the model
        //      abandons a tool call and starts a new one with the
        //      SAME tool name (e.g. retries `create index.html`),
        //      the backend's `tool_call_starting` signal fires with
        //      an id that will never appear again. Without the
        //      fallback, the abandoned placeholder lingers forever
        //      as "(tool call was interrupted before arguments
        //      arrived)".
        //
        // We prefer exact id match; only if none is found do we scan
        // for a pending placeholder with the same tool name in the
        // current step. Both are cheap O(N) operations over the
        // local StepEvent[] state (typically < 50 entries).
        const renderedContent = safeRender(parsedArgs || rawArgs);
        let replaced = false;
        p().setSteps((prev) => {
          // Attempt 1: exact id match.
          let idx = -1;
          if (toolCallId) {
            idx = prev.findIndex(
              (s) =>
                s.type === "tool_call" &&
                s.isPending &&
                s.toolCallId === toolCallId,
            );
          }

          // Attempt 2: name fallback (same step, same tool name).
          if (idx === -1 && name) {
            idx = prev.findIndex(
              (s) =>
                s.type === "tool_call" &&
                s.isPending &&
                s.toolName === name &&
                s.step === step,
            );
          }

          if (idx !== -1) {
            replaced = true;
            if (toolCallId) {
              pendingToolCallsRef.current.delete(toolCallId);
            }
            const next = [...prev];
            next[idx] = {
              ...next[idx],
              content: renderedContent,
              isPending: false,
              toolCallId: toolCallId ?? next[idx].toolCallId,
            };
            return next;
          }

          // No placeholder to replace — append a fresh step.
          return [
            ...prev,
            {
              id: `${Date.now()}-${Math.random()}`,
              step,
              type: "tool_call",
              content: renderedContent,
              toolName: name || undefined,
              toolCallId,
              timestamp: new Date().toLocaleTimeString(),
            },
          ];
        });
        if (replaced && toolCallId) {
          // Successfully paired the completed event with a pending
          // placeholder; remove the old id from the tracking set so
          // it can never match again.
          pendingToolCallsRef.current.delete(toolCallId);
        }
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
      } else if (eventType === "tool_output") {
        // ─── Tool Output Streaming: raw stdout/stderr chunk ─────────
        //
        // Emitted WHILE a tool is executing. Accumulated by
        // `useToolStreamBuffer` and flushed at most once per animation
        // frame to avoid a re-render per chunk.
        //
        // Deduplication and attempt separation are handled by the
        // reducer in `lib/stream-reducer.ts` (which updates `ToolPair`
        // in `stream-store`). The local `StepEvent[]` state handled
        // here mirrors that behavior with a simpler contract:
        // `useToolStreamBuffer` already coalesces per key, and the
        // `runStreamManager` delivers each event once per successful
        // SSE connection.
        const outToolCallId =
          payload.data?.tool_call_id || payload.data?.toolCallId;
        const outStream = String(payload.data?.stream || "stdout");
        const outChunk = String(payload.data?.chunk || "");
        if (outToolCallId && outChunk) {
          pushToolOutputStream(outToolCallId, outStream, outChunk);
        }
      } else if (eventType === "tool_progress") {
        // ─── Tool Output Streaming: structured progress update ─────
        //
        // Progress updates are low-frequency (typically < 1 Hz per
        // tool) and carry small payloads, so they are applied
        // synchronously without batching. Batching a handful of
        // progress events per second through rAF would add latency
        // for no measurable benefit.
        const progToolCallId =
          payload.data?.tool_call_id || payload.data?.toolCallId;
        if (progToolCallId) {
          const progressPayload = {
            current:
              typeof payload.data?.current === "number"
                ? payload.data.current
                : undefined,
            total:
              typeof payload.data?.total === "number"
                ? payload.data.total
                : undefined,
            phase:
              typeof payload.data?.phase === "string"
                ? payload.data.phase
                : undefined,
            message:
              typeof payload.data?.chunk === "string"
                ? payload.data.chunk
                : typeof payload.data?.message === "string"
                ? payload.data.message
                : undefined,
          };
          ctx.setSteps((prev) =>
            prev.map((s) => {
              if (s.type === "tool_call" && s.toolCallId === progToolCallId) {
                return { ...s, progress: progressPayload };
              }
              return s;
            })
          );
        }
      } else if (eventType === "observation") {
        // Ensure any chunks buffered in the last animation frame land
        // in the tool_call step BEFORE the observation is appended.
        // Without this, the observation would commit first and the
        // final live chunk would appear AFTER it — breaking the
        // visual ordering the user expects.
        flushToolOutputStream();

        // [TOOL_CALL_STARTING] Close any orphaned pending placeholders
        // in this step. An observation means at least one tool
        // finished executing, so any "preparing…" placeholder that
        // never received its completed tool_call was abandoned by
        // the model. Leaving them visible confuses the user (they
        // see "preparing…" for tools that never ran).
        p().setSteps((prev) =>
          prev.map((s) =>
            s.isPending && s.step === step
              ? {
                  ...s,
                  isPending: false,
                  content: "(not completed — model did not finalize this call)",
                }
              : s,
          ),
        );

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
        // Flush any pending tool-output chunks before composing the
        // final result. This guarantees no chunks are lost if the
        // tool's last write happened in the same frame as the final
        // event.
        flushToolOutputStream();

        // [TOOL_CALL_STARTING] Remove any orphaned pending
        // placeholders entirely. The model abandoned them, so showing
        // "(tool call was interrupted before arguments arrived)" adds
        // noise without useful information. Removing them yields a
        // clean timeline.
        //
        // Note: we FILTER (remove) rather than convert to a "failed"
        // state because the model produced these starting signals but
        // never completed the tool call — there is no user-actionable
        // information to convey. If the model needs to try again, it
        // will emit a NEW tool_call with a fresh id, which the user
        // will see as a fresh card.
        pendingToolCallsRef.current.clear();
        p().setSteps((prev) => prev.filter((s) => !s.isPending));

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
        if (eventType === "final") {
          let finalContent = safeRender(resText);

          // In chat mode, ChatThread does not render the timeline.
          // To surface the images, append a special markdown code
          // block that MarkdownRenderer converts into an inline
          // gallery. The block is invisible to the user; only the
          // rendered gallery appears.
          // Skip client-side injection when the backend already
          // embedded the gallery block in the persisted result text
          // (the new dispatcher behavior). The guard prevents double
          // rendering when both the backend and the frontend try to
          // add the same block.
          // Fallback: inject the image_gallery block when the backend
          // did not already embed it. The backend now handles this in
          // BOTH modes (dispatcher.py for chat, peldrun_engine.py for
          // agent), but the frontend keeps this path as a defensive
          // fallback for older backend versions or partial failures.
          // The ``!finalContent.includes`` guard prevents double
          // rendering when the backend already injected the block.
          if (
            collectedImagesRef.current.length > 0 &&
            !finalContent.includes("```image_gallery")
          ) {
            try {
              const payloadJson = JSON.stringify({
                images: collectedImagesRef.current,
                query: collectedImagesQueryRef.current,
                provider: collectedImagesProviderRef.current,
              });
              finalContent += `\n\n\`\`\`image_gallery\n${payloadJson}\n\`\`\`\n`;
            } catch (jsonErr) {
              if (DEBUG_SSE) {
                console.warn(
                  "[SSE] Failed to serialize image gallery payload",
                  jsonErr
                );
              }
            }
          }

          ctx.setFinalResult(finalContent);

          // Clear accumulated image state now that the turn is
          // complete. This prevents leakage into the next turn.
          collectedImagesRef.current = [];
          collectedImagesQueryRef.current = "";
          collectedImagesProviderRef.current = "";
        }

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
        // [Phase F.3] Connection close is handled by runStreamManager
        // via its `isTerminalEvent` guard. No local `es.close()` here.

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
      } else if (eventType === "mcp_auth_required") {
        // ── MCP per-chat authentication gate ────────────────────────
        //
        // The backend's pre-flight check in peldrun_engine rejected
        // the run because an MCP server declared in the agent's
        // manifest requires credentials that have no recorded decision
        // for this chat. The job has already been marked failed
        // server-side, so this stream will receive no further events.
        //
        // Behaviour:
        //   1. Surface metadata to the container via the callback.
        //      The container opens the auth modal. If the callback is
        //      absent (older integrations), we still close the stream
        //      and show the fallback message.
        //   2. Set a visible terminal message. If the user closes the
        //      modal without authenticating, this message explains
        //      the failure. If they do authenticate, the container
        //      will re-issue the run with the same prompt and start a
        //      new stream.
        //   3. Mark the run "failed" — the backend status reflects
        //      that the run has terminated; the modal is the "auth
        //      in progress" surface.
        //   4. Do NOT touch humanQuery. A pending ask_human card, if
        //      any, must survive this event unchanged.
        const serverId = String(payload.data?.server_id || "");
        const serverName = String(
          payload.data?.server_name || serverId || "MCP server"
        );
        const authMethod = String(payload.data?.auth_method || "env");
        const oauthSupported = Boolean(payload.data?.oauth_supported);
        const envSchema =
          payload.data?.env_schema &&
          typeof payload.data.env_schema === "object"
            ? (payload.data.env_schema as Record<string, unknown>)
            : null;

        // Do NOT set finalResult to a big red error box here.
        // The modal opens automatically and guides the user. If
        // the user closes the modal, the turn simply ends with no
        // visual error, which is much cleaner than a red block.
        ctx.setFinalResult(null);
        ctx.setStatus("idle"); // Hide the spinner

        // Note: The backend already failed the job. If the user
        // dismisses the modal, they can just re-send their prompt.

        if (typeof window !== "undefined") {
          logEntry(
            `[${logTimestamp}] [MCP_AUTH_REQUIRED] ${serverName} — ${authMethod}`
          );
          window.dispatchEvent(new CustomEvent("peldrun:run-completed"));
        }

        ctx.fetchJobFiles(jobId);

        // Forward to the container so it can open the auth modal.
        // A throwing callback must NOT prevent us from closing the
        // stream — the stream is already dead from the backend's
        // perspective, and leaving it open only delays the retry.
        try {
          ctx.onMcpAuthRequired?.({
            serverId,
            serverName,
            authMethod,
            envSchema,
            oauthSupported,
          });
        } catch (cbErr) {
          if (DEBUG_SSE) {
            console.warn(
              "[SSE] onMcpAuthRequired callback threw; stream will still close:",
              cbErr
            );
          }
        }

        // [Phase F.3] Connection close handled by runStreamManager.

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
      } else if (eventType === "image_results") {
        // Structured image payload from the backend. Two consumers:
        //   * Agent mode: append a step (rendered by ChatTimeline
        //     via <ImageGallery>).
        //   * Chat mode: collect for injection into finalResult on
        //     the `final` event (ChatDeliverable -> MarkdownRenderer).
        const rawImages = payload.data?.images;
        const imagesArray = Array.isArray(rawImages) ? rawImages : [];
        const provider = String(payload.data?.provider || "search");
        const query = String(payload.data?.query || "");

        if (imagesArray.length > 0) {
          // Accumulate for chat-mode finalResult injection.
          collectedImagesRef.current.push(...imagesArray);
          if (query) collectedImagesQueryRef.current = query;
          collectedImagesProviderRef.current = provider;

          // Agent mode: emit a dedicated timeline step so the gallery
          // renders inside ChatTimeline (which ChatThread already
          // forwards `activeGroupedSteps` to).
          if (ctx.execMode === "agent") {
            ctx.setSteps((prev) => [
              ...prev,
              {
                id: `img-${step}-${Date.now()}-${Math.random()
                  .toString(36)
                  .slice(2, 6)}`,
                step,
                type: "image_results",
                content: `${imagesArray.length} image${
                  imagesArray.length === 1 ? "" : "s"
                } via ${provider}`,
                images: imagesArray,
                imageProvider: provider,
                imageQuery: query,
                timestamp: new Date().toLocaleTimeString(),
              },
            ]);
            ctx.setExpandedSteps((prev) => ({
              ...prev,
              [`active-${step}`]: true,
            }));
          }

          logEntry(
            `[${logTimestamp}] [IMAGE_RESULTS] ${imagesArray.length} image${
              imagesArray.length === 1 ? "" : "s"
            } via ${provider}`
          );
        }
      } else if (eventType === "error") {
        // Flush pending chunks so the user sees the last output
        // before the error card renders.
        flushToolOutputStream();

        // [TOOL_CALL_STARTING] Remove orphaned pending placeholders on
        // error as well. Same rationale as the `final` handler above.
        pendingToolCallsRef.current.clear();
        p().setSteps((prev) => prev.filter((s) => !s.isPending));
        ctx.setHumanQuery(null);
        const errText = payload.data?.message ?? "Execution error encountered.";
        ctx.setFinalResult(safeRender(errText));
        ctx.setStatus("failed");
        // Discard any accumulated images so the next turn starts clean.
        collectedImagesRef.current = [];
        collectedImagesQueryRef.current = "";
        collectedImagesProviderRef.current = "";
        if (typeof window !== "undefined") {
          logEntry(`[${logTimestamp}] [ERROR] ${safeRender(errText)}`);
          window.dispatchEvent(new CustomEvent("peldrun:run-completed"));
        }
        ctx.fetchJobFiles(jobId);
        // [Phase F.3] Connection close handled by runStreamManager.

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

      // -----------------------------------------------------------------
      // Delegation / Multi-Agent lifecycle events
      //
      // [Phase F.3] These handlers were previously bound via a separate
      // `DELEGATION_EVENT_TYPES.forEach` block that registered an
      // `es.addEventListener` per event. Since the manager now owns the
      // EventSource, the routing is unified: every event (including
      // delegation events) flows through this function, so the manager
      // subscription only needs a single dispatch point.
      //
      // Payload shape: same as the previous per-event listener — the
      // fields are read from `payload.data` (which is `envelope.data`
      // when the manager forwards an event).
      // -----------------------------------------------------------------
      else if (eventType === "approval_required") {
        const approvalId = payload.data?.approval_request_id;
        if (typeof approvalId === "string" && approvalId) {
          ctx.setPendingApprovals((prev) => ({
            ...prev,
            [approvalId]: {
              approvalRequestId: approvalId,
              taskId: payload.data?.task_id || "",
              parentRunId: payload.data?.parent_run_id || "",
              targetAgentId: payload.data?.target_agent_id || "",
              instruction: payload.data?.instruction || "",
              status: "pending",
              timeoutSeconds: payload.data?.timeout_seconds || 300,
              createdAt: Date.now(),
            },
          }));
        }
        logEntry(
          `[${logTimestamp}] [APPROVAL] ${payload.data?.target_agent_id || "agent"} awaiting approval`
        );
      } else if (eventType === "approval_resolved") {
        const approvalId = payload.data?.approval_request_id;
        if (typeof approvalId === "string" && approvalId) {
          ctx.setPendingApprovals((prev) => {
            const next = { ...prev };
            delete next[approvalId];
            return next;
          });
        }
      } else if (eventType === "child_run_started") {
        const childId = payload.data?.child_run_id;
        if (typeof childId === "string" && childId) {
          ctx.setActiveChildRuns((prev) => ({
            ...prev,
            [childId]: {
              childRunId: childId,
              parentRunId: payload.data?.parent_run_id || "",
              taskId: payload.data?.task_id || "",
              targetAgentId: payload.data?.target_agent_id || "",
              instruction: payload.data?.instruction || "",
              status: "running",
              startedAt: Date.now(),
              steps: [],
              deliverables: [],
            },
          }));
        }
      } else if (
        eventType === "child_run_completed" ||
        eventType === "child_run_failed" ||
        eventType === "child_run_cancelled"
      ) {
        const childId = payload.data?.child_run_id;
        if (typeof childId === "string" && childId) {
          const cStatus =
            eventType === "child_run_completed"
              ? "completed"
              : eventType === "child_run_cancelled"
              ? "cancelled"
              : "failed";
          ctx.setActiveChildRuns((prev) => {
            const cur = prev[childId];
            if (!cur) return prev;
            return {
              ...prev,
              [childId]: {
                ...cur,
                status: cStatus,
                deliverables: payload.data?.deliverables || cur.deliverables,
                tokensUsed: payload.data?.tokens_used,
                durationSeconds: payload.data?.duration_seconds,
                error: payload.data?.error,
              },
            };
          });
        }
      }
    };

    // -----------------------------------------------------------------
    // [Phase F.3] Subscribe to the shared runStreamManager for this job.
    //
    // The manager owns the EventSource: it opens exactly one connection
    // per jobId, handles reconnection with backoff, uses
    // `after_sequence` to resume, detects heartbeats, and closes on
    // terminal events. It also keeps stream-store and session-store
    // in sync (see lib/run-stream-manager.ts).
    //
    // This hook's only remaining responsibility is to project each event
    // into ChatContainer's local UI state via handleEventPayload. The
    // envelope shape matches the payload shape handleEventPayload was
    // written against:
    //   envelope.type  ↔  event name
    //   envelope.step  ↔  payload.step
    //   envelope.data  ↔  payload.data
    // -----------------------------------------------------------------
    runStreamManager.registerRun(jobId, resolvedChatId, {
      prompt: options?.prompt,
      title: options?.title,
      agentId: options?.agentId,
      projectId: options?.projectId,
    });

    const unsubscribe = runStreamManager.subscribe(jobId, (_run, envelope) => {
      // The manager fires subscribers once with a null envelope during
      // initial sync (so consumers can read the current state without
      // a race). We only process real events.
      if (!envelope) return;

      try {
        handleEventPayload(envelope.type as string, envelope);
      } catch (err) {
        if (DEBUG_SSE) {
          console.warn(
            "[SSE] handleEventPayload threw for event type",
            envelope.type,
            err,
          );
        }
      }
    });

    unsubscribeRef.current = unsubscribe;
  },
  []); // Stable identity: prevents the parent's effects from re-running.

  return { connectStream, closeStream, eventSourceRef };
}
