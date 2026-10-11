// Path: frontend/src/components/chat/chat-container.tsx

"use client";

import React, { useState, useRef, useEffect, useMemo, useCallback } from "react";
import { useRouter } from "next/navigation";
import { WorkspacePanel, getFileCategory } from "@/components/workspace/workspace-panel";
import { Composer } from "@/components/chat/composer";
import { ChatSubHeader } from "@/components/chat/chat-sub-header";
import { ChatLanding } from "@/components/chat/chat-landing";
import { ChatThread, ChatTurn, AttachedUserFile } from "@/components/chat/chat-thread";
import { StepEvent } from "@/components/chat/chat-timeline";
import { EngineOption } from "@/components/chat/engine-selector";
import { useChatStore } from "@/stores/chat-store";
import { useAppStorage } from "@/hooks/use-app-storage";
import { inferModelCapabilities, fetchServerMetadata } from "@/lib/modelMetadata";

import { getGenerativeUIPayload } from "@/components/generative-ui/store";

interface ChildRunState {
  childRunId: string;
  parentRunId: string;
  taskId: string;
  targetAgentId: string;
  instruction: string;
  status: "running" | "completed" | "failed" | "cancelled";
  startedAt: number;
  steps: any[];
  deliverables: any[];
  tokensUsed?: any;
  durationSeconds?: number;
  error?: string;
}

interface DelegationApprovalRequest {
  approvalRequestId: string;
  taskId: string;
  parentRunId: string;
  targetAgentId: string;
  instruction: string;
  status: "pending" | "resolved";
  timeoutSeconds: number;
  createdAt: number;
}

function safeRender(val: any): string {
  if (val === null || val === undefined) return "";
  if (typeof val === "string") {
    const trimmed = val.trim();
    if (trimmed === "{}" || trimmed === "null" || trimmed === "undefined") return "";
    return val;
  }
  if (typeof val === "number" || typeof val === "boolean") return String(val);
  try {
    const str = JSON.stringify(val, null, 2);
    if (str === "{}" || str === "[]") return "";
    return str;
  } catch {
    return String(val);
  }
}

export interface ChatContainerProps {
  initialJobId?: string | null;
}

export function ChatContainer({ initialJobId }: ChatContainerProps) {
  const router = useRouter();
  const { selectedAgentId, setSelectedAgentId, getAgentMaxSteps } = useChatStore();
  const [inputValue, setInputValue] = useState("");
  const [landingAttachedFiles, setLandingAttachedFiles] = useState<File[]>([]);
  const [submittedPrompt, setSubmittedPrompt] = useState("");
  const [submittedFiles, setSubmittedFiles] = useState<AttachedUserFile[]>([]);
  const [activeJobId, setActiveJobId] = useState<string | null>(initialJobId || null);
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const [historyTurns, setHistoryTurns] = useState<ChatTurn[]>([]);
  const [status, setStatus] = useState<"idle" | "running" | "completed" | "failed">("idle");
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [finalResult, setFinalResult] = useState<string | null>(null);
  const [currentStepNum, setCurrentStepNum] = useState(0);
  const [producedFiles, setProducedFiles] = useState<{ name: string; path: string }[]>([]);
  const [selectedFileForEditor, setSelectedFileForEditor] = useState<string | null>(null);
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>({});
  const [copiedSection, setCopiedSection] = useState<string | null>(null);
  const [showRawTrace, setShowRawTrace] = useState(false);
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});

  const [activeChildRuns, setActiveChildRuns] = useState<Record<string, ChildRunState>>({});
  const [pendingApprovals, setPendingApprovals] = useState<Record<string, DelegationApprovalRequest>>({});

  const [showRightPanel, setShowRightPanel] = useAppStorage("right_panel_open");
  const [execMode, setExecMode] = useAppStorage("exec_mode");
  const [reasoningEffort] = useAppStorage("reasoning_effort");
  const [selectedEngineId, setSelectedEngineId] = useAppStorage("selected_engine");
  const [activeModelName, setActiveModelName] = useAppStorage("active_model");
  const [activeLlmOverride] = useAppStorage("active_llm_override");

  const [sandboxDraft, setSandboxDraft] = useState<{ filename: string; content: string } | null>(null);
  const [tokensUsed, setTokensUsed] = useState({ input: 0, output: 0, total: 0 });
  const [currentUsageMetrics, setCurrentUsageMetrics] = useState<{
    cost_usd?: number;
    latency_ms?: number;
    tokens_per_second?: number;
  } | null>(null);

  const [humanQuery, setHumanQuery] = useState<string | null>(null);
  const [humanAnswer, setHumanAnswer] = useState("");
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [sessionTimestamp, setSessionTimestamp] = useState<string>("");

  const eventSourceRef = useRef<EventSource | null>(null);
  const chatScrollBottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    fetchServerMetadata().then((data) => setMetadataVault(data || {}));
    const onMetadataUpdate = (e: any) => setMetadataVault(e.detail || {});
    window.addEventListener("omweb:metadata-updated", onMetadataUpdate);
    return () => window.removeEventListener("omweb:metadata-updated", onMetadataUpdate);
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") {
      const onModelChange = (e: any) => {
        if (e.detail?.model) setActiveModelName(e.detail.model);
      };
      window.addEventListener("omweb:model-change", onModelChange);
      return () => window.removeEventListener("omweb:model-change", onModelChange);
    }
  }, [setActiveModelName]);

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

  useEffect(() => {
    if (status === "running" || steps.length > 0 || submittedPrompt) {
      chatScrollBottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [steps, status, finalResult, submittedPrompt]);

  useEffect(() => {
    const handleSandboxEvent = (e: Event) => {
      const customEvent = e as CustomEvent<{ code: string; language: string; filename: string }>;
      if (customEvent.detail) {
        setShowRightPanel(true);
        setSandboxDraft({
          filename: customEvent.detail.filename,
          content: customEvent.detail.code,
        });
      }
    };
    const handleArtifactEvent = (e: Event) => {
      const ce = e as CustomEvent<{ artifact?: string; path?: string; file?: string }>;
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
      window.removeEventListener("peldrun:artifact-created", handleArtifactEvent);
      window.removeEventListener("peldrun:open-in-sandbox", handleSandboxEvent);
    };
  }, [setShowRightPanel]);

  useEffect(() => {
    if (initialJobId) {
      setActiveJobId(initialJobId);
      fetchJobDetails(initialJobId);
    }
  }, [initialJobId]);

  const fetchJobDetails = async (jobId: string) => {
    try {
      const res = await fetch(`/api/run/jobs/${jobId}`);
      if (res.ok) {
        const data = await res.json();
        if (data.chat_id) setActiveChatId(data.chat_id);
        if (data.prompt) setSubmittedPrompt(data.prompt);
        if (data.agent_id) setSelectedAgentId(data.agent_id);
        if (data.mode) { setExecMode(data.mode); }
        if (data.model) setActiveModelName(data.model);
        if (data.status) setStatus(data.status);
        if (data.result) setFinalResult(safeRender(data.result));
        setSessionTimestamp(data.created_at || data.timestamp || new Date().toLocaleString());

        if (data.turns && Array.isArray(data.turns)) {
          const loadedTurns: ChatTurn[] = data.turns.map((t: any, idx: number) => {
            const replayedSteps: StepEvent[] = (t.events || [])
              .map((ev: any, evIdx: number) => ({
                id: `hist-${idx}-step-${evIdx}`,
                step: ev.step || 1,
                type: ev.type || "thought",
                content: safeRender(ev.data?.thought || ev.data?.output || ev.data?.content || ev.data || JSON.stringify(ev)),
                toolName: ev.data?.name,
                timestamp: ev.timestamp || "",
              }))
              .filter((ev: StepEvent) => ev.content.trim() !== "" || Boolean(ev.toolName));

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
              attachedFiles: t.attached_files || undefined,
              usage: t.usage || undefined,
              tokensUsed: t.usage ? {
                input: t.usage.prompt_tokens || t.usage.input_tokens || 0,
                output: t.usage.completion_tokens || t.usage.output_tokens || 0,
                total: t.usage.total_tokens || 0,
              } : undefined,
            };
          });
          setHistoryTurns(loadedTurns);

          if (data.usage_summary && data.usage_summary.total_tokens > 0) {
            setTokensUsed({
              input: data.usage_summary.input_tokens || 0,
              output: data.usage_summary.output_tokens || 0,
              total: data.usage_summary.total_tokens || 0,
            });
            setCurrentUsageMetrics({
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
            const evContent = ev.data?.thought || ev.data?.output || ev.data?.content || ev.data || JSON.stringify(ev);
            const rendered = safeRender(evContent);
            const tool = ev.data?.name;
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
          setSteps(replayed);
          const maxStep = replayed.reduce((max, s) => Math.max(max, s.step), 0);
          if (maxStep > 0) setCurrentStepNum(maxStep);
        }

        fetchJobFiles(data.id || jobId);
        if (data.status === "running") {
          connectStream(data.id || jobId);
        }
      }
    } catch (e) {
      console.error("Failed to fetch job details", e);
    }
  };

  const connectStream = (jobId: string) => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }
    const es = new EventSource(`/api/run/jobs/${jobId}/stream`);
    eventSourceRef.current = es;

    const appendStep = (type: StepEvent["type"], content: any, stepNum = 1, toolName?: string) => {
      const cleanContent = safeRender(content);
      const cleanTool = toolName ? safeRender(toolName) : undefined;
      if (type === "tool_call" && (!cleanContent && !cleanTool)) return;
      if (!cleanContent && !cleanTool) return;

      setSteps((prev) => [
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

    const handleEventPayload = (eventType: string, payload: any) => {
      const step = payload.step || payload.data?.step || currentStepNum || 1;
      setCurrentStepNum(step);

      if (payload.data?.model) {
        setActiveModelName(payload.data.model);
      }

      const logTimestamp = new Date().toLocaleTimeString();

      if (eventType === "step_start") {
        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("peldrun:log-entry", {
              detail: { line: `[${logTimestamp}] [STEP] Step ${step} started` },
            })
          );
        }
      } else if (eventType === "thought") {
        const raw = payload.data?.thought ?? payload.data?.content ?? payload.data;
        if (safeRender(raw).trim() !== "") {
          appendStep("thought", raw, step);
          setExpandedSteps((prev) => ({ ...prev, [`active-${step}`]: true }));
          if (typeof window !== "undefined") {
            window.dispatchEvent(
              new CustomEvent("peldrun:log-entry", {
                detail: { line: `[${logTimestamp}] [THOUGHT] ${safeRender(raw)}` },
              })
            );
          }
        }
        if (payload.data?.tokens) setTokensUsed(payload.data.tokens);
      } else if (eventType === "tool_call") {
        const name = payload.data?.name;
        const args = payload.data?.arguments ?? "";
        if (name === "ask_human" || (typeof args === "string" && (args.includes("?") || args.includes("prefer")))) {
          setHumanQuery(typeof args === "string" ? args : JSON.stringify(args));
        }
        appendStep("tool_call", args, step, name);
        setExpandedSteps((prev) => ({ ...prev, [`active-${step}`]: true }));
        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("peldrun:log-entry", {
              detail: { line: `[${logTimestamp}] [TOOL_CALL] ${name || "tool"}(${typeof args === "string" ? args : JSON.stringify(args)})` },
            })
          );
        }
      } else if (eventType === "observation") {
        const raw = payload.data?.output ?? "Execution completed.";
        appendStep("observation", raw, step);
        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("peldrun:log-entry", {
              detail: { line: `[${logTimestamp}] [OBSERVATION] ${safeRender(raw)}` },
            })
          );
        }

        const artName = payload.data?.artifact || payload.data?.path || payload.data?.file || "";
        if (payload.data?.event === "artifact_created" || artName) {
          setShowRightPanel(true);
          if (artName) {
            const cat = getFileCategory(artName);
            if (cat === "preview" || cat === "artifacts") {
              setSelectedFileForEditor(artName);
            }
            if (typeof window !== "undefined") {
              window.dispatchEvent(new CustomEvent("peldrun:artifact-created", { detail: { ...payload.data, file: artName } }));
            }
          }
        }
        fetchJobFiles(jobId);

      } else if (eventType === "artifact_created" || eventType === "artifact") {
        const artName = payload.data?.artifact || payload.data?.path || payload.data?.file || "";
        if (artName) {
          setShowRightPanel(true);
          const cat = getFileCategory(artName);
          if (cat === "preview" || cat === "artifacts") {
            setSelectedFileForEditor(artName);
          }
          if (typeof window !== "undefined") {
            window.dispatchEvent(new CustomEvent("peldrun:artifact-created", { detail: { ...payload.data, file: artName } }));
          }
        }
        fetchJobFiles(jobId);

      } else if (eventType === "final" || eventType === "done") {
        const resText = payload.data?.result ?? payload.data?.content ?? "Task completed successfully.";
        if (eventType === "final") setFinalResult(safeRender(resText));

        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("peldrun:log-entry", {
              detail: { line: `[${logTimestamp}] [FINAL] ${safeRender(resText)}` },
            })
          );
          window.dispatchEvent(new CustomEvent("peldrun:run-completed"));
        }

        if (payload.data?.usage_summary) {
          setTokensUsed({
            input: payload.data.usage_summary.input_tokens || 0,
            output: payload.data.usage_summary.output_tokens || 0,
            total: payload.data.usage_summary.total_tokens || 0,
          });
          setCurrentUsageMetrics({
            cost_usd: payload.data.usage_summary.total_cost_usd,
            latency_ms: payload.data.usage_summary.latency_ms,
            tokens_per_second: payload.data.usage_summary.tokens_per_second,
          });
        } else if (payload.data?.usage) {
          const u = payload.data.usage;
          const inTok = u.prompt_tokens ?? u.input_tokens ?? 0;
          const outTok = u.completion_tokens ?? u.output_tokens ?? 0;
          const totTok = u.total_tokens ?? (inTok + outTok);
          setTokensUsed((prev) => ({
            input: inTok > 0 ? inTok : prev.input,
            output: outTok > 0 ? outTok : prev.output,
            total: totTok > 0 ? totTok : (inTok + outTok > 0 ? inTok + outTok : prev.total),
          }));
          setCurrentUsageMetrics({
            cost_usd: u.cost_usd,
            latency_ms: u.latency_ms,
            tokens_per_second: u.tokens_per_second,
          });
        }

        setStatus("completed");
        setExpandedSteps({});
        fetchJobFiles(jobId);
        es.close();

        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("omweb:chat-status-changed", {
              detail: {
                chatId: activeChatId,
                jobId: jobId,
                status: "completed",
              },
            })
          );
          window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        }
      } else if (eventType === "error") {
        const errText = payload.data?.message ?? "Execution error encountered.";
        setFinalResult(safeRender(errText));
        setStatus("failed");
        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("peldrun:log-entry", {
              detail: { line: `[${logTimestamp}] [ERROR] ${safeRender(errText)}` },
            })
          );
          window.dispatchEvent(new CustomEvent("peldrun:run-completed"));
        }
        fetchJobFiles(jobId);
        es.close();

        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent("omweb:chat-status-changed", {
              detail: {
                chatId: activeChatId,
                jobId: jobId,
                status: "failed",
              },
            })
          );
          window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        }
      }
    };

    const bindEvt = (name: string) => {
      es.addEventListener(name, (e: any) => {
        try {
          handleEventPayload(name, JSON.parse(e.data));
        } catch {
          handleEventPayload(name, { data: e.data });
        }
      });
    };

    [
      "step_start",
      "thought",
      "tool_call",
      "observation",
      "artifact_created",
      "artifact",
      "final",
      "done",
      "error",
    ].forEach(bindEvt);

    [
      "delegation_requested",
      "approval_required",
      "approval_resolved",
      "child_run_started",
      "child_run_step",
      "child_run_completed",
      "child_run_failed",
      "child_run_cancelled",
    ].forEach((name) => {
      es.addEventListener(name, (e: any) => {
        try {
          const payload = JSON.parse(e.data);
          if (name === "approval_required") {
            setPendingApprovals((prev) => ({
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
            setPendingApprovals((prev) => {
              const next = { ...prev };
              delete next[payload.data.approval_request_id];
              return next;
            });
          } else if (name === "child_run_started") {
            setActiveChildRuns((prev) => ({
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
          } else if (name === "child_run_completed" || name === "child_run_failed" || name === "child_run_cancelled") {
            const cStatus = name === "child_run_completed" ? "completed" : (name === "child_run_cancelled" ? "cancelled" : "failed");
            setActiveChildRuns((prev) => {
              const cur = prev[payload.data.child_run_id];
              if (!cur) return prev;
              return {
                ...prev,
                [payload.data.child_run_id]: {
                  ...cur,
                  status: cStatus,
                  deliverables: payload.data.deliverables || cur.deliverables,
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

    es.onerror = () => {
      es.close();
      fetchJobFiles(jobId);
    };
  };

  useEffect(() => {
    let timer: NodeJS.Timeout;
    if (status === "running") {
      timer = setInterval(() => setElapsedSeconds((prev) => prev + 1), 1000);
    }
    return () => clearInterval(timer);
  }, [status]);

  const toggleStep = (key: string) => {
    setExpandedSteps((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const copyText = (text: string, identifier: string) => {
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
      setCopiedSection(identifier);
      setTimeout(() => setCopiedSection(null), 2000);
    });
  };

  const handleNewSession = () => {
    if (eventSourceRef.current) eventSourceRef.current.close();
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
    setActiveChildRuns({});
    setPendingApprovals({});
    router.push("/chat");
  };

  const handleStopTask = async () => {
    if (!activeJobId || status !== "running") return;
    try {
      await fetch(`/api/run/jobs/${activeJobId}/stop`, { method: "POST" });
      setStatus("failed");
      if (eventSourceRef.current) eventSourceRef.current.close();
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("omweb:chat-status-changed", {
            detail: {
              chatId: activeChatId,
              jobId: activeJobId,
              status: "failed",
            },
          })
        );
        window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
      }
    } catch (e) {
      console.error("Failed to stop job", e);
    }
  };

  const handleSendHumanAnswer = async (customAnswer?: string) => {
    const finalAnswer = (typeof customAnswer === "string" ? customAnswer : humanAnswer).trim();
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
  };

  const handleApproveDelegation = async (approvalId: string, editedInstruction?: string) => {
    if (!activeJobId) return;
    try {
      await fetch(`/api/run/jobs/${activeJobId}/respond`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answer: {
            decision: "approve",
            instruction: editedInstruction,
          },
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
  };

  const handleRejectDelegation = async (approvalId: string) => {
    if (!activeJobId) return;
    try {
      await fetch(`/api/run/jobs/${activeJobId}/respond`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answer: { decision: "reject" },
        }),
      });
      setPendingApprovals((prev) => {
        const next = { ...prev };
        delete next[approvalId];
        return next;
      });
    } catch (err) {
      console.error("Failed to reject delegation:", err);
    }
  };

  const handleCancelSubtask = async (childRunId: string) => {
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
  };

  const fetchJobFiles = async (jobId: string) => {
    try {
      const res = await fetch(`/api/run/jobs/${jobId}/files`);
      if (res.ok) {
        const data = await res.json();
        const turnFiles: { name: string; path: string; action?: string; status?: string }[] =
          data.delta_files !== undefined ? data.delta_files : (data.files || []);
        setProducedFiles(turnFiles);
      }
    } catch (e) {
      console.error("Error fetching job files", e);
    }
  };

  const handleStartTask = async (
    customPrompt?: string,
    customOverride?: any,
    filesToUpload?: File[]
  ): Promise<{ accepted: true; jobId: string; chatId: string }> => {
    const rawText = (customPrompt !== undefined ? customPrompt : inputValue).trim();
    if ((!rawText && (!filesToUpload || filesToUpload.length === 0)) || status === "running") {
      throw new Error("Task cannot be started with empty input or during ongoing run.");
    }

    let finalPrompt = rawText;
    const effectiveMode = customOverride?.mode || execMode;

    const activeCaps = inferModelCapabilities(activeModelName, metadataVault[activeModelName]);
    const isModelReasoning = customOverride?.is_reasoning_model !== undefined
      ? Boolean(customOverride.is_reasoning_model)
      : Boolean(activeCaps?.isReasoning);

    const effectiveReasoningEffort = isModelReasoning
      ? (customOverride?.reasoning_effort || reasoningEffort || "none")
      : "none";
    const targetChatId = activeChatId || `chat_${Date.now().toString(36)}_${Math.random().toString(36).substring(2, 8)}`;

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

        const uploadRes = await fetch("/api/files/upload", { method: "POST", body: formData });
        if (!uploadRes.ok) {
          const uploadErrData = await uploadRes.json().catch(() => ({}));
          throw new Error(uploadErrData.detail || "Failed to upload attached files");
        }

        const uploadData = await uploadRes.json();
        const uploadedList: { name: string; path: string }[] = uploadData.files || [];
        if (uploadedList.length > 0) {
          const filesSummary = uploadedList.map((f) => `- ${f.name} (in workspace directory)`).join("\n");
          finalPrompt = rawText
            ? `${rawText}\n\n[Uploaded Files]:\n${filesSummary}`
            : `Inspect workspace files:\n${filesSummary}`;
        }
      } catch (uploadErr) {
        console.error("[ChatContainer] Failed to upload files:", uploadErr);
        throw uploadErr;
      }
    }

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
          usage: tokensUsed.total > 0 ? {
            input_tokens: tokensUsed.input,
            output_tokens: tokensUsed.output,
            total_tokens: tokensUsed.total,
            cost_usd: currentUsageMetrics?.cost_usd ?? 0,
            latency_ms: currentUsageMetrics?.latency_ms ?? (elapsedSeconds * 1000),
            tokens_per_second: currentUsageMetrics?.tokens_per_second ?? (elapsedSeconds > 0 ? tokensUsed.output / elapsedSeconds : 0),
            provider: "local",
            model: activeModelName,
          } as any : undefined,
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
          max_steps: effectiveMode === "agent" ? getAgentMaxSteps(selectedAgentId) : 1,
          chat_id: targetChatId,
          agent_id: selectedAgentId || "peldrun",
          mode: effectiveMode,
          reasoning_effort: effectiveReasoningEffort,
          is_reasoning_model: isModelReasoning,
          generative_ui: generativeUiPayload,
          ...storedOverride,
          ...(customOverride || {})
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
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const groupStepEvents = (evts: StepEvent[]) => {
    return evts.reduce((acc, s) => {
      const isVisible = (s.content && s.content.trim() !== "") || Boolean(s.toolName);
      if (!isVisible) return acc;
      if (!acc[s.step]) acc[s.step] = [];
      acc[s.step].push(s);
      return acc;
    }, {} as Record<number, StepEvent[]>);
  };

  const isFreshSession = historyTurns.length === 0 && steps.length === 0 && !submittedPrompt && status !== "running";

  const getLiveStatusMessage = () => {
    if (execMode === "chat") return "Synthesizing conversational response...";
    if (steps.length === 0) return "Analyzing request and formulating execution plan...";
    const lastEvt = steps[steps.length - 1];
    if (lastEvt.type === "tool_call") return `Executing tool: ${lastEvt.toolName || "external tool"}...`;
    if (lastEvt.type === "observation") return "Processing tool observation & planning next action...";
    if (lastEvt.type === "thought") return "Deep reasoning and verifying solution...";
    return "Agent reasoning & executing autonomously...";
  };

  const currentCostUsd = useMemo(() => {
    return currentUsageMetrics?.cost_usd ?? 0;
  }, [currentUsageMetrics]);

  const currentLatencyMs = useMemo(() => {
    if (currentUsageMetrics?.latency_ms && currentUsageMetrics.latency_ms > 0) {
      return currentUsageMetrics.latency_ms;
    }
    return elapsedSeconds > 0 ? elapsedSeconds * 1000 : null;
  }, [currentUsageMetrics, elapsedSeconds]);

  const currentTokensPerSecond = useMemo(() => {
    if (currentUsageMetrics?.tokens_per_second && currentUsageMetrics.tokens_per_second > 0) {
      return currentUsageMetrics.tokens_per_second;
    }
    if (elapsedSeconds > 0 && tokensUsed.output > 0) {
      return Number((tokensUsed.output / elapsedSeconds).toFixed(1));
    }
    return null;
  }, [currentUsageMetrics, elapsedSeconds, tokensUsed.output]);

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
          showRightPanel={showRightPanel}
          onToggleRightPanel={() => setShowRightPanel(!showRightPanel)}
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
            activeGroupedSteps={execMode === "agent" ? groupStepEvents(steps) : {}}
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
              isRunning={status === "running"}
              disabled={status === "running"}
            />
          </div>
        )}
      </div>

      {showRightPanel && (
        <div className="flex-1 h-full min-w-0 transition-all">
          <WorkspacePanel
            activeJobId={activeJobId}
            activeChatId={activeChatId || activeJobId}
            overrideFile={selectedFileForEditor}
            overrideDraft={sandboxDraft}
          />
        </div>
      )}
    </div>
  );
}

export default ChatContainer;
