// frontend/src/features/source-control/components/github/PullRequestDetail.tsx

import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  ChevronDown,
  ExternalLink,
  GitMerge,
  GitPullRequest,
  Loader2,
  MessageSquare,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type {
  PullRequestCommentResponse,
  PullRequestResponse,
  PullRequestReviewResponse,
  PullRequestTimelineResponse,
} from "../../api/schemas";

export interface PullRequestDetailProps {
  repositoryId: string;
  number: string;
  provider?: string;
  /** Called when the user clicks the back button. */
  onBack?: () => void;
  /** Called after a successful merge, with the refreshed PR. */
  onMerged?: (pr: PullRequestResponse) => void;
}

type DetailTab = "conversation" | "reviews";

type MergeMethod = "merge" | "squash" | "rebase";

/**
 * Detail view for a single pull request.
 *
 * Layout (top-to-bottom):
 *   1. Header: title, state badge, author, branches, external
 *      "Open on GitHub" link, and a back button.
 *   2. Body card: the PR description rendered as preformatted text.
 *      Markdown rendering is intentionally deferred to a later
 *      revision; the pre-formatted text preserves structure without
 *      pulling in a Markdown dependency.
 *   3. Merge box: visible only when the PR is open. Provides a
 *      merge-method selector, an optional delete-source-branch
 *      checkbox, and a merge button.
 *   4. Review box: visible only when the PR is open. Allows
 *      APPROVE / REQUEST_CHANGES / COMMENT with an optional body.
 *   5. Timeline section: two sub-tabs — "Conversation" (general
 *      comments) and "Reviews" (review submissions plus inline
 *      comments rendered grouped by file/line).
 *
 * LOADING MODEL:
 *   The PR, the timeline (reviews + comments), and any in-flight
 *   merge/review action are tracked independently. A failed
 *   timeline fetch does not hide the PR header/body (which are
 *   already in memory); a failed merge does not clear the
 *   conversation. Each failure surfaces as an inline banner above
 *   the section that failed.
 *
 * MERGE SAFETY:
 *   The merge endpoint accepts an optional `expected_head_sha`
 *   which the backend uses to detect a head that moved between
 *   load and merge. This view does NOT set it by default: the
 *   refreshed PR's current head SHA is not necessarily the one the
 *   user reviewed, and inferring it from the initial load would
 *   produce false positives whenever the PR received a legitimate
 *   push. A future revision may add an explicit "Review and merge"
 *   flow that captures the SHA at the moment the user opens the
 *   merge box.
 */
export function PullRequestDetail({
  repositoryId,
  number,
  provider = "github",
  onBack,
  onMerged,
}: PullRequestDetailProps) {
  const [pr, setPr] = useState<PullRequestResponse | null>(null);
  const [prLoading, setPrLoading] = useState(true);
  const [prError, setPrError] = useState<string | null>(null);

  const [timeline, setTimeline] =
    useState<PullRequestTimelineResponse | null>(null);
  const [timelineLoading, setTimelineLoading] = useState(true);
  const [timelineError, setTimelineError] = useState<string | null>(null);

  const [detailTab, setDetailTab] = useState<DetailTab>("conversation");

  // --- Load PR ---
  const loadPr = useCallback(() => {
    let cancelled = false;
    setPrLoading(true);
    setPrError(null);
    sourceControlClient
      .getPullRequest(repositoryId, number, provider)
      .then((data) => {
        if (!cancelled) setPr(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setPrError(
            err instanceof Error
              ? err.message
              : "Failed to load pull request."
          );
        }
      })
      .finally(() => {
        if (!cancelled) setPrLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repositoryId, number, provider]);

  // --- Load timeline ---
  const loadTimeline = useCallback(() => {
    let cancelled = false;
    setTimelineLoading(true);
    setTimelineError(null);
    sourceControlClient
      .getPullRequestTimeline(repositoryId, number, provider)
      .then((data) => {
        if (!cancelled) setTimeline(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setTimelineError(
            err instanceof Error
              ? err.message
              : "Failed to load reviews and comments."
          );
        }
      })
      .finally(() => {
        if (!cancelled) setTimelineLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repositoryId, number, provider]);

  useEffect(() => {
    const c1 = loadPr();
    const c2 = loadTimeline();
    return () => {
      c1();
      c2();
    };
  }, [loadPr, loadTimeline]);

  const refresh = () => {
    loadPr();
    loadTimeline();
  };

  const isOpen = !!pr && pr.state !== "closed" && !pr.merged_at;
  const isMerged = !!pr && (pr.state === "merged" || pr.merged_at != null);
  const isDraft = !!pr && pr.is_draft && isOpen;

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
          <GitPullRequest className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
          <span className="text-[10px] font-mono text-muted-foreground shrink-0">
            #{number}
          </span>
          {pr && (
            <>
              <StateBadge
                state={pr.state}
                isMerged={isMerged}
                isDraft={isDraft}
              />
              <span className="text-xs font-medium text-foreground truncate">
                {pr.title || "(no title)"}
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {pr?.web_url && (
            <a
              href={pr.web_url}
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
            disabled={prLoading}
            className="p-1 rounded hover:bg-muted disabled:opacity-50"
            title="Reload"
            aria-label="Reload"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 text-muted-foreground ${
                prLoading ? "animate-spin" : ""
              }`}
            />
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        {prLoading && !pr && (
          <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Loading pull request…
          </div>
        )}

        {!prLoading && prError && (
          <div className="p-3 flex items-start gap-2 text-xs text-red-700">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{prError}</span>
          </div>
        )}

        {pr && (
          <div className="p-3 space-y-3">
            {/* Meta card */}
            <div className="text-[11px] text-muted-foreground flex flex-wrap gap-x-3 gap-y-1">
              {pr.author_login && <span>by {pr.author_login}</span>}
              <span>
                <code className="font-mono">{pr.source_branch}</code>
                {" → "}
                <code className="font-mono">{pr.target_branch}</code>
              </span>
              {pr.created_at && (
                <span>opened {formatDate(pr.created_at)}</span>
              )}
              {pr.merged_at && (
                <span>merged {formatDate(pr.merged_at)}</span>
              )}
            </div>

            {/* Description */}
            {pr.body && (
              <div className="rounded-md border border-border bg-card/40 p-3">
                <pre className="text-xs whitespace-pre-wrap break-words font-sans text-foreground">
                  {pr.body}
                </pre>
              </div>
            )}

            {/* Merge box */}
            {isOpen && (
              <MergeBox
                repositoryId={repositoryId}
                number={number}
                provider={provider}
                sourceBranch={pr.source_branch}
                onMerged={(updated) => {
                  setPr(updated);
                  loadTimeline();
                  onMerged?.(updated);
                }}
              />
            )}

            {/* Review box */}
            {isOpen && (
              <ReviewBox
                repositoryId={repositoryId}
                number={number}
                provider={provider}
                onSubmitted={loadTimeline}
              />
            )}

            {/* Timeline */}
            <div className="border-t border-border pt-2">
              <div className="flex items-center gap-1 mb-2">
                <TimelineTab
                  active={detailTab === "conversation"}
                  onClick={() => setDetailTab("conversation")}
                  icon={<MessageSquare className="w-3 h-3" />}
                  label={`Conversation (${timeline?.general_comments.length ?? 0})`}
                />
                <TimelineTab
                  active={detailTab === "reviews"}
                  onClick={() => setDetailTab("reviews")}
                  icon={<Check className="w-3 h-3" />}
                  label={`Reviews (${timeline?.reviews.length ?? 0})`}
                />
              </div>

              {timelineLoading && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground p-2">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Loading timeline…
                </div>
              )}

              {!timelineLoading && timelineError && (
                <div className="flex items-start gap-2 text-xs text-red-700 p-2">
                  <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                  <span>{timelineError}</span>
                </div>
              )}

              {!timelineLoading &&
                !timelineError &&
                timeline &&
                detailTab === "conversation" && (
                  <CommentList
                    comments={timeline.general_comments}
                    emptyLabel="No comments yet."
                  />
                )}

              {!timelineLoading &&
                !timelineError &&
                timeline &&
                detailTab === "reviews" && (
                  <ReviewsList
                    reviews={timeline.reviews}
                    inlineComments={timeline.inline_comments}
                  />
                )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Merge box
// ----------------------------------------------------------------------

function MergeBox({
  repositoryId,
  number,
  provider,
  sourceBranch,
  onMerged,
}: {
  repositoryId: string;
  number: string;
  provider: string;
  sourceBranch: string;
  onMerged: (pr: PullRequestResponse) => void;
}) {
  const [method, setMethod] = useState<MergeMethod>("merge");
  const [deleteBranch, setDeleteBranch] = useState(false);
  const [merging, setMerging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const handleMerge = async () => {
    setMerging(true);
    setError(null);
    setResult(null);
    try {
      const res = await sourceControlClient.mergePullRequest(
        repositoryId,
        number,
        {
          merge_method: method,
          delete_source_branch: deleteBranch,
        },
        provider
      );
      if (res.merged) {
        setResult(
          res.sha
            ? `Merged as ${res.sha.slice(0, 8)}.`
            : "Merged."
        );
        // Re-fetch to reflect the new state.
        try {
          const updated = await sourceControlClient.getPullRequest(
            repositoryId,
            number,
            provider
          );
          onMerged(updated);
        } catch {
          // If re-fetch fails, we still report success; the user
          // can hit refresh manually.
        }
      } else {
        setError(res.message || "Merge was not applied.");
      }
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Merge failed."
      );
    } finally {
      setMerging(false);
    }
  };

  return (
    <div className="rounded-md border border-border bg-card/40 p-3 space-y-2">
      <div className="flex items-center gap-1.5">
        <GitMerge className="w-3.5 h-3.5 text-purple-500" />
        <h4 className="text-xs font-semibold text-foreground">Merge</h4>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <label className="text-[10px] text-muted-foreground">
          Method:
        </label>
        <div className="relative">
          <select
            value={method}
            onChange={(e) => setMethod(e.target.value as MergeMethod)}
            disabled={merging}
            className="text-[11px] pl-2 pr-6 py-1 rounded border border-border bg-background appearance-none focus:outline-none focus:ring-1 focus:ring-primary"
          >
            <option value="merge">Merge commit</option>
            <option value="squash">Squash and merge</option>
            <option value="rebase">Rebase and merge</option>
          </select>
          <ChevronDown className="absolute right-1.5 top-1/2 -translate-y-1/2 w-3 h-3 text-muted-foreground pointer-events-none" />
        </div>
      </div>

      <label className="flex items-center gap-2 text-[11px] text-foreground cursor-pointer select-none">
        <input
          type="checkbox"
          checked={deleteBranch}
          onChange={(e) => setDeleteBranch(e.target.checked)}
          disabled={merging}
          className="rounded border-border"
        />
        Delete source branch{" "}
        <code className="font-mono text-muted-foreground">
          {sourceBranch}
        </code>{" "}
        after merge
      </label>

      {error && (
        <div className="flex items-start gap-1.5 text-[11px] text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1">
          <AlertCircle className="w-3 h-3 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}
      {result && (
        <div className="flex items-start gap-1.5 text-[11px] text-green-700 bg-green-50 border border-green-200 rounded px-2 py-1">
          <Check className="w-3 h-3 shrink-0 mt-0.5" />
          <span>{result}</span>
        </div>
      )}

      <button
        type="button"
        onClick={handleMerge}
        disabled={merging}
        className="w-full text-xs px-3 py-1.5 rounded bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-50 inline-flex items-center justify-center gap-1.5"
      >
        {merging ? (
          <Loader2 className="w-3 h-3 animate-spin" />
        ) : (
          <GitMerge className="w-3 h-3" />
        )}
        Merge pull request
      </button>
    </div>
  );
}

// ----------------------------------------------------------------------
// Review box
// ----------------------------------------------------------------------

function ReviewBox({
  repositoryId,
  number,
  provider,
  onSubmitted,
}: {
  repositoryId: string;
  number: string;
  provider: string;
  onSubmitted: () => void;
}) {
  const [body, setBody] = useState("");
  const [submitting, setSubmitting] = useState<
    "APPROVE" | "REQUEST_CHANGES" | "COMMENT" | null
  >(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const submit = async (
    event: "APPROVE" | "REQUEST_CHANGES" | "COMMENT"
  ) => {
    // REQUEST_CHANGES and COMMENT require a body; APPROVE does not.
    if (
      (event === "REQUEST_CHANGES" || event === "COMMENT") &&
      !body.trim()
    ) {
      setError(
        event === "REQUEST_CHANGES"
          ? "Requesting changes requires a comment."
          : "A comment body is required."
      );
      return;
    }
    setSubmitting(event);
    setError(null);
    setSuccess(null);
    try {
      await sourceControlClient.submitPullRequestReview(
        repositoryId,
        number,
        {
          event,
          body: body.trim() || null,
          comments: [],
        },
        provider
      );
      setBody("");
      setSuccess(
        event === "APPROVE"
          ? "Approved."
          : event === "REQUEST_CHANGES"
          ? "Changes requested."
          : "Comment posted."
      );
      onSubmitted();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to submit review."
      );
    } finally {
      setSubmitting(null);
    }
  };

  return (
    <div className="rounded-md border border-border bg-card/40 p-3 space-y-2">
      <h4 className="text-xs font-semibold text-foreground">Review</h4>
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Leave a comment (required for Request changes and Comment)."
        rows={3}
        maxLength={65_536}
        disabled={!!submitting}
        className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary resize-none"
      />
      {error && (
        <div className="flex items-start gap-1.5 text-[11px] text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1">
          <AlertCircle className="w-3 h-3 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}
      {success && (
        <div className="flex items-start gap-1.5 text-[11px] text-green-700 bg-green-50 border border-green-200 rounded px-2 py-1">
          <Check className="w-3 h-3 shrink-0 mt-0.5" />
          <span>{success}</span>
        </div>
      )}
      <div className="flex items-center gap-1.5 flex-wrap">
        <button
          type="button"
          onClick={() => submit("APPROVE")}
          disabled={!!submitting}
          className="text-[11px] px-2 py-1 rounded border border-green-300 bg-green-50 text-green-700 hover:bg-green-100 disabled:opacity-50 inline-flex items-center gap-1"
        >
          {submitting === "APPROVE" ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : (
            <Check className="w-3 h-3" />
          )}
          Approve
        </button>
        <button
          type="button"
          onClick={() => submit("REQUEST_CHANGES")}
          disabled={!!submitting}
          className="text-[11px] px-2 py-1 rounded border border-red-300 bg-red-50 text-red-700 hover:bg-red-100 disabled:opacity-50 inline-flex items-center gap-1"
        >
          {submitting === "REQUEST_CHANGES" ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : (
            <XCircle className="w-3 h-3" />
          )}
          Request changes
        </button>
        <button
          type="button"
          onClick={() => submit("COMMENT")}
          disabled={!!submitting}
          className="text-[11px] px-2 py-1 rounded border border-border bg-background hover:bg-muted disabled:opacity-50 inline-flex items-center gap-1"
        >
          {submitting === "COMMENT" ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : (
            <MessageSquare className="w-3 h-3" />
          )}
          Comment
        </button>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Timeline sub-components
// ----------------------------------------------------------------------

function CommentList({
  comments,
  emptyLabel,
}: {
  comments: PullRequestCommentResponse[];
  emptyLabel: string;
}) {
  if (comments.length === 0) {
    return (
      <div className="text-[11px] text-muted-foreground p-2 italic">
        {emptyLabel}
      </div>
    );
  }
  return (
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
            {c.created_at && <span>{formatDate(c.created_at)}</span>}
            {c.path && (
              <>
                <span>·</span>
                <code className="font-mono text-primary">
                  {c.path}
                  {c.line ? `:${c.line}` : ""}
                </code>
              </>
            )}
          </div>
          <pre className="text-xs whitespace-pre-wrap break-words font-sans text-foreground">
            {c.body}
          </pre>
        </li>
      ))}
    </ul>
  );
}

function ReviewsList({
  reviews,
  inlineComments,
}: {
  reviews: PullRequestReviewResponse[];
  inlineComments: PullRequestCommentResponse[];
}) {
  if (reviews.length === 0 && inlineComments.length === 0) {
    return (
      <div className="text-[11px] text-muted-foreground p-2 italic">
        No reviews yet.
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {reviews.length > 0 && (
        <div>
          <h5 className="text-[10px] font-semibold text-muted-foreground uppercase mb-1">
            Reviews
          </h5>
          <ul className="space-y-1.5">
            {reviews.map((r) => (
              <li
                key={r.review_id}
                className="rounded border border-border bg-card/30 p-2"
              >
                <div className="text-[10px] text-muted-foreground flex items-center gap-1.5 flex-wrap">
                  {r.author_login && (
                    <span className="font-medium text-foreground">
                      {r.author_login}
                    </span>
                  )}
                  <ReviewStateBadge state={r.state} />
                  {r.submitted_at && (
                    <>
                      <span>·</span>
                      <span>{formatDate(r.submitted_at)}</span>
                    </>
                  )}
                </div>
                {r.body && (
                  <pre className="text-xs whitespace-pre-wrap break-words font-sans text-foreground mt-1">
                    {r.body}
                  </pre>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {inlineComments.length > 0 && (
        <div>
          <h5 className="text-[10px] font-semibold text-muted-foreground uppercase mb-1">
            Inline comments
          </h5>
          <CommentList comments={inlineComments} emptyLabel="No inline comments." />
        </div>
      )}
    </div>
  );
}

function ReviewStateBadge({ state }: { state: string }) {
  const lower = state.toLowerCase();
  let label = state || "unknown";
  let cls = "bg-gray-100 text-gray-700";
  if (lower === "approved") {
    label = "Approved";
    cls = "bg-green-100 text-green-700";
  } else if (lower === "changes_requested") {
    label = "Changes requested";
    cls = "bg-red-100 text-red-700";
  } else if (lower === "commented") {
    label = "Commented";
    cls = "bg-blue-100 text-blue-700";
  } else if (lower === "dismissed") {
    label = "Dismissed";
    cls = "bg-gray-100 text-gray-700";
  }
  return (
    <span
      className={`text-[9px] px-1 py-0.5 rounded font-medium ${cls}`}
    >
      {label}
    </span>
  );
}

// ----------------------------------------------------------------------
// Shared helpers
// ----------------------------------------------------------------------

function StateBadge({
  state,
  isMerged,
  isDraft,
}: {
  state: string;
  isMerged: boolean;
  isDraft: boolean;
}) {
  let label = "Open";
  let cls = "bg-green-100 text-green-700";
  if (isDraft) {
    label = "Draft";
    cls = "bg-gray-100 text-gray-700";
  } else if (isMerged) {
    label = "Merged";
    cls = "bg-purple-100 text-purple-700";
  } else if (state === "closed") {
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

function TimelineTab({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1 px-2 py-1 rounded text-[11px] font-medium transition-colors ${
        active
          ? "bg-primary/10 text-primary"
          : "text-muted-foreground hover:bg-muted"
      }`}
    >
      {icon}
      {label}
    </button>
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
