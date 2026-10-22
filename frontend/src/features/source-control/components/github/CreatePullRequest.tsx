// frontend/src/features/source-control/components/github/CreatePullRequest.tsx

import React, { useEffect, useMemo, useState } from "react";
import { AlertCircle, ChevronDown, Loader2, X } from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type {
  BranchResponse,
  PullRequestResponse,
} from "../../api/schemas";

export interface CreatePullRequestProps {
  repositoryId: string;
  provider?: string;
  /** Optional pre-selected source branch (e.g. the current one). */
  defaultSourceBranch?: string;
  /** Optional pre-selected target branch. */
  defaultTargetBranch?: string;
  /** Called after a successful creation with the created PR. */
  onCreated?: (pr: PullRequestResponse) => void;
  /** Called when the user cancels the form. */
  onCancel?: () => void;
}

/**
 * Modal-style form for creating a new pull request.
 *
 * The form fetches the repository's branches once on mount so that
 * the source and target dropdowns are populated with real values.
 * Title and body are free-form; `draft` defaults to true, matching
 * the subsystem-wide policy that PELDRUN never opens a PR in
 * review-ready state without the user opting in.
 *
 * VALIDATION:
 *   - `source_branch` and `target_branch` MUST differ. The backend
 *     rejects the request with a 422 otherwise; the client
 *     prevents the round-trip by disabling submit until the two
 *     values differ.
 *   - `title` MUST be non-empty. The submit button is disabled
 *     while the title is empty or whitespace.
 */
export function CreatePullRequest({
  repositoryId,
  provider = "github",
  defaultSourceBranch,
  defaultTargetBranch,
  onCreated,
  onCancel,
}: CreatePullRequestProps) {
  const [branches, setBranches] = useState<BranchResponse[]>([]);
  const [branchesLoading, setBranchesLoading] = useState(true);
  const [branchesError, setBranchesError] = useState<string | null>(null);

  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [sourceBranch, setSourceBranch] = useState(
    defaultSourceBranch ?? ""
  );
  const [targetBranch, setTargetBranch] = useState(
    defaultTargetBranch ?? ""
  );
  const [draft, setDraft] = useState(true);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Load branches.
  useEffect(() => {
    let cancelled = false;
    setBranchesLoading(true);
    setBranchesError(null);
    sourceControlClient
      .listBranches(repositoryId, provider, 100)
      .then((data) => {
        if (cancelled) return;
        setBranches(data);
        // Auto-pick the default branch as target when none was
        // provided. This matches the most common workflow (PR into
        // main) without forcing the user to select it manually.
        if (!targetBranch) {
          const def = data.find((b) => b.is_default);
          if (def) setTargetBranch(def.name);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setBranchesError(
          err instanceof Error
            ? err.message
            : "Failed to load branches."
        );
      })
      .finally(() => {
        if (!cancelled) setBranchesLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // `targetBranch` is intentionally excluded: this effect only
    // runs on mount / when the repository or provider changes. The
    // auto-pick is a best-effort convenience, not a reactive
    // dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repositoryId, provider]);

  const canSubmit = useMemo(() => {
    return (
      title.trim().length > 0 &&
      sourceBranch.length > 0 &&
      targetBranch.length > 0 &&
      sourceBranch !== targetBranch &&
      !submitting &&
      !branchesLoading
    );
  }, [
    title,
    sourceBranch,
    targetBranch,
    submitting,
    branchesLoading,
  ]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const pr = await sourceControlClient.createPullRequest(
        repositoryId,
        {
          source_branch: sourceBranch,
          target_branch: targetBranch,
          title: title.trim(),
          body,
          draft,
          provider,
        }
      );
      onCreated?.(pr);
    } catch (err) {
      setSubmitError(
        err instanceof Error
          ? err.message
          : "Failed to create pull request."
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
          New pull request
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
        {branchesError && (
          <div className="flex items-start gap-2 text-xs text-red-700 p-2 border border-red-200 bg-red-50 rounded">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{branchesError}</span>
          </div>
        )}

        {/* Branch selectors */}
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="block text-[10px] font-medium text-muted-foreground mb-1">
              Source branch
            </label>
            <BranchSelect
              value={sourceBranch}
              onChange={setSourceBranch}
              branches={branches}
              loading={branchesLoading}
              disabled={submitting}
              placeholder="Select source"
            />
          </div>
          <div>
            <label className="block text-[10px] font-medium text-muted-foreground mb-1">
              Target branch
            </label>
            <BranchSelect
              value={targetBranch}
              onChange={setTargetBranch}
              branches={branches}
              loading={branchesLoading}
              disabled={submitting}
              placeholder="Select target"
            />
          </div>
        </div>

        {sourceBranch && targetBranch && sourceBranch === targetBranch && (
          <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
            Source and target branches must differ.
          </div>
        )}

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
            placeholder="Add a short title"
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
            placeholder="Describe the change. Markdown is supported."
            rows={8}
            maxLength={65_536}
            className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary resize-none font-mono"
          />
        </div>

        {/* Draft checkbox */}
        <label className="flex items-center gap-2 text-xs text-foreground cursor-pointer select-none">
          <input
            type="checkbox"
            checked={draft}
            onChange={(e) => setDraft(e.target.checked)}
            disabled={submitting}
            className="rounded border-border"
          />
          Create as draft
          <span className="text-[10px] text-muted-foreground">
            (draft PRs are not ready for review)
          </span>
        </label>

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
          Create pull request
        </button>
      </div>
    </form>
  );
}

// ----------------------------------------------------------------------
// Branch selector (native select styled to match the form)
// ----------------------------------------------------------------------

function BranchSelect({
  value,
  onChange,
  branches,
  loading,
  disabled,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  branches: BranchResponse[];
  loading: boolean;
  disabled: boolean;
  placeholder: string;
}) {
  return (
    <div className="relative">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled || loading}
        className="w-full text-xs pl-2 pr-6 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary appearance-none font-mono"
      >
        <option value="">{loading ? "Loading…" : placeholder}</option>
        {branches.map((b) => (
          <option key={b.name} value={b.name}>
            {b.name}
            {b.is_default ? " (default)" : ""}
          </option>
        ))}
      </select>
      <ChevronDown className="absolute right-1.5 top-1/2 -translate-y-1/2 w-3 h-3 text-muted-foreground pointer-events-none" />
    </div>
  );
}
