// Path: frontend/src/components/chat/chat-landing.tsx

"use client";

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import NeuralLoaderV3 from "@/components/NeuralLoaderV3";
import {
  Bot,
  MessageSquare,
  Paperclip,
  Sparkles,
  ArrowUp,
  FileText,
  Image as ImageIcon,
  X,
  Maximize2,
  Minimize2,
  Globe,
  GitFork,
  Info,
  AlertCircle,
  CheckCircle2,
  Loader2,
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

// ---------------------------------------------------------------------------
// Multimodal attachment model
// ---------------------------------------------------------------------------

/**
 * A single image attachment tracked by the landing composer.
 *
 * Only images use this structure. Non-image files continue to live in
 * the parent's ``landingAttachedFiles`` list untouched, preserving the
 * existing ``onStartTask`` contract. Images are uploaded to the
 * temporary artifact store immediately so the user sees live progress,
 * and only their ``fileId`` references travel to the backend once the
 * user dispatches the task.
 *
 * Note: this type intentionally duplicates ``PendingAttachment`` from
 * ``composer.tsx``. The two composers are independent components with
 * different lifecycle contracts; sharing a type between them would
 * couple their evolution. If a third consumer appears, extract this
 * to a shared module then.
 */
interface PendingImageAttachment {
  id: string;
  file: File;
  fileName: string;
  fileSize: number;
  mimeType: string;
  status: "uploading" | "ready" | "error";
  fileId?: string;
  width?: number;
  height?: number;
  errorMessage?: string;
  previewUrl: string;
}

interface UploadTempResponse {
  file_id: string;
  mime_type: string;
  size_bytes: number;
  width?: number | null;
  height?: number | null;
  filename?: string;
  url?: string;
}

async function uploadImageToTemp(file: File): Promise<UploadTempResponse> {
  const formData = new FormData();
  formData.append("file", file);

  const res = await fetch("/api/files/upload-temp", {
    method: "POST",
    body: formData,
  });

  if (!res.ok) {
    let detail = "";
    try {
      detail = await res.text();
    } catch {
      /* ignore */
    }
    throw new Error(
      `Upload failed (${res.status}${detail ? `: ${detail.slice(0, 120)}` : ""})`
    );
  }

  return (await res.json()) as UploadTempResponse;
}

function generateImageAttachmentId(): string {
  return `img_att_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

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
  const [pendingImageAttachments, setPendingImageAttachments] = useState<
    PendingImageAttachment[]
  >([]);

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

  // Revoke any outstanding preview URLs on unmount. A ref keeps the
  // latest list accessible from the cleanup closure without re-running
  // the cleanup on every state change.
  const pendingImageAttachmentsRef = useRef<PendingImageAttachment[]>([]);
  useEffect(() => {
    pendingImageAttachmentsRef.current = pendingImageAttachments;
  }, [pendingImageAttachments]);
  useEffect(() => {
    return () => {
      pendingImageAttachmentsRef.current.forEach((att) => {
        if (att.previewUrl) {
          try {
            URL.revokeObjectURL(att.previewUrl);
          } catch {
            /* ignore */
          }
        }
      });
    };
  }, []);

  // Dynamically resolve whether the currently active model supports reasoning
  const modelCaps = useMemo(() => {
    return inferModelCapabilities(
      activeModel,
      activeModel ? metadataVault[activeModel] : null
    );
  }, [activeModel, metadataVault]);

  const isReasoningSupported = Boolean(modelCaps?.isReasoning);

  /**
   * Route a batch of incoming files through the correct pipeline:
   *
   *   * Images  -> internal ``pendingImageAttachments`` list, uploaded
   *                to the temporary artifact store immediately.
   *   * Others  -> parent's ``landingAttachedFiles`` list, unchanged
   *                from the pre-multimodal behavior.
   *
   * This split preserves the parent's contract (``onStartTask`` keeps
   * receiving the same ``File[]`` it always has) while giving images a
   * dedicated upload lifecycle so the vision pipeline can resolve them
   * later via their server-assigned ``fileId``.
   */
  const ingestLandingFiles = useCallback(
    (incoming: File[]) => {
      if (incoming.length === 0) return;

      const imageFiles: File[] = [];
      const nonImageFiles: File[] = [];

      for (const file of incoming) {
        if (file.type.startsWith("image/")) {
          imageFiles.push(file);
        } else {
          nonImageFiles.push(file);
        }
      }

      // Non-images keep going through the parent's existing flow.
      if (nonImageFiles.length > 0) {
        setLandingAttachedFiles((prev) => [...prev, ...nonImageFiles]);
      }

      if (imageFiles.length === 0) return;

      const newAttachments: PendingImageAttachment[] = imageFiles.map(
        (file) => ({
          id: generateImageAttachmentId(),
          file,
          fileName: file.name || "pasted-image",
          fileSize: file.size,
          mimeType: file.type || "image/png",
          status: "uploading" as const,
          previewUrl: URL.createObjectURL(file),
        })
      );

      setPendingImageAttachments((prev) => [...prev, ...newAttachments]);

      // Kick off uploads in parallel. Each completion mutates only its
      // own attachment, so concurrent pastes never clobber each other.
      newAttachments.forEach((att) => {
        const attId = att.id;
        void (async () => {
          try {
            const response = await uploadImageToTemp(att.file);
            setPendingImageAttachments((prev) =>
              prev.map((current) =>
                current.id === attId
                  ? {
                      ...current,
                      status: "ready",
                      fileId: response.file_id,
                      mimeType: response.mime_type || current.mimeType,
                      width: response.width ?? undefined,
                      height: response.height ?? undefined,
                    }
                  : current
              )
            );
          } catch (error) {
            console.error(
              "[ChatLanding] Image upload failed for",
              att.fileName,
              error
            );
            setPendingImageAttachments((prev) =>
              prev.map((current) =>
                current.id === attId
                  ? {
                      ...current,
                      status: "error",
                      errorMessage:
                        error instanceof Error
                          ? error.message
                          : String(error),
                    }
                  : current
              )
            );
          }
        })();
      });
    },
    [setLandingAttachedFiles]
  );

  const handleLandingFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      ingestLandingFiles(Array.from(e.target.files));
    }
    if (landingFileInputRef.current) {
      landingFileInputRef.current.value = "";
    }
  };

  /**
   * ImagePastePlugin callback. Uniform with picker and drop: images
   * always flow through the same immediate-upload path.
   */
  const handleImagePasted = useCallback(
    (file: File) => {
      ingestLandingFiles([file]);
    },
    [ingestLandingFiles]
  );

  const handleRemoveImageAttachment = useCallback((id: string) => {
    setPendingImageAttachments((prev) => {
      const target = prev.find((att) => att.id === id);
      if (target?.previewUrl) {
        try {
          URL.revokeObjectURL(target.previewUrl);
        } catch {
          /* ignore */
        }
      }
      return prev.filter((att) => att.id !== id);
    });
  }, []);

  const handleDispatch = (customPrompt?: string) => {
    const rawText = landingEditorRef.current
      ? landingEditorRef.current.getText()
      : inputValue;
    const textToRun = (customPrompt !== undefined ? customPrompt : rawText).trim();

    const hasImages = pendingImageAttachments.length > 0;
    if (!textToRun && landingAttachedFiles.length === 0 && !hasImages) {
      return;
    }

    // Only fully-uploaded images travel to the backend. Still-uploading
    // or errored entries are excluded: the backend would otherwise
    // receive null file IDs and the engine would treat each reference
    // as broken. The user can retry dispatch once uploads settle.
    const readyImageRefs = pendingImageAttachments
      .filter((att) => att.status === "ready" && att.fileId)
      .map((att) => ({
        fileId: att.fileId as string,
        mimeType: att.mimeType,
        fileName: att.fileName,
        width: att.width,
        height: att.height,
      }));

    const payloadOverride = {
      is_reasoning_model: isReasoningSupported,
      reasoning_effort: isReasoningSupported ? (reasoningEffort || "none") : "none",
      // Attach web search flag (chat mode only)
      ...(execMode === "chat" && { web_search_enabled: webSearchEnabled }),
      // Attach delegation flag (agent mode only)
      ...(execMode === "agent" && { delegation_allowed: delegationAllowed }),
      // Multimodal image references. Consumed by start_run.py and
      // forwarded to the engine as llm_override["attachments"].
      // Omitted entirely when no image finished uploading successfully.
      ...(readyImageRefs.length > 0 ? { attachments: readyImageRefs } : {}),
    };

    onStartTask(textToRun, payloadOverride, landingAttachedFiles);

    // Success path: revoke preview URLs before clearing state so the
    // Blob references release immediately rather than waiting for GC.
    pendingImageAttachments.forEach((att) => {
      if (att.previewUrl) {
        try {
          URL.revokeObjectURL(att.previewUrl);
        } catch {
          /* ignore */
        }
      }
    });

    landingEditorRef.current?.clear();
    setInputValue("");
    setLandingAttachedFiles([]);
    setPendingImageAttachments([]);
    setWebSearchEnabled(false); // Reset after dispatch
  };

  const displayProvider = activeProvider || "your provider";
  const displayModel = activeModel || "your model";
  const dynamicPlaceholder =
    execMode === "agent"
      ? `Ask ${displayProvider} (${displayModel}) to execute autonomous tasks...`
      : `Chat directly with ${displayProvider} (${displayModel})...`;

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
            ingestLandingFiles(Array.from(e.dataTransfer.files));
          }
        }}
        className={`w-full bg-card rounded-3xl transition-all p-3.5 peldrun-input-bar text-start ${
          isLandingDragging ? "ring-2 ring-primary/40 bg-card" : "bg-card"
        }`}
      >
        {/* Attached Files & Image Badges Container */}
        {(landingAttachedFiles.length > 0 ||
          pendingImageAttachments.length > 0) && (
          <div className="flex flex-col border-b border-border/40 bg-muted/20 mb-2 pb-1">
            <div className="flex flex-wrap gap-2 p-2.5 pb-1">
              {/* Non-image files: legacy pipeline, held in parent state. */}
              {landingAttachedFiles.map((file, idx) => (
                <div
                  key={`legacy-${file.name}-${idx}`}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs bg-muted/60 border border-border shadow-xs text-foreground animate-in fade-in"
                >
                  <FileText size={13} className="text-primary shrink-0" />
                  <span
                    className="font-medium truncate max-w-[160px]"
                    title={file.name}
                  >
                    {file.name}
                  </span>
                  <span className="text-[10px] text-muted-foreground font-mono">
                    ({formatFileSize(file.size)})
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      setLandingAttachedFiles((prev) =>
                        prev.filter((_, i) => i !== idx)
                      )
                    }
                    className="p-0.5 rounded hover:bg-muted text-muted-foreground hover:text-foreground cursor-pointer ml-1"
                    title="Remove file"
                  >
                    <X size={12} />
                  </button>
                </div>
              ))}

              {/* Images: live upload pipeline, held in internal state. */}
              {pendingImageAttachments.map((att) => (
                <div
                  key={att.id}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs bg-muted/60 border border-border shadow-xs text-foreground animate-in fade-in"
                >
                  {att.previewUrl ? (
                    <img
                      src={att.previewUrl}
                      alt={att.fileName}
                      className="w-5 h-5 rounded object-cover shrink-0 border border-border/40"
                    />
                  ) : (
                    <ImageIcon size={13} className="text-primary shrink-0" />
                  )}
                  <span
                    className="font-medium truncate max-w-[160px]"
                    title={att.fileName}
                  >
                    {att.fileName}
                  </span>
                  <span className="text-[10px] text-muted-foreground font-mono">
                    ({formatFileSize(att.fileSize)})
                  </span>
                  {att.status === "uploading" && (
                    <Loader2
                      size={12}
                      className="text-primary animate-spin shrink-0"
                    />
                  )}
                  {att.status === "ready" && (
                    <CheckCircle2
                      size={12}
                      className="text-emerald-500 shrink-0"
                    />
                  )}
                  {att.status === "error" && (
                    <AlertCircle
                      size={12}
                      className="text-rose-500 shrink-0"
                      aria-label={att.errorMessage || "Upload failed"}
                    />
                  )}
                  <button
                    type="button"
                    onClick={() => handleRemoveImageAttachment(att.id)}
                    className="p-0.5 rounded hover:bg-muted text-muted-foreground hover:text-foreground cursor-pointer ml-1"
                    title="Remove image"
                  >
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>
            <div className="px-2.5 pb-1.5 text-[10px] text-muted-foreground/70 flex items-center gap-1">
              <Info size={10} className="shrink-0" />
              <span>
                Images upload immediately. Non-image files are held in
                memory and upload upon confirmed task submission.
              </span>
            </div>
          </div>
        )}

        {/* Lexical Omnibar Editor */}
        <div className="w-full">
          <LexicalEditorComponent
            ref={landingEditorRef}
            placeholder={
              landingAttachedFiles.length > 0 ||
              pendingImageAttachments.length > 0
                ? "Add instructions for attached files..."
                : dynamicPlaceholder
            }
            onSubmit={() => handleDispatch()}
            onChange={(val) => setInputValue(val)}
            onImagePasted={handleImagePasted}
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
              {(landingAttachedFiles.length > 0 ||
                pendingImageAttachments.length > 0) && (
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
              disabled={
                !inputValue.trim() &&
                landingAttachedFiles.length === 0 &&
                pendingImageAttachments.length === 0
              }
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
