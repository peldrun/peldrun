// frontend/src/features/source-control/state/source-control-store.ts

import { create } from "zustand";
import { OperationResponse, DiffSummaryResponse } from "../api/schemas";
import {
  WorkbenchPanelTab,
  SourceControlViewMode,
  SourceControlUiError,
} from "../types";

/**
 * Interface defining the Source Control Zustand store state and actions.
 */
interface SourceControlStoreState {
  // --- Panel & View State ---
  /** Indicates if the global right-side workbench panel is visible */
  isPanelOpen: boolean;

  /** The currently active tab in the right-side panel (e.g., 'preview', 'source-control') */
  activeTab: WorkbenchPanelTab;

  /** The specific view mode within the Source Control tab (idle, review, timeline) */
  viewMode: SourceControlViewMode;

  // --- Active Operation Context ---
  /** The ID of the operation currently loaded or being reviewed */
  activeOperationId: string | null;

  /** The full operation record fetched from the backend */
  activeOperation: OperationResponse | null;

  /** The diff summary fetched for the active operation */
  activeDiff: DiffSummaryResponse | null;

  // --- UI Error State ---
  /** Structured error object for UI rendering (e.g., 412 Stale Review, 401 Auth Required) */
  error: SourceControlUiError | null;

  // --- Actions ---
  /** Opens the workbench panel, optionally setting the active tab */
  openPanel: (tab?: WorkbenchPanelTab) => void;

  /** Closes the workbench panel */
  closePanel: () => void;

  /** Sets the active tab in the workbench panel */
  setActiveTab: (tab: WorkbenchPanelTab) => void;

  /** Sets the view mode within the Source Control tab */
  setViewMode: (mode: SourceControlViewMode) => void;

  /** Sets the active operation data and updates the view mode accordingly */
  setActiveOperation: (operation: OperationResponse | null) => void;

  /** Sets the active diff data */
  setActiveDiff: (diff: DiffSummaryResponse | null) => void;

  /** Sets an error to be displayed in the UI */
  setError: (error: SourceControlUiError | null) => void;

  /** Clears all active operation context and resets to idle state */
  resetOperationContext: () => void;
}

/**
 * Zustand store for managing the Source Control UI state.
 * This store is intentionally kept "dumb" (it does not fetch data).
 * Data fetching and backend interaction are handled by custom hooks (e.g., useOperation).
 */
export const useSourceControlStore = create<SourceControlStoreState>((set) => ({
  // --- Initial State ---
  isPanelOpen: false,
  activeTab: "preview",
  viewMode: "idle",
  activeOperationId: null,
  activeOperation: null,
  activeDiff: null,
  error: null,

  // --- Actions ---
  openPanel: (tab) =>
    set((state) => ({
      isPanelOpen: true,
      activeTab: tab ?? state.activeTab,
    })),

  closePanel: () => set({ isPanelOpen: false }),

  setActiveTab: (tab) => set({ activeTab: tab }),

  setViewMode: (mode) => set({ viewMode: mode }),

  setActiveOperation: (operation) =>
    set({
      activeOperation: operation,
      activeOperationId: operation ? operation.operation_id : null,
      // Automatically switch to review mode if the operation requires approval
      viewMode: operation
        ? operation.current_state === "review_required"
          ? "review"
          : "timeline"
        : "idle",
    }),

  setActiveDiff: (diff) => set({ activeDiff: diff }),

  setError: (error) => set({ error }),

  resetOperationContext: () =>
    set({
      viewMode: "idle",
      activeOperationId: null,
      activeOperation: null,
      activeDiff: null,
      error: null,
    }),
}));
