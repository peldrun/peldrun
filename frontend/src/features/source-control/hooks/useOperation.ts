// frontend/src/features/source-control/hooks/useOperation.ts

import { useState, useCallback, useEffect } from "react";
import { useSourceControlStore } from "../state/source-control-store";
import { sourceControlClient, SourceControlApiError } from "../api/source-control-client";
import { OperationResponse, DiffSummaryResponse } from "../api/schemas";

/**
 * Custom hook to manage the lifecycle of a single Source Control Operation.
 * Connects the UI components to the backend API and synchronizes state with Zustand.
 */
export function useOperation(operationId: string | null) {
  const {
    activeOperation,
    activeDiff,
    setActiveOperation,
    setActiveDiff,
    setError,
    setViewMode,
  } = useSourceControlStore();

  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isActionLoading, setIsActionLoading] = useState<boolean>(false);

  /**
   * Fetches the current operation state and its diff (if applicable).
   */
  const fetchOperation = useCallback(async (showLoader: boolean = true) => {
    if (!operationId) return;

    if (showLoader) setIsLoading(true);
    setError(null);

    try {
      // 1. Fetch operation details
      const operation = await sourceControlClient.getOperation(operationId);
      setActiveOperation(operation);

      // 2. If the operation is in review state or has a workspace, fetch the diff
      if (
        operation.current_state === "review_required" ||
        operation.current_state === "stale_review_required" ||
        operation.workspace_id
      ) {
        try {
          // If the backend requires a base_ref, it might be operation.base_sha.
          // Here we rely on the backend's default diff calculation for the workspace.
          const diff = await sourceControlClient.inspectWorkspaceDiff(
            operation.workspace_id as string
          );
          setActiveDiff(diff);
        } catch (diffErr) {
          // Distinguish two failure modes:
          //
          //   - 404 NOT_FOUND: the workspace no longer exists on
          //     disk. This is the common case after the chat that
          //     owned the operation was deleted. The operation
          //     record survives in the journal until the next
          //     `POST /operations/cleanup` call, so the card still
          //     renders — but there is no diff to review and no
          //     path to approve. Surface a clear, non-retryable
          //     error so the UI can disable the Approve/Reject
          //     buttons and show an actionable message.
          //
          //   - Any other error: transient failure (network blip,
          //     workspace not yet fully materialized, etc.). Keep
          //     the existing warn-and-continue behavior so a
          //     momentary hiccup does not permanently lock the
          //     review panel.
          if (
            diffErr instanceof SourceControlApiError &&
            diffErr.status === 404
          ) {
            setError({
              code: "WORKSPACE_GONE",
              message:
                "The workspace for this operation no longer exists on disk. " +
                "The chat that created it was deleted, so the operation " +
                "cannot be reviewed or approved. Start a new task to continue.",
              isRetryable: false,
            });
            setActiveDiff(null);
          } else {
            console.warn(
              "Could not fetch diff for operation:",
              diffErr
            );
            setActiveDiff(null);
          }
        }
      }
    } catch (err) {
      if (err instanceof SourceControlApiError) {
        if (err.status === 404) {
          setError({
            code: "NOT_FOUND",
            message: "This operation no longer exists or was removed.",
            isRetryable: false,
          });
        } else {
          setError({
            code: err.code,
            message: err.detail,
            isRetryable: true,
          });
        }
      } else {
        setError({
          code: "UNKNOWN_ERROR",
          message: "Failed to load operation details.",
          isRetryable: true,
        });
      }
    } finally {
      setIsLoading(false);
    }
  }, [operationId, setActiveOperation, setActiveDiff, setError]);

  // Fetch on mount or when operationId changes
  useEffect(() => {
    if (operationId) {
      fetchOperation();
    }
  }, [operationId, fetchOperation]);

  /**
   * Approves the operation and immediately triggers the push.
   * If the backend detects a stale review (412), it refreshes the state.
   */
  const approveAndPush = useCallback(async () => {
    if (!activeOperation || !activeOperation.approval_id || !activeOperation.workspace_id) {
      setError({
        code: "MISSING_CONTEXT",
        message: "Cannot approve: missing approval ticket or workspace ID.",
        isRetryable: false,
      });
      return false;
    }

    setIsActionLoading(true);
    setError(null);

    try {
      // 1. Grant approval (Consume ticket)
      await sourceControlClient.approveOperation(activeOperation.operation_id, {
        approval_id: activeOperation.approval_id,
        user_id: "user", // Defaulting for desktop local-first app
      });

      // 2. Execute the push
      await sourceControlClient.pushApprovedBranch(activeOperation.operation_id, {
        approval_id: activeOperation.approval_id,
        workspace_id: activeOperation.workspace_id,
        remote_name: "origin",
        set_upstream: true,
      });

      // 3. Refresh state to show "pushed" status
      await fetchOperation(false);
      return true;

    } catch (err) {
      if (err instanceof SourceControlApiError) {
        // Handle Stale Review (Remote moved)
        if (err.status === 412 || err.code === "REMOTE_STATE_CHANGED" || err.code === "CONFLICT") {
          setError({
            code: "REMOTE_STATE_CHANGED",
            message: "The remote branch has changed since you started reviewing. Please re-review the latest changes.",
            isRetryable: false,
          });
          // Re-fetch to get the new diff and state
          await fetchOperation(false);
        } else {
          setError({
            code: err.code,
            message: err.detail,
            isRetryable: true,
          });
        }
      } else {
        setError({
          code: "UNKNOWN_ERROR",
          message: "An unexpected error occurred during push.",
          isRetryable: true,
        });
      }
      return false;
    } finally {
      setIsActionLoading(false);
    }
  }, [activeOperation, fetchOperation, setError]);

  /**
   * Rejects the pending operation with a mandatory reason.
   */
  const rejectOperation = useCallback(async (reason: string) => {
    if (!activeOperation || !activeOperation.approval_id) {
      setError({
        code: "MISSING_CONTEXT",
        message: "Cannot reject: missing approval ticket.",
        isRetryable: false,
      });
      return false;
    }

    setIsActionLoading(true);
    setError(null);

    try {
      await sourceControlClient.rejectOperation(activeOperation.operation_id, {
        approval_id: activeOperation.approval_id,
        user_id: "user",
        reason: reason,
      });

      await fetchOperation(false);
      return true;
    } catch (err) {
      if (err instanceof SourceControlApiError) {
        setError({
          code: err.code,
          message: err.detail,
          isRetryable: true,
        });
      } else {
        setError({
          code: "UNKNOWN_ERROR",
          message: "Failed to reject the operation.",
          isRetryable: true,
        });
      }
      return false;
    } finally {
      setIsActionLoading(false);
    }
  }, [activeOperation, fetchOperation, setError]);

  /**
   * Creates a Pull/Merge Request after a successful push.
   */
  const createChangeRequest = useCallback(async (title: string, body: string, draft: boolean = true) => {
    if (!activeOperation) return false;

    setIsActionLoading(true);
    setError(null);

    try {
      await sourceControlClient.createChangeRequest(activeOperation.operation_id, {
        repository_id: activeOperation.repository_id,
        source_branch: activeOperation.target_branch, // In this context, target_branch was the feature branch pushed
        target_branch: "main", // TODO: Allow selecting PR target branch in UI
        title: title,
        body: body,
        draft: draft,
        provider: activeOperation.provider,
        // If the backend requires an approval_id for PR creation, we would need a new ticket here.
        // Based on current flow, PR creation might be a separate operation or use the same push ticket.
      });

      await fetchOperation(false);
      return true;
    } catch (err) {
      if (err instanceof SourceControlApiError) {
        setError({
          code: err.code,
          message: err.detail,
          isRetryable: true,
        });
      } else {
        setError({
          code: "UNKNOWN_ERROR",
          message: "Failed to create change request.",
          isRetryable: true,
        });
      }
      return false;
    } finally {
      setIsActionLoading(false);
    }
  }, [activeOperation, fetchOperation, setError]);

  return {
    // State
    operation: activeOperation,
    diff: activeDiff,
    isLoading,
    isActionLoading,

    // Actions
    refresh: () => fetchOperation(true),
    approveAndPush,
    rejectOperation,
    createChangeRequest,
  };
}
