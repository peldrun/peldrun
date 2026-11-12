// src/features/memory/components/ProjectStatsPanel.tsx

/**
 * Detailed statistics for one project's memory.
 *
 * Rendered on the project detail page above the settings
 * override form. Shows:
 *
 *   * Total / active / archived / superseded counts.
 *   * Importance range (min, max, avg).
 *   * A type distribution (text bars -- the full pie lives in
 *     the usage dashboard, not here; a text bar is a better fit
 *     for the compact detail panel and needs no chart library).
 *   * Top tags with their counts.
 *
 * Empty-project handling
 * ----------------------
 *
 * A project reached via a direct URL may have zero entries.
 * The backend returns `importance` and `created_range` with
 * sentinel values (0). The panel detects "empty" via
 * `total === 0` and swaps the whole body for an
 * `<EmptyState>` -- it never tries to render the sentinel
 * values, which would be misleading.
 */

'use client';

import { useTranslation } from 'react-i18next';
import { BarChart3 } from 'lucide-react';

import type { ProjectStats } from '../types/responses';
import { cn } from '../lib/cn';
import { EmptyState } from './EmptyState';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ProjectStatsPanelProps {
  stats: ProjectStats;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ProjectStatsPanel({
  stats,
  className,
}: ProjectStatsPanelProps) {
  const { t } = useTranslation('memory');

  if (stats.total === 0) {
    return (
      <EmptyState
        icon={BarChart3}
        title={t('projects.detail.emptyStats')}
        description={t('projects.empty.description')}
        className={className}
      />
    );
  }

  const maxTypeCount = stats.types.reduce(
    (m, x) => Math.max(m, x.count),
    1,
  );

  return (
    <div className={cn('space-y-4', className)}>
      {/* ---- Counts ------------------------------------------ */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label={t('projects.columns.total')} value={stats.total} />
        <Stat label={t('projects.columns.active')} value={stats.active} tone="emerald" />
        <Stat
          label={t('projects.columns.archived')}
          value={stats.archived}
          tone="slate"
        />
        <Stat
          label={t('projects.columns.superseded')}
          value={stats.superseded}
          tone="amber"
        />
      </div>

      {/* ---- Importance range -------------------------------- */}
      <section>
        <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t('projects.detail.importanceRange')}
        </h4>
        <div className="flex items-center gap-3 text-xs text-foreground">
          <span className="tabular-nums">
            <span className="text-muted-foreground">min </span>
            {stats.importance.min}
          </span>
          <span className="tabular-nums">
            <span className="text-muted-foreground">avg </span>
            {Number.isFinite(stats.importance.avg)
              ? stats.importance.avg.toFixed(1)
              : '—'}
          </span>
          <span className="tabular-nums">
            <span className="text-muted-foreground">max </span>
            {stats.importance.max}
          </span>
        </div>
      </section>

      {/* ---- Type distribution ------------------------------- */}
      <section>
        <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t('projects.detail.typeDistribution')}
        </h4>
        {stats.types.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            {t('common.empty')}
          </p>
        ) : (
          <ul className="space-y-1">
            {stats.types.map((row) => {
              const pct = Math.round((row.count / maxTypeCount) * 100);
              return (
                <li
                  key={row.memory_type}
                  className="flex items-center gap-2 text-[11px]"
                >
                  <span className="w-20 shrink-0 truncate text-foreground">
                    {t(`type.${row.memory_type}`, {
                      defaultValue: row.memory_type,
                    })}
                  </span>
                  <div className="flex h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                    <div
                      className="bg-primary/70"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="w-6 shrink-0 text-end tabular-nums text-muted-foreground">
                    {row.count}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* ---- Top tags ---------------------------------------- */}
      <section>
        <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t('projects.detail.topTags')}
        </h4>
        {stats.top_tags.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            {t('common.empty')}
          </p>
        ) : (
          <div className="flex flex-wrap gap-1">
            {stats.top_tags.map((tag) => (
              <span
                key={tag.tag}
                className="inline-flex items-center gap-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
              >
                {tag.tag}
                <span className="tabular-nums opacity-60">{tag.count}</span>
              </span>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stat pill
// ---------------------------------------------------------------------------

interface StatProps {
  label: string;
  value: number;
  tone?: 'default' | 'emerald' | 'slate' | 'amber';
}

function Stat({ label, value, tone = 'default' }: StatProps) {
  const valueClass =
    tone === 'emerald'
      ? 'text-emerald-600 dark:text-emerald-400'
      : tone === 'slate'
        ? 'text-slate-600 dark:text-slate-400'
        : tone === 'amber'
          ? 'text-amber-600 dark:text-amber-400'
          : 'text-foreground';
  return (
    <div className="rounded-md border border-border bg-muted/30 px-2 py-1.5">
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className={cn('text-sm font-semibold tabular-nums', valueClass)}>
        {value}
      </p>
    </div>
  );
}
