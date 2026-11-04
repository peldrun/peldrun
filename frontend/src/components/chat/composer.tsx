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
  Image as ImageIcon,
  X,
  Maximize2,
  Minimize2,
  RotateCcw,
  AlertCircle,
  CheckCircle2,
  Loader2,
  Info,
  GitFork,
  Globe,
  Sparkles,
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

// ---------------------------------------------------------------------------
// Multimodal attachment model
// ---------------------------------------------------------------------------

/**
 * A single attachment tracked by the composer.
 *
 * Images are uploaded to the temporary artifact store on add, so they
 * carry a server-assigned ``fileId`` once the upload succeeds. Non-image
 * files are kept in memory and forwarded to the parent's ``onSend``
 * handler as raw ``File`` objects (the legacy upload path).
 *
 * ``previewUrl`` holds a ``URL.createObjectURL`` handle for images so the
 * chip row can render a thumbnail. It MUST be revoked when the attachment
 * is removed or the component unmounts; otherwise the underlying Blob is
 * leaked for the lifetime of the tab.
 */
export interface PendingAttachment {
  id: string;
  file: File;
  fileName: string;
  fileSize: number;
  mimeType: string;
  isImage: boolean;
  status: "uploading" | "ready" | "error";
  // For images: opaque fileId returned by /api/files/upload-temp.
  fileId?: string;
  // For non-images: workspace-relative path returned by
  // /api/files/upload. This is the value forwarded to the backend as
  // ``context_refs[].relPath``, so the dispatcher's
  // ``inject_referenced_files`` can find the file and inline its
  // CONTENT into the prompt (with PDF extraction + binary detection
  // handled inside ``mention_resolver.py``).
  //
  // Exactly one of ``fileId`` / ``relPath`` is populated on success,
  // discriminated by ``isImage``.
  relPath?: string;
  width?: number;
  height?: number;
  errorMessage?: string;
  previewUrl?: string;
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

function generateAttachmentId(): string {
  return `att_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

// ---------------------------------------------------------------------------
// Workspace upload for non-image files
// ---------------------------------------------------------------------------
//
// Non-image files (PDF, TXT, CSV, DOCX, ...) are uploaded DIRECTLY to
// the chat workspace at attach time via ``POST /api/files/upload``.
// This mirrors the ChatLanding flow and produces a workspace-relative
// ``relPath`` that the backend's ``mention_resolver`` can read.
//
// Why not defer the upload to send time (the pre-fix behavior)?
//   1. ``ChatContainer.handleStartTask`` re-uploads files passed via
//      its ``filesToUpload`` argument, which produces a duplicate copy
//      AND appends a useless ``[Uploaded Files]: name (in workspace
//      directory)`` marker to the prompt. That marker gives the model
//      no way to actually READ the content — the prompt contains only
//      the filename, and chat mode has no tool-calling loop to read
//      it. The result was that Chat-mode attachments were ignored
//      entirely by the model.
//   2. Uploading eagerly gives the user real progress feedback
//      (spinner → check) and surfaces network errors immediately,
//      instead of losing the file silently at send time.
interface UploadWorkspaceResponse {
  files: Array<{ name: string; path: string; size?: number }>;
}

async function uploadFileToWorkspace(
  file: File,
  chatId: string,
): Promise<{ name: string; path: string }> {
  const formData = new FormData();
  formData.append("files", file);
  formData.append("chat_id", chatId);

  const res = await fetch("/api/files/upload", {
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

  const data = (await res.json()) as UploadWorkspaceResponse;
  const uploaded = (data.files || [])[0];
  if (!uploaded || !uploaded.path) {
    throw new Error(
      "Server returned no workspace path for the uploaded file."
    );
  }
  return { name: uploaded.name || file.name, path: uploaded.path };
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
  const [pendingAttachments, setPendingAttachments] = useState<
    PendingAttachment[]
  >([]);
  const [selectedMentions, setSelectedMentions] = useState<MentionItem[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});
  const [isExpanded, setIsExpanded] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Web Search: temporary per-message toggle, chat mode only.
  const [webSearchEnabled, setWebSearchEnabled] = useState(false);

  // Multi-Agent Delegation Toggle State
  const [delegationAllowed, setDelegationAllowed] = useAppStorage(
    "peldrun_delegation_allowed" as Parameters<typeof useAppStorage>[0]
  );

  // ── Skills ────────────────────────────────────────────────────
  // `activeSkills` holds the skill IDs the user explicitly selected
  // from the / menu in the current draft. They are injected into the
  // run payload as `invoked_skills` and merged by the backend with
  // the agent's assigned skills at prompt-injection time.
  //
  // `availableSkills` is the catalogue of chat-invocable skills
  // fetched from /api/skills. It is used only to render skill
  // metadata (name, description) on the chips; the runtime uses the
  // IDs.
  const [activeSkills, setActiveSkills] = useState<string[]>([]);
  const [availableSkills, setAvailableSkills] = useState<
    Array<{
      id: string;
      name: string;
      description: string;
      short_description?: string;
      tags?: string[];
    }>
  >([]);

  const [failedDraft, setFailedDraft] = useState<{
    text: string;
  } | null>(null);
  const backupDraftRef = useRef<{
    text: string;
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

  // Two independent subscriptions. The read selector fires re-renders only
  // when `selectedAgentId` changes; the setter selector is stable across
  // renders and therefore never triggers a re-render on its own. See
  // docs/state-contracts.md §6.4.
  const selectedAgentId = useChatStore((s) => s.selectedAgentId);
  const setSelectedAgentId = useChatStore((s) => s.setSelectedAgentId);

  const editorRef = useRef<LexicalEditorHandle>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {

    const unregisterPeldrun = commandRegistry.registerMentionItem({
      id: "agent-peldrun",
      label: "@peldrun",
      description: "Autonomous Master Agent",
      category: "agent",
      insertText: "`peldrun` ",
      mentionToken: "peldrun",
      metadata: {
        agentId: "peldrun",
      },
    });

    return () => {
      unregisterPeldrun();
    };
  }, []);

  // ── Skills: load catalogue and register each as a slash command ──
  //
  // Fetches /api/skills once on mount and re-fetches whenever a
  // `peldrun:skills-changed` event fires (dispatched by the /store
  // skills section after install/delete/toggle). Every
  // chat-invocable skill is registered as a SlashCommandItem; its
  // `execute` handler does NOT insert text but dispatches
  // `peldrun:skill-invoked`, which the next effect observes to add
  // the skill ID to `activeSkills`.
  useEffect(() => {
    if (typeof window === "undefined") return;

    let cancelled = false;
    let unregisterFns: Array<() => void> = [];

    const loadAndRegisterSkills = async () => {
      unregisterFns.forEach((fn) => fn());
      unregisterFns = [];

      try {
        const res = await fetch("/api/skills?include_disabled=false");
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const skills = (Array.isArray(data.skills) ? data.skills : []).filter(
          (s: any) =>
            s &&
            typeof s.id === "string" &&
            s.enabled === true &&
            s.chat_invocable === true &&
            s.is_deprecated !== true
        );
        if (cancelled) return;

        setAvailableSkills(skills);

        for (const skill of skills) {
          const unregister = commandRegistry.registerSlashCommand({
            id: `skill-${skill.id}`,
            trigger: skill.id,
            label: `/${skill.id}`,
            description:
              skill.short_description ||
              skill.description ||
              `Invoke skill ${skill.id}`,
            category: "skill",
            icon: "Sparkles",
            searchTerms: [
              skill.name,
              skill.display_name,
              ...(Array.isArray(skill.tags) ? skill.tags : []),
            ].filter(
              (t): t is string =>
                typeof t === "string" && t.trim().length > 0
            ),
            execute: ({ dispatchCustomEvent }) => {
              dispatchCustomEvent("peldrun:skill-invoked", {
                skillId: skill.id,
              });
            },
          });
          unregisterFns.push(unregister);
        }
      } catch (err) {
        console.debug(
          "[Composer] Failed to load skills catalogue:",
          err
        );
      }
    };

    void loadAndRegisterSkills();

    const onSkillsChanged = () => {
      void loadAndRegisterSkills();
    };
    window.addEventListener("peldrun:skills-changed", onSkillsChanged);

    return () => {
      cancelled = true;
      window.removeEventListener(
        "peldrun:skills-changed",
        onSkillsChanged
      );
      unregisterFns.forEach((fn) => fn());
    };
  }, []);

  // ── Skills: listen for selections from the / menu ─────────────
  useEffect(() => {
    if (typeof window === "undefined") return;

    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ skillId?: string }>).detail;
      const skillId = detail?.skillId;
      if (typeof skillId !== "string" || !skillId) return;

      setActiveSkills((prev) =>
        prev.includes(skillId) ? prev : [...prev, skillId]
      );
    };

    window.addEventListener("peldrun:skill-invoked", handler as EventListener);
    return () => {
      window.removeEventListener(
        "peldrun:skill-invoked",
        handler as EventListener
      );
    };
  }, []);

  useEffect(() => {
    const handleMentionSelect = (event: Event) => {
      const item = (event as CustomEvent<MentionItem>).detail;
      if (!item) return;

      // [FIX-2.1] Accept file category in addition to agent and tool.
      // File mentions are tracked in `selectedMentions` and later projected
      // into `context_refs` for the run payload.
      if (
        item.category !== "agent" &&
        item.category !== "tool" &&
        item.category !== "file"
      ) {
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

  // Revoke any outstanding preview URLs when the composer unmounts.
  // Without this, every thumbnail created via URL.createObjectURL leaks
  // its Blob for the lifetime of the tab. A ref holds the latest state
  // so the cleanup runs against the correct list even after the initial
  // render (the empty-array capture would otherwise be a no-op).
  const pendingAttachmentsRef = useRef<PendingAttachment[]>([]);
  useEffect(() => {
    pendingAttachmentsRef.current = pendingAttachments;
  }, [pendingAttachments]);
  useEffect(() => {
    return () => {
      pendingAttachmentsRef.current.forEach((att) => {
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

  const modelCaps = useMemo(() => {
    return inferModelCapabilities(
      activeModel,
      activeModel ? metadataVault[activeModel] : null
    );
  }, [activeModel, metadataVault]);

  const isReasoningSupported = Boolean(modelCaps?.isReasoning);

  // Role Governance: Delegation is ONLY allowed for the primary Team Lead ('peldrun')
  const isTeamLead = (selectedAgentId || "peldrun").toLowerCase() === "peldrun";

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

    // Strictly enforce role-based delegation permission in payload
    const canDelegate = execMode === "agent" && isTeamLead && Boolean(delegationAllowed);

    // When no model/provider has been selected yet, omit those keys
    // entirely. The backend will then use its config.toml defaults.
    // Sending empty strings would override the backend defaults with
    // empty values and break inference.
    let base: Record<string, any> = {
      ...(activeModel ? { model: activeModel } : {}),
      ...(activeProvider
        ? {
            provider: activeProvider.toLowerCase().replace(/[^a-z0-9]/g, ""),
            provider_name: activeProvider,
          }
        : {}),
      // NOTE: base_url remains hardcoded for now (audit report item 1.2).
      // This is a frontend-side override of the backend LM Studio URL,
      // not a hydration concern. See roadmap for architectural cleanup.
      base_url: "http://127.0.0.1:1234/v1",
      api_key: "",
      api_type: "",
      mode: directives.agentId ? "agent" : execMode,
      reasoning_effort: effectiveEffort,
      is_reasoning_model: isReasoning,
      agent_id:
        execMode === "agent" || directives.agentId ? effectiveAgentId : "peldrun",
      delegation_allowed: canDelegate,
    };

    if (directives.toolIds.length > 0) {
      base.tools = directives.toolIds;
    }

    // Web Search is a chat-only, per-message opt-in. Never sent in agent mode.
    if (execMode === "chat" && !directives.agentId) {
      base.web_search_enabled = webSearchEnabled;
    }

    if (activeLlmOverride) {
      try {
        const parsed = JSON.parse(activeLlmOverride);
        const merged: Record<string, any> = {
          ...base,
          ...parsed,
          mode: base.mode,
          reasoning_effort: effectiveEffort,
          is_reasoning_model: isReasoning,
          agent_id: base.agent_id,
          delegation_allowed: canDelegate,
          tools: base.tools || parsed.tools,
        };
        // Preserve the chat-only web search flag across the override merge.
        if (execMode === "chat" && !directives.agentId) {
          merged.web_search_enabled = webSearchEnabled;
        } else {
          delete merged.web_search_enabled;
        }
        return merged;
      } catch (e) {}
    }
    return base;
  };

  /**
   * Route a batch of user-supplied files through the correct pipeline:
   * images are uploaded to the temporary artifact store immediately
   * (so the user sees progress), while non-images stay in memory and
   * are forwarded to the parent's ``onSend`` handler as raw ``File``
   * objects via the legacy upload path.
   */
  const ingestFiles = useCallback((incoming: File[]) => {
    if (incoming.length === 0) return;

    // A chat id is required to upload non-image files to the correct
    // workspace. In the normal flow ``activeSessionId`` is set (the
    // composer only renders after the first message), but if it is
    // missing for any reason we fall back to the pre-fix behavior
    // (hold in memory, upload on send via ChatContainer) rather than
    // uploading to a random workspace that the run would not read.
    const uploadChatId = activeSessionId || "";

    const newAttachments: PendingAttachment[] = [];
    const imagesToUpload: Array<{ id: string; file: File }> = [];
    const filesToUpload: Array<{ id: string; file: File }> = [];

    for (const file of incoming) {
      const isImage = file.type.startsWith("image/");
      const id = generateAttachmentId();

      if (isImage) {
        newAttachments.push({
          id,
          file,
          fileName: file.name || "pasted-image",
          fileSize: file.size,
          mimeType: file.type || "image/png",
          isImage: true,
          status: "uploading",
          previewUrl: URL.createObjectURL(file),
        });
        imagesToUpload.push({ id, file });
      } else {
        // Non-image: start as "uploading" so the user sees live
        // progress. Without a chat id we cannot upload, so fall back
        // to "ready" with a note (ChatContainer will handle it as a
        // legacy File-object upload, which still gives a chip but no
        // content inlining — matching the pre-fix behavior).
        newAttachments.push({
          id,
          file,
          fileName: file.name,
          fileSize: file.size,
          mimeType: file.type || "application/octet-stream",
          isImage: false,
          status: uploadChatId ? "uploading" : "ready",
        });
        if (uploadChatId) {
          filesToUpload.push({ id, file });
        }
      }
    }

    setPendingAttachments((prev) => [...prev, ...newAttachments]);

    // Kick off image uploads in parallel. Each upload only mutates its
    // own attachment, so concurrent pastes never clobber each other.
    imagesToUpload.forEach(({ id, file }) => {
      void (async () => {
        try {
          const response = await uploadImageToTemp(file);
          setPendingAttachments((prev) =>
            prev.map((att) =>
              att.id === id
                ? {
                    ...att,
                    status: "ready",
                    fileId: response.file_id,
                    mimeType: response.mime_type || att.mimeType,
                    width: response.width ?? undefined,
                    height: response.height ?? undefined,
                  }
                : att
            )
          );
        } catch (error) {
          console.error(
            "[Composer] Image upload failed for",
            file.name,
            error
          );
          setPendingAttachments((prev) =>
            prev.map((att) =>
              att.id === id
                ? {
                    ...att,
                    status: "error",
                    errorMessage:
                      error instanceof Error
                        ? error.message
                        : String(error),
                  }
                : att
            )
          );
        }
      })();
    });

    // Upload non-image files to the chat workspace in parallel. On
    // success the attachment transitions to "ready" with a ``relPath``
    // that ``handleSend`` forwards as ``context_refs[].relPath``.
    filesToUpload.forEach(({ id, file }) => {
      void (async () => {
        try {
          const uploaded = await uploadFileToWorkspace(file, uploadChatId);
          setPendingAttachments((prev) =>
            prev.map((att) =>
              att.id === id
                ? {
                    ...att,
                    status: "ready",
                    relPath: uploaded.path,
                    fileName: uploaded.name || att.fileName,
                  }
                : att
            )
          );
        } catch (error) {
          console.error(
            "[Composer] File upload failed for",
            file.name,
            error
          );
          setPendingAttachments((prev) =>
            prev.map((att) =>
              att.id === id
                ? {
                    ...att,
                    status: "error",
                    errorMessage:
                      error instanceof Error
                        ? error.message
                        : String(error),
                  }
                : att
            )
          );
        }
      })();
    });
  }, [activeSessionId]);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      ingestFiles(Array.from(e.target.files));
    }
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const handleRemoveAttachment = (id: string) => {
    setPendingAttachments((prev) => {
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
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      ingestFiles(Array.from(e.dataTransfer.files));
    }
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  };

  /**
   * ImagePastePlugin callback. Delegates to the shared ingestion path so
   * a paste behaves exactly like a drag-drop or file-picker selection:
   * the image is uploaded immediately and the chip row reflects live
   * upload progress.
   */
  const handleImagePasted = useCallback(
    (file: File) => {
      ingestFiles([file]);
    },
    [ingestFiles]
  );

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
    saveDraft(draftStorageKey, failedDraft.text);
    setFailedDraft(null);
    // Attachments are intentionally NOT restored here: they were never
    // cleared on the failure path (see handleSend), so the current
    // pendingAttachments state already holds the correct set.
  };

  const handleSend = async () => {
    const rawText = editorRef.current ? editorRef.current.getText() : text;
    const trimmedText = rawText.trim();

    if (
      (!trimmedText && pendingAttachments.length === 0) ||
      isRunning ||
      disabled ||
      isSubmitting
    ) {
      return;
    }

    // Partition attachments by kind:
    //   * Non-images are ALREADY on the workspace (uploaded at attach
    //     time by ``ingestFiles``). They travel as ``context_refs``
    //     entries, so the dispatcher's ``inject_referenced_files`` path
    //     can read and inline their CONTENT into the prompt. No raw
    //     ``File`` objects are passed to the parent — that would trigger
    //     a redundant ``POST /api/files/upload`` in ChatContainer and
    //     append a useless ``[Uploaded Files]: name (in workspace
    //     directory)`` marker to the prompt.
    //   * Ready images become AttachmentRef objects traveling inside
    //     ``llmOverride.attachments``.
    //   * Still-uploading or errored entries (of either kind) are
    //     excluded: sending them would reach the backend with a null
    //     reference and the engine would treat the reference as broken.
    const readyFileRefs = pendingAttachments
      .filter(
        (att) => !att.isImage && att.status === "ready" && att.relPath
      )
      .map((att) => ({
        relPath: att.relPath as string,
        fileName: att.fileName,
      }));

    const readyImageRefs = pendingAttachments
      .filter(
        (att) => att.isImage && att.status === "ready" && att.fileId
      )
      .map((att) => ({
        fileId: att.fileId as string,
        mimeType: att.mimeType,
        fileName: att.fileName,
        width: att.width,
        height: att.height,
      }));

    const currentPayload = getActivePayload(trimmedText);

    // Read file mentions directly from the editor node tree. This is the
    // authoritative source: it preserves the canonical workspace-relative
    // path of each referenced file, avoiding the lossy basename extraction
    // that regex-on-text would produce for files in subdirectories.
    const editorMentions: Array<{ display: string; path: string }> =
      editorRef.current?.getFileMentions?.() ?? [];

    // Merge editor-mention refs with uploads-in-workspace refs into a
    // single deduplicated ``context_refs`` list. Editor mentions come
    // from inline `@file` tokens; uploaded-file refs come from the
    // non-image pipeline added in this revision. Both point to files
    // already on disk at a workspace-relative path, so the backend
    // treats them identically.
    const seenPaths = new Set<string>();
    const contextRefs: Array<{ relPath: string; fileName: string }> = [];
    for (const m of editorMentions) {
      const path = m.path.trim();
      if (!path || seenPaths.has(path)) continue;
      seenPaths.add(path);
      const fileName = path.split("/").pop() || path;
      contextRefs.push({ relPath: path, fileName });
    }
    for (const r of readyFileRefs) {
      const path = r.relPath.trim();
      if (!path || seenPaths.has(path)) continue;
      seenPaths.add(path);
      contextRefs.push({ relPath: path, fileName: r.fileName });
    }

    const dispatchText = trimmedText
      .replace(/@\\?\[(?:[^\\\]]|\\.)+?\\?\]\s*/g, "")
      .replace(/\s{2,}/g, " ")
      .trim();

    const finalPromptText = dispatchText || trimmedText;

    try {
      setIsSubmitting(true);

      // ``files`` (second argument) is intentionally ``undefined``:
      // non-image files were already uploaded to the workspace at
      // attach time, and re-uploading them here would (a) create a
      // duplicate copy and (b) append the deprecated
      // ``[Uploaded Files]: name (in workspace directory)`` marker
      // that gives the model no way to read the content in chat mode.
      // They travel as ``context_refs`` instead — the same channel
      // @-mentions use — so the dispatcher inlines their CONTENT.
      //
      // ``chat_id`` pins the target to the workspace where the
      // uploads actually landed. In the normal flow this matches
      // ``activeChatId`` anyway, but the explicit field protects
      // against any future divergence.
      const sendResult = onSend(
        finalPromptText,
        undefined,
        {
          ...currentPayload,
          // [FIX-2.3 + FIX-CHAT-REFS] Structured workspace file
          // references (editor @-mentions + uploaded non-images).
          context_refs: contextRefs.length > 0 ? contextRefs : undefined,
          // [FIX-CHAT-ID] Pin the chat id so /api/run uses the same
          // workspace where the file uploads landed.
          ...(activeSessionId ? { chat_id: activeSessionId } : {}),
          // Multimodal image references (from /api/files/upload-temp).
          // Consumed by ``start_run.py`` and forwarded to the engine as
          // ``llm_override["attachments"]``. Omitted entirely when no
          // image finished uploading successfully.
          ...(readyImageRefs.length > 0
            ? { attachments: readyImageRefs }
            : {}),
          // [SKILLS] Explicit skill invocations from the / menu.
          // Dispatcher forwards these into EngineRunContext.invoked_skills;
          // the engine merges them with the agent's assigned skills
          // before building the system prompt.
          ...(activeSkills.length > 0
            ? { invoked_skills: activeSkills }
            : {}),
        }
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

      // Success path: revoke every preview URL before clearing state so
      // the Blob references are released immediately rather than waiting
      // for GC to reclaim them.
      pendingAttachments.forEach((att) => {
        if (att.previewUrl) {
          try {
            URL.revokeObjectURL(att.previewUrl);
          } catch {
            /* ignore */
          }
        }
      });

      setWebSearchEnabled(false);
      editorRef.current?.clear();
      setText("");
      setPendingAttachments([]);
      setSelectedMentions([]);
      setActiveSkills([]);
      setFailedDraft(null);
      backupDraftRef.current = null;
      clearDraft(draftStorageKey);
    } catch (error) {
      console.error("[Composer] Message dispatch failed:", error);

      // Failure path: attachments stay intact so the user can retry
      // without re-uploading images that already reached the server.
      setFailedDraft({ text: trimmedText });

      if (editorRef.current) {
        editorRef.current.setText(trimmedText);
      }
      setText(trimmedText);
    } finally {
      setIsSubmitting(false);
    }
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const displayProvider = activeProvider || "your provider";
  const displayModel = activeModel || "your model";
  const dynamicPlaceholder =
    placeholder ||
    (execMode === "agent"
      ? `Ask ${displayProvider} (${displayModel}) to execute autonomous tasks...`
      : `Chat directly with ${displayProvider} (${displayModel})...`);

  const isInputEmpty =
    !text.trim() && pendingAttachments.length === 0;

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
        {pendingAttachments.length > 0 && (
          <div className="flex flex-col border-b border-border/40 bg-muted/20">
            <div className="flex flex-wrap gap-2 p-2.5 pb-1">
              {pendingAttachments.map((att) => (
                <div
                  key={att.id}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs bg-card border border-border shadow-xs text-foreground animate-in fade-in"
                >
                  {att.isImage && att.previewUrl ? (
                    <img
                      src={att.previewUrl}
                      alt={att.fileName}
                      className="w-5 h-5 rounded object-cover shrink-0 border border-border/40"
                    />
                  ) : att.isImage ? (
                    <ImageIcon size={13} className="text-primary shrink-0" />
                  ) : (
                    <FileText size={13} className="text-primary shrink-0" />
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
                  {/* The check icon now appears for both images and
                   *  non-images: non-images reach "ready" state after
                   *  their workspace upload completes (previously they
                   *  were created with status "ready" and shown without
                   *  a check). */}
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
                    onClick={() => handleRemoveAttachment(att.id)}
                    className="p-0.5 rounded hover:bg-muted text-muted-foreground hover:text-foreground cursor-pointer ml-1"
                    title="Remove attachment"
                  >
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>
            <div className="px-2.5 pb-1.5 text-[10px] text-muted-foreground/70 flex items-center gap-1">
              <Info size={10} className="shrink-0" />
              <span>
                Files upload to the workspace immediately so their content
                can be inlined into the model&apos;s prompt. Images upload
                to the temporary artifact store.
              </span>
            </div>
          </div>
        )}

        {activeSkills.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 px-3 pt-2 pb-1.5 border-b border-border/40 bg-primary/5">
            <span className="flex items-center gap-1 text-[10px] text-primary font-semibold uppercase tracking-wider mr-1">
              <Sparkles size={11} />
              Skills
            </span>
            {activeSkills.map((skillId) => {
              const skill = availableSkills.find((s) => s.id === skillId);
              const label = skill?.name || skillId;
              return (
                <span
                  key={skillId}
                  className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] bg-primary/10 border border-primary/30 text-primary"
                  title={skill?.description || skillId}
                >
                  <Sparkles size={10} className="shrink-0" />
                  <span className="font-mono font-semibold">{label}</span>
                  <button
                    type="button"
                    onClick={() =>
                      setActiveSkills((prev) =>
                        prev.filter((s) => s !== skillId)
                      )
                    }
                    className="ml-1 p-0.5 rounded hover:bg-primary/20 cursor-pointer"
                    aria-label={`Remove ${skillId}`}
                  >
                    <X size={10} />
                  </button>
                </span>
              );
            })}
          </div>
        )}

        <LexicalEditorComponent
          ref={editorRef}
          chatId={activeSessionId}
          placeholder={
            pendingAttachments.length > 0
              ? "Add instructions for attached files..."
              : dynamicPlaceholder
          }
          disabled={disabled || isRunning || isSubmitting}
          onSubmit={handleSend}
          onChange={handleEditorChange}
          onImagePasted={handleImagePasted}
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
              {pendingAttachments.length > 0 && (
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
                    ? "bg-card text-foreground shadow-xs font-semibold  bg-emerald-100"
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
                    ? "bg-card text-foreground shadow-xs font-semibold  bg-emerald-100"
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

            {execMode === "chat" && (
              <button
                type="button"
                onClick={() => setWebSearchEnabled((v) => !v)}
                disabled={disabled || isRunning || isSubmitting}
                className={`flex items-center text-xs font-medium transition-all cursor-pointer disabled:opacity-40 ${
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



            {execMode === "agent" && (
              <div className="animate-in fade-in duration-150 flex items-center gap-1.5">
                <AgentSelector disabled={disabled || isRunning || isSubmitting} />
                <ActiveToolsModal />

                {/* Delegation Toggle with Strict Role Governance */}
                {isTeamLead ? (
                  <button
                    type="button"
                    onClick={() => setDelegationAllowed(!delegationAllowed)}
                    disabled={disabled || isRunning || isSubmitting}
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
