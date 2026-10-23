// frontend/src/features/source-control/state/manual-mode-store.ts

import { create } from "zustand";
import {
  DiffSummaryResponse,
  RepositoryResponse,
  WorkspaceResponse,
} from "../api/schemas";
import { ManualStep } from "../utils/manual-mode-steps";

interface ManualModeState {
  // --- State ---
  isActive: boolean;
  step: ManualStep;
  chatId: string | null;

  // Selection
  provider: string;
  repository: RepositoryResponse | null;
  branch: string;

  // Workspace
  workspaceId: string | null;

  // Operation + Approval
  operationId: string | null;
  approvalId: string | null;
  diff: DiffSummaryResponse | null;
  // Scope of the approval ticket (Task 1.3). Two values:
  //   - "push_branch"       → normal push, refused if target
  //                           turns out to be protected
  //   - "push_to_protected" → explicit consent to push to a
  //                           branch that matches the protected set
  // Set by `setApproval` when the store receives the ticket from
  // the backend. Used by ManualDiffReview to warn the user before
  // they click "Proceed to Approve".
  approvalScope: string;

  // Change description (optional). Collected in the "reviewing"
  // and "approving" steps and surfaced to the backend as
  // `metadata.title` / `metadata.description` on the operation.
  // These are the values the user would want as the eventual PR
  // title and body, so the PRs tab can pre-fill the form from the
  // operation's metadata.
  title: string;
  description: string;
  // Optional user-chosen branch name for the auto-created feature
  // branch (Task 1.5). Only used when the target branch is
  // protected AND settings have `auto_create_feature_branch=true`.
  // When empty, the backend generates a timestamped name like
  // `peldrun/manual-YYYYMMDD-HHMMSS`.
  preferredBranchName: string;
  // When true, the user has explicitly checked the "Owner override"
  // box on the Repo Selector screen. The backend will skip the
  // auto-branch redirect and issue an approval ticket with scope
  // "push_to_protected".
  forceProtectedPush: boolean;

  // Error
  error: string | null;

  // --- Actions ---
  activate: (chatId: string) => void;
  deactivate: () => void;
  setStep: (step: ManualStep) => void;

  setProvider: (provider: string) => void;
  setRepository: (repo: RepositoryResponse | null) => void;
  setBranch: (branch: string) => void;

  setWorkspace: (ws: WorkspaceResponse) => void;
  clearWorkspace: () => void;

  setApproval: (
    operationId: string,
    approvalId: string,
    diff: DiffSummaryResponse,
    approvalScope?: string
  ) => void;
  clearApproval: () => void;

  setTitle: (title: string) => void;
  setDescription: (description: string) => void;
  setPreferredBranchName: (preferredBranchName: string) => void;
  setForceProtectedPush: (force: boolean) => void;

  setError: (error: string | null) => void;
  reset: () => void;
}

const initialState = {
  isActive: false,
  step: "idle" as ManualStep,
  chatId: null as string | null,
  provider: "github",
  repository: null as RepositoryResponse | null,
  branch: "main",
  workspaceId: null as string | null,
  operationId: null as string | null,
  approvalId: null as string | null,
  diff: null as DiffSummaryResponse | null,
  approvalScope: "push_branch",
  title: "",
  description: "",
  preferredBranchName: "",
  forceProtectedPush: false,
  error: null as string | null,
};

export const useManualModeStore = create<ManualModeState>((set) => ({
  ...initialState,

  activate: (chatId) =>
    set((state) => {
      // If the user already has an active workspace in the SAME
      // chat, do not force them to re-clone. Jump straight to the
      // editing step so they can resume work.
      if (state.chatId === chatId && state.workspaceId) {
        return {
          isActive: true,
          step: "editing",
          // Clear all operation-specific state so a fresh workflow
          // does not inherit the previous attempt's data (Bug 3/5).
          operationId: null,
          approvalId: null,
          diff: null,
          approvalScope: "push_branch",
          title: "",
          description: "",
          preferredBranchName: "",
          // Do NOT reset `forceProtectedPush`. If the user chose
          // to override protection in this workspace, keep that
          // choice for subsequent workflows until they explicitly
          // change it or change the repository.
          error: null,
        };
      }
      return {
        ...initialState,
        isActive: true,
        chatId,
        step: "select-repo",
      };
    }),

  deactivate: () =>
    set({
      ...initialState,
      // Do NOT delete the workspace or files. Just close the UI.
    }),

  setStep: (step) => set({ step }),

  setProvider: (provider) => set({ provider }),
  setRepository: (repository) => set({ repository }),
  setBranch: (branch) => set({ branch }),

  setWorkspace: (ws) =>
    set({
      workspaceId: ws.workspace_id,
      branch: ws.current_branch,
      step: "editing",
      error: null,
    }),

  clearWorkspace: () =>
    set({
      workspaceId: null,
      step: "select-repo",
    }),

  // IMPORTANT: Do NOT switch step to "approving" here.
  // The user must review the diff in step "reviewing" first;
  // the transition to "approving" happens via an explicit
  // "Proceed to Approve & Push" button.
  setApproval: (operationId, approvalId, diff, approvalScope) =>
    set({
      operationId,
      approvalId,
      diff,
      approvalScope: approvalScope || "push_branch",
      error: null,
    }),

  clearApproval: () =>
    set({
      operationId: null,
      approvalId: null,
      diff: null,
      approvalScope: "push_branch",
      // The description belongs to the approved change; when the
      // approval is cleared, the description is cleared with it.
      // Keeping it around would make the next review silently
      // inherit the previous attempt's text.
      title: "",
      description: "",
      step: "editing",
    }),

  setTitle: (title) => set({ title }),
  setDescription: (description) => set({ description }),
  setPreferredBranchName: (preferredBranchName) =>
    set({ preferredBranchName }),
  setForceProtectedPush: (forceProtectedPush) =>
    set({ forceProtectedPush }),

  // Only move to the "error" step when SETTING an error. When
  // CLEARING it (`setError(null)`), leave the current step
  // untouched — otherwise every action that starts by clearing a
  // stale error (createWorkspace, inspectAndRequestApproval,
  // approveAndPush, rejectApproval) would silently reset the
  // workflow to "idle", hiding the panel even after the API call
  // succeeded.
  setError: (error) =>
    set((state) =>
      error
        ? { error, step: "error" as ManualStep }
        : { error }
    ),

  reset: () => set({ ...initialState }),
}));
