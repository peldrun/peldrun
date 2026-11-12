// src/app/memory/projects/[projectId]/page.tsx

/**
 * Project detail page (`/memory/projects/[projectId]`).
 *
 * Three sections, top to bottom:
 *
 *   1. **Stats** -- `ProjectStatsPanel`: counts, importance
 *      range, type distribution, top tags. Sourced from
 *      `useProjectStats(projectId)`.
 *
 *   2. **Bulk actions** -- `BulkActionsBar`: archive-all and
 *      purge-all with type-to-confirm dialogs.
 *
 *   3. **Settings override** -- `ProjectOverrideForm`: the
 *      per-project configuration delta.
 *
 * Backend mode interaction
 * ------------------------
 *
 *   * The STATS endpoint requires `mode.canRead`. In `disabled`
 *     mode, it returns `mode_disabled`. The page detects this
 *     and replaces the stats panel with a banner pointing at
 *     settings -- the override form and bulk actions still
 *     render (they gate on their own conditions internally).
 *
 *   * The OVERRIDE form uses `GET /settings/projects/{id}`,
 *     which is NOT mode-gated (spec §1.3). Even when memory is
 *     disabled, the user can still view and edit the project's
 *     override -- this is how they customize per-project
 *     behavior before re-enabling memory.
 *
 *   * BULK ACTIONS require `read_write`; `BulkActionsBar`
 *     disables its buttons with a tooltip in every other
 *     state.
 *
 * Routes and path depth
 * ---------------------
 *
 * This file lives 4 directory levels deep from `src/`:
 * `src/app/memory/projects/[projectId]/page.tsx`. Its
 * imports therefore use `../../../../features/...` (four
 * `../` to reach `src/`). A common mistake is using three
 * levels (like the sibling `projects/page.tsx` does) and
 * getting "module not found" at build time -- the depth
 * differs by exactly one segment.
 */

'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  ArrowLeft,
  FolderOpen,
  RefreshCw,
} from 'lucide-react';

import {
  useMemorySettings,
  useProjectStats,
} from '../../../../features/memory/api/queries';
import { ProjectStatsPanel } from '../../../../features/memory/components/ProjectStatsPanel';
import { ProjectOverrideForm } from '../../../../features/memory/components/ProjectOverrideForm';
import { BulkActionsBar } from '../../../../features/memory/components/BulkActionsBar';
import { EmptyState } from '../../../../features/memory/components/EmptyState';
import { cn } from '../../../../features/memory/lib/cn';

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ProjectDetailPage() {
  const { t } = useTranslation('memory');
  const params = useParams<{ projectId?: string | string[] }>();

  // Normalize the route param -- Next.js may give us a string
  // OR a string[] depending on version and segment type.
  const projectId = Array.isArray(params?.projectId)
    ? params.projectId[0]
    : params?.projectId;

  const settingsQuery = useMemorySettings();
  const mode = settingsQuery.data?.mode;
  const isDisabled = mode === 'disabled';
  const canReadStats = mode === 'read_write' || mode === 'read_only';

  const statsQuery = useProjectStats(projectId);

  // ---- Missing projectId (should not happen, but be safe) ---------
  if (!projectId) {
    return (
      <div className="mx-auto max-w-4xl p-4">
        <EmptyState
          icon={AlertTriangle}
          title={t('errors.invalid_project_id')}
          description={t('projects.empty.description')}
          size="spacious"
          action={
            <Link
              href="/memory/projects"
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            >
              <ArrowLeft size={12} aria-hidden="true" />
              {t('nav.projects')}
            </Link>
          }
        />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4">
      {/* ============ Header ============ */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Link
            href="/memory/projects"
            className="inline-flex items-center gap-1 rounded-md px-1 text-[11px] text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            <ArrowLeft size={11} aria-hidden="true" />
            {t('nav.projects')}
          </Link>
        </div>
        <Link
          href={`/memory/browse?scope=${encodeURIComponent(`project:${projectId}`)}`}
          className="inline-flex h-7 items-center gap-1 rounded-md border border-border bg-card px-2 text-[11px] font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
        >
          <FolderOpen size={11} aria-hidden="true" />
          {t('nav.browse')}
        </Link>
      </div>

      <div>
        <h2 className="text-base font-semibold text-foreground">
          {t('projects.detail.title', { id: projectId })}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('projects.detail.stats')}
        </p>
      </div>

      {/* ============ Disabled banner ============ */}
      {isDisabled && (
        <p className="flex items-start gap-2 rounded-md border border-amber-300/60 bg-amber-50/60 p-2.5 text-[11px] text-amber-800 dark:border-amber-700/60 dark:bg-amber-900/10 dark:text-amber-300">
          <AlertTriangle
            size={12}
            aria-hidden="true"
            className="mt-0.5 shrink-0"
          />
          <span>
            {t('errors.mode_disabled')}{' '}
            <Link
              href="/memory/settings"
              className="underline"
            >
              {t('errors.withSettings')}
            </Link>
          </span>
        </p>
      )}

      {/* ============ Stats ============ */}
      <section className="rounded-lg border border-border bg-card p-3">
        {!canReadStats ? (
          <p className="py-4 text-center text-[11px] text-muted-foreground">
            {t('projects.detail.emptyStats')}
          </p>
        ) : statsQuery.isLoading ? (
          <div className="space-y-2" aria-busy="true">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {[0, 1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="h-14 animate-pulse rounded-md bg-muted"
                />
              ))}
            </div>
            <div className="h-3 w-2/3 animate-pulse rounded bg-muted" />
            <div className="h-3 w-1/2 animate-pulse rounded bg-muted" />
          </div>
        ) : statsQuery.error ? (
          <EmptyState
            icon={RefreshCw}
            title={t('errors.generic')}
            description={statsQuery.error.message}
            action={
              <button
                type="button"
                onClick={() => statsQuery.refetch()}
                className={cn(
                  'inline-flex h-7 items-center gap-1 rounded-md border border-border bg-card px-2 text-[11px] font-medium text-foreground',
                  'hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
                )}
              >
                {t('common.retry')}
              </button>
            }
          />
        ) : statsQuery.data ? (
          <ProjectStatsPanel stats={statsQuery.data.stats} />
        ) : null}
      </section>

      {/* ============ Bulk actions ============ */}
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('projects.bulk.archiveButton').replace(
            t('common.empty') === 'Empty' ? /\s+\w+$/ : /$/,
            '',
          )}
        </h3>
        <BulkActionsBar
          projectId={projectId}
          total={statsQuery.data?.stats.total ?? 0}
        />
      </section>

      {/* ============ Settings override ============ */}
      <ProjectOverrideForm projectId={projectId} />
    </div>
  );
}
