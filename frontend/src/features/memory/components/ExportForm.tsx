// src/features/memory/components/ExportForm.tsx

/**
 * The `/memory/export` form: scope + format picker + download.
 *
 * Two actions:
 *
 *   * **Download** -- calls `useExportZip` (or `useExportJson`
 *     depending on format), then triggers a browser download
 *     from the returned blob. Triggers the `download` API via a
 *     dynamically created `<a download>` element and a
 *     `URL.createObjectURL` link.
 *
 *   * **Inspect first** -- calls `useExportJson` regardless of
 *     the selected format, then renders a compact manifest +
 *     first-N-entries panel. Useful for verifying before saving.
 *
 * The form is DISABLED when `mode === 'disabled'` (the backend
 * rejects export in that state). This differs from the settings
 * page, which is always editable -- exports are reads, and reads
 * are forbidden when memory is off. The disabled state has a
 * tooltip pointing at the settings page, matching the spec's
 * "disable, don't hide" rule.
 */

'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Download, Eye, FileArchive, FileJson } from 'lucide-react';

import type { ExportFormat, MemoryStatusFilter } from '../types/enums';
import { MemoryScope } from '../types/enums';
import { EXPORT_DEFAULT_ENTRIES, EXPORT_MAX_ENTRIES } from '../types/import-export';
import {
  useExportJson,
  useExportZip,
} from '../api/mutations';
import { useMemorySettings } from '../api/queries';
import { cn } from '../lib/cn';
import { ScopeBadge } from './ScopeBadge';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ExportFormProps {
  /**
   * Initial scope. The user can change it if `allowScopeChange`
   * is true.
   */
  initialScope?: { scope_type: MemoryScope; scope_id: string };

  /** When `true`, allow the user to edit the scope fields. */
  allowScopeChange?: boolean;

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ExportForm({
  initialScope = { scope_type: MemoryScope.PROJECT, scope_id: '' },
  allowScopeChange = true,
  className,
}: ExportFormProps) {
  const { t } = useTranslation('memory');

  const settingsQuery = useMemorySettings();
  const isDisabled = settingsQuery.data?.mode === 'disabled';

  const exportZip = useExportZip();
  const exportJson = useExportJson();

  const [scopeType, setScopeType] = useState<MemoryScope>(
    initialScope.scope_type,
  );
  const [scopeId, setScopeId] = useState<string>(initialScope.scope_id);
  const [format, setFormat] = useState<ExportFormat>('zip');
  const [status, setStatus] = useState<MemoryStatusFilter>('all');
  const [limit, setLimit] = useState<number>(EXPORT_DEFAULT_ENTRIES);
  const [preview, setPreview] = useState<{
    manifest: Record<string, unknown>;
    memories: unknown[];
    truncated: boolean;
    totalMatched: number;
  } | null>(null);

  const trimmedScopeId = scopeId.trim();
  const formValid = trimmedScopeId.length > 0;
  const isPending = exportZip.isPending || exportJson.isPending;

  // ---- Download ---------------------------------------------------
  async function handleDownload() {
    if (!formValid) return;
    try {
      const result = await exportZip.mutateAsync({
        scope_type: scopeType,
        scope_id: trimmedScopeId,
        status,
        limit,
      });
      // Trigger the browser download from the blob.
      const url = URL.createObjectURL(result.blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = result.filename;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      // Defer revoke so the click handler completes.
      setTimeout(() => {
        URL.revokeObjectURL(url);
        a.remove();
      }, 500);
    } catch {
      // error surfaced below
    }
  }

  // ---- Inspect ----------------------------------------------------
  async function handleInspect() {
    if (!formValid) return;
    try {
      const result = await exportJson.mutateAsync({
        scope_type: scopeType,
        scope_id: trimmedScopeId,
        status,
        limit,
      });
      const entries = result.memories ?? [];
      setPreview({
        manifest: result.manifest ?? {},
        memories: entries,
        truncated: entries.length >= limit,
        totalMatched: entries.length,
      });
    } catch {
      // error surfaced below
    }
  }

  // ---- Render -----------------------------------------------------
  const error = exportZip.error ?? exportJson.error;

  return (
    <div className={cn('space-y-4', className)}>
      {isDisabled && (
        <p className="rounded-md border border-amber-300/60 bg-amber-50/60 p-2 text-[11px] text-amber-800 dark:border-amber-700/60 dark:bg-amber-900/10 dark:text-amber-300">
          {t('errors.mode_disabled')}
        </p>
      )}

      {/* Scope */}
      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('export.fields.scope')}
        </h3>
        {allowScopeChange ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[10rem_1fr]">
            <select
              value={scopeType}
              onChange={(e) => setScopeType(e.target.value as MemoryScope)}
              disabled={isPending || isDisabled}
              className="h-8 rounded-md border border-border bg-background px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            >
              {Object.values(MemoryScope).map((s) => (
                <option key={s} value={s}>
                  {t(`scope.${s}`)}
                </option>
              ))}
            </select>
            <input
              type="text"
              value={scopeId}
              onChange={(e) => setScopeId(e.target.value)}
              placeholder={t('scope.scopeId')}
              disabled={isPending || isDisabled}
              className="h-8 rounded-md border border-border bg-background px-2 text-xs font-mono text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            />
          </div>
        ) : (
          <ScopeBadge
            scope_type={scopeType}
            scope_id={scopeType === 'user' ? undefined : trimmedScopeId}
          />
        )}
      </section>

      {/* Format */}
      <section>
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('export.fields.format')}
        </h3>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {(['zip', 'json'] as const).map((f) => (
            <label
              key={f}
              className={cn(
                'flex items-center gap-2 rounded-md border bg-card p-2 text-xs',
                format === f
                  ? 'border-primary/60 bg-primary/5'
                  : 'border-border hover:bg-muted/40',
                (isPending || isDisabled) && 'cursor-not-allowed opacity-60',
              )}
            >
              <input
                type="radio"
                name="export-format"
                value={f}
                checked={format === f}
                onChange={() => setFormat(f)}
                disabled={isPending || isDisabled}
                className="h-3.5 w-3.5 border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
              />
              {f === 'zip' ? (
                <FileArchive size={12} aria-hidden="true" />
              ) : (
                <FileJson size={12} aria-hidden="true" />
              )}
              <span className="font-medium text-foreground">
                {t(`export.formats.${f}`)}
              </span>
            </label>
          ))}
        </div>
      </section>

      {/* Status + limit */}
      <section className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label
            htmlFor="export-status"
            className="mb-1 block text-[11px] font-medium text-foreground"
          >
            {t('export.fields.status')}
          </label>
          <select
            id="export-status"
            value={status}
            onChange={(e) => setStatus(e.target.value as MemoryStatusFilter)}
            disabled={isPending || isDisabled}
            className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
          >
            <option value="all">{t('export.statusOptions.all')}</option>
            <option value="active">{t('export.statusOptions.active')}</option>
          </select>
        </div>
        <div>
          <label
            htmlFor="export-limit"
            className="mb-1 block text-[11px] font-medium text-foreground"
          >
            {t('export.fields.limit')}
          </label>
          <input
            id="export-limit"
            type="number"
            value={limit}
            min={1}
            max={EXPORT_MAX_ENTRIES}
            step={100}
            onChange={(e) => {
              const v = Number(e.target.value);
              if (Number.isFinite(v) && v >= 1 && v <= EXPORT_MAX_ENTRIES) {
                setLimit(v);
              }
            }}
            disabled={isPending || isDisabled}
            className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs tabular-nums text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
          />
        </div>
      </section>

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <button
          type="button"
          onClick={handleDownload}
          disabled={!formValid || isPending || isDisabled}
          className={cn(
            'inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground',
            'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          <Download size={12} aria-hidden="true" />
          {exportZip.isPending ? t('common.loading') : t('export.actions.download')}
        </button>
        <button
          type="button"
          onClick={handleInspect}
          disabled={!formValid || isPending || isDisabled}
          className={cn(
            'inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground',
            'hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
        >
          <Eye size={12} aria-hidden="true" />
          {exportJson.isPending ? t('common.loading') : t('export.actions.inspect')}
        </button>
      </div>

      {/* Error */}
      {error && (
        <p
          className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-[11px] text-destructive"
          role="alert"
        >
          {error.message}
        </p>
      )}

      {/* Preview panel */}
      {preview && (
        <section className="space-y-2 rounded-md border border-border bg-muted/30 p-2.5">
          <div className="flex items-center justify-between text-[11px]">
            <span className="font-semibold text-foreground">
              {t('export.inspectPanel.title')}
            </span>
            <span className="text-muted-foreground">
              {t('export.inspectPanel.firstN', {
                shown: Math.min(preview.memories.length, 10),
                total: preview.totalMatched,
              })}
            </span>
          </div>
          {preview.truncated && (
            <p className="text-[10px] text-amber-700 dark:text-amber-400">
              {t('export.truncated', {
                limit,
                total: preview.totalMatched,
              })}
            </p>
          )}
          <ul className="space-y-0.5">
            {preview.memories.slice(0, 10).map((m, i) => {
              const e = m as { memory_id: string; content: string };
              return (
                <li
                  key={e.memory_id ?? i}
                  className="truncate text-[11px] text-foreground/80"
                  title={e.content}
                >
                  · {e.content?.slice(0, 100)}
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
