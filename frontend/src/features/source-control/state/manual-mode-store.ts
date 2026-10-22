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
    diff: DiffSummaryResponse
  ) => void;
  clearApproval: () => void;

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
  setApproval: (operationId, approvalId, diff) =>
    set({
      operationId,
      approvalId,
      diff,
      error: null,
    }),

  clearApproval: () =>
    set({
      operationId: null,
      approvalId: null,
      diff: null,
      step: "editing",
    }),

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
