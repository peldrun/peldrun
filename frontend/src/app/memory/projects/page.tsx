// src/app/memory/projects/page.tsx

/**
 * Projects list page (`/memory/projects`).
 *
 * Renders a responsive grid of `ProjectCard` tiles, one per
 * project that has at least one memory entry. Projects with
 * zero entries do NOT appear (backend behavior -- the endpoint
 * filters them out). The empty state explains this so users do
 * not think the feature is broken.
 *
 * The page itself is a thin composition:
 *
 *   * Fetch via `useProjectsList`.
 *   * Render four states: loading skeleton, error, empty, grid.
 *
 * Client-only by inheritance: the parent `MemoryLayout` uses a
 * `mounted` gate to avoid SSR/i18n hydration mismatches, so
 * this page never server-renders. That means `useTranslation`
 * and any other client hooks in this file are safe -- there is
 * no server render to mismatch against.
 *
 * No Suspense boundary is needed here: the page does not call
 * `useSearchParams()`. Only `useProjectsList`, which is a
 * standard query hook.
 */

'use client';

import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import { FolderOpen, RefreshCw } from 'lucide-react';

import { useProjectsList } from '../../../features/memory/api/queries';
import { ProjectCard } from '../../../features/memory/components/ProjectCard';
import { EmptyState } from '../../../features/memory/components/EmptyState';

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ProjectsListPage() {
  const { t } = useTranslation('memory');
  const projectsQuery = useProjectsList();

  // ---- Title (shared across all three branches below) -------------
  const header = (
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <div>
        <h2 className="text-base font-semibold text-foreground">
          {t('projects.title')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('projects.empty.description')}
        </p>
      </div>
    </div>
  );

  // ---- Loading ----------------------------------------------------
  if (projectsQuery.isLoading) {
    return (
      <div className="mx-auto max-w-6xl space-y-4">
        {header}
        <div
          className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3"
          aria-busy="true"
        >
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div
              key={i}
              className="h-24 animate-pulse rounded-lg border border-border bg-muted/40"
            />
          ))}
        </div>
      </div>
    );
  }

  // ---- Error ------------------------------------------------------
  if (projectsQuery.error) {
    return (
      <div className="mx-auto max-w-6xl space-y-4">
        {header}
        <EmptyState
          icon={RefreshCw}
          title={t('errors.generic')}
          description={projectsQuery.error.message}
          size="spacious"
          action={
            <button
              type="button"
              onClick={() => projectsQuery.refetch()}
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            >
              {t('common.retry')}
            </button>
          }
        />
      </div>
    );
  }

  // ---- Empty ------------------------------------------------------
  const projects = projectsQuery.data?.projects ?? [];
  if (projects.length === 0) {
    return (
      <div className="mx-auto max-w-6xl space-y-4">
        {header}
        <EmptyState
          icon={FolderOpen}
          title={t('projects.empty.title')}
          description={t('projects.empty.description')}
          size="spacious"
          action={
            <Link
              href="/memory/browse"
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            >
              {t('nav.browse')}
            </Link>
          }
        />
      </div>
    );
  }

  // ---- Grid -------------------------------------------------------
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      {header}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {projects.map((project) => (
          <Link
            key={project.project_id}
            href={`/memory/projects/${encodeURIComponent(project.project_id)}`}
            className="rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <ProjectCard project={project} />
          </Link>
        ))}
      </div>
    </div>
  );
}
