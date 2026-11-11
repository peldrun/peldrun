// Path: frontend/src/components/chat/chat-container/hooks/use-job-details.ts
"use client";

import { useCallback, useEffect, useRef } from "react";
import type { AttachedUserFile, ChatTurn } from "@/components/chat/chat-thread";
import type { StepEvent } from "@/components/chat/chat-timeline";
import { safeRender } from "../utils";
import type { ChatStatus, TokenUsage, UsageMetrics } from "../types";


/**
 * Merge consecutive ``thought`` steps that belong to the same numeric
 * step into a single block.
 *
 * Background
 * ----------
 * The SSE stream persists EVERY thought delta as its own event in
 * ``session.json`` (one token = one event). The live SSE path in
 * ``use-sse-stream.ts`` already concatenates these deltas into the
 * last thought step during streaming, so the user sees one growing
 * bubble. But the replay path (this file) creates a separate
 * ``StepEvent`` per persisted event, which produces N separate
 * bubbles on every page refresh — one for every token the model ever
 * emitted.
 *
 * This function restores the same behavior for replay: consecutive
 * thoughts for the same step become one coherent block. A tool call,
 * observation, or any other event between two thoughts breaks the
 * run (correctly), so reasoning on either side of a tool call is
 * preserved as distinct blocks.
 *
 * Called BEFORE the empty-content filter so that whitespace-only
 * deltas (e.g. ``" "`` between words) survive concatenation — the
 * filter would otherwise drop them, corrupting word boundaries.
 */
function mergeConsecutiveThoughts(steps: StepEvent[]): StepEvent[] {
  if (!steps || steps.length === 0) return steps;
  const merged: StepEvent[] = [];
  for (const step of steps) {
    const last = merged.length > 0 ? merged[merged.length - 1] : null;
    if (
      last !== null &&
      last.type === "thought" &&
      step.type === "thought" &&
      last.step === step.step
    ) {
      // Adjacent thoughts for the same step: concatenate content.
      // Keep the earliest id/timestamp so React's key stays stable.
      merged[merged.length - 1] = {
        ...last,
        content: last.content + step.content,
      };
    } else {
      merged.push(step);
    }
  }
  return merged;
}


interface UseJobDetailsParams {
  setActiveChatId: (id: string | null) => void;
  setSubmittedPrompt: (p: string) => void;
  setSubmittedFiles: (files: AttachedUserFile[]) => void;
  setSelectedAgentId: (id: string) => void;
  setExecMode: (m: any) => void;
  setActiveModelName: (m: string) => void;
  /**
   * Sync the frontend's active-provider selection from the chat's
   * persisted `provider_name` field when a chat is loaded.
   *
   * WHY this exists (mirrors `setActiveModelName` exactly):
   *
   *   `activeProvider` in ChatContainer is sourced from the GLOBAL
   *   `useAppStorage("active_provider")` slot. Without this sync,
   *   switching to a different chat (or refreshing) leaves the
   *   current turn's identity header showing whatever provider the
   *   user last selected ANYWHERE — not the one that produced THIS
   *   chat's most recent run.
   *
   *   Historical turns are unaffected: they read their own
   *   per-turn `provider` snapshot from `session.json` (see the
   *   turns[] mapping below), so they always display the provider
   *   that actually generated them.
   *
   * The setter is optional to keep the hook usable from any future
   * consumer that does not subscribe to `active_provider` (e.g. a
   * read-only history viewer). When omitted, the sync is a no-op
   * and the existing global value is preserved.
   */
  setActiveProvider?: (p: string) => void;
  /**
   * Called after a chat is loaded from the backend. Provides the
   * authoritative provider/model of the LAST turn in the loaded
   * session (from session.json), so the caller can sync its per-turn
   * snapshot refs and keep the current turn's header showing the
   * correct provider after a reload.
   *
   * Without this, a page reload leaves `runStartProviderRef` at the
   * mount-time value while `activeProvider` is overwritten with
   * `data.provider_name` — the current turn header would then show
   * whichever value happened to win.
   */
  onChatLoaded?: (snapshot: {
    provider: string | null;
    model: string | null;
    mode?: "agent" | "chat";
  }) => void;
  setStatus: (s: ChatStatus) => void;
  setFinalResult: (r: string | null) => void;
  setSessionTimestamp: (t: string) => void;
  setHistoryTurns: React.Dispatch<React.SetStateAction<ChatTurn[]>>;
  setTokensUsed: React.Dispatch<React.SetStateAction<TokenUsage>>;
  setCurrentUsageMetrics: (m: UsageMetrics | null) => void;
  setSteps: React.Dispatch<React.SetStateAction<StepEvent[]>>;
  /**
   * [CHAT-REASONING] Reconstructs the dedicated reasoning stream for
   * Chat mode after a page refresh. Chat mode does not use `steps[]`,
   * so a naive replay would leave the reasoning panel empty even
   * though the persisted events carry the full thought stream.
   * Optional so agent-only consumers remain unaffected.
   */
  setChatReasoningText?: React.Dispatch<React.SetStateAction<string>>;
  setCurrentStepNum: (n: number) => void;
  currentStepNumRef: React.MutableRefObject<number>;
  fetchJobFiles: (jobId: string) => void;
  // [Phase F.3.b] connectStream now accepts the chatId and seeding
  // metadata. This mirrors the signature exposed by useSseStream
  // (chat-container/hooks/use-sse-stream.ts) after Phase F.3.a, which
  // delegates to runStreamManager.registerRun().
  connectStream: (
    jobId: string,
    chatId?: string,
    options?: {
      prompt?: string;
      title?: string;
      agentId?: string;
      projectId?: string;
    },
  ) => void;
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
      // Sync the active provider from the persisted session so the
      // CURRENT turn's identity header matches the provider that
      // actually produced it. Falls through silently when either
      // the backend did not expose `provider_name` (pre-fix
      // sessions) or the caller did not supply a setter (read-only
      // consumers).
      //
      // Historical turns are NOT affected by this assignment: their
      // provider comes from `t.provider` on each turn object (see
      // the turns[] mapping below), which was captured at run
      // archive time and is therefore immune to any later global
      // selection changes.
      if (data.provider_name && typeof data.provider_name === "string") {
        p.setActiveProvider?.(data.provider_name);
      }
      // Sync the caller's per-turn snapshot refs with the loaded
      // session's authoritative values. Only invoked when at least
      // one value is present so legacy sessions with no provider
      // metadata do not clear an in-flight ref.
      if (data.provider_name || data.model || data.mode) {
        p.onChatLoaded?.({
          provider:
            typeof data.provider_name === "string"
              ? data.provider_name
              : null,
          model:
            typeof data.model === "string" ? data.model : null,
          mode:
            data.mode === "agent" || data.mode === "chat"
              ? data.mode
              : undefined,
        });
      }
      if (data.status) p.setStatus(data.status);
      if (data.result) p.setFinalResult(safeRender(data.result));
      p.setSessionTimestamp(
        data.created_at || data.timestamp || new Date().toLocaleString()
      );

      if (data.turns && Array.isArray(data.turns)) {
        const loadedTurns: ChatTurn[] = data.turns.map(
          (t: any, idx: number) => {
            // Build raw steps first (no filtering). Filtering happens
            // AFTER merge so that whitespace-only thought deltas (e.g.
            // " " between words) survive concatenation.
            const rawSteps: StepEvent[] = (t.events || []).map(
              (ev: any, evIdx: number) => ({
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
              })
            );

            // [FIX-THOUGHT-MERGE] Collapse N consecutive thought deltas
            // (same step) into one block, then filter out truly empty
            // entries. Mirrors the live-streaming behavior in
            // use-sse-stream.ts, which appends deltas to the last step.
            const mergedSteps = mergeConsecutiveThoughts(rawSteps);
            const replayedSteps = mergedSteps.filter(
              (ev: StepEvent) =>
                ev.content.trim() !== "" || Boolean(ev.toolName)
            );

            // Reconstruct reasoning text from persisted events,
            // REGARDLESS of `t.mode`.
            //
            // `t.mode` is unreliable in legacy sessions (missing
            // entirely) and in some edge cases may be stamped
            // incorrectly. The presence of `thought` events is the
            // only definitive evidence that reasoning was captured.
            // A turn that carries thoughts will display the panel;
            // a turn with none will simply skip it.
            let hydratedReasoningText: string | undefined = undefined;
            {
              const reasoningParts: string[] = [];
              (t.events || []).forEach((ev: any) => {
                if (ev.type !== "thought") return;
                const raw =
                  ev.data?.thought ??
                  ev.data?.delta ??
                  ev.data?.content;
                if (typeof raw === "string" && raw) {
                  reasoningParts.push(raw);
                }
              });
              if (reasoningParts.length > 0) {
                hydratedReasoningText = reasoningParts.join("");
              }
            }

            return {
              id: t.job_id || `turn-${idx}`,
              jobId: t.job_id || "",
              prompt: t.prompt || "",
              timestamp: t.created_at || "",
              steps: replayedSteps,
              finalResult: safeRender(t.result),
              status: t.status || "completed",
              model: t.model,
              // Provider snapshot captured by the backend at turn
              // archival time (see start_run.py → turns.append({...
              // "provider": existing_chat.get("provider_name") ...})).
              //
              // This is what keeps each historical turn displaying
              // the provider that ACTUALLY produced it, even when
              // the user has since switched to a different provider.
              //
              // `||` normalizes three inputs to `undefined`:
              //   * `undefined`        → field was never set (pre-fix
              //                          sessions on disk).
              //   * `null`             → backend explicitly cleared it.
              //   * empty string ""    → legacy session that saved a
              //                          blank provider_name.
              // HistoricalTurn then falls back to `activeProvider` in
              // exactly those cases, preserving the pre-fix behavior
              // for old data without ever masking a valid provider.
              provider: t.provider || undefined,
              // Optional mode field. HistoricalTurn already infers
              // `mode` from step count when this is undefined, so this
              // is a pass-through improvement only — it does not
              // change behavior for sessions where the backend does
              // not persist `mode` per turn.
              mode:
                t.mode === "agent" || t.mode === "chat"
                  ? t.mode
                  : undefined,
              // [REGRESSION-FIX] Attach the reconstructed reasoning
              // text to the ChatTurn so HistoricalTurn can render
              // the Reasoning panel after a page reload.
              //
              // Root cause of the "reasoning panel disappears after
              // F5" regression: `hydratedReasoningText` was computed
              // a few lines above but never returned as part of the
              // turn object. The field was therefore `undefined` for
              // every historical turn, and HistoricalTurn's rendering
              // guard `turn.chatReasoningText && turn.chatReasoningText.trim()`
              // always failed.
              //
              // The CURRENT (last) turn was unaffected because it is
              // rendered by ChatThread from `data.events` (via
              // `setChatReasoningText`), not from this map.
              chatReasoningText: hydratedReasoningText,
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
        // Build raw steps first (no filtering). The filter runs after
        // merge so that whitespace-only thought deltas survive
        // concatenation and word boundaries are preserved.
        const rawReplayed: StepEvent[] = [];
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

          const replayedStep: StepEvent = {
            id: `replay-${idx}-${Math.random()}`,
            step: ev.step || 1,
            type: evType,
            content: rendered,
            toolName: tool,
            timestamp: ev.timestamp || new Date().toLocaleTimeString(),
          };

          // image_results events carry structured image metadata
          // (proxied URLs, dimensions, source pages) that is NOT
          // recoverable from the rendered markdown string. Without
          // this preservation, the ChatTimeline receives an
          // image_results step with an empty `images` array after
          // a page refresh, so the gallery never renders again.
          if (
            evType === "image_results" &&
            ev.data &&
            typeof ev.data === "object"
          ) {
            const evImages = (ev.data as any).images;
            if (Array.isArray(evImages) && evImages.length > 0) {
              replayedStep.images = evImages;
              replayedStep.imageProvider =
                typeof (ev.data as any).provider === "string"
                  ? (ev.data as any).provider
                  : undefined;
              replayedStep.imageQuery =
                typeof (ev.data as any).query === "string"
                  ? (ev.data as any).query
                  : undefined;
            }
          }

          rawReplayed.push(replayedStep);
        });

        // [FIX-THOUGHT-MERGE] Collapse N consecutive thought deltas
        // (same step) into one block, then filter out truly empty
        // entries. This is what makes a page refresh show ONE
        // reasoning block per step instead of one bubble per token.
        const mergedReplayed = mergeConsecutiveThoughts(rawReplayed);
        const replayed = mergedReplayed.filter(
          (ev) => ev.content.trim() !== "" || Boolean(ev.toolName)
        );

        p.setSteps(replayed);
        const maxStep = replayed.reduce((max, s) => Math.max(max, s.step), 0);
        if (maxStep > 0) {
          p.setCurrentStepNum(maxStep);
          p.currentStepNumRef.current = maxStep;
        }

        // [CHAT-REASONING] For chat mode, reconstruct the reasoning
        // stream from the persisted events. Chat mode has no `steps`
        // timeline in the UI, so without this the reasoning panel
        // would appear empty after a page refresh — even though the
        // events clearly contain hundreds of thought deltas.
        //
        // We concatenate the deltas verbatim (no mergeConsecutiveThoughts
        // needed here because we're building a single string, not an
        // array of Steps). Deltas already include whitespace tokens
        // that preserve word boundaries.
        // Reconstruct reasoning text for the CURRENT turn,
        // REGARDLESS of `data.mode`.
        //
        // Same rationale as the per-turn reconstruction above:
        // `data.mode` is not a reliable signal. We use it only to
        // know when to explicitly CLEAR stale state for chat-mode
        // sessions that genuinely produced no thoughts. For any
        // events that contain thought deltas, we always restore
        // them so the panel survives a reload.
        if (p.setChatReasoningText) {
          const reasoningParts: string[] = [];
          data.events.forEach((ev: any) => {
            if (ev.type !== "thought") return;
            const raw =
              ev.data?.thought ??
              ev.data?.delta ??
              ev.data?.content;
            if (typeof raw === "string" && raw) {
              reasoningParts.push(raw);
            }
          });
          if (reasoningParts.length > 0) {
            p.setChatReasoningText(reasoningParts.join(""));
          } else if (data.mode === "chat") {
            // No thoughts AND explicitly chat mode — clear any
            // stale state from a previously opened session.
            p.setChatReasoningText("");
          }
        }
      }

      p.fetchJobFiles(data.id || jobId);
      if (data.status === "running") {
        // [Phase F.3.b] Propagate the full (jobId, chatId, metadata)
        // tuple so registerRun can seed the centralized stores and
        // open the SSE stream with the correct resume point.
        //
        // `data` is the raw response from GET /api/run/jobs/{id}, whose
        // shape is documented in backend/omweb/routers/run_routes/jobs.py.
        // After Phase E.4 it includes chat_id, prompt, agent_id, and
        // is_actively_running — all of which are forwarded here.
        const resolvedJobId = data.id || jobId;
        const resolvedChatId =
          typeof data.chat_id === "string" && data.chat_id
            ? data.chat_id
            : undefined;
        const resolvedPrompt =
          typeof data.prompt === "string" ? data.prompt : undefined;
        const resolvedAgentId =
          typeof data.agent_id === "string" ? data.agent_id : undefined;

        p.connectStream(resolvedJobId, resolvedChatId, {
          prompt: resolvedPrompt,
          title: resolvedPrompt ? resolvedPrompt.slice(0, 40) : undefined,
          agentId: resolvedAgentId,
        });
      }
    } catch (e) {
      console.error("Failed to fetch job details", e);
    }
  }, []); // ← Stable identity: prevents the parent's effect from re-running.
}
