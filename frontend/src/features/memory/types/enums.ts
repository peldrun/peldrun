// src/features/memory/types/enums.ts

/**
 * String-literal enum definitions for the PELDRUN Memory subsystem.
 *
 * This module is the SINGLE SOURCE OF TRUTH for every closed-set of
 * string values that crosses the HTTP boundary. It mirrors the
 * backend's ``peldrun.memory.domain.enums`` module one-to-one, so
 * that any value accepted by the API has a matching TypeScript
 * type here -- and any value the frontend sends compiles only if
 * the backend will accept it.
 *
 * Design rules:
 *
 *   * Every enum is a ``const`` object paired with a derived union
 *     type. This gives BOTH a runtime value (for select dropdowns,
 *     default props, etc.) AND compile-time exhaustiveness checks.
 *
 *   * Values are the exact strings the backend emits/accepts. No
 *     translation layer -- translation lives in i18n labels keyed
 *     by these values (see ``features/memory/lib/format.ts``).
 *
 *   * The ``ALL_*`` arrays enumerate every value in a stable order.
 *     They are meant for UI selectors; iteration order is part of
 *     the contract and should not be shuffled casually.
 *
 *   * Type guards (``isMemoryType``, etc.) exist so that unknown
 *     values arriving from untrusted sources (URL params, LLM
 *     output, imported data) can be narrowed safely. They are
 *     intentionally strict: unknown -> false.
 *
 *   * No display strings live here. Labels, tooltips, and aria
 *     attributes are provided by i18next via ``t('memory.type.decision')``
 *     style keys.
 */

// ---------------------------------------------------------------------------
// MemoryType
// ---------------------------------------------------------------------------

/**
 * The nature of a memory entry -- what kind of knowledge it stores.
 *
 * Mirrors ``peldrun.memory.domain.enums.MemoryType``.
 *
 *   * ``semantic``   -- facts, definitions, domain knowledge.
 *   * ``episodic``   -- events, meetings, incidents with a time anchor.
 *   * ``procedural`` -- how-to steps, workflows, runbooks.
 *   * ``preference`` -- user likes/dislikes, style, tone.
 *   * ``decision``   -- an explicit choice the team committed to.
 */
export const MemoryType = {
  SEMANTIC: 'semantic',
  EPISODIC: 'episodic',
  PROCEDURAL: 'procedural',
  PREFERENCE: 'preference',
  DECISION: 'decision',
} as const;

export type MemoryType = (typeof MemoryType)[keyof typeof MemoryType];

/** All MemoryType values in UI display order. */
export const ALL_MEMORY_TYPES: readonly MemoryType[] = [
  MemoryType.SEMANTIC,
  MemoryType.EPISODIC,
  MemoryType.PROCEDURAL,
  MemoryType.PREFERENCE,
  MemoryType.DECISION,
] as const;

/** Narrow an arbitrary string to ``MemoryType``. */
export function isMemoryType(value: unknown): value is MemoryType {
  return (
    typeof value === 'string' &&
    (ALL_MEMORY_TYPES as readonly string[]).includes(value)
  );
}

// ---------------------------------------------------------------------------
// MemoryScope
// ---------------------------------------------------------------------------

/**
 * The boundary within which a memory entry is visible.
 *
 * Mirrors ``peldrun.memory.domain.enums.MemoryScope``. The four
 * scopes form a hierarchy of visibility, but NOT of precedence --
 * scope precedence in ranking is separate (see the ranker spec).
 *
 * Security note: the ``user`` scope has an additional constraint
 * enforced by the backend -- only ``source_type='explicit_user'``
 * can write into it. The frontend must respect this by never
 * offering another source type when ``scope_type === 'user'``.
 */
export const MemoryScope = {
  USER: 'user',
  PROJECT: 'project',
  CHAT: 'chat',
  AGENT: 'agent',
} as const;

export type MemoryScope = (typeof MemoryScope)[keyof typeof MemoryScope];

/** All MemoryScope values in UI display order (user first). */
export const ALL_MEMORY_SCOPES: readonly MemoryScope[] = [
  MemoryScope.USER,
  MemoryScope.PROJECT,
  MemoryScope.CHAT,
  MemoryScope.AGENT,
] as const;

/** Narrow an arbitrary string to ``MemoryScope``. */
export function isMemoryScope(value: unknown): value is MemoryScope {
  return (
    typeof value === 'string' &&
    (ALL_MEMORY_SCOPES as readonly string[]).includes(value)
  );
}

// ---------------------------------------------------------------------------
// MemoryMode
// ---------------------------------------------------------------------------

/**
 * The operational mode of the entire memory subsystem.
 *
 * Mirrors ``peldrun.memory.domain.enums.MemoryMode``.
 *
 *   * ``disabled``   -- no reads, no writes, no imports. The
 *                       settings page remains reachable so the
 *                       user can re-enable.
 *   * ``read_only``  -- reads and imports allowed; ordinary
 *                       writes (create/update/delete) rejected.
 *   * ``read_write`` -- full capability.
 *
 * Frontend consequence: gating UI on this enum is a hard requirement.
 * See ``features/memory/hooks/useMemoryMode.ts`` for derived helpers.
 */
export const MemoryMode = {
  DISABLED: 'disabled',
  READ_ONLY: 'read_only',
  READ_WRITE: 'read_write',
} as const;

export type MemoryMode = (typeof MemoryMode)[keyof typeof MemoryMode];

/** All MemoryMode values in ascending permissiveness. */
export const ALL_MEMORY_MODES: readonly MemoryMode[] = [
  MemoryMode.DISABLED,
  MemoryMode.READ_ONLY,
  MemoryMode.READ_WRITE,
] as const;

/** Narrow an arbitrary string to ``MemoryMode``. */
export function isMemoryMode(value: unknown): value is MemoryMode {
  return (
    typeof value === 'string' &&
    (ALL_MEMORY_MODES as readonly string[]).includes(value)
  );
}

// ---------------------------------------------------------------------------
// MemorySourceType
// ---------------------------------------------------------------------------

/**
 * Where a memory entry originated from.
 *
 * Mirrors ``peldrun.memory.domain.enums.MemorySourceType``.
 *
 *   * ``explicit_user`` -- the user clicked "Save to memory" (or
 *                          equivalent). The ONLY source type
 *                          permitted when ``scope_type === 'user'``.
 *   * ``agent_tool``    -- the agent invoked a memory tool.
 *   * ``extraction``    -- a background job extracted candidates
 *                          from conversation.
 *   * ``import``        -- restored from a package. NOT a valid
 *                          value for ``POST /api/memory`` (the
 *                          backend rejects it) -- only written by
 *                          the ``/import/apply`` path.
 */
export const MemorySourceType = {
  EXPLICIT_USER: 'explicit_user',
  AGENT_TOOL: 'agent_tool',
  EXTRACTION: 'extraction',
  IMPORT: 'import',
} as const;

export type MemorySourceType =
  (typeof MemorySourceType)[keyof typeof MemorySourceType];

/** All MemorySourceType values in UI display order. */
export const ALL_MEMORY_SOURCE_TYPES: readonly MemorySourceType[] = [
  MemorySourceType.EXPLICIT_USER,
  MemorySourceType.AGENT_TOOL,
  MemorySourceType.EXTRACTION,
  MemorySourceType.IMPORT,
] as const;

/**
 * Source types permitted on ``POST /api/memory``.
 *
 * Mirrors the backend's ``_ALLOWED_CREATE_SOURCE_TYPES`` whitelist.
 * The UI's "new entry" form MUST restrict its source-type selector
 * to these values -- ``import`` is deliberately absent because
 * imports go through ``/import/apply``.
 */
export const ALLOWED_CREATE_SOURCE_TYPES: readonly MemorySourceType[] = [
  MemorySourceType.EXPLICIT_USER,
  MemorySourceType.AGENT_TOOL,
  MemorySourceType.EXTRACTION,
] as const;

/** Narrow an arbitrary string to ``MemorySourceType``. */
export function isMemorySourceType(
  value: unknown,
): value is MemorySourceType {
  return (
    typeof value === 'string' &&
    (ALL_MEMORY_SOURCE_TYPES as readonly string[]).includes(value)
  );
}

// ---------------------------------------------------------------------------
// MemoryStatus
// ---------------------------------------------------------------------------

/**
 * The lifecycle state of a memory entry.
 *
 * Mirrors ``peldrun.memory.domain.enums.MemoryStatus``.
 *
 *   * ``active``     -- visible to retrieval.
 *   * ``archived``   -- soft-deleted; retained but not retrieved
 *                       (unless explicitly listed with status=all).
 *   * ``superseded`` -- replaced by a newer entry during a
 *                       SUPERSEDE write action; retained for audit.
 */
export const MemoryStatus = {
  ACTIVE: 'active',
  ARCHIVED: 'archived',
  SUPERSEDED: 'superseded',
} as const;

export type MemoryStatus =
  (typeof MemoryStatus)[keyof typeof MemoryStatus];

/** All MemoryStatus values in UI display order. */
export const ALL_MEMORY_STATUSES: readonly MemoryStatus[] = [
  MemoryStatus.ACTIVE,
  MemoryStatus.ARCHIVED,
  MemoryStatus.SUPERSEDED,
] as const;

/** Narrow an arbitrary string to ``MemoryStatus``. */
export function isMemoryStatus(value: unknown): value is MemoryStatus {
  return (
    typeof value === 'string' &&
    (ALL_MEMORY_STATUSES as readonly string[]).includes(value)
  );
}

// ---------------------------------------------------------------------------
// MemoryStatusFilter
// ---------------------------------------------------------------------------

/**
 * The status filter accepted by list and export endpoints.
 *
 * This is a separate concept from ``MemoryStatus``: the filter is
 * a query parameter ("show me active only" vs "show me everything"),
 * while ``MemoryStatus`` is the actual field on an entry.
 *
 *   * ``active`` -- only entries with status='active' (what would
 *                   actually be injected into a prompt).
 *   * ``all``    -- every entry regardless of status.
 *
 * Mirrors the backend's ``_VALID_STATUS_FILTERS``.
 */
export const MemoryStatusFilter = {
  ACTIVE: 'active',
  ALL: 'all',
} as const;

export type MemoryStatusFilter =
  (typeof MemoryStatusFilter)[keyof typeof MemoryStatusFilter];

/** All MemoryStatusFilter values in UI display order. */
export const ALL_MEMORY_STATUS_FILTERS: readonly MemoryStatusFilter[] = [
  MemoryStatusFilter.ALL,
  MemoryStatusFilter.ACTIVE,
] as const;

/** Narrow an arbitrary string to ``MemoryStatusFilter``. */
export function isMemoryStatusFilter(
  value: unknown,
): value is MemoryStatusFilter {
  return (
    typeof value === 'string' &&
    (ALL_MEMORY_STATUS_FILTERS as readonly string[]).includes(value)
  );
}

// ---------------------------------------------------------------------------
// RouterDecision (from the retrieval router)
// ---------------------------------------------------------------------------

/**
 * The deterministic classifier's decision for a single turn.
 *
 * Mirrors ``peldrun.memory.services.router`` decisions. Surfaced
 * on ``POST /api/memory/search`` responses as ``router_decision``.
 *
 *   * ``no_need``          -- the message is small talk; skip
 *                             retrieval entirely.
 *   * ``directed``         -- "remember X" / "what did we decide"
 *                             style; retrieve broadly.
 *   * ``precise``          -- "in this project..." style; use
 *                             narrow scopes only.
 *   * ``during_execution`` -- the message does not clearly demand
 *                             retrieval, but the agent may look up
 *                             on demand during tool use.
 */
export const RouterDecision = {
  NO_NEED: 'no_need',
  DIRECTED: 'directed',
  PRECISE: 'precise',
  DURING_EXECUTION: 'during_execution',
} as const;

export type RouterDecision =
  (typeof RouterDecision)[keyof typeof RouterDecision];

/** Narrow an arbitrary string to ``RouterDecision``. */
export function isRouterDecision(value: unknown): value is RouterDecision {
  return (
    typeof value === 'string' &&
    Object.values(RouterDecision).includes(value as RouterDecision)
  );
}

// ---------------------------------------------------------------------------
// Usage dimensions
// ---------------------------------------------------------------------------

/**
 * Time window accepted by every ``/api/memory/usage/*`` endpoint.
 *
 * Mirrors the backend's ``_ACCEPTED_PERIODS``.
 */
export const UsagePeriod = {
  DAY: 'day',
  WEEK: 'week',
  MONTH: 'month',
  ALL: 'all',
} as const;

export type UsagePeriod =
  (typeof UsagePeriod)[keyof typeof UsagePeriod];

/** All UsagePeriod values in ascending span. */
export const ALL_USAGE_PERIODS: readonly UsagePeriod[] = [
  UsagePeriod.DAY,
  UsagePeriod.WEEK,
  UsagePeriod.MONTH,
  UsagePeriod.ALL,
] as const;

/** Narrow an arbitrary string to ``UsagePeriod``. */
export function isUsagePeriod(value: unknown): value is UsagePeriod {
  return (
    typeof value === 'string' &&
    (ALL_USAGE_PERIODS as readonly string[]).includes(value)
  );
}

/**
 * Dimension accepted by ``GET /api/memory/usage/breakdown``.
 *
 * ``source`` is an accepted alias for ``scope`` on the wire; the
 * backend normalizes it. The frontend sends the canonical form
 * (``scope``) in normal use, but accepts both when parsing a
 * response (the echoed ``by`` field carries the normalized value).
 */
export const UsageBreakdownDimension = {
  SCOPE: 'scope',
  SOURCE: 'source',
  PROJECT: 'project',
  CHAT: 'chat',
} as const;

export type UsageBreakdownDimension =
  (typeof UsageBreakdownDimension)[keyof typeof UsageBreakdownDimension];

/** All UsageBreakdownDimension values in UI display order. */
export const ALL_USAGE_BREAKDOWN_DIMENSIONS: readonly UsageBreakdownDimension[] =
  [
    UsageBreakdownDimension.SCOPE,
    UsageBreakdownDimension.PROJECT,
    UsageBreakdownDimension.CHAT,
  ] as const;

/** Narrow an arbitrary string to ``UsageBreakdownDimension``. */
export function isUsageBreakdownDimension(
  value: unknown,
): value is UsageBreakdownDimension {
  return (
    typeof value === 'string' &&
    Object.values(UsageBreakdownDimension).includes(
      value as UsageBreakdownDimension,
    )
  );
}

/**
 * Bucket size accepted by ``GET /api/memory/usage/timeseries``.
 *
 * Mirrors the backend's ``_ACCEPTED_GRANULARITIES``.
 */
export const UsageGranularity = {
  DAY: 'day',
  WEEK: 'week',
  MONTH: 'month',
} as const;

export type UsageGranularity =
  (typeof UsageGranularity)[keyof typeof UsageGranularity];

/** All UsageGranularity values in ascending span. */
export const ALL_USAGE_GRANULARITIES: readonly UsageGranularity[] = [
  UsageGranularity.DAY,
  UsageGranularity.WEEK,
  UsageGranularity.MONTH,
] as const;

/** Narrow an arbitrary string to ``UsageGranularity``. */
export function isUsageGranularity(
  value: unknown,
): value is UsageGranularity {
  return (
    typeof value === 'string' &&
    (ALL_USAGE_GRANULARITIES as readonly string[]).includes(value)
  );
}

// ---------------------------------------------------------------------------
// Export formats
// ---------------------------------------------------------------------------

/**
 * Output format accepted by ``GET /api/memory/export``.
 *
 * Mirrors the backend's ``_ACCEPTED_FORMATS``.
 */
export const ExportFormat = {
  ZIP: 'zip',
  JSON: 'json',
} as const;

export type ExportFormat =
  (typeof ExportFormat)[keyof typeof ExportFormat];

/** All ExportFormat values in UI display order. */
export const ALL_EXPORT_FORMATS: readonly ExportFormat[] = [
  ExportFormat.ZIP,
  ExportFormat.JSON,
] as const;

/** Narrow an arbitrary string to ``ExportFormat``. */
export function isExportFormat(value: unknown): value is ExportFormat {
  return (
    typeof value === 'string' &&
    (ALL_EXPORT_FORMATS as readonly string[]).includes(value)
  );
}

// ---------------------------------------------------------------------------
// Import adapters
// ---------------------------------------------------------------------------

/**
 * The adapter_id values the backend ships as builtins.
 *
 * Adapters are dynamically registered on the backend, so the API
 * response may include additional ids. Treat any id outside this
 * set as opaque -- pass it through to the API verbatim, never
 * switch over it without a default branch.
 *
 * Mirrors the built-in registry entries documented on
 * ``GET /api/memory/import/adapters``.
 */
export const KnownImportAdapterId = {
  JSON: 'json',
  MARKDOWN: 'markdown',
  SQLITE: 'sqlite',
} as const;

export type KnownImportAdapterId =
  (typeof KnownImportAdapterId)[keyof typeof KnownImportAdapterId];

/** All built-in adapter ids in the registry's priority order. */
export const ALL_KNOWN_IMPORT_ADAPTER_IDS: readonly KnownImportAdapterId[] =
  [
    KnownImportAdapterId.JSON,
    KnownImportAdapterId.MARKDOWN,
    KnownImportAdapterId.SQLITE,
  ] as const;

/**
 * Narrow an arbitrary string to the KNOWN builtin adapter ids.
 *
 * Note: this returns ``false`` for third-party adapters that the
 * backend may register at runtime. Use it only to branch on
 * adapter-specific behavior that is guaranteed for the builtins;
 * for everything else, treat the id as opaque ``string``.
 */
export function isKnownImportAdapterId(
  value: unknown,
): value is KnownImportAdapterId {
  return (
    typeof value === 'string' &&
    (ALL_KNOWN_IMPORT_ADAPTER_IDS as readonly string[]).includes(value)
  );
}
