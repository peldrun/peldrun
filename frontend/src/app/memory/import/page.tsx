// src/app/memory/import/page.tsx

/**
 * Import wizard (`/memory/import`).
 *
 * Three steps:
 *
 *   1. **Select**    -- pick file, target scope, optional adapter.
 *   2. **Preview**   -- show `ImportPreviewTable`, wait for
 *                       explicit confirmation.
 *   3. **Result**    -- summary of created / skipped / errors,
 *                       plus "view project" and "import another".
 *
 * State machine (local)
 * ---------------------
 *
 *   select  --[Preview Import]-->  preview
 *   preview --[Back]----------->  select
 *   preview --[Confirm & Import]->  result
 *   result  --[Import Another]->  select
 *
 * Every "back" transition preserves the entered values (file,
 * scope, adapter) so the user can fix one field and re-submit
 * without re-doing the whole wizard.
 *
 * Scope picker
 * ------------
 *
 * Uses a `<datalist>` on a plain text input. This beats both
 * alternatives for this use case:
 *
 *   * A `<select>` forbids NEW project ids -- but a user
 *     importing into a project that has no memories yet (its
 *     id will not appear in `useProjectsList`) must be able
 *     to type it.
 *
 *   * A bare text input gives no autocomplete for the common
 *     case of importing into an existing project.
 *
 * `<datalist>` gives both: suggestions when useful, free
 * typing when necessary.
 *
 * Mode gating
 * -----------
 *
 * Import requires `mode !== 'disabled'` (spec §1.3). The
 * wizard is fully visible but all actions are disabled with
 * an amber banner when memory is off. Unlike settings, we
 * cannot offer a "re-enable inline" button -- importing
 * into disabled memory is a configuration error, not a
 * transient condition.
 *
 * Preview file lifecycle
 * ----------------------
 *
 * The selected `File` is kept in a state variable across
 * the select/preview transition. When the user clicks
 * "Confirm & Import", the SAME file object is re-submitted
 * to `/apply` -- the backend re-parses it, but the user
 * does not re-upload. This is safe because the file content
 * is guaranteed unchanged (it is an immutable browser
 * object). A 100 MB upload is done once, not twice.
 */

'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  FileText,
  FolderOpen,
  Upload,
  User as UserIcon,
} from 'lucide-react';

import type { ImportFileInfo } from '../../../features/memory/types/import-export';
import type {
  ImportAdapter,
  ImportPreview,
} from '../../../features/memory/types/responses';
import type { MemoryApiError } from '../../../features/memory/types/errors';
import { MemoryScope } from '../../../features/memory/types/enums';
import {
  useImportAdapters,
  useMemorySettings,
  useProjectsList,
} from '../../../features/memory/api/queries';
import {
  useImportApply,
  useImportPreview,
  type ImportMutationVariables,
} from '../../../features/memory/api/mutations';
import { cn } from '../../../features/memory/lib/cn';
import { ImportDropZone } from '../../../features/memory/components/ImportDropZone';
import { ImportPreviewTable } from '../../../features/memory/components/ImportPreviewTable';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type WizardStep = 'select' | 'preview' | 'result';

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ImportPage() {
  const { t } = useTranslation('memory');

  // ---- Queries --------------------------------------------------
  const settingsQuery = useMemorySettings();
  const projectsQuery = useProjectsList();
  const adaptersQuery = useImportAdapters();

  const mode = settingsQuery.data?.mode;
  const canImport = mode !== undefined && mode !== 'disabled';

  // ---- Mutations ------------------------------------------------
  const previewMutation = useImportPreview();
  const applyMutation = useImportApply();

  // ---- Wizard state ---------------------------------------------
  const [step, setStep] = useState<WizardStep>('select');
  const [file, setFile] = useState<File | null>(null);
  const [fileInfo, setFileInfo] = useState<ImportFileInfo | null>(null);
  const [scopeType, setScopeType] = useState<MemoryScope>(
    MemoryScope.PROJECT,
  );
  const [scopeId, setScopeId] = useState('');
  const [adapterId, setAdapterId] = useState('');
  const [rejectionError, setRejectionError] = useState<string | null>(null);

  // ---- Derived --------------------------------------------------
  const effectiveScopeId =
    scopeType === MemoryScope.USER ? 'default' : scopeId.trim();

  const canSubmit =
    canImport &&
    file !== null &&
    effectiveScopeId.length > 0 &&
    !previewMutation.isPending;

  const projectIds = useMemo(
    () => (projectsQuery.data?.projects ?? []).map((p) => p.project_id),
    [projectsQuery.data?.projects],
  );

  const preview: ImportPreview | null = previewMutation.data?.preview ?? null;
  const applyResult = applyMutation.data?.result ?? null;

  // ---- Handlers --------------------------------------------------
  function buildVariables(): ImportMutationVariables | null {
    if (!file) return null;
    if (effectiveScopeId.length === 0) return null;
    return {
      kind: 'file',
      request: {
        file,
        target: {
          scope_type: scopeType,
          scope_id: effectiveScopeId,
        },
        adapter_id: adapterId === '' ? undefined : adapterId,
      },
    };
  }

  async function handlePreview() {
    const vars = buildVariables();
    if (!vars) return;
    try {
      await previewMutation.mutateAsync(vars);
      setStep('preview');
    } catch {
      // error shown via previewMutation.error
    }
  }

  async function handleApply() {
    const vars = buildVariables();
    if (!vars) return;
    try {
      await applyMutation.mutateAsync(vars);
      setStep('result');
    } catch {
      // error shown via applyMutation.error
    }
  }

  function handleImportAnother() {
    setStep('select');
    setFile(null);
    setFileInfo(null);
    setRejectionError(null);
    // Keep scopeType/scopeId -- the user is likely
    // importing into the same project.
    previewMutation.reset();
    applyMutation.reset();
  }

  // ---- Render ---------------------------------------------------
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      {/* ============ Title ============ */}
      <div>
        <h2 className="text-base font-semibold text-foreground">
          {t('import.title')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('import.description')}
        </p>
      </div>

      {/* ============ Step indicator ============ */}
      <StepIndicator current={step} />

      {/* ============ Disabled banner ============ */}
      {!canImport && (
        <p className="flex items-start gap-2 rounded-md border border-amber-300/60 bg-amber-50/60 p-2.5 text-[11px] text-amber-800 dark:border-amber-700/60 dark:bg-amber-900/10 dark:text-amber-300">
          <span className="mt-0.5 shrink-0 font-bold">!</span>
          <span>
            {t('errors.mode_disabled')}{' '}
            <Link href="/memory/settings" className="underline">
              {t('errors.withSettings')}
            </Link>
          </span>
        </p>
      )}

      {/* ============ Step 1: Select ============ */}
      {step === 'select' && (
        <div className="space-y-4">
          {/* Scope picker */}
          <section className="space-y-2 rounded-lg border border-border bg-card p-3">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t('import.upload.scopeLabel')}
            </h3>

            <div className="flex gap-3">
              {(
                [
                  { value: MemoryScope.PROJECT, icon: FolderOpen, label: t('scope.project') },
                  { value: MemoryScope.USER, icon: UserIcon, label: t('scope.user') },
                ] as const
              ).map(({ value, icon: Icon, label }) => (
                <label
                  key={value}
                  className={cn(
                    'flex flex-1 cursor-pointer items-center gap-1.5 rounded-md border bg-background p-2 text-[11px] transition-colors',
                    scopeType === value
                      ? 'border-primary/60 bg-primary/5'
                      : 'border-border hover:bg-muted/40',
                    !canImport && 'cursor-not-allowed opacity-60',
                  )}
                >
                  <input
                    type="radio"
                    name="import-scope-type"
                    value={value}
                    checked={scopeType === value}
                    onChange={() => setScopeType(value)}
                    disabled={!canImport}
                    className="h-3.5 w-3.5 border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
                  />
                  <Icon size={11} aria-hidden="true" />
                  <span className="font-medium text-foreground">{label}</span>
                </label>
              ))}
            </div>

            {scopeType === MemoryScope.PROJECT && (
              <div>
                <label
                  htmlFor="import-scope-id"
                  className="mb-1 block text-[11px] font-medium text-foreground"
                >
                  {t('scope.scopeId')}
                </label>
                <input
                  id="import-scope-id"
                  type="text"
                  list="import-project-ids"
                  value={scopeId}
                  onChange={(e) => setScopeId(e.target.value)}
                  placeholder="acme"
                  disabled={!canImport || previewMutation.isPending}
                  className="h-8 w-full rounded-md border border-border bg-background px-2 font-mono text-xs text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
                />
                <datalist id="import-project-ids">
                  {projectIds.map((id) => (
                    <option key={id} value={id} />
                  ))}
                </datalist>
              </div>
            )}
          </section>

          {/* Dropzone */}
          <section className="space-y-2 rounded-lg border border-border bg-card p-3">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t('import.upload.selectFile')}
            </h3>

            {!file || !fileInfo ? (
              <ImportDropZone
                onFileSelected={(f, info) => {
                  setFile(f);
                  setFileInfo(info);
                  setRejectionError(null);
                }}
                onRejected={(_reason, message) => {
                  setRejectionError(message);
                  setFile(null);
                  setFileInfo(null);
                }}
                disabled={!canImport || previewMutation.isPending}
              />
            ) : (
              <div className="flex items-center justify-between gap-2 rounded-md border border-border bg-muted/30 p-2">
                <div className="flex min-w-0 items-center gap-2">
                  <div
                    className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary"
                    aria-hidden="true"
                  >
                    <FileText size={13} strokeWidth={1.8} />
                  </div>
                  <div className="min-w-0">
                    <p className="truncate font-mono text-[11px] text-foreground">
                      {fileInfo.filename}
                    </p>
                    <p className="text-[10px] text-muted-foreground">
                      {(fileInfo.sizeBytes / 1024).toFixed(1)} KB
                      {fileInfo.detectedAdapter && (
                        <> · {fileInfo.detectedAdapter}</>
                      )}
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    setFile(null);
                    setFileInfo(null);
                  }}
                  disabled={previewMutation.isPending}
                  className="shrink-0 rounded-md border border-border bg-background px-2 py-1 text-[10px] font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {t('common.reset')}
                </button>
              </div>
            )}

            {rejectionError && (
              <p
                className="rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1 text-[11px] text-destructive"
                role="alert"
              >
                {rejectionError}
              </p>
            )}
          </section>

          {/* Adapter picker */}
          <section className="space-y-2 rounded-lg border border-border bg-card p-3">
            <label
              htmlFor="import-adapter"
              className="block text-xs font-semibold uppercase tracking-wide text-muted-foreground"
            >
              {t('import.upload.adapterLabel')}
            </label>
            <select
              id="import-adapter"
              value={adapterId}
              onChange={(e) => setAdapterId(e.target.value)}
              disabled={
                !canImport ||
                previewMutation.isPending ||
                adaptersQuery.isLoading
              }
              className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            >
              <option value="">{t('import.upload.adapterAuto')}</option>
              {(adaptersQuery.data?.adapters ?? []).map(
                (a: ImportAdapter) => (
                  <option key={a.adapter_id} value={a.adapter_id}>
                    {a.adapter_id} — {a.description}
                  </option>
                ),
              )}
            </select>
          </section>

          {/* Preview error */}
          {previewMutation.error && (
            <ErrorBanner error={previewMutation.error} />
          )}

          {/* Actions */}
          <div className="flex items-center justify-end gap-2 border-t border-border pt-3">
            <button
              type="button"
              onClick={handlePreview}
              disabled={!canSubmit}
              className={cn(
                'inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground',
                'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                'disabled:cursor-not-allowed disabled:opacity-60',
              )}
            >
              <ArrowRight size={12} aria-hidden="true" />
              {previewMutation.isPending
                ? t('common.loading')
                : t('import.preview.title')}
            </button>
          </div>
        </div>
      )}

      {/* ============ Step 2: Preview ============ */}
      {step === 'preview' && preview && (
        <div className="space-y-4">
          <div className="rounded-lg border border-border bg-card p-3">
            <ImportPreviewTable preview={preview} />
          </div>

          {applyMutation.error && <ErrorBanner error={applyMutation.error} />}

          <div className="flex items-center justify-between gap-2 border-t border-border pt-3">
            <button
              type="button"
              onClick={() => setStep('select')}
              disabled={applyMutation.isPending}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <ArrowLeft size={12} aria-hidden="true" />
              {t('import.preview.back')}
            </button>
            <button
              type="button"
              onClick={handleApply}
              disabled={applyMutation.isPending}
              className={cn(
                'inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground',
                'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                'disabled:cursor-not-allowed disabled:opacity-60',
              )}
            >
              <Upload size={12} aria-hidden="true" />
              {applyMutation.isPending
                ? t('common.loading')
                : t('import.preview.confirm')}
            </button>
          </div>
        </div>
      )}

      {/* ============ Step 3: Result ============ */}
      {step === 'result' && applyResult && (
        <div className="space-y-4">
          <div className="rounded-lg border border-border bg-card p-4">
            <div className="mb-2 flex items-center gap-2">
              <div
                className="flex h-8 w-8 items-center justify-center rounded-full bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-400"
                aria-hidden="true"
              >
                <CheckCircle2 size={16} strokeWidth={2} />
              </div>
              <h3 className="text-sm font-semibold text-foreground">
                {t('import.result.title')}
              </h3>
            </div>
            <p className="text-xs text-muted-foreground">
              {t('import.result.summary', {
                created: applyResult.created,
                skipped: applyResult.skipped,
                errors: applyResult.errors.length,
              })}
            </p>

            {applyResult.errors.length > 0 && (
              <div className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 p-2">
                <p className="mb-1 text-[11px] font-medium text-destructive">
                  {t('import.result.errors', {
                    count: applyResult.errors.length,
                  })}
                </p>
                <ul className="space-y-0.5">
                  {applyResult.errors.slice(0, 5).map((e, i) => (
                    <li
                      key={i}
                      className="text-[10px] leading-snug text-destructive/90"
                    >
                      · {e}
                    </li>
                  ))}
                  {applyResult.errors.length > 5 && (
                    <li className="text-[10px] italic text-destructive/70">
                      · ... {applyResult.errors.length - 5}{' '}
                      {t('import.result.errors', {
                        count: applyResult.errors.length - 5,
                      })}
                    </li>
                  )}
                </ul>
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 border-t border-border pt-3">
            {scopeType === MemoryScope.PROJECT && (
              <Link
                href={`/memory/projects/${encodeURIComponent(effectiveScopeId)}`}
                className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
              >
                <FolderOpen size={12} aria-hidden="true" />
                {t('import.result.viewProject')}
              </Link>
            )}
            <button
              type="button"
              onClick={handleImportAnother}
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            >
              <Upload size={12} aria-hidden="true" />
              {t('import.result.importAnother')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step indicator
// ---------------------------------------------------------------------------

interface StepIndicatorProps {
  current: WizardStep;
}

function StepIndicator({ current }: StepIndicatorProps) {
  const { t } = useTranslation('memory');

  const steps: Array<{ key: WizardStep; labelKey: string }> = [
    { key: 'select', labelKey: 'import.steps.select' },
    { key: 'preview', labelKey: 'import.steps.preview' },
    { key: 'result', labelKey: 'import.steps.result' },
  ];
  const currentIndex = steps.findIndex((s) => s.key === current);

  return (
    <ol
      className="flex items-center gap-1 text-[11px]"
      aria-label={t('import.title')}
    >
      {steps.map((s, i) => {
        const active = i === currentIndex;
        const done = i < currentIndex;
        return (
          <li key={s.key} className="flex items-center gap-1">
            <span
              className={cn(
                'flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-semibold transition-colors',
                active && 'bg-primary text-primary-foreground',
                done && 'bg-emerald-600 text-white',
                !active && !done && 'bg-muted text-muted-foreground',
              )}
              aria-current={active ? 'step' : undefined}
            >
              {i + 1}
            </span>
            <span
              className={cn(
                'font-medium',
                active ? 'text-foreground' : 'text-muted-foreground',
              )}
            >
              {t(s.labelKey)}
            </span>
            {i < steps.length - 1 && (
              <span
                aria-hidden="true"
                className="mx-1 h-px w-4 bg-border"
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// Error banner
// ---------------------------------------------------------------------------

interface ErrorBannerProps {
  error: MemoryApiError;
}

function ErrorBanner({ error }: ErrorBannerProps) {
  const { t } = useTranslation('memory');
  const suggestSettings =
    error.errorType === 'mode_disabled' ||
    error.errorType === 'mode_read_only';

  return (
    <div
      className="flex items-start justify-between gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-[11px] text-destructive"
      role="alert"
    >
      <span>{error.message}</span>
      {suggestSettings && (
        <Link
          href="/memory/settings"
          className="shrink-0 underline hover:opacity-80"
        >
          {t('errors.withSettings')}
        </Link>
      )}
    </div>
  );
}
