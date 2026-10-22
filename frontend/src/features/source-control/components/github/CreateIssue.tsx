// frontend/src/features/source-control/components/github/CreateIssue.tsx

import React, { useMemo, useState } from "react";
import { AlertCircle, Loader2, X } from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type { IssueResponse } from "../../api/schemas";

export interface CreateIssueProps {
  repositoryId: string;
  provider?: string;
  /** Called after a successful creation with the created issue. */
  onCreated?: (issue: IssueResponse) => void;
  /** Called when the user cancels the form. */
  onCancel?: () => void;
}

/**
 * Modal-style form for creating a new issue.
 *
 * Labels and assignees are collected as comma-separated strings
 * rather than as a multi-select widget: the vast majority of
 * repositories in the target audience have a short, well-known
 * label set (bug, feature, docs, ...), and a comma-separated input
 * is both simpler to build and faster to use than a picker. The
 * backend clamps both lists at 20/10 entries respectively.
 *
 * The submit button is disabled while the title is empty or
 * whitespace. `body` is optional; GitHub accepts an issue with an
 * empty body.
 */
export function CreateIssue({
  repositoryId,
  provider = "github",
  onCreated,
  onCancel,
}: CreateIssueProps) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [labelsRaw, setLabelsRaw] = useState("");
  const [assigneesRaw, setAssigneesRaw] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const labels = useMemo(
    () =>
      labelsRaw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    [labelsRaw]
  );

  const assignees = useMemo(
    () =>
      assigneesRaw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    [assigneesRaw]
  );

  const labelsTooMany = labels.length > 20;
  const assigneesTooMany = assignees.length > 10;

  const canSubmit = useMemo(
    () =>
      title.trim().length > 0 &&
      !submitting &&
      !labelsTooMany &&
      !assigneesTooMany,
    [title, submitting, labelsTooMany, assigneesTooMany]
  );

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const issue = await sourceControlClient.createIssue(repositoryId, {
        title: title.trim(),
        body,
        labels,
        assignees,
        provider,
      });
      onCreated?.(issue);
    } catch (err) {
      setSubmitError(
        err instanceof Error
          ? err.message
          : "Failed to create issue."
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col h-full bg-background"
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-card/40 shrink-0">
        <h3 className="text-xs font-semibold text-foreground">
          New issue
        </h3>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="p-1 rounded hover:bg-muted disabled:opacity-50"
            title="Cancel"
            aria-label="Cancel"
          >
            <X className="w-3.5 h-3.5 text-muted-foreground" />
          </button>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {/* Title */}
        <div>
          <label className="block text-[10px] font-medium text-muted-foreground mb-1">
            Title <span className="text-red-600">*</span>
          </label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            disabled={submitting}
            placeholder="Short, descriptive title"
            maxLength={256}
            className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>

        {/* Body */}
        <div>
          <label className="block text-[10px] font-medium text-muted-foreground mb-1">
            Description (Markdown)
          </label>
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            disabled={submitting}
            placeholder="What happened? What did you expect?"
            rows={8}
            maxLength={65_536}
            className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary resize-none font-mono"
          />
        </div>

        {/* Labels */}
        <div>
          <label className="block text-[10px] font-medium text-muted-foreground mb-1">
            Labels{" "}
            <span className="text-muted-foreground/70">
              (comma-separated, max 20)
            </span>
          </label>
          <input
            type="text"
            value={labelsRaw}
            onChange={(e) => setLabelsRaw(e.target.value)}
            disabled={submitting}
            placeholder="bug, enhancement"
            className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary font-mono"
          />
          {labelsTooMany && (
            <div className="text-[10px] text-red-700 mt-1">
              Too many labels ({labels.length}). Maximum is 20.
            </div>
          )}
        </div>

        {/* Assignees */}
        <div>
          <label className="block text-[10px] font-medium text-muted-foreground mb-1">
            Assignees{" "}
            <span className="text-muted-foreground/70">
              (comma-separated logins, max 10)
            </span>
          </label>
          <input
            type="text"
            value={assigneesRaw}
            onChange={(e) => setAssigneesRaw(e.target.value)}
            disabled={submitting}
            placeholder="octocat, hubot"
            className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary font-mono"
          />
          {assigneesTooMany && (
            <div className="text-[10px] text-red-700 mt-1">
              Too many assignees ({assignees.length}). Maximum is 10.
            </div>
          )}
        </div>

        {submitError && (
          <div className="flex items-start gap-2 text-xs text-red-700 p-2 border border-red-200 bg-red-50 rounded">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{submitError}</span>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="flex items-center justify-end gap-2 px-4 py-2 border-t border-border bg-card/40 shrink-0">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="text-xs px-3 py-1.5 rounded border border-border hover:bg-muted disabled:opacity-50"
          >
            Cancel
          </button>
        )}
        <button
          type="submit"
          disabled={!canSubmit}
          className="text-xs px-3 py-1.5 rounded bg-primary text-primary-foreground disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          {submitting && <Loader2 className="w-3 h-3 animate-spin" />}
          Create issue
        </button>
      </div>
    </form>
  );
}
