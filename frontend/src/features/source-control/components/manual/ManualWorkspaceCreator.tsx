// frontend/src/features/source-control/components/manual/ManualWorkspaceCreator.tsx

// frontend/src/features/source-control/components/manual/ManualWorkspaceCreator.tsx

import React, { useState } from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { useManualMode } from "../../hooks/useManualMode";
import { ArrowLeft, FolderGit2, AlertTriangle } from "lucide-react";

/**
 * Step 2 of Manual Mode: create the workspace.
 *
 * IMPORTANT — do not confuse the two flags:
 *
 *   - `step === "creating-workspace"` means "the user is currently
 *     on this screen". It is set by the previous step's Continue
 *     button and stays true the entire time this component is
 *     mounted.
 *
 *   - `isCreating` (local state) means "the create_workspace API
 *     call is currently in flight". It is set only when the user
 *     clicks the Create Workspace button, and cleared in a
 *     `finally` block (so it survives unexpected errors).
 *
 *   The delivery report conflated the two, which disabled the
 *   Create button permanently. This implementation fixes that.
 */
export function ManualWorkspaceCreator() {
  const { repository, branch, chatId } = useManualModeStore();
  const { createWorkspace } = useManualMode();

  const [isCreating, setIsCreating] = useState(false);
  const [showWarning, setShowWarning] = useState(true);

  const handleCreate = async () => {
    if (isCreating) return;
    setIsCreating(true);
    try {
      await createWorkspace();
      // On success, useManualMode's createWorkspace() calls
      // setWorkspace(ws) internally, which advances `step` to
      // "editing" and unmounts this component.
      //
      // On failure, it calls setError(msg), which advances `step`
      // to "error" and also unmounts this component.
      //
      // Either way, the `finally` below runs before React actually
      // unmounts the component on the next render tick, so setting
      // local state is safe here.
    } finally {
      setIsCreating(false);
    }
  };

  const handleBack = () => {
    if (isCreating) return;
    useManualModeStore.getState().setStep("select-repo");
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">
          Create Workspace
        </h3>
        <p className="text-xs text-muted-foreground mb-4">
          This will clone{" "}
          <strong>{repository?.full_name ?? "(no repository selected)"}</strong>{" "}
          into the files folder of chat{" "}
          <code className="font-mono text-[11px]">{chatId ?? "unknown"}</code>.
        </p>
      </div>

      {/* Warning — dismissible */}
      {showWarning && (
        <div className="flex items-start rounded-md bg-yellow-50 p-3 border border-yellow-200">
          <AlertTriangle className="h-5 w-5 text-yellow-600 mr-2 flex-shrink-0 mt-0.5" />
          <div className="text-xs text-yellow-800 flex-1">
            <p className="font-medium">Before you continue:</p>
            <ul className="mt-1 list-disc list-inside space-y-0.5">
              <li>
                If this chat already has files, they may conflict with the
                clone.
              </li>
              <li>
                It is recommended to use a fresh chat for source-control
                work.
              </li>
              <li>Your existing files will not be deleted automatically.</li>
            </ul>
            <button
              type="button"
              onClick={() => setShowWarning(false)}
              className="mt-2 text-xs font-medium underline hover:no-underline"
            >
              I understand, dismiss
            </button>
          </div>
        </div>
      )}

      {/* Summary */}
      <div className="rounded-md border border-border bg-card p-3 space-y-1 text-xs">
        <div>
          <span className="text-muted-foreground">Repository:</span>{" "}
          <span className="font-medium">
            {repository?.full_name ?? "—"}
          </span>
        </div>
        <div>
          <span className="text-muted-foreground">Base branch:</span>{" "}
          <span className="font-medium">{branch || "—"}</span>
        </div>
        <div>
          <span className="text-muted-foreground">Target chat:</span>{" "}
          <span className="font-mono">{chatId ?? "—"}</span>
        </div>
      </div>

      {/* Actions */}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={handleBack}
          disabled={isCreating}
          className="flex items-center gap-1 rounded-md border border-border bg-background px-4 py-2 text-sm disabled:opacity-50"
        >
          <ArrowLeft size={14} />
          Back
        </button>
        <button
          type="button"
          onClick={handleCreate}
          disabled={isCreating || !repository || !chatId}
          className="flex-1 flex items-center justify-center gap-2 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isCreating ? (
            <>
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-r-transparent" />
              Cloning…
            </>
          ) : (
            <>
              <FolderGit2 size={16} />
              Create Workspace
            </>
          )}
        </button>
      </div>
    </div>
  );
}
