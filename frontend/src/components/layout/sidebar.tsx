"use client";

import React, { useState, useEffect, useCallback, useRef, useMemo } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Folder,
  Plus,
  Pin,
  Download,
  Trash2,
  Archive,
  ChevronDown,
  Activity
} from "lucide-react";
import {
  fetchProjects,
  fetchChats,
  deleteChat,
  togglePinChat,
  toggleArchiveChat,
  categorizeChatTimeBucket,
  Project,
  ChatSession
} from "@/lib/chatsApi";
import { useConfirmModal } from "@/components/ui/ConfirmModal";
import { showToast, ToastContainer } from "@/components/ui/ToastNotification";
import { ServerStatusIndicator } from "./ServerStatusIndicator";

export function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const { confirm, ConfirmDialog } = useConfirmModal();

  const [projects, setProjects] = useState<Project[]>([]);
  const [chats, setChats] = useState<ChatSession[]>([]);
  const [isCreatingProject, setIsCreatingProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [pastWeekLimit, setPastWeekLimit] = useState(10);
  const [olderLimit, setOlderLimit] = useState(10);

  const lastFetchTimeRef = useRef<number>(0);
  const isFetchingRef = useRef<boolean>(false);

  const fetchData = useCallback(async (force = false) => {
    const now = Date.now();
    if (!force && (now - lastFetchTimeRef.current < 3000 || isFetchingRef.current)) {
      return;
    }

    lastFetchTimeRef.current = now;
    isFetchingRef.current = true;

    try {
      const [pData, cData] = await Promise.all([
        fetchProjects(),
        fetchChats({ includeArchived: false })
      ]);
      setProjects(pData || []);
      setChats(cData || []);
    } catch (e) {
      console.error("Failed to load sidebar data", e);
    } finally {
      isFetchingRef.current = false;
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData, pathname]);

  // Reactive listener bridge for live updates and status changes
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        fetchData();
      }
    };

    const handleChatsUpdated = () => {
      fetchData(true);
      // Secondary delayed fetch to synchronize asynchronous backend title summarization
      const t = setTimeout(() => {
        fetchData(true);
      }, 800);
      return () => clearTimeout(t);
    };

    const handleChatStatusChanged = (e: Event) => {
      const customEvent = e as CustomEvent<{
        chatId?: string;
        jobId?: string;
        status?: string;
        title?: string;
      }>;
      if (!customEvent.detail) return;
      const { chatId, jobId, status: newStatus, title: newTitle } = customEvent.detail;

      // Optimistic instant state mutation for zero-latency status indicator feedback
      setChats((prev) =>
        prev.map((c) => {
          const matches =
            (chatId && (c.id === chatId || c.job_id === chatId)) ||
            (jobId && (c.job_id === jobId || c.id === jobId));
          if (matches) {
            return {
              ...c,
              ...(newStatus ? { status: newStatus as any } : {}),
              ...(newTitle ? { title: newTitle } : {}),
            };
          }
          return c;
        })
      );

      // Trigger server synchronization
      fetchData(true);
    };

    window.addEventListener("visibilitychange", handleVisibility);
    window.addEventListener("omweb:refresh-sidebar", handleChatsUpdated);
    window.addEventListener("omweb:chats-updated", handleChatsUpdated);
    window.addEventListener("omweb:chat-status-changed", handleChatStatusChanged);

    return () => {
      window.removeEventListener("visibilitychange", handleVisibility);
      window.removeEventListener("omweb:refresh-sidebar", handleChatsUpdated);
      window.removeEventListener("omweb:chats-updated", handleChatsUpdated);
      window.removeEventListener("omweb:chat-status-changed", handleChatStatusChanged);
    };
  }, [fetchData]);

  // Smart polling fallback: only active while any task is actively running
  useEffect(() => {
    const hasRunningSession = chats.some((c) => c.status === "running");
    if (!hasRunningSession) return;

    const interval = setInterval(() => {
      fetchData(true);
    }, 3000);

    return () => clearInterval(interval);
  }, [chats, fetchData]);

  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProjectName.trim()) return;
    try {
      const res = await fetch("/api/chats/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newProjectName.trim() })
      });
      if (res.ok) {
        showToast.success("Workspace Created", `Project workspace '${newProjectName.trim()}' is ready.`);
        setNewProjectName("");
        setIsCreatingProject(false);
        fetchData(true);
      }
    } catch (err: any) {
      showToast.error("Creation Failed", err.message);
    }
  };

  const handleDeleteChat = async (e: React.MouseEvent, chatId: string, jobId: string) => {
    e.preventDefault();
    e.stopPropagation();

    const ok = await confirm({
      title: "Delete Chat Session",
      description: "Are you sure you want to permanently delete this chat? Workspace files and logs will be purged.",
      variant: "danger",
      confirmText: "Delete Session"
    });

    if (!ok) return;

    try {
      const success = await deleteChat(chatId);
      if (success) {
        setChats((prev) => prev.filter((c) => c.id !== chatId && c.job_id !== jobId));
        showToast.success("Session Deleted", `Session ${chatId} was permanently removed.`);
        window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        if (pathname.includes(jobId) || pathname.includes(chatId)) {
          router.push("/chat");
        }
      } else {
        showToast.error("Delete Failed", "Server could not delete session.");
      }
    } catch (err: any) {
      showToast.error("Delete Error", err.message);
    }
  };

  const handleTogglePin = async (e: React.MouseEvent, chatId: string) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      const updated = await togglePinChat(chatId);
      if (updated) {
        setChats((prev) =>
          prev.map((c) => (c.id === chatId ? { ...c, is_pinned: updated.is_pinned, pinned: updated.is_pinned } : c))
        );
        showToast.success("Pinned Status", updated.is_pinned ? "Session pinned to top." : "Session unpinned.");
        window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
      }
    } catch (err: any) {
      showToast.error("Pin Error", err.message);
    }
  };

  const handleToggleArchive = async (e: React.MouseEvent, chatId: string) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      const updated = await toggleArchiveChat(chatId);
      if (updated && updated.is_archived) {
        setChats((prev) => prev.filter((c) => c.id !== chatId));
        showToast.info("Archived", "Session moved to archive and hidden from sidebar.");
        window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
      }
    } catch (err: any) {
      showToast.error("Archive Error", err.message);
    }
  };

  const displayProjects = useMemo(() => {
    return projects.filter((p) => p.id !== "default_project");
  }, [projects]);

  const standaloneChats = useMemo(() => {
    return chats.filter((c) => {
      const pid = c.project_id;
      const isProjectChat = pid && pid !== "default_project" && pid !== "";
      if (isProjectChat) return false;
      if (c.is_archived) return false;
      return true;
    });
  }, [chats]);

  const pinnedChats = useMemo(() => {
    return standaloneChats.filter((c) => c.is_pinned || c.pinned);
  }, [standaloneChats]);

  const nonPinnedChats = useMemo(() => {
    return standaloneChats.filter((c) => !c.is_pinned && !c.pinned);
  }, [standaloneChats]);

  const pastWeekChats = useMemo(() => {
    return nonPinnedChats.filter((c) => categorizeChatTimeBucket(c.created_at) !== "older");
  }, [nonPinnedChats]);

  const visiblePastWeekChats = useMemo(() => {
    return pastWeekChats.slice(0, pastWeekLimit);
  }, [pastWeekChats, pastWeekLimit]);

  const olderChats = useMemo(() => {
    return nonPinnedChats.filter((c) => categorizeChatTimeBucket(c.created_at) === "older");
  }, [nonPinnedChats]);

  const visibleOlderChats = useMemo(() => {
    return olderChats.slice(0, olderLimit);
  }, [olderChats, olderLimit]);

  return (
    <>
      <aside className="w-[260px] border-r border-border bg-card backdrop-blur-md flex flex-col h-screen select-none shrink-0 font-sans transition-all duration-200">
        {/* Workspace Header */}
        <div className="h-14 flex items-center justify-between px-4 border-b border-border">
          <span className="font-heading font-semibold text-xs tracking-wider text-muted-foreground uppercase">
            Workspaces & Chats
          </span>
          <button
            type="button"
            onClick={() => setIsCreatingProject((v) => !v)}
            className="p-1 hover:text-foreground text-muted-foreground rounded-sm hover:bg-muted transition cursor-pointer"
            title="Create New Workspace"
          >
            <Plus size={14} />
          </button>
        </div>

        {/* Scrollable Workspaces & Sessions */}
        <div className="flex-1 p-3 overflow-y-auto space-y-4 scrollbar-thin">
          {/* Workspaces Section */}
          <div>
            <div className="flex items-center justify-between text-[11px] font-semibold text-muted-foreground px-2 uppercase tracking-wider mb-1.5">
              <span>Workspaces</span>
              {displayProjects.length > 0 && (
                <span className="text-[10px] font-mono opacity-70">{displayProjects.length}</span>
              )}
            </div>

            {isCreatingProject && (
              <form onSubmit={handleCreateProject} className="mb-2 px-1">
                <input
                  type="text"
                  autoFocus
                  placeholder="Workspace name..."
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  className="w-full px-2.5 py-1.5 rounded-xs bg-background text-xs border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-primary font-sans"
                />
              </form>
            )}

            <div className="space-y-0.5">
              {displayProjects.length === 0 ? (
                <div className="text-[11px] text-muted-foreground/70 px-2 py-1">
                  No workspaces yet.
                </div>
              ) : (
                displayProjects.map((proj) => {
                  const isActiveProj = pathname === `/projects/${proj.id}`;
                  const projChatsCount = chats.filter((c) => c.project_id === proj.id).length;
                  return (
                    <Link
                      key={proj.id}
                      href={`/projects/${proj.id}`}
                      className={`flex items-center justify-between px-2.5 py-1.5 rounded-xs text-xs transition-all ${
                        isActiveProj
                          ? "bg-muted text-foreground font-medium border border-border/80 shadow-xs"
                          : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                      }`}
                    >
                      <div className="flex items-center gap-2 truncate">
                        <Folder size={13} className="text-primary shrink-0" />
                        <span className="truncate">{proj.name}</span>
                      </div>
                      {projChatsCount > 0 && (
                        <span className="text-[10px] px-1.5 py-0.2 rounded-sm bg-background border border-border text-muted-foreground font-mono">
                          {projChatsCount}
                        </span>
                      )}
                    </Link>
                  );
                })
              )}
            </div>
          </div>

          {/* Pinned Section */}
          {pinnedChats.length > 0 && (
            <div>
              <div className="flex items-center gap-1.5 text-[10px] font-bold text-primary px-2 uppercase tracking-wider mb-1">
                <Pin size={10} className="rotate-45" />
                <span>Pinned Sessions</span>
              </div>
              <div className="space-y-0.5">
                {pinnedChats.map((chat) => (
                  <SidebarChatRow
                    key={chat.id}
                    chat={chat}
                    pathname={pathname}
                    onTogglePin={handleTogglePin}
                    onToggleArchive={handleToggleArchive}
                    onDeleteChat={handleDeleteChat}
                  />
                ))}
              </div>
            </div>
          )}

          {/* Recent Chats (Past 7 Days - Paginated by 10) */}
          <div>
            <div className="flex items-center justify-between text-[11px] font-semibold text-muted-foreground px-2 uppercase tracking-wider mb-1.5">
              <span>Past 7 Days</span>
              <span className="text-[10px] font-mono opacity-70">
                {visiblePastWeekChats.length}/{pastWeekChats.length}
              </span>
            </div>

            <div className="space-y-0.5">
              {pastWeekChats.length === 0 && pinnedChats.length === 0 ? (
                <div className="text-[11px] text-muted-foreground/70 px-2 py-2">
                  No sessions in the past 7 days.
                </div>
              ) : (
                visiblePastWeekChats.map((chat) => (
                  <SidebarChatRow
                    key={chat.id}
                    chat={chat}
                    pathname={pathname}
                    onTogglePin={handleTogglePin}
                    onToggleArchive={handleToggleArchive}
                    onDeleteChat={handleDeleteChat}
                  />
                ))
              )}
            </div>

            {visiblePastWeekChats.length < pastWeekChats.length && (
              <button
                type="button"
                onClick={() => setPastWeekLimit((prev) => prev + 10)}
                className="w-full mt-1.5 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground bg-muted/20 hover:bg-muted/50 border border-border/80 rounded flex items-center justify-center gap-1 transition cursor-pointer"
              >
                <ChevronDown size={11} />
                <span>Load More (+10)</span>
              </button>
            )}
          </div>

          {/* Older Sessions (Paginated by 10) */}
          {olderChats.length > 0 && (
            <div>
              <div className="flex items-center justify-between text-[11px] font-semibold text-muted-foreground px-2 uppercase tracking-wider mb-1.5">
                <span>Older Sessions</span>
                <span className="text-[10px] font-mono opacity-70">
                  {visibleOlderChats.length}/{olderChats.length}
                </span>
              </div>

              <div className="space-y-0.5">
                {visibleOlderChats.map((chat) => (
                  <SidebarChatRow
                    key={chat.id}
                    chat={chat}
                    pathname={pathname}
                    onTogglePin={handleTogglePin}
                    onToggleArchive={handleToggleArchive}
                    onDeleteChat={handleDeleteChat}
                  />
                ))}
              </div>

              {visibleOlderChats.length < olderChats.length && (
                <button
                  type="button"
                  onClick={() => setOlderLimit((prev) => prev + 10)}
                  className="w-full mt-2 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground bg-muted/20 hover:bg-muted/50 border border-border/80 rounded flex items-center justify-center gap-1 transition cursor-pointer"
                >
                  <ChevronDown size={11} />
                  <span>Load More ({olderChats.length - visibleOlderChats.length} remaining)</span>
                </button>
              )}
            </div>
          )}
        </div>

        {/* Footer System Status */}
        <Link
          href="/status"
          title="View System Health & Diagnostics"
          className="p-2.5 border-t border-border flex items-center justify-between text-[11px] text-muted-foreground font-mono hover:bg-muted/70 hover:text-foreground transition-all group cursor-pointer"
        >
          <span className="flex items-center gap-1.5">
            <Activity size={13} className="text-peldrun-accent group-hover:scale-110 transition-transform" />
            <span className="group-hover:text-foreground transition-colors">System Health</span>
          </span>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted border border-border text-muted-foreground group-hover:text-foreground">
            v1.0.0
          </span>
        </Link>

        {/* Dynamic Server Status Indicator */}
        <div className="flex items-center gap-2 shrink-0 p-2.5 border-t border-border flex items-center justify-between text-[11px] text-muted-foreground font-mono hover:bg-muted/70 hover:text-foreground transition-all group cursor-pointer">
          <ServerStatusIndicator showLatency={true} />
        </div>
      </aside>

      {/* Standalone Injected Dialog and Toasts */}
      <ConfirmDialog />
      <ToastContainer />
    </>
  );
}

interface SidebarChatRowProps {
  chat: ChatSession;
  pathname: string;
  onTogglePin: (e: React.MouseEvent, chatId: string) => void;
  onToggleArchive: (e: React.MouseEvent, chatId: string) => void;
  onDeleteChat: (e: React.MouseEvent, chatId: string, jobId: string) => void;
}

function SidebarChatRow({
  chat,
  pathname,
  onTogglePin,
  onToggleArchive,
  onDeleteChat
}: SidebarChatRowProps) {
  const effectiveTarget = chat.job_id || chat.id;
  const isActive = pathname === `/chat/${effectiveTarget}` || pathname === `/chat/${chat.id}`;
  const isPinned = chat.is_pinned || chat.pinned;

  return (
    <div
      className={`group flex items-center justify-between px-2.5 py-1.5 rounded-md text-xs transition-all ${
        isActive
          ? "bg-muted text-foreground font-medium border border-border shadow-xs"
          : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
      }`}
    >
      <Link
        href={`/chat/${effectiveTarget}`}
        className="flex items-center gap-2 truncate flex-1 mr-1"
      >
        <span
          className={`w-2 h-2 rounded-full shrink-0 transition-colors ${
            chat.status === "completed"
              ? "bg-emerald-500"
              : chat.status === "running"
              ? "bg-amber-500 animate-pulse"
              : chat.status === "failed"
              ? "bg-rose-500"
              : "bg-muted-foreground/40"
          }`}
        />
        <span className="truncate">{chat.title || chat.prompt || "New Session"}</span>
      </Link>

      <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
        <button
          type="button"
          onClick={(e) => onTogglePin(e, chat.id)}
          title={isPinned ? "Unpin session" : "Pin session"}
          className={`p-1 hover:text-foreground cursor-pointer rounded-sm ${
            isPinned ? "text-primary font-bold" : "text-muted-foreground"
          }`}
        >
          <Pin size={11} className={isPinned ? "rotate-45" : ""} />
        </button>

        <button
          type="button"
          onClick={(e) => onToggleArchive(e, chat.id)}
          title="Archive (hide from sidebar)"
          className="p-1 text-muted-foreground hover:text-foreground cursor-pointer rounded-sm"
        >
          <Archive size={11} />
        </button>

        <button
          type="button"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            window.open(`/api/chats/${chat.id}/log/download`, "_blank");
          }}
          title="Download Log"
          className="p-1 text-muted-foreground hover:text-foreground cursor-pointer rounded-sm"
        >
          <Download size={11} />
        </button>

        <button
          type="button"
          onClick={(e) => onDeleteChat(e, chat.id, chat.job_id)}
          title="Delete chat"
          className="p-1 text-muted-foreground hover:text-destructive cursor-pointer rounded-sm"
        >
          <Trash2 size={11} />
        </button>
      </div>
    </div>
  );
}

export default Sidebar;