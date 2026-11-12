// src/features/memory/types/settings.ts

/**
 * Memory subsystem configuration types.
 *
 * This module mirrors ``peldrun.memory.domain.settings`` one-to-one.
 * Everything here is snake_case to match the wire payload produced
 * by the backend's ``model_dump(mode="json")`` -- there is no
 * transformation layer (see ``entry.ts`` for the same policy).
 *
 * Boundary with ``responses.ts``:
 *
 *   * THIS module -- pure domain shapes (the ``MemorySettings``
 *     object and its sub-parts).
 *
 *   * ``responses.ts`` -- HTTP envelopes that WRAP these shapes
 *     (``{status: 'ok', operation: 'settings_get', settings: {...}}``).
 *
 * Keeping the two separate means UI logic can consume
 * ``MemorySettings`` directly (e.g. as a prop type) without
 * threading through the envelope, and endpoint callers can
 * narrow to the envelope without depending on domain details
 * they don't care about.
 *
 * Constraint source of truth
 * --------------------------
 *
 * Every numeric range below (``default_limit`` 1..50,
 * ``max_tokens`` 64..32768, etc.) mirrors the Pydantic
 * ``Field(ge=..., le=...)`` declarations on the backend's
 * ``MemorySettings`` and ``MemoryTokenBudget`` models. These
 * bounds are exported as constants below so that:
 *
 *   * ``features/memory/lib/validators.ts`` can compose Zod
 *     schemas from the same constants, guaranteeing client-side
 *     pre-validation matches server-side post-validation.
 *
 *   * ``SettingsForm`` can build ``<input min= max=>`` attributes
 *     from them without hardcoding literals.
 *
 *   * A backend change to a bound that is not reflected here
 *     becomes a visible bug in the first validation attempt
 *     (server rejects a value the client thought was valid),
 *     not a silent data corruption.
 */

import type { MemoryMode } from './enums';

// ---------------------------------------------------------------------------
// Constraint constants
// ---------------------------------------------------------------------------

/**
 * Numeric bounds mirrored from the backend's Pydantic
 * ``Field(ge=..., le=...)`` declarations.
 *
 * Grouped here (not in ``constants.ts``) because they are part of
 * the settings contract, not generic UI constants. ``constants.ts``
 * re-exports them for convenience; consumers may import from
 * either module.
 */
export const MemorySettingsBounds = {
  default_limit: { min: 1, max: 50 },
  min_relevance_score: { min: 0.0, max: 1.0 },
  boost_pinned: { min: 1.0, max: 10.0 },
  auto_extraction_min_turns: { min: 1, max: 100 },
  token_budget_max_tokens: { min: 64, max: 32768 },
  token_budget_warn_threshold: { min: 0.0, max: 1.0 },
} as const;

// ---------------------------------------------------------------------------
// Token budget
// ---------------------------------------------------------------------------

/**
 * Token allocation for retrieved memory injected into a prompt.
 *
 * Mirrors ``peldrun.memory.domain.settings.MemoryTokenBudget``.
 *
 * The budget fitter (see the spec's retrieval pipeline, stage 4)
 * uses these numbers to decide which retrieved entries survive.
 *
 * Field-by-field:
 *
 *   * ``max_tokens``               -- hard ceiling on total tokens
 *                                     spent on the memory section
 *                                     of the system prompt.
 *
 *   * ``reserved_for_user_scope``  -- Phase-1 reservation: this
 *                                     many tokens are set aside
 *                                     for ``user``-scope entries
 *                                     BEFORE any other scope is
 *                                     fitted. Prevents a chatty
 *                                     project from starving the
 *                                     user's own preferences.
 *                                     MUST be <= ``max_tokens``.
 *
 *   * ``warn_threshold``           -- fraction of ``max_tokens``
 *                                     above which the usage
 *                                     dashboard emits a warning
 *                                     ("you are near the budget").
 *                                     Typical: 0.8.
 *
 * The ``reserved_for_user_scope <= max_tokens`` invariant is
 * enforced by the backend's Pydantic validator; the client-side
 * Zod schema MUST enforce the same before submission (see
 * ``features/memory/lib/validators.ts``).
 */
export interface MemoryTokenBudget {
  max_tokens: number;
  reserved_for_user_scope: number;
  warn_threshold: number;
}

/**
 * A "settings overlay" -- a partial settings object in which
 * ``token_budget`` is ALSO partial.
 *
 * This is the shape accepted by ``computeEffectiveOverride`` and
 * ``mergeProjectSettings``. It intentionally differs from
 * ``Partial<MemorySettings>``, which would force callers to
 * supply a COMPLETE ``MemoryTokenBudget`` when they only want to
 * override one of its sub-fields (e.g. raise ``max_tokens`` but
 * leave ``reserved_for_user_scope`` at global).
 *
 * Both ``MemoryProjectOverride`` (per-project PUT body) and
 * ``MemorySettingsPatch`` (global PATCH body) are structurally
 * assignable to this type. Keeping them as separate named
 * interfaces documents the INTENT of each call site, while
 * routing both through this shared shape keeps the helpers
 * usable from either. TS structural typing makes the
 * assignment free -- no runtime cast, no wrapper.
 */
export interface MemorySettingsOverlay {
  mode?: MemoryMode;
  provider_id?: string;
  default_limit?: number;
  min_relevance_score?: number;
  boost_pinned?: number;
  token_budget?: Partial<MemoryTokenBudget>;
  auto_extraction_enabled?: boolean;
  auto_extraction_min_turns?: number;
}

// ---------------------------------------------------------------------------
// Project override (delta form)
// ---------------------------------------------------------------------------

/**
 * A per-project delta applied on top of the global settings.
 *
 * Mirrors the ``project_overrides[project_id]`` value.
 *
 * Design:
 *
 *   * Every field is OPTIONAL. Absent fields inherit from the
 *     global settings -- the ``effective_for_project`` merge on
 *     the backend only walks fields that are present here.
 *
 *   * ``project_overrides`` itself is FORBIDDEN here -- a
 *     per-project override cannot contain a nested
 *     ``project_overrides`` map. The backend's
 *     ``MemorySettings`` validator rejects the nesting, and the
 *     memory_settings router has a defense-in-depth check for it.
 *
 *   * This is the shape accepted by
 *     ``PUT /api/memory/settings/projects/{id}``.
 *
 * Notes on what the API returns
 * -----------------------------
 *
 * The ``GET /api/memory/settings/projects/{id}`` response's
 * ``override`` field is typed as ``Partial<MemorySettings>`` (see
 * ``responses.ts``), not as this interface -- because the backend
 * MAY return a full settings object (with defaults filled in) as
 * the override, depending on how ``MemorySettings`` stores the
 * delta. This interface describes what the frontend SENDS in a
 * ``PUT`` body, which is unambiguously delta-only.
 *
 * If you need to know which fields are actually different from
 * global, use ``computeEffectiveOverride`` below rather than
 * trusting ``Object.keys`` of the response's ``override``.
 */
export interface MemoryProjectOverride {
  mode?: MemoryMode;
  provider_id?: string;
  default_limit?: number;
  min_relevance_score?: number;
  boost_pinned?: number;
  token_budget?: Partial<MemoryTokenBudget>;
  auto_extraction_enabled?: boolean;
  auto_extraction_min_turns?: number;
}

// ---------------------------------------------------------------------------
// Settings patch (global PATCH body)
// ---------------------------------------------------------------------------

/**
 * The shape accepted by ``PATCH /api/memory/settings``.
 *
 * Identical field set to ``MemoryProjectOverride`` at the type
 * level, but semantically distinct:
 *
 *   * ``MemoryProjectOverride`` -- "customize these fields for
 *     ONE project".
 *
 *   * ``MemorySettingsPatch``  -- "change these fields GLOBALLY,
 *     for every project that has no override".
 *
 * Kept as separate named types (rather than a type alias) so that
 * handler signatures read as their intent:
 * ``patchSettings(patch: MemorySettingsPatch)`` vs
 * ``putProjectOverride(id, override: MemoryProjectOverride)``.
 *
 * Deliberately EXCLUDES ``project_overrides`` -- the whitelist on
 * the backend's ``_PATCHABLE_FIELDS`` omits it because per-project
 * overrides are managed by the dedicated endpoints, not by a bulk
 * PATCH. Sending it via PATCH would bypass per-project merge
 * semantics.
 *
 * An EMPTY patch (``{}``) is rejected by the backend (400
 * ``empty_update``). The UI must guard against sending an empty
 * patch -- see ``SettingsForm`` for the "nothing changed"
 * short-circuit.
 */
export interface MemorySettingsPatch {
  mode?: MemoryMode;
  provider_id?: string;
  default_limit?: number;
  min_relevance_score?: number;
  boost_pinned?: number;
  token_budget?: Partial<MemoryTokenBudget>;
  auto_extraction_enabled?: boolean;
  auto_extraction_min_turns?: number;
}

// ---------------------------------------------------------------------------
// Global settings (full shape)
// ---------------------------------------------------------------------------

/**
 * The complete memory subsystem configuration.
 *
 * Mirrors ``peldrun.memory.domain.settings.MemorySettings``.
 *
 * Every field has a backend default, so ``MemorySettings`` is
 * always fully populated -- partial states exist only DURING the
 * editing process (as a ``MemorySettingsPatch`` or a
 * ``MemoryProjectOverride``).
 *
 * Field notes (deeper per-field contract lives on the linked
 * sub-interfaces):
 *
 *   * ``mode`` -- the master switch. Every UI gate derives from
 *     ``mode``. See ``useMemoryMode`` for the derived helpers
 *     (``canRead``, ``canWrite``, ``isDisabled``).
 *
 *   * ``provider_id`` -- the storage backend's registered id.
 *     Currently only ``"sqlite_fts"`` ships; future providers are
 *     still ``string`` here so the UI does not need to change
 *     when a new one lands.
 *
 *   * ``default_limit`` -- the ranker's default ``LIMIT`` (how
 *     many entries retrieval considers). Range 1..50. Feeds the
 *     ranker and the search preview's ``token_budget`` response
 *     calculation.
 *
 *   * ``min_relevance_score`` -- entries whose normalized
 *     relevance falls below this threshold are dropped by the
 *     ranker BEFORE the budget fitter runs. Range 0..1.
 *
 *   * ``boost_pinned`` -- multiplier applied to pinned entries'
 *     final score. Range 1.0..10.0. A value of 1.0 disables the
 *     pin boost without disabling pinning itself.
 *
 *   * ``token_budget`` -- see ``MemoryTokenBudget``.
 *
 *   * ``auto_extraction_enabled`` -- whether the background
 *     extraction job runs after conversations. Off by default
 *     for privacy/accounting reasons.
 *
 *   * ``auto_extraction_min_turns`` -- minimum number of user/
 *     assistant turn pairs before extraction is attempted.
 *     Range 1..100.
 *
 *   * ``project_overrides`` -- a map from ``project_id`` to
 *     ``MemoryProjectOverride``. Only projects that have at
 *     least one customized field appear as keys. The empty map
 *     means "no project has an override; everyone uses global
 *     settings".
 */
export interface MemorySettings {
  mode: MemoryMode;
  provider_id: string;
  default_limit: number;
  min_relevance_score: number;
  boost_pinned: number;
  token_budget: MemoryTokenBudget;
  auto_extraction_enabled: boolean;
  auto_extraction_min_turns: number;
  project_overrides: Record<string, MemoryProjectOverride>;
}

// ---------------------------------------------------------------------------
// Patchable field whitelist
// ---------------------------------------------------------------------------

/**
 * The exact set of fields a caller may include in a
 * ``PATCH /api/memory/settings`` body.
 *
 * Mirrors the backend's ``_PATCHABLE_FIELDS`` frozenset.
 *
 * Type constraint: ``(keyof MemorySettingsPatch)[]`` guarantees
 * every entry here is a real field on the patch type -- adding a
 * typo or a stale name is a compile error. Conversely,
 * ``as const satisfies`` ensures the constant itself is treated
 * as a literal tuple by the type system, so callers can use it in
 * ``Object.fromEntries`` and exhaustive checks.
 */
export const MEMORY_SETTINGS_PATCHABLE_FIELDS = [
  'mode',
  'provider_id',
  'default_limit',
  'min_relevance_score',
  'boost_pinned',
  'token_budget',
  'auto_extraction_enabled',
  'auto_extraction_min_turns',
] as const satisfies readonly (keyof MemorySettingsPatch)[];

/**
 * Type-level assertion that ``MEMORY_SETTINGS_PATCHABLE_FIELDS``
 * covers every key of ``MemorySettingsPatch``.
 *
 * If a future field is added to ``MemorySettingsPatch`` but
 * forgotten in the whitelist, the ``_MissingPatchField`` helper
 * below will fail to be ``never`` and this module will not
 * compile. This is the "compile-time lint" pattern that keeps
 * the two declarations synchronized without a runtime test.
 */
type _MissingPatchField = Exclude<
  keyof MemorySettingsPatch,
  (typeof MEMORY_SETTINGS_PATCHABLE_FIELDS)[number]
>;
// If any key is missing above, this line fails to compile.
const _patchableFieldsAreExhaustive: _MissingPatchField extends never
  ? true
  : never = true;
// Reference to silence "unused" warnings without changing the
// value's semantics.
void _patchableFieldsAreExhaustive;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Deep-partial compare: return only the fields of ``override``
 * whose values DIFFER from the corresponding fields of ``base``.
 *
 * Rationale: the backend's project-settings response MAY return
 * a full ``MemorySettings`` object in its ``override`` field
 * (with default values filled in), OR only the explicitly-set
 * delta -- the exact behavior depends on how the backend's
 * ``with_project_override`` stores the delta internally. Rather
 * than assume one, the UI computes the EFFECTIVE override by
 * comparing against the global settings.
 *
 * The returned object is safe to display as "what you have
 * customized for this project". Fields whose effective override
 * value matches the global default are omitted, so a user who
 * re-set ``default_limit`` to the same value as global sees
 * "no customization" (which matches their intent).
 *
 * ``token_budget`` is compared field-by-field, not by object
 * identity: a project that only overrides ``max_tokens`` but
 * leaves ``reserved_for_user_scope`` and ``warn_threshold`` at
 * their global values returns
 * ``{ token_budget: { max_tokens: <new> } }`` -- not the whole
 * budget object.
 */
export function computeEffectiveOverride(
  base: MemorySettings,
  override: MemorySettingsOverlay,
): MemoryProjectOverride {
  const effective: MemoryProjectOverride = {};

  if (
    override.mode !== undefined &&
    override.mode !== base.mode
  ) {
    effective.mode = override.mode;
  }
  if (
    override.provider_id !== undefined &&
    override.provider_id !== base.provider_id
  ) {
    effective.provider_id = override.provider_id;
  }
  if (
    override.default_limit !== undefined &&
    override.default_limit !== base.default_limit
  ) {
    effective.default_limit = override.default_limit;
  }
  if (
    override.min_relevance_score !== undefined &&
    override.min_relevance_score !== base.min_relevance_score
  ) {
    effective.min_relevance_score = override.min_relevance_score;
  }
  if (
    override.boost_pinned !== undefined &&
    override.boost_pinned !== base.boost_pinned
  ) {
    effective.boost_pinned = override.boost_pinned;
  }
  if (override.token_budget !== undefined) {
    const budgetDelta: Partial<MemoryTokenBudget> = {};
    if (
      override.token_budget.max_tokens !== undefined &&
      override.token_budget.max_tokens !== base.token_budget.max_tokens
    ) {
      budgetDelta.max_tokens = override.token_budget.max_tokens;
    }
    if (
      override.token_budget.reserved_for_user_scope !== undefined &&
      override.token_budget.reserved_for_user_scope !==
        base.token_budget.reserved_for_user_scope
    ) {
      budgetDelta.reserved_for_user_scope =
        override.token_budget.reserved_for_user_scope;
    }
    if (
      override.token_budget.warn_threshold !== undefined &&
      override.token_budget.warn_threshold !==
        base.token_budget.warn_threshold
    ) {
      budgetDelta.warn_threshold = override.token_budget.warn_threshold;
    }
    if (Object.keys(budgetDelta).length > 0) {
      effective.token_budget = budgetDelta;
    }
  }
  if (
    override.auto_extraction_enabled !== undefined &&
    override.auto_extraction_enabled !== base.auto_extraction_enabled
  ) {
    effective.auto_extraction_enabled = override.auto_extraction_enabled;
  }
  if (
    override.auto_extraction_min_turns !== undefined &&
    override.auto_extraction_min_turns !==
      base.auto_extraction_min_turns
  ) {
    effective.auto_extraction_min_turns =
      override.auto_extraction_min_turns;
  }

  return effective;
}

/**
 * Return ``true`` when a patch would be a no-op relative to the
 * current settings.
 *
 * Used by ``SettingsForm`` to disable the Save button and to
 * short-circuit before hitting ``PATCH /settings`` -- the
 * backend rejects empty patches with ``400 empty_update``, and
 * "changed a value and changed it back" is a common user path
 * that should not produce an error toast.
 *
 * Semantics: a patch is "no-op" when ``computeEffectiveOverride``
 * of it against the current settings is empty. Because that
 * helper compares per-field (including nested
 * ``token_budget``), sending ``{default_limit: <current value>}``
 * is correctly recognized as a no-op.
 */
export function isNoOpPatch(
  current: MemorySettings,
  patch: MemorySettingsPatch,
): boolean {
  return Object.keys(computeEffectiveOverride(current, patch)).length === 0;
}

/**
 * Merge a project override onto the base settings to produce the
 * effective settings for one project.
 *
 * Mirrors the backend's ``MemorySettings.effective_for_project``
 * behavior. The frontend uses this primarily for OPTIMISTIC UI --
 * after a ``PUT`` succeeds, the freshly-returned ``effective``
 * from the response is authoritative; this helper exists so the
 * UI can preview the result during the request (e.g. disable a
 * field based on the merged mode) without waiting.
 *
 * Only defined fields of the override participate in the merge;
 * undefined fields inherit from ``base`` verbatim. ``token_budget``
 * is merged field-by-field (a partial override of the budget
 * leaves the unspecified budget fields at their base values).
 *
 * Returns a NEW object -- ``base`` and ``override`` are not
 * mutated.
 */
export function mergeProjectSettings(
  base: MemorySettings,
  override: MemorySettingsOverlay,
): MemorySettings {
  const token_budget: MemoryTokenBudget = {
    max_tokens:
      override.token_budget?.max_tokens ?? base.token_budget.max_tokens,
    reserved_for_user_scope:
      override.token_budget?.reserved_for_user_scope ??
      base.token_budget.reserved_for_user_scope,
    warn_threshold:
      override.token_budget?.warn_threshold ??
      base.token_budget.warn_threshold,
  };

  return {
    mode: override.mode ?? base.mode,
    provider_id: override.provider_id ?? base.provider_id,
    default_limit: override.default_limit ?? base.default_limit,
    min_relevance_score:
      override.min_relevance_score ?? base.min_relevance_score,
    boost_pinned: override.boost_pinned ?? base.boost_pinned,
    token_budget,
    auto_extraction_enabled:
      override.auto_extraction_enabled ?? base.auto_extraction_enabled,
    auto_extraction_min_turns:
      override.auto_extraction_min_turns ??
      base.auto_extraction_min_turns,
    // project_overrides on the merge result is inherited from
    // base -- overrides do not nest overrides, so there is
    // nothing project-specific to merge in. The response's
    // ``effective`` field will carry the same value.
    project_overrides: base.project_overrides,
  };
}
