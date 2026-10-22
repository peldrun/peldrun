// frontend/src/features/source-control/components/git/BranchManager.tsx

import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  FileWarning,
  GitBranch,
  GitMerge,
  Loader2,
  Plus,
  RefreshCw,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type {
  BranchResponse,
  ConflictListResponse,
  WorkspaceResponse,
} from "../../api/schemas";
import { MergeDialog } from "./MergeDialog";
import { ConflictResolver } from "./ConflictResolver";

export interface BranchManagerProps {
  workspaceId: string;
  repositoryId: string;
  provider?: string;
  /** The workspace's current branch, kept in sync by the parent. */
  currentBranch: string;
  /** Called after a successful checkout with the refreshed workspace. */
  onAfterCheckout?: (ws: WorkspaceResponse) => void;
}

/**
 * Remote-branch browser with local checkout.
 *
 * Lists every branch the remote reports, marks the workspace's
 * current branch, and lets the user check out any branch locally.
 * A "Create branch" form creates a new branch on the remote.
 *
 * DESIGN — remote list, local checkout:
 *   The branch list comes from the remote (`listBranches`) because
 *   that is the authoritative source of what the repository
 *   contains. The checkout action is local (`checkoutBranch`):
 *   the workspace switches to a branch that already exists as a
 *   remote-tracking ref. If a branch exists only on the remote and
 *   has never been fetched, the checkout fails with a clear error;
 *   a follow-up "fetch" action can be added in a later phase to
 *   resolve this case.
 *
 * DESIGN — create is remote-only:
 *   Creating a branch via this component creates it on the remote,
 *   not locally. The new branch will appear in the list after the
 *   next `listBranches` call, but the user must explicitly
 *   `checkout` it to switch the workspace to it. This matches the
 *   backend API: `createBranch` is a remote operation, and
 *   `checkoutBranch` is a local one.
 */
export function BranchManager({
  workspaceId,
  repositoryId,
  provider = "github",
  currentBranch,
  onAfterCheckout,
}: BranchManagerProps) {
  const [branches, setBranches] = useState<BranchResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [checkoutLoading, setCheckoutLoading] = useState<string | null>(
    null
  );
  const [checkoutError, setCheckoutError] = useState<string | null>(
    null
  );

  const [showCreate, setShowCreate] = useState(false);
  const [newBranchName, setNewBranchName] = useState("");
  const [createLoading, setCreateLoading] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [localCurrent, setLocalCurrent] = useState(currentBranch);

  // Merge / conflict modal state.
  const [showMergeDialog, setShowMergeDialog] = useState(false);
  const [showConflictResolver, setShowConflictResolver] = useState(false);
  const [conflictState, setConflictState] =
    useState<ConflictListResponse | null>(null);

  // Keep the local mirror in sync when the parent pushes a new
  // current branch (e.g. after the Agent flow switched branches).
  useEffect(() => {
    setLocalCurrent(currentBranch);
  }, [currentBranch]);

  const loadBranches = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    sourceControlClient
      .listBranches(repositoryId, provider, 100)
      .then((data) => {
        if (!cancelled) setBranches(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(
            err instanceof Error
              ? err.message
              : "Failed to load branches."
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [repositoryId, provider]);

  useEffect(() => {
    const cleanup = loadBranches();
    return cleanup;
  }, [loadBranches]);

  // Probe for an in-progress operation on mount (and whenever the
  // current branch changes). This covers the "user reloaded the
  // page while a merge was in conflict" case: the browser does not
  // have to remember, the Git repository is the source of truth.
  useEffect(() => {
    let cancelled = false;
    sourceControlClient
      .listWorkspaceConflicts(workspaceId)
      .then((res) => {
        if (cancelled) return;
        if (res.operation_state !== "none") {
          setConflictState(res);
        } else {
          setConflictState(null);
        }
      })
      .catch(() => {
        // A probe failure is not fatal: the user can still open
        // the resolver manually via the merge dialog.
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId, localCurrent]);

  const handleCheckout = useCallback(
    async (branchName: string) => {
      if (checkoutLoading) return;
      setCheckoutLoading(branchName);
      setCheckoutError(null);
      try {
        const ws = await sourceControlClient.checkoutBranch(workspaceId, {
          branch_name: branchName,
          force: false,
        });
        setLocalCurrent(ws.current_branch);
        onAfterCheckout?.(ws);
      } catch (err) {
        setCheckoutError(
          err instanceof Error ? err.message : "Checkout failed."
        );
      } finally {
        setCheckoutLoading(null);
      }
    },
    [workspaceId, checkoutLoading, onAfterCheckout]
  );

  const handleCreate = useCallback(async () => {
    const name = newBranchName.trim();
    if (!name) return;
    setCreateLoading(true);
    setCreateError(null);
    try {
      await sourceControlClient.createBranch(repositoryId, {
        branch_name: name,
      });
      setShowCreate(false);
      setNewBranchName("");
      // Refresh the list so the new branch shows up immediately.
      loadBranches();
    } catch (err) {
      setCreateError(
        err instanceof Error
          ? err.message
          : "Branch creation failed."
      );
    } finally {
      setCreateLoading(false);
    }
  }, [newBranchName, repositoryId, loadBranches]);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-card/40 shrink-0">
        <div className="text-xs font-semibold text-foreground flex items-center gap-1.5">
          <GitBranch className="w-3.5 h-3.5" />
          Branches
          <span className="ml-1 text-[10px] font-normal text-muted-foreground">
            (current: {localCurrent})
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setShowMergeDialog(true)}
            disabled={branches.length < 2}
            className="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-border hover:bg-muted disabled:opacity-50"
            title="Merge branches"
          >
            <GitMerge className="w-3 h-3" />
            Merge
          </button>
          <button
            type="button"
            onClick={() => setShowCreate((v) => !v)}
            className="p-1 rounded hover:bg-muted"
            title="Create branch"
            aria-label="Create branch"
          >
            <Plus className="w-3.5 h-3.5 text-muted-foreground" />
          </button>
          <button
            type="button"
            onClick={loadBranches}
            disabled={loading}
            className="p-1 rounded hover:bg-muted disabled:opacity-50"
            title="Refresh"
            aria-label="Refresh"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 text-muted-foreground ${
                loading ? "animate-spin" : ""
              }`}
            />
          </button>
        </div>
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="px-3 py-2 border-b border-border bg-muted/30 space-y-2 shrink-0">
          <div className="flex items-center gap-1.5">
            <input
              type="text"
              value={newBranchName}
              onChange={(e) => setNewBranchName(e.target.value)}
              placeholder="feature/my-branch"
              className="flex-1 text-xs px-2 py-1 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary font-mono"
              disabled={createLoading}
            />
            <button
              type="button"
              onClick={handleCreate}
              disabled={createLoading || !newBranchName.trim()}
              className="text-xs px-2 py-1 rounded bg-primary text-primary-foreground disabled:opacity-50 inline-flex items-center gap-1"
            >
              {createLoading && (
                <Loader2 className="w-3 h-3 animate-spin" />
              )}
              Create
            </button>
          </div>
          {createError && (
            <div className="text-[10px] text-red-700">
              {createError}
            </div>
          )}
        </div>
      )}

      {checkoutError && (
        <div className="px-3 py-2 border-b border-border bg-red-50 text-[10px] text-red-700 flex items-start gap-1.5 shrink-0">
          <AlertCircle className="w-3 h-3 shrink-0 mt-0.5" />
          <span>{checkoutError}</span>
        </div>
      )}

      {/* Conflict banner */}
      {conflictState && conflictState.operation_state !== "none" && (
        <div className="flex items-start gap-2 px-3 py-2 border-b border-amber-200 bg-amber-50 text-[11px] text-amber-800 shrink-0">
          <FileWarning className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <div className="font-medium">
              A {conflictState.operation_state} operation is in
              progress.
            </div>
            <div className="text-[10px] mt-0.5">
              {conflictState.total} conflicted file
              {conflictState.total === 1 ? "" : "s"} waiting to be
              resolved.
            </div>
          </div>
          <button
            type="button"
            onClick={() => setShowConflictResolver(true)}
            className="shrink-0 text-[10px] px-2 py-0.5 rounded border border-amber-300 bg-amber-100 hover:bg-amber-200"
          >
            Resolve
          </button>
        </div>
      )}

      {/* Branch list */}
      <div className="flex-1 overflow-auto">
        {loading && (
          <div className="p-3 flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Loading branches…
          </div>
        )}

        {!loading && error && (
          <div className="p-3 flex items-start gap-2 text-xs text-red-700">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {!loading && !error && branches.length === 0 && (
          <div className="p-3 text-xs text-muted-foreground">
            No branches returned by the remote.
          </div>
        )}

        {!loading && !error && branches.length > 0 && (
          <ul className="divide-y divide-border">
            {branches.map((branch) => {
              const isCurrent = branch.name === localCurrent;
              const isCheckingOut = checkoutLoading === branch.name;
              return (
                <li
                  key={branch.name}
                  className="flex items-center gap-2 px-3 py-2 hover:bg-muted/30"
                >
                  <GitBranch
                    className={`w-3.5 h-3.5 shrink-0 ${
                      isCurrent
                        ? "text-primary"
                        : "text-muted-foreground"
                    }`}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs text-foreground truncate flex items-center gap-1 flex-wrap">
                      <span
                        className={isCurrent ? "font-semibold" : ""}
                      >
                        {branch.name}
                      </span>
                      {branch.is_default && (
                        <span className="text-[9px] px-1 py-0.5 rounded bg-primary/10 text-primary">
                          default
                        </span>
                      )}
                      {branch.is_protected && (
                        <span className="text-[9px] px-1 py-0.5 rounded bg-amber-100 text-amber-700">
                          protected
                        </span>
                      )}
                    </div>
                    {branch.head_sha && (
                      <div className="text-[10px] text-muted-foreground font-mono">
                        {branch.head_sha.slice(0, 8)}
                      </div>
                    )}
                  </div>
                  {isCurrent ? (
                    <span className="flex items-center gap-1 text-[10px] text-primary">
                      <Check className="w-3 h-3" />
                      checked out
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => handleCheckout(branch.name)}
                      disabled={!!checkoutLoading}
                      className="text-[10px] px-2 py-1 rounded border border-border hover:bg-muted disabled:opacity-50 inline-flex items-center gap-1"
                    >
                      {isCheckingOut ? (
                        <Loader2 className="w-3 h-3 animate-spin" />
                      ) : (
                        "Checkout"
                      )}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* Merge modal */}
      {showMergeDialog && (
        <MergeDialog
          workspaceId={workspaceId}
          branches={branches}
          currentBranch={localCurrent}
          onCancel={() => setShowMergeDialog(false)}
          onResult={(outcome) => {
            setShowMergeDialog(false);
            if (outcome.kind === "conflicted") {
              // Refresh the conflict list then open the resolver.
              sourceControlClient
                .listWorkspaceConflicts(workspaceId)
                .then((res) => {
                  setConflictState(res);
                  setShowConflictResolver(true);
                })
                .catch(() => {
                  // Fall back to opening the resolver without
                  // pre-loaded state; it re-fetches on mount.
                  setShowConflictResolver(true);
                });
            } else {
              // Clean or up-to-date: reload the branches so any
              // new state (e.g. a merge commit on the target)
              // reflects in the list immediately.
              loadBranches();
              setConflictState(null);
            }
          }}
        />
      )}

      {/* Conflict resolver modal */}
      {showConflictResolver && (
        <ConflictResolver
          workspaceId={workspaceId}
          onDone={() => {
            setShowConflictResolver(false);
            setConflictState(null);
            // Reload branches: finalizing a merge advances the
            // target branch, which changes its `head_sha` in the
            // list.
            loadBranches();
          }}
        />
      )}
    </div>
  );
}
