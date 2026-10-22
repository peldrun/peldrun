// frontend/src/features/source-control/components/git/MergeDialog.tsx

import React, { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ChevronDown,
  GitMerge,
  Loader2,
  X,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type {
  BranchResponse,
  MergeOperationResponse,
} from "../../api/schemas";

export interface MergeDialogProps {
  workspaceId: string;
  branches: BranchResponse[];
  /** The workspace's current branch (used as the default target). */
  currentBranch: string;
  onCancel: () => void;
  /** Called after the merge attempt with the operation outcome. */
  onResult: (outcome: MergeOperationResponse) => void;
}

type Strategy = "merge" | "squash" | "ff-only";

const STRATEGIES: { key: Strategy; label: string; hint: string }[] = [
  {
    key: "merge",
    label: "Create a merge commit",
    hint: "Always creates a merge commit, preserving branch topology.",
  },
  {
    key: "squash",
    label: "Squash and merge",
    hint: "Combines all commits into a single commit on the target.",
  },
  {
    key: "ff-only",
    label: "Fast-forward only",
    hint: "Refuses if a fast-forward is not possible.",
  },
];

/**
 * Modal dialog for merging one branch into another inside the
 * workspace.
 *
 * The dialog is self-contained: it does not fetch the branch list
 * (the parent already has it) and it does not run the merge until
 * the user confirms. On confirmation, it calls the backend and
 * forwards the outcome to `onResult`. A "conflicted" outcome is
 * NOT an error: the caller is expected to open the conflict
 * resolver with the same workspace id.
 */
export function MergeDialog({
  workspaceId,
  branches,
  currentBranch,
  onCancel,
  onResult,
}: MergeDialogProps) {
  // Default source: the current branch (if it is not the default).
  // Default target: the default branch. If the current branch IS
  // the default, pick the first non-default branch as source.
  const defaultBranch = useMemo(
    () => branches.find((b) => b.is_default)?.name ?? "main",
    [branches]
  );

  const initialSource = useMemo(() => {
    if (currentBranch && currentBranch !== defaultBranch) {
      return currentBranch;
    }
    const nonDefault = branches.find((b) => !b.is_default);
    return nonDefault?.name ?? "";
  }, [currentBranch, defaultBranch, branches]);

  const [sourceBranch, setSourceBranch] = useState(initialSource);
  const [targetBranch, setTargetBranch] = useState(defaultBranch);
  const [strategy, setStrategy] = useState<Strategy>("merge");
  const [commitMessage, setCommitMessage] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Sync defaults if the branches list updates after mount (e.g.
  // the parent re-fetched branches while the dialog was open).
  useEffect(() => {
    if (!targetBranch) setTargetBranch(defaultBranch);
  }, [defaultBranch, targetBranch]);

  const canSubmit = useMemo(() => {
    return (
      sourceBranch.length > 0 &&
      targetBranch.length > 0 &&
      sourceBranch !== targetBranch &&
      !submitting
    );
  }, [sourceBranch, targetBranch, submitting]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const outcome = await sourceControlClient.mergeWorkspaceBranches(
        workspaceId,
        {
          source_branch: sourceBranch,
          target_branch: targetBranch,
          strategy,
          commit_message: commitMessage.trim() || null,
        }
      );
      onResult(outcome);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Merge failed."
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="merge-dialog-title"
    >
      <form
        onSubmit={handleSubmit}
        className="bg-background rounded-lg shadow-xl w-full max-w-md flex flex-col overflow-hidden border border-border"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-card/40">
          <h3
            id="merge-dialog-title"
            className="text-sm font-semibold text-foreground flex items-center gap-1.5"
          >
            <GitMerge className="w-4 h-4 text-purple-500" />
            Merge branches
          </h3>
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="p-1 rounded hover:bg-muted disabled:opacity-50"
            title="Cancel"
            aria-label="Cancel"
          >
            <X className="w-4 h-4 text-muted-foreground" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-3 max-h-[60vh]">
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-[10px] font-medium text-muted-foreground mb-1">
                From (source)
              </label>
              <BranchSelect
                value={sourceBranch}
                onChange={setSourceBranch}
                branches={branches}
                disabled={submitting}
                placeholder="Select source"
              />
            </div>
            <div>
              <label className="block text-[10px] font-medium text-muted-foreground mb-1">
                Into (target)
              </label>
              <BranchSelect
                value={targetBranch}
                onChange={setTargetBranch}
                branches={branches}
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

          <div>
            <label className="block text-[10px] font-medium text-muted-foreground mb-1">
              Strategy
            </label>
            <div className="space-y-1">
              {STRATEGIES.map((opt) => (
                <label
                  key={opt.key}
                  className={`flex items-start gap-2 px-2 py-1.5 rounded border cursor-pointer text-[11px] transition-colors ${
                    strategy === opt.key
                      ? "border-primary bg-primary/5"
                      : "border-border hover:bg-muted/40"
                  }`}
                >
                  <input
                    type="radio"
                    name="merge-strategy"
                    value={opt.key}
                    checked={strategy === opt.key}
                    onChange={() => setStrategy(opt.key)}
                    disabled={submitting}
                    className="mt-0.5"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-foreground">
                      {opt.label}
                    </div>
                    <div className="text-[10px] text-muted-foreground">
                      {opt.hint}
                    </div>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {strategy !== "ff-only" && (
            <div>
              <label className="block text-[10px] font-medium text-muted-foreground mb-1">
                Commit message (optional)
              </label>
              <textarea
                value={commitMessage}
                onChange={(e) => setCommitMessage(e.target.value)}
                disabled={submitting}
                rows={2}
                maxLength={65_536}
                placeholder="Leave blank to use Git's default"
                className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary resize-none"
              />
            </div>
          )}

          {error && (
            <div className="flex items-start gap-2 text-[11px] text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1.5">
              <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-4 py-2.5 border-t border-border bg-card/40">
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="text-xs px-3 py-1.5 rounded border border-border hover:bg-muted disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={!canSubmit}
            className="text-xs px-3 py-1.5 rounded bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            {submitting ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <GitMerge className="w-3 h-3" />
            )}
            Merge
          </button>
        </div>
      </form>
    </div>
  );
}

// ----------------------------------------------------------------------
// Branch selector
// ----------------------------------------------------------------------

function BranchSelect({
  value,
  onChange,
  branches,
  disabled,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  branches: BranchResponse[];
  disabled: boolean;
  placeholder: string;
}) {
  return (
    <div className="relative">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className="w-full text-xs pl-2 pr-6 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary appearance-none font-mono"
      >
        <option value="">{placeholder}</option>
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
