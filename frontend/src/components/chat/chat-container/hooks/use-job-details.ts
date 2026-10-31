// Path: frontend/src/components/chat/chat-container/hooks/use-job-details.ts
"use client";

import { useCallback, useEffect, useRef } from "react";
import type { AttachedUserFile, ChatTurn } from "@/components/chat/chat-thread";
import type { StepEvent } from "@/components/chat/chat-timeline";
import { safeRender } from "../utils";
import type { ChatStatus, TokenUsage, UsageMetrics } from "../types";

interface UseJobDetailsParams {
  setActiveChatId: (id: string | null) => void;
  setSubmittedPrompt: (p: string) => void;
  setSubmittedFiles: (files: AttachedUserFile[]) => void;
  setSelectedAgentId: (id: string) => void;
  setExecMode: (m: any) => void;
  setActiveModelName: (m: string) => void;
  setStatus: (s: ChatStatus) => void;
  setFinalResult: (r: string | null) => void;
  setSessionTimestamp: (t: string) => void;
  setHistoryTurns: React.Dispatch<React.SetStateAction<ChatTurn[]>>;
  setTokensUsed: React.Dispatch<React.SetStateAction<TokenUsage>>;
  setCurrentUsageMetrics: (m: UsageMetrics | null) => void;
  setSteps: React.Dispatch<React.SetStateAction<StepEvent[]>>;
  setCurrentStepNum: (n: number) => void;
  currentStepNumRef: React.MutableRefObject<number>;
  fetchJobFiles: (jobId: string) => void;
  connectStream: (jobId: string) => void;
}

/**
 * Project the top-level ``attachments`` array returned by
 * ``GET /api/run/jobs/{id}`` into the ``AttachedUserFile`` shape consumed
 * by ``UserMessageBubble`` / ``AttachedFileCard``.
 *
 * Mirrors ``_project_attachments_for_turn`` on the backend: entries
 * without a valid string ``fileId`` are dropped, duplicates are
 * collapsed, and optional fields are only included when well-typed.
 * Non-array inputs yield ``[]`` so a schema drift on the backend never
 * crashes the container during hydration.
 */
function projectAttachmentsForCurrentTurn(
  attachments: unknown
): AttachedUserFile[] {
  if (!Array.isArray(attachments)) return [];
  const result: AttachedUserFile[] = [];
  const seen = new Set<string>();
  for (const a of attachments) {
    if (!a || typeof a !== "object") continue;
    const entry = a as Record<string, unknown>;
    const fileId = entry.fileId;
    if (typeof fileId !== "string" || !fileId) continue;
    if (seen.has(fileId)) continue;
    seen.add(fileId);
    const fileNameRaw = entry.fileName;
    const name =
      typeof fileNameRaw === "string" && fileNameRaw ? fileNameRaw : fileId;
    const item: AttachedUserFile = { name, fileId };
    const mimeType = entry.mimeType;
    if (typeof mimeType === "string" && mimeType) {
      item.mimeType = mimeType;
    }
    const width = entry.width;
    if (typeof width === "number") item.width = width;
    const height = entry.height;
    if (typeof height === "number") item.height = height;
    result.push(item);
  }
  return result;
}

/**
 * Fetches a job's persisted state (turn history, replayed events, usage)
 * and, if the job is still running, attaches the SSE stream.
 *
 * Design note:
 *   The caller passes `params` inline on every render, so its identity is
 *   not stable. To keep the returned callback referentially stable — which
 *   is essential for the parent's `useEffect(..., [initialJobId, fetchJobDetails])`
 *   dependency array — the params object is mirrored into a ref and the
 *   callback is declared with an empty dependency list.
 */
export function useJobDetails(params: UseJobDetailsParams) {
  const paramsRef = useRef(params);
  useEffect(() => {
    paramsRef.current = params;
  }, [params]);

  return useCallback(async (jobId: string) => {
    const p = paramsRef.current;

    try {
      const res = await fetch(`/api/run/jobs/${jobId}`);
      if (!res.ok) return;

      const data = await res.json();
      if (data.chat_id) p.setActiveChatId(data.chat_id);
      if (data.prompt) p.setSubmittedPrompt(data.prompt);
      // Project the current turn's multimodal attachments. Populated
      // whenever the session carries image references at the top level
      // (i.e. before the turn has been flushed into turns[] by the next
      // run). This is the ONLY path that lets a page reload surface the
      // active turn's images; historical turns get theirs from
      // ``t.attached_files`` during the turns[] loop below.
      p.setSubmittedFiles(
        projectAttachmentsForCurrentTurn(data.attachments)
      );
      if (data.agent_id) p.setSelectedAgentId(data.agent_id);
      if (data.mode) p.setExecMode(data.mode);
      if (data.model) p.setActiveModelName(data.model);
      if (data.status) p.setStatus(data.status);
      if (data.result) p.setFinalResult(safeRender(data.result));
      p.setSessionTimestamp(
        data.created_at || data.timestamp || new Date().toLocaleString()
      );

      if (data.turns && Array.isArray(data.turns)) {
        const loadedTurns: ChatTurn[] = data.turns.map(
          (t: any, idx: number) => {
            const replayedSteps: StepEvent[] = (t.events || [])
              .map((ev: any, evIdx: number) => ({
                id: `hist-${idx}-step-${evIdx}`,
                step: ev.step || 1,
                type: ev.type || "thought",
                content: safeRender(
                  ev.data?.thought ||
                    ev.data?.output ||
                    ev.data?.content ||
                    ev.data ||
                    JSON.stringify(ev)
                ),
                toolName:
                  ev.data?.tool_name || ev.data?.name || ev.data?.tool,
                timestamp: ev.timestamp || "",
              }))
              .filter(
                (ev: StepEvent) =>
                  ev.content.trim() !== "" || Boolean(ev.toolName)
              );

            return {
              id: t.job_id || `turn-${idx}`,
              jobId: t.job_id || "",
              prompt: t.prompt || "",
              timestamp: t.created_at || "",
              steps: replayedSteps,
              finalResult: safeRender(t.result),
              status: t.status || "completed",
              model: t.model,
              producedFiles: t.produced_files,
              attachedFiles: t.attachedFiles || undefined,
              usage: t.usage || undefined,
              tokensUsed: t.usage
                ? {
                    input: t.usage.prompt_tokens || t.usage.input_tokens || 0,
                    output:
                      t.usage.completion_tokens ||
                      t.usage.output_tokens ||
                      0,
                    total: t.usage.total_tokens || 0,
                  }
                : undefined,
            };
          }
        );
        p.setHistoryTurns(loadedTurns);

        if (data.usage_summary && data.usage_summary.total_tokens > 0) {
          p.setTokensUsed({
            input: data.usage_summary.input_tokens || 0,
            output: data.usage_summary.output_tokens || 0,
            total: data.usage_summary.total_tokens || 0,
          });
          p.setCurrentUsageMetrics({
            cost_usd: data.usage_summary.total_cost_usd,
            latency_ms: data.usage_summary.latency_ms,
            tokens_per_second: data.usage_summary.tokens_per_second,
          });
        }
      }

      if (data.events && Array.isArray(data.events)) {
        const replayed: StepEvent[] = [];
        data.events.forEach((ev: any, idx: number) => {
          const evType = ev.type || "thought";
          const evContent =
            ev.data?.thought ||
            ev.data?.output ||
            ev.data?.content ||
            ev.data ||
            JSON.stringify(ev);
          const rendered = safeRender(evContent);
          const tool = ev.data?.tool_name || ev.data?.name || ev.data?.tool;
          if (rendered.trim() !== "" || Boolean(tool)) {
            replayed.push({
              id: `replay-${idx}-${Math.random()}`,
              step: ev.step || 1,
              type: evType,
              content: rendered,
              toolName: tool,
              timestamp: ev.timestamp || new Date().toLocaleTimeString(),
            });
          }
        });
        p.setSteps(replayed);
        const maxStep = replayed.reduce((max, s) => Math.max(max, s.step), 0);
        if (maxStep > 0) {
          p.setCurrentStepNum(maxStep);
          p.currentStepNumRef.current = maxStep;
        }
      }

      p.fetchJobFiles(data.id || jobId);
      if (data.status === "running") {
        p.connectStream(data.id || jobId);
      }
    } catch (e) {
      console.error("Failed to fetch job details", e);
    }
  }, []); // ← Stable identity: prevents the parent's effect from re-running.
}
