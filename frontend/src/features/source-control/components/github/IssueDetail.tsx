// frontend/src/features/source-control/components/github/IssueDetail.tsx

import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  CircleDot,
  CircleSlash,
  ExternalLink,
  Loader2,
  MessageSquare,
  RefreshCw,
  Send,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type {
  IssueCommentResponse,
  IssueResponse,
} from "../../api/schemas";

export interface IssueDetailProps {
  repositoryId: string;
  number: string;
  provider?: string;
  onBack?: () => void;
  onStateChanged?: (issue: IssueResponse) => void;
}

/**
 * Detail view for a single issue.
 *
 * Sections (top-to-bottom):
 *   1. Header: number, state badge, title, external link, back button.
 *   2. Meta: author, labels, dates.
 *   3. Body: rendered as pre-formatted text (Markdown rendering is
 *      a future enhancement, same as PullRequestDetail).
 *   4. Comments thread: loaded alongside the issue.
 *   5. Comment box: visible for any state — commenting on a closed
 *      issue is a legitimate workflow on GitHub.
 *   6. Close/reopen action: a single button that toggles state.
 *
 * The issue and the comments are fetched in parallel. A failure in
 * one does not block the other.
 */
export function IssueDetail({
  repositoryId,
  number,
  provider = "github",
  onBack,
  onStateChanged,
}: IssueDetailProps) {
  const [issue, setIssue] = useState<IssueResponse | null>(null);
  const [issueLoading, setIssueLoading] = useState(true);
  const [issueError, setIssueError] = useState<string | null>(null);

  const [comments, setComments] = useState<IssueCommentResponse[]>([]);
  const [commentsLoading, setCommentsLoading] = useState(true);
  const [commentsError, setCommentsError] = useState<string | null>(
    null
  );

  const [stateChanging, setStateChanging] = useState(false);
  const [stateError, setStateError] = useState<string | null>(null);

  const [commentBody, setCommentBody] = useState("");
  const [commentSubmitting, setCommentSubmitting] = useState(false);
  const [commentError, setCommentError] = useState<string | null>(
    null
  );

  const loadIssue = useCallback(() => {
    let cancelled = false;
    setIssueLoading(true);
    setIssueError(null);
    sourceControlClient
      .getIssue(repositoryId, number, provider)
      .then((data) => {
        if (!cancelled) setIssue(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setIssueError(
            err instanceof Error
              ? err.message
              : "Failed to load issue."
          );
        }
      })
      .finally(() => {
        if (!cancelled) setIssueLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repositoryId, number, provider]);

  const loadComments = useCallback(() => {
    let cancelled = false;
    setCommentsLoading(true);
    setCommentsError(null);
    sourceControlClient
      .listIssueComments(repositoryId, number, provider)
      .then((data) => {
        if (!cancelled) setComments(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setCommentsError(
            err instanceof Error
              ? err.message
              : "Failed to load comments."
          );
        }
      })
      .finally(() => {
        if (!cancelled) setCommentsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repositoryId, number, provider]);

  useEffect(() => {
    const c1 = loadIssue();
    const c2 = loadComments();
    return () => {
      c1();
      c2();
    };
  }, [loadIssue, loadComments]);

  const refresh = () => {
    loadIssue();
    loadComments();
  };

  const toggleState = async () => {
    if (!issue) return;
    const nextState = issue.state === "open" ? "closed" : "open";
    setStateChanging(true);
    setStateError(null);
    try {
      const updated = await sourceControlClient.updateIssueState(
        repositoryId,
        number,
        {
          state: nextState,
          state_reason:
            nextState === "closed" ? "completed" : null,
        },
        provider
      );
      setIssue(updated);
      onStateChanged?.(updated);
    } catch (err) {
      setStateError(
        err instanceof Error
          ? err.message
          : "Failed to update issue state."
      );
    } finally {
      setStateChanging(false);
    }
  };

  const submitComment = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!commentBody.trim() || commentSubmitting) return;
    setCommentSubmitting(true);
    setCommentError(null);
    try {
      const created = await sourceControlClient.createIssueComment(
        repositoryId,
        number,
        { body: commentBody.trim() },
        provider
      );
      setComments((prev) => [...prev, created]);
      setCommentBody("");
      // Refresh the issue to update the comments_count badge.
      loadIssue();
    } catch (err) {
      setCommentError(
        err instanceof Error ? err.message : "Failed to post comment."
      );
    } finally {
      setCommentSubmitting(false);
    }
  };

  const isClosed = issue?.state === "closed";

  return (
    <div className="flex flex-col h-full bg-background">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-card/40 shrink-0 gap-2">
        <div className="flex items-center gap-2 min-w-0">
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              className="p-1 rounded hover:bg-muted shrink-0"
              title="Back to list"
              aria-label="Back to list"
            >
              <ArrowLeft className="w-3.5 h-3.5 text-muted-foreground" />
            </button>
          )}
          {isClosed ? (
            <CircleSlash className="w-3.5 h-3.5 text-purple-500 shrink-0" />
          ) : (
            <CircleDot className="w-3.5 h-3.5 text-green-600 shrink-0" />
          )}
          <span className="text-[10px] font-mono text-muted-foreground shrink-0">
            #{number}
          </span>
          {issue && (
            <>
              <StateBadge state={issue.state} />
              <span className="text-xs font-medium text-foreground truncate">
                {issue.title || "(no title)"}
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {issue?.web_url && (
            <a
              href={issue.web_url}
              target="_blank"
              rel="noopener noreferrer"
              className="p-1 rounded hover:bg-muted"
              title="Open on provider"
              aria-label="Open on provider"
            >
              <ExternalLink className="w-3.5 h-3.5 text-muted-foreground" />
            </a>
          )}
          <button
            type="button"
            onClick={refresh}
            disabled={issueLoading}
            className="p-1 rounded hover:bg-muted disabled:opacity-50"
            title="Reload"
            aria-label="Reload"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 text-muted-foreground ${
                issueLoading ? "animate-spin" : ""
              }`}
            />
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        {issueLoading && !issue && (
          <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Loading issue…
          </div>
        )}

        {!issueLoading && issueError && (
          <div className="p-3 flex items-start gap-2 text-xs text-red-700">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{issueError}</span>
          </div>
        )}

        {issue && (
          <div className="p-3 space-y-3">
            {/* Meta */}
            <div className="text-[11px] text-muted-foreground flex flex-wrap gap-x-3 gap-y-1">
              {issue.author_login && <span>by {issue.author_login}</span>}
              {issue.created_at && (
                <span>opened {formatDate(issue.created_at)}</span>
              )}
              {issue.closed_at && (
                <span>closed {formatDate(issue.closed_at)}</span>
              )}
            </div>

            {issue.labels.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {issue.labels.map((label) => (
                  <span
                    key={label}
                    className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-foreground"
                  >
                    {label}
                  </span>
                ))}
              </div>
            )}

            {/* Body */}
            {issue.body ? (
              <div className="rounded-md border border-border bg-card/40 p-3">
                <pre className="text-xs whitespace-pre-wrap break-words font-sans text-foreground">
                  {issue.body}
                </pre>
              </div>
            ) : (
              <div className="text-[11px] text-muted-foreground italic">
                No description provided.
              </div>
            )}

            {/* State action */}
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={toggleState}
                disabled={stateChanging}
                className={`text-[11px] px-2 py-1 rounded border inline-flex items-center gap-1 disabled:opacity-50 ${
                  isClosed
                    ? "border-green-300 bg-green-50 text-green-700 hover:bg-green-100"
                    : "border-purple-300 bg-purple-50 text-purple-700 hover:bg-purple-100"
                }`}
              >
                {stateChanging && (
                  <Loader2 className="w-3 h-3 animate-spin" />
                )}
                {isClosed ? "Reopen issue" : "Close issue"}
              </button>
              {stateError && (
                <span className="text-[10px] text-red-700">
                  {stateError}
                </span>
              )}
            </div>

            {/* Comments */}
            <div className="border-t border-border pt-2 space-y-2">
              <h4 className="text-[11px] font-semibold text-foreground flex items-center gap-1">
                <MessageSquare className="w-3 h-3" />
                Comments ({comments.length})
              </h4>

              {commentsLoading && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground p-2">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Loading comments…
                </div>
              )}

              {!commentsLoading && commentsError && (
                <div className="flex items-start gap-2 text-xs text-red-700 p-2">
                  <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                  <span>{commentsError}</span>
                </div>
              )}

              {!commentsLoading &&
                !commentsError &&
                comments.length === 0 && (
                  <div className="text-[11px] text-muted-foreground p-2 italic">
                    No comments yet.
                  </div>
                )}

              {!commentsLoading && !commentsError && comments.length > 0 && (
                <ul className="space-y-1.5">
                  {comments.map((c) => (
                    <li
                      key={c.comment_id}
                      className="rounded border border-border bg-card/30 p-2"
                    >
                      <div className="text-[10px] text-muted-foreground mb-1 flex items-center gap-1.5 flex-wrap">
                        {c.author_login && (
                          <span className="font-medium text-foreground">
                            {c.author_login}
                          </span>
                        )}
                        {c.created_at && <span>·</span>}
                        {c.created_at && (
                          <span>{formatDate(c.created_at)}</span>
                        )}
                      </div>
                      <pre className="text-xs whitespace-pre-wrap break-words font-sans text-foreground">
                        {c.body}
                      </pre>
                    </li>
                  ))}
                </ul>
              )}

              {/* Comment form */}
              <form
                onSubmit={submitComment}
                className="space-y-1.5 pt-1"
              >
                <textarea
                  value={commentBody}
                  onChange={(e) => setCommentBody(e.target.value)}
                  placeholder="Leave a comment"
                  rows={3}
                  maxLength={65_536}
                  disabled={commentSubmitting}
                  className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary resize-none"
                />
                {commentError && (
                  <div className="text-[10px] text-red-700">
                    {commentError}
                  </div>
                )}
                <div className="flex items-center justify-end">
                  <button
                    type="submit"
                    disabled={
                      commentSubmitting || !commentBody.trim()
                    }
                    className="text-[11px] px-2 py-1 rounded bg-primary text-primary-foreground disabled:opacity-50 inline-flex items-center gap-1"
                  >
                    {commentSubmitting ? (
                      <Loader2 className="w-3 h-3 animate-spin" />
                    ) : (
                      <Send className="w-3 h-3" />
                    )}
                    Comment
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function StateBadge({ state }: { state: string }) {
  const isOpen = state === "open";
  return (
    <span
      className={`text-[9px] px-1 py-0.5 rounded font-medium ${
        isOpen
          ? "bg-green-100 text-green-700"
          : "bg-purple-100 text-purple-700"
      }`}
    >
      {isOpen ? "Open" : "Closed"}
    </span>
  );
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
