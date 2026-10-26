// Path: frontend/src/components/chat/chat-landing.tsx

"use client";

import React, { useRef, useState, useEffect, useMemo } from "react";
import NeuralLoaderV3 from "@/components/NeuralLoaderV3";
import {
  Bot,
  MessageSquare,
  Paperclip,
  Sparkles,
  ArrowUp,
  FileText,
  X,
  Maximize2,
  Minimize2,
  Globe,
  GitFork,
  Info,
} from "lucide-react";
import { AgentSelector } from "./agent-selector";
import { ActiveToolsModal } from "./active-tools-modal";
import { ReasoningEffortSelector } from "./reasoning-effort-selector";
import { useAppStorage } from "@/hooks/use-app-storage";
import { inferModelCapabilities, fetchServerMetadata } from "@/lib/modelMetadata";
import { LexicalEditorComponent } from "./composer/lexical-editor";
import { LexicalEditorHandle } from "./composer/types";
import { commandRegistry } from "./composer/registry/command-registry";
import { useChatStore } from "@/stores/chat-store";

interface ChatLandingProps {
  inputValue: string;
  setInputValue: (val: string) => void;
  execMode: "agent" | "chat";
  handleModeChange: (mode: "agent" | "chat") => void;
  landingAttachedFiles: File[];
  setLandingAttachedFiles: React.Dispatch<React.SetStateAction<File[]>>;
  onStartTask: (prompt?: string, override?: any, files?: File[]) => void;
  formatFileSize: (bytes: number) => string;
}

export function ChatLanding({
  inputValue,
  setInputValue,
  execMode,
  handleModeChange,
  landingAttachedFiles,
  setLandingAttachedFiles,
  onStartTask,
  formatFileSize,
}: ChatLandingProps) {
  const [isLandingDragging, setIsLandingDragging] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);

  // Web Search: temporary per-message toggle, chat mode only.
  const [webSearchEnabled, setWebSearchEnabled] = useState(false);

  // Multi-Agent Delegation Toggle State
  const [delegationAllowed, setDelegationAllowed] = useAppStorage(
    "peldrun_delegation_allowed" as Parameters<typeof useAppStorage>[0]
  );

  const { selectedAgentId } = useChatStore();
  const isTeamLead = (selectedAgentId || "peldrun").toLowerCase() === "peldrun";

  // Multi-tier storage state synchronization
  const [activeModel, setActiveModel] = useAppStorage("active_model");
  const [activeProvider, setActiveProvider] = useAppStorage("active_provider");
  const [reasoningEffort] = useAppStorage("reasoning_effort");
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});

  const landingEditorRef = useRef<LexicalEditorHandle | null>(null);
  const landingFileInputRef = useRef<HTMLInputElement | null>(null);

  // Dynamically register active built-in agent mentions for autocomplete
  useEffect(() => {
    const unregisterPeldrun = commandRegistry.registerMentionItem({
      id: "agent-peldrun",
      label: "@peldrun",
      description: "Autonomous Master Agent",
      category: "agent",
      insertText: "`peldrun` ",
      mentionToken: "peldrun",
      metadata: { agentId: "peldrun" },
    });
    return () => {
      unregisterPeldrun();
    };
  }, []);

  // Synchronize server metadata for dynamic capability evaluation
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

  const handleLandingFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      setLandingAttachedFiles((prev) => [...prev, ...Array.from(e.target.files!)]);
    }
    if (landingFileInputRef.current) {
      landingFileInputRef.current.value = "";
    }
  };

  const handleDispatch = (customPrompt?: string) => {
    const rawText = landingEditorRef.current
      ? landingEditorRef.current.getText()
      : inputValue;
    const textToRun = (customPrompt !== undefined ? customPrompt : rawText).trim();
    if (!textToRun && landingAttachedFiles.length === 0) return;

    const payloadOverride = {
      is_reasoning_model: isReasoningSupported,
      reasoning_effort: isReasoningSupported ? (reasoningEffort || "none") : "none",
      // Attach web search flag (chat mode only)
      ...(execMode === "chat" && { web_search_enabled: webSearchEnabled }),
      // Attach delegation flag (agent mode only)
      ...(execMode === "agent" && { delegation_allowed: delegationAllowed }),
    };

    onStartTask(textToRun, payloadOverride, landingAttachedFiles);
    landingEditorRef.current?.clear();
    setInputValue("");
    setLandingAttachedFiles([]);
    setWebSearchEnabled(false); // Reset after dispatch
  };

  const dynamicPlaceholder =
    execMode === "agent"
      ? `Ask ${activeProvider} (${activeModel}) to execute autonomous tasks...`
      : `Chat directly with ${activeProvider} (${activeModel})...`;

  return (
    <div className="glow-wrapper flex-1 flex flex-col items-center justify-center p-6 text-center max-w-3xl mx-auto w-full font-sans relative">
      <div className="flex items-center justify-center chatlanding-neuralloader">
        <NeuralLoaderV3 size={100} color="blue" speed={30} />
      </div>

      <h1 className="font-serif text-3xl sm:text-5xl font-normal text-black dark:text-white tracking-tight mb-4">
        What can I do for you?
      </h1>

      <div className="flex items-center justify-center mb-6 gap-3 relative">
        <div className="inline-flex items-center bg-white-foreground p-1.5 rounded-full border border-border/80 shadow-md backdrop-blur-md gap-1">
          <button
            type="button"
            onClick={() => handleModeChange("agent")}
            className={`flex items-center gap-2 px-4 py-1.5 rounded-full text-xs transition-all duration-300 cursor-pointer ${
              execMode === "agent"
                ? "bg-card text-sky-500 border shadow-[0_0_8px_#0ea5e9] border-sky-500/50 font-semibold ring-1 ring-sky-500/30"
                : "text-muted-foreground hover:text-foreground border border-transparent"
            }`}
            title="Autonomous Agent: multi-step planning, tools & execution"
          >
            <Bot size={12} className={execMode === "agent" ? "text-sky-500 animate-pulse" : ""} />
            <span>Agent</span>
            {execMode === "agent" && (
              <span className="w-1.5 h-1.5 rounded-full bg-sky-500 shadow-[0_0_8px_#10b981] animate-pulse ring-sky-500/30" />
            )}
          </button>
          <button
            type="button"
            onClick={() => handleModeChange("chat")}
            className={`flex items-center gap-2 px-4 py-1.5 rounded-full text-xs transition-all duration-300 cursor-pointer ${
              execMode === "chat"
                ? "bg-card text-emerald-500 border border-emerald-500/50 shadow-[0_0_16px_rgba(14,165,233,0.38)] font-semibold ring-1 ring-emerald-500/30"
                : "text-muted-foreground hover:text-foreground border border-transparent"
            }`}
            title="Direct Chat: fast response, no tools or execution steps"
          >
            <MessageSquare size={14} className={execMode === "chat" ? "text-emerald-500 animate-pulse" : ""} />
            <span>Chat</span>
            {execMode === "chat" && (
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shadow-[0_0_16px_rgba(14,165,233,0.38)] animate-pulse" />
            )}
          </button>
        </div>
      </div>

      {/* Hidden File Input */}
      <input
        type="file"
        multiple
        ref={landingFileInputRef}
        onChange={handleLandingFileChange}
        className="hidden"
      />

      {/* Omnibar Input Box with Drag & Drop */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setIsLandingDragging(true);
        }}
        onDragLeave={(e) => {
          e.preventDefault();
          setIsLandingDragging(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setIsLandingDragging(false);
          if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            setLandingAttachedFiles((prev) => [...prev, ...Array.from(e.dataTransfer.files)]);
          }
        }}
        className={`w-full bg-card rounded-3xl transition-all p-3.5 peldrun-input-bar text-start ${
          isLandingDragging ? "ring-2 ring-primary/40 bg-card" : "bg-card"
        }`}
      >
        {/* Attached Files Badges Container */}
        {landingAttachedFiles.length > 0 && (
          <div className="flex flex-col border-b border-border/40 bg-muted/20 mb-2 pb-1">
            <div className="flex flex-wrap gap-2 p-2.5 pb-1">
              {landingAttachedFiles.map((file, idx) => (
                <div
                  key={`${file.name}-${idx}`}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs bg-muted/60 border border-border shadow-xs text-foreground animate-in fade-in"
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
                    onClick={() =>
                      setLandingAttachedFiles((prev) => prev.filter((_, i) => i !== idx))
                    }
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

        {/* Lexical Omnibar Editor */}
        <div className="w-full">
          <LexicalEditorComponent
            ref={landingEditorRef}
            placeholder={
              landingAttachedFiles.length > 0
                ? "Add instructions for attached files..."
                : dynamicPlaceholder
            }
            onSubmit={() => handleDispatch()}
            onChange={(val) => setInputValue(val)}
            contentClassName={
              isExpanded
                ? "min-h-[520px] max-h-[680px] px-2 py-2 leading-relaxed"
                : "min-h-[72px] max-h-[220px] px-1 py-1 leading-relaxed"
            }
            placeholderClassName={isExpanded ? "px-2 py-2" : "px-1 py-1"}
            className="w-full bg-card"
            onClearEditor={() => {
              landingEditorRef.current?.clear();
              setInputValue("");
            }}
          />
        </div>

        {/* Omnibar Bottom Action Bar */}
        <div className="flex items-center justify-between pt-2 border-t border-border/50 mt-1">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => landingFileInputRef.current?.click()}
              className="inline-flex items-center justify-center h-8 w-8 rounded-full hover:bg-muted text-muted-foreground hover:text-foreground transition-all cursor-pointer relative"
              title="Attach files or images"
            >
              <Paperclip size={15} />
              {landingAttachedFiles.length > 0 && (
                <span className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-primary" />
              )}
            </button>

            {/* Dynamic Reasoning Effort Selector */}
            {isReasoningSupported && (
              <div className="animate-in fade-in zoom-in-95 duration-200">
                <ReasoningEffortSelector />
              </div>
            )}

            {/* Web Search Toggle (Chat Mode Only) */}
            {execMode === "chat" && (
              <button
                type="button"
                onClick={() => setWebSearchEnabled((v) => !v)}
                className={`flex items-center text-xs font-medium transition-all cursor-pointer ${
                  webSearchEnabled
                    ? "text-rb shadow-xs"
                    : "text-muted-foreground"
                }`}
                title={
                  webSearchEnabled
                    ? "Web Search: ENABLED for the next message only"
                    : "Web Search: disabled. Click to enable for the next message."
                }
              >
                <Globe size={14} className={webSearchEnabled ? "text-rb" : "text-muted-foreground"} />
               </button>
            )}

            {/* Agent Selector, Tools, & Delegation (Agent Mode Only) */}
            {execMode === "agent" && (
              <div className="animate-in fade-in duration-150 flex items-center gap-1.5 z-50">
                <AgentSelector />
                <ActiveToolsModal />

                {/* Delegation Toggle with Strict Role Governance */}
                {isTeamLead ? (
                  <button
                    type="button"
                    onClick={() => setDelegationAllowed(!delegationAllowed)}
                    className={`flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium border transition-all cursor-pointer ${
                      delegationAllowed
                        ? "bg-primary/15 border-primary/40 text-primary shadow-xs"
                        : "bg-card hover:bg-muted border-border text-muted-foreground"
                    }`}
                    title={
                      delegationAllowed
                        ? "Multi-Agent Delegation: ENABLED (Leader can delegate subtasks)"
                        : "Multi-Agent Delegation: DISABLED (Single agent execution only)"
                    }
                  >
                    <GitFork size={12} className={delegationAllowed ? "text-primary" : "text-muted-foreground"} />
                    <span className="text-[11px]">Delegation</span>
                  </button>
                ) : (
                  <div
                    className="flex items-center gap-1 px-2 py-1 rounded-md text-xs border border-border/40 bg-muted/30 text-muted-foreground/50 cursor-not-allowed select-none"
                    title="Delegation is strictly restricted to Team Lead ('peldrun'). Manual specialists cannot delegate."
                  >
                    <GitFork size={12} />
                    <span className="text-[11px]">Solo</span>
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="flex items-center gap-1.5">
            {/* Expand / Collapse Omnibar Button */}
            <button
              type="button"
              onClick={() => setIsExpanded(!isExpanded)}
              className="h-8 w-8 rounded-full hover:bg-muted text-muted-foreground hover:text-foreground flex items-center justify-center transition-all cursor-pointer"
              title={isExpanded ? "Collapse Editor" : "Expand Editor"}
            >
              {isExpanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
            </button>

            {/* Dispatch Task Button */}
            <button
              type="button"
              onClick={() => handleDispatch()}
              disabled={!inputValue.trim() && landingAttachedFiles.length === 0}
              className="h-8 w-8 rounded-full bg-black dark:bg-white text-white dark:text-black flex items-center justify-center transition-all disabled:opacity-40 disabled:cursor-not-allowed shadow-peldrun-xs cursor-pointer hover:opacity-90"
              title="Dispatch Task (Enter)"
            >
              <ArrowUp size={15} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default ChatLanding;
