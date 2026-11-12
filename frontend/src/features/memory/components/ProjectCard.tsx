// src/features/memory/components/ProjectCard.tsx

/**
 * A clickable tile summarizing one project's memory footprint.
 *
 * Used by `/memory/projects` in a responsive grid. Shows the
 * total / active / archived / superseded counts as a compact
 * horizontal breakdown, a thin proportion bar, and a relative
 * "last updated" timestamp.
 *
 * The bar segments are proportional to their category within
 * `total`. When `total === 0` the bar is hidden (the backend
 * does not return such projects in the list, but the type
 * allows it -- defensive rendering).
 */

'use client';

import { useTranslation } from 'react-i18next';
import { Folder, ArrowRight } from 'lucide-react';

import type { MemoryProjectSummary } from '../types/responses';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Time formatting
// ---------------------------------------------------------------------------

/**
 * Format a Unix-seconds timestamp as a relative string.
 *
 * `t` is passed in (rather than imported from a hook) so that
 * the function has no React dependency and can be tested or
 * reused from non-component code. Pluralization is resolved by
 * i18next from the `dates.*` keys.
 */
function formatRelativeTime(
  unixSeconds: number,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const now = Date.now() / 1000;
  const diff = Math.max(0, now - unixSeconds);
  if (diff < 60) return t('dates.justNow');
  if (diff < 3600) {
    return t('dates.minutesAgo', { count: Math.floor(diff / 60) });
  }
  if (diff < 86400) {
    return t('dates.hoursAgo', { count: Math.floor(diff / 3600) });
  }
  if (diff < 604800) {
    return t('dates.daysAgo', { count: Math.floor(diff / 86400) });
  }
  if (diff < 2592000) {
    return t('dates.weeksAgo', { count: Math.floor(diff / 604800) });
  }
  if (diff < 31536000) {
    return t('dates.monthsAgo', { count: Math.floor(diff / 2592000) });
  }
  return t('dates.yearsAgo', { count: Math.floor(diff / 31536000) });
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ProjectCardProps {
  project: MemoryProjectSummary;
  onClick?: () => void;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ProjectCard({
  project,
  onClick,
  className,
}: ProjectCardProps) {
  const { t } = useTranslation('memory');

  const total = Math.max(0, project.total);
  const active = Math.max(0, project.active);
  const archived = Math.max(0, project.archived);
  const superseded = Math.max(0, project.superseded);
  const hasData = total > 0;

  // Percentages for the proportion bar. Clamped so a rounding
  // glitch cannot push the sum above 100 and make the bar
  // overflow its track.
  const activePct = hasData ? Math.round((active / total) * 100) : 0;
  const archivedPct = hasData ? Math.round((archived / total) * 100) : 0;
  const supersededPct = hasData
    ? Math.max(0, 100 - activePct - archivedPct)
    : 0;

  const Comp: 'button' | 'article' = onClick ? 'button' : 'article';

  return (
    <Comp
      onClick={onClick}
      type={onClick ? 'button' : undefined}
      className={cn(
        'group flex w-full flex-col gap-3 rounded-lg border border-border bg-card p-3 text-start text-foreground',
        onClick &&
          'cursor-pointer transition-colors hover:border-primary/40 hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
        className,
      )}
      aria-label={t('projects.detail.title', { id: project.project_id })}
    >
      {/* Header */}
      <div className="flex items-center gap-2">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
          <Folder size={13} aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate font-mono text-xs font-medium text-foreground">
            {project.project_id}
          </p>
          <p
            className="text-[10px] text-muted-foreground"
            suppressHydrationWarning
          >
            {formatRelativeTime(project.last_updated, t)}
          </p>
        </div>
        {onClick && (
          <ArrowRight
            size={12}
            aria-hidden="true"
            className="shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5"
          />
        )}
      </div>

      {/* Counts */}
      <div className="flex items-baseline gap-3 text-xs">
        <span className="font-semibold tabular-nums text-foreground">
          {total}
        </span>
        <span className="text-[11px] text-muted-foreground">
          {active} {t('status.active').toLowerCase()} ·{' '}
          {archived} {t('status.archived').toLowerCase()}
          {superseded > 0 && (
            <> · {superseded} {t('status.superseded').toLowerCase()}</>
          )}
        </span>
      </div>

      {/* Proportion bar */}
      {hasData && (
        <div
          aria-hidden="true"
          className="flex h-1 overflow-hidden rounded-full bg-muted"
        >
          <div
            className="bg-emerald-500"
            style={{ width: `${activePct}%` }}
          />
          <div
            className="bg-slate-400"
            style={{ width: `${archivedPct}%` }}
          />
          <div
            className="bg-amber-500"
            style={{ width: `${supersededPct}%` }}
          />
        </div>
      )}
    </Comp>
  );
}
