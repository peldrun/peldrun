// frontend/src/features/source-control/components/github/PullRequestList.tsx

import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  ChevronRight,
  GitPullRequest,
  Loader2,
  Plus,
  RefreshCw,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type { PullRequestResponse } from "../../api/schemas";

export interface PullRequestListProps {
  repositoryId: string;
  provider?: string;
  /** Called when the user clicks a PR row. */
  onSelect?: (number: string) => void;
  /** Called when the user clicks the "New PR" button. */
  onCreateNew?: () => void;
}

type StateFilter = "open" | "closed" | "merged" | "all";

const FILTER_OPTIONS: { key: StateFilter; label: string }[] = [
  { key: "open", label: "Open" },
  { key: "closed", label: "Closed" },
  { key: "merged", label: "Merged" },
  { key: "all", label: "All" },
];

/**
 * Paginated list of pull requests with a state filter.
 *
 * The list is fetched on mount and whenever the filter, repository,
 * or provider changes. Pagination uses a "load more" button rather
 * than infinite scroll: PR listings are typically short enough that
 * a button is simpler and more predictable, and avoids the
 * scroll-anchoring bugs that plague infinite-scroll implementations
 * on variable-height rows.
 *
 * STATE BADGES:
 *   GitHub returns `state` as `open`/`closed`, plus a separate
 *   `merged_at` field for merged PRs and a `draft` boolean. The
 *   list view shows a derived badge:
 *     - draft  : state == "open" && is_draft
 *     - open   : state == "open" && !is_draft
 *     - merged : state == "merged" || merged_at != null
 *     - closed : state == "closed" && !merged_at
 */
export function PullRequestList({
  repositoryId,
  provider = "github",
  onSelect,
  onCreateNew,
}: PullRequestListProps) {
  const [filter, setFilter] = useState<StateFilter>("open");
  const [items, setItems] = useState<PullRequestResponse[]>([]);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const PAGE_SIZE = 30;

  const loadFirstPage = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setItems([]);
    setPage(1);
    setHasMore(false);

    sourceControlClient
      .listPullRequests(repositoryId, {
        state: filter,
        page: 1,
        page_size: PAGE_SIZE,
        provider,
      })
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setHasMore(res.items.length === PAGE_SIZE);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof Error
            ? err.message
            : "Failed to load pull requests."
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [repositoryId, provider, filter]);

  useEffect(() => {
    const cleanup = loadFirstPage();
    return cleanup;
  }, [loadFirstPage, reloadKey]);

  const loadMore = useCallback(() => {
    if (loading || loadingMore || !hasMore) return;
    let cancelled = false;
    setLoadingMore(true);
    const nextPage = page + 1;

    sourceControlClient
      .listPullRequests(repositoryId, {
        state: filter,
        page: nextPage,
        page_size: PAGE_SIZE,
        provider,
      })
      .then((res) => {
        if (cancelled) return;
        setItems((prev) => [...prev, ...res.items]);
        setPage(nextPage);
        setHasMore(res.items.length === PAGE_SIZE);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof Error
            ? err.message
            : "Failed to load more pull requests."
        );
      })
      .finally(() => {
        if (!cancelled) setLoadingMore(false);
      });

    return () => {
      cancelled = true;
    };
  }, [
    repositoryId,
    provider,
    filter,
    page,
    loading,
    loadingMore,
    hasMore,
  ]);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-card/40 shrink-0 gap-2">
        <div className="flex items-center gap-1 rounded-md bg-muted p-0.5 overflow-x-auto">
          {FILTER_OPTIONS.map((opt) => (
            <button
              key={opt.key}
              type="button"
              onClick={() => setFilter(opt.key)}
              className={`px-2 py-0.5 text-[11px] font-medium rounded transition-colors shrink-0 ${
                filter === opt.key
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          {onCreateNew && (
            <button
              type="button"
              onClick={onCreateNew}
              className="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-border hover:bg-muted"
              title="New pull request"
            >
              <Plus className="w-3 h-3" />
              New
            </button>
          )}
          <button
            type="button"
            onClick={() => setReloadKey((k) => k + 1)}
            disabled={loading}
            className="p-1 rounded hover:bg-muted disabled:opacity-50"
            title="Reload"
            aria-label="Reload"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 text-muted-foreground ${
                loading ? "animate-spin" : ""
              }`}
            />
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto">
        {loading && (
          <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Loading pull requests…
          </div>
        )}

        {!loading && error && (
          <div className="p-3 flex items-start gap-2 text-xs text-red-700">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {!loading && !error && items.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full p-8 text-center text-muted-foreground">
            <GitPullRequest className="w-10 h-10 mb-3 text-muted-foreground/50" />
            <h3 className="text-xs font-semibold text-foreground">
              No {filter === "all" ? "" : filter} pull requests
            </h3>
            <p className="text-[11px] mt-1 max-w-xs leading-relaxed">
              {onCreateNew
                ? "Open a new pull request to propose a change."
                : "Nothing to show for this filter."}
            </p>
          </div>
        )}

        {!loading && !error && items.length > 0 && (
          <ul className="divide-y divide-border">
            {items.map((pr) => (
              <li key={pr.number}>
                <button
                  type="button"
                  onClick={() => onSelect?.(pr.number)}
                  className="w-full text-left px-3 py-2.5 hover:bg-muted/30 flex items-start gap-2"
                >
                  <GitPullRequest
                    className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${prStateColor(
                      pr
                    )}`}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs font-medium text-foreground truncate">
                      {pr.title || "(no title)"}
                    </div>
                    <div className="text-[10px] text-muted-foreground mt-0.5 flex items-center gap-1.5 flex-wrap">
                      <code className="font-mono">#{pr.number}</code>
                      <StateBadge pr={pr} />
                      <span>·</span>
                      <span className="font-mono truncate max-w-[10em]">
                        {pr.source_branch}
                      </span>
                      <span>→</span>
                      <span className="font-mono truncate max-w-[10em]">
                        {pr.target_branch}
                      </span>
                      {pr.author_login && (
                        <>
                          <span>·</span>
                          <span>{pr.author_login}</span>
                        </>
                      )}
                      {pr.updated_at && (
                        <>
                          <span>·</span>
                          <span>{formatRelative(pr.updated_at)}</span>
                        </>
                      )}
                    </div>
                  </div>
                  <ChevronRight className="w-3.5 h-3.5 mt-0.5 shrink-0 text-muted-foreground" />
                </button>
              </li>
            ))}
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
              {loadingMore && <Loader2 className="w-3 h-3 animate-spin" />}
              Load more
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------

function isMerged(pr: PullRequestResponse): boolean {
  return pr.state === "merged" || pr.merged_at != null;
}

function StateBadge({ pr }: { pr: PullRequestResponse }) {
  const merged = isMerged(pr);
  const isDraft = pr.is_draft && pr.state === "open" && !merged;
  const isClosed = pr.state === "closed" && !merged;

  let label = "Open";
  let cls = "bg-green-100 text-green-700";
  if (isDraft) {
    label = "Draft";
    cls = "bg-gray-100 text-gray-700";
  } else if (merged) {
    label = "Merged";
    cls = "bg-purple-100 text-purple-700";
  } else if (isClosed) {
    label = "Closed";
    cls = "bg-red-100 text-red-700";
  }

  return (
    <span
      className={`text-[9px] px-1 py-0.5 rounded font-medium ${cls}`}
    >
      {label}
    </span>
  );
}

function prStateColor(pr: PullRequestResponse): string {
  if (isMerged(pr)) return "text-purple-500";
  if (pr.state === "closed") return "text-red-500";
  if (pr.is_draft) return "text-muted-foreground";
  return "text-green-600";
}

function formatRelative(iso: string): string {
  try {
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return iso;
    const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days}d ago`;
    return new Date(then).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
    });
  } catch {
    return iso;
  }
}
