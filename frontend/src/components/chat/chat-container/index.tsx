"use client";

import React, {
  useState,
  useRef,
  useEffect,
  useMemo,
  useCallback,
} from "react";
import { useRouter } from "next/navigation";
import dynamic from "next/dynamic";
import {
  WorkspacePanel,
  getFileCategory,
} from "@/components/workspace/workspace-panel";
import { useSourceControlSettings } from "@/features/source-control/state/source-control-settings-store";

// Lazy-load the Source Control workspace panel. This guarantees
// that users who have the subsystem disabled never download its
// ~200 KB bundle. The `{ ssr: false }` flag is required because
// the panel reads from `localStorage` in its settings store and
// would otherwise hydration-mismatch on first paint.
const SourceControlWorkspacePanel = dynamic(
  () =>
    import(
      "@/features/source-control/components/source-control-workspace-panel"
    ).then((m) => m.SourceControlWorkspacePanel),
  { ssr: false }
);

import { Composer } from "@/components/chat/composer";
import { ChatSubHeader } from "@/components/chat/chat-sub-header";
import { ChatLanding } from "@/components/chat/chat-landing";
import {
  ChatThread,
  ChatTurn,
  AttachedUserFile,
} from "@/components/chat/chat-thread";
import type { StepEvent } from "@/components/chat/chat-timeline";
import type { EngineOption } from "@/components/chat/engine-selector";
import { useChatStore } from "@/stores/chat-store";
import { useSessionStore } from "@/stores/session-store";
import { useSourceControlStore } from "@/features/source-control/state/source-control-store";
import { useAppStorage } from "@/hooks/use-app-storage";
import { inferModelCapabilities } from "@/lib/modelMetadata";

import { getGenerativeUIPayload } from "@/components/generative-ui/store";

import type {
  ChatContainerProps,
  ChatStatus,
  TokenUsage,
  UsageMetrics,
  ChildRunState,
  DelegationApprovalRequest,
} from "./types";
import { formatFileSize, groupStepEvents } from "./utils";
import { useMetadataVault } from "./hooks/use-metadata-vault";
import { useElapsedTimer } from "./hooks/use-elapsed-timer";
import { useJobFiles } from "./hooks/use-job-files";
import { useSseStream } from "./hooks/use-sse-stream";
import type { McpAuthRequiredPayload } from "./hooks/use-sse-stream";
import { useJobDetails } from "./hooks/use-job-details";
import { MCPAuthModal } from "@/components/chat/mcp-auth/MCPAuthModal";
import { showToast } from "@/components/ui/ToastNotification";

export type { ChatContainerProps };

export function ChatContainer({ initialJobId }: ChatContainerProps) {
  const router = useRouter();

  // Three independent subscriptions. Only `selectedAgentId` can trigger a
  // re-render (when the user switches agents); both `setSelectedAgentId`
  // and `getAgentMaxSteps` are stable references that never change, so
  // subscribing to them via selectors costs nothing. Previously the whole
  // store was subscribed, causing ChatContainer — which owns the entire
  // chat timeline — to re-render on every unrelated mutation (message
  // append, step append, etc.). See docs/state-contracts.md §6.4.
  const selectedAgentId = useChatStore((s) => s.selectedAgentId);
  const setSelectedAgentId = useChatStore((s) => s.setSelectedAgentId);
  const getAgentMaxSteps = useChatStore((s) => s.getAgentMaxSteps);

  // --- UI / input state -----------------------------------------------------
  const [inputValue, setInputValue] = useState("");
  const [landingAttachedFiles, setLandingAttachedFiles] = useState<File[]>([]);
  const [submittedPrompt, setSubmittedPrompt] = useState("");
  const [submittedFiles, setSubmittedFiles] = useState<AttachedUserFile[]>([]);
  const [activeJobId, setActiveJobId] = useState<string | null>(
    initialJobId || null
  );
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const [historyTurns, setHistoryTurns] = useState<ChatTurn[]>([]);
  // After widening ChatStatus in chat-container/types.ts (Phase G.4.d),
  // the union already includes "paused" and every other canonical
  // status, so the extra `| "paused"` annotation and all the
  // subsequent `as any` casts become unnecessary.
  const [status, setStatus] = useState<ChatStatus>("idle");
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [finalResult, setFinalResult] = useState<string | null>(null);
  const [currentStepNum, setCurrentStepNum] = useState(0);
  const [selectedFileForEditor, setSelectedFileForEditor] = useState<
    string | null
  >(null);
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>(
    {}
  );
  const [copiedSection, setCopiedSection] = useState<string | null>(null);
  const [showRawTrace, setShowRawTrace] = useState(false);
  const [sandboxDraft, setSandboxDraft] = useState<{
    filename: string;
    content: string;
  } | null>(null);
  const [tokensUsed, setTokensUsed] = useState<TokenUsage>({
    input: 0,
    output: 0,
    total: 0,
  });
  const [currentUsageMetrics, setCurrentUsageMetrics] =
    useState<UsageMetrics | null>(null);
  const [humanQuery, setHumanQuery] = useState<string | null>(null);
  const [humanAnswer, setHumanAnswer] = useState("");
  const [sessionTimestamp, setSessionTimestamp] = useState<string>("");
  const [activeChildRuns, setActiveChildRuns] = useState<
    Record<string, ChildRunState>
  >({});
  const [pendingApprovals, setPendingApprovals] = useState<
    Record<string, DelegationApprovalRequest>
  >({});

  // [CHAT-REASONING] Dedicated reasoning stream for Chat mode.
  //
  // Chat mode does NOT use the step-based timeline at all: reasoning
  // is a single continuous stream displayed in a dedicated
  // collapsible panel (like DeepSeek-R1's "Thinking…" block). This
  // state is completely isolated from the agent-mode `steps` array
  // and avoids the groupStepEvents reducer on every token.
  const [chatReasoningText, setChatReasoningText] = useState<string>("");

  // Payload of the in-flight MCP authentication request, or null when
  // no modal is open. Populated by the SSE handler when the backend
  // emits `mcp_auth_required` (see use-sse-stream.ts). Cleared either
  // by the modal's onClose (user declined) or by onSuccess after a
  // decision has been recorded, at which point the container re-issues
  // POST /api/run with the same prompt.
  const [mcpAuthRequiredPayload, setMcpAuthRequiredPayload] =
    useState<McpAuthRequiredPayload | null>(null);

  // --- Persisted user preferences ------------------------------------------
  //
  // Panel visibility uses a hydration-safe localStorage pattern:
  //
  //   1. Server renders with `false` (no access to localStorage).
  //   2. Client's first render also uses `false` (matching server).
  //   3. A mount-time effect restores the persisted value.
  //   4. Every toggle writes back to localStorage.
  //
  // This guarantees zero hydration mismatch and zero cookie/server
  // dependency — the preference lives entirely in the browser.
  const [showRightPanel, setShowRightPanelState] = useState<boolean>(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const stored = window.localStorage.getItem("peldrun:right_panel_open");
      if (stored !== null) {
        setShowRightPanelState(stored === "true");
      }
    } catch {
      // localStorage may be unavailable (Safari ITP, private mode).
    }
  }, []);

  const setShowRightPanel = useCallback((open: boolean) => {
    setShowRightPanelState(open);
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem("peldrun:right_panel_open", String(open));
    } catch {
      // Persistence is best-effort.
    }
  }, []);

  // Which of the two right-hand panels is active:
  //   - "workspace"      : the existing WorkspacePanel (Preview, Files,
  //                        Artifacts, Logs, Editor).
  //   - "source-control" : the standalone SourceControlWorkspacePanel
  //                        (Agent / Manual / Browse tabs from the
  //                        source-control subsystem).
  //
  // NOTE — why `useState` + manual `localStorage` instead of
  // `useAppStorage`:
  //
  // `useAppStorage` is typed against a fixed allow-list of storage
  // keys (right_panel_open, exec_mode, theme, ...). `right_panel_mode`
  // is not on that list, so a direct call fails with:
  //
  //   Type '"workspace" | "source-control"' does not satisfy the
  //   constraint '...'
  //
  // Adding the key to the allow-list would require editing the
  // shared `useAppStorage` hook, which is used by many unrelated
  // components. That is overkill for one preference. The manual
  // pattern below gives us the same persistence behavior (survives
  // page reload, falls back gracefully if storage is unavailable)
  // without touching any shared code.
  // Same hydration-safe pattern as `showRightPanel`. Reading
  // localStorage inside a useState initializer produces a mismatch
  // because the server always renders the default. We instead start
  // with a server-consistent default and restore the persisted value
  // after mount.
  const [rightPanelMode, setRightPanelMode] = useState<
    "workspace" | "source-control"
  >("workspace");

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const stored = window.localStorage.getItem("peldrun:right_panel_mode");
      if (stored === "source-control") {
        setRightPanelMode("source-control");
      }
    } catch {
      // Access to localStorage can throw in some privacy modes.
    }
  }, []);

  // Persist `rightPanelMode` on every change.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        "peldrun:right_panel_mode",
        rightPanelMode
      );
    } catch {
      // Storage full or unavailable — persistence is best-effort.
    }
  }, [rightPanelMode]);

  // `sourceControlDefaultMode` is the mode the SourceControlPanel opens
  // in the NEXT time it mounts. `sourceControlMountKey` is bumped to
  // force a remount so the new default actually takes effect (React
  // ignores prop changes to `useState`'s initializer without a key
  // change).
  //
  // Example flow:
  //   - User clicks the GitBranch icon in the header → mount key
  //     stays the same, default mode is "browse".
  //   - User clicks "Open Review & Approve" on a review card →
  //     mount key bumps, default mode becomes "agent", so the panel
  //     opens directly on the review UI.
  const [sourceControlDefaultMode, setSourceControlDefaultMode] =
    useState<"agent" | "manual" | "browse">("browse");
  const [sourceControlMountKey, setSourceControlMountKey] = useState(0);

  const [execMode, setExecMode] = useAppStorage("exec_mode");
  const [reasoningEffort] = useAppStorage("reasoning_effort");
  const [selectedEngineId, setSelectedEngineId] =
    useAppStorage("selected_engine");
  const [activeModelName, setActiveModelName] = useAppStorage("active_model");
  // Subscribing to `active_provider` lets ChatThread + HistoricalTurn
  // render the correct provider icon/name in the identity header.
  // The value can change at any time via EngineSelector's
  // handleSelectEngine, which writes through useAppStorage — no extra
  // wiring is needed here.
  // Both the value and its setter are needed:
  //   * `activeProvider`      — read for display (ChatThread header,
  //                             fallback for historical turns).
  //   * `setActiveProvider`   — written by useJobDetails when a chat
  //                             is loaded, so the current turn's
  //                             header reflects the provider that
  //                             actually produced it (persisted in
  //                             session.json) rather than whatever
  //                             the user happened to select in a
  //                             different chat previously.
  const [activeProvider, setActiveProvider] = useAppStorage("active_provider");
  const [activeLlmOverride] = useAppStorage("active_llm_override");

  // --- Extracted hooks ------------------------------------------------------
  const metadataVault = useMetadataVault();
  const [elapsedSeconds, setElapsedSeconds] = useElapsedTimer(status as any);
  const { producedFiles, setProducedFiles, fetchJobFiles } = useJobFiles();

  // Master switch from the settings store. When this is false, the
  // entire Source Control subsystem is treated as opt-in and none
  // of its UI is rendered. See `source-control-settings-store.ts`
  // for the full design rationale.
  const sourceControlEnabled = useSourceControlSettings(
    (s) => s.enabled
  );

  const handleOpenReview = useCallback(
    (operationId: string) => {
      // Defensive guard: even if a stale event bubbles up from an
      // earlier session, do not open the panel when the user has
      // explicitly disabled Source Control.
      if (!sourceControlEnabled) {
        console.warn(
          "[handleOpenReview] Source Control is disabled; ignoring."
        );
        return;
      }
      useSourceControlStore.setState({ activeOperationId: operationId });
      useSourceControlStore.getState().openPanel("source-control");
      // Force the Source Control panel to open in Agent mode, which
      // is the review UI. The mount key change is required because
      // `SourceControlPanel` uses `useState(defaultMode)` — changing
      // the prop alone does not reset the internal mode state.
      setSourceControlDefaultMode("agent");
      setSourceControlMountKey((k) => k + 1);
      setRightPanelMode("source-control");
      setShowRightPanel(true);
    },
    [setShowRightPanel, setRightPanelMode, sourceControlEnabled]
  );

  // Refs mirror fast-changing state so SSE handlers always read the latest
  // value rather than a closure-captured snapshot.
  const currentStepNumRef = useRef(currentStepNum);
  const activeChatIdRef = useRef(activeChatId);

  // Provider + model snapshot for the CURRENTLY RUNNING (or most
  // recently completed) turn. Captured at run START time so that
  // when the run completes and gets archived into `historyTurns`, it
  // keeps the provider/model that actually produced it — even if the
  // user changed the selection in EngineSelector in the meantime.
  //
  // Without these refs, an archiving bug is unavoidable: if the user
  // changes provider/model while a run is in flight (or between
  // runs before clicking Send again), the OLD run gets archived with
  // the NEW provider/model, producing historical turns that claim to
  // have been generated by a provider they never used.
  //
  // The refs are updated in handleStartTask / handleRegenerate right
  // BEFORE dispatching the request to the backend. Until the first
  // run of a session, they hold the current active* values.
  const runStartProviderRef = useRef<string | null>(activeProvider);
  const runStartModelRef = useRef<string | null>(activeModelName);
  // Capture the execution mode (agent/chat) at the start of each run,
  // so the turn is archived with the mode it was ACTUALLY executed in,
  // even if the user switches modes before sending the next message.
  const runStartModeRef = useRef<"agent" | "chat">(execMode);

  // Tracks the safety-net timer created by handleStopTask. Declared
  // here (not further down) because it is referenced by the status
  // effect below AND by handleStopTask. Placing it before the first
  // consumer prevents a temporal-dead-zone TypeScript error.
  const stopFallbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    currentStepNumRef.current = currentStepNum;
  }, [currentStepNum]);
  useEffect(() => {
    activeChatIdRef.current = activeChatId;
  }, [activeChatId]);

  // Clear the stop-fallback timer as soon as the terminal state is
  // reached through the SSE path. Without this, the fallback would
  // still fire ~5s later and call `runStreamManager.disconnect()` on
  // an already-disconnected job — harmless but noisy in the console.
  useEffect(() => {
    if (status === "cancelled" || status === "failed" || status === "interrupted") {
      if (stopFallbackTimerRef.current !== null) {
        clearTimeout(stopFallbackTimerRef.current);
        stopFallbackTimerRef.current = null;
      }
    }
  }, [status]);

  const { connectStream, closeStream, eventSourceRef } = useSseStream({
    currentStepNumRef,
    activeChatIdRef,
    execMode,
    setSteps,
    setCurrentStepNum,
    setActiveModelName,
    setTokensUsed,
    setCurrentUsageMetrics,
    setFinalResult,
    setStatus,
    setExpandedSteps,
    setHumanQuery,
    setShowRightPanel,
    setSelectedFileForEditor,
    setActiveChildRuns,
    setPendingApprovals,
    setChatReasoningText,
    fetchJobFiles,
    // Opens the per-chat MCP auth modal. The SSE handler calls this
    // before closing the stream, so we still get the payload even
    // though the underlying job has already failed server-side.
    onMcpAuthRequired: setMcpAuthRequiredPayload,
  });

  const fetchJobDetails = useJobDetails({
    setActiveChatId,
    setSubmittedPrompt,
    setSubmittedFiles,
    setSelectedAgentId,
    setExecMode,
    setActiveModelName,
    setActiveProvider,
    // Sync the per-turn snapshot refs from the loaded chat, so the
    // current turn's header keeps showing the provider that actually
    // produced it (even if the user has since changed the EngineSelector
    // or reloaded the page).
    onChatLoaded: ({ provider, model, mode }) => {
      if (provider) runStartProviderRef.current = provider;
      if (model) runStartModelRef.current = model;
      if (mode) runStartModeRef.current = mode;
    },
    setStatus,
    setFinalResult,
    setSessionTimestamp,
    setHistoryTurns,
    setTokensUsed,
    setCurrentUsageMetrics,
    setSteps,
    setChatReasoningText,
    setCurrentStepNum,
    currentStepNumRef,
    fetchJobFiles,
    connectStream,
  });

  const chatScrollBottomRef = useRef<HTMLDivElement | null>(null);

  // --- Effects --------------------------------------------------------------
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onModelChange = (e: any) => {
      if (e.detail?.model) setActiveModelName(e.detail.model);
    };
    window.addEventListener("omweb:model-change", onModelChange);
    return () => window.removeEventListener("omweb:model-change", onModelChange);
  }, [setActiveModelName]);

  useEffect(() => {
    if (status === "running" || steps.length > 0 || submittedPrompt) {
      chatScrollBottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [steps, status, finalResult, submittedPrompt]);

  useEffect(() => {
    const handleSandboxEvent = (e: Event) => {
      const customEvent = e as CustomEvent<{
        code: string;
        language: string;
        filename: string;
      }>;
      if (customEvent.detail) {
        setShowRightPanel(true);
        setSandboxDraft({
          filename: customEvent.detail.filename,
          content: customEvent.detail.code,
        });
      }
    };
    const handleArtifactEvent = (e: Event) => {
      const ce = e as CustomEvent<{
        artifact?: string;
        path?: string;
        file?: string;
      }>;
      const artName = ce.detail?.path || ce.detail?.file || ce.detail?.artifact;
      if (artName) {
        const cat = getFileCategory(artName);
        if (cat === "preview" || cat === "artifacts") {
          setSelectedFileForEditor(artName);
        }
      }
      setShowRightPanel(true);
    };
    window.addEventListener("peldrun:artifact-created", handleArtifactEvent);
    window.addEventListener("peldrun:open-in-sandbox", handleSandboxEvent);
    return () => {
      window.removeEventListener(
        "peldrun:artifact-created",
        handleArtifactEvent
      );
      window.removeEventListener("peldrun:open-in-sandbox", handleSandboxEvent);
    };
  }, [setShowRightPanel]);

  useEffect(() => {
    if (initialJobId) {
      setActiveJobId(initialJobId);
      fetchJobDetails(initialJobId);
    }
  }, [initialJobId, fetchJobDetails]);

  // Listen for file-mention clicks dispatched from the composer. Opens the
  // referenced file in the workspace panel by selecting it and forcing the
  // right-hand panel visible.
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent<{ path?: string; source?: string }>;
      const path = ce.detail?.path;
      if (!path) return;

      setSelectedFileForEditor(path);
      setShowRightPanel(true);
    };
    window.addEventListener("peldrun:open-file-in-workspace", handler);
    return () =>
      window.removeEventListener("peldrun:open-file-in-workspace", handler);
  }, [setShowRightPanel]);

  // --- Mode / engine handlers ----------------------------------------------
  const handleModeChange = useCallback(
    (newMode: "agent" | "chat") => {
      setExecMode(newMode);
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("omweb:mode-change", { detail: newMode })
        );
      }
    },
    [setExecMode]
  );

  const handleSelectEngine = useCallback(
    (engine: EngineOption) => {
      setSelectedEngineId(engine.id);
      handleModeChange(engine.mode);
    },
    [setSelectedEngineId, handleModeChange]
  );

  // --- Timeline helpers -----------------------------------------------------
  const toggleStep = useCallback((key: string) => {
    setExpandedSteps((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  const copyText = useCallback((text: string, identifier: string) => {
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
      setCopiedSection(identifier);
      setTimeout(() => setCopiedSection(null), 2000);
    });
  }, []);

  // --- Session control ------------------------------------------------------
  const handleNewSession = useCallback(() => {
    closeStream();
    setInputValue("");
    setLandingAttachedFiles([]);
    setSubmittedPrompt("");
    setSubmittedFiles([]);
    setActiveJobId(null);
    setActiveChatId(null);
    setHistoryTurns([]);
    setStatus("idle");
    setSteps([]);
    setFinalResult(null);
    setCurrentStepNum(0);
    setProducedFiles([]);
    setSelectedFileForEditor(null);
    setExpandedSteps({});
    setTokensUsed({ input: 0, output: 0, total: 0 });
    setCurrentUsageMetrics(null);
    setHumanQuery(null);
    setHumanAnswer("");
    setElapsedSeconds(0);
    setSessionTimestamp("");
    setShowRightPanel(false);
    setRightPanelMode("workspace");
    setSourceControlDefaultMode("browse");
    setActiveChildRuns({});
    setPendingApprovals({});
    setChatReasoningText("");
    setMcpAuthRequiredPayload(null);
    router.push("/chat");
  }, [
    closeStream,
    router,
    setProducedFiles,
    setElapsedSeconds,
    setShowRightPanel,
  ]);

  // Cleanup on unmount so a queued fallback never fires after the
  // component is gone.
  useEffect(() => {
    return () => {
      if (stopFallbackTimerRef.current !== null) {
        clearTimeout(stopFallbackTimerRef.current);
        stopFallbackTimerRef.current = null;
      }
    };
  }, []);

  const handleStopTask = useCallback(async () => {
    // Only allow Stop while the run is genuinely `running`. `cancelling`
    // means Stop is already in flight (double-click guard); any other
    // state means the run is not active.
    if (!activeJobId || status !== "running") return;

    const jobIdToStop = activeJobId;

    // ─── Layer 1: Immediate visual feedback ─────────────────────────
    //
    // Show "cancelling" the instant the user clicks Stop. This prevents
    // the "frozen UI" perception that appeared when F.4.b removed the
    // optimistic setStatus. The final state ("cancelled") is applied by
    // Layer 2 (SSE event) or Layer 3 (fallback timer).
    setStatus("cancelling");
    setHumanQuery(null);

    // ─── Layer 3: Safety-net timer ──────────────────────────────────
    //
    // If the SSE `cancelled` event does not arrive within 5 seconds
    // (possible when the backend's LLM stream is orphaned — a known
    // backend bug documented in docs/state-contracts.md §11), force
    // the local state to `cancelled` and close the stream locally.
    //
    // The 2s window is:
    //   * Long enough for a healthy SSE round-trip (typical latency:
    //     50–200ms with Phase F.4.b's dispatch_terminal_event drain).
    //   * Short enough that the user does not perceive a hang if the
    //     backend is slow (the LLM cancellation bug in
    //     AsyncLLMClient can delay the SSE "cancelled" beyond 2s, in
    //     which case this fallback fires as the ultimate safety net).
    if (stopFallbackTimerRef.current !== null) {
      clearTimeout(stopFallbackTimerRef.current);
    }
    stopFallbackTimerRef.current = setTimeout(() => {
      stopFallbackTimerRef.current = null;
      // Guard against a stale timer firing after the user has already
      // started a new task, or after the SSE path has already delivered
      // the terminal `cancelled` event.
      setStatus((current) => {
        if (current === "cancelled") return current;
        if (current === "running") return current; // new run started
        console.warn(
          "[ChatContainer] Stop fallback triggered for %s after 2s without SSE cancelled",
          jobIdToStop,
        );
        return "cancelled";
      });
      // Force-close the manager connection for this job so it does not
      // linger if the backend never sends a terminal event.
      try {
        // Local import to avoid a circular top-level dependency
        // (run-stream-manager imports session-store, which imports
        // chatsApi, which is also imported by ChatContainer's parent
        // chain).
        import("@/lib/run-stream-manager")
          .then(({ runStreamManager }) => {
            runStreamManager.disconnect(jobIdToStop);
          })
          .catch(() => {
            /* best-effort */
          });
      } catch {
        /* ignore */
      }
    }, 2000);

    try {
      const res = await fetch(
        `/api/run/jobs/${encodeURIComponent(jobIdToStop)}/stop`,
        { method: "POST" },
      );

      if (!res.ok) {
        // Backend rejected the stop. Revert to running so the user can
        // retry, and cancel the fallback timer.
        console.warn(
          "[ChatContainer] Stop request returned HTTP %d for job %s",
          res.status,
          jobIdToStop,
        );
        if (stopFallbackTimerRef.current !== null) {
          clearTimeout(stopFallbackTimerRef.current);
          stopFallbackTimerRef.current = null;
        }
        setStatus("running");
      }
      // Success: Layer 2 (SSE) or Layer 3 (fallback) will complete the
      // transition to `cancelled`.
    } catch (e) {
      console.error("[ChatContainer] Failed to send stop request:", e);
      // Network failure: cancel the fallback and force `cancelled`
      // immediately. The backend may or may not have received the
      // request — the next reload will reconcile from session.json.
      if (stopFallbackTimerRef.current !== null) {
        clearTimeout(stopFallbackTimerRef.current);
        stopFallbackTimerRef.current = null;
      }
      setStatus("cancelled");
    }
  }, [activeJobId, status]);

  // Pause/Resume handlers were removed in Phase H.
  // Reason: no LLM provider (LM Studio, GigaChat, OpenAI, Anthropic,
  // Ollama) exposes true streaming-pause semantics. The button
  // appeared to work in the UI but the LLM kept generating tokens
  // in the background, causing visual contradiction. Professional
  // tools (ChatGPT, Claude, Cursor, Gemini) offer Stop only.
  // Stop (handleStopTask above) is the single universal control.

  // --- Human-in-the-loop ----------------------------------------------------
  const handleSendHumanAnswer = useCallback(
    async (customAnswer?: string) => {
      const finalAnswer = (
        typeof customAnswer === "string" ? customAnswer : humanAnswer
      ).trim();
      if (!finalAnswer || !activeJobId) return;

      try {
        const res = await fetch(`/api/run/jobs/${activeJobId}/respond`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answer: finalAnswer }),
        });
        if (res.ok) {
          setHumanAnswer("");
          setHumanQuery(null);
        }
      } catch (e) {
        console.error("Failed to send human answer:", e);
      }
    },
    [activeJobId, humanAnswer]
  );

  const handleApproveDelegation = useCallback(
    async (approvalId: string, editedInstruction?: string) => {
      if (!activeJobId) return;
      try {
        await fetch(`/api/run/jobs/${activeJobId}/respond`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            answer: { decision: "approve", instruction: editedInstruction },
          }),
        });
        setPendingApprovals((prev) => {
          const next = { ...prev };
          delete next[approvalId];
          return next;
        });
      } catch (err) {
        console.error("Failed to approve delegation:", err);
      }
    },
    [activeJobId]
  );

  const handleRejectDelegation = useCallback(
    async (approvalId: string) => {
      if (!activeJobId) return;
      try {
        await fetch(`/api/run/jobs/${activeJobId}/respond`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answer: { decision: "reject" } }),
        });
        setPendingApprovals((prev) => {
          const next = { ...prev };
          delete next[approvalId];
          return next;
        });
      } catch (err) {
        console.error("Failed to reject delegation:", err);
      }
    },
    [activeJobId]
  );

  const handleCancelSubtask = useCallback(async (childRunId: string) => {
    try {
      await fetch(`/api/run/jobs/${childRunId}/stop`, { method: "POST" });
      setActiveChildRuns((prev) => {
        if (!prev[childRunId]) return prev;
        return {
          ...prev,
          [childRunId]: { ...prev[childRunId], status: "cancelled" },
        };
      });
    } catch (err) {
      console.error("Failed to cancel subtask:", err);
    }
  }, []);

  // --- Task dispatch --------------------------------------------------------
  const handleStartTask = useCallback(
    async (
      customPrompt?: string,
      customOverride?: any,
      filesToUpload?: File[],
      isAuthRetry: boolean = false
    ): Promise<{ accepted: true; jobId: string; chatId: string }> => {
      const rawText = (
        customPrompt !== undefined ? customPrompt : inputValue
      ).trim();
      if (
        (!rawText && (!filesToUpload || filesToUpload.length === 0)) ||
        status === "running" ||
        status === "cancelling"
      ) {
        throw new Error(
          "Task cannot be started with empty input or during ongoing run."
        );
      }

      let finalPrompt = rawText;
      const effectiveMode = customOverride?.mode || execMode;

      // activeModelName is `string | null` because the storage schema
      // allows "no model selected yet". Indexing metadataVault with a
      // null key throws TS2538, so guard the metadata lookup. The
      // inferModelCapabilities function already handles a null
      // modelKey by returning a neutral capability profile.
      const activeCaps = inferModelCapabilities(
        activeModelName,
        activeModelName ? metadataVault[activeModelName] : null
      );
      const isModelReasoning =
        customOverride?.is_reasoning_model !== undefined
          ? Boolean(customOverride.is_reasoning_model)
          : Boolean(activeCaps?.isReasoning);

      const effectiveReasoningEffort = isModelReasoning
        ? customOverride?.reasoning_effort || reasoningEffort || "none"
        : "none";
      const targetChatId =
        activeChatId ||
        `chat_${Date.now().toString(36)}_${Math.random()
          .toString(36)
          .substring(2, 8)}`;

      let attachedMetadata: AttachedUserFile[] = [];

      if (filesToUpload && filesToUpload.length > 0) {
        attachedMetadata = filesToUpload.map((f) => ({
          name: f.name,
          type: f.type,
          url: f.type.startsWith("image/") ? URL.createObjectURL(f) : undefined,
        }));

        try {
          const formData = new FormData();
          filesToUpload.forEach((f) => formData.append("files", f));
          formData.append("chat_id", targetChatId);
          if (activeJobId) formData.append("job_id", activeJobId);

          const uploadRes = await fetch("/api/files/upload", {
            method: "POST",
            body: formData,
          });
          if (!uploadRes.ok) {
            const uploadErrData = await uploadRes.json().catch(() => ({}));
            throw new Error(
              uploadErrData.detail || "Failed to upload attached files"
            );
          }

          const uploadData = await uploadRes.json();
          const uploadedList: { name: string; path: string }[] =
            uploadData.files || [];
          if (uploadedList.length > 0) {
            const filesSummary = uploadedList
              .map((f) => `- ${f.name} (in workspace directory)`)
              .join("\n");
            finalPrompt = rawText
              ? `${rawText}\n\n[Uploaded Files]:\n${filesSummary}`
              : `Inspect workspace files:\n${filesSummary}`;
          }
        } catch (uploadErr) {
          console.error("[ChatContainer] Failed to upload files:", uploadErr);
          throw uploadErr;
        }
      }


            // [FIX-5.7] Merge file references from `context_refs` into the
      // submitted files list so the UI renders them as chips above the
      // message. The composer already stripped the markers from the
      // prompt text, so this is the sole source of truth for chip display.
      const refsFromOverride: Array<{ relPath?: string; fileName?: string }> =
        Array.isArray(customOverride?.context_refs)
          ? customOverride.context_refs
          : [];

      // [FIX-5.7] Use flatMap instead of map+filter to avoid TypeScript's
      // type predicate limitation: the produced `{ name, path }` object is
      // NOT assignable to a type predicate over `AttachedUserFile` (which
      // declares `path?` as optional). flatMap expresses the same intent
      // without needing a predicate.
      // [FIX-DEDUP] Deduplicate by canonical path. Even though the composer
      // now deduplicates `context_refs` before dispatch, this is a defensive
      // safeguard for older payloads (stale sessionStorage drafts, replays,
      // or third-party integrations). Each file appears exactly once in the
      // chip row, mirroring the backend's own deduplication before content
      // injection.
      const seenRefPaths = new Set<string>();
      const refAttachments: AttachedUserFile[] = [];
      for (const r of refsFromOverride) {
        const relPath = typeof r?.relPath === "string" ? r.relPath : "";
        if (!relPath || seenRefPaths.has(relPath)) continue;
        seenRefPaths.add(relPath);
        const fileName =
          typeof r?.fileName === "string" && r.fileName
            ? r.fileName
            : relPath.split("/").pop() || relPath;
        refAttachments.push({ name: fileName, path: relPath });
      }

      // [FIX-MULTIMODAL] Attach image references selected via the
      // composer's multimodal pipeline. Each entry carries a server-side
      // ``fileId`` assigned by ``POST /api/files/upload-temp``; the
      // binary content never travels through this path. The backend
      // (``start_run.py``) forwards them to the engine as
      // ``llm_override["attachments"]``, where the context builder either
      // embeds them inline (vision-capable model) or routes them through
      // the vision worker (text-only model).
      //
      // The same list is projected into ``AttachedUserFile[]`` here so
      // ``AttachedFileCard`` can render thumbnails from
      // ``/api/files/temp/{fileId}`` — which survives a page reload,
      // unlike a browser-only ``URL.createObjectURL`` handle.
      const attachmentsFromOverride: Array<{
        fileId?: string;
        mimeType?: string;
        fileName?: string;
        width?: number;
        height?: number;
      }> = Array.isArray(customOverride?.attachments)
        ? customOverride.attachments
        : [];

      // Defensive filtering:
      //   * Entries without a string ``fileId`` are dropped — sending
      //     them would land on the backend as null references and the
      //     engine would treat each one as broken.
      //   * Duplicate ``fileId`` values are collapsed so the chip row
      //     (and the backend's own deduplication) never has to handle
      //     the same image twice.
      // The ``fileId`` is used as the fallback display name only when
      // the composer did not supply a filename; the backend still
      // resolves the artifact by ID, never by name.
      const seenAttachmentIds = new Set<string>();
      const imageAttachments: AttachedUserFile[] = [];
      for (const a of attachmentsFromOverride) {
        if (!a || typeof a.fileId !== "string" || !a.fileId) continue;
        if (seenAttachmentIds.has(a.fileId)) continue;
        seenAttachmentIds.add(a.fileId);
        imageAttachments.push({
          name:
            typeof a.fileName === "string" && a.fileName
              ? a.fileName
              : a.fileId,
          fileId: a.fileId,
          mimeType:
            typeof a.mimeType === "string" && a.mimeType
              ? a.mimeType
              : undefined,
          width: typeof a.width === "number" ? a.width : undefined,
          height: typeof a.height === "number" ? a.height : undefined,
        });
      }

      attachedMetadata = [
        ...attachedMetadata,
        ...refAttachments,
        ...imageAttachments,
      ];

      // Archive the current turn before starting a new one.
      //
      // IMPORTANT — provider/model snapshot semantics:
      //
      // The turn being archived represents a run that STARTED with
      // `runStartProviderRef.current` / `runStartModelRef.current`
      // (captured at dispatch time in the previous handleStartTask
      // call). Those refs are the ONLY correct source for the archive,
      // because:
      //
      //   * `activeProvider` / `activeModelName` may have been
      //     changed by the user via EngineSelector WHILE this turn
      //     was running, or between its completion and this new
      //     message.
      //   * The `usage.provider` field was previously hard-coded to
      //     the literal string "local", which silently misrepresented
      //     every cloud / provider-package turn in the UI.
      //
      // After the archive is pushed, the refs are updated to the
      // CURRENT selection (see the bottom of this block) so the next
      // run captures the right values.
      if (submittedPrompt && (finalResult || steps.length > 0)) {
        const archivedProvider = runStartProviderRef.current;
        const archivedModel = runStartModelRef.current;

        setHistoryTurns((prev) => [
          ...prev,
          {
            id: activeJobId || `turn-${Date.now()}`,
            jobId: activeJobId || "",
            prompt: submittedPrompt,
            timestamp: sessionTimestamp || new Date().toLocaleString(),
            steps: [...steps],
            finalResult,
            status: status === "failed" ? "failed" : "completed",
            tokensUsed: { ...tokensUsed },
            producedFiles: [...producedFiles],
            attachedFiles: [...submittedFiles],
            // ChatTurn.model is typed `string | undefined`. Coerce
            // `null` (unselected) to `undefined` so the shape matches
            // the historical contract without forcing a fake default.
            model: archivedModel ?? undefined,
            // Same for provider: keep `undefined` when unknown so
            // HistoricalTurn falls back to the currently-active
            // provider for pre-upgrade turns.
            provider: archivedProvider ?? undefined,
            // Persist the mode for the "Agent" badge and — more
            // importantly — ALWAYS preserve the reasoning text.
            //
            // We intentionally DO NOT gate on
            // `runStartModeRef.current === "chat"`. The `mode` ref
            // can become stale (mode toggled by user right before
            // send), and even a "chat-like" agent turn without tool
            // calls deserves to keep its reasoning visible after
            // archival. HistoricalTurn's display logic is what
            // ultimately decides whether to render the panel:
            //   * has tool calls  → timeline (no reasoning panel)
            //   * no tool calls   → reasoning panel (if text exists)
            mode: runStartModeRef.current,
            chatReasoningText: chatReasoningText || undefined,
            usage:
              tokensUsed.total > 0
                ? ({
                    input_tokens: tokensUsed.input,
                    output_tokens: tokensUsed.output,
                    total_tokens: tokensUsed.total,
                    cost_usd: currentUsageMetrics?.cost_usd ?? 0,
                    latency_ms:
                      currentUsageMetrics?.latency_ms ?? elapsedSeconds * 1000,
                    tokens_per_second:
                      currentUsageMetrics?.tokens_per_second ??
                      (elapsedSeconds > 0
                        ? tokensUsed.output / elapsedSeconds
                        : 0),
                    // Actual provider that produced this turn, not
                    // the current selection. Falls back to "local"
                    // only when no provider was ever captured (very
                    // first turn of a session).
                    provider: archivedProvider ?? "local",
                    model: archivedModel ?? activeModelName ?? "",
                  } as any)
                : undefined,
          },
        ]);
      }

      // Snapshot the CURRENT provider/model/mode for the run we are
      // about to start.
      // We use `effectiveMode` here (not `execMode`) because `execMode`
      // might still hold the previous value if the user just toggled
      // the mode and immediately sent a message. `effectiveMode`
      // represents the exact mode sent to the backend.
      runStartProviderRef.current = activeProvider;
      runStartModelRef.current = activeModelName;
      runStartModeRef.current = effectiveMode as "agent" | "chat";

      setInputValue("");
      setLandingAttachedFiles([]);
      setSubmittedPrompt(finalPrompt);
      setSubmittedFiles(attachedMetadata);
      setStatus("running");
      setSteps([]);
      setFinalResult(null);
      setCurrentStepNum(1);
      setProducedFiles([]);
      setSelectedFileForEditor(null);
      setExpandedSteps({ "active-1": true });
      setHumanQuery(null);
      setTokensUsed({ input: 0, output: 0, total: 0 });
      setCurrentUsageMetrics(null);
      setElapsedSeconds(0);
      setSessionTimestamp(new Date().toLocaleString());
      setActiveChildRuns({});
      setPendingApprovals({});
      setChatReasoningText("");

      try {
        let storedOverride = {};
        if (activeLlmOverride) {
          try {
            storedOverride = JSON.parse(activeLlmOverride);
          } catch (e) {}
        }

        const generativeUiPayload = getGenerativeUIPayload();

        const res = await fetch("/api/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            prompt: finalPrompt,
            max_steps:
              effectiveMode === "agent"
                ? getAgentMaxSteps(selectedAgentId)
                : 1,
            chat_id: targetChatId,
            agent_id: selectedAgentId || "peldrun",
            mode: effectiveMode,
            reasoning_effort: effectiveReasoningEffort,
            is_reasoning_model: isModelReasoning,
            generative_ui: generativeUiPayload,
            ...storedOverride,
            ...(customOverride || {}),
          }),
        });

        if (!res.ok) {
          const errorData = await res.json().catch(() => ({}));
          throw new Error(errorData.detail || "Failed to start run");
        }

        const data = await res.json();
        const jobId = data.job_id;
        const returnedChatId = data.chat_id || targetChatId;
        setActiveChatId(returnedChatId);
        if (typeof window !== "undefined") {
          window.history.replaceState(null, "", `/chat/${returnedChatId}`);
        }
        setActiveJobId(jobId);

        // [Phase F.8-b] Seed the session store directly from the
        // backend's response. start_run.py now returns a `session`
        // object (see Phase F.8), so we can populate the store with
        // the authoritative record (title, project_id, created_at,
        // turns, events, ...) without a follow-up GET /api/chats/{id}.
        //
        // This runs BEFORE `connectStream` so the store contains the
        // real session — `registerRun` (called by `connectStream`)
        // then rebinds the `job_id` and reinforces `status: "running"`.
        // Without this enrichment, the store would carry a minimal
        // stub until the next full refresh.
        if (data.session) {
          useSessionStore.getState().upsertSession(data.session);
        }

        // [Phase F.3.b] Pass chatId and metadata so registerRun can seed
        // both stores before opening the SSE stream. The manager:
        //   1. Calls streamStore.initializeRun(jobId, prompt)
        //   2. Calls sessionStore.registerRun(chatId, jobId, {...})
        //   3. Opens the SSE connection with after_sequence
        // See lib/run-stream-manager.ts registerRun().
        connectStream(jobId, returnedChatId, {
          prompt: finalPrompt,
          title: finalPrompt.slice(0, 40),
          agentId: selectedAgentId,
        });
        return { accepted: true, jobId, chatId: returnedChatId };
      } catch (err: any) {
        console.error("[ChatContainer] Execution dispatch error:", err);
        setStatus("failed");
        setFinalResult(err?.message || "Execution error encountered.");
        throw err;
      }
    },
    [
      inputValue,
      status,
      execMode,
      activeModelName,
      activeProvider,
      metadataVault,
      reasoningEffort,
      activeChatId,
      activeJobId,
      submittedPrompt,
      finalResult,
      steps,
      sessionTimestamp,
      tokensUsed,
      producedFiles,
      submittedFiles,
      currentUsageMetrics,
      elapsedSeconds,
      activeLlmOverride,
      getAgentMaxSteps,
      selectedAgentId,
      connectStream,
      setProducedFiles,
      setElapsedSeconds,
      // Required so handleStartTask reads the LATEST reasoning text
      // when archiving the previous turn. Without it, the closure
      // captures "" from the initial state and every archived turn
      // ends up with an empty reasoning panel.
      chatReasoningText,
    ]
  );

  // --- MCP Authentication Handlers ------------------------------------------
  //
  // Flow recap:
  //   1. Backend emits `mcp_auth_required` with a per-server payload.
  //   2. use-sse-stream forwards it via setMcpAuthRequiredPayload and
  //      closes the stream. The job is already failed server-side.
  //   3. The modal records the user's decision via one of the three
  //      /mcp-auth endpoints (or completes an OAuth flow in a popup).
  //   4. onSuccess fires: we clear the payload and re-issue
  //      POST /api/run with the SAME prompt. The `activeChatId` is
  //      preserved, so the backend picks up the newly-recorded
  //      credentials and the run proceeds normally.
  //
  // If the user dismisses the modal without deciding, onClose fires:
  // we clear the payload and leave the UI on the failed turn, whose
  // finalResult already explains that authentication is required.
  const handleMcpAuthSuccess = useCallback(() => {
    const promptToRetry = submittedPrompt;
    setMcpAuthRequiredPayload(null);

    if (!promptToRetry) {
      showToast.error(
        "Retry Failed",
        "The original prompt is no longer available. Please re-send it."
      );
      return;
    }

    // Delegate to the normal dispatch path. handleStartTask archives
    // the current (failed-auth) turn into historyTurns, resets the
    // current-run state, and issues POST /api/run against the same
    // chat_id. Its returned promise is intentionally not awaited here:
    // the modal has already closed, and any failure surfaces through
    // the toast below.
    handleStartTask(promptToRetry).catch((err: any) => {
      console.error("[ChatContainer] MCP auth retry failed:", err);
      showToast.error(
        "Retry Failed",
        err?.message || "Could not resume the task after authentication."
      );
    });
  }, [submittedPrompt, handleStartTask]);

  const handleMcpAuthClose = useCallback(() => {
    setMcpAuthRequiredPayload(null);
  }, []);

  // --- Turn Regeneration Handler --------------------------------------------
  const handleRegenerate = useCallback(
    async (turnIndex: number, turn?: ChatTurn) => {
      if (status === "running" || status === "cancelling") return;

      if (turnIndex === -1 || turnIndex === undefined) {
        // Regenerating the current/latest completed turn
        const promptToRerun = submittedPrompt;
        if (!promptToRerun) return;

        setStatus("running");
        setSteps([]);
        setFinalResult(null);
        setCurrentStepNum(1);
        setProducedFiles([]);
        setSelectedFileForEditor(null);
        setExpandedSteps({ "active-1": true });
        setHumanQuery(null);
        setTokensUsed({ input: 0, output: 0, total: 0 });
        setCurrentUsageMetrics(null);
        setElapsedSeconds(0);
        setSessionTimestamp(new Date().toLocaleString());
        setActiveChildRuns({});
        setPendingApprovals({});
        setChatReasoningText("");

        // Regeneration is a NEW run — it captures the CURRENT
        // provider/model selection, not the snapshot from the turn
        // being regenerated. This matches the user's mental model
        // ("run the same prompt again with the settings I have
        // selected NOW") and mirrors handleStartTask's behavior.
        runStartProviderRef.current = activeProvider;
        runStartModelRef.current = activeModelName;
        runStartModeRef.current = execMode;

        const targetChatId = activeChatId;
        const effectiveMode = execMode;

        try {
          let storedOverride = {};
          if (activeLlmOverride) {
            try {
              storedOverride = JSON.parse(activeLlmOverride);
            } catch (e) {}
          }

          const generativeUiPayload = getGenerativeUIPayload();

          const res = await fetch("/api/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              prompt: promptToRerun,
              max_steps:
                effectiveMode === "agent"
                  ? getAgentMaxSteps(selectedAgentId)
                  : 1,
              chat_id: targetChatId,
              agent_id: selectedAgentId || "peldrun",
              mode: effectiveMode,
              regenerate_turn_id: "latest",
              generative_ui: generativeUiPayload,
              ...storedOverride,
            }),
          });

          if (!res.ok) {
            const errorData = await res.json().catch(() => ({}));
            throw new Error(errorData.detail || "Failed to regenerate turn");
          }

          const data = await res.json();
          const jobId = data.job_id;
          setActiveJobId(jobId);
          // [Phase F.3.b] Pass the chat context so registerRun can seed
          // the stores correctly. targetChatId is the current session's
          // chat_id; the fallback to "" is defensive (the manager treats
          // an empty chatId as invalid and logs a warning).
          connectStream(jobId, targetChatId || "", {
            prompt: promptToRerun,
            title: promptToRerun.slice(0, 40),
            agentId: selectedAgentId,
          });
        } catch (err: any) {
          console.error("[ChatContainer] Regeneration dispatch error:", err);
          setStatus("failed");
          setFinalResult(err?.message || "Regeneration failed.");
        }
      } else {
        // Regenerating a historical turn: truncate subsequent history to branch
        const targetTurn = turn || historyTurns[turnIndex];
        if (!targetTurn) return;

        const promptToRerun = targetTurn.prompt;
        const targetTurnId = targetTurn.jobId || String(turnIndex);

        setHistoryTurns((prev) => prev.slice(0, turnIndex));
        setSubmittedPrompt(promptToRerun);
        setSubmittedFiles(targetTurn.attachedFiles || []);
        setStatus("running");
        setSteps([]);
        setFinalResult(null);
        setCurrentStepNum(1);
        setProducedFiles([]);
        setSelectedFileForEditor(null);
        setExpandedSteps({ "active-1": true });
        setHumanQuery(null);
        setTokensUsed({ input: 0, output: 0, total: 0 });
        setCurrentUsageMetrics(null);
        setElapsedSeconds(0);
        setSessionTimestamp(new Date().toLocaleString());
        setActiveChildRuns({});
        setPendingApprovals({});
        setChatReasoningText("");

        // Same rationale as the latest-turn branch above: regeneration
        // uses the CURRENT provider/model selection, not the stored
        // snapshot from the turn being regenerated. The mode is the
        // only field preserved from the original turn (targetTurn.mode).
        runStartProviderRef.current = activeProvider;
        runStartModelRef.current = activeModelName;
        runStartModeRef.current = targetTurn.mode || execMode;

        const targetChatId = activeChatId;
        const effectiveMode = targetTurn.mode || execMode;

        try {
          let storedOverride = {};
          if (activeLlmOverride) {
            try {
              storedOverride = JSON.parse(activeLlmOverride);
            } catch (e) {}
          }

          const generativeUiPayload = getGenerativeUIPayload();

          const res = await fetch("/api/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              prompt: promptToRerun,
              max_steps:
                effectiveMode === "agent"
                  ? getAgentMaxSteps(selectedAgentId)
                  : 1,
              chat_id: targetChatId,
              agent_id: selectedAgentId || "peldrun",
              mode: effectiveMode,
              regenerate_turn_id: targetTurnId,
              generative_ui: generativeUiPayload,
              ...storedOverride,
            }),
          });

          if (!res.ok) {
            const errorData = await res.json().catch(() => ({}));
            throw new Error(errorData.detail || "Failed to regenerate turn");
          }

          const data = await res.json();
          const jobId = data.job_id;
          setActiveJobId(jobId);
          // [Phase F.3.b] Same as the current-turn branch above.
          connectStream(jobId, targetChatId || "", {
            prompt: promptToRerun,
            title: promptToRerun.slice(0, 40),
            agentId: selectedAgentId,
          });
        } catch (err: any) {
          console.error("[ChatContainer] Historical turn regeneration error:", err);
          setStatus("failed");
          setFinalResult(err?.message || "Regeneration failed.");
        }
      }
    },
    [
      status,
      submittedPrompt,
      activeChatId,
      execMode,
      activeLlmOverride,
      getAgentMaxSteps,
      selectedAgentId,
      historyTurns,
      connectStream,
      setProducedFiles,
      setElapsedSeconds,
      // Provider / model snapshots — required because we read them
      // synchronously when capturing `runStartProviderRef` /
      // `runStartModelRef` at the start of each regeneration.
      activeProvider,
      activeModelName,
    ]
  );

  // --- Derived values -------------------------------------------------------
  const isFreshSession =
    historyTurns.length === 0 &&
    steps.length === 0 &&
    !submittedPrompt &&
    status !== "running" &&
    status !== "cancelling";

  const getLiveStatusMessage = useCallback(() => {
    if (execMode === "chat") return "Synthesizing conversational response...";
    if (steps.length === 0)
      return "Analyzing request and formulating execution plan...";
    const lastEvt = steps[steps.length - 1];
    if (lastEvt.type === "tool_call")
      return `Executing tool: ${lastEvt.toolName || "external tool"}...`;
    if (lastEvt.type === "observation")
      return "Processing tool observation & planning next action...";
    if (lastEvt.type === "thought")
      return "Deep reasoning and verifying solution...";
    return "Agent reasoning & executing autonomously...";
    // `status` used to be in the deps array because the pre-Phase-H
    // version returned "Execution paused..." for the paused state.
    // That branch is gone, so `status` is no longer read here.
  }, [execMode, steps]);

  const currentCostUsd = useMemo(() => {
    return currentUsageMetrics?.cost_usd ?? 0;
  }, [currentUsageMetrics]);

  const currentLatencyMs = useMemo(() => {
    if (
      currentUsageMetrics?.latency_ms &&
      currentUsageMetrics.latency_ms > 0
    ) {
      return currentUsageMetrics.latency_ms;
    }
    return elapsedSeconds > 0 ? elapsedSeconds * 1000 : null;
  }, [currentUsageMetrics, elapsedSeconds]);

  const currentTokensPerSecond = useMemo(() => {
    if (
      currentUsageMetrics?.tokens_per_second &&
      currentUsageMetrics.tokens_per_second > 0
    ) {
      return currentUsageMetrics.tokens_per_second;
    }
    if (elapsedSeconds > 0 && tokensUsed.output > 0) {
      return Number((tokensUsed.output / elapsedSeconds).toFixed(1));
    }
    return null;
  }, [currentUsageMetrics, elapsedSeconds, tokensUsed.output]);

  // Narrow the widened ChatStatus down to what ChatThread actually
  // distinguishes. After Phase H (Pause removal), "paused" is no
  // longer a valid status, so the adapter is much simpler:
  //
  //   pending/queued/starting/retrying → "running"  (active work — spinner)
  //   everything else                  → passthrough
  //
  // ChatThread only branches on `status === "running"` (live spinner)
  // and `status === "failed"` (deliverable styling). All other values
  // — including waiting_for_input, cancelling, cancelled, interrupted,
  // completed — fall through to the "not running, not failed" branch,
  // which is exactly what those states need.
  //
  // `threadStatus` is now typed as `ChatStatus` (from ./types), which
  // matches `ChatThreadProps.status` after Phase H's type widening.
  const threadStatus: ChatStatus =
    status === "pending" ||
    status === "queued" ||
    status === "starting" ||
    status === "retrying"
      ? "running"
      : status;

  // --- Render ---------------------------------------------------------------
  return (
    <div className="flex h-full w-full bg-background text-foreground overflow-hidden font-sans">
      <div className="flex-1 flex flex-col h-full border-r border-border min-w-0 transition-all">
        <ChatSubHeader
          isFreshSession={isFreshSession}
          submittedPrompt={submittedPrompt}
          activeChatId={activeChatId}
          activeJobId={activeJobId}
          historyTurnsCount={historyTurns.length}
          tokensUsed={tokensUsed}
          currentStepNum={currentStepNum}
          maxSteps={getAgentMaxSteps(selectedAgentId)}
          execMode={execMode}
          selectedEngineId={selectedEngineId}
          onSelectEngine={handleSelectEngine}
          onNewSession={handleNewSession}
          onStopTask={handleStopTask}
          showRightPanel={
            showRightPanel && rightPanelMode === "workspace"
          }
          onToggleRightPanel={() => {
            // The toggle has three states:
            //   1. Workspace panel is open      → hide it.
            //   2. Source Control panel is open → switch to Workspace.
            //   3. Nothing is open              → open Workspace.
            if (showRightPanel && rightPanelMode === "workspace") {
              setShowRightPanel(false);
            } else {
              setRightPanelMode("workspace");
              setShowRightPanel(true);
            }
          }}
          showSourceControlPanel={
            sourceControlEnabled &&
            showRightPanel &&
            rightPanelMode === "source-control"
          }
          onToggleSourceControlPanel={
            sourceControlEnabled
              ? () => {
                  if (
                    showRightPanel &&
                    rightPanelMode === "source-control"
                  ) {
                    setShowRightPanel(false);
                  } else {
                    if (
                      !showRightPanel ||
                      rightPanelMode !== "source-control"
                    ) {
                      setSourceControlDefaultMode("browse");
                      setSourceControlMountKey((k) => k + 1);
                    }
                    setRightPanelMode("source-control");
                    setShowRightPanel(true);
                  }
                }
              : undefined
          }
          status={status}
          activeModel={activeModelName}
        />

        {isFreshSession ? (
          <ChatLanding
            inputValue={inputValue}
            setInputValue={setInputValue}
            execMode={execMode}
            handleModeChange={handleModeChange}
            landingAttachedFiles={landingAttachedFiles}
            setLandingAttachedFiles={setLandingAttachedFiles}
            onStartTask={handleStartTask}
            formatFileSize={formatFileSize}
          />
        ) : (
          <ChatThread
            historyTurns={historyTurns}
            chatId={activeChatId || undefined}
            submittedPrompt={submittedPrompt}
            submittedFiles={submittedFiles}
            sessionTimestamp={sessionTimestamp}
            humanQuery={humanQuery}
            humanAnswer={humanAnswer}
            setHumanAnswer={setHumanAnswer}
            onSendHumanAnswer={handleSendHumanAnswer}
            // Use the mode of the LAST RUN (not the user's current
            // selection) to render the current turn. This ensures the
            // Agent timeline / Chat reasoning panel stay attached to
            // the turn that actually produced them, even after the
            // user toggles the mode for the next message.
            execMode={runStartModeRef.current}
            activeGroupedSteps={
              runStartModeRef.current === "agent" ? groupStepEvents(steps) : {}
            }
            chatReasoningText={
              runStartModeRef.current === "chat" ? chatReasoningText : ""
            }
            currentStepNum={currentStepNum}
            status={threadStatus}
            elapsedSeconds={elapsedSeconds}
            finalResult={finalResult}
            producedFiles={producedFiles}
            copiedSection={copiedSection}
            copyText={copyText}
            expandedSteps={expandedSteps}
            toggleStep={toggleStep}
            onSelectFile={(f: string) => {
              setSelectedFileForEditor(f);
              setShowRightPanel(true);
              const targetTab = getFileCategory(f);
              if (typeof window !== "undefined") {
                window.dispatchEvent(
                  new CustomEvent("peldrun:switch-tab", {
                    detail: { tab: targetTab, file: f, userInitiated: true },
                  })
                );
              }
            }}
            showRawTrace={showRawTrace}
            setShowRawTrace={setShowRawTrace}
            getLiveStatusMessage={getLiveStatusMessage}
            // ChatThreadProps.activeModelName is `string | undefined`
            // (optional). Coerce null to undefined at the boundary.
            activeModelName={activeModelName ?? undefined}
            // `activeProvider` / `activeModelName` = the user's CURRENT
            // EngineSelector selection. Used by HistoricalTurn as the
            // fallback for pre-upgrade turns that lack a snapshot.
            activeProvider={activeProvider ?? undefined}
            // `currentTurnProvider` / `currentTurnModel` = the snapshot
            // captured when the CURRENT run started. Used by
            // ChatThread's current turn header, so switching the
            // EngineSelector mid-turn does NOT relabel the turn that is
            // already displayed.
            currentTurnProvider={runStartProviderRef.current ?? undefined}
            currentTurnModel={runStartModelRef.current ?? undefined}
            chatScrollBottomRef={chatScrollBottomRef}
            tokensUsed={tokensUsed}
            costUsd={currentCostUsd}
            latencyMs={currentLatencyMs}
            tokensPerSecond={currentTokensPerSecond}
            activeChildRuns={activeChildRuns}
            pendingApprovals={pendingApprovals}
            onApproveDelegation={handleApproveDelegation}
            onRejectDelegation={handleRejectDelegation}
            onCancelSubtask={handleCancelSubtask}
            onRegenerate={handleRegenerate}
            onOpenReview={handleOpenReview}
          />
        )}

        {!isFreshSession && (
          <div className="p-3 sm:p-4 bg-background shrink-0">
            <Composer
              chatId={activeChatId || undefined}
              onSend={(textToSend, files, llmOverride) =>
                handleStartTask(textToSend, llmOverride, files)
              }
              onStop={handleStopTask}
              // Treat `cancelling` the same as `running`/`paused` for
              // the composer state: block input and keep the Stop
              // button visible. This prevents the user from firing a
              // new task while a cancellation is still in flight.
              isRunning={
                status === "running" ||
                status === "cancelling"
              }
              disabled={
                status === "running" ||
                status === "cancelling"
              }
            />
          </div>
        )}
      </div>

      {showRightPanel && (
        <div className="flex-1 h-full min-w-0 transition-all">
          {sourceControlEnabled &&
          rightPanelMode === "source-control" ? (
            <SourceControlWorkspacePanel
              key={sourceControlMountKey}
              activeChatId={activeChatId || activeJobId}
              defaultMode={sourceControlDefaultMode}
              onClose={() => setShowRightPanel(false)}
            />
          ) : (
            <WorkspacePanel
              activeJobId={activeJobId}
              activeChatId={activeChatId || activeJobId}
              overrideFile={selectedFileForEditor}
              overrideDraft={sandboxDraft}
            />
          )}
        </div>
      )}

      {/* MCP per-chat authentication modal.
       *
       * The wrapper is a `fixed inset-0` overlay, so its position in the
       * DOM tree does not affect layout. We render it as the last child
       * of the root flex container to keep the sibling tree tidy.
       *
       * The `activeChatId` guard is defensive: in practice the backend
       * sets chat_id on every successful POST /api/run, so SSE for an
       * in-flight job always reaches us with a valid chat. But typing
       * requires the explicit string guard, and if the invariant ever
       * regressed we would simply not open the modal rather than crash
       * with a null prop. */}
      {activeChatId && mcpAuthRequiredPayload && (
        <MCPAuthModal
          open={!!mcpAuthRequiredPayload}
          payload={mcpAuthRequiredPayload}
          chatId={activeChatId}
          onClose={handleMcpAuthClose}
          onSuccess={handleMcpAuthSuccess}
        />
      )}
    </div>
  );
}

export default ChatContainer;
