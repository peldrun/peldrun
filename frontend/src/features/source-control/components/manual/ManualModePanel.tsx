// frontend/src/features/source-control/components/manual/ManualModePanel.tsx

import React from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { ManualStepIndicator } from "./ManualStepIndicator";
import { ManualRepoSelector } from "./ManualRepoSelector";
import { ManualWorkspaceCreator } from "./ManualWorkspaceCreator";
import { ManualEditingGuide } from "./ManualEditingGuide";
import { ManualDiffReview } from "./ManualDiffReview";
import { ManualPushButton } from "./ManualPushButton";
import {
  AlertCircle,
  CheckCircle2,
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
  const { step, error, chatId } = useManualModeStore();

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

  return (
    <div className="flex flex-col h-full bg-background">
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

  const handleStartOver = () => {
    // Re-activate with the same chat id so the user keeps the chat
    // context but starts a fresh workflow. reset() alone would
    // clear chatId and leave the panel unmountable.
    if (chatId) {
      useManualModeStore.getState().activate(chatId);
    } else {
      useManualModeStore.getState().reset();
    }

    // Notify any file/workspace panels that state changed.
    if (typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("peldrun:artifact-updated", {
          detail: { source: "manual-mode", action: "pushed" },
        })
      );
    }
  };

  return (
    <div className="text-center py-8">
      <CheckCircle2 className="h-12 w-12 text-green-500 mx-auto mb-4" />
      <h3 className="text-lg font-semibold text-foreground">
        Push Successful
      </h3>
      <p className="text-sm text-muted-foreground mt-2">
        Your changes have been pushed to the remote branch.
      </p>
      <button
        type="button"
        onClick={handleStartOver}
        className="mt-4 text-sm text-primary hover:underline"
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
