// frontend/src/features/source-control/components/git/ConflictResolver.tsx

import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronRight,
  FileWarning,
  GitMerge,
  Loader2,
  X,
  XCircle,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type {
  ConflictFileResponse,
  ConflictListResponse,
} from "../../api/schemas";

export interface ConflictResolverProps {
  workspaceId: string;
  /** Called when the user closes the dialog or finishes resolving. */
  onDone: () => void;
}

type BulkStrategy = "ours" | "theirs" | "manual";

/**
 * Modal dialog for resolving in-progress merge/rebase conflicts.
 *
 * Loads the current conflict list on mount, displays one row per
 * conflicted file, and provides two ways to resolve:
 *
 *   1. Per-row quick buttons: "Ours" / "Theirs" — resolve a single
 *      file by taking one side. Each click calls the backend and
 *      refreshes the list.
 *   2. Bulk actions on a selection: resolve all selected files by
 *      taking ours, theirs, or marking them as manually resolved
 *      (useful when the user edited the files in the editor tab).
 *
 * After all conflicts are resolved, a "Finalize" button commits the
 * merge. An "Abort" button discards the in-progress operation.
 */
export function ConflictResolver({
  workspaceId,
  onDone,
}: ConflictResolverProps) {
  const [data, setData] = useState<ConflictListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(
    new Set()
  );
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [finalizing, setFinalizing] = useState(false);
  const [aborting, setAborting] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    sourceControlClient
      .listWorkspaceConflicts(workspaceId)
      .then((res) => {
        if (cancelled) return;
        setData(res);
        // Preserve selection for paths that are still conflicted.
        setSelectedPaths((prev) => {
          const stillConflicted = new Set(res.files.map((f) => f.path));
          const next = new Set<string>();
          prev.forEach((p) => {
            if (stillConflicted.has(p)) next.add(p);
          });
          return next;
        });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof Error
            ? err.message
            : "Failed to load conflicts."
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  useEffect(() => {
    const cleanup = load();
    return cleanup;
  }, [load]);

  const files = data?.files ?? [];
  const allResolved = data !== null && files.length === 0;
  const inProgress =
    data?.operation_state && data.operation_state !== "none";

  const toggleSelect = (path: string) => {
    setSelectedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedPaths.size === files.length) {
      setSelectedPaths(new Set());
    } else {
      setSelectedPaths(new Set(files.map((f) => f.path)));
    }
  };

  const resolveOne = async (
    path: string,
    strategy: "ours" | "theirs"
  ) => {
    setActionLoading(true);
    setActionError(null);
    try {
      await sourceControlClient.resolveWorkspaceConflicts(workspaceId, {
        strategy,
        paths: [path],
      });
      await load();
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : "Failed to resolve file."
      );
    } finally {
      setActionLoading(false);
    }
  };

  const resolveSelected = async (strategy: BulkStrategy) => {
    if (selectedPaths.size === 0) return;
    setActionLoading(true);
    setActionError(null);
    try {
      await sourceControlClient.resolveWorkspaceConflicts(workspaceId, {
        strategy,
        paths: Array.from(selectedPaths),
      });
      setSelectedPaths(new Set());
      await load();
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : "Failed to resolve selected files."
      );
    } finally {
      setActionLoading(false);
    }
  };

  const handleFinalize = async () => {
    setFinalizing(true);
    setActionError(null);
    try {
      await sourceControlClient.finalizeWorkspaceOperation(workspaceId);
      onDone();
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : "Failed to finalize the operation."
      );
    } finally {
      setFinalizing(false);
    }
  };

  const handleAbort = async () => {
    setAborting(true);
    setActionError(null);
    try {
      await sourceControlClient.abortWorkspaceOperation(workspaceId);
      onDone();
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : "Failed to abort the operation."
      );
    } finally {
      setAborting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="conflict-resolver-title"
    >
      <div className="bg-background rounded-lg shadow-xl w-full max-w-2xl flex flex-col overflow-hidden border border-border max-h-[80vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-card/40 shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            <FileWarning className="w-4 h-4 text-amber-600 shrink-0" />
            <h3
              id="conflict-resolver-title"
              className="text-sm font-semibold text-foreground truncate"
            >
              Resolve conflicts
            </h3>
            {data && inProgress && (
              <span className="text-[9px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 font-medium">
                {data.operation_state}
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={onDone}
            className="p-1 rounded hover:bg-muted"
            title="Close"
            aria-label="Close"
          >
            <X className="w-4 h-4 text-muted-foreground" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {loading && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              Loading conflicts…
            </div>
          )}

          {!loading && error && (
            <div className="flex items-start gap-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded p-2">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          {!loading && !error && data && !inProgress && (
            <div className="text-xs text-muted-foreground italic">
              No merge or rebase is currently in progress.
            </div>
          )}

          {!loading &&
            !error &&
            data &&
            inProgress &&
            allResolved && (
              <div className="flex items-start gap-2 text-xs text-green-700 bg-green-50 border border-green-200 rounded p-3">
                <Check className="w-4 h-4 shrink-0 mt-0.5" />
                <div>
                  <div className="font-medium">
                    All conflicts resolved.
                  </div>
                  <div className="text-[11px] mt-0.5">
                    Finalize to commit the merge, or abort to undo it.
                  </div>
                </div>
              </div>
            )}

          {!loading && !error && files.length > 0 && (
            <>
              {/* Bulk actions */}
              <div className="flex items-center gap-2 flex-wrap text-[11px]">
                <label className="flex items-center gap-1.5 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={
                      files.length > 0 &&
                      selectedPaths.size === files.length
                    }
                    onChange={toggleSelectAll}
                    disabled={actionLoading}
                  />
                  <span className="text-muted-foreground">
                    {selectedPaths.size > 0
                      ? `${selectedPaths.size} selected`
                      : "Select all"}
                  </span>
                </label>
                <div className="flex items-center gap-1 ml-auto">
                  <button
                    type="button"
                    onClick={() => resolveSelected("ours")}
                    disabled={
                      actionLoading || selectedPaths.size === 0
                    }
                    className="px-2 py-1 rounded border border-border hover:bg-muted disabled:opacity-50"
                  >
                    Resolve selected as ours
                  </button>
                  <button
                    type="button"
                    onClick={() => resolveSelected("theirs")}
                    disabled={
                      actionLoading || selectedPaths.size === 0
                    }
                    className="px-2 py-1 rounded border border-border hover:bg-muted disabled:opacity-50"
                  >
                    as theirs
                  </button>
                  <button
                    type="button"
                    onClick={() => resolveSelected("manual")}
                    disabled={
                      actionLoading || selectedPaths.size === 0
                    }
                    className="px-2 py-1 rounded border border-border hover:bg-muted disabled:opacity-50"
                  >
                    mark as manual
                  </button>
                </div>
              </div>

              {/* File list */}
              <ul className="border border-border rounded divide-y divide-border">
                {files.map((file) => (
                  <ConflictRow
                    key={file.path}
                    file={file}
                    selected={selectedPaths.has(file.path)}
                    onToggle={() => toggleSelect(file.path)}
                    onResolveOurs={() => resolveOne(file.path, "ours")}
                    onResolveTheirs={() => resolveOne(file.path, "theirs")}
                    disabled={actionLoading}
                  />
                ))}
              </ul>
            </>
          )}

          {actionError && (
            <div className="flex items-start gap-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded p-2">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{actionError}</span>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-t border-border bg-card/40 shrink-0">
          <button
            type="button"
            onClick={handleAbort}
            disabled={aborting || finalizing || !inProgress}
            className="text-xs px-3 py-1.5 rounded border border-red-300 bg-red-50 text-red-700 hover:bg-red-100 disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            {aborting ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <XCircle className="w-3 h-3" />
            )}
            Abort
          </button>
          <button
            type="button"
            onClick={handleFinalize}
            disabled={
              finalizing ||
              aborting ||
              !inProgress ||
              !allResolved ||
              actionLoading
            }
            className="text-xs px-3 py-1.5 rounded bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            {finalizing ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <GitMerge className="w-3 h-3" />
            )}
            Finalize
          </button>
        </div>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Single conflict row
// ----------------------------------------------------------------------

function ConflictRow({
  file,
  selected,
  onToggle,
  onResolveOurs,
  onResolveTheirs,
  disabled,
}: {
  file: ConflictFileResponse;
  selected: boolean;
  onToggle: () => void;
  onResolveOurs: () => void;
  onResolveTheirs: () => void;
  disabled: boolean;
}) {
  return (
    <li className="flex items-center gap-2 px-3 py-2 text-xs hover:bg-muted/30">
      <input
        type="checkbox"
        checked={selected}
        onChange={onToggle}
        disabled={disabled}
      />
      <FileWarning className="w-3.5 h-3.5 shrink-0 text-amber-600" />
      <div className="flex-1 min-w-0">
        <div className="font-mono text-foreground truncate">
          {file.path}
        </div>
        <div className="text-[10px] text-muted-foreground flex items-center gap-1.5 flex-wrap mt-0.5">
          {file.is_delete_modify && (
            <span className="px-1 py-0.5 rounded bg-amber-100 text-amber-700">
              delete/modify
            </span>
          )}
          {file.is_add_add && (
            <span className="px-1 py-0.5 rounded bg-amber-100 text-amber-700">
              add/add
            </span>
          )}
          <span>
            base:{file.has_base ? "✓" : "—"} ours:{file.has_ours ? "✓" : "—"}{" "}
            theirs:{file.has_theirs ? "✓" : "—"}
          </span>
        </div>
      </div>
      <div className="flex items-center gap-1 shrink-0">
        <button
          type="button"
          onClick={onResolveOurs}
          disabled={disabled || !file.has_ours}
          className="text-[10px] px-2 py-0.5 rounded border border-border hover:bg-muted disabled:opacity-40"
          title="Take the local (ours) side"
        >
          Ours
        </button>
        <button
          type="button"
          onClick={onResolveTheirs}
          disabled={disabled || !file.has_theirs}
          className="text-[10px] px-2 py-0.5 rounded border border-border hover:bg-muted disabled:opacity-40"
          title="Take the incoming (theirs) side"
        >
          Theirs
        </button>
      </div>
    </li>
  );
}
