// Path: frontend/src/components/layout/sidebar.tsx
"use client";

/**
 * Sidebar — display-only view over the centralized stores.
 *
 * ## Prior behavior (pre-Phase F)
 *
 *   * Local `useState` for projects and chats.
 *   * `fetchData()` with `Promise.all([fetchProjects(), fetchChats()])`
 *     called on mount, on pathname change, on `visibilitychange`, on
 *     three custom events (`omweb:refresh-sidebar`, `omweb:chats-updated`,
 *     `omweb:chat-status-changed`), and on a 3-second `setInterval`
 *     whenever any session was `running`.
 *
 * ## Current behavior (post-Phase F)
 *
 *   * `useProjectStore` supplies the projects.
 *   * `useSessionStore` supplies the sessions.
 *   * SSE lifecycle events update the stores automatically through
 *     `runStreamManager` → `sessionStore.applyRunLifecycleEvent`.
 *   * No polling, no fetch, no custom event listeners.
 *
 * ## Pagination model
 *
 * The session store holds one page (50 sessions) at a time. Each
 * bucket (pinned / past-week / older) slices that loaded set further
 * into user-facing 10-item increments. When a bucket's slice is
 * exhausted and the backend reports `hasMore`, the "Load More" button
 * triggers `loadMoreSidebarSessions()` to append the next 50-item page.
 *
 * This preserves the exact UX users see today (10 at a time) without
 * ever asking the backend for the full history.
 *
 * ## Cross-tab synchronization
 *
 * The `online` listener forces a session/project reload. This covers
 * the "user came back online after working in another tab" case. Per-
 * tab SSE connections handle in-tab updates; nothing else is needed
 * because Zustand state is per-tab by design, and stale data is better
 * than no data until the next explicit refresh.
 *
 * ## Error display
 *
 * A failed refresh now sets `error` on the store (see chatsApi.ts
 * ApiError). The Sidebar surfaces it as a small banner above the list
 * so the user knows they are looking at cached data.
 */

import React, {
  useState,
  useEffect,
  useCallback,
  useMemo,
} from "react";
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
  Activity,
  AlertCircle,
  Loader2,
} from "lucide-react";

import {
  fetchProjects,
  fetchChats,
  deleteChat,
  togglePinChat,
  toggleArchiveChat,
  categorizeChatTimeBucket,
  Project,
  ChatSession,
} from "@/lib/chatsApi";
import { useSessionStore } from "@/stores/session-store";
import { useProjectStore } from "@/stores/project-store";
import { useConfirmModal } from "@/components/ui/ConfirmModal";
import { showToast, ToastContainer } from "@/components/ui/ToastNotification";


// ---------------------------------------------------------------------------
// Reserved project ids excluded from the sidebar's "Workspaces" section.
// Mirrors DEFAULT_PROJECT_ID in project-store and the backend's
// `project_manager.DEFAULT_PROJECT_ID`.
// ---------------------------------------------------------------------------

const DEFAULT_PROJECT_ID = "default_project";


// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const { confirm, ConfirmDialog } = useConfirmModal();

  // ─── Route mode detection ─────────────────────────────────────────────
  //
  // The sidebar renders three distinct modes:
  //
  //   * "chat"            → /chat and other app pages (not /projects)
  //                          Shows: Workspaces + Pinned + Past 7d + Older
  //
  //   * "projects-list"   → /projects (list page)
  //                          Shows: Workspaces ONLY (no chat sessions)
  //
  //   * "project-detail"  → /projects/{projectId}
  //                          Shows: Workspaces + Sessions of {projectId}
  //
  // See docs/state-contracts.md §6.1 for the sidebar mode rules.
  const projectIdFromPath = useMemo<string | null>(() => {
    const match = pathname.match(/^\/projects\/([^/?#]+)/);
    return match?.[1] ?? null;
  }, [pathname]);

  const sidebarMode: "chat" | "projects-list" | "project-detail" =
    pathname === "/projects"
      ? "projects-list"
      : projectIdFromPath
      ? "project-detail"
      : "chat";

  // -------------------------------------------------------------------------
  // Store subscriptions (one selector each — see docs/state-contracts.md §6.4)
  //
  //   * orderedProjects / orderedSessions are derived by the store from
  //     the raw records so we subscribe to the two independent slices
  //     and let React.memo / shallow-compare skip unnecessary re-renders.
  //
  //   * isLoading / hasMore drive the pagination UI; both are boolean /
  //     integer scalars so Zustand's Object.is comparison catches changes
  //     precisely.
  //
  //   * error strings surface a soft warning banner when a refresh
  //     failed but the cached data is still shown.
  //
  // We intentionally pull `sessionsById` and `sidebarOrder` separately
  // (rather than calling `getOrderedSessions()` directly in the selector)
  // so that React can reuse the same `ChatSession` object references for
  // unchanged sessions and only re-render the rows whose status actually
  // changed.
  // -------------------------------------------------------------------------
  const projectsById = useProjectStore((s) => s.projectsById);
  const projectOrder = useProjectStore((s) => s.projectOrder);
  const projectError = useProjectStore((s) => s.error);

  const sessionsById = useSessionStore((s) => s.sessionsById);
  const sidebarOrder = useSessionStore((s) => s.sidebarOrder);
  const sessionError = useSessionStore((s) => s.error);
  const sessionHasMore = useSessionStore((s) => s.hasMore);
  const sessionIsLoading = useSessionStore((s) => s.isLoading);
  const loadMoreSessions = useSessionStore((s) => s.loadMoreSidebarSessions);

  // -------------------------------------------------------------------------
  // Local UI state (does not belong in a store)
  // -------------------------------------------------------------------------
  const [isCreatingProject, setIsCreatingProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [pastWeekLimit, setPastWeekLimit] = useState(10);
  const [olderLimit, setOlderLimit] = useState(10);

  // -------------------------------------------------------------------------
  // Effects
  //
  //   * Reconcile on online — the only network-triggering effect. Uses
  //     `force=true` so the reload actually hits the network (the store
  //     otherwise short-circuits after the initial load).
  // -------------------------------------------------------------------------
  useEffect(() => {
    const handleOnline = () => {
      void useSessionStore.getState().loadSidebarSessions(true);
      void useProjectStore.getState().loadProjects(true);
    };
    window.addEventListener("online", handleOnline);
    return () => window.removeEventListener("online", handleOnline);
  }, []);

  // -------------------------------------------------------------------------
  // Derived lists
  //
  // The four buckets (projects, pinned, past-week, older) are recomputed
  // whenever the underlying store slices change. Each memo has the
  // narrowest possible dependency set so React can skip the work when
  // only an unrelated bucket changed.
  // -------------------------------------------------------------------------
  const orderedProjects = useMemo(() => {
    const out: Project[] = [];
    for (const id of projectOrder) {
      const p = projectsById[id];
      if (p) out.push(p);
    }
    return out;
  }, [projectOrder, projectsById]);

  const orderedSessions = useMemo(() => {
    const out: ChatSession[] = [];
    for (const id of sidebarOrder) {
      const s = sessionsById[id];
      if (s) out.push(s);
    }
    return out;
  }, [sidebarOrder, sessionsById]);

  const displayProjects = useMemo(
    () => orderedProjects.filter((p) => p.id !== DEFAULT_PROJECT_ID),
    [orderedProjects]
  );

  const standaloneChats = useMemo(
    () =>
      orderedSessions.filter((c) => {
        const pid = c.project_id;
        const isProjectChat = pid && pid !== DEFAULT_PROJECT_ID && pid !== "";
        if (isProjectChat) return false;
        if (c.is_archived) return false;
        return true;
      }),
    [orderedSessions]
  );

  const pinnedChats = useMemo(
    () => standaloneChats.filter((c) => c.is_pinned || c.pinned),
    [standaloneChats]
  );

  const nonPinnedChats = useMemo(
    () => standaloneChats.filter((c) => !c.is_pinned && !c.pinned),
    [standaloneChats]
  );

  const pastWeekChats = useMemo(
    () =>
      nonPinnedChats.filter(
        (c) => categorizeChatTimeBucket(c.created_at) !== "older"
      ),
    [nonPinnedChats]
  );

  const visiblePastWeekChats = useMemo(
    () => pastWeekChats.slice(0, pastWeekLimit),
    [pastWeekChats, pastWeekLimit]
  );

  const olderChats = useMemo(
    () =>
      nonPinnedChats.filter(
        (c) => categorizeChatTimeBucket(c.created_at) === "older"
      ),
    [nonPinnedChats]
  );

  const visibleOlderChats = useMemo(
    () => olderChats.slice(0, olderLimit),
    [olderChats, olderLimit]
  );

  // -------------------------------------------------------------------------
  // CRUD handlers
  //
  // Pattern (universal across all handlers below):
  //   1. Call the backend.
  //   2. On success, mutate the centralized store.
  //   3. Optionally show a toast.
  //   4. On failure, show a toast. Do NOT mutate the store.
  //
  // This is the "optimistic after confirm" policy from
  // docs/state-contracts.md §13.5: the UI is updated only after the
  // server confirms, so a failed delete cannot make a session vanish
  // from the sidebar while still existing on disk.
  // -------------------------------------------------------------------------

  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    const name = newProjectName.trim();
    if (!name || isCreating) return;

    setIsCreating(true);
    try {
      const res = await fetch("/api/chats/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (data.project) {
        useProjectStore.getState().upsertProject(data.project);
      }
      showToast.success("Workspace Created", `Project workspace '${name}' is ready.`);
      setNewProjectName("");
      setIsCreatingProject(false);
    } catch (err: any) {
      showToast.error("Creation Failed", err?.message || "Could not create project.");
    } finally {
      setIsCreating(false);
    }
  };

  const handleDeleteChat = useCallback(
    async (e: React.MouseEvent, chatId: string, jobId: string) => {
      e.preventDefault();
      e.stopPropagation();

      const ok = await confirm({
        title: "Delete Chat Session",
        description:
          "Are you sure you want to permanently delete this chat? Workspace files and logs will be purged.",
        variant: "danger",
        confirmText: "Delete Session",
      });
      if (!ok) return;

      try {
        const res = await fetch(`/api/chats/${encodeURIComponent(chatId)}`, {
          method: "DELETE",
        });

        if (!res.ok) {
          if (res.status === 409) {
            const body = await res.json().catch(() => ({}));
            showToast.error(
              "Cannot Delete",
              body.detail || "A run is still active in this session."
            );
            return;
          }
          throw new Error(`HTTP ${res.status}`);
        }

        useSessionStore.getState().removeSession(chatId);
        showToast.success(
          "Session Deleted",
          `Session ${chatId} was permanently removed.`
        );

        // If the deleted session is currently open, navigate away.
        if (pathname.includes(jobId) || pathname.includes(chatId)) {
          router.push("/chat");
        }
      } catch (err: any) {
        showToast.error("Delete Error", err?.message || "Could not delete session.");
      }
    },
    [confirm, pathname, router]
  );

  const handleTogglePin = useCallback(
    async (e: React.MouseEvent, chatId: string) => {
      e.preventDefault();
      e.stopPropagation();

      try {
        const updated = await togglePinChat(chatId);
        if (!updated) {
          showToast.error("Pin Failed", "Server could not update the session.");
          return;
        }
        useSessionStore.getState().patchSession(chatId, {
          is_pinned: updated.is_pinned,
          pinned: updated.is_pinned,
        });
        showToast.success(
          "Pinned Status",
          updated.is_pinned ? "Session pinned to top." : "Session unpinned."
        );
      } catch (err: any) {
        showToast.error("Pin Error", err?.message || "Could not toggle pin.");
      }
    },
    []
  );

  const handleToggleArchive = useCallback(
    async (e: React.MouseEvent, chatId: string) => {
      e.preventDefault();
      e.stopPropagation();

      try {
        const updated = await toggleArchiveChat(chatId);
        if (updated && updated.is_archived) {
          useSessionStore.getState().removeSession(chatId);
          showToast.info(
            "Archived",
            "Session moved to archive and hidden from sidebar."
          );
        }
      } catch (err: any) {
        showToast.error("Archive Error", err?.message || "Could not archive.");
      }
    },
    []
  );

  // -------------------------------------------------------------------------
  // Pagination handlers
  //
  // Each bucket exposes its own "Load More" button. The button does two
  // things on click:
  //
  //   1. Bump the local slice limit (+10) so the user sees the next
  //      batch immediately.
  //
  //   2. If the local slice is already at its ceiling AND the backend
  //      reports more pages, fetch the next 50-session page. The new
  //      sessions are distributed across buckets by their `created_at`
  //      automatically (the filters above recompute).
  //
  // The double check `bucket.length <= newLimit` ensures we only trigger
  // the network call when the local buffer is exhausted — otherwise the
  // click would fire a redundant fetch that just returns rows we already
  // have.
  // -------------------------------------------------------------------------

  const handleLoadMorePastWeek = useCallback(async () => {
    const newLimit = pastWeekLimit + 10;
    setPastWeekLimit(newLimit);
    if (
      pastWeekChats.length <= newLimit &&
      sessionHasMore &&
      !sessionIsLoading
    ) {
      await loadMoreSessions();
    }
  }, [
    pastWeekLimit,
    pastWeekChats.length,
    sessionHasMore,
    sessionIsLoading,
    loadMoreSessions,
  ]);

  const handleLoadMoreOlder = useCallback(async () => {
    const newLimit = olderLimit + 10;
    setOlderLimit(newLimit);
    if (
      olderChats.length <= newLimit &&
      sessionHasMore &&
      !sessionIsLoading
    ) {
      await loadMoreSessions();
    }
  }, [
    olderLimit,
    olderChats.length,
    sessionHasMore,
    sessionIsLoading,
    loadMoreSessions,
  ]);

  // -------------------------------------------------------------------------
  // Visibility helpers
  // -------------------------------------------------------------------------
  const canLoadMorePastWeek =
    visiblePastWeekChats.length < pastWeekChats.length ||
    (sessionHasMore && pastWeekChats.length <= pastWeekLimit);

  const canLoadMoreOlder =
    visibleOlderChats.length < olderChats.length ||
    (sessionHasMore && olderChats.length <= olderLimit);

  const showErrorBanner = Boolean(sessionError || projectError);
  const isInitialLoading =
    sessionIsLoading && orderedSessions.length === 0 && orderedProjects.length === 0;

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------
  return (
    <>
      <aside className="w-[260px] border-r border-border bg-sidebg backdrop-blur-md flex flex-col h-screen select-none shrink-0 font-sans transition-all duration-200">
        {/* Workspace Header */}
        <div className="h-14 flex items-center justify-between px-4 border-b border-border">
          <span className="font-heading font-semibold text-xs tracking-wider text-muted-foreground uppercase">
            Workspaces &amp; Chats
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

        {/* Soft error banner — shown when a refresh failed but cached
            data is still being displayed. Per §13.9, data is NEVER
            wiped on failure, only marked as "cached". */}
        {showErrorBanner && (
          <div className="px-3 pt-2">
            <div className="flex items-start gap-1.5 p-2 rounded-md bg-amber-500/10 border border-amber-500/20 text-[10px] text-amber-600 dark:text-amber-400 font-mono">
              <AlertCircle size={11} className="shrink-0 mt-0.5" />
              <span>
                {sessionError || projectError}
                {" · showing cached data"}
              </span>
            </div>
          </div>
        )}

        {/* Scrollable Workspaces & Sessions */}
        <div className="flex-1 p-3 overflow-y-auto space-y-4 scrollbar-thin">
          {/* Initial loading indicator */}
          {isInitialLoading && (
            <div className="flex items-center justify-center gap-2 py-6 text-[11px] text-muted-foreground font-mono">
              <Loader2 size={12} className="animate-spin text-primary" />
              Loading workspaces…
            </div>
          )}

          {/* Workspaces Section */}
          <div>
            <div className="flex items-center justify-between text-[11px] font-semibold text-muted-foreground px-2 uppercase tracking-wider mb-1.5">
              <span>Workspaces</span>
              {displayProjects.length > 0 && (
                <span className="text-[10px] font-mono opacity-70">
                  {displayProjects.length}
                </span>
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
                  disabled={isCreating}
                  className="w-full px-2.5 py-1.5 rounded-xs bg-background text-xs border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-primary font-sans disabled:opacity-50"
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
                  const projChatsCount = orderedSessions.filter(
                    (c) => c.project_id === proj.id
                  ).length;
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

          {/* ─── Mode: chat ────────────────────────────────────────
              Show Pinned + Past 7 Days + Older Sessions.
              This is the pre-Phase-F layout, unchanged. */}
          {sidebarMode === "chat" && (
            <>
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

              {/* Recent Chats (Past 7 Days) */}
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

                {canLoadMorePastWeek && (
                  <button
                    type="button"
                    onClick={handleLoadMorePastWeek}
                    disabled={sessionIsLoading}
                    className="w-full mt-1.5 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground bg-muted/20 hover:bg-muted/50 border border-border/80 rounded flex items-center justify-center gap-1 transition cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {sessionIsLoading ? (
                      <Loader2 size={11} className="animate-spin" />
                    ) : (
                      <ChevronDown size={11} />
                    )}
                    <span>
                      {visiblePastWeekChats.length < pastWeekChats.length
                        ? "Load More (+10)"
                        : "Load More from Server"}
                    </span>
                  </button>
                )}
              </div>

              {/* Older Sessions */}
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

                  {canLoadMoreOlder && (
                    <button
                      type="button"
                      onClick={handleLoadMoreOlder}
                      disabled={sessionIsLoading}
                      className="w-full mt-2 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground bg-muted/20 hover:bg-muted/50 border border-border/80 rounded flex items-center justify-center gap-1 transition cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {sessionIsLoading ? (
                        <Loader2 size={11} className="animate-spin" />
                      ) : (
                        <ChevronDown size={11} />
                      )}
                      <span>
                        {visibleOlderChats.length < olderChats.length
                          ? `Load More (${Math.min(
                              olderChats.length - visibleOlderChats.length,
                              10
                            )} remaining)`
                          : "Load More from Server"}
                      </span>
                    </button>
                  )}
                </div>
              )}
            </>
          )}

          {/* ─── Mode: projects-list ────────────────────────────────
              Only Workspaces. No chat sessions. This keeps the
              user's focus on project management while they are
              browsing the projects page. */}
          {sidebarMode === "projects-list" && (
            <div className="text-[11px] text-muted-foreground/60 px-2 py-3 italic">
              Select a workspace to view its sessions.
            </div>
          )}

          {/* ─── Mode: project-detail ───────────────────────────────
              Sessions belonging to the current project only.
              Fetched lazily on first visit; cached for instant re-
              visits. See session-store.loadProjectSessions. */}
          {sidebarMode === "project-detail" && projectIdFromPath && (
            <ProjectSessionsSection
              projectId={projectIdFromPath}
              pathname={pathname}
              onTogglePin={handleTogglePin}
              onToggleArchive={handleToggleArchive}
              onDeleteChat={handleDeleteChat}
            />
          )}
        </div>

        {/* Footer System Status */}
        <Link
          href="/status"
          title="View System Health & Diagnostics"
          className="p-2.5 border-t border-border flex items-center justify-between text-[11px] text-muted-foreground font-mono hover:bg-muted/70 hover:text-foreground transition-all group cursor-pointer"
        >
          <span className="flex items-center gap-1.5">
            <Activity
              size={13}
              className="text-peldrun-accent group-hover:scale-110 transition-transform"
            />
            <span className="group-hover:text-foreground transition-colors">
              System Health
            </span>
          </span>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted border border-border text-muted-foreground group-hover:text-foreground">
            v1.0.0
          </span>
        </Link>

        <div className="flex items-center gap-2 shrink-0 p-2.5 border-t border-border justify-between text-[11px] text-muted-foreground font-mono" />
      </aside>

      <ConfirmDialog />
      <ToastContainer />
    </>
  );
}


// ---------------------------------------------------------------------------
// SidebarChatRow
//
// Unchanged from the pre-Phase-F version. It remains a pure presentational
// component: all state lives in `chat`, all interaction lives in the
// parent's callbacks.
// ---------------------------------------------------------------------------

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
  onDeleteChat,
}: SidebarChatRowProps) {
  const effectiveTarget = chat.job_id || chat.id;
  const isActive =
    pathname === `/chat/${effectiveTarget}` || pathname === `/chat/${chat.id}`;
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
              : chat.status === "cancelled"
              ? "bg-slate-400"
              : chat.status === "interrupted"
              ? "bg-orange-400"
              : "bg-muted-foreground/40"
          }`}

          title={`Status: ${chat.status || "unknown"}`}
        />
        <span className="truncate">
          {chat.title || chat.prompt || "New Session"}
        </span>
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


// ---------------------------------------------------------------------------
// ProjectSessionsSection — sessions belonging to a specific project.
//
// Rendered when the user is on /projects/{projectId}. Uses the session
// store's project cache (see session-store.loadProjectSessions), so:
//
//   * First visit to a project triggers exactly ONE fetch.
//   * Subsequent visits use the cache — no network round-trip.
//   * Live status updates propagate via SSE through the manager.
//
// Pagination is handled client-side; if the backend reports `hasMore`,
// a "Load More from Server" button appears at the bottom of the list.
// ---------------------------------------------------------------------------

interface ProjectSessionsSectionProps {
  projectId: string;
  pathname: string;
  onTogglePin: (e: React.MouseEvent, chatId: string) => void;
  onToggleArchive: (e: React.MouseEvent, chatId: string) => void;
  onDeleteChat: (e: React.MouseEvent, chatId: string, jobId: string) => void;
}

function ProjectSessionsSection({
  projectId,
  pathname,
  onTogglePin,
  onToggleArchive,
  onDeleteChat,
}: ProjectSessionsSectionProps) {
  // Read the cache and loading state for this project from the store.
  const projectSessions = useSessionStore(
    (s) => s.projectSessionsCache[projectId]
  );
  const isLoading = useSessionStore(
    (s) => s.projectSessionsLoading[projectId] === true
  );
  const hasMore = useSessionStore(
    (s) => s.projectSessionsHasMore[projectId] === true
  );
  const error = useSessionStore((s) => s.projectSessionsError[projectId]);
  const loadProjectSessions = useSessionStore((s) => s.loadProjectSessions);
  const loadMoreProjectSessions = useSessionStore(
    (s) => s.loadMoreProjectSessions
  );

  // Fetch on first mount for this project. The store's idempotency
  // guard makes this safe under StrictMode double-mount, and makes
  // subsequent re-mounts of the same project a no-op.
  useEffect(() => {
    void loadProjectSessions(projectId);
  }, [projectId, loadProjectSessions]);

  // Local pagination: show 10 at a time, same UX as the chat view.
  const [visibleLimit, setVisibleLimit] = useState(10);

  // Reset the local pagination when switching projects.
  useEffect(() => {
    setVisibleLimit(10);
  }, [projectId]);

  const allSessions = projectSessions ?? [];
  const visibleSessions = allSessions.slice(0, visibleLimit);
  const canLoadMoreLocally = visibleLimit < allSessions.length;
  const canLoadMoreFromServer = hasMore;

  const handleLoadMore = useCallback(async () => {
    if (canLoadMoreLocally) {
      setVisibleLimit((n) => n + 10);
      return;
    }
    if (canLoadMoreFromServer) {
      await loadMoreProjectSessions(projectId);
    }
  }, [
    canLoadMoreLocally,
    canLoadMoreFromServer,
    loadMoreProjectSessions,
    projectId,
  ]);

  return (
    <div>
      <div className="flex items-center justify-between text-[11px] font-semibold text-muted-foreground px-2 uppercase tracking-wider mb-1.5">
        <span>Sessions in Project</span>
        <span className="text-[10px] font-mono opacity-70">
          {visibleSessions.length}/{allSessions.length}
        </span>
      </div>

      {/* Error banner for this project's fetch */}
      {error && (
        <div className="mx-1 mb-2 p-2 rounded-md bg-amber-500/10 border border-amber-500/20 text-[10px] text-amber-600 dark:text-amber-400 font-mono flex items-start gap-1.5">
          <AlertCircle size={11} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {/* Initial loading spinner */}
      {isLoading && allSessions.length === 0 && (
        <div className="flex items-center justify-center gap-2 py-4 text-[11px] text-muted-foreground font-mono">
          <Loader2 size={12} className="animate-spin text-primary" />
          Loading project sessions…
        </div>
      )}

      {/* Empty state */}
      {!isLoading && allSessions.length === 0 && !error && (
        <div className="text-[11px] text-muted-foreground/70 px-2 py-3">
          No sessions in this project yet.
        </div>
      )}

      {/* Session rows */}
      <div className="space-y-0.5">
        {visibleSessions.map((chat) => (
          <SidebarChatRow
            key={chat.id}
            chat={chat}
            pathname={pathname}
            onTogglePin={onTogglePin}
            onToggleArchive={onToggleArchive}
            onDeleteChat={onDeleteChat}
          />
        ))}
      </div>

      {/* Pagination button */}
      {(canLoadMoreLocally || canLoadMoreFromServer) && (
        <button
          type="button"
          onClick={handleLoadMore}
          disabled={isLoading}
          className="w-full mt-1.5 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground bg-muted/20 hover:bg-muted/50 border border-border/80 rounded flex items-center justify-center gap-1 transition cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isLoading ? (
            <Loader2 size={11} className="animate-spin" />
          ) : (
            <ChevronDown size={11} />
          )}
          <span>
            {canLoadMoreLocally
              ? `Load More (${Math.min(
                  allSessions.length - visibleSessions.length,
                  10
                )} remaining)`
              : "Load More from Server"}
          </span>
        </button>
      )}
    </div>
  );
}


export default Sidebar;
