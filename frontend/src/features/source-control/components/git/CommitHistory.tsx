// frontend/src/features/source-control/components/git/CommitHistory.tsx

import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  FolderOpen,
  GitCommit,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type {
  CommitEntryResponse,
  CommitWithStatsResponse,
} from "../../api/schemas";

export interface CommitHistoryProps {
  workspaceId: string;
  /** Git ref to browse. Defaults to HEAD. */
  ref?: string;
  /** Page size for both initial load and "load more". */
  pageSize?: number;
  /** Called when the user clicks "Browse files at this commit". */
  onBrowseRef?: (sha: string) => void;
}

const DEFAULT_PAGE_SIZE = 30;

/**
 * Paginated commit history.
 *
 * Renders a flat, newest-first list of commits. Expanding a row
 * lazily fetches the commit's aggregate file-change stats via
 * `getCommit`. The initial page is fetched on mount and whenever
 * `ref` changes; subsequent pages are appended with a "Load more"
 * button.
 *
 * DESIGN — no virtualization:
 *   The list is paginated at 30 rows per page. Even with a handful
 *   of "load more" clicks the DOM stays small enough that
 *   virtualization would be overkill; adding it now would force
 *   every consumer to pass explicit heights, which the phase-1
 *   layout does not want.
 *
 * DESIGN — lazy stats:
 *   Stats are fetched only when a row is expanded. Fetching them
 *   eagerly for every commit would require N additional `git show`
 *   invocations for a list that most users scroll past without
 *   inspecting. The expanded-state cache is keyed by SHA, so
 *   re-expanding the same row is instant.
 */
export function CommitHistory({
  workspaceId,
  ref = "HEAD",
  pageSize = DEFAULT_PAGE_SIZE,
  onBrowseRef,
}: CommitHistoryProps) {
  const [commits, setCommits] = useState<CommitEntryResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [expandedSha, setExpandedSha] = useState<string | null>(null);
  const [stats, setStats] = useState<
    Record<string, CommitWithStatsResponse>
  >({});
  const [statsLoading, setStatsLoading] = useState<
    Record<string, boolean>
  >({});

  const loadFirstPage = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setCommits([]);
    setHasMore(false);
    setExpandedSha(null);
    setStats({});
    setStatsLoading({});

    sourceControlClient
      .getLog(workspaceId, { limit: pageSize, ref })
      .then((data) => {
        if (cancelled) return;
        setCommits(data);
        setHasMore(data.length === pageSize);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof Error ? err.message : "Failed to load commits."
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [workspaceId, ref, pageSize]);

  useEffect(() => {
    const cleanup = loadFirstPage();
    return cleanup;
  }, [loadFirstPage]);

  const loadMore = useCallback(() => {
    if (loading || loadingMore || !hasMore) return;
    let cancelled = false;
    setLoadingMore(true);

    sourceControlClient
      .getLog(workspaceId, {
        limit: pageSize,
        skip: commits.length,
        ref,
      })
      .then((data) => {
        if (cancelled) return;
        setCommits((prev) => [...prev, ...data]);
        setHasMore(data.length === pageSize);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof Error
            ? err.message
            : "Failed to load more commits."
        );
      })
      .finally(() => {
        if (!cancelled) setLoadingMore(false);
      });

    return () => {
      cancelled = true;
    };
  }, [
    workspaceId,
    ref,
    pageSize,
    commits.length,
    loading,
    loadingMore,
    hasMore,
  ]);

  const toggleExpand = useCallback(
    (sha: string) => {
      if (expandedSha === sha) {
        setExpandedSha(null);
        return;
      }
      setExpandedSha(sha);
      if (!stats[sha] && !statsLoading[sha]) {
        setStatsLoading((prev) => ({ ...prev, [sha]: true }));
        sourceControlClient
          .getCommit(workspaceId, sha)
          .then((data) => {
            setStats((prev) => ({ ...prev, [sha]: data }));
          })
          .catch(() => {
            // Stats fetch failure is non-fatal: the row stays
            // expandable and the user sees a placeholder instead
            // of numbers. The failure is not surfaced as a banner
            // because it would be noise on every row that happens
            // to hit a transient error.
          })
          .finally(() => {
            setStatsLoading((prev) => ({ ...prev, [sha]: false }));
          });
      }
    },
    [expandedSha, stats, statsLoading, workspaceId]
  );

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-card/40 shrink-0">
        <div className="text-xs font-semibold text-foreground truncate">
          Commit history
          <span className="ml-2 text-[10px] font-normal text-muted-foreground">
            @ {ref}
          </span>
        </div>
        <button
          type="button"
          onClick={loadFirstPage}
          disabled={loading}
          className="p-1 rounded hover:bg-muted disabled:opacity-50"
          title="Reload history"
          aria-label="Reload history"
        >
          <RefreshCw
            className={`w-3.5 h-3.5 text-muted-foreground ${
              loading ? "animate-spin" : ""
            }`}
          />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto">
        {loading && (
          <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Loading commits…
          </div>
        )}

        {!loading && error && (
          <div className="p-3 flex items-start gap-2 text-xs text-red-700">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {!loading && !error && commits.length === 0 && (
          <div className="p-3 text-xs text-muted-foreground">
            No commits yet.
          </div>
        )}

        {!loading && !error && commits.length > 0 && (
          <ul className="divide-y divide-border">
            {commits.map((commit) => {
              const isExpanded = expandedSha === commit.sha;
              const commitStats = stats[commit.sha];
              const statsLoadingThis = statsLoading[commit.sha] ?? false;
              return (
                <li key={commit.sha} className="hover:bg-muted/30">
                  <button
                    type="button"
                    onClick={() => toggleExpand(commit.sha)}
                    className="w-full text-left px-3 py-2 flex items-start gap-2"
                  >
                    {isExpanded ? (
                      <ChevronDown className="w-3.5 h-3.5 mt-0.5 shrink-0 text-muted-foreground" />
                    ) : (
                      <ChevronRight className="w-3.5 h-3.5 mt-0.5 shrink-0 text-muted-foreground" />
                    )}
                    <GitCommit className="w-3.5 h-3.5 mt-0.5 shrink-0 text-purple-500" />
                    <div className="flex-1 min-w-0">
                      <div className="text-xs font-medium text-foreground truncate">
                        {firstLine(commit.message) || "(no message)"}
                      </div>
                      <div className="text-[10px] text-muted-foreground mt-0.5 flex items-center gap-1.5 flex-wrap">
                        <code className="font-mono">
                          {commit.sha.slice(0, 8)}
                        </code>
                        <span>·</span>
                        <span className="truncate max-w-[10em]">
                          {commit.author_name}
                        </span>
                        <span>·</span>
                        <span>{formatDate(commit.committed_at)}</span>
                      </div>
                    </div>
                  </button>

                  {isExpanded && (
                    <div className="px-9 pb-2 text-[11px] text-muted-foreground space-y-1.5">
                      {statsLoadingThis && (
                        <div className="flex items-center gap-1.5">
                          <Loader2 className="w-3 h-3 animate-spin" />
                          Loading stats…
                        </div>
                      )}
                      {commitStats && (
                        <div className="flex items-center gap-3">
                          <span>
                            {commitStats.files_changed} file
                            {commitStats.files_changed === 1 ? "" : "s"}
                          </span>
                          <span className="text-green-600">
                            +{commitStats.insertions}
                          </span>
                          <span className="text-red-600">
                            −{commitStats.deletions}
                          </span>
                        </div>
                      )}
                      {!statsLoadingThis && !commitStats && (
                        <div className="italic">Stats unavailable.</div>
                      )}
                      {onBrowseRef && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            onBrowseRef(commit.sha);
                          }}
                          className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
                        >
                          <FolderOpen className="w-3 h-3" />
                          Browse files at this commit
                        </button>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {!loading && !error && hasMore && (
          <div className="p-3 border-t border-border">
            <button
              type="button"
              onClick={loadMore}
              disabled={loadingMore}
              className="w-full text-xs px-3 py-1.5 rounded border border-border hover:bg-muted disabled:opacity-50 inline-flex items-center justify-center gap-1.5"
            >
              {loadingMore && (
                <Loader2 className="w-3 h-3 animate-spin" />
              )}
              Load more
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function firstLine(message: string): string {
  if (!message) return "";
  const idx = message.indexOf("\n");
  return idx < 0 ? message : message.slice(0, idx);
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
