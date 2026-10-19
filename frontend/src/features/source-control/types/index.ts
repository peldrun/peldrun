// frontend/src/features/source-control/types/index.ts

/**
 * Re-export all API schemas and inferred types.
 * This provides a single, unified import point for the entire feature.
 */
export * from "../api/schemas";

// ----------------------------------------------------------------------
// UI Specific State & Types
// ----------------------------------------------------------------------

/**
 * The available tabs in the global Right-Side Workbench Panel.
 */
export type WorkbenchPanelTab = "preview" | "files" | "source-control" | "logs";

/**
 * The distinct modes for the Source Control tab in the Right Panel.
 */
export type SourceControlViewMode = "idle" | "review" | "timeline";

/**
 * Global UI state for the Source Control feature, managed by Zustand.
 */
export interface SourceControlUiState {
  /** Indicates if the right-side panel is physically visible */
  isPanelOpen: boolean;

  /** The currently active tab in the right-side panel */
  activeTab: WorkbenchPanelTab;

  /** The specific view mode within the Source Control tab */
  viewMode: SourceControlViewMode;

  /** The ID of the operation currently being reviewed or tracked */
  activeOperationId: string | null;

  /** Cached operation data to prevent unnecessary loading states */
  activeOperation: import("../api/schemas").OperationResponse | null;

  /** Cached diff data for the active operation */
  activeDiff: import("../api/schemas").DiffSummaryResponse | null; // Fixed: Changed to DiffSummaryResponse
}

/**
 * Structured error object for consistent UI rendering.
 */
export interface SourceControlUiError {
  code: string;
  message: string;
  isRetryable: boolean;
  actionLabel?: string;
  actionHandler?: () => void;
}

/**
 * Props for components that need to access the chat context.
 */
export interface ChatContextProps {
  chatId: string;
  projectId?: string | null;
}
