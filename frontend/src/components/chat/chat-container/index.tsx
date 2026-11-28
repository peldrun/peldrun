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
import { Loader2 } from "lucide-react";
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

// Lazy-load the Project workspace panel for the same reasons as the
// Source Control panel: legacy chat routes (no `projectId`) never need
// it, and it reads from browser storage via `useWorkspaceResolver`, so
// it must not run during SSR (would cause a hydration mismatch).
//
// Only mounted when `projectId` is provided, in the exact same slot
// the legacy `WorkspacePanel` occupies — zero UI difference beyond
// the scoping.
const ProjectWorkspacePanel = dynamic(
  () =>
    import("@/components/workspace/project-workspace-panel").then(
      (m) => m.ProjectWorkspacePanel
    ),
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
// Used to resolve `initialChatId` → `job_id` on mount when the user
// lands on `/projects/[projectId]/chat/[chatId]` directly (via sidebar,
// bookmark, or browser refresh). The lookup is project-scoped when
// `projectId` is present so we never scan the global chat list on
// large deployments.
//
// `ChatSession` is imported alongside `fetchChats` so the resolution
// callback can be typed explicitly, guaranteeing that future signature
// changes to `fetchChats` surface as compile-time errors at this call
// site rather than as silent `any` narrowing.
import { fetchChats, type ChatSession } from "@/lib/chatsApi";

import { getGenerativeUIPayload } from "@/components/generative-ui/store";

import type {
  ChatContainerProps,
  ChatStatus,
  TokenUsage,
  UsageMetrics,
  ChildRunState,
  DelegationApprovalRequest,
  ProducedFile,
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

function normalizeDeliverableRecords(input: any): ProducedFile[] {
  const rows: any[] = Array.isArray(input)
    ? input
    : Array.isArray(input?.files)
      ? input.files
      : Array.isArray(input?.produced_files)
        ? input.produced_files
        : Array.isArray(input?.producedFiles)
          ? input.producedFiles
          : Array.isArray(input?.artifacts)
            ? input.artifacts
            : Array.isArray(input?.deliverables)
              ? input.deliverables
              : [];

  const seen = new Set<string>();
  const out: ProducedFile[] = [];
  for (const entry of rows) {
    const rawPath =
      typeof entry === "string"
        ? entry
        : entry?.path ||
          entry?.relative_path ||
          entry?.rel_root ||
          entry?.name ||
          entry?.filename ||
          "";
    if (!rawPath || typeof rawPath !== "string") continue;
    const cleanPath = rawPath.replace(/\\/g, "/").replace(/^\/+/, "");
    if (!cleanPath) continue;
    if (cleanPath.split("/").some((seg) => seg.startsWith("."))) continue;
    if (seen.has(cleanPath)) continue;
    seen.add(cleanPath);
    out.push({ name: cleanPath.split("/").pop() || cleanPath, path: cleanPath });
  }
  return out;
}

function extractSessionDeliverables(payload: any): ProducedFile[] {
  const session = payload?.chat || payload?.session || payload || {};
  const turns = Array.isArray(session?.turns)
    ? session.turns
    : Array.isArray(payload?.turns)
      ? payload.turns
      : [];
  const lastTurn = turns.length > 0 ? turns[turns.length - 1] : null;
  const fromTurn = normalizeDeliverableRecords(
    lastTurn?.produced_files ??
      lastTurn?.producedFiles ??
      lastTurn?.deliverables ??
      lastTurn?.artifacts ??
      lastTurn?.files ??
      []
  );
  if (fromTurn.length > 0) return fromTurn;
  return normalizeDeliverableRecords(session);
}

function mergeDeliverableLists(...lists: ProducedFile[][]): ProducedFile[] {
  const seen = new Set<string>();
  const out: ProducedFile[] = [];
  for (const list of lists) {
    for (const file of list || []) {
      const key = file?.path || file?.name;
      if (!key || seen.has(key)) continue;
      seen.add(key);
      out.push(file);
    }
  }
  return out;
}

export type { ChatContainerProps };

export function ChatContainer({
  initialJobId,
  initialChatId,
  projectId,
}: ChatContainerProps) {
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

  // True while the initial-chat resolution effect is running. Prevents
  // ChatLanding from flashing on F5 for an existing chat, and gates the
  // composer so the user cannot fire a send before activeChatId is set.
  //
  // Starts as `true` only when we actually have a chat to resolve
  // (initialChatId present AND no initialJobId). When both are absent
  // (the `/chat/new` route), this stays `false` so ChatLanding renders
  // immediately.
  const [isResolvingChat, setIsResolvingChat] = useState<boolean>(
    Boolean(initialChatId && !initialJobId)
  );

  // Tracks which chat id has already been resolved in this mount cycle.
  // Prevents the effect from re-fetching the same chat every time an
  // unrelated dependency (activeChatId, activeJobId) changes.
  //
  // Reset to null whenever activeChatId returns to null (user clicked
  // "New Session"), so re-navigating to the same chat later re-resolves
  // it fresh.
  const resolvedChatIdRef = useRef<string | null>(null);
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

  // Project-scoped deliverables captured from artifact events.
  //
  // useJobFiles polls /api/run/jobs/{jobId}/files, which lists the
  // per-chat session directory. In project mode the agent writes into
  // the project workspace instead, so that listing comes back empty and
  // the "Task Deliverables & Output Files" card never renders. Artifact
  // events carry the exact workspace-relative path of every generated
  // file, so accumulating them restores the card with correct per-run
  // semantics: only this run's output, never the whole workspace.
  const [projectDeliverables, setProjectDeliverables] =
    useState<ProducedFile[]>([]);

  // Reset the accumulator whenever a different run becomes active, so
  // each turn's card shows only its own output. Artifact events arrive
  // over SSE, which is connected AFTER activeJobId is set, so this
  // always runs before the first event of a new run.
  useEffect(() => {
    // Only clear when a run is actually active. On F5 the job id
    // transitions null -> resolved while status is still idle/completed;
    // clearing there would wipe the list restored from storage before it
    // ever renders. A real new run always sets an active status first.
    if (
      status === "running" ||
      status === "pending" ||
      status === "queued" ||
      status === "starting" ||
      status === "retrying"
    ) {
      setProjectDeliverables([]);
    }
  }, [activeJobId, status]);

  // Per-run storage key. Survives F5 in the same tab; the backend
  // hydration below covers a fresh tab or cleared storage.
  const deliverableStorageKey = useMemo(() => {
    if (!projectId) return null;
    const scope = activeJobId || activeChatId;
    return scope ? `peldrun:project-deliverables:${projectId}:${scope}` : null;
  }, [projectId, activeJobId, activeChatId]);

  useEffect(() => {
    if (!deliverableStorageKey || typeof window === "undefined") return;
    if (status === "running" || status === "cancelling") return;
    try {
      const raw = window.sessionStorage.getItem(deliverableStorageKey);
      if (!raw) return;
      const restored = normalizeDeliverableRecords(JSON.parse(raw));
      if (restored.length > 0) {
        setProjectDeliverables((prev) => mergeDeliverableLists(prev, restored));
      }
    } catch {
      // Corrupt storage is non-fatal; backend hydration still runs.
    }
  }, [deliverableStorageKey, status]);

  // Merge chat-scoped and project-scoped deliverables, de-duplicated by
  // path. Legacy chats return `producedFiles` untouched (zero behavior
  // change); projects fall back to the event-derived list whenever the
  // job-files listing is empty.
  const effectiveProducedFiles = useMemo(() => {
    if (!projectId || projectDeliverables.length === 0) return producedFiles;
    const seen = new Set(producedFiles.map((f) => f.path || f.name));
    const merged = [...producedFiles];
    for (const f of projectDeliverables) {
      const key = f.path || f.name;
      if (!key || seen.has(key)) continue;
      seen.add(key);
      merged.push(f);
    }
    return merged;
  }, [projectId, producedFiles, projectDeliverables]);

  // Persist the merged list so a same-tab refresh restores the card
  // instantly, without waiting for any network round-trip.
  useEffect(() => {
    if (!deliverableStorageKey || typeof window === "undefined") return;
    try {
      if (effectiveProducedFiles.length > 0) {
        window.sessionStorage.setItem(
          deliverableStorageKey,
          JSON.stringify(effectiveProducedFiles)
        );
      } else {
        window.sessionStorage.removeItem(deliverableStorageKey);
      }
    } catch {
      // Best-effort; the card still renders from live state.
    }
  }, [deliverableStorageKey, effectiveProducedFiles]);

  // Backend hydration for a fresh tab / cleared storage. Tries the
  // persisted session first (per-turn semantics), then project artifacts
  // scoped to this session, and only as a last resort the workspace
  // listing. Skips entirely while a run is active or when the list is
  // already populated, so it never fights the live capture.
  useEffect(() => {
    if (!projectId || (!activeChatId && !activeJobId)) return;
    if (status === "running" || status === "cancelling") return;
    if (effectiveProducedFiles.length > 0) return;
    if (!finalResult && steps.length === 0 && historyTurns.length === 0) return;

    let cancelled = false;
    const scope = activeChatId || activeJobId || "";

    const hydrate = async () => {
      let collected: ProducedFile[] = [];

      if (activeChatId) {
        try {
          const res = await fetch(`/api/chats/${encodeURIComponent(activeChatId)}`, { cache: "no-store" });
          if (res.ok) collected = mergeDeliverableLists(collected, extractSessionDeliverables(await res.json()));
        } catch { /* fall through */ }
      }

      if (collected.length === 0 && activeJobId) {
        try {
          const res = await fetch(`/api/run/jobs/${encodeURIComponent(activeJobId)}`, { cache: "no-store" });
          if (res.ok) collected = mergeDeliverableLists(collected, extractSessionDeliverables(await res.json()));
        } catch { /* fall through */ }
      }

      if (collected.length === 0) {
        try {
          const res = await fetch(
            `/api/projects/${encodeURIComponent(projectId)}/artifacts?session_id=${encodeURIComponent(scope)}`,
            { cache: "no-store" }
          );
          if (res.ok) {
            const data = await res.json();
            const rows = Array.isArray(data) ? data : data?.artifacts || data?.files || [];
            const scoped = Array.isArray(rows)
              ? rows.filter((r: any) => {
                  const sid = r?.session_id || r?.chat_id;
                  const jid = r?.job_id;
                  if (sid && sid !== activeChatId && sid !== activeJobId) return false;
                  if (jid && activeJobId && jid !== activeJobId) return false;
                  return true;
                })
              : [];
            collected = mergeDeliverableLists(collected, normalizeDeliverableRecords(scoped));
          }
        } catch { /* fall through */ }
      }

      // Do not use the project workspace listing as a deliverables fallback.
      // It contains files from other turns and chats in the shared workspace.

      if (!cancelled && collected.length > 0) {
        setProjectDeliverables((prev) => mergeDeliverableLists(prev, collected));
      }
    };

    hydrate();
    return () => { cancelled = true; };
  }, [
    projectId,
    activeChatId,
    activeJobId,
    status,
    finalResult,
    steps.length,
    historyTurns.length,
    effectiveProducedFiles.length,
  ]);

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

  // Guards `handleStartTask` against concurrent sends. `status` is a
  // closure snapshot, so two sends in the same tick (double Enter) or a
  // send during the attachment-upload await both still see "completed"
  // and both pass the status guard. A ref flips synchronously.
  const sendLockRef = useRef(false);
  // Shared "last intent wins" counter consumed by useJobDetails. Each
  // hydration captures the current value as its epoch; a newer send /
  // regenerate / new-session bumps it, invalidating any in-flight
  // snapshot. See use-job-details.ts §4.3b.
  const hydrationEpochRef = useRef(0);
  // Tracks the most recent `initialJobId` already hydrated so the
  // hydration effect cannot re-fire for the same id when its
  // dependencies churn (see I4b).
  const resolvedJobIdRef = useRef<string | null>(null);

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
    // The mode the CURRENT RUN started with (same source ChatThread
    // renders by). Using the live selector here sent reasoning to the
    // wrong place when the user toggled the mode mid-run.
    execMode: runStartModeRef.current,
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
    // Shared "last intent wins" counter (see use-job-details.ts §4.3).
    hydrationEpochRef,
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
        relative_path?: string;
        file?: string;
      }>;
      const artName =
        ce.detail?.relative_path ||
        ce.detail?.path ||
        ce.detail?.file ||
        ce.detail?.artifact;
      if (artName) {
        const cat = getFileCategory(artName);
        if (cat === "preview" || cat === "artifacts") {
          setSelectedFileForEditor(artName);
        }
      }
      // Project mode: record the deliverable so the output-files card
      // renders even though the chat-scoped job listing is empty.
      if (artName && projectId) {
        const cleanPath = artName.replace(/\\/g, "/").replace(/^\/+/, "");
        // Skip workspace internals (.git objects, caches, dotfiles) —
        // mirrors the filter ChatDeliverable applies when rendering.
        const isInternal = cleanPath
          .split("/")
          .some((seg) => seg.startsWith("."));
        if (cleanPath && !isInternal) {
          setProjectDeliverables((prev) =>
            prev.some((f) => f.path === cleanPath)
              ? prev
              : [
                  ...prev,
                  {
                    name: cleanPath.split("/").pop() || cleanPath,
                    path: cleanPath,
                  },
                ]
          );
        }
      }
      setShowRightPanel(true);
    };
    window.addEventListener("peldrun:artifact-created", handleArtifactEvent);
    window.addEventListener("peldrun:artifact-updated", handleArtifactEvent);
    window.addEventListener("peldrun:open-in-sandbox", handleSandboxEvent);
    return () => {
      window.removeEventListener(
        "peldrun:artifact-created",
        handleArtifactEvent
      );
      window.removeEventListener(
        "peldrun:artifact-updated",
        handleArtifactEvent
      );
      window.removeEventListener("peldrun:open-in-sandbox", handleSandboxEvent);
    };
  }, [setShowRightPanel, projectId]);

  // Hydration entry point for the chat container.
  //
  // Precedence:
  //   1. `initialJobId` — legacy `/chat/[jobId]` route. Used as-is.
  //   2. `initialChatId` — new `/projects/[projectId]/chat/[chatId]`
  //      route. Resolved to its `job_id` via `GET /api/chats`, then
  //      fed to `fetchJobDetails` for full hydration (history, steps,
  //      tokens, artifacts).
  //
  // The `initialChatId` lookup is scoped to `projectId` when present
  // so we never scan the global chat list on large deployments.
  //
  // If the chat is not found in the list, this is one of two cases:
  //   a. The chat was just created client-side and has not yet been
  //      persisted by the backend (user clicked "+ New Chat" in the
  //      sidebar, then landed on this URL).
  //   b. The chat was deleted concurrently from another tab.
  //
  // In both cases we set `activeChatId` to the requested id so that
  // the next POST /api/run targets the correct chat session. The user
  // sees a fresh session in case (a), and will be re-synced from the
  // backend the next time they send a message in case (b).
  // Reset the resolved-chat marker whenever activeChatId goes back to
  // null (via handleNewSession). Otherwise re-navigating to the same
  // chat id would be silently skipped by the effect below.
  useEffect(() => {
    if (activeChatId === null) {
      resolvedChatIdRef.current = null;
      resolvedJobIdRef.current = null;
    }
  }, [activeChatId]);

  useEffect(() => {
    // ── Legacy path: explicit jobId ──
    if (initialJobId) {
      // Hydrate at most once per id, in this mount cycle. The
      // surrounding effect lists `activeChatId` / `activeJobId` as
      // dependencies; without this guard, every such change would
      // re-fetch and overwrite the live turn with a stale snapshot.
      if (resolvedJobIdRef.current === initialJobId) return;
      resolvedJobIdRef.current = initialJobId;
      setActiveJobId(initialJobId);
      fetchJobDetails(initialJobId);
      return;
    }

    // ── Fresh session (no chatId at all) ──
    if (!initialChatId) {
      setIsResolvingChat(false);
      return;
    }

    // ── Just created this chat ourselves (post-send URL rewrite) ──
    // handleStartTask sets activeChatId + activeJobId BEFORE
    // window.history.replaceState rewrites the URL. When the prop
    // change reaches this effect, re-fetching would clobber the
    // live SSE state with a backend snapshot that may be behind
    // the stream. Detect this exact case and skip.
    if (activeChatId === initialChatId && activeJobId) {
      resolvedChatIdRef.current = initialChatId;
      setIsResolvingChat(false);
      return;
    }

    // ── Already resolved this chat in this mount cycle ──
    if (resolvedChatIdRef.current === initialChatId) {
      return;
    }

    // ── Full resolution: F5, direct nav, or switching chats ──
    // Close any stream from a previous chat before loading the new one.
    try {
      closeStream();
    } catch {
      /* non-fatal */
    }
    setIsResolvingChat(true);
    let cancelled = false;

    (async () => {
      let match: ChatSession | null = null;

      // Attempt 1: default page size (fast path).
      try {
        const chats = await fetchChats(
          projectId ? { projectId } : undefined
        );
        if (cancelled) return;
        match = chats.find((c: ChatSession) => c.id === initialChatId) ?? null;
      } catch (e) {
        if (cancelled) return;
        console.warn(
          "[ChatContainer] fetchChats (default) failed:",
          e
        );
      }

      // Attempt 2: larger page (chat may be on page 2+).
      if (!match) {
        try {
          const chats = await fetchChats(
            projectId ? { projectId, limit: 200 } : { limit: 200 }
          );
          if (cancelled) return;
          match =
            chats.find((c: ChatSession) => c.id === initialChatId) ?? null;
        } catch (e) {
          if (cancelled) return;
          console.warn(
            "[ChatContainer] fetchChats (limit=200) failed:",
            e
          );
        }
      }

      // Attempt 3: direct GET /api/chats/{id} (bypasses pagination).
      if (!match) {
        try {
          const res = await fetch(
            `/api/chats/${encodeURIComponent(initialChatId)}`
          );
          if (res.ok) {
            const detail = await res.json();
            if (detail?.chat?.job_id) {
              match = detail.chat as ChatSession;
            }
          }
        } catch (e) {
          if (cancelled) return;
          console.warn(
            "[ChatContainer] direct chat fetch failed:",
            e
          );
        }
      }

      if (cancelled) return;

      if (match?.job_id) {
        setActiveChatId(match.id);
        setActiveJobId(match.job_id);
        // Await the snapshot before hiding the spinner so the composer
        // never appears with a half-populated turn. fetchJobDetails
        // swallows its own errors internally, so this cannot throw.
        await fetchJobDetails(match.job_id);
        if (cancelled) return;
      } else {
        // Chat id exists in URL but the backend has no record of it.
        // Preserve the id so the next POST /api/run attaches to it.
        setActiveChatId(initialChatId);
      }
      resolvedChatIdRef.current = initialChatId;
      setIsResolvingChat(false);
    })().catch((err) => {
      if (cancelled) return;
      console.error(
        "[ChatContainer] Failed to resolve initialChatId:",
        err
      );
      setActiveChatId(initialChatId);
      resolvedChatIdRef.current = initialChatId;
      setIsResolvingChat(false);
    });

    return () => {
      cancelled = true;
    };
  }, [
    initialJobId,
    initialChatId,
    activeChatId,
    activeJobId,
    projectId,
    fetchJobDetails,
    closeStream,
  ]);

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
    // Invalidate any in-flight hydration so it cannot repopulate this
    // fresh session with the previous chat's snapshot.
    hydrationEpochRef.current++;
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
    // Project-scoped "New Session" navigates to the dedicated new-chat
    // route `/projects/{id}/chat/new` — NOT to the project root
    // `/projects/{id}`.
    //
    // Why the new-chat route (rather than the project root):
    //
    //   * Project root renders the chat LIST page, requiring the user
    //     to click "New Chat" again — one extra hop for a flow they
    //     already initiated.
    //
    //   * `/projects/{id}/chat/new` renders `<ChatContainer
    //     projectId={id} />` with no `initialChatId`. All state is
    //     already reset by the setters above, so `isFreshSession` is
    //     true and `ChatLanding` renders immediately with
    //     `projectId` still attached. The first message the user
    //     sends carries `project_id` in the `POST /api/run` body,
    //     creating the new session under the correct project.
    //
    //   * No remount is triggered: `/projects/{id}/chat/[chatId]`
    //     and `/projects/{id}/chat/new` both render the same
    //     `ChatContainer` component, so React reconciles the props
    //     (`initialChatId` goes from a string to `undefined`) without
    //     tearing down and rebuilding the component tree. This means
    //     `closeStream()` above is sufficient to stop the previous
    //     SSE connection; the new session starts clean.
    //
    // Legacy chats (no project) keep the flat `/chat` target — they
    // have no project segment to preserve.
    router.push(
      projectId ? `/projects/${projectId}/chat/new` : "/chat"
    );
  }, [
    closeStream,
    router,
    projectId,
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
  const startTaskInner = useCallback(
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
          if (projectId) formData.append("project_id", projectId);

          // Project-scoped uploads land in the persistent project
          // workspace; legacy chats keep using the global endpoint.
          const uploadEndpoint = projectId
            ? `/api/projects/${projectId}/workspace/upload`
            : "/api/files/upload";

          const uploadRes = await fetch(uploadEndpoint, {
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

      // Detach from the previous run BEFORE mutating any state, and
      // invalidate any in-flight snapshot (see use-job-details).
      closeStream();
      hydrationEpochRef.current++;

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
            // v4.1 FIX: `id` is a React list key, NOT the job id. Making
            // it globally unique here prevents the
            //   "Encountered two children with the same key"
            // warning when:
            //   * two card-action clicks fire before the first
            //     POST /api/run response arrives, so both archives
            //     read the same stale `activeJobId`, or
            //   * `activeJobId` was hydrated from the backend as a
            //     chat id (e.g. `chat_mwyvjlrt_xa2w04`) instead of a
            //     genuine job id.
            //
            // `jobId` below still carries the real (possibly empty)
            // job id — that is the field regeneration and backend
            // routes actually use, so this change does not affect
            // those code paths.
            id: `turn_${Date.now().toString(36)}_${Math.random()
              .toString(36)
              .substring(2, 8)}`,
            jobId: activeJobId || "",
            prompt: submittedPrompt,
            timestamp: sessionTimestamp || new Date().toLocaleString(),
            steps: [...steps],
            finalResult,
            status: status === "failed" ? "failed" : "completed",
            tokensUsed: { ...tokensUsed },
            producedFiles: [...effectiveProducedFiles],
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
            project_id: projectId || undefined,
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
          // Keep the address bar in sync with the active session.
          // Project-scoped routes preserve the project segment so a
          // refresh lands back on the same two-column layout with
          // the project workspace panel on the right; legacy chats
          // keep the flat `/chat/{id}` shape they have always had.
          const nextPath = projectId
            ? `/projects/${projectId}/chat/${returnedChatId}`
            : `/chat/${returnedChatId}`;
          window.history.replaceState(null, "", nextPath);
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
      effectiveProducedFiles,
      submittedFiles,
      currentUsageMetrics,
      elapsedSeconds,
      activeLlmOverride,
      getAgentMaxSteps,
      selectedAgentId,
      connectStream,
      setProducedFiles,
      setElapsedSeconds,
      // Required so file uploads are routed to the correct workspace
      // (project-scoped vs. legacy chat-scoped) and so the run body
      // carries the right `project_id`.
      projectId,
      // Required so handleStartTask reads the LATEST reasoning text
      // when archiving the previous turn. Without it, the closure
      // captures "" from the initial state and every archived turn
      // ends up with an empty reasoning panel.
      chatReasoningText,
      // Required so the hook can detach from the previous run before
      // mutating state (see the closeStream() call above).
      closeStream,
    ]
  );

  /**
   * Public entry point for starting a run.
   *
   * Wraps `startTaskInner` with a synchronous ref-based send lock so
   * that two dispatches in the same tick (double Enter, or a send that
   * races the previous send's attachment upload) cannot both pass the
   * `status` guard and produce two jobs / a double-archived turn.
   */
  const handleStartTask = useCallback(
    async (
      customPrompt?: string,
      customOverride?: any,
      filesToUpload?: File[],
      isAuthRetry: boolean = false
    ) => {
      if (sendLockRef.current) {
        throw new Error("A send is already in progress.");
      }
      sendLockRef.current = true;
      try {
        return await startTaskInner(
          customPrompt,
          customOverride,
          filesToUpload,
          isAuthRetry
        );
      } finally {
        sendLockRef.current = false;
      }
    },
    [startTaskInner]
  );

  // v3: bridge from `PeldrunCardBlock` prompt actions to the run dispatch
  // path. `CardRuntimeContext.sendPrompt` calls this with the raw prompt
  // string the model attached to the card action button.
  //
  // Guards:
  //   * empty prompt -> ignored (defensive; PeldrunCardBlock already
  //                     hides prompt-less actions)
  //   * run in progress -> toast, do not queue. Matches the composer's
  //                        disabled state so the UX stays consistent
  //                        between the two send entry points.
  //   * handleStartTask rejection (network, backend) -> logged. The
  //                        status is already flipped to "failed" by
  //                        handleStartTask's own error handler, so the
  //                        UI reflects the failure without a second
  //                        error surface here.
  //
  // `handleStartTask` archives the current turn into `historyTurns`
  // before starting the new run, so the card the user clicked remains
  // visible in the transcript above the new exchange. This is the
  // intended UX.
  //
  // Return value (consumed by ```ui forms and card actions through
  // `CardRuntimeContext.sendPrompt`):
  //   true  -> the prompt was handed to handleStartTask
  //   false -> rejected before dispatch (empty prompt, run active, or
  //            another send still in flight)
  // A rejected send must never look like a success to the caller;
  // otherwise a form would freeze on "Submitted" for a message that
  // never left.
  const handleSendPromptImpl = useCallback(
    (prompt: string): boolean => {
      const text = (prompt || "").trim();
      if (!text) return false;
      if (status === "running" || status === "cancelling") {
        console.warn("[ChatContainer] sendPrompt rejected, status:", status);
        showToast.error(
          "Busy",
          "Wait for the current response to finish before sending another message."
        );
        return false;
      }
      // Another dispatch is still in flight (attachment upload, rapid
      // double click). handleStartTask would throw for it, so report the
      // rejection here instead of claiming success.
      if (sendLockRef.current) return false;
      handleStartTask(text).catch((err: any) => {
        console.error("[ChatContainer] Card action failed:", err);
      });
      return true;
    },
    [status, handleStartTask],
  );

  // Stable proxy handed to the chat thread (and from there to every card).
  //
  // Why it exists: an archived turn is mounted in the SAME render in which
  // `status` flips to "running" (a new run starts). Anything below that
  // memoizes or caches the callback it first received keeps a handler whose
  // closure says "running" forever, so the old buttons answer "Busy" even
  // when nothing is running. This proxy never changes identity and always
  // forwards to the newest implementation, so no consumer can hold a stale
  // `status` or a stale `handleStartTask`. (Same idea as the *Ref mirrors
  // above that SSE handlers use.)
  const sendPromptImplRef = useRef(handleSendPromptImpl);
  useEffect(() => {
    sendPromptImplRef.current = handleSendPromptImpl;
  });
  const handleSendPrompt = useCallback(
    (prompt: string): boolean => sendPromptImplRef.current(prompt),
    [],
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

        closeStream();
        hydrationEpochRef.current++;

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
              project_id: projectId || undefined,
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

        closeStream();
        hydrationEpochRef.current++;

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
              project_id: projectId || undefined,
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
      // Required so regeneration dispatches to the correct project
      // workspace (memory scope, file routes, artifact targets).
      projectId,
      // Required so the handler can detach from the previous run and
      // invalidate any in-flight snapshot before mutating state.
      closeStream,
    ]
  );

  // --- Derived values -------------------------------------------------------
  const isFreshSession =
    !isResolvingChat &&
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

  // Grouping is a pure reduction over `steps`. Previously it ran inline
  // in the JSX (`groupStepEvents(steps)`), so every parent re-render —
  // including composer keystrokes, right-panel toggles, and the
  // elapsed-timer tick — re-walked the entire step list. Memoizing on
  // `steps` collapses that to one walk per actual stream mutation.
  const activeGroupedSteps = useMemo(
    () => (runStartModeRef.current === "agent" ? groupStepEvents(steps) : {}),
    [steps]
  );

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

        {isResolvingChat ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="flex flex-col items-center gap-3">
              <Loader2 size={28} className="animate-spin text-accent" />
              <p className="text-sm text-muted-foreground">
                Loading chat…
              </p>
            </div>
          </div>
        ) : isFreshSession ? (
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
            activeGroupedSteps={activeGroupedSteps}
            chatReasoningText={
              runStartModeRef.current === "chat" ? chatReasoningText : ""
            }
            currentStepNum={currentStepNum}
            status={threadStatus}
            elapsedSeconds={elapsedSeconds}
            finalResult={finalResult}
            producedFiles={effectiveProducedFiles}
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
            // v3: forwarded through ChatThread to every turn's
            // MarkdownRenderer, then into CardRuntimeContext. Card
            // prompt actions click-through to the composer.
            onSendPrompt={handleSendPrompt}
          />
        )}

        {!isFreshSession && !isResolvingChat && (
          <div className="p-3 sm:p-4 bg-background shrink-0">
            <Composer
              chatId={activeChatId || undefined}
              projectId={projectId}
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
          ) : projectId ? (
            // Project-scoped workspace panel. Same slot, same header
            // controls, same override/overrideDraft semantics as the
            // legacy WorkspacePanel — the only difference is the
            // backing storage (persistent project workspace instead
            // of the per-chat session directory).
            <ProjectWorkspacePanel
              projectId={projectId}
              activeChatId={activeChatId || activeJobId}
              activeJobId={activeJobId}
              overrideFile={selectedFileForEditor}
              overrideDraft={sandboxDraft}
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
