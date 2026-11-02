// Path: frontend/src/app/projects/[projectId]/page.tsx
"use client";

/**
 * Project Workspace page.
 *
 * Reads project metadata from `project-store` and sessions from
 * `session-store.projectSessionsCache`. No local fetch on mount —
 * the bootstrap provider loads projects, and
 * `loadProjectSessions(projectId)` fetches this project's sessions
 * exactly once (cached on subsequent visits).
 *
 * ## Design decisions
 *
 *   * **No N+1 file fetch.** The old version issued one
 *     `/api/run/jobs/{id}/files` call per session to build the
 *     "Deliverables" count — 31 requests for a 30-session project.
 *     Files are now loaded only when the user opens a session, by
 *     `ChatContainer` → `WorkspacePanel`. The project page shows
 *     session metadata only.
 *
 *   * **409 handling.** Both delete endpoints (`/api/chats/{id}` and
 *     `/api/chats/projects/{id}`) return 409 when an active run
 *     exists in the target. The handlers surface a clear toast
 *     instead of silently failing.
 *
 *   * **No custom events.** The old version relied on
 *     `omweb:chats-updated` to nudge the sidebar. After Phase F.4
 *     that event is no longer listened to. Store mutations replace
 *     it entirely.
 *
 *   * **Cache invalidation on create.** After `POST /api/run` creates
 *     a chat under this project, the project's session cache is
 *     invalidated so the next visit reflects the new chat.
 */

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  Folder,
  MessageSquare,
  Send,
  Trash2,
  Calendar,
  Download,
  ArrowLeft,
  Clock,
  CheckCircle2,
  AlertCircle,
  FileCode,
  Sparkles,
  ExternalLink,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useProjectStore } from "@/stores/project-store";
import { useSessionStore } from "@/stores/session-store";
import { showToast } from "@/components/ui/ToastNotification";

export default function ProjectWorkspacePage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params?.projectId as string;

  // Store subscriptions.
  const project = useProjectStore((s) =>
    projectId ? s.projectsById[projectId] : undefined,
  );
  const projectSessions = useSessionStore((s) =>
    projectId ? s.projectSessionsCache[projectId] : undefined,
  );
  const isLoading = useSessionStore(
    (s) => projectId ? s.projectSessionsLoading[projectId] === true : false,
  );
  const loadProjectSessions = useSessionStore((s) => s.loadProjectSessions);

  // Local UI state.
  const [promptInput, setPromptInput] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  const chats = projectSessions ?? [];

  // Load project sessions on mount / project switch. The store method
  // is idempotent — cached data is returned without a network call on
  // re-visits.
  useEffect(() => {
    if (projectId) {
      void loadProjectSessions(projectId);
    }
  }, [projectId, loadProjectSessions]);

  const handleStartProjectChat = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = promptInput.trim();
    if (!text || isSubmitting) return;
    setIsSubmitting(true);
    try {
      const res = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: text,
          project_id: projectId,
          max_steps: 30,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setPromptInput("");

      // Invalidate this project's cached session list so the next
      // visit shows the newly created chat.
      useSessionStore.getState().invalidateProjectSessions(projectId);

      if (data.job_id) {
        router.push(`/chat/${data.job_id}`);
      } else if (data.chat_id) {
        router.push(`/chat/${data.chat_id}`);
      }
    } catch (err: any) {
      showToast.error(
        "Dispatch Failed",
        err?.message || "Could not create project task.",
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleDeleteChat = async (
    e: React.MouseEvent,
    chatId: string,
    jobId: string,
  ) => {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm("Delete this session from this project?")) return;

    try {
      const res = await fetch(
        `/api/chats/${encodeURIComponent(chatId)}`,
        { method: "DELETE" },
      );

      if (!res.ok) {
        if (res.status === 409) {
          const body = await res.json().catch(() => ({}));
          showToast.error(
            "Cannot Delete",
            body.detail || "A run is still active in this session.",
          );
          return;
        }
        throw new Error(`HTTP ${res.status}`);
      }

      // removeSession clears both the sidebar entry and any project
      // cache entry for this chat.
      useSessionStore.getState().removeSession(chatId);
      showToast.success("Session Deleted", `Session removed from project.`);
    } catch (err: any) {
      showToast.error(
        "Delete Failed",
        err?.message || "Could not delete session.",
      );
    }
  };

  const handleDeleteProject = async () => {
    if (!confirm("Are you sure you want to delete this entire project workspace?"))
      return;

    try {
      const res = await fetch(
        `/api/chats/projects/${encodeURIComponent(projectId)}`,
        { method: "DELETE" },
      );

      if (!res.ok) {
        if (res.status === 409) {
          const body = await res.json().catch(() => ({}));
          showToast.error(
            "Cannot Delete",
            body.detail || "A run is still active in this project.",
          );
          return;
        }
        throw new Error(`HTTP ${res.status}`);
      }

      // Remove all sessions in this project from the store first,
      // then the project itself.
      const sessionStore = useSessionStore.getState();
      for (const chat of chats) {
        sessionStore.removeSession(chat.id);
      }
      useProjectStore.getState().removeProject(projectId);

      showToast.success("Project Deleted", "Workspace removed.");
      router.push("/projects");
    } catch (err: any) {
      showToast.error(
        "Delete Failed",
        err?.message || "Could not delete project.",
      );
    }
  };

  return (
    <div className="flex flex-col flex-1 h-full w-full bg-background text-foreground overflow-hidden font-sans">
      <div className="h-14 flex items-center justify-between px-8 border-b border-border bg-card/40 backdrop-blur-sm shrink-0">
        <div className="flex items-center gap-3">
          <Link
            href="/projects"
            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
            title="Back to all projects"
          >
            <ArrowLeft size={16} />
          </Link>
          <Folder className="h-4 w-4 text-peldrun-accent" />
          <div>
            <div className="flex items-center gap-2">
              <h1 className="font-heading font-semibold text-sm text-foreground">
                {project?.name || "Project Workspace"}
              </h1>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded-md bg-primary/15 text-primary border border-primary/30">
                {projectId}
              </span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            onClick={handleDeleteProject}
            className="flex items-center gap-1.5 h-8 px-3 rounded-md border-peldrun-error/30 text-peldrun-error hover:bg-peldrun-error/10 text-xs transition cursor-pointer shadow-peldrun-xs"
          >
            <Trash2 size={13} />
            <span>Delete Workspace</span>
          </Button>
        </div>
      </div>

      <div className="flex-1 p-8 overflow-y-auto space-y-6 bg-background">
        <div className="p-5 rounded-2xl border border-border bg-card space-y-3 shadow-peldrun-xs">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold text-peldrun-success uppercase tracking-wider flex items-center gap-1.5">
              <Sparkles size={12} />
              Project Objective &amp; Context
            </span>
            <div className="flex items-center gap-3 text-xs font-mono">
              <span className="px-2.5 py-1 rounded-md bg-background border border-border text-foreground flex items-center gap-1.5 shadow-peldrun-xs">
                <MessageSquare size={12} className="text-peldrun-success" />
                <span>{chats.length} Sessions</span>
              </span>
            </div>
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed max-w-3xl font-sans">
            {project?.description ||
              "All chats, files, and deliverables generated in this workspace are strictly scoped and stored under this project."}
          </p>
          <div className="flex items-center gap-4 pt-1 text-[11px] text-muted-foreground font-mono">
            <span className="flex items-center gap-1">
              <Calendar size={11} /> Created: {project?.created_at || "Recent"}
            </span>
          </div>
        </div>

        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-2">
              <MessageSquare size={13} className="text-peldrun-success" />
              <span>Project Sessions ({chats.length})</span>
            </h2>
          </div>

          {isLoading && chats.length === 0 ? (
            <div className="flex items-center justify-center h-48 text-muted-foreground text-xs animate-pulse font-mono">
              Loading project workspace sessions...
            </div>
          ) : chats.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-52 text-center text-muted-foreground space-y-2 border border-dashed border-border rounded-2xl bg-card/40">
              <FileCode size={34} className="text-muted-foreground" />
              <p className="text-xs font-medium text-foreground">
                No chat sessions recorded inside this project yet.
              </p>
              <p className="text-[11px] text-muted-foreground">
                Use the input composer below to dispatch your first task
                under this workspace.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {chats.map((chat) => {
                const targetJob = chat.job_id || chat.id;
                return (
                  <div
                    key={chat.id}
                    className="flex flex-col justify-between p-4 rounded-sm border border-border bg-card hover:border-primary/40 transition-all group shadow-peldrun-xs"
                  >
                    <div>
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-[10px] text-muted-foreground font-mono font-semibold">
                          {targetJob}
                        </span>
                        <span
                          className={`px-2 py-0.5 rounded-md text-[10px] font-mono font-medium flex items-center gap-1 ${
                            chat.status === "completed"
                              ? "bg-peldrun-success/15 text-peldrun-success border border-peldrun-success/30"
                              : chat.status === "running"
                              ? "bg-peldrun-warning/15 text-peldrun-warning border border-peldrun-warning/30 animate-pulse"
                              : chat.status === "cancelled"
                              ? "bg-slate-500/15 text-slate-500 border border-slate-500/30"
                              : chat.status === "interrupted"
                              ? "bg-orange-500/15 text-orange-500 border border-orange-500/30"
                              : "bg-peldrun-error/15 text-peldrun-error border border-peldrun-error/30"
                          }`}
                        >
                          {chat.status === "completed" && <CheckCircle2 size={10} />}
                          {chat.status === "running" && <Clock size={10} />}
                          {chat.status === "failed" && <AlertCircle size={10} />}
                          <span>{chat.status}</span>
                        </span>
                      </div>

                      <h3 className="text-xs font-semibold text-foreground line-clamp-2 mb-2 leading-relaxed font-sans group-hover:text-primary transition">
                        {chat.title || chat.prompt || "New Session"}
                      </h3>
                    </div>

                    <div className="flex items-center justify-between pt-3 border-t border-border/60 mt-auto text-xs">
                      <Link
                        href={`/chat/${targetJob}`}
                        className="text-primary hover:underline font-medium flex items-center gap-1"
                      >
                        <span>Open Session</span>
                        <ExternalLink size={11} />
                      </Link>

                      <div className="flex items-center gap-1.5">
                        <a
                          href={`/api/run/jobs/${targetJob}/download-zip`}
                          title="Download ZIP"
                          className="p-1.5 rounded-md bg-muted hover:bg-muted/80 text-foreground border border-border text-[11px] transition shadow-peldrun-xs"
                        >
                          <Download size={12} />
                        </a>
                        <button
                          type="button"
                          onClick={(e) => handleDeleteChat(e, chat.id, chat.job_id)}
                          title="Delete session"
                          className="p-1.5 rounded-md bg-white-foreground hover:bg-peldrun-error/20 text-muted-foreground hover:text-peldrun-error border border-border transition cursor-pointer"
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <div className="p-4 border-t border-border bg-card/60 backdrop-blur-sm shrink-0">
        <form
          onSubmit={handleStartProjectChat}
          className="flex items-center gap-2 p-1.5 pl-3.5 rounded-sm border border-border bg-background shadow-peldrun-sm focus-within:ring-1 focus-within:ring-primary focus-within:border-primary/50 transition-all"
        >
          <input
            type="text"
            value={promptInput}
            onChange={(e) => setPromptInput(e.target.value)}
            placeholder={`Assign a new autonomous task to ${project?.name || "this project"}...`}
            disabled={isSubmitting}
            className="flex-1 bg-transparent border-0 outline-none text-xs text-foreground placeholder:text-muted-foreground disabled:opacity-50 font-sans"
          />
          <Button
            type="submit"
            size="sm"
            disabled={isSubmitting || !promptInput.trim()}
            className="h-7 w-7 p-0 rounded-md shrink-0 cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed shadow-peldrun-xs"
          >
            <Send size={12} />
          </Button>
        </form>
      </div>
    </div>
  );
}
