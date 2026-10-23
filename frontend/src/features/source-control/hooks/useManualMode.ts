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
    const { repository, chatId, branch, workspaceId, setStep, setError } =
      useManualModeStore.getState();

    if (!repository || !chatId) {
      setError("Repository and chat context are required.");
      return false;
    }

    // Smart handling when a workspace already exists: allow branch
    // changes without requiring the user to delete the workspace.
    // The backend's `create_workspace` refuses to clone into a chat
    // folder that already contains `.git`, so re-requesting the same
    // repository with a different branch used to fail with an opaque
    // "workspace already exists" error. This block intercepts that
    // case and performs the right operation instead.
    if (workspaceId) {
      try {
        const existing = await sourceControlClient.getWorkspace(workspaceId);
        if (existing.repository_id === repository.repository_id) {
          if (existing.current_branch !== branch) {
            // Same repo, different branch: switch to the new branch.
            //
            // When the workspace is DIRTY, passing `commit_message`
            // instructs the backend to commit any pending changes
            // BEFORE the branch switch — preserving the user's edits
            // instead of refusing the operation with "Cannot switch
            // branches with uncommitted changes". Without this, the
            // user would be forced to either complete the current
            // workflow (push) or use "Change Repository" (destructive).
            //
            // The message follows a "WIP:" prefix so that it is
            // immediately recognizable as a snapshot commit rather
            // than a real change. It references the source and target
            // branches so the user can trace it back later.
            //
            // When the workspace is CLEAN, `commit_message` is left
            // undefined; JSON.stringify omits it from the wire
            // payload, and the backend proceeds with the simple
            // `git checkout` path — no commit call is made.
            const wipMessage = existing.is_dirty
              ? `WIP: switch to ${branch} (from ${existing.current_branch})`
              : undefined;

            const updated = await sourceControlClient.checkoutBranch(
              workspaceId,
              {
                branch_name: branch,
                force: false,
                commit_message: wipMessage,
              }
            );
            useManualModeStore.setState({
              workspaceId: updated.workspace_id,
              branch: updated.current_branch,
              step: "editing",
              error: null,
            });
            return true;
          }
          // Same repo, same branch: nothing to do, just go to editing.
          useManualModeStore.setState({ step: "editing", error: null });
          return true;
        }
        // Different repository: must delete the existing workspace
        // first. Surface a clear, actionable message.
        setError(
          "You already have a workspace for a different repository " +
            "in this chat. Click 'Change Repository' to delete it, " +
            "then create a new workspace."
        );
        return false;
      } catch (err) {
        // Distinguish two very different failure modes:
        //
        //   404  — the workspace no longer exists on the backend
        //          (server restart, failed delete, manual cleanup).
        //          Fall through to create a fresh one; this is the
        //          expected recovery path.
        //
        //   Other — the workspace still exists, but switching
        //          branches is impossible for some reason. The most
        //          common case is `Cannot switch branches with
        //          uncommitted changes`: `checkout_branch` refuses
        //          to discard user work. Falling through here would
        //          trigger the backend's "workspace already exists"
        //          error, which is a completely misleading message
        //          for what is really a "dirty workspace" situation.
        //
        //          Surface the real diagnostic and stop. The user
        //          can either complete the current workflow
        //          (push), or use "Change Repository" to discard.
        if (
          err instanceof SourceControlApiError &&
          err.status === 404
        ) {
          // Workspace is gone; fall through to create a fresh one.
        } else {
          const msg =
            err instanceof SourceControlApiError
              ? err.detail
              : "Cannot switch branches.";
          useManualModeStore.getState().setError(
            msg +
              " — complete the current workflow (push) first, or use " +
              "'Change Repository' to discard your changes and start " +
              "fresh."
          );
          return false;
        }
      }
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

  // --- Change repository: delete the current workspace ---
  //
  // Used by the "Change Repository" button in ManualModePanel. The
  // backend's `create_workspace` refuses to clone into a chat folder
  // that already contains a `.git` directory, so the existing
  // workspace must be deleted before the user can start over with a
  // different repository.
  //
  // The delete call is issued with `forceDeleteFiles: true` because
  // the whole point of "Change Repository" is to reclaim the chat's
  // files folder — without this flag, the registry entry would be
  // removed but the on-disk `.git` would remain, and the next
  // `createWorkspace` call would fail with the same "workspace
  // already exists" error.
  //
  // On success, the store is reset to `select-repo` with all
  // operation/approval/workspace state cleared. On failure, the
  // store's `error` field is set and the UI keeps the current
  // workspace so the user does not lose context.
  const deleteWorkspace = useCallback(async (): Promise<boolean> => {
    const { workspaceId, setError } = useManualModeStore.getState();

    if (!workspaceId) {
      setError("No workspace to delete.");
      return false;
    }

    setError(null);

    try {
      await sourceControlClient.deleteWorkspace(workspaceId, {
        forceDeleteFiles: true,
      });

      // Reset the workspace and all downstream state, then rewind
      // to step 1 so the user can pick a different repository.
      useManualModeStore.setState({
        workspaceId: null,
        operationId: null,
        approvalId: null,
        diff: null,
        title: "",
        description: "",
        error: null,
        step: "select-repo",
      });

      // Notify file/workspace panels that the folder contents changed
      // so they refresh their listings.
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("peldrun:artifact-updated", {
            detail: {
              source: "manual-mode",
              action: "workspace-deleted",
              workspace_id: workspaceId,
            },
          })
        );
      }

      return true;
    } catch (err) {
      const msg =
        err instanceof SourceControlApiError
          ? err.detail
          : "Failed to delete workspace.";
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
        // Task 1.5: if the user typed a preferred branch name in the
        // "Describe your changes" card, forward it. The backend uses
        // it ONLY when auto-branching triggers (target protected +
        // auto_create_feature_branch=true); otherwise it is ignored.
        preferred_branch_name:
          storeState.preferredBranchName.trim() || undefined,
        // Task 2.2: forward the owner-override decision. When True,
        // the backend skips auto-branching and issues the approval
        // ticket with scope="push_to_protected".
        force_protected_push: storeState.forceProtectedPush,
      });

      // The operation may have redirected the target branch (either
      // via auto-branching or via an explicit owner override that
      // kept the original target). `op.target_branch` reflects the
      // ACTUAL branch the push will target — update the store so
      // the review screen shows the truth.
      if (op.target_branch && op.target_branch !== branch) {
        useManualModeStore.getState().setBranch(op.target_branch);
      }

      // 2. Request the approval ticket (computes diff, binds it).
      const ticket = await sourceControlClient.requestOperationApproval(
        op.operation_id
      );

      // 3. Fetch the diff for display.
      const diff = await sourceControlClient.inspectWorkspaceDiff(workspaceId);

      // Task 2.1: Fix the "UI Lie".
      // The backend may have redirected the target branch (e.g.,
      // from `main` to `peldrun/manual-...`). The ticket response
      // contains the ACTUAL `target_branch` the operation will push to.
      // We MUST update the store so the UI reflects the truth,
      // otherwise the user sees `main` while the system pushes to
      // `peldrun/manual-...`.
      if (ticket.target_branch && ticket.target_branch !== branch) {
        useManualModeStore.getState().setBranch(ticket.target_branch);
      }

      // NOTE: setApproval does NOT change `step`. It stays
      // "reviewing" so the user can see the diff in
      // ManualDiffReview before proceeding.
      useManualModeStore
        .getState()
        .setApproval(
          op.operation_id,
          ticket.approval_id,
          diff,
          ticket.approval_scope
        );

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

  // --- Step 6: Request PR Approval ---
  const requestPRApproval = useCallback(async (): Promise<boolean> => {
    const { operationId, setStep, setError } = useManualModeStore.getState();
    if (!operationId) {
      setError("Operation is required.");
      return false;
    }
    setStep("creating-pr");
    setError(null);
    try {
      const ticket = await sourceControlClient.requestOperationApproval(
        operationId,
        "create_change_request"
      );
      useManualModeStore.setState({ approvalId: ticket.approval_id });
      setStep("pr-approving");
      return true;
    } catch (err) {
      const msg = err instanceof SourceControlApiError ? err.detail : "Failed to request PR approval.";
      setError(msg);
      return false;
    }
  }, []);

  // --- Step 7: Submit PR ---
  const submitPR = useCallback(async (): Promise<boolean> => {
    const { operationId, approvalId, repository, branch, prTitle, prBody, setStep, setError, setPrUrl } = useManualModeStore.getState();
    if (!operationId || !approvalId || !repository) {
      setError("Missing data for PR creation.");
      return false;
    }

    const targetBranch = repository.default_branch || "main";
    if (branch === targetBranch) {
      setError(`You pushed directly to ${targetBranch}. No pull request is needed.`);
      return false;
    }

    setError(null);
    try {
      await sourceControlClient.approveOperation(operationId, {
        approval_id: approvalId,
        user_id: "user",
      });

      const pr = await sourceControlClient.createChangeRequest(operationId, {
        repository_id: repository.repository_id,
        provider: repository.provider,
        source_branch: branch,
        target_branch: targetBranch,
        title: prTitle.trim() || `Update from ${branch}`,
        body: prBody.trim(),
        draft: false,
        approval_id: approvalId,
      });

      setPrUrl(pr.message);
      setStep("pr-created");
      return true;
    } catch (err) {
      const msg = err instanceof SourceControlApiError ? err.detail : "Failed to create PR.";
      setError(msg);
      return false;
    }
  }, []);

  return {
    createWorkspace,
    deleteWorkspace,
    inspectAndRequestApproval,
    approveAndPush,
    rejectApproval,
    requestPRApproval,
    submitPR,
  };
}
