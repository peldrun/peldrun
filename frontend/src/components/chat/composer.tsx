"use client";

import React, { useState, useEffect, useRef, useMemo } from "react";
import {
  Send,
  Paperclip,
  StopCircle,
  Bot,
  MessageSquare,
  FileText,
  X
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { AgentSelector } from "./agent-selector";
import { EngineSelector } from "./engine-selector";
import { ActiveToolsModal } from "./active-tools-modal";
import { ReasoningEffortSelector } from "./reasoning-effort-selector";
import { useChatStore } from "@/stores/chat-store";
import { useAppStorage } from "@/hooks/use-app-storage";
import { inferModelCapabilities, fetchServerMetadata } from "@/lib/modelMetadata";

interface ComposerProps {
  onSend: (text: string, files?: File[], llmOverride?: any) => void;
  onStop?: () => void;
  isRunning?: boolean;
  disabled?: boolean;
  placeholder?: string;
}

export function Composer({ onSend, onStop, isRunning, disabled, placeholder }: ComposerProps) {
  const [text, setText] = useState("");
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});

  // Unified multi-tier reactive storage
  const [activeModel, setActiveModel] = useAppStorage("active_model");
  const [activeProvider, setActiveProvider] = useAppStorage("active_provider");
  const [execMode, setExecMode] = useAppStorage("exec_mode");
  const [reasoningEffort] = useAppStorage("reasoning_effort");
  const [activeLlmOverride] = useAppStorage("active_llm_override");

  const { selectedAgentId } = useChatStore();

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Synchronize server metadata for dynamic model capability resolution
  useEffect(() => {
    fetchServerMetadata().then((data) => setMetadataVault(data || {}));
    const onMetadataUpdate = (e: any) => setMetadataVault(e.detail || {});
    window.addEventListener("omweb:metadata-updated", onMetadataUpdate);
    return () => window.removeEventListener("omweb:metadata-updated", onMetadataUpdate);
  }, []);

  // Listen to live engine and model changes
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

  // Dynamically resolve whether the currently active model supports reasoning
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

  const getActivePayload = () => {
    const isReasoning = isReasoningSupported;
    const effectiveEffort = isReasoning ? (reasoningEffort || "none") : "none";

    let base = {
      model: activeModel,
      provider: activeProvider.toLowerCase().replace(/[^a-z0-9]/g, ""),
      provider_name: activeProvider,
      base_url: "http://127.0.0.1:1234/v1",
      api_key: "",
      api_type: "",
      mode: execMode,
      reasoning_effort: effectiveEffort,
      is_reasoning_model: isReasoning,
      agent_id: execMode === "agent" ? (selectedAgentId || "peldrun") : "peldrun"
    };

    if (activeLlmOverride) {
      try {
        const parsed = JSON.parse(activeLlmOverride);
        return {
          ...base,
          ...parsed,
          mode: execMode,
          reasoning_effort: effectiveEffort,
          is_reasoning_model: isReasoning,
          agent_id: base.agent_id
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

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSend = () => {
    if ((!text.trim() && attachedFiles.length === 0) || isRunning || disabled) return;
    const currentPayload = getActivePayload();
    onSend(text.trim(), attachedFiles.length > 0 ? attachedFiles : undefined, currentPayload);
    setText("");
    setAttachedFiles([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const dynamicPlaceholder = placeholder || (
    execMode === "agent"
      ? `Ask ${activeProvider} (${activeModel}) to execute autonomous tasks...`
      : `Chat directly with ${activeProvider} (${activeModel})...`
  );

  return (
    <div className="relative w-full max-w-[1000px] mx-auto font-sans">
      {/* Hidden File Input */}
      <input
        type="file"
        multiple
        ref={fileInputRef}
        onChange={handleFileChange}
        className="hidden"
      />

      {/* Top Model & Engine Switcher Bar */}
      <div className="flex items-center justify-between mb-1.5 px-1">
        <EngineSelector direction="up" />
        <span className="text-[10px] text-muted-foreground">Press Enter to send, Shift+Enter for new line</span>
      </div>

      {/* Floating Omnibar Input Box with Drag & Drop */}
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
        {/* Attached Files Badges Container */}
        {attachedFiles.length > 0 && (
          <div className="flex flex-wrap gap-2 p-2.5 pb-1 border-b border-border/40 bg-muted/20">
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
        )}

        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            e.target.style.height = "auto";
            e.target.style.height = `${Math.min(e.target.scrollHeight, 220)}px`;
          }}
          onKeyDown={handleKeyDown}
          placeholder={attachedFiles.length > 0 ? "Add instructions for attached files..." : dynamicPlaceholder}
          rows={1}
          disabled={disabled}
          className="w-full resize-none bg-transparent px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none max-h-[220px] font-sans"
        />

        <div className="flex items-center justify-between px-3 pb-2.5 pt-1">
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => fileInputRef.current?.click()}
              className="h-8 w-8 p-0 text-muted-foreground hover:text-foreground cursor-pointer hover:bg-muted relative"
              title="Attach files or images"
            >
              <Paperclip size={15} />
              {attachedFiles.length > 0 && (
                <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-primary" />
              )}
            </Button>

            {/* Mode Toggle Control */}
            <div className="flex items-center bg-muted/60 p-0.5 rounded-lg border border-border/60 text-[11px]">
              <button
                type="button"
                onClick={() => handleModeChange("agent")}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md transition-all font-medium cursor-pointer ${
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
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md transition-all font-medium cursor-pointer ${
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

            {/* Dynamic Reasoning Effort Selector: Appears or hides based on active model reasoning support */}
            {isReasoningSupported && (
              <div className="animate-in fade-in zoom-in-95 duration-200">
                <ReasoningEffortSelector disabled={disabled || isRunning} />
              </div>
            )}

            {/* Agent Selector & Active Tools Modal: Visible ONLY when execMode === 'agent' */}
            {execMode === "agent" && (
              <div className="animate-in fade-in duration-150 flex items-center gap-1.5">
                <AgentSelector disabled={disabled || isRunning} />
                <ActiveToolsModal />
              </div>
            )}
          </div>

          <div className="flex items-center gap-2">
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
                disabled={(!text.trim() && attachedFiles.length === 0) || disabled}
                size="sm"
                className="h-8 px-3.5 text-xs bg-primary text-primary-foreground hover:opacity-90 flex items-center gap-1.5 cursor-pointer shadow-xs disabled:opacity-40"
              >
                <span>Send</span>
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