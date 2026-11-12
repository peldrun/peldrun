// src/features/memory/components/SettingsForm.tsx

/**
 * The global memory settings form.
 *
 * Loads the current settings via `useMemorySettings`, maintains
 * a local editable copy, and PATCHes only the fields that
 * differ on save. This diff-based submission is REQUIRED by
 * the backend (empty PATCH -> 400) and prevents the
 * "changed then changed back" case from producing a misleading
 * error toast.
 *
 * The form is ALWAYS editable -- including when `mode === 'disabled'`.
 * This is the app's only path to re-enabling memory (spec §1.3
 * and §6.1 rule 1).
 *
 * Validation strategy
 * -------------------
 *
 * `makeSettingsPatchSchema(current)` is rebuilt on every render
 * with the LATEST loaded settings. It cross-checks
 * `reserved <= max` on the merged token budget, so a user who
 * lowers `max_tokens` below the current `reserved` gets an
 * inline error the moment they tab out -- not a round-trip
 * rejection.
 *
 * The Save button is disabled when `isNoOpPatch(current, draft)`
 * is true, which covers both "nothing changed" and "changed
 * back to the original value".
 */

'use client';

import {
  type FormEvent,
  useEffect,
  useId,
  useMemo,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Save } from 'lucide-react';

import {
  MemoryMode,
  ALL_MEMORY_MODES,
} from '../types/enums';
import type {
  MemorySettings,
  MemorySettingsPatch,
  MemoryTokenBudget,
} from '../types/settings';
import {
  computeEffectiveOverride,
  isNoOpPatch,
  MemorySettingsBounds,
} from '../types/settings';
import { makeSettingsPatchSchema, zodErrorToFieldMap } from '../lib/validators';
import { usePatchSettings } from '../api/mutations';
import { useMemorySettings } from '../api/queries';
import { cn } from '../lib/cn';
import { ModeBadge } from './ModeBadge';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SettingsForm({ className }: { className?: string }) {
  const { t } = useTranslation('memory');
  const formId = useId();

  const settingsQuery = useMemorySettings();
  const patchMutation = usePatchSettings();

  const [form, setForm] = useState<MemorySettings | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [justSaved, setJustSaved] = useState(false);

  // Initialize / sync form state.
  useEffect(() => {
    if (settingsQuery.data && form === null) {
      setForm(structuredClone(settingsQuery.data));
    }
  }, [settingsQuery.data, form]);

  // After successful save, reset form to the server's canonical
  // response (which may normalize some values, e.g. trim a
  // provider_id). This also closes the "saved" pulse.
  useEffect(() => {
    if (patchMutation.isSuccess && patchMutation.data) {
      setForm(structuredClone(patchMutation.data.settings));
      setJustSaved(true);
      const handle = setTimeout(() => setJustSaved(false), 1200);
      return () => clearTimeout(handle);
    }
    return undefined;
  }, [patchMutation.isSuccess, patchMutation.data]);

  const current = settingsQuery.data ?? null;

  const patch = useMemo<MemorySettingsPatch>(() => {
    if (!current || !form) return {};
    return computeEffectiveOverride(current, form) as MemorySettingsPatch;
  }, [current, form]);

  const isDirty = !current ? false : !isNoOpPatch(current, patch);

  // ---- Loading / error states ------------------------------------
  if (settingsQuery.isLoading) {
    return (
      <div className={cn('space-y-2', className)} aria-busy="true">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="h-16 w-full animate-pulse rounded-md bg-muted"
          />
        ))}
      </div>
    );
  }

  if (settingsQuery.error || !form || !current) {
    return (
      <div
        className={cn(
          'rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive',
          className,
        )}
        role="alert"
      >
        {settingsQuery.error?.message ?? t('errors.generic')}
      </div>
    );
  }

  // ---- Handlers --------------------------------------------------
  function updateField<K extends keyof MemorySettings>(
    key: K,
    value: MemorySettings[K],
  ) {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
  }

  function updateBudgetField(
    key: keyof MemoryTokenBudget,
    value: number,
  ) {
    setForm((prev) =>
      prev
        ? { ...prev, token_budget: { ...prev.token_budget, [key]: value } }
        : prev,
    );
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!current) return;
    const result = makeSettingsPatchSchema(current).safeParse(patch);
    if (!result.success) {
      setErrors(zodErrorToFieldMap(result.error));
      return;
    }
    setErrors({});
    try {
      await patchMutation.mutateAsync(result.data);
    } catch {
      // error surfaced via mutation state
    }
  }

  // ---- Render ----------------------------------------------------
  const isPending = patchMutation.isPending;
  const canSave = isDirty && !isPending;

  return (
    <form
      onSubmit={handleSubmit}
      className={cn('space-y-6', className)}
      aria-labelledby={`${formId}-title`}
    >
      <h2 id={`${formId}-title`} className="sr-only">
        {t('settings.title')}
      </h2>

      {/* ============ Mode ============ */}
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('settings.sections.mode')}
        </h3>
        <div className="space-y-1.5">
          {ALL_MEMORY_MODES.map((mode) => (
            <label
              key={mode}
              className={cn(
                'flex items-start gap-2 rounded-md border bg-card p-2.5 text-xs transition-colors',
                form.mode === mode
                  ? 'border-primary/60 bg-primary/5'
                  : 'border-border hover:bg-muted/40',
                isPending && 'cursor-not-allowed opacity-60',
              )}
            >
              <input
                type="radio"
                name="memory-mode"
                value={mode}
                checked={form.mode === mode}
                onChange={() => updateField('mode', mode)}
                disabled={isPending}
                className="mt-0.5 h-3.5 w-3.5 border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="font-medium text-foreground">
                    {t(`mode.${mode}.name`)}
                  </span>
                  {form.mode === mode && <ModeBadge mode={mode} size="xs" />}
                </div>
                <p className="mt-0.5 text-[11px] text-muted-foreground">
                  {t(`mode.${mode}.description`)}
                </p>
              </div>
            </label>
          ))}
        </div>
        {form.mode === 'disabled' && (
          <p className="mt-2 rounded-md bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800 dark:bg-amber-900/20 dark:text-amber-300">
            {t('settings.modeWarningDisabled')}
          </p>
        )}
      </section>

      {/* ============ Provider ============ */}
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('settings.sections.provider')}
        </h3>
        <div>
          <label
            htmlFor={`${formId}-provider`}
            className="mb-1 block text-[11px] font-medium text-foreground"
          >
            {t('settings.fields.provider')}
          </label>
          <select
            id={`${formId}-provider`}
            value={form.provider_id}
            onChange={(e) => updateField('provider_id', e.target.value)}
            disabled={isPending}
            className="h-8 w-full max-w-xs rounded-md border border-border bg-background px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
          >
            <option value="sqlite_fts">sqlite_fts</option>
            {form.provider_id !== 'sqlite_fts' && (
              <option value={form.provider_id}>{form.provider_id}</option>
            )}
          </select>
        </div>
      </section>

      {/* ============ Retrieval ============ */}
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('settings.sections.retrieval')}
        </h3>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <NumberField
            id={`${formId}-default-limit`}
            label={t('settings.fields.defaultLimit')}
            help={t('settings.help.defaultLimit')}
            value={form.default_limit}
            min={MemorySettingsBounds.default_limit.min}
            max={MemorySettingsBounds.default_limit.max}
            step={1}
            onChange={(v) => updateField('default_limit', v)}
            disabled={isPending}
            error={errors.default_limit}
          />
          <NumberField
            id={`${formId}-min-relevance`}
            label={t('settings.fields.minRelevance')}
            help={t('settings.help.minRelevance')}
            value={form.min_relevance_score}
            min={MemorySettingsBounds.min_relevance_score.min}
            max={MemorySettingsBounds.min_relevance_score.max}
            step={0.05}
            onChange={(v) => updateField('min_relevance_score', v)}
            disabled={isPending}
            error={errors.min_relevance_score}
          />
          <NumberField
            id={`${formId}-boost-pinned`}
            label={t('settings.fields.boostPinned')}
            help={t('settings.help.boostPinned')}
            value={form.boost_pinned}
            min={MemorySettingsBounds.boost_pinned.min}
            max={MemorySettingsBounds.boost_pinned.max}
            step={0.1}
            onChange={(v) => updateField('boost_pinned', v)}
            disabled={isPending}
            error={errors.boost_pinned}
          />
        </div>
      </section>

      {/* ============ Token budget ============ */}
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('settings.sections.tokenBudget')}
        </h3>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <NumberField
            id={`${formId}-max-tokens`}
            label={t('settings.fields.maxTokens')}
            help={t('settings.help.maxTokens')}
            value={form.token_budget.max_tokens}
            min={MemorySettingsBounds.token_budget_max_tokens.min}
            max={MemorySettingsBounds.token_budget_max_tokens.max}
            step={64}
            onChange={(v) => updateBudgetField('max_tokens', v)}
            disabled={isPending}
            error={errors['token_budget.max_tokens']}
          />
          <NumberField
            id={`${formId}-reserved`}
            label={t('settings.fields.reservedForUserScope')}
            help={t('settings.help.reservedForUserScope')}
            value={form.token_budget.reserved_for_user_scope}
            min={0}
            max={form.token_budget.max_tokens}
            step={64}
            onChange={(v) =>
              updateBudgetField('reserved_for_user_scope', v)
            }
            disabled={isPending}
            error={errors['token_budget.reserved_for_user_scope']}
          />
          <NumberField
            id={`${formId}-warn-threshold`}
            label={t('settings.fields.warnThreshold')}
            help={t('settings.help.warnThreshold')}
            value={form.token_budget.warn_threshold}
            min={MemorySettingsBounds.token_budget_warn_threshold.min}
            max={MemorySettingsBounds.token_budget_warn_threshold.max}
            step={0.05}
            onChange={(v) => updateBudgetField('warn_threshold', v)}
            disabled={isPending}
            error={errors['token_budget.warn_threshold']}
          />
        </div>
      </section>

      {/* ============ Auto extraction ============ */}
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('settings.sections.autoExtraction')}
        </h3>
        <div className="space-y-3">
          <label className="inline-flex items-center gap-1.5 text-xs text-foreground">
            <input
              type="checkbox"
              checked={form.auto_extraction_enabled}
              onChange={(e) =>
                updateField('auto_extraction_enabled', e.target.checked)
              }
              disabled={isPending}
              className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
            />
            {t('settings.fields.autoExtractionEnabled')}
          </label>
          <NumberField
            id={`${formId}-min-turns`}
            label={t('settings.fields.autoExtractionMinTurns')}
            help={t('settings.help.autoExtractionMinTurns')}
            value={form.auto_extraction_min_turns}
            min={MemorySettingsBounds.auto_extraction_min_turns.min}
            max={MemorySettingsBounds.auto_extraction_min_turns.max}
            step={1}
            onChange={(v) => updateField('auto_extraction_min_turns', v)}
            disabled={isPending || !form.auto_extraction_enabled}
            error={errors.auto_extraction_min_turns}
            width="sm"
          />
        </div>
      </section>

      {/* ============ Footer ============ */}
      <div className="flex items-center justify-end gap-2 border-t border-border pt-3">
        {justSaved && (
          <span className="inline-flex items-center gap-1 text-[11px] text-emerald-600 dark:text-emerald-400">
            <Check size={11} aria-hidden="true" />
            {t('settings.savedToast')}
          </span>
        )}
        {!isDirty && !justSaved && (
          <span className="text-[11px] text-muted-foreground">
            {t('settings.noChanges')}
          </span>
        )}
        <button
          type="submit"
          disabled={!canSave}
          className={cn(
            'inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground',
            'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          <Save size={12} aria-hidden="true" />
          {isPending ? t('common.saving') : t('common.saveChanges')}
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Number field
// ---------------------------------------------------------------------------

interface NumberFieldProps {
  id: string;
  label: string;
  help?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  error?: string;
  width?: 'full' | 'sm';
}

function NumberField({
  id,
  label,
  help,
  value,
  min,
  max,
  step,
  onChange,
  disabled = false,
  error,
  width = 'full',
}: NumberFieldProps) {
  const { t } = useTranslation('memory');
  return (
    <div className={width === 'sm' ? 'max-w-[10rem]' : undefined}>
      <label
        htmlFor={id}
        className="mb-1 block text-[11px] font-medium text-foreground"
      >
        {label}
      </label>
      <input
        id={id}
        type="number"
        value={Number.isFinite(value) ? value : 0}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          const parsed = Number(e.target.value);
          onChange(Number.isFinite(parsed) ? parsed : min);
        }}
        disabled={disabled}
        aria-invalid={error !== undefined}
        aria-describedby={help ? `${id}-help` : undefined}
        className={cn(
          'h-8 w-full rounded-md border bg-background px-2 text-xs text-foreground tabular-nums',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
          'disabled:cursor-not-allowed disabled:opacity-60',
          error ? 'border-destructive' : 'border-border',
        )}
      />
      {help && (
        <p
          id={`${id}-help`}
          className="mt-0.5 text-[10px] text-muted-foreground"
        >
          {help}
        </p>
      )}
      {error && (
        <p className="mt-0.5 text-[10px] text-destructive" role="alert">
          {t(error, { min, max })}
        </p>
      )}
    </div>
  );
}
