// Path: frontend/src/components/chat/composer.tsx

"use client";

import React, { useState, useEffect, useRef, useMemo, useCallback } from "react";
import { useParams } from "next/navigation";
import {
  Send,
  Paperclip,
  StopCircle,
  Bot,
  MessageSquare,
  FileText,
  X,
  Maximize2,
  Minimize2,
  RotateCcw,
  AlertCircle,
  Info,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { AgentSelector } from "./agent-selector";
import { EngineSelector } from "./engine-selector";
import { ActiveToolsModal } from "./active-tools-modal";
import { ReasoningEffortSelector } from "./reasoning-effort-selector";
import { useChatStore } from "@/stores/chat-store";
import { useAppStorage } from "@/hooks/use-app-storage";
import { inferModelCapabilities, fetchServerMetadata } from "@/lib/modelMetadata";
import { LexicalEditorComponent } from "./composer/lexical-editor";
import { LexicalEditorHandle } from "./composer/types";
import {
  commandRegistry,
  getMentionId,
  resolveExecutionDirectives,
  MentionItem,
} from "./composer/registry/command-registry";

export interface SubmissionAccepted {
  accepted: boolean;
  jobId?: string;
  chatId?: string;
}

export interface ComposerDraft {
  version: 1;
  text: string;
  updatedAt: number;
}

export function saveDraft(key: string, text: string): void {
  if (typeof window === "undefined") return;
  try {
    if (text.trim()) {
      const draft: ComposerDraft = {
        version: 1,
        text,
        updatedAt: Date.now(),
      };
      sessionStorage.setItem(key, JSON.stringify(draft));
    } else {
      sessionStorage.removeItem(key);
    }
  } catch (error) {
    console.error("[Composer] Could not persist draft:", error);
  }
}

export function loadDraft(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed === "object" &&
      parsed.version === 1 &&
      typeof parsed.text === "string"
    ) {
      return parsed.text;
    }
    if (typeof parsed === "string") return parsed;
    return null;
  } catch (error) {
    console.error("[Composer] Could not load draft:", error);
    return null;
  }
}

export function clearDraft(key: string): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.removeItem(key);
  } catch (error) {
    console.error("[Composer] Could not clear draft:", error);
  }
}

interface ComposerProps {
  onSend: (
    text: string,
    files?: File[],
    llmOverride?: any
  ) => Promise<SubmissionAccepted | void> | SubmissionAccepted | void;
  onStop?: () => void;
  isRunning?: boolean;
  disabled?: boolean;
  placeholder?: string;
  chatId?: string;
}

export function Composer({
  onSend,
  onStop,
  isRunning,
  disabled,
  placeholder,
  chatId,
}: ComposerProps) {
  const params = useParams();
  const urlChatId = (params?.id as string) || (params?.chatId as string) || undefined;
  const activeSessionId = chatId || urlChatId;

  const [text, setText] = useState("");
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [selectedMentions, setSelectedMentions] = useState<MentionItem[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});
  const [isExpanded, setIsExpanded] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [failedDraft, setFailedDraft] = useState<{
    text: string;
    files: File[];
  } | null>(null);
  const backupDraftRef = useRef<{
    text: string;
    files: File[];
  } | null>(null);

  const draftStorageKey = useMemo(
    () => `peldrun_composer_draft_${activeSessionId || "default"}`,
    [activeSessionId]
  );

  const [activeModel, setActiveModel] = useAppStorage("active_model");
  const [activeProvider, setActiveProvider] = useAppStorage("active_provider");
  const [execMode, setExecMode] = useAppStorage("exec_mode");
  const [reasoningEffort] = useAppStorage("reasoning_effort");
  const [activeLlmOverride] = useAppStorage("active_llm_override");

  const { selectedAgentId, setSelectedAgentId } = useChatStore();

  const editorRef = useRef<LexicalEditorHandle>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const unregisterPeldrun = commandRegistry.registerMentionItem({
      id: "agent-peldrun",
      label: "@peldrun",
      description: "Autonomous Master Agent",
      category: "agent",
      insertText: "@peldrun ",
      metadata: {
        agentId: "peldrun",
      },
    });

    return () => {
      unregisterPeldrun();
    };
  }, []);

  useEffect(() => {
    const handleMentionSelect = (event: Event) => {
      const item = (event as CustomEvent<MentionItem>).detail;
      if (!item) return;

      if (item.category !== "agent" && item.category !== "tool") {
        return;
      }

      const id = getMentionId(item);
      if (!id) return;

      setSelectedMentions((previous) => {
        const withoutSameItem = previous.filter((existing) => existing.id !== item.id);
        return [...withoutSameItem, item];
      });

      if (item.category === "agent") {
        setSelectedAgentId(id);
        setExecMode("agent");
      }
    };

    window.addEventListener("peldrun:mention-selected", handleMentionSelect as EventListener);
    return () => {
      window.removeEventListener("peldrun:mention-selected", handleMentionSelect as EventListener);
    };
  }, [setSelectedAgentId, setExecMode]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const saved = loadDraft(draftStorageKey);
    if (saved) {
      const timer = setTimeout(() => {
        if (editorRef.current && !editorRef.current.getText()) {
          editorRef.current.setText(saved);
          setText(saved);
        }
      }, 50);
      return () => clearTimeout(timer);
    }
  }, [draftStorageKey]);

  useEffect(() => {
    fetchServerMetadata().then((data) => setMetadataVault(data || {}));
    const onMetadataUpdate = (e: any) => setMetadataVault(e.detail || {});
    window.addEventListener("omweb:metadata-updated", onMetadataUpdate);
    return () => window.removeEventListener("omweb:metadata-updated", onMetadataUpdate);
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") {
      const onModelChange = (e: any) => {
        if (e.detail?.model) setActiveModel(e.detail.model);
        if (e.detail?.provider_name) setActiveProvider(e.detail.provider_name);
      };

      window.addEventListener("omweb:model-change", onModelChange);
      return () => window.removeEventListener("omweb:model-change", onModelChange);
    }
  }, [setActiveModel, setActiveProvider]);

  const modelCaps = useMemo(() => {
    return inferModelCapabilities(activeModel, metadataVault[activeModel]);
  }, [activeModel, metadataVault]);

  const isReasoningSupported = Boolean(modelCaps?.isReasoning);

  const handleModeChange = (mode: "agent" | "chat") => {
    setExecMode(mode);
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("omweb:mode-change", { detail: mode }));
    }
  };

  const getActivePayload = (inputText = "") => {
    const isReasoning = isReasoningSupported;
    const effectiveEffort = isReasoning ? (reasoningEffort || "none") : "none";

    const directives = resolveExecutionDirectives(inputText, selectedMentions);
    const effectiveAgentId = directives.agentId || selectedAgentId || "peldrun";

    let base: Record<string, any> = {
      model: activeModel,
      provider: activeProvider.toLowerCase().replace(/[^a-z0-9]/g, ""),
      provider_name: activeProvider,
      base_url: "http://127.0.0.1:1234/v1",
      api_key: "",
      api_type: "",
      mode: directives.agentId ? "agent" : execMode,
      reasoning_effort: effectiveEffort,
      is_reasoning_model: isReasoning,
      agent_id: execMode === "agent" || directives.agentId ? effectiveAgentId : "peldrun",
    };

    if (directives.toolIds.length > 0) {
      base.tools = directives.toolIds;
    }

    if (activeLlmOverride) {
      try {
        const parsed = JSON.parse(activeLlmOverride);
        return {
          ...base,
          ...parsed,
          mode: base.mode,
          reasoning_effort: effectiveEffort,
          is_reasoning_model: isReasoning,
          agent_id: base.agent_id,
          tools: base.tools || parsed.tools,
        };
      } catch (e) {}
    }
    return base;
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const newFiles = Array.from(e.target.files);
      setAttachedFiles((prev) => [...prev, ...newFiles]);
    }
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const handleRemoveFile = (index: number) => {
    setAttachedFiles((prev) => prev.filter((_, idx) => idx !== index));
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const droppedFiles = Array.from(e.dataTransfer.files);
      setAttachedFiles((prev) => [...prev, ...droppedFiles]);
    }
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const handleEditorChange = useCallback(
    (val: string) => {
      setText(val);
      saveDraft(draftStorageKey, val);
    },
    [draftStorageKey]
  );

  const handleRestoreDraft = () => {
    if (!failedDraft) return;
    if (editorRef.current) {
      editorRef.current.setText(failedDraft.text);
    }
    setText(failedDraft.text);
    setAttachedFiles(failedDraft.files);
    saveDraft(draftStorageKey, failedDraft.text);
    setFailedDraft(null);
  };

  const handleSend = async () => {
    const rawText = editorRef.current ? editorRef.current.getText() : text;
    const trimmedText = rawText.trim();

    if (
      (!trimmedText && attachedFiles.length === 0) ||
      isRunning ||
      disabled ||
      isSubmitting
    ) {
      return;
    }

    const currentFiles = [...attachedFiles];
    const currentPayload = getActivePayload(trimmedText);

    backupDraftRef.current = {
      text: trimmedText,
      files: currentFiles,
    };

    try {
      setIsSubmitting(true);

      const sendResult = onSend(
        trimmedText,
        currentFiles.length > 0 ? currentFiles : undefined,
        currentPayload
      );

      let acceptedResponse: any = sendResult;
      if (sendResult && typeof (sendResult as any).then === "function") {
        acceptedResponse = await sendResult;
      }

      if (
        acceptedResponse &&
        typeof acceptedResponse === "object" &&
        "accepted" in acceptedResponse
      ) {
        if (!acceptedResponse.accepted || !acceptedResponse.jobId) {
          throw new Error("Job submission was not confirmed by the execution engine");
        }
      }

      editorRef.current?.clear();
      setText("");
      setAttachedFiles([]);
      setSelectedMentions([]);
      setFailedDraft(null);
      backupDraftRef.current = null;
      clearDraft(draftStorageKey);
    } catch (error) {
      console.error("[Composer] Message dispatch failed:", error);

      setFailedDraft({
        text: trimmedText,
        files: currentFiles,
      });

      if (editorRef.current) {
        editorRef.current.setText(trimmedText);
      }
      setText(trimmedText);
      setAttachedFiles(currentFiles);
    } finally {
      setIsSubmitting(false);
    }
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const dynamicPlaceholder =
    placeholder ||
    (execMode === "agent"
      ? `Ask ${activeProvider} (${activeModel}) to execute autonomous tasks...`
      : `Chat directly with ${activeProvider} (${activeModel})...`);

  const isInputEmpty = !text.trim() && attachedFiles.length === 0;

  return (
    <div className="relative w-full max-w-[1000px] mx-auto font-sans">
      <input
        type="file"
        multiple
        ref={fileInputRef}
        onChange={handleFileChange}
        className="hidden"
      />

      {failedDraft && (
        <div className="flex items-center justify-between mb-2 px-3 py-1.5 rounded-lg bg-destructive/10 border border-destructive/20 text-xs text-destructive animate-in fade-in slide-in-from-top-1">
          <div className="flex items-center gap-2">
            <AlertCircle size={14} className="shrink-0" />
            <span>Message dispatch failed. Your draft and files have been preserved.</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleRestoreDraft}
              className="font-medium underline hover:opacity-80 flex items-center gap-1 cursor-pointer"
            >
              <RotateCcw size={12} />
              <span>Restore Draft</span>
            </button>
            <button
              type="button"
              onClick={() => setFailedDraft(null)}
              className="p-0.5 rounded hover:bg-destructive/10 text-muted-foreground hover:text-foreground cursor-pointer"
              title="Dismiss"
            >
              <X size={12} />
            </button>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between mb-1.5 px-1">
        <EngineSelector direction="up" />
        <span className="text-[10px] text-muted-foreground">
          Press Enter to send, Shift+Enter for new line
        </span>
      </div>

      <div
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className={`relative flex flex-col w-full rounded-xl border bg-card shadow-lg transition-all peldrun-id-input-bar ${
          isDragging
            ? "border-primary ring-2 ring-primary/40 bg-primary/5"
            : "border-border focus-within:ring-1 focus-within:ring-muted focus-within:border-muted "
        }`}
      >
        {attachedFiles.length > 0 && (
          <div className="flex flex-col border-b border-border/40 bg-muted/20">
            <div className="flex flex-wrap gap-2 p-2.5 pb-1">
              {attachedFiles.map((file, idx) => (
                <div
                  key={`${file.name}-${idx}`}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs bg-card border border-border shadow-xs text-foreground animate-in fade-in"
                >
                  <FileText size={13} className="text-primary shrink-0" />
                  <span className="font-medium truncate max-w-[160px]" title={file.name}>
                    {file.name}
                  </span>
                  <span className="text-[10px] text-muted-foreground font-mono">
                    ({formatFileSize(file.size)})
                  </span>
                  <button
                    type="button"
                    onClick={() => handleRemoveFile(idx)}
                    className="p-0.5 rounded hover:bg-muted text-muted-foreground hover:text-foreground cursor-pointer ml-1"
                    title="Remove file"
                  >
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>
            <div className="px-2.5 pb-1.5 text-[10px] text-muted-foreground/70 flex items-center gap-1">
              <Info size={10} className="shrink-0" />
              <span>Unsent attachments are held in memory and upload upon confirmed task submission.</span>
            </div>
          </div>
        )}

        <LexicalEditorComponent
          ref={editorRef}
          chatId={activeSessionId}
          placeholder={
            attachedFiles.length > 0
              ? "Add instructions for attached files..."
              : dynamicPlaceholder
          }
          disabled={disabled || isRunning || isSubmitting}
          onSubmit={handleSend}
          onChange={handleEditorChange}
          contentClassName={
            isExpanded
              ? "min-h-[220px] max-h-[460px] px-4 py-3"
              : "min-h-[44px] max-h-[220px] px-4 py-3"
          }
          onClearEditor={() => {
            editorRef.current?.clear();
            setText("");
            setSelectedMentions([]);
            clearDraft(draftStorageKey);
          }}
        />

        <div className="flex items-center justify-between px-3 pb-2.5 pt-1">
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => fileInputRef.current?.click()}
              disabled={disabled || isRunning || isSubmitting}
              className="h-8 w-8 p-0 text-muted-foreground hover:text-foreground cursor-pointer hover:bg-muted relative disabled:opacity-40"
              title="Attach files or images"
            >
              <Paperclip size={15} />
              {attachedFiles.length > 0 && (
                <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-primary" />
              )}
            </Button>

            <div className="flex items-center bg-muted/60 p-0.5 rounded-lg border border-border/60 text-[11px]">
              <button
                type="button"
                onClick={() => handleModeChange("agent")}
                disabled={disabled || isRunning || isSubmitting}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md transition-all font-medium cursor-pointer disabled:opacity-40 ${
                  execMode === "agent"
                    ? "bg-card text-foreground shadow-xs font-semibold"
                    : "text-muted-foreground hover:text-foreground"
                }`}
                title="Autonomous Agent: multi-step planning, tool execution, bash & browser"
              >
                <Bot size={13} className={execMode === "agent" ? "text-primary" : ""} />
                <span>Agent</span>
              </button>
              <button
                type="button"
                onClick={() => handleModeChange("chat")}
                disabled={disabled || isRunning || isSubmitting}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md transition-all font-medium cursor-pointer disabled:opacity-40 ${
                  execMode === "chat"
                    ? "bg-card text-foreground shadow-xs font-semibold"
                    : "text-muted-foreground hover:text-foreground"
                }`}
                title="Direct Chat: fast response, conversational completion"
              >
                <MessageSquare size={13} className={execMode === "chat" ? "text-primary" : ""} />
                <span>Chat</span>
              </button>
            </div>

            {isReasoningSupported && (
              <div className="animate-in fade-in zoom-in-95 duration-200">
                <ReasoningEffortSelector disabled={disabled || isRunning || isSubmitting} />
              </div>
            )}

            {execMode === "agent" && (
              <div className="animate-in fade-in duration-150 flex items-center gap-1.5">
                <AgentSelector disabled={disabled || isRunning || isSubmitting} />
                <ActiveToolsModal />
              </div>
            )}
          </div>

          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setIsExpanded(!isExpanded)}
              className="h-8 w-8 p-0 text-muted-foreground hover:text-foreground cursor-pointer hover:bg-muted"
              title={isExpanded ? "Collapse Editor" : "Expand Editor"}
            >
              {isExpanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
            </Button>

            {isRunning ? (
              <Button
                type="button"
                onClick={onStop}
                size="sm"
                className="h-8 px-3 text-xs bg-rose-600 hover:bg-rose-700 text-white flex items-center gap-1.5 cursor-pointer shadow-xs"
              >
                <StopCircle size={14} />
                <span>Stop</span>
              </Button>
            ) : (
              <Button
                type="button"
                onClick={handleSend}
                disabled={isInputEmpty || disabled || isSubmitting}
                size="sm"
                className="h-8 px-3.5 text-xs bg-primary text-primary-foreground hover:opacity-90 flex items-center gap-1.5 cursor-pointer shadow-xs disabled:opacity-40"
              >
                <span>{isSubmitting ? "Sending..." : "Send"}</span>
                <Send size={13} />
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default Composer;
