// frontend/src/features/source-control/components/manual/ManualModePanel.tsx

import React, { useState, useEffect } from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { useManualMode } from "../../hooks/useManualMode";
import { OperationResponse } from "../../api/schemas";
import { sourceControlClient } from "../../api/source-control-client";
import { ManualStepIndicator } from "./ManualStepIndicator";
import { ManualRepoSelector } from "./ManualRepoSelector";
import { ManualWorkspaceCreator } from "./ManualWorkspaceCreator";
import { ManualEditingGuide } from "./ManualEditingGuide";
import { ManualDiffReview } from "./ManualDiffReview";
import { ManualPushButton } from "./ManualPushButton";
import {
  AlertCircle,
  CheckCircle2,
  FolderX,
  MessageSquare,
  RotateCcw,
} from "lucide-react";

/**
 * Main container for Manual Mode.
 *
 * Renders the step indicator at the top, then the body for the
 * current step below. The indicator is always visible so the user
 * can see where they are in the 5-step workflow even after an
 * error, a push success, or a rejection.
 *
 * Every step value from `ManualStep` has an explicit rendering
 * branch below. Two of them — "error" and "idle" — are NOT in the
 * delivery report and would have left the panel blank. They are
 * handled here as first-class states:
 *
 *   - "idle"  → shown only if the panel is mounted before
 *               `activate(chatId)` runs. Provides a recovery
 *               button.
 *   - "error" → shown after any unrecoverable failure. Displays
 *               the store's `error` message plus a "Start over"
 *               button (which re-activates the store with the same
 *               chat id, so the user keeps their chat context).
 *
 * Note: "creating-workspace" is a *transient navigation state*
 * driven by the user clicking "Continue" in ManualRepoSelector. It
 * does NOT imply an API call is in flight — ManualWorkspaceCreator
 * owns that flag locally. See the comment on T3.3 for details.
 */
export function ManualModePanel() {
  const { step, error, chatId, workspaceId } = useManualModeStore();
  const { deleteWorkspace } = useManualMode();
  const [isDeleting, setIsDeleting] = useState(false);

  // Guard: without a chat id we cannot persist anything (files go
  // under storage/chats/<chatId>/files/). This should never happen
  // when the toggle activates the panel, but show a clear message
  // rather than rendering a broken workflow.
  if (!chatId && step !== "idle") {
    return (
      <div className="flex flex-col h-full items-center justify-center p-6">
        <AlertCircle className="h-8 w-8 text-red-500 mb-3" />
        <h3 className="text-sm font-semibold text-foreground">
          No chat context
        </h3>
        <p className="mt-2 text-xs text-muted-foreground text-center max-w-xs">
          Manual Mode requires an active chat. Open this panel from within a
          chat, or start a new chat first.
        </p>
      </div>
    );
  }

  // Show "Change Repository" whenever a workspace exists and no
  // destructive operation is in flight. It is intentionally
  // visible on `select-repo` and `error` as well: those are the
  // two steps where the user most often needs to abandon a
  // workspace they cannot use (e.g. they picked the wrong
  // repository and the backend now refuses to clone a second one
  // into the same folder).
  const showChangeRepo =
    !!workspaceId && step !== "pushing" && step !== "idle";

  const handleChangeRepo = async () => {
    if (isDeleting) return;
    // Destructive, non-reversible action: deletes the workspace
    // folder from disk. Confirm before executing.
    const confirmed = window.confirm(
      "This will delete the current workspace and all of its files " +
      "from disk. You will then be asked to select a new repository. " +
      "Continue?"
    );
    if (!confirmed) return;
    setIsDeleting(true);
    try {
      await deleteWorkspace();
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <div className="flex flex-col h-full bg-background">
      {/* Change Repository bar — only when a workspace exists */}
      {showChangeRepo && (
        <div className="flex items-center justify-end px-4 py-2 border-b border-border bg-background">
          <button
            type="button"
            onClick={handleChangeRepo}
            disabled={isDeleting}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isDeleting ? (
              <>
                <div className="h-3 w-3 animate-spin rounded-full border-2 border-current border-r-transparent" />
                Deleting workspace…
              </>
            ) : (
              <>
                <FolderX size={12} />
                Change Repository
              </>
            )}
          </button>
        </div>
      )}

      {/* Step indicator — always visible */}
      <ManualStepIndicator />

      {/* Store-level error banner (visible on every step) */}
      {error && step !== "error" && (
        <div className="mx-4 mt-4 flex items-start rounded-md bg-red-50 p-3 border border-red-200">
          <AlertCircle className="h-5 w-5 text-red-600 mr-2 flex-shrink-0" />
          <div className="flex-1">
            <h4 className="text-sm font-medium text-red-800">Error</h4>
            <p className="text-xs text-red-700 mt-1 whitespace-pre-wrap">
              {error}
            </p>
          </div>
        </div>
      )}

      {/* Step body */}
      <div className="flex-1 overflow-y-auto p-4">
        {/* The description card is shown on ALL THREE steps that
            touch the change:

              - "editing"    : filled BEFORE clicking "I'm done
                               editing", so the values are in the
                               store when `inspectAndRequestApproval`
                               creates the operation. This is the
                               critical step — without it, the card
                               appears too late.
              - "reviewing"  : still editable so the user can
                               refine the wording while looking at
                               the actual diff.
              - "approving"  : last chance to fix a typo before
                               the final confirm.
        */}
        {(step === "editing" ||
          step === "reviewing" ||
          step === "approving") && <ChangeDescriptionCard />}
        <StepBody step={step} />
      </div>
    </div>
  );
}

/**
 * Dispatches the current step to its rendering component.
 *
 * Kept as a separate function so the guard checks at the top of
 * ManualModePanel stay readable, and so each step can be tested
 * in isolation by rendering StepBody with a specific step value.
 */
function StepBody({ step }: { step: ReturnType<typeof useManualModeStore.getState>["step"] }) {
  switch (step) {
    case "idle":
      return <IdleState />;

    case "select-repo":
      return <ManualRepoSelector />;

    case "creating-workspace":
      // The "creating-workspace" step is navigation state; the
      // Create button inside ManualWorkspaceCreator is the only
      // thing that actually fires the API call.
      return <ManualWorkspaceCreator />;

    case "editing":
      return <ManualEditingGuide />;

    case "reviewing":
      return <ManualDiffReview />;

    case "approving":
      return <ManualPushButton />;

    case "pushing":
      return <PushingState />;

    case "pushed":
      return <PushedState />;

    case "error":
      return <ErrorState />;

    default: {
      // Exhaustiveness check: if ManualStep gains a new member in
      // the future, TypeScript will fail this line, forcing us to
      // add a branch here.
      const _exhaustive: never = step;
      return (
        <div className="text-center py-8 text-xs text-muted-foreground">
          Unknown step: {String(_exhaustive)}
        </div>
      );
    }
  }
}

// ----------------------------------------------------------------------
// Change description card (visible on editing + review + approve steps)
// ----------------------------------------------------------------------

/**
 * Optional "describe your changes" card.
 *
 * Appears on the "editing", "reviewing", and "approving" steps.
 * The values it collects are stored in `manual-mode-store` and
 * are propagated to `createOperation` in `useManualMode.ts` as
 * `title` / `description`. The backend persists them as
 * `metadata.title` / `metadata.description` on the operation;
 * `create_change_request` later uses them as the PR title and
 * body when the caller does not supply its own.
 *
 * These fields are intentionally OPTIONAL: the flow works without
 * them (the operation simply has no user-supplied description),
 * but when the user does fill them the eventual PR title and body
 * are significantly more helpful than the auto-generated
 * "Update from task <uuid>" default.
 */
function ChangeDescriptionCard() {
  const title = useManualModeStore((s) => s.title);
  const description = useManualModeStore((s) => s.description);
  const preferredBranchName = useManualModeStore(
    (s) => s.preferredBranchName
  );
  const setTitle = useManualModeStore((s) => s.setTitle);
  const setDescription = useManualModeStore((s) => s.setDescription);
  const setPreferredBranchName = useManualModeStore(
    (s) => s.setPreferredBranchName
  );

  return (
    <div className="mb-4 rounded-md border border-border bg-card/40 p-3 space-y-3">
      <div className="flex items-center gap-1.5">
        <MessageSquare className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
        <h4 className="text-xs font-semibold text-foreground">
          Describe your changes
        </h4>
        <span className="text-[10px] text-muted-foreground">
          (optional)
        </span>
      </div>

      <div>
        <label
          htmlFor="manual-title"
          className="block text-[10px] font-medium text-muted-foreground mb-1"
        >
          Title / Commit message
        </label>
        <input
          id="manual-title"
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="e.g. feat: add login button"
          maxLength={256}
          className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary"
        />
      </div>

      <div>
        <label
          htmlFor="manual-description"
          className="block text-[10px] font-medium text-muted-foreground mb-1"
        >
          Description (Markdown)
        </label>
        <textarea
          id="manual-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What changed and why?"
          rows={4}
          maxLength={65_536}
          className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary resize-none font-mono"
        />
      </div>

      {/* Task 1.5: optional preferred branch name for the auto-created
          feature branch. Always shown, but marked clearly as
          conditional so a user pushing to a non-protected branch
          does not wonder why nothing happened after typing a value.
          The backend silently ignores the value when auto-branching
          is not triggered (target not protected, or
          auto_create_feature_branch=false). */}
      <div>
        <label
          htmlFor="manual-preferred-branch"
          className="block text-[10px] font-medium text-muted-foreground mb-1"
        >
          Feature branch name{" "}
          <span className="text-muted-foreground font-normal">
            (optional)
          </span>
        </label>
        <input
          id="manual-preferred-branch"
          type="text"
          value={preferredBranchName}
          onChange={(e) => setPreferredBranchName(e.target.value)}
          placeholder="e.g. feature/my-fix"
          maxLength={200}
          className="w-full text-xs px-2 py-1.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary font-mono"
        />
        <p className="mt-1 text-[10px] text-muted-foreground">
          Used <strong>only if</strong> the target branch is protected
          and PELDRUN auto-creates a feature branch. Leave empty to
          let PELDRUN generate a timestamped name
          (<code>peldrun/manual-YYYYMMDD-HHMMSS</code>).
        </p>
      </div>

      <p className="text-[10px] text-muted-foreground">
        These fields are drafts stored in the UI. They will become the
        operation's suggested PR title and body once wired to the
        backend.
      </p>
    </div>
  );
}

// ----------------------------------------------------------------------
// Sub-states
// ----------------------------------------------------------------------

function IdleState() {
  return (
    <div className="flex flex-col items-center justify-center py-8 text-center">
      <p className="text-sm text-muted-foreground">
        Manual Mode is not active.
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        Open this panel from within a chat to start a workflow.
      </p>
    </div>
  );
}

function PushingState() {
  return (
    <div className="text-center py-8">
      <div className="inline-block h-8 w-8 animate-spin rounded-full border-4 border-primary border-r-transparent" />
      <p className="mt-4 text-sm text-muted-foreground">Pushing branch…</p>
      <p className="mt-1 text-[11px] text-muted-foreground">
        This may take up to two minutes for large repositories.
      </p>
    </div>
  );
}

function PushedState() {
  const chatId = useManualModeStore.getState().chatId;
  const operationId = useManualModeStore.getState().operationId;
  const branch = useManualModeStore.getState().branch;

  // Fetch the full operation record to display a comprehensive
  // "success receipt". This replaces the minimal branch-only view
  // that was shown in an earlier revision.
  const [operation, setOperation] = useState<OperationResponse | null>(null);
  const [isLoadingOp, setIsLoadingOp] = useState(true);

  useEffect(() => {
    let cancelled = false;
    if (!operationId) {
      setIsLoadingOp(false);
      return;
    }

    sourceControlClient
      .getOperation(operationId)
      .then((op) => {
        if (!cancelled) setOperation(op);
      })
      .catch(() => {
        // best-effort: if we cannot load the operation, we still
        // show the basic success message below.
      })
      .finally(() => {
        if (!cancelled) setIsLoadingOp(false);
      });

    return () => {
      cancelled = true;
    };
  }, [operationId]);

  const handleStartOver = () => {
    if (chatId) {
      useManualModeStore.getState().activate(chatId);
    } else {
      useManualModeStore.getState().reset();
    }

    if (typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("peldrun:artifact-updated", {
          detail: { source: "manual-mode", action: "pushed" },
        })
      );
    }
  };

  const autoBranch = operation?.metadata?.auto_created_branch as
    | { from?: string; to?: string }
    | undefined;
  const targetBranch = operation?.target_branch || branch;

  return (
    <div className="flex flex-col items-center py-6 px-2">
      <CheckCircle2 className="h-12 w-12 text-green-500 mb-4" />
      <h3 className="text-lg font-semibold text-foreground text-center">
        Push Successful
      </h3>
      <p className="text-sm text-muted-foreground mt-2 text-center">
        Your changes have been pushed to the remote repository.
      </p>

      {/* Detailed Operation Receipt */}
      {isLoadingOp ? (
        <div className="mt-6 flex items-center gap-2 text-xs text-muted-foreground">
          <div className="h-3 w-3 animate-spin rounded-full border-2 border-current border-r-transparent" />
          Loading operation details...
        </div>
      ) : operation ? (
        <div className="mt-6 w-full max-w-sm rounded-lg border border-border bg-card p-4 text-left shadow-sm">
          <h4 className="text-xs font-semibold text-foreground mb-3 border-b border-border pb-2">
            Operation Summary
          </h4>

          <div className="space-y-2 text-xs">
            <div className="flex justify-between items-start gap-2">
              <span className="text-muted-foreground shrink-0">Repository:</span>
              <span className="font-medium text-right break-all">
                {operation.repository_id}
              </span>
            </div>

            <div className="flex justify-between items-start gap-2">
              <span className="text-muted-foreground shrink-0">Target Branch:</span>
              <span className="font-mono font-medium text-right break-all text-primary">
                {targetBranch}
              </span>
            </div>

            <div className="flex justify-between items-start gap-2">
              <span className="text-muted-foreground shrink-0">Status:</span>
              <span className="font-medium text-green-600 uppercase">
                {operation.current_state}
              </span>
            </div>

            <div className="flex justify-between items-start gap-2">
              <span className="text-muted-foreground shrink-0">Operation ID:</span>
              <span className="font-mono text-[10px] text-right break-all">
                {operation.operation_id}
              </span>
            </div>

            {operation.change_request_url && (
              <div className="flex justify-between items-start gap-2 pt-2 border-t border-border mt-2">
                <span className="text-muted-foreground shrink-0">Pull Request:</span>
                <a
                  href={operation.change_request_url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium text-blue-600 hover:underline text-right break-all"
                >
                  Open PR
                </a>
              </div>
            )}
          </div>

          {/* Auto-Branch Info (if applicable) */}
          {autoBranch && autoBranch.from && autoBranch.to && (
            <div className="mt-4 rounded-md bg-blue-50 border border-blue-200 p-3">
              <p className="font-medium text-blue-900 text-xs mb-1">
                Auto-Branch Redirect
              </p>
              <p className="text-[11px] text-blue-800 leading-relaxed">
                <span className="font-mono">{autoBranch.from}</span> is protected.
                Your changes were safely redirected to:
                <br />
                <span className="font-mono font-semibold mt-1 inline-block">
                  {autoBranch.to}
                </span>
                <br />
                <span className="mt-2 inline-block">
                  Open a pull request from <strong>{autoBranch.to}</strong> to{" "}
                  <strong>{autoBranch.from}</strong> to merge.
                </span>
              </p>
            </div>
          )}
        </div>
      ) : (
        <div className="mt-4 text-xs text-muted-foreground">
          Branch: <span className="font-mono">{targetBranch}</span>
        </div>
      )}

      <button
        type="button"
        onClick={handleStartOver}
        className="mt-6 rounded-md bg-primary text-primary-foreground px-6 py-2 text-sm font-medium hover:bg-primary/90 transition-colors"
      >
        Start a new workflow
      </button>
    </div>
  );
}

function ErrorState() {
  const { error, chatId } = useManualModeStore();

  const handleStartOver = () => {
    if (chatId) {
      useManualModeStore.getState().activate(chatId);
    } else {
      useManualModeStore.getState().reset();
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-start rounded-md bg-red-50 p-3 border border-red-200">
        <AlertCircle className="h-5 w-5 text-red-600 mr-2 flex-shrink-0 mt-0.5" />
        <div className="flex-1">
          <h4 className="text-sm font-medium text-red-800">
            Workflow failed
          </h4>
          <p className="text-xs text-red-700 mt-1 whitespace-pre-wrap">
            {error ?? "An unknown error occurred."}
          </p>
        </div>
      </div>

      <button
        type="button"
        onClick={handleStartOver}
        className="w-full flex items-center justify-center gap-2 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90"
      >
        <RotateCcw size={16} />
        Start over
      </button>

      <p className="text-[11px] text-center text-muted-foreground">
        Starting over will reset the Manual Mode UI. It does NOT delete any
        workspace or files you already created.
      </p>
    </div>
  );
}
