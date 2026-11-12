// src/features/memory/index.ts

/**
 * Public surface of the PELDRUN Memory feature.
 *
 * Consumers outside `src/features/memory/` SHOULD import from
 * this module rather than reaching into internals:
 *
 *     import {
 *       ModeBadge,
 *       EntryCard,
 *       useMemorySettings,
 *       memoryKeys,
 *       MemoryApiError,
 *     } from '@/features/memory';
 *
 * This gives one stable path even as files are moved, split,
 * or merged inside the feature.
 *
 * What this module re-exports
 * ---------------------------
 *
 *   * **types** -- the full public type surface (entry, settings,
 *     responses, errors, import-export, enums). Types barrel in
 *     `types/index.ts` already re-exports everything, so this
 *     module is a thin pass-through.
 *
 *   * **api** -- `memoryClient`, `memoryKeys`, and the public
 *     hooks (`useMemorySettings`, `useEntryList`, `useCreateEntry`,
 *     ...). Internal helpers (`memoryFetch`, dispatchers) are
 *     NOT re-exported -- they are implementation details.
 *
 *   * **stores** -- the two Zustand stores and their exported
 *     types (`BrowseFilters`, `DEFAULT_FILTERS`, ...).
 *
 *   * **lib** -- shared constants (`MAX_TAGS`, ...) and helpers
 *     (`cn`, `zodErrorToFieldMap`, `formatDateTime`, ...).
 *
 *   * **components** -- every UI component. Components are
 *     re-exported individually (not via `export *`) so the
 *     public list is auditable: adding a new component means
 *     editing this file, which forces a conscious decision
 *     about whether it belongs on the public surface.
 *
 * What this module does NOT re-export
 * -----------------------------------
 *
 *   * **hooks** -- the feature-level hooks (`useMemoryMode`,
 *     `useEntryDraft`, `useMemoryList`, `useScopeFromContext`,
 *     `useKeyboardShortcuts`). These are currently used only
 *     inside the feature, and re-exporting them would commit
 *     to a public API that is still evolving. A caller that
 *     needs them can import directly from
 *     `features/memory/hooks/<name>`.
 *
 *   * **`MemoryLayoutSkeleton`** -- internal, not for external
 *     use.
 *
 *   * **Zod schemas** -- `lib/validators.ts` exports them for
 *     the forms; they are not re-exported here because the
 *     schemas are not part of the public contract. Callers
 *     that need client-side validation import directly.
 *
 * Note: an external re-export barrel is only valuable for
 * groups whose consumers ACTUALLY live outside `features/memory`
 * (the chat composer's `SimilarMemoriesPanel`, for instance).
 * Anything used only within the feature imports from relative
 * paths as it already does.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type {
  // Entity
  MemoryEntry,
  MemoryProvenance,
  MemoryScopeRef,
  MemoryScopeRefString,
  MemoryEntryDraft,
  // Settings
  MemorySettings,
  MemoryTokenBudget,
  MemoryProjectOverride,
  MemorySettingsPatch,
  MemorySettingsOverlay,
  // Errors
  MemoryApiError as MemoryApiErrorType,
  MemoryFieldError,
  KnownMemoryErrorType,
  // Import/export
  ImportFileInfo,
  ImportFileValidation,
  ImportFileRejectionReason as ImportFileRejectionReasonType,
  ImportMultipartRequest,
  ImportJsonRequest,
  ImportRequest,
  ImportTargetScope,
  ImportWizardStep,
  ExportQueryParams,
  ExportDownloadResult,
  ExportJsonPreview,
  // Responses
  MemoryProjectSummary,
  ProjectStats,
  ImportAdapter,
  ImportPreview,
  ImportApplyResult,
  UsageBreakdownBucket,
  UsageTimeseriesPoint,
} from './types';

export {
  // Enums (value exports)
  MemoryType,
  MemoryScope,
  MemoryMode,
  MemorySourceType,
  MemoryStatus,
  MemoryStatusFilter,
  RouterDecision,
  UsagePeriod,
  UsageBreakdownDimension,
  UsageGranularity,
  ExportFormat,
  KnownImportAdapterId,
  // Scope helpers
  formatScopeRef,
  parseScopeRef,
  entryScopeRef,
  // Error class + classifier
  MemoryApiError,
  classifyMemoryError,
  uxHintFor,
  isModeError,
  isValidationError,
  isTransientError,
} from './types';

// ---------------------------------------------------------------------------
// API layer
// ---------------------------------------------------------------------------

export { memoryClient } from './api/client';
export { memoryKeys, isMemoryQueryKey } from './api/keys';

export {
  useMemorySettingsQuery,
  useMemorySettings,
  useProjectSettings,
  useEntryList,
  useEntryDetail,
  useProjectsList,
  useProjectStats,
  useUsageSummary,
  useUsageBreakdown,
  useUsageTimeseries,
  useImportAdapters,
} from './api/queries';

export {
  usePatchSettings,
  usePutProjectOverride,
  useDeleteProjectOverride,
  useCreateEntry,
  useUpdateEntry,
  useDeleteEntry,
  usePinEntry,
  usePromoteEntry,
  useSearchMemories,
  useBulkArchive,
  useBulkPurge,
  useImportPreview,
  useImportApply,
  useExportZip,
  useExportJson,
} from './api/mutations';

// ---------------------------------------------------------------------------
// Stores
// ---------------------------------------------------------------------------

export {
  useMemoryUIStore,
  DEFAULT_FILTERS,
} from './stores/memory-ui-store';
export type { BrowseFilters } from './stores/memory-ui-store';

export { useMemoryDraftStore } from './stores/memory-draft-store';

// ---------------------------------------------------------------------------
// Constants and helpers
// ---------------------------------------------------------------------------

export {
  MAX_CONTENT_LENGTH,
  MAX_TAGS,
  MAX_TAG_LENGTH,
  DEFAULT_PAGE_SIZE,
  MAX_PAGE_SIZE,
  DEFAULT_IMPORTANCE,
  DEFAULT_IS_PINNED,
  HARD_DELETE_CONFIRM_TOKEN,
  TOAST_DURATION_MS,
  SEARCH_DEBOUNCE_MS,
  SEARCH_MIN_QUERY_LENGTH,
  IMPORT_MAX_UPLOAD_BYTES,
  EXPORT_MAX_ENTRIES,
  EXPORT_DEFAULT_ENTRIES,
  MemorySettingsBounds,
} from './lib/constants';

export { cn } from './lib/cn';

export {
  zodErrorToFieldMap,
  zodIssuesForField,
} from './lib/validators';

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

export { ModeBadge } from './components/ModeBadge';
export { ScopeBadge } from './components/ScopeBadge';
export { TypeBadge } from './components/TypeBadge';
export { ImportanceSlider, importanceTier } from './components/ImportanceSlider';
export { TagsInput } from './components/TagsInput';
export { EmptyState } from './components/EmptyState';
export { ConfirmDangerDialog } from './components/ConfirmDangerDialog';
export { EntryCard } from './components/EntryCard';
export { EntryList } from './components/EntryList';
export { EntryFilters } from './components/EntryFilters';
export { EntryEditor } from './components/EntryEditor';
export { SettingsForm } from './components/SettingsForm';
export { ProjectOverrideForm } from './components/ProjectOverrideForm';
export { ProjectCard } from './components/ProjectCard';
export { ProjectStatsPanel } from './components/ProjectStatsPanel';
export { BulkActionsBar } from './components/BulkActionsBar';
export { SimilarMemoriesPanel } from './components/SimilarMemoriesPanel';
export { ImportDropZone } from './components/ImportDropZone';
export { ImportPreviewTable } from './components/ImportPreviewTable';
export { ExportForm } from './components/ExportForm';
export { UsageKpiCards } from './components/UsageKpiCards';
export { UsageBreakdownChart } from './components/UsageBreakdownChart';
export { UsageTimeseriesChart } from './components/UsageTimeseriesChart';
