// src/features/memory/lib/validators.ts

/**
 * Zod schemas for every memory form.
 *
 * This module is the client-side mirror of the backend's
 * Pydantic validation. The rule is simple: **every field the
 * backend validates, the form validates too**. This gives the
 * user an inline error the moment they tab out of a field,
 * rather than a round-trip and a toast after they click Save.
 *
 * Bounds source
 * -------------
 *
 * Every numeric bound comes from `MemorySettingsBounds`
 * (`../types/settings.ts`) or one of the `MAX_*` / `EXPORT_*`
 * constants re-exported by `./constants.ts`. No hardcoded
 * numbers appear in this file. If the backend changes a bound,
 * the change flows through exactly one place.
 *
 * Message strategy
 * ----------------
 *
 * Validation messages are **i18n keys** (`'validation.required'`,
 * `'validation.tooLong'`, ...), not user-facing English
 * strings. Forms translate them at display time:
 *
 *     const issue = ...;  // from ZodError
 *     t(issue.message, { max: MAX_CONTENT_LENGTH });
 *
 * This keeps the schema i18n-agnostic and the translations in
 * `memory.json`, where the rest of the UI's text lives.
 *
 * Factory vs static schemas
 * -------------------------
 *
 * Most schemas are STATIC module-level constants. Two families
 * are FACTORIES that take `current: MemorySettings`:
 *
 *   * `makeSettingsPatchSchema` -- because the cross-field
 *     `reserved <= max` check must consider the CURRENT values
 *     for whichever budget field the patch does not supply.
 *
 *   * `makeProjectOverrideSchema` -- same reasoning.
 *
 * A static schema for these two would silently accept
 * `{max_tokens: 100}` when the current `reserved` is 500 -- a
 * state the backend will reject with a confusing
 * `invalid_settings` response. The factory catches it
 * client-side, pointing at the specific field.
 *
 * Scope resolution
 * ----------------
 *
 * The entry schemas accept EITHER an explicit scope pair
 * (`scope_type` + `scope_id`) OR a contextual scope
 * (`chat_id` or `project_id`). Both forms are legal on the
 * backend (see `_resolve_single_scope_for_write`), and the
 * refine clauses enforce "one or the other, not both" so the
 * caller never hits `400 multiple_scopes_ambiguous`.
 */

import { z } from 'zod';

import {
  ALLOWED_CREATE_SOURCE_TYPES,
  ALL_MEMORY_SCOPES,
  ALL_MEMORY_TYPES,
  ALL_USAGE_BREAKDOWN_DIMENSIONS,
  ALL_USAGE_GRANULARITIES,
  ALL_USAGE_PERIODS,
  MemoryMode,
  MemoryScope,
  MemorySourceType,
  type MemoryScope as MemoryScopeT,
  type MemorySourceType as MemorySourceTypeT,
  type MemoryType as MemoryTypeT,
} from '../types/enums';
import type {
  MemorySettings,
  MemoryTokenBudget,
} from '../types/settings';
import {
  EXPORT_MAX_ENTRIES,
  MAX_CONTENT_LENGTH,
  MAX_PAGE_SIZE,
  MAX_TAGS,
  MAX_TAG_LENGTH,
  MemorySettingsBounds,
} from './constants';

// ---------------------------------------------------------------------------
// Enum schemas
// ---------------------------------------------------------------------------
//
// One schema per closed-set on the wire. Each is derived from
// the `ALL_*` arrays so adding a new enum value requires
// touching ONLY `types/enums.ts` -- these schemas pick up the
// new value automatically.
//
// `z.nativeEnum` is used for the const-object enums because it
// preserves the exact literal union (not a widened `string`),
// which is what makes the parsed data usable by the rest of the
// typed codebase without a cast.

/** Literal schema for `MemoryType`. */
export const memoryTypeSchema = z.nativeEnum(
  Object.fromEntries(
    ALL_MEMORY_TYPES.map((v) => [v.toUpperCase(), v]),
  ) as Record<string, MemoryTypeT>,
) as unknown as z.ZodType<MemoryTypeT>;

/**
 * NOTE on the enum-schema construction above:
 *
 * `z.nativeEnum` expects an object whose VALUES are all the
 * strings we want to accept. The `Object.fromEntries(...)` maps
 * the exported `ALL_MEMORY_TYPES` array to that shape without
 * redeclaring the values. The `as unknown as` cast is required
 * because `nativeEnum`'s return type is keyed by the object's
 * keys (uppercase here), not by its values -- and we want the
 * parsed output to be the VALUE (`'semantic'`), which it is at
 * runtime. The cast is the price of the pattern.
 *
 * The alternative -- writing the literal array inline -- would
 * duplicate the source of truth and drift when a new value is
 * added. Verbose-but-safe was the wrong trade here; the
 * duplication was the bigger risk.
 */

/** Literal schema for `MemoryScope`. */
export const memoryScopeSchema = z.nativeEnum(
  Object.fromEntries(
    ALL_MEMORY_SCOPES.map((v) => [v.toUpperCase(), v]),
  ) as Record<string, MemoryScopeT>,
) as unknown as z.ZodType<MemoryScopeT>;

/** Literal schema for `MemoryMode`. */
export const memoryModeSchema = z.nativeEnum(MemoryMode);

/** Literal schema for `MemorySourceType` (all four values). */
export const memorySourceTypeSchema = z.nativeEnum(
  Object.fromEntries(
    Object.values(MemorySourceType).map((v) => [v.toUpperCase(), v]),
  ) as Record<string, MemorySourceTypeT>,
) as unknown as z.ZodType<MemorySourceTypeT>;

/**
 * Source types permitted on `POST /api/memory`.
 *
 * Excludes `'import'` -- the backend rejects it at the create
 * endpoint. The form's source-type selector uses this schema
 * (not `memorySourceTypeSchema`) to restrict the choices to the
 * ones the backend will actually accept.
 */
export const allowedCreateSourceTypeSchema = z.nativeEnum(
  Object.fromEntries(
    ALLOWED_CREATE_SOURCE_TYPES.map((v) => [v.toUpperCase(), v]),
  ) as Record<string, MemorySourceTypeT>,
) as unknown as z.ZodType<MemorySourceTypeT>;

/** Literal schema for the entry status FILTER ('active' | 'all'). */
export const memoryStatusFilterSchema = z.enum(['active', 'all']);

/** Literal schema for export format. */
export const exportFormatSchema = z.enum(['zip', 'json']);

/** Literal schema for usage period. */
export const usagePeriodSchema = z.enum(
  [...ALL_USAGE_PERIODS] as [string, ...string[]],
) as unknown as z.ZodType<(typeof ALL_USAGE_PERIODS)[number]>;

/** Literal schema for usage breakdown dimension (accepts 'source' alias). */
export const usageBreakdownDimensionSchema = z.enum(
  [...ALL_USAGE_BREAKDOWN_DIMENSIONS, 'source'] as unknown as [
    string,
    ...string[],
  ],
) as unknown as z.ZodType<
  (typeof ALL_USAGE_BREAKDOWN_DIMENSIONS)[number] | 'source'
>;

/** Literal schema for usage granularity. */
export const usageGranularitySchema = z.enum(
  [...ALL_USAGE_GRANULARITIES] as [string, ...string[]],
) as unknown as z.ZodType<(typeof ALL_USAGE_GRANULARITIES)[number]>;

// ---------------------------------------------------------------------------
// Primitive field schemas
// ---------------------------------------------------------------------------

/**
 * A single tag: trimmed, non-empty, bounded length.
 *
 * The `.trim()` transform means the parsed output is the
 * trimmed string -- forms do not need to trim on submit.
 */
const singleTagSchema = z
  .string()
  .trim()
  .min(1, { message: 'validation.tagEmpty' })
  .max(MAX_TAG_LENGTH, { message: 'validation.tagTooLong' });

/**
 * The full tag list for one entry.
 *
 * Bounds (max count, max per-tag length, uniqueness) all mirror
 * the backend's `MemoryEntry.create` validation. The `.refine`
 * for uniqueness is the ONLY rule that is not in the backend's
 * Pydantic field declaration -- it lives in the domain model.
 * Client-side, catching a duplicate tag inline is friendlier
 * than a full `MemoryValidationError` round-trip.
 */
const tagListSchema = z
  .array(singleTagSchema)
  .max(MAX_TAGS, { message: 'validation.tagsTooMany' })
  .refine(
    (arr) => new Set(arr).size === arr.length,
    { message: 'validation.tagDuplicate' },
  );

/** Memory content: trimmed, 1..MAX_CONTENT_LENGTH characters. */
const contentSchema = z
  .string()
  .trim()
  .min(1, { message: 'validation.contentRequired' })
  .max(MAX_CONTENT_LENGTH, { message: 'validation.tooLong' });

/** Importance: integer in 0..100. */
const importanceSchema = z
  .number({ message: 'validation.invalidType' })
  .int({ message: 'validation.notInteger' })
  .min(0, { message: 'validation.numberMin' })
  .max(100, { message: 'validation.numberMax' });

/**
 * Scope identifier: a non-empty string with a soft 500-char cap.
 *
 * The backend does not enforce an upper bound on `scope_id`
 * (any non-empty string that does not contain `:` is valid).
 * The soft cap here is a UX guard against pasting a huge
 * document into a scope field; 500 is far above any realistic
 * project or chat id.
 */
const scopeIdSchema = z
  .string()
  .trim()
  .min(1, { message: 'validation.scopeRequired' })
  .max(500, { message: 'validation.tooLong' });

// ---------------------------------------------------------------------------
// Token budget
// ---------------------------------------------------------------------------

/**
 * Schema for a COMPLETE `MemoryTokenBudget`.
 *
 * Used when reading a full budget back from the backend (never
 * for PATCH bodies, which are partial -- see
 * `makeSettingsPatchSchema`). All three fields are required and
 * the cross-field `reserved <= max` check runs on the complete
 * object.
 */
export const memoryTokenBudgetSchema = z
  .object({
    max_tokens: z
      .number({ message: 'validation.invalidType' })
      .int({ message: 'validation.notInteger' })
      .min(MemorySettingsBounds.token_budget_max_tokens.min, {
        message: 'validation.numberMin',
      })
      .max(MemorySettingsBounds.token_budget_max_tokens.max, {
        message: 'validation.numberMax',
      }),
    reserved_for_user_scope: z
      .number({ message: 'validation.invalidType' })
      .int({ message: 'validation.notInteger' })
      .min(0, { message: 'validation.numberMin' }),
    warn_threshold: z
      .number({ message: 'validation.invalidType' })
      .min(MemorySettingsBounds.token_budget_warn_threshold.min, {
        message: 'validation.numberMin',
      })
      .max(MemorySettingsBounds.token_budget_warn_threshold.max, {
        message: 'validation.numberMax',
      }),
  })
  .superRefine((val, ctx) => {
    if (val.reserved_for_user_scope > val.max_tokens) {
      ctx.addIssue({
        code: 'custom',
        path: ['reserved_for_user_scope'],
        message: 'validation.reservedExceedsMax',
      });
    }
  });

/**
 * Schema for a PARTIAL token-budget override.
 *
 * Every field is optional. The cross-field check is NOT
 * performed here -- the caller supplies the merged result to
 * the factory schemas (`makeSettingsPatchSchema`, ...), which
 * validate the merge against the current budget.
 */
const partialTokenBudgetSchema = z.object({
  max_tokens: z
    .number({ message: 'validation.invalidType' })
    .int({ message: 'validation.notInteger' })
    .min(MemorySettingsBounds.token_budget_max_tokens.min, {
      message: 'validation.numberMin',
    })
    .max(MemorySettingsBounds.token_budget_max_tokens.max, {
      message: 'validation.numberMax',
    })
    .optional(),
  reserved_for_user_scope: z
    .number({ message: 'validation.invalidType' })
    .int({ message: 'validation.notInteger' })
    .min(0, { message: 'validation.numberMin' })
    .optional(),
  warn_threshold: z
    .number({ message: 'validation.invalidType' })
    .min(MemorySettingsBounds.token_budget_warn_threshold.min, {
      message: 'validation.numberMin',
    })
    .max(MemorySettingsBounds.token_budget_warn_threshold.max, {
      message: 'validation.numberMax',
    })
    .optional(),
});

// ---------------------------------------------------------------------------
// Settings -- patch and project override factories
// ---------------------------------------------------------------------------

/**
 * The static set of field schemas shared by the settings PATCH
 * and the project-override PUT. Factories below wrap this base
 * with cross-field refinements against the CURRENT settings.
 */
const settingsFieldSchemas = {
  mode: memoryModeSchema.optional(),
  provider_id: z
    .string()
    .trim()
    .min(1, { message: 'validation.required' })
    .max(100, { message: 'validation.tooLong' })
    .optional(),
  default_limit: z
    .number({ message: 'validation.invalidType' })
    .int({ message: 'validation.notInteger' })
    .min(MemorySettingsBounds.default_limit.min, {
      message: 'validation.numberMin',
    })
    .max(MemorySettingsBounds.default_limit.max, {
      message: 'validation.numberMax',
    })
    .optional(),
  min_relevance_score: z
    .number({ message: 'validation.invalidType' })
    .min(MemorySettingsBounds.min_relevance_score.min, {
      message: 'validation.numberMin',
    })
    .max(MemorySettingsBounds.min_relevance_score.max, {
      message: 'validation.numberMax',
    })
    .optional(),
  boost_pinned: z
    .number({ message: 'validation.invalidType' })
    .min(MemorySettingsBounds.boost_pinned.min, {
      message: 'validation.numberMin',
    })
    .max(MemorySettingsBounds.boost_pinned.max, {
      message: 'validation.numberMax',
    })
    .optional(),
  token_budget: partialTokenBudgetSchema.optional(),
  auto_extraction_enabled: z.boolean().optional(),
  auto_extraction_min_turns: z
    .number({ message: 'validation.invalidType' })
    .int({ message: 'validation.notInteger' })
    .min(MemorySettingsBounds.auto_extraction_min_turns.min, {
      message: 'validation.numberMin',
    })
    .max(MemorySettingsBounds.auto_extraction_min_turns.max, {
      message: 'validation.numberMax',
    })
    .optional(),
};

/**
 * Strip `undefined` values from a partial token-budget override
 * before merging with the current budget.
 *
 * A form may submit `{ max_tokens: 100, reserved: undefined }`
 * when the caller spreads the whole draft object into the body.
 * Spreading that into `{ ...current, ...patch }` would overwrite
 * `reserved` with `undefined`, corrupting the merged value.
 * Filtering first avoids that trap.
 */
function cleanPartialBudget(
  budget: Partial<MemoryTokenBudget> | undefined,
): Partial<MemoryTokenBudget> {
  if (!budget) return {};
  const out: Partial<MemoryTokenBudget> = {};
  if (budget.max_tokens !== undefined) out.max_tokens = budget.max_tokens;
  if (budget.reserved_for_user_scope !== undefined) {
    out.reserved_for_user_scope = budget.reserved_for_user_scope;
  }
  if (budget.warn_threshold !== undefined) {
    out.warn_threshold = budget.warn_threshold;
  }
  return out;
}

/**
 * Build the settings PATCH schema for the current settings
 * snapshot.
 *
 * Factories are needed (not static) because two of the checks
 * depend on values the patch does not necessarily supply:
 *
 *   * `reserved <= max` on the MERGED token budget.
 *   * "at least one field changed" -- can only be known by
 *     comparing against `current`, but the schema only
 *     validates shape, so the form must call `isNoOpPatch`
 *     separately. Here we only enforce "patch is not
 *     structurally empty".
 *
 * The returned schema's parsed output is the patch itself
 * (`MemorySettingsPatch`-shaped), ready to POST.
 */
export function makeSettingsPatchSchema(current: MemorySettings) {
  return z
    .object(settingsFieldSchemas)
    .superRefine((patch, ctx) => {
      // Structural emptiness check.
      if (Object.keys(patch).length === 0) {
        ctx.addIssue({
          code: 'custom',
          message: 'validation.noFieldsToUpdate',
        });
        return;
      }

      // Merged-budget check.
      if (patch.token_budget) {
        const merged: MemoryTokenBudget = {
          ...current.token_budget,
          ...cleanPartialBudget(patch.token_budget),
        };
        if (merged.reserved_for_user_scope > merged.max_tokens) {
          ctx.addIssue({
            code: 'custom',
            path: ['token_budget', 'reserved_for_user_scope'],
            message: 'validation.reservedExceedsMax',
          });
        }
      }
    });
}

export type SettingsPatchInput = z.infer<
  ReturnType<typeof makeSettingsPatchSchema>
>;

/**
 * Build the project-override PUT schema for the current settings.
 *
 * Structurally identical to `makeSettingsPatchSchema` but
 * separate for two reasons:
 *
 *   1. Different empty-check message -- a PUT with no fields is
 *      an error on this endpoint too (`empty_override`), but the
 *      caller's INTENT differs ("I want to customize this
 *      project" vs "I want to change a global setting"). Keeping
 *      the schemas separate lets a future change diverge without
 *      breaking the other.
 *
 *   2. `project_overrides` is FORBIDDEN here. The base schema
 *      does not declare it, and `.strict()` would reject it if
 *      a caller sent one. Using `.strict()` (instead of the
 *      default `.strip()`) means a future addition to the
 *      settings shape that accidentally ends up in this schema
 *      is caught at parse time, not at the backend.
 */
export function makeProjectOverrideSchema(current: MemorySettings) {
  return z
    .object(settingsFieldSchemas)
    .strict()
    .superRefine((override, ctx) => {
      if (Object.keys(override).length === 0) {
        ctx.addIssue({
          code: 'custom',
          message: 'validation.noFieldsToUpdate',
        });
        return;
      }
      if (override.token_budget) {
        const merged: MemoryTokenBudget = {
          ...current.token_budget,
          ...cleanPartialBudget(override.token_budget),
        };
        if (merged.reserved_for_user_scope > merged.max_tokens) {
          ctx.addIssue({
            code: 'custom',
            path: ['token_budget', 'reserved_for_user_scope'],
            message: 'validation.reservedExceedsMax',
          });
        }
      }
    });
}

export type ProjectOverrideInput = z.infer<
  ReturnType<typeof makeProjectOverrideSchema>
>;

// ---------------------------------------------------------------------------
// Entry -- create
// ---------------------------------------------------------------------------

/**
 * The create-entry form schema.
 *
 * Accepts EITHER an explicit scope (`scope_type` + `scope_id`)
 * OR a contextual scope (`chat_id` or `project_id`). Enforces:
 *
 *   * one form or the other -- never both, never neither,
 *   * `user` scope requires `source_type = 'explicit_user'`
 *     (the backend's security rule),
 *   * `source_type` is one of the ALLOWED create values
 *     (excludes `'import'`).
 *
 * The `content` field is trimmed; `tags` are trimmed and
 * de-duplicated via the `singleTagSchema` plus the list's
 * uniqueness refine.
 */
export const memoryEntryCreateSchema = z
  .object({
    content: contentSchema,
    memory_type: memoryTypeSchema,
    source_type: allowedCreateSourceTypeSchema,
    scope_type: memoryScopeSchema.optional(),
    scope_id: scopeIdSchema.optional(),
    chat_id: z
      .string()
      .trim()
      .min(1, { message: 'validation.required' })
      .max(500, { message: 'validation.tooLong' })
      .optional(),
    project_id: z
      .string()
      .trim()
      .min(1, { message: 'validation.required' })
      .max(500, { message: 'validation.tooLong' })
      .optional(),
    importance: importanceSchema,
    tags: tagListSchema,
    is_pinned: z.boolean(),
  })
  .superRefine((val, ctx) => {
    // Rule 1: exactly one scope form, not both.
    const hasScopeType = val.scope_type !== undefined;
    const hasScopeId = val.scope_id !== undefined;
    const hasChat = val.chat_id !== undefined;
    const hasProject = val.project_id !== undefined;
    const hasContextual = hasChat || hasProject;

    if (hasScopeType !== hasScopeId) {
      ctx.addIssue({
        code: 'custom',
        path: hasScopeType ? ['scope_id'] : ['scope_type'],
        message: 'validation.scopeRequired',
      });
      return;
    }

    const hasExplicit = hasScopeType && hasScopeId;
    if (!hasExplicit && !hasContextual) {
      ctx.addIssue({
        code: 'custom',
        message: 'validation.scopeRequired',
      });
      return;
    }
    if (hasExplicit && hasContextual) {
      ctx.addIssue({
        code: 'custom',
        message: 'validation.scopeRequired',
      });
      return;
    }

    // Rule 2: user scope requires explicit_user source.
    if (
      hasExplicit &&
      val.scope_type === MemoryScope.USER &&
      val.source_type !== MemorySourceType.EXPLICIT_USER
    ) {
      ctx.addIssue({
        code: 'custom',
        path: ['source_type'],
        message: 'errors.user_scope_requires_explicit',
      });
    }
  });

export type MemoryEntryCreateInput = z.infer<
  typeof memoryEntryCreateSchema
>;

// ---------------------------------------------------------------------------
// Entry -- update
// ---------------------------------------------------------------------------

/**
 * The update-entry form schema.
 *
 * All fields are optional, but at least one must be present.
 * `content` (when present) must be non-empty; tags follow the
 * same validation as create. `memory_type` can be changed here
 * (the backend's `_UpdateBody` accepts it) but the entry's SCOPE
 * cannot -- use `promote` to copy into user scope.
 */
export const memoryEntryUpdateSchema = z
  .object({
    content: contentSchema.optional(),
    importance: importanceSchema.optional(),
    tags: tagListSchema.optional(),
    is_pinned: z.boolean().optional(),
    memory_type: memoryTypeSchema.optional(),
  })
  .refine(
    (val) => Object.values(val).some((v) => v !== undefined),
    { message: 'validation.noFieldsToUpdate' },
  );

export type MemoryEntryUpdateInput = z.infer<
  typeof memoryEntryUpdateSchema
>;

// ---------------------------------------------------------------------------
// List filters
// ---------------------------------------------------------------------------

/**
 * The browse-page filter schema.
 *
 * `memory_type` and `is_pinned` allow `null` for "no filter" --
 * matching the params interface in `api/keys.ts`. The `scope`
 * field is the wire-form `<type>:<id>` string.
 */
export const memoryListFiltersSchema = z.object({
  scope: z
    .string()
    .trim()
    .min(1, { message: 'validation.scopeRequired' })
    .regex(/^[a-z]+:.+$/, { message: 'validation.scopeRequired' }),
  status: memoryStatusFilterSchema,
  memory_type: memoryTypeSchema.nullable(),
  is_pinned: z.boolean().nullable(),
  limit: z
    .number({ message: 'validation.invalidType' })
    .int({ message: 'validation.notInteger' })
    .min(1, { message: 'validation.numberMin' })
    .max(MAX_PAGE_SIZE, { message: 'validation.numberMax' }),
  offset: z
    .number({ message: 'validation.invalidType' })
    .int({ message: 'validation.notInteger' })
    .min(0, { message: 'validation.numberMin' }),
});

export type MemoryListFiltersInput = z.infer<
  typeof memoryListFiltersSchema
>;

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------

/**
 * Target scope for an import operation.
 *
 * Simpler than the entry-create scope refinement -- the import
 * endpoint does NOT accept contextual scopes (chat_id /
 * project_id), only an explicit pair. So the schema requires
 * both fields unconditionally.
 */
export const importTargetScopeSchema = z.object({
  scope_type: memoryScopeSchema,
  scope_id: scopeIdSchema,
});

export type ImportTargetScopeInput = z.infer<
  typeof importTargetScopeSchema
>;

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

/**
 * The export form schema.
 *
 * `limit` is capped at `EXPORT_MAX_ENTRIES`; values above are
 * rejected client-side rather than silently clamped by the
 * backend (so the user knows why their "10000 entries" request
 * is being rejected when they actually asked for 99999).
 */
export const exportQuerySchema = z.object({
  scope_type: memoryScopeSchema,
  scope_id: scopeIdSchema,
  format: exportFormatSchema,
  status: memoryStatusFilterSchema,
  limit: z
    .number({ message: 'validation.invalidType' })
    .int({ message: 'validation.notInteger' })
    .min(1, { message: 'validation.numberMin' })
    .max(EXPORT_MAX_ENTRIES, { message: 'validation.numberMax' }),
});

export type ExportQueryInput = z.infer<typeof exportQuerySchema>;

// ---------------------------------------------------------------------------
// Usage
// ---------------------------------------------------------------------------

/**
 * The usage-dashboard filter schema.
 *
 * Single shape covering all three usage views (summary,
 * breakdown, timeseries) -- the caller ignores the fields it
 * does not need. Validation is permissive because the backend
 * accepts a lot here (any period string, no cross-field checks
 * between period and granularity).
 */
export const usageQuerySchema = z.object({
  period: usagePeriodSchema,
  by: usageBreakdownDimensionSchema.optional(),
  granularity: usageGranularitySchema.optional(),
  chat_id: z
    .string()
    .trim()
    .max(500, { message: 'validation.tooLong' })
    .nullable()
    .optional(),
  project_id: z
    .string()
    .trim()
    .max(500, { message: 'validation.tooLong' })
    .nullable()
    .optional(),
});

export type UsageQueryInput = z.infer<typeof usageQuerySchema>;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Flatten a `ZodError` into a `Record<pathKey, i18nMessage>`.
 *
 * `pathKey` is the dot-joined path (`"token_budget.max_tokens"`)
 * or `"_root"` for object-level issues. The first issue per
 * path wins -- a form shows one message per field, and the
 * first is usually the most specific.
 *
 * Forms use the result like:
 *
 *     const result = schema.safeParse(values);
 *     if (!result.success) {
 *       const errors = zodErrorToFieldMap(result.error);
 *       setErrors(errors);  // react-hook-form-compatible shape
 *     }
 *
 * The message is the raw i18n KEY. The form translates it with
 * `t(message)` before display, so the schema stays
 * language-agnostic.
 */
export function zodErrorToFieldMap(
  error: z.ZodError,
): Record<string, string> {
  const map: Record<string, string> = {};
  for (const issue of error.issues) {
    const key =
      issue.path.length > 0 ? issue.path.join('.') : '_root';
    if (!(key in map)) {
      map[key] = issue.message;
    }
  }
  return map;
}

/**
 * Return every issue on a specific field, in order.
 *
 * Useful for list-style validation UIs (e.g. an errors panel
 * beneath a nested editor) that want to show more than the
 * first error per field. Most forms use `zodErrorToFieldMap`
 * instead; this is for the "detailed" case.
 */
export function zodIssuesForField(
  error: z.ZodError,
  fieldPath: string | string[],
): string[] {
  const target = Array.isArray(fieldPath)
    ? fieldPath.join('.')
    : fieldPath;
  return error.issues
    .filter((issue) => issue.path.join('.') === target)
    .map((issue) => issue.message);
}
