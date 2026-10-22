// frontend/src/features/source-control/components/manual/ManualEditingGuide.tsx

import React, { useState } from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { useManualMode } from "../../hooks/useManualMode";
import { AlertCircle, ArrowRight, FileEdit } from "lucide-react";

/**
 * Step 3 of Manual Mode: instruct the user to edit files.
 *
 * The workspace already exists on disk (under
 * `storage/chats/<chatId>/files/`). The user can now edit files
 * through the project's Editor tab, which reads/writes the same
 * folder. This component only:
 *
 *   - Explains the workflow.
 *   - Provides a "I'm done — Inspect Diff" button that triggers
 *     `inspectAndRequestApproval()`, which:
 *       1. Creates a durable `push_branch` operation.
 *       2. Requests the approval ticket (computes the diff hash).
 *       3. Fetches the diff summary.
 *     All three land in the store, and `step` becomes "reviewing"
 *     so `ManualDiffReview` takes over.
 *
 * NOTE — no `workspace_path` is displayed. The backend's
 * `WorkspaceResponse` does not include a path field; the folder is
 * implied by `chatId` and the workspace is bound to that chat.
 */
export function ManualEditingGuide() {
  const { workspaceId, chatId, step } = useManualModeStore();
  const { inspectAndRequestApproval } = useManualMode();

  const [isInspecting, setIsInspecting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const handleContinue = async () => {
    if (isInspecting) return;
    if (!workspaceId) {
      setLocalError(
        "No workspace is bound to this chat. Please go back and create one."
      );
      return;
    }

    setIsInspecting(true);
    setLocalError(null);
    try {
      const ok = await inspectAndRequestApproval();
      if (!ok) {
        // The hook already wrote a detailed message to the store via
        // setError(), which also moved `step` to "error". We set a
        // short local copy so the user does not lose context if the
        // error banner is far away.
        setLocalError("Could not request approval. See the error banner.");
      }
    } finally {
      setIsInspecting(false);
    }
  };

  const handleBack = () => {
    if (isInspecting) return;
    // Going back to step 1 lets the user pick a different repo/branch.
    // Existing workspace is NOT deleted.
    useManualModeStore.getState().setStep("select-repo");
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">
          Edit Your Files
        </h3>
        <p className="text-xs text-muted-foreground mb-4">
          Your workspace is ready. Use the Files tab to edit any file in the
          cloned repository.
        </p>
      </div>

      {/* Local error */}
      {localError && (
        <div className="flex items-start rounded-md bg-red-50 p-3 border border-red-200">
          <AlertCircle className="h-4 w-4 text-red-600 mr-2 flex-shrink-0 mt-0.5" />
          <p className="text-xs text-red-700 flex-1">{localError}</p>
        </div>
      )}

      {/* Instructions */}
      <div className="rounded-md border border-border bg-card p-4 space-y-3">
        <InstructionRow
          number={1}
          title="Open the Files tab"
          description="Your repository files are now visible there."
        />
        <InstructionRow
          number={2}
          title="Click any file to edit it"
          description="The Editor tab will open automatically."
        />
        <InstructionRow
          number={3}
          title="Save your changes"
          description="Press Ctrl+S or use the Save button in the Editor."
        />
        <InstructionRow
          number={4}
          title="Return here when done"
          description="Click the button below to inspect your diff and request approval."
        />
      </div>

      {/* Workspace info */}
      <div className="rounded-md bg-muted/50 p-3 text-xs space-y-1">
        <div>
          <span className="text-muted-foreground">Workspace ID:</span>{" "}
          <span className="font-mono break-all">
            {workspaceId ?? "—"}
          </span>
        </div>
        <div>
          <span className="text-muted-foreground">Chat ID:</span>{" "}
          <span className="font-mono break-all">{chatId ?? "—"}</span>
        </div>
        <div className="text-muted-foreground">
          Files are stored under{" "}
          <code className="font-mono text-[11px]">
            storage/chats/{chatId ?? "<chat>"}/files/
          </code>
        </div>
      </div>

      {/* Actions */}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={handleBack}
          disabled={isInspecting}
          className="rounded-md border border-border bg-background px-4 py-2 text-sm disabled:opacity-50"
        >
          Back
        </button>
        <button
          type="button"
          onClick={handleContinue}
          disabled={isInspecting || !workspaceId}
          className="flex-1 flex items-center justify-center gap-2 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isInspecting ? (
            <>
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-r-transparent" />
              Computing diff…
            </>
          ) : (
            <>
              <FileEdit size={16} />
              I'm done editing — Inspect Diff
              <ArrowRight size={14} />
            </>
          )}
        </button>
      </div>

      {/* Hint about the current step variable */}
      {step !== "editing" && step !== "reviewing" && (
        <p className="text-[11px] text-muted-foreground text-center">
          Current step: <code className="font-mono">{step}</code>
        </p>
      )}
    </div>
  );
}

/**
 * Small helper for the four-instruction block. Keeps the JSX above
 * readable and gives every row the same spacing/typography.
 */
function InstructionRow(props: {
  number: number;
  title: string;
  description: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <div className="flex h-6 w-6 items-center justify-center rounded-full bg-primary text-primary-foreground text-xs font-medium shrink-0">
        {props.number}
      </div>
      <div>
        <p className="text-sm font-medium">{props.title}</p>
        <p className="text-xs text-muted-foreground">{props.description}</p>
      </div>
    </div>
  );
}
