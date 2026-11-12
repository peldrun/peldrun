// src/features/memory/components/ProjectOverrideForm.tsx

/**
 * Editor for one project's settings override.
 *
 * Loads (effective, override) via `useProjectSettings`, then
 * presents a form pre-filled with the CURRENT override's
 * explicit fields. Fields that the project does not override
 * display the global default in a muted "inheriting" style.
 *
 * The user can:
 *
 *   * Edit one or more fields and Save -> PUT (full override
 *     replacement with only the fields the form actually
 *     changed).
 *   * Remove the override entirely -> DELETE, with a confirm
 *     dialog (`ConfirmDangerDialog`), because reverting to
 *     global defaults can silently change retrieval behavior
 *     on the next run.
 *
 * Save / Remove gating
 * --------------------
 *
 *   * Save: disabled when no field differs from the current
 *     override (or when the override is empty and no field is
 *     set).
 *   * Remove: disabled when there is no existing override.
 *
 * Both are also disabled while a mutation is pending.
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
import { RotateCcw, Save, Trash2 } from 'lucide-react';

import {
  MemoryMode,
  ALL_MEMORY_MODES,
} from '../types/enums';
import type {
  MemoryProjectOverride,
  MemorySettings,
  MemoryTokenBudget,
} from '../types/settings';
import {
  computeEffectiveOverride,
  MemorySettingsBounds,
} from '../types/settings';
import {
  makeProjectOverrideSchema,
  zodErrorToFieldMap,
} from '../lib/validators';
import {
  useDeleteProjectOverride,
  usePutProjectOverride,
} from '../api/mutations';
import { useProjectSettings } from '../api/queries';
import { cn } from '../lib/cn';
import { ConfirmDangerDialog } from './ConfirmDangerDialog';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ProjectOverrideFormProps {
  projectId: string;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ProjectOverrideForm({
  projectId,
  className,
}: ProjectOverrideFormProps) {
  const { t } = useTranslation('memory');
  const formId = useId();

  const query = useProjectSettings(projectId);
  const putMutation = usePutProjectOverride();
  const deleteMutation = useDeleteProjectOverride();

  const [form, setForm] = useState<MemoryProjectOverride>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirmRemoveOpen, setConfirmRemoveOpen] = useState(false);

  const globalBase = query.data?.effective ?? null;
  const initialOverride = query.data?.override ?? null;

  // Initialize / reset the form when the loaded override changes.
  useEffect(() => {
    if (initialOverride === null || !globalBase) return;
    const effective = computeEffectiveOverride(globalBase, initialOverride);
    setForm(effective);
  }, [initialOverride, globalBase]);

  const patch = useMemo<MemoryProjectOverride>(() => {
    if (!globalBase || !initialOverride) return {};
    const effectiveOverride = computeEffectiveOverride(
      globalBase,
      initialOverride,
    );
    return computeEffectiveOverride(
      { ...globalBase, project_overrides: {} },
      {
        // Merge current form values over the initial override.
        ...effectiveOverride,
        ...form,
        token_budget: {
          ...(effectiveOverride.token_budget ?? {}),
          ...(form.token_budget ?? {}),
        },
      },
    );
  }, [globalBase, initialOverride, form]);

  const isDirty = Object.keys(patch).length > 0 || Object.keys(form).length > 0;
  const hasExistingOverride =
    initialOverride !== null &&
    Object.keys(computeEffectiveOverride(globalBase!, initialOverride)).length >
      0;

  // ---- Loading / error ------------------------------------------
  if (query.isLoading || !globalBase) {
    return (
      <div className={cn('space-y-2', className)} aria-busy="true">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="h-12 w-full animate-pulse rounded-md bg-muted"
          />
        ))}
      </div>
    );
  }

  if (query.error) {
    return (
      <div
        className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive"
        role="alert"
      >
        {query.error.message}
      </div>
    );
  }

  // ---- Handlers -------------------------------------------------
  function setField<K extends keyof MemoryProjectOverride>(
    key: K,
    value: MemoryProjectOverride[K],
  ) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function setBudgetField(key: keyof MemoryTokenBudget, value: number) {
    setForm((prev) => ({
      ...prev,
      token_budget: { ...(prev.token_budget ?? {}), [key]: value },
    }));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!globalBase) return;
    const result = makeProjectOverrideSchema(globalBase).safeParse(patch);
    if (!result.success) {
      setErrors(zodErrorToFieldMap(result.error));
      return;
    }
    setErrors({});
    try {
      await putMutation.mutateAsync({
        projectId,
        override: result.data,
      });
    } catch {
      // error surfaced via mutation state
    }
  }

  async function handleRemove() {
    try {
      await deleteMutation.mutateAsync({ projectId });
      setConfirmRemoveOpen(false);
    } catch {
      // error surfaced via mutation state
    }
  }

  // ---- Render ---------------------------------------------------
  const isPending = putMutation.isPending || deleteMutation.isPending;
  const mergedBudget: MemoryTokenBudget = {
    ...globalBase.token_budget,
    ...(form.token_budget ?? {}),
  };

  return (
    <>
      <form
        onSubmit={handleSubmit}
        className={cn(
          'space-y-4 rounded-lg border border-border bg-card p-3',
          className,
        )}
        aria-labelledby={`${formId}-title`}
      >
        <div className="flex items-center justify-between gap-2 border-b border-border pb-2">
          <h3
            id={`${formId}-title`}
            className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
          >
            {t('settings.sections.projectOverrides')}
          </h3>
          {hasExistingOverride && (
            <button
              type="button"
              onClick={() => setConfirmRemoveOpen(true)}
              disabled={isPending}
              className="inline-flex items-center gap-1 rounded px-1.5 text-[11px] text-destructive hover:bg-destructive/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Trash2 size={10} aria-hidden="true" />
              {t('settings.removeProjectOverride')}
            </button>
          )}
        </div>

        <p className="text-[11px] text-muted-foreground">
          {t('settings.projectOverrideNote')}
        </p>

        {/* Mode */}
        <FieldRow
          id={`${formId}-mode`}
          label={t('settings.sections.mode')}
          inheriting={form.mode === undefined}
          inheritedValue={t(`mode.${globalBase.mode}.name`)}
        >
          <select
            id={`${formId}-mode`}
            value={form.mode ?? ''}
            onChange={(e) => {
              const v = e.target.value;
              setField(
                'mode',
                v === '' ? undefined : (v as MemoryMode),
              );
            }}
            disabled={isPending}
            className="h-7 rounded-md border border-border bg-background px-1.5 text-[11px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-0"
          >
            <option value="">—</option>
            {ALL_MEMORY_MODES.map((m) => (
              <option key={m} value={m}>
                {t(`mode.${m}.name`)}
              </option>
            ))}
          </select>
        </FieldRow>

        {/* Default limit */}
        <FieldRow
          id={`${formId}-default-limit`}
          label={t('settings.fields.defaultLimit')}
          inheriting={form.default_limit === undefined}
          inheritedValue={String(globalBase.default_limit)}
          error={errors.default_limit}
        >
          <input
            id={`${formId}-default-limit`}
            type="number"
            value={form.default_limit ?? ''}
            min={MemorySettingsBounds.default_limit.min}
            max={MemorySettingsBounds.default_limit.max}
            step={1}
            onChange={(e) => {
              const v = e.target.value;
              setField(
                'default_limit',
                v === '' ? undefined : Number(v),
              );
            }}
            disabled={isPending}
            placeholder={String(globalBase.default_limit)}
            className="h-7 w-24 rounded-md border border-border bg-background px-1.5 text-[11px] tabular-nums text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-0"
          />
        </FieldRow>

        {/* Min relevance */}
        <FieldRow
          id={`${formId}-min-relevance`}
          label={t('settings.fields.minRelevance')}
          inheriting={form.min_relevance_score === undefined}
          inheritedValue={String(globalBase.min_relevance_score)}
          error={errors.min_relevance_score}
        >
          <input
            id={`${formId}-min-relevance`}
            type="number"
            value={form.min_relevance_score ?? ''}
            min={MemorySettingsBounds.min_relevance_score.min}
            max={MemorySettingsBounds.min_relevance_score.max}
            step={0.05}
            onChange={(e) => {
              const v = e.target.value;
              setField(
                'min_relevance_score',
                v === '' ? undefined : Number(v),
              );
            }}
            disabled={isPending}
            placeholder={String(globalBase.min_relevance_score)}
            className="h-7 w-24 rounded-md border border-border bg-background px-1.5 text-[11px] tabular-nums text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-0"
          />
        </FieldRow>

        {/* Boost pinned */}
        <FieldRow
          id={`${formId}-boost-pinned`}
          label={t('settings.fields.boostPinned')}
          inheriting={form.boost_pinned === undefined}
          inheritedValue={String(globalBase.boost_pinned)}
          error={errors.boost_pinned}
        >
          <input
            id={`${formId}-boost-pinned`}
            type="number"
            value={form.boost_pinned ?? ''}
            min={MemorySettingsBounds.boost_pinned.min}
            max={MemorySettingsBounds.boost_pinned.max}
            step={0.1}
            onChange={(e) => {
              const v = e.target.value;
              setField(
                'boost_pinned',
                v === '' ? undefined : Number(v),
              );
            }}
            disabled={isPending}
            placeholder={String(globalBase.boost_pinned)}
            className="h-7 w-24 rounded-md border border-border bg-background px-1.5 text-[11px] tabular-nums text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-0"
          />
        </FieldRow>

        {/* Token budget - max */}
        <FieldRow
          id={`${formId}-max-tokens`}
          label={t('settings.fields.maxTokens')}
          inheriting={form.token_budget?.max_tokens === undefined}
          inheritedValue={String(globalBase.token_budget.max_tokens)}
          error={errors['token_budget.max_tokens']}
        >
          <input
            id={`${formId}-max-tokens`}
            type="number"
            value={form.token_budget?.max_tokens ?? ''}
            min={MemorySettingsBounds.token_budget_max_tokens.min}
            max={MemorySettingsBounds.token_budget_max_tokens.max}
            step={64}
            onChange={(e) => {
              const v = e.target.value;
              if (v === '') {
                setForm((prev) => {
                  const next = { ...prev };
                  if (next.token_budget) {
                    const { max_tokens: _m, ...rest } = next.token_budget;
                    void _m;
                    next.token_budget = rest;
                    if (Object.keys(rest).length === 0) delete next.token_budget;
                  }
                  return next;
                });
              } else {
                setBudgetField('max_tokens', Number(v));
              }
            }}
            disabled={isPending}
            placeholder={String(globalBase.token_budget.max_tokens)}
            className="h-7 w-24 rounded-md border border-border bg-background px-1.5 text-[11px] tabular-nums text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-0"
          />
        </FieldRow>

        {/* Merged-budget hint */}
        {form.token_budget &&
          mergedBudget.reserved_for_user_scope > mergedBudget.max_tokens && (
            <p className="text-[11px] text-destructive" role="alert">
              {t('validation.reservedExceedsMax')}
            </p>
          )}

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 border-t border-border pt-2">
          {!isDirty && (
            <span className="text-[10px] text-muted-foreground">
              {t('settings.noChanges')}
            </span>
          )}
          {isDirty && (
            <button
              type="button"
              onClick={() => {
                setForm(
                  initialOverride
                    ? computeEffectiveOverride(globalBase, initialOverride)
                    : {},
                );
                setErrors({});
              }}
              disabled={isPending}
              className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-card px-2 text-[11px] text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-60"
            >
              <RotateCcw size={10} aria-hidden="true" />
              {t('common.reset')}
            </button>
          )}
          <button
            type="submit"
            disabled={!isDirty || isPending}
            className={cn(
              'inline-flex h-7 items-center gap-1 rounded-md bg-primary px-2 text-[11px] font-medium text-primary-foreground',
              'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
              'disabled:cursor-not-allowed disabled:opacity-60',
            )}
          >
            <Save size={10} aria-hidden="true" />
            {isPending ? t('common.saving') : t('common.save')}
          </button>
        </div>
      </form>

      <ConfirmDangerDialog
        open={confirmRemoveOpen}
        onOpenChange={setConfirmRemoveOpen}
        title={t('settings.removeProjectOverride')}
        description={t('settings.removeOverrideConfirm')}
        confirmToken={projectId}
        confirmLabel={t('settings.removeProjectOverride')}
        isPending={deleteMutation.isPending}
        onConfirm={handleRemove}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Field row
// ---------------------------------------------------------------------------

interface FieldRowProps {
  id: string;
  label: string;
  inheriting: boolean;
  inheritedValue: string;
  error?: string;
  children: React.ReactNode;
}

function FieldRow({
  id,
  label,
  inheriting,
  inheritedValue,
  error,
  children,
}: FieldRowProps) {
  return (
    <div className="grid grid-cols-[1fr_auto] items-center gap-2">
      <div>
        <label
          htmlFor={id}
          className="text-[11px] font-medium text-foreground"
        >
          {label}
        </label>
        <p className="mt-0.5 text-[10px] text-muted-foreground">
          {inheriting ? (
            <span className="italic">
              · inheriting <span className="font-mono">{inheritedValue}</span>
            </span>
          ) : (
            <span className="text-primary">· overridden</span>
          )}
        </p>
        {error && (
          <p className="mt-0.5 text-[10px] text-destructive" role="alert">
            {error}
          </p>
        )}
      </div>
      {children}
    </div>
  );
}
