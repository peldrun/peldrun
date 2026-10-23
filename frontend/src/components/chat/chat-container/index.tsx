"use client";

import React, {
  useState,
  useRef,
  useEffect,
  useMemo,
  useCallback,
} from "react";
import { useRouter } from "next/navigation";
import {
  WorkspacePanel,
  getFileCategory,
} from "@/components/workspace/workspace-panel";
import { SourceControlWorkspacePanel } from "@/features/source-control/components/source-control-workspace-panel";

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
import { useJobDetails } from "./hooks/use-job-details";

export type { ChatContainerProps };

export function ChatContainer({ initialJobId }: ChatContainerProps) {
  const router = useRouter();
  const { selectedAgentId, setSelectedAgentId, getAgentMaxSteps } =
    useChatStore();

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
  const [status, setStatus] = useState<ChatStatus | "paused">("idle");
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

  // --- Persisted user preferences ------------------------------------------
  const [showRightPanel, setShowRightPanel] = useAppStorage("right_panel_open");

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
  const [rightPanelMode, setRightPanelMode] = useState<
    "workspace" | "source-control"
  >(() => {
    if (typeof window === "undefined") return "workspace";
    try {
      const stored = window.localStorage.getItem(
        "peldrun:right_panel_mode"
      );
      return stored === "source-control" ? "source-control" : "workspace";
    } catch {
      // Access to localStorage can throw in some privacy modes
      // (Safari ITP, Firefox total-cookie-protection, etc.). Falling
      // back to the default is safer than crashing the whole panel.
      return "workspace";
    }
  });

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
  const [activeLlmOverride] = useAppStorage("active_llm_override");

  // --- Extracted hooks ------------------------------------------------------
  const metadataVault = useMetadataVault();
  const [elapsedSeconds, setElapsedSeconds] = useElapsedTimer(status as any);
  const { producedFiles, setProducedFiles, fetchJobFiles } = useJobFiles();

  const handleOpenReview = useCallback(
    (operationId: string) => {
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
    [setShowRightPanel, setRightPanelMode]
  );

  // Refs mirror fast-changing state so SSE handlers always read the latest
  // value rather than a closure-captured snapshot.
  const currentStepNumRef = useRef(currentStepNum);
  const activeChatIdRef = useRef(activeChatId);
  useEffect(() => {
    currentStepNumRef.current = currentStepNum;
  }, [currentStepNum]);
  useEffect(() => {
    activeChatIdRef.current = activeChatId;
  }, [activeChatId]);

  const { connectStream, closeStream, eventSourceRef } = useSseStream({
    currentStepNumRef,
    activeChatIdRef,
    setSteps,
    setCurrentStepNum,
    setActiveModelName,
    setTokensUsed,
    setCurrentUsageMetrics,
    setFinalResult,
    setStatus: setStatus as any,
    setExpandedSteps,
    setHumanQuery,
    setShowRightPanel,
    setSelectedFileForEditor,
    setActiveChildRuns,
    setPendingApprovals,
    fetchJobFiles,
  });

  const fetchJobDetails = useJobDetails({
    setActiveChatId,
    setSubmittedPrompt,
    setSelectedAgentId,
    setExecMode,
    setActiveModelName,
    setStatus: setStatus as any,
    setFinalResult,
    setSessionTimestamp,
    setHistoryTurns,
    setTokensUsed,
    setCurrentUsageMetrics,
    setSteps,
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
    if (status === "running" || status === "paused" || steps.length > 0 || submittedPrompt) {
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
    router.push("/chat");
  }, [
    closeStream,
    router,
    setProducedFiles,
    setElapsedSeconds,
    setShowRightPanel,
  ]);

  const handleStopTask = useCallback(async () => {
    if (!activeJobId || (status !== "running" && status !== "paused")) return;
    try {
      await fetch(`/api/run/jobs/${activeJobId}/stop`, { method: "POST" });
      setStatus("failed");
      setHumanQuery(null);
      closeStream();
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("omweb:chat-status-changed", {
            detail: { chatId: activeChatId, jobId: activeJobId, status: "failed" },
          })
        );
        window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
      }
    } catch (e) {
      console.error("[ChatContainer] Failed to stop job:", e);
    }
  }, [activeJobId, status, activeChatId, closeStream]);

  const handlePauseTask = useCallback(async () => {
    if (!activeJobId || status !== "running") return;
    try {
      await fetch(`/api/run/jobs/${activeJobId}/pause`, { method: "POST" });
      setStatus("paused");
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("omweb:chat-status-changed", {
            detail: { chatId: activeChatId, jobId: activeJobId, status: "paused" },
          })
        );
      }
    } catch (e) {
      console.error("[ChatContainer] Failed to pause job:", e);
    }
  }, [activeJobId, status, activeChatId]);

  const handleResumeTask = useCallback(async () => {
    if (!activeJobId || status !== "paused") return;
    try {
      await fetch(`/api/run/jobs/${activeJobId}/resume`, { method: "POST" });
      setStatus("running");
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("omweb:chat-status-changed", {
            detail: { chatId: activeChatId, jobId: activeJobId, status: "running" },
          })
        );
      }
    } catch (e) {
      console.error("[ChatContainer] Failed to resume job:", e);
    }
  }, [activeJobId, status, activeChatId]);

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
      filesToUpload?: File[]
    ): Promise<{ accepted: true; jobId: string; chatId: string }> => {
      const rawText = (
        customPrompt !== undefined ? customPrompt : inputValue
      ).trim();
      if (
        (!rawText && (!filesToUpload || filesToUpload.length === 0)) ||
        status === "running" ||
        status === "paused"
      ) {
        throw new Error(
          "Task cannot be started with empty input or during ongoing run."
        );
      }

      let finalPrompt = rawText;
      const effectiveMode = customOverride?.mode || execMode;

      const activeCaps = inferModelCapabilities(
        activeModelName,
        metadataVault[activeModelName]
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

      attachedMetadata = [...attachedMetadata, ...refAttachments];

      // Archive the current turn before starting a new one.
      if (submittedPrompt && (finalResult || steps.length > 0)) {
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
            model: activeModelName,
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
                    provider: "local",
                    model: activeModelName,
                  } as any)
                : undefined,
          },
        ]);
      }

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

        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("omweb:chat-status-changed", {
              detail: {
                chatId: returnedChatId,
                jobId: jobId,
                status: "running",
                title: finalPrompt.slice(0, 30),
              },
            })
          );
          window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        }

        connectStream(jobId);
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
    ]
  );

  // --- Turn Regeneration Handler --------------------------------------------
  const handleRegenerate = useCallback(
    async (turnIndex: number, turn?: ChatTurn) => {
      if (status === "running" || status === "paused") return;

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
          connectStream(jobId);
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
          connectStream(jobId);
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
    ]
  );

  // --- Derived values -------------------------------------------------------
  const isFreshSession =
    historyTurns.length === 0 &&
    steps.length === 0 &&
    !submittedPrompt &&
    status !== "running" &&
    status !== "paused";

  const getLiveStatusMessage = useCallback(() => {
    if (status === "paused") return "Execution paused at step boundary";
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
  }, [execMode, steps, status]);

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
            showRightPanel && rightPanelMode === "source-control"
          }
          onToggleSourceControlPanel={() => {
            // Same three-state toggle, but targeting Source Control.
            // The "mount key" is bumped on every open-from-hidden so
            // the panel always starts on the "browse" tab (this
            // handler is the one used when the user opens the panel
            // manually from the header — NOT when `handleOpenReview`
            // is used, which sets `sourceControlDefaultMode = "agent"`).
            if (
              showRightPanel &&
              rightPanelMode === "source-control"
            ) {
              setShowRightPanel(false);
            } else {
              if (!showRightPanel || rightPanelMode !== "source-control") {
                setSourceControlDefaultMode("browse");
                setSourceControlMountKey((k) => k + 1);
              }
              setRightPanelMode("source-control");
              setShowRightPanel(true);
            }
          }}
          status={status as any}
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
            submittedPrompt={submittedPrompt}
            submittedFiles={submittedFiles}
            sessionTimestamp={sessionTimestamp}
            humanQuery={humanQuery}
            humanAnswer={humanAnswer}
            setHumanAnswer={setHumanAnswer}
            onSendHumanAnswer={handleSendHumanAnswer}
            execMode={execMode}
            activeGroupedSteps={
              execMode === "agent" ? groupStepEvents(steps) : {}
            }
            currentStepNum={currentStepNum}
            status={status}
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
            activeModelName={activeModelName}
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
            onPauseJob={handlePauseTask}
            onResumeJob={handleResumeTask}
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
              isRunning={status === "running" || status === "paused"}
              disabled={status === "running" || status === "paused"}
            />
          </div>
        )}
      </div>

      {showRightPanel && (
        <div className="flex-1 h-full min-w-0 transition-all">
          {rightPanelMode === "source-control" ? (
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
    </div>
  );
}

export default ChatContainer;
