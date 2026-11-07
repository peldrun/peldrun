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
import { showToast } from "@/components/ui/ToastNotification";
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

// ---------------------------------------------------------------------------
// Non-image file references (uploaded to workspace at attach time)
// ---------------------------------------------------------------------------
//
// Unlike images — which go to the temporary artifact store and travel as
// ``fileId`` references — non-image files (PDF, TXT, CSV, DOCX, ...) are
// uploaded DIRECTLY to the chat workspace via ``POST /api/files/upload``
// so that:
//
//   1. The backend can inline their CONTENT into the prompt (see
//      ``dispatcher.run_direct_chat`` and ``run_instrumented`` — both
//      call ``inject_referenced_files`` which reads from the workspace).
//   2. The agent's file tools (``str_replace_editor``, ``file_ops``, ...)
//      can find them at the workspace path without any additional
//      copy step.
//
// The upload happens IMMEDIATELY on attach so the user sees live
// progress, and so a single network failure never silently drops the
// file (as was the case with the previous "upload-at-dispatch" flow).
// Successful uploads carry a ``relPath`` (workspace-relative) which is
// exactly what ``context_refs[].relPath`` expects on the backend.
interface UploadedFileRef {
  id: string;
  fileName: string;
  fileSize: number;
  mimeType: string;
  status: "uploading" | "ready" | "error";
  relPath?: string;
  errorMessage?: string;
}

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

function generateUploadedFileRefId(): string {
  return `upref_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function generateLandingChatId(): string {
  // Mirrors the format used by ChatContainer.handleStartTask so the id
  // is indistinguishable from one generated downstream. Charset matches
  // (base36 timestamp + base36 random suffix) to avoid any collision or
  // normalization surprises when the backend validates it.
  return `chat_${Date.now().toString(36)}_${Math.random()
    .toString(36)
    .substring(2, 8)}`;
}

// ---------------------------------------------------------------------------
// Skill metadata model (shared with composer.tsx — kept intentionally
// minimal here to avoid a cross-file dependency just for a shape).
// ---------------------------------------------------------------------------

interface SkillMeta {
  id: string;
  name: string;
  description: string;
  short_description?: string;
  display_name?: string;
  tags?: string[];
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
  // NOTE: ``landingAttachedFiles`` remains part of ChatLandingProps
  // (for backward compatibility with the parent) but this component
  // no longer reads it. Non-image files are uploaded to the workspace
  // immediately and travel as ``context_refs`` instead — see
  // ``uploadedFileRefs`` below for the new state container.
  setLandingAttachedFiles,
  onStartTask,
  formatFileSize,
}: ChatLandingProps) {
  const [isLandingDragging, setIsLandingDragging] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);
  const [pendingImageAttachments, setPendingImageAttachments] = useState<
    PendingImageAttachment[]
  >([]);
  // Non-image attachments, uploaded to the workspace immediately. Each
  // entry represents one file that is uploading, ready, or errored.
  // Successful entries flow to the backend as ``context_refs`` — the
  // same channel @-mentions use — so their CONTENT is inlined into the
  // prompt by ``mention_resolver.inject_referenced_files``.
  const [uploadedFileRefs, setUploadedFileRefs] = useState<
    UploadedFileRef[]
  >([]);
  // A client-side chat identifier reserved for this landing session.
  // Generated once on mount so all non-image uploads from this composer
  // land in the same workspace the eventual /api/run call will use.
  // ChatLanding unmounts on the first successful dispatch, so the id
  // is scoped exactly to the "fresh session" window.
  const [landingChatId] = useState<string>(() => generateLandingChatId());

  // Web Search: temporary per-message toggle, chat mode only.
  const [webSearchEnabled, setWebSearchEnabled] = useState(false);

  // ── Skills ────────────────────────────────────────────────────
  // `activeSkills` holds skill IDs the user explicitly selected from
  // the / menu in the current draft; they travel to the backend as
  // `invoked_skills` and are merged with the agent's assigned skills
  // at prompt-injection time (see backend omweb/skills/assignments.py).
  //
  // `availableSkills` holds metadata for rendering the chip labels
  // (name, description). The runtime only needs the IDs.
  const [activeSkills, setActiveSkills] = useState<string[]>([]);
  const [availableSkills, setAvailableSkills] = useState<SkillMeta[]>([]);

  // ── Assigned skills (for duplicate detection) ─────────────────
  // When a user invokes a skill via `/` that is already assigned to
  // the currently-selected agent, we skip adding it to `activeSkills`
  // and show a toast explaining that it runs automatically.
  const [assignedSkillIds, setAssignedSkillIds] = useState<Set<string>>(
    () => new Set()
  );

  // Multi-Agent Delegation Toggle State
  const [delegationAllowed, setDelegationAllowed] = useAppStorage(
    "peldrun_delegation_allowed" as Parameters<typeof useAppStorage>[0]
  );

  // Selective subscription: only re-render when `selectedAgentId` changes.
  // Without the selector, any update to any other field of the chat store
  // (messages, activeSteps, currentJobId, availableAgents) would trigger a
  // re-render of this entire landing screen, which is the most expensive
  // component in the chat pipeline. See docs/state-contracts.md §6.4.
  const selectedAgentId = useChatStore((s) => s.selectedAgentId);
  const isTeamLead = (selectedAgentId || "peldrun").toLowerCase() === "peldrun";

  // Refs mirror the latest state into the long-lived
  // `peldrun:skill-invoked` listener registered once on mount.
  // Declared here (after `selectedAgentId`) so they capture the
  // correct values on first render.
  const assignedSkillIdsRef = useRef<Set<string>>(new Set());
  const execModeRef = useRef(execMode);
  const selectedAgentIdRef = useRef(selectedAgentId);
  const availableSkillsRef = useRef<SkillMeta[]>([]);

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

  // ── Skills: load catalogue and register each as a slash command ──
  //
  // Fetches /api/skills once on mount and re-fetches whenever
  // `peldrun:skills-changed` is dispatched (by the /store skills
  // section after install/delete/toggle). Each chat-invocable skill
  // registers as a SlashCommandItem whose `execute` dispatches
  // `peldrun:skill-invoked`; the next effect listens and adds the ID
  // to `activeSkills`.
  //
  // Mirrors the equivalent logic in composer.tsx so both entry points
  // behave identically.
  useEffect(() => {
    if (typeof window === "undefined") return;
    // Skip skill registration in chat mode. The typeahead menu is
    // disabled in chat mode (see typeaheadEnabled below), so slash
    // commands can never be triggered. Skipping also avoids a
    // pointless fetch.
    if (execMode !== "agent") {
      return;
    }

    let cancelled = false;
    let unregisterFns: Array<() => void> = [];

    const loadAndRegisterSkills = async () => {
      unregisterFns.forEach((fn) => fn());
      unregisterFns = [];

      try {
        const res = await fetch("/api/skills?include_disabled=false");
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const skills: SkillMeta[] = (Array.isArray(data.skills)
          ? data.skills
          : []
        ).filter(
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
          "[ChatLanding] Failed to load skills catalogue:",
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
  }, [execMode]);

  // ── Skills: listen for selections from the / menu ─────────────
  useEffect(() => {
    if (typeof window === "undefined") return;

    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ skillId?: string }>).detail;
      const skillId = detail?.skillId;
      if (typeof skillId !== "string" || !skillId) return;

      // Duplicate check: if the user invokes a skill via `/` that is
      // already assigned to the currently-selected agent, skip adding
      // it to `activeSkills` and inform them.
      if (
        execModeRef.current === "agent" &&
        assignedSkillIdsRef.current.has(skillId)
      ) {
        const skillMeta = availableSkillsRef.current.find(
          (s) => s.id === skillId
        );
        const skillName = skillMeta?.name || skillId;
        const agentDisplayName = selectedAgentIdRef.current
          ? `"${selectedAgentIdRef.current}"`
          : "the default agent";
        showToast.info(
          "Skill Already Assigned",
          `"${skillName}" is already assigned to ${agentDisplayName}. ` +
            `It runs automatically with every message — no need to invoke it via /.`
        );
        return;
      }

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

  // ── Fetch assigned skills for duplicate detection ─────────────
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (execMode !== "agent") {
      setAssignedSkillIds(new Set());
      return;
    }

    const agentForFetch = selectedAgentId || "peldrun";
    let cancelled = false;

    const fetchAssigned = async () => {
      try {
        const res = await fetch(
          `/api/store/agents/${encodeURIComponent(agentForFetch)}/skills`
        );
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const effective: string[] = Array.isArray(data.effective_skills)
          ? data.effective_skills
          : [];
        setAssignedSkillIds(new Set(effective));
      } catch (err) {
        console.debug(
          "[ChatLanding] Failed to fetch agent assigned skills:",
          err
        );
      }
    };

    void fetchAssigned();

    const onAgentSkillsChanged = () => {
      void fetchAssigned();
    };
    window.addEventListener(
      "peldrun:agent-skills-changed",
      onAgentSkillsChanged
    );

    return () => {
      cancelled = true;
      window.removeEventListener(
        "peldrun:agent-skills-changed",
        onAgentSkillsChanged
      );
    };
  }, [execMode, selectedAgentId]);

  // ── Prune activeSkills on mode / assignment changes ───────────
  useEffect(() => {
    if (execMode !== "agent") {
      // Chat mode does not support skill invocation.
      setActiveSkills((prev) => (prev.length === 0 ? prev : []));
      return;
    }
    if (assignedSkillIds.size === 0) return;
    setActiveSkills((prev) => {
      const filtered = prev.filter((id) => !assignedSkillIds.has(id));
      return filtered.length === prev.length ? prev : filtered;
    });
  }, [execMode, assignedSkillIds]);

  // Mirror the latest state into refs consumed by the long-lived
  // `peldrun:skill-invoked` listener.
  useEffect(() => {
    assignedSkillIdsRef.current = assignedSkillIds;
    execModeRef.current = execMode;
    selectedAgentIdRef.current = selectedAgentId;
    availableSkillsRef.current = availableSkills;
  });

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
   *   * Images  -> ``pendingImageAttachments`` list, uploaded to the
   *                temporary artifact store immediately.
   *   * Others  -> ``uploadedFileRefs`` list, uploaded to the CHAT
   *                WORKSPACE immediately so the backend can inline
   *                their content into the prompt.
   *
   * Both uploads run in parallel and update their own state entries on
   * completion, so the user sees live progress chips for every
   * attachment type.
   *
   * The parent's legacy ``landingAttachedFiles`` array is no longer
   * populated. Files travel as ``context_refs`` (workspace-relative
   * paths) in ``handleDispatch`` instead. This matches the flow that
   * ``run_direct_chat`` and ``run_instrumented`` already expect — see
   * ``dispatcher.inject_referenced_files`` — and eliminates the previous
   * ``[Uploaded Files]: name (in workspace directory)`` prompt hack
   * which only told the model that a file EXISTED without giving it
   * any way to read the content in chat mode.
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

      // ── Non-image pipeline: upload to workspace immediately ────
      if (nonImageFiles.length > 0) {
        const newRefs: UploadedFileRef[] = nonImageFiles.map((file) => ({
          id: generateUploadedFileRefId(),
          fileName: file.name || "attachment",
          fileSize: file.size,
          mimeType: file.type || "application/octet-stream",
          status: "uploading" as const,
        }));

        setUploadedFileRefs((prev) => [...prev, ...newRefs]);

        newRefs.forEach((ref, idx) => {
          const file = nonImageFiles[idx];
          const refId = ref.id;
          void (async () => {
            try {
              const uploaded = await uploadFileToWorkspace(
                file,
                landingChatId
              );
              setUploadedFileRefs((prev) =>
                prev.map((current) =>
                  current.id === refId
                    ? {
                        ...current,
                        status: "ready",
                        relPath: uploaded.path,
                        fileName: uploaded.name || current.fileName,
                      }
                    : current
                )
              );
            } catch (error) {
              console.error(
                "[ChatLanding] File upload failed for",
                ref.fileName,
                error
              );
              setUploadedFileRefs((prev) =>
                prev.map((current) =>
                  current.id === refId
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
      }

      // ── Image pipeline: unchanged (temp artifact store) ───────
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
    [landingChatId]
  );

  const handleLandingFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      ingestLandingFiles(Array.from(e.target.files));
    }
    if (landingFileInputRef.current) {
      landingFileInputRef.current.value = "";
    }
  };

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
    const textToRun = (customPrompt !== undefined
      ? customPrompt
      : rawText
    ).trim();

    const hasImages = pendingImageAttachments.length > 0;
    const hasFiles = uploadedFileRefs.length > 0;
    if (
      !textToRun &&
      !hasFiles &&
      !hasImages &&
      activeSkills.length === 0
    ) {
      return;
    }

    // Only fully-uploaded images travel to the backend. Still-uploading
    // or errored entries are excluded.
    const readyImageRefs = pendingImageAttachments
      .filter((att) => att.status === "ready" && att.fileId)
      .map((att) => ({
        fileId: att.fileId as string,
        mimeType: att.mimeType,
        fileName: att.fileName,
        width: att.width,
        height: att.height,
      }));

    // Only fully-uploaded non-image files are forwarded to the backend
    // as ``context_refs``. This is the SAME channel @-mentions use, so
    // the dispatcher's existing ``inject_referenced_files`` path picks
    // them up and inlines their CONTENT into the prompt (with PDF text
    // extraction and binary classification handled inside
    // ``mention_resolver.py``).
    //
    // Still-uploading or errored entries are excluded so the backend
    // never receives a reference to a file that is not yet on disk.
    const readyFileRefs = uploadedFileRefs
      .filter((ref) => ref.status === "ready" && ref.relPath)
      .map((ref) => ({
        relPath: ref.relPath as string,
        fileName: ref.fileName,
      }));

    const payloadOverride: any = {
      is_reasoning_model: isReasoningSupported,
      reasoning_effort: isReasoningSupported
        ? reasoningEffort || "none"
        : "none",
      // Attach web search flag (chat mode only)
      ...(execMode === "chat" && { web_search_enabled: webSearchEnabled }),
      // Attach delegation flag (agent mode only)
      ...(execMode === "agent" && { delegation_allowed: delegationAllowed }),
      // Multimodal image references. Consumed by start_run.py and
      // forwarded to the engine as llm_override["attachments"].
      ...(readyImageRefs.length > 0 ? { attachments: readyImageRefs } : {}),
      // [FIX-CHAT-REFS] Non-image file references. Forwarded to the
      // backend as llm_override["context_refs"], where the dispatcher
      // resolves each one against the chat workspace and injects its
      // content into the prompt. Without this, the file exists in the
      // workspace but the model has no way to read it in chat mode.
      ...(readyFileRefs.length > 0
        ? { context_refs: readyFileRefs }
        : {}),
      // [FIX-CHAT-ID] Pin the chat_id so /api/run uses the same workspace
      // the file uploads landed in. ChatContainer generates a fresh id
      // when this field is absent — which would orphan every upload the
      // user just did.
      chat_id: landingChatId,
      // [SKILLS] Explicit skill invocations from the / menu.
      // Forwarded to EngineRunContext.invoked_skills; the engine
      // merges them with the agent's assigned skills at prompt time.
      ...(activeSkills.length > 0 ? { invoked_skills: activeSkills } : {}),
    };

    // [FIX-CHAT-REFS] No 3rd ``files`` argument: non-image files were
    // already uploaded to the workspace on attach. The dispatcher picks
    // them up via ``context_refs`` — no re-upload is needed.
    onStartTask(textToRun, payloadOverride);

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
    setLandingAttachedFiles([]); // defensive: legacy state always empty
    setPendingImageAttachments([]);
    setUploadedFileRefs([]);
    setActiveSkills([]);
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
        {/* Active Skills Chips */}
        {activeSkills.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 px-2.5 pt-1 pb-2 border-b border-border/40 bg-primary/5 mb-2 rounded-t-xl">
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

        {/* Attached Files & Image Badges Container */}
        {(uploadedFileRefs.length > 0 ||
          pendingImageAttachments.length > 0) && (
          <div className="flex flex-col border-b border-border/40 bg-muted/20 mb-2 pb-1">
            <div className="flex flex-wrap gap-2 p-2.5 pb-1">
              {/* Non-image files: uploaded to workspace immediately. */}
              {uploadedFileRefs.map((ref) => (
                <div
                  key={ref.id}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs bg-muted/60 border border-border shadow-xs text-foreground animate-in fade-in"
                >
                  <FileText size={13} className="text-primary shrink-0" />
                  <span
                    className="font-medium truncate max-w-[160px]"
                    title={ref.fileName}
                  >
                    {ref.fileName}
                  </span>
                  <span className="text-[10px] text-muted-foreground font-mono">
                    ({formatFileSize(ref.fileSize)})
                  </span>
                  {ref.status === "uploading" && (
                    <Loader2
                      size={12}
                      className="text-primary animate-spin shrink-0"
                    />
                  )}
                  {ref.status === "ready" && (
                    <CheckCircle2
                      size={12}
                      className="text-emerald-500 shrink-0"
                    />
                  )}
                  {ref.status === "error" && (
                    <AlertCircle
                      size={12}
                      className="text-rose-500 shrink-0"
                      aria-label={ref.errorMessage || "Upload failed"}
                    />
                  )}
                  <button
                    type="button"
                    onClick={() =>
                      setUploadedFileRefs((prev) =>
                        prev.filter((r) => r.id !== ref.id)
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
                Files upload to the workspace immediately so their
                content can be inlined into the model's prompt. Images
                upload to the temporary artifact store.
              </span>
            </div>
          </div>
        )}

        {/* Lexical Omnibar Editor */}
        <div className="w-full">
          <LexicalEditorComponent
            ref={landingEditorRef}
            placeholder={
              uploadedFileRefs.length > 0 ||
              pendingImageAttachments.length > 0
                ? "Add instructions for attached files..."
                : activeSkills.length > 0
                  ? "Describe what you want the skill to do..."
                  : dynamicPlaceholder
            }
            onSubmit={() => handleDispatch()}
            onChange={(val) => setInputValue(val)}
            typeaheadEnabled={execMode === "agent"}
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
              {(uploadedFileRefs.length > 0 ||
                pendingImageAttachments.length > 0) && (
                <span className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-primary" />
              )}
            </button>

            {/* Active skills indicator badge */}
            {activeSkills.length > 0 && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono bg-primary/10 border border-primary/30 text-primary"
                title={`${activeSkills.length} skill(s) active`}
              >
                <Sparkles size={10} />
                {activeSkills.length}
              </span>
            )}

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
                uploadedFileRefs.length === 0 &&
                pendingImageAttachments.length === 0 &&
                activeSkills.length === 0
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
