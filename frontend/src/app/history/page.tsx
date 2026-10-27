// frontend/src/app/history/page.tsx

"use client";

import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
import Link from "next/link";
import {
  History,
  HardDrive,
  Trash2,
  Download,
  ExternalLink,
  Calendar,
  Pin,
  Archive,
  RefreshCw,
  Search,
  X,
  FolderOpen,
  ChevronDown,
  Loader2,
  AlertCircle,
} from "lucide-react";
import {
  fetchChatsPage,
  fetchStorageStats,
  sweepStorageOrphans,
  togglePinChat,
  toggleArchiveChat,
  deleteChat,
  deleteAllChats,
  formatChatDateTime,
  ChatSession,
  ChatTimeRange,
  StorageStats,
  SweepReport,
  PurgeReport,
} from "@/lib/chatsApi";
import { Button } from "@/components/ui/button";
import { useConfirmModal } from "@/components/ui/ConfirmModal";
import { showToast, ToastContainer } from "@/components/ui/ToastNotification";

/**
 * Number of chats loaded per section per "Load More" click. Chosen to
 * keep the initial render fast while still showing meaningful context.
 */
const SECTION_PAGE_SIZE = 10;

/** Debounce window for the search input (milliseconds). */
const SEARCH_DEBOUNCE_MS = 300;

type TabKey = "all" | "active" | "pinned" | "archived";

const TIME_RANGES: ChatTimeRange[] = [
  "today",
  "yesterday",
  "last_7_days",
  "last_30_days",
  "older",
];

const SECTION_LABELS: Record<ChatTimeRange, string> = {
  today: "Today",
  yesterday: "Yesterday",
  last_7_days: "Last 7 Days",
  last_30_days: "Last 30 Days",
  older: "Older",
};

interface SectionState {
  items: ChatSession[];
  total: number | null;
  nextCursor: string | null;
  hasMore: boolean;
  loadingInitial: boolean;
  loadingMore: boolean;
  error: string | null;
}

const emptySection = (): SectionState => ({
  items: [],
  total: null,
  nextCursor: null,
  hasMore: false,
  loadingInitial: false,
  loadingMore: false,
  error: null,
});

const buildInitialSections = (): Record<ChatTimeRange, SectionState> =>
  TIME_RANGES.reduce(
    (acc, r) => ({ ...acc, [r]: emptySection() }),
    {} as Record<ChatTimeRange, SectionState>
  );

export default function HistoryPage() {
  const { confirm, ConfirmDialog } = useConfirmModal();

  const [sections, setSections] = useState<Record<ChatTimeRange, SectionState>>(
    buildInitialSections
  );
  const [stats, setStats] = useState<StorageStats | null>(null);

  const [sweeping, setSweeping] = useState(false);
  const [clearingAll, setClearingAll] = useState(false);

  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [activeTab, setActiveTab] = useState<TabKey>("all");

  // Monotonic generation: any section request tagged with an older
  // generation must be discarded. Guards against rapid tab/search
  // switches.
  const generationRef = useRef(0);
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ----------------------------------------------------------------
  // Param builder: shared filter context for every section.
  // ----------------------------------------------------------------
  const buildBaseParams = useCallback(() => {
    const base: {
      limit: number;
      includeArchived?: boolean;
      pinnedOnly?: boolean;
      archivedOnly?: boolean;
      search?: string;
    } = { limit: SECTION_PAGE_SIZE };
    if (debouncedSearch) base.search = debouncedSearch;

    switch (activeTab) {
      case "active":
        base.includeArchived = false;
        break;
      case "pinned":
        base.includeArchived = true;
        base.pinnedOnly = true;
        break;
      case "archived":
        base.includeArchived = true;
        base.archivedOnly = true;
        break;
      case "all":
      default:
        base.includeArchived = true;
        break;
    }
    return base;
  }, [activeTab, debouncedSearch]);

  // ----------------------------------------------------------------
  // Section loaders
  // ----------------------------------------------------------------
  const loadSection = useCallback(
    async (
      range: ChatTimeRange,
      mode: "initial" | "more",
      cursorOverride?: string | null
    ) => {
      const myGen = generationRef.current;
      setSections((prev) => {
        const current = prev[range];
        return {
          ...prev,
          [range]: {
            ...current,
            loadingInitial: mode === "initial" ? true : current.loadingInitial,
            loadingMore: mode === "more" ? true : current.loadingMore,
            error: null,
          },
        };
      });

      try {
        const cursor =
          mode === "more" ? cursorOverride ?? undefined : undefined;
        const page = await fetchChatsPage({
          ...buildBaseParams(),
          timeRange: range,
          cursor: cursor || undefined,
        });
        if (myGen !== generationRef.current) return;

        setSections((prev) => {
          const current = prev[range];
          const items =
            mode === "initial"
              ? page.items
              : [...current.items, ...page.items];
          return {
            ...prev,
            [range]: {
              items,
              total: page.total,
              nextCursor: page.nextCursor,
              hasMore: page.hasMore,
              loadingInitial: false,
              loadingMore: false,
              error: null,
            },
          };
        });
      } catch (err: any) {
        if (myGen !== generationRef.current) return;
        setSections((prev) => ({
          ...prev,
          [range]: {
            ...prev[range],
            loadingInitial: false,
            loadingMore: false,
            error: err?.message || "Failed to load section",
          },
        }));
      }
    },
    [buildBaseParams]
  );

  const loadAllSections = useCallback(() => {
    generationRef.current += 1;
    setSections(buildInitialSections());
    // Fire all section requests in parallel.
    TIME_RANGES.forEach((range) => {
      loadSection(range, "initial");
    });
  }, [loadSection]);

  const refreshStats = useCallback(() => {
    fetchStorageStats().then((s) => s && setStats(s));
  }, []);

  // ----------------------------------------------------------------
  // Effects
  // ----------------------------------------------------------------
  useEffect(() => {
    if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current);
    searchDebounceRef.current = setTimeout(() => {
      setDebouncedSearch(searchQuery);
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current);
    };
  }, [searchQuery]);

  useEffect(() => {
    loadAllSections();
  }, [loadAllSections]);

  useEffect(() => {
    refreshStats();
  }, [refreshStats]);

  // ----------------------------------------------------------------
  // Derived counts for tab badges
  // ----------------------------------------------------------------
  const allCount = useMemo(
    () => stats?.total_chats ?? 0,
    [stats]
  );
  const activeCount = useMemo(
    () => stats?.active_chats ?? 0,
    [stats]
  );
  const pinnedCount = useMemo(
    () => stats?.pinned_chats ?? 0,
    [stats]
  );
  const archivedCount = useMemo(
    () => stats?.archived_chats ?? 0,
    [stats]
  );

  const anyLoading = useMemo(
    () => TIME_RANGES.some((r) => sections[r].loadingInitial),
    [sections]
  );

  const totalVisible = useMemo(
    () => TIME_RANGES.reduce((sum, r) => sum + sections[r].items.length, 0),
    [sections]
  );

  const hasAnyError = useMemo(
    () => TIME_RANGES.some((r) => !!sections[r].error),
    [sections]
  );

  // ----------------------------------------------------------------
  // Handlers
  // ----------------------------------------------------------------
  const handleSweepStorage = async () => {
    setSweeping(true);
    try {
      const report: SweepReport | null = await sweepStorageOrphans();
      if (report && report.status === "ok") {
        showToast.success(
          "Storage Swept Clean",
          `Cleaned ${report.removed_orphans_count} orphans and freed ${report.freed_mb} MB.`
        );
        loadAllSections();
        refreshStats();
      } else {
        showToast.info("Sweep Clean", "No orphaned workspace folders found on disk.");
      }
    } catch (err: any) {
      showToast.error("Sweep Error", err?.message || "Failed");
    } finally {
      setSweeping(false);
    }
  };

  const handleClearAllChats = async () => {
    const ok = await confirm({
      title: "Purge All Chats & Workspace Artifacts?",
      description:
        "Warning: This action will permanently wipe all chats, delete all workspace folders, and completely reset jobs.json to empty. This cannot be undone.",
      variant: "danger",
      confirmText: "Purge Everything",
    });
    if (!ok) return;
    setClearingAll(true);
    try {
      const report: PurgeReport | null = await deleteAllChats();
      if (report && report.status === "ok") {
        showToast.success(
          "Complete Disk Purge",
          `Purged ${report.deleted_sessions_count} sessions and freed ${report.freed_mb} MB.`
        );
        window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        loadAllSections();
        refreshStats();
      } else {
        showToast.error("Purge Failed", "Server could not complete deep purge.");
      }
    } catch (err: any) {
      showToast.error("Purge Error", err?.message);
    } finally {
      setClearingAll(false);
    }
  };

  const handleTogglePin = async (chatId: string) => {
    try {
      const updated = await togglePinChat(chatId);
      if (updated) {
        showToast.success(
          "Updated",
          updated.is_pinned ? "Session pinned to top." : "Session unpinned."
        );
        window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        loadAllSections();
        refreshStats();
      }
    } catch (err: any) {
      showToast.error("Error", err?.message);
    }
  };

  const handleToggleArchive = async (chatId: string) => {
    try {
      const updated = await toggleArchiveChat(chatId);
      if (updated) {
        showToast.info(
          "Status Updated",
          updated.is_archived
            ? "Archived and hidden from sidebar."
            : "Restored to active sidebar."
        );
        window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        loadAllSections();
        refreshStats();
      }
    } catch (err: any) {
      showToast.error("Error", err?.message);
    }
  };

  const handleDelete = async (chatId: string) => {
    const ok = await confirm({
      title: "Purge Session",
      description:
        "Permanently delete this session? Deliverables, workspace logs, and jobs.json records will be wiped.",
      variant: "danger",
      confirmText: "Delete Permanently",
    });
    if (!ok) return;
    try {
      const success = await deleteChat(chatId);
      if (success) {
        showToast.success("Session Purged", `Session ${chatId} completely removed.`);
        window.dispatchEvent(new CustomEvent("omweb:chats-updated"));
        loadAllSections();
        refreshStats();
      } else {
        showToast.error("Delete Failed", "Server could not delete session.");
      }
    } catch (err: any) {
      showToast.error("Delete Error", err?.message);
    }
  };

  // ----------------------------------------------------------------
  // Render
  // ----------------------------------------------------------------
  const isEmpty = !anyLoading && totalVisible === 0 && !hasAnyError;

  return (
    <>
      <div className="flex flex-col flex-1 h-full w-full bg-background text-foreground overflow-hidden font-sans">
        {/* Header Bar */}
        <header className="border-b border-border bg-card/80 backdrop-blur-md sticky top-0 z-10 shrink-0">
          <div className="mx-auto w-full px-4 sm:px-6 lg:px-8 py-3.5 flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-xl bg-primary/10 text-primary border border-primary/20 shadow-xs">
                <History className="h-5 w-5" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <h1 className="text-base font-bold text-foreground tracking-tight font-serif">
                    Session Archives & Disk Governance
                  </h1>
                  <span className="hidden sm:inline-flex px-2 py-0.5 text-[10px] font-mono font-medium rounded-full bg-muted text-muted-foreground border border-border">
                    Sovereign Core
                  </span>
                </div>
                <p className="text-xs text-muted-foreground">
                  Time-bucketed sessions, per-section pagination, and cascaded purging
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handleClearAllChats}
                disabled={clearingAll || allCount === 0}
                className="text-xs border-destructive/40 bg-destructive/10 hover:bg-destructive/20 text-destructive transition shadow-xs h-8 cursor-pointer"
                title="Permanently wipe all chats, files, and jobs.json"
              >
                <Trash2 size={13} className="mr-1.5" />
                <span>{clearingAll ? "Purging..." : "Purge All Chats"}</span>
              </Button>

              <Button
                variant="outline"
                size="sm"
                onClick={handleSweepStorage}
                disabled={sweeping}
                className="text-xs border-border bg-muted/50 hover:bg-muted text-foreground transition shadow-xs h-8 cursor-pointer"
                title="Sweep orphaned workspace files not linked to existing sessions"
              >
                <HardDrive
                  size={13}
                  className={`mr-1.5 text-primary ${sweeping ? "animate-spin" : ""}`}
                />
                <span>Sweep Orphan Storage</span>
              </Button>

              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  loadAllSections();
                  refreshStats();
                }}
                disabled={anyLoading}
                className="text-xs border-border bg-muted/50 hover:bg-muted text-foreground transition shadow-xs h-8 cursor-pointer"
              >
                <RefreshCw
                  size={13}
                  className={`mr-1.5 ${anyLoading ? "animate-spin text-primary" : ""}`}
                />
                Refresh
              </Button>
            </div>
          </div>
        </header>

        {/* Metrics Bar */}
        {stats && (
          <div className="border-b border-border bg-muted/15 py-3 shrink-0 font-sans">
            <div className="max-w-[1240px] mx-auto px-4 sm:px-6 lg:px-8 grid grid-cols-2 sm:grid-cols-4 gap-3">
              <MetricCard label="Total Sessions" value={stats.total_chats} icon={<FolderOpen size={14} />} />
              <MetricCard
                label="Pinned Active"
                value={stats.pinned_chats}
                valueClass="text-primary"
                icon={<Pin size={14} className="rotate-45" />}
                iconClass="bg-primary/10 text-primary"
              />
              <MetricCard label="Archived" value={stats.archived_chats} icon={<Archive size={14} />} />
              <MetricCard
                label="Disk Consumption"
                value={`${stats.disk_size_mb} MB`}
                valueClass="text-emerald-600 dark:text-emerald-400"
                icon={<HardDrive size={14} />}
                iconClass="bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
              />
            </div>
          </div>
        )}

        {/* Tabs & Search */}
        <div className="w-full border-b border-border bg-custom">
          <div className="max-w-[1240px] mx-auto w-full px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="flex items-center gap-1 overflow-x-auto pt-2 scrollbar-none">
              <TabButton label="All Sessions" count={allCount} active={activeTab === "all"} onClick={() => setActiveTab("all")} />
              <TabButton label="Active" count={activeCount} active={activeTab === "active"} onClick={() => setActiveTab("active")} />
              <TabButton
                label="Pinned"
                count={pinnedCount}
                active={activeTab === "pinned"}
                onClick={() => setActiveTab("pinned")}
                icon={<Pin size={12} className="rotate-45" />}
              />
              <TabButton
                label="Archived"
                count={archivedCount}
                active={activeTab === "archived"}
                onClick={() => setActiveTab("archived")}
                icon={<Archive size={12} />}
              />
            </div>

            <div className="flex items-center gap-2.5 pb-2 sm:pb-0">
              <div className="relative w-full sm:w-72">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground h-3.5 w-3.5" />
                <input
                  type="text"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="Search title, prompt, job ID, or paths..."
                  className="w-full pl-8 pr-7 py-1.5 text-xs rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary shadow-xs"
                />
                {searchQuery && (
                  <button
                    type="button"
                    onClick={() => setSearchQuery("")}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground cursor-pointer"
                  >
                    <X size={12} />
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Main Content Area */}
        <main className="w-full flex-1 overflow-y-auto scrollbar-thin">
          <div className="max-w-[1240px] mx-auto w-full px-4 sm:px-6 lg:px-8 py-6 space-y-6">
            {isEmpty ? (
              <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
                <History className="h-10 w-10 text-muted-foreground mb-3" />
                <h3 className="text-sm font-semibold text-foreground">
                  No sessions match your filter
                </h3>
                <p className="text-xs text-muted-foreground max-w-sm mt-1">
                  {searchQuery
                    ? "Try refining your search keyword."
                    : "Start a new task in Chat to create sessions."}
                </p>
              </div>
            ) : (
              <>
                {TIME_RANGES.map((range) => (
                  <SectionBlock
                    key={range}
                    range={range}
                    label={SECTION_LABELS[range]}
                    state={sections[range]}
                    onLoadMore={(cursor) => loadSection(range, "more", cursor)}
                    onRetry={() => loadSection(range, "initial")}
                    onTogglePin={handleTogglePin}
                    onToggleArchive={handleToggleArchive}
                    onDelete={handleDelete}
                  />
                ))}
              </>
            )}
          </div>
        </main>
      </div>

      <ConfirmDialog />
      <ToastContainer />
    </>
  );
}

// ----------------------------------------------------------------
// Sub-components
// ----------------------------------------------------------------

interface MetricCardProps {
  label: string;
  value: number | string;
  valueClass?: string;
  icon: React.ReactNode;
  iconClass?: string;
}

function MetricCard({ label, value, valueClass, icon, iconClass }: MetricCardProps) {
  return (
    <div className="p-2.5 rounded-lg border border-border/80 bg-card shadow-xs flex items-center justify-between">
      <div>
        <span className="text-[10px] uppercase font-bold text-muted-foreground">
          {label}
        </span>
        <p className={`text-sm font-bold font-mono ${valueClass ?? "text-foreground"}`}>
          {value}
        </p>
      </div>
      <div className={`p-1.5 rounded-md ${iconClass ?? "bg-muted text-muted-foreground"}`}>
        {icon}
      </div>
    </div>
  );
}

interface TabButtonProps {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  icon?: React.ReactNode;
}

function TabButton({ label, count, active, onClick, icon }: TabButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-2 px-4 py-2.5 text-xs font-semibold border-b-2 whitespace-nowrap transition-all cursor-pointer ${
        active
          ? "border-primary text-primary bg-primary/5"
          : "border-transparent text-muted-foreground hover:text-foreground"
      }`}
    >
      {icon}
      <span>{label}</span>
      <span className="ml-1 px-1.5 py-0.5 rounded-full text-[10px] font-mono bg-muted text-muted-foreground border border-border">
        {count}
      </span>
    </button>
  );
}

interface SectionBlockProps {
  range: ChatTimeRange;
  label: string;
  state: SectionState;
  onLoadMore: (cursor: string | null) => void;
  onRetry: () => void;
  onTogglePin: (chatId: string) => void;
  onToggleArchive: (chatId: string) => void;
  onDelete: (chatId: string) => void;
}

function SectionBlock({
  label,
  state,
  onLoadMore,
  onRetry,
  onTogglePin,
  onToggleArchive,
  onDelete,
}: SectionBlockProps) {
  const {
    items,
    total,
    nextCursor,
    hasMore,
    loadingInitial,
    loadingMore,
    error,
  } = state;

  // Hide empty sections entirely (but keep them visible while loading
  // their first page so the user sees progress).
  if (!loadingInitial && !error && items.length === 0) {
    return null;
  }

  const remaining = total !== null ? Math.max(0, total - items.length) : 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-2.5 px-1">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold uppercase tracking-wider text-foreground">
            {label}
          </span>
          {total !== null && (
            <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-primary/10 text-primary border border-primary/20">
              {items.length}/{total}
            </span>
          )}
        </div>
      </div>

      {error ? (
        <div className="flex items-start gap-3 p-3 rounded-lg border border-destructive/40 bg-destructive/10 text-destructive text-xs">
          <AlertCircle size={14} className="mt-0.5 flex-shrink-0" />
          <div className="flex-1">
            <p className="font-semibold">Failed to load {label.toLowerCase()}</p>
            <p className="text-destructive/80">{error}</p>
          </div>
          <button
            type="button"
            onClick={onRetry}
            className="text-xs font-medium underline cursor-pointer"
          >
            Retry
          </button>
        </div>
      ) : loadingInitial ? (
        <div className="flex items-center justify-center py-6 text-xs text-muted-foreground gap-2">
          <Loader2 size={13} className="animate-spin" />
          Loading {label.toLowerCase()}...
        </div>
      ) : (
        <>
          <div className="space-y-1.5">
            {items.map((chat) => (
              <HistoryListRow
                key={chat.id}
                chat={chat}
                onTogglePin={onTogglePin}
                onToggleArchive={onToggleArchive}
                onDelete={onDelete}
              />
            ))}
          </div>

          {hasMore && (
            <div className="pt-2.5 flex justify-center">
              <Button
                variant="outline"
                size="sm"
                onClick={() => onLoadMore(nextCursor)}
                disabled={loadingMore}
                className="text-xs border-border bg-card hover:bg-muted text-foreground transition shadow-xs cursor-pointer gap-1.5 h-7"
              >
                {loadingMore ? (
                  <>
                    <Loader2 size={12} className="animate-spin" />
                    <span>Loading...</span>
                  </>
                ) : (
                  <>
                    <ChevronDown size={12} />
                    <span>
                      Load More {label}
                      {remaining > 0 ? ` (+${Math.min(SECTION_PAGE_SIZE, remaining)})` : ""}
                    </span>
                  </>
                )}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

interface HistoryListRowProps {
  chat: ChatSession;
  onTogglePin: (chatId: string) => void;
  onToggleArchive: (chatId: string) => void;
  onDelete: (chatId: string) => void;
}

function HistoryListRow({
  chat,
  onTogglePin,
  onToggleArchive,
  onDelete,
}: HistoryListRowProps) {
  const isPinned = chat.is_pinned || chat.pinned;
  const isArchived = chat.is_archived;
  const effectiveTarget = chat.job_id || chat.id;

  return (
    <div
      className={`flex items-center justify-between p-3 rounded-lg border transition-all text-xs ${
        isPinned
          ? "border-primary/40 bg-primary/5 shadow-xs"
          : isArchived
          ? "border-border/60 bg-muted/20 opacity-80"
          : "border-border bg-card hover:border-primary/30 shadow-xs"
      }`}
    >
      <div className="flex items-center gap-3 min-w-0 flex-1 mr-4">
        <span
          className={`shrink-0 w-2.5 h-2.5 rounded-full ${
            chat.status === "completed"
              ? "bg-emerald-500"
              : chat.status === "running"
              ? "bg-amber-500 animate-pulse"
              : "bg-muted-foreground/40"
          }`}
          title={`Status: ${chat.status || "active"}`}
        />

        <span className="font-semibold text-foreground truncate select-all">
          {chat.title || chat.prompt || "Untitled Session"}
        </span>

        {isPinned && (
          <span className="shrink-0 px-1.5 py-0.2 rounded text-[9px] font-mono uppercase bg-primary/10 text-primary border border-primary/20 flex items-center gap-0.5">
            <Pin size={8} className="rotate-45" /> PINNED
          </span>
        )}

        {isArchived && (
          <span className="shrink-0 px-1.5 py-0.2 rounded text-[9px] font-mono uppercase bg-muted text-muted-foreground border border-border flex items-center gap-0.5">
            <Archive size={8} /> ARCHIVED
          </span>
        )}

        <span className="hidden sm:flex items-center gap-1 text-[11px] font-mono text-muted-foreground shrink-0 ml-auto mr-2">
          <Calendar size={11} className="opacity-70" />
          <span>{formatChatDateTime(chat.created_at)}</span>
        </span>
      </div>

      <div className="flex items-center gap-1.5 shrink-0">
        <Link
          href={`/chat/${effectiveTarget}`}
          className="flex items-center gap-1 px-2.5 py-1 rounded bg-primary/10 hover:bg-primary text-primary hover:text-primary-foreground font-medium text-[11px] transition"
        >
          <ExternalLink size={11} />
          <span>Open</span>
        </Link>

        <button
          type="button"
          onClick={() => onTogglePin(chat.id)}
          className={`p-1.5 rounded-md border text-[11px] transition cursor-pointer ${
            isPinned
              ? "bg-primary/15 border-primary/40 text-primary font-bold"
              : "bg-muted/50 border-border text-muted-foreground hover:text-foreground hover:bg-muted"
          }`}
          title={isPinned ? "Unpin session" : "Pin session to top"}
        >
          <Pin size={12} className={isPinned ? "rotate-45" : ""} />
        </button>

        <button
          type="button"
          onClick={() => onToggleArchive(chat.id)}
          className={`p-1.5 rounded-md border text-[11px] transition cursor-pointer ${
            isArchived
              ? "bg-muted border-border text-foreground font-semibold"
              : "bg-muted/50 border-border text-muted-foreground hover:text-foreground hover:bg-muted"
          }`}
          title={isArchived ? "Restore to sidebar" : "Archive session"}
        >
          <Archive size={12} />
        </button>

        <a
          href={`/api/chats/${chat.id}/log/download`}
          title="Download chat log"
          className="p-1.5 rounded-md bg-muted/50 hover:bg-muted text-foreground border border-border text-[11px] transition"
        >
          <Download size={12} />
        </a>

        <button
          type="button"
          onClick={() => onDelete(chat.id)}
          title="Permanently purge session from disk and jobs.json"
          className="p-1.5 rounded-md bg-muted/50 hover:bg-destructive/10 text-muted-foreground hover:text-destructive border border-border transition cursor-pointer"
        >
          <Trash2 size={12} />
        </button>
      </div>
    </div>
  );
}
