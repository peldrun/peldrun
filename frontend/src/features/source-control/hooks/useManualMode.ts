// frontend/src/features/source-control/hooks/useManualMode.ts

import { useCallback } from "react";
import {
  sourceControlClient,
  SourceControlApiError,
} from "../api/source-control-client";
import { useManualModeStore } from "../state/manual-mode-store";

/**
 * Encapsulates every API call needed by Manual Mode.
 *
 * Each returned function corresponds to one user action in the UI.
 *
 * DESIGN NOTE — read carefully before modifying:
 *
 *   This hook intentionally does NOT subscribe to the store's state.
 *   Every action reads the latest values via
 *   `useManualModeStore.getState()` at the moment the action runs.
 *
 *   Why? Two reasons:
 *
 *     1. Avoid stale closures. If we destructured the state at the
 *        top of the hook, an async action that takes ~2 seconds
 *        (e.g. a git clone) would capture the state from the render
 *        that created the callback, which may be several renders
 *        old by the time the action completes. Reading
 *        `getState()` at execution time always returns the
 *        current values.
 *
 *     2. Avoid unnecessary re-renders. Calling
 *        `useManualModeStore()` (without a selector) causes the
 *        consumer component to re-render on every store mutation,
 *        even ones this hook does not care about. By not
 *        subscribing, the consumer only re-renders when it also
 *        calls the store directly (which the UI components do
 *        anyway to read state).
 */
export function useManualMode() {
  // --- Step 2: Create workspace ---
  const createWorkspace = useCallback(async (): Promise<boolean> => {
    const { repository, chatId, branch, setStep, setError } =
      useManualModeStore.getState();

    if (!repository || !chatId) {
      setError("Repository and chat context are required.");
      return false;
    }

    setStep("creating-workspace");
    setError(null);

    try {
      const ws = await sourceControlClient.createWorkspace({
        repository_id: repository.repository_id,
        branch_name: branch,
        provider: repository.provider,
        chat_id: chatId, // Tells backend to use <chat_id>/files
      });

      useManualModeStore.getState().setWorkspace(ws);

      // Notify any workspace/file panels to refresh their listings.
      // NOTE: we dispatch `workspace_id`, not `workspace_path`.
      // The backend's WorkspaceResponse does not include a path
      // field (see T1.6 in the delivery notes).
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("peldrun:artifact-updated", {
            detail: { workspace_id: ws.workspace_id },
          })
        );
      }

      return true;
    } catch (err) {
      const msg =
        err instanceof SourceControlApiError
          ? err.detail
          : "Failed to create workspace.";
      useManualModeStore.getState().setError(msg);
      return false;
    }
  }, []);

  // --- Step 4: Inspect diff + request approval ---
  const inspectAndRequestApproval = useCallback(async (): Promise<boolean> => {
    const { repository, workspaceId, branch, setStep, setError } =
      useManualModeStore.getState();

    if (!workspaceId) {
      setError("Workspace is required.");
      return false;
    }

    // The repository might be null if the user jumped straight to the
    // editing step because an existing workspace was detected.
    // In that case, we fetch the workspace again to get the repository_id.
    let repoId = repository?.repository_id;
    let provider = repository?.provider || "github";

    if (!repoId) {
      try {
        const ws = await sourceControlClient.getWorkspace(workspaceId);
        repoId = ws.repository_id;
      } catch (err) {
        setError("Could not determine repository for this workspace.");
        return false;
      }
    }

    setStep("reviewing");
    setError(null);

    try {
      // 1. Create the durable operation.
      //
      // Include the user-entered title and description in the
      // operation's metadata. The Manual Mode "Describe your
      // changes" card writes these values to the store BEFORE the
      // user clicks "I'm done editing", so they are guaranteed to
      // be present here. The backend stores them as
      // `metadata.title` / `metadata.description` and the PR
      // auto-creation flow later uses them as the PR title and body.
      const storeState = useManualModeStore.getState();
      const op = await sourceControlClient.createOperation({
        operation_type: "push_branch",
        repository_id: repoId,
        target_branch: branch,
        workspace_id: workspaceId,
        provider: provider,
        title: storeState.title.trim() || undefined,
        description: storeState.description.trim() || undefined,
      });

      // 2. Request the approval ticket (computes diff, binds it).
      const ticket = await sourceControlClient.requestOperationApproval(
        op.operation_id
      );

      // 3. Fetch the diff for display.
      const diff = await sourceControlClient.inspectWorkspaceDiff(workspaceId);

      // NOTE: setApproval does NOT change `step`. It stays
      // "reviewing" so the user can see the diff in
      // ManualDiffReview before proceeding.
      useManualModeStore
        .getState()
        .setApproval(op.operation_id, ticket.approval_id, diff);

      return true;
    } catch (err) {
      const msg =
        err instanceof SourceControlApiError
          ? err.detail
          : "Failed to request approval.";
      useManualModeStore.getState().setError(msg);
      return false;
    }
  }, []);

  // --- Step 5: Approve + push ---
  const approveAndPush = useCallback(async (): Promise<boolean> => {
    const { operationId, approvalId, workspaceId, setStep, setError } =
      useManualModeStore.getState();

    if (!operationId || !approvalId || !workspaceId) {
      setError("Operation, approval, and workspace are required.");
      return false;
    }

    setStep("pushing");
    setError(null);

    try {
      // 1. Grant the single-use ticket.
      await sourceControlClient.approveOperation(operationId, {
        approval_id: approvalId,
        user_id: "user",
      });

      // 2. Consume the ticket and push.
      await sourceControlClient.pushApprovedBranch(operationId, {
        approval_id: approvalId,
        workspace_id: workspaceId,
      });

      useManualModeStore.getState().setStep("pushed");
      return true;
    } catch (err) {
      // Stale review: the remote moved since approval was requested.
      // The backend returns 412 in this case. We surface a clear
      // message and force the user back to the "editing" step so
      // they re-inspect the diff and request a fresh approval.
      if (err instanceof SourceControlApiError && err.status === 412) {
        useManualModeStore.getState().setError(
          "The remote branch moved since you requested approval. " +
            "Please inspect the diff again and request a new approval."
        );
        useManualModeStore.getState().setStep("editing");
      } else {
        const msg =
          err instanceof SourceControlApiError
            ? err.detail
            : "Push failed.";
        useManualModeStore.getState().setError(msg);
      }
      return false;
    }
  }, []);

  // --- Reject ---
  const rejectApproval = useCallback(
    async (reason: string): Promise<boolean> => {
      const { operationId, approvalId, setError } =
        useManualModeStore.getState();

      if (!operationId || !approvalId) return false;
      setError(null);

      try {
        await sourceControlClient.rejectOperation(operationId, {
          approval_id: approvalId,
          user_id: "user",
          reason,
        });
        useManualModeStore.getState().clearApproval();
        return true;
      } catch (err) {
        const msg =
          err instanceof SourceControlApiError
            ? err.detail
            : "Reject failed.";
        useManualModeStore.getState().setError(msg);
        return false;
      }
    },
    []
  );

  return {
    createWorkspace,
    inspectAndRequestApproval,
    approveAndPush,
    rejectApproval,
  };
}
