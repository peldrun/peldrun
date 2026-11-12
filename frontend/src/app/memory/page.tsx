// src/app/memory/page.tsx

/**
 * The memory dashboard home (`/memory`).
 *
 * Sections:
 *
 *   1. Disabled-memory banner (when `mode === 'disabled'`).
 *   2. Four KPI cards (runs, tokens, budget, drop rate) over
 *      the last 30 days.
 *   3. A compact tokens-over-time chart (last 7 days, daily
 *      buckets).
 *   4. Quick actions (new memory, import, export, usage).
 *   5. Recently updated projects (top 5 by last_updated).
 *
 * Every section renders independently -- if the usage query
 * fails, the KPIs show a skeleton while the quick actions and
 * projects section still work. That is deliberate: the
 * dashboard is a navigation hub, and one failing widget
 * should never hide the way to the pages that DO work.
 */

'use client';

import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import {
  BarChart3,
  Brain,
  Download,
  FolderOpen,
  Plus,
  Upload,
} from 'lucide-react';

import {
  useMemorySettings,
  useProjectsList,
  useUsageSummary,
  useUsageTimeseries,
} from '../../features/memory/api/queries';
import { UsageKpiCards } from '../../features/memory/components/UsageKpiCards';
import { UsageTimeseriesChart } from '../../features/memory/components/UsageTimeseriesChart';
import { ProjectCard } from '../../features/memory/components/ProjectCard';
import { EmptyState } from '../../features/memory/components/EmptyState';
import { cn } from '../../features/memory/lib/cn';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function MemoryDashboardPage() {
  const { t } = useTranslation('memory');

  const settingsQuery = useMemorySettings();
  const summaryQuery = useUsageSummary({
    period: 'month',
    chat_id: null,
    project_id: null,
  });
  const timeseriesQuery = useUsageTimeseries({
    period: 'week',
    granularity: 'day',
  });
  const projectsQuery = useProjectsList();

  const mode = settingsQuery.data?.mode;
  const isDisabled = mode === 'disabled';

  // Top five most recently-updated projects.
  const recentProjects = [...(projectsQuery.data?.projects ?? [])]
    .sort((a, b) => b.last_updated - a.last_updated)
    .slice(0, 5);

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      {/* ============ Title ============ */}
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-foreground">
            {t('dashboard.title')}
          </h2>
          <p className="text-[11px] text-muted-foreground">
            {t('dashboard.subtitle')}
          </p>
        </div>
        {mode && (
          <span className="text-[10px] text-muted-foreground">
            {t('usage.period.label')}: {t('usage.period.month')}
          </span>
        )}
      </div>

      {/* ============ Disabled banner ============ */}
      {isDisabled && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-300/60 bg-amber-50/60 p-3 text-xs dark:border-amber-700/60 dark:bg-amber-900/10">
          <div className="flex items-start gap-2">
            <Brain
              size={14}
              aria-hidden="true"
              className="mt-0.5 shrink-0 text-amber-700 dark:text-amber-400"
            />
            <div>
              <p className="font-medium text-amber-800 dark:text-amber-300">
                {t('mode.disabled.name')}
              </p>
              <p className="text-[11px] text-amber-700/80 dark:text-amber-400/80">
                {t('mode.disabled.description')}
              </p>
            </div>
          </div>
          <Link
            href="/memory/settings"
            className="inline-flex h-7 items-center gap-1.5 rounded-md bg-amber-600 px-2.5 text-[11px] font-medium text-white hover:bg-amber-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2"
          >
            {t('mode.enable')}
          </Link>
        </div>
      )}

      {/* ============ KPI cards ============ */}
      {summaryQuery.isLoading ? (
        <KpiSkeleton />
      ) : summaryQuery.error ? (
        <p
          className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-[11px] text-destructive"
          role="alert"
        >
          {summaryQuery.error.message}
        </p>
      ) : summaryQuery.data ? (
        <UsageKpiCards
          summary={summaryQuery.data}
          warnThreshold={settingsQuery.data?.token_budget.warn_threshold}
        />
      ) : null}

      {/* ============ Mini chart ============ */}
      {timeseriesQuery.data && timeseriesQuery.data.points.length > 0 && (
        <section className="rounded-lg border border-border bg-card p-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t('usage.timeseries.title')}
          </h3>
          <UsageTimeseriesChart data={timeseriesQuery.data} height={180} />
        </section>
      )}

      {/* ============ Quick actions ============ */}
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('dashboard.quickActions')}
        </h3>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          <QuickAction
            href="/memory/browse"
            icon={Plus}
            label={t('entry.drafts.createTitle')}
            hint={t('dashboard.quickActionsHint.newMemory')}
            tone="primary"
          />
          <QuickAction
            href="/memory/import"
            icon={Upload}
            label={t('nav.import')}
            hint={t('dashboard.quickActionsHint.import')}
          />
          <QuickAction
            href="/memory/export"
            icon={Download}
            label={t('nav.export')}
            hint={t('dashboard.quickActionsHint.export')}
          />
          <QuickAction
            href="/memory/usage"
            icon={BarChart3}
            label={t('nav.usage')}
            hint={t('dashboard.quickActionsHint.usage')}
          />
        </div>
      </section>

      {/* ============ Recent projects ============ */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t('dashboard.recentActivity')}
          </h3>
          {recentProjects.length > 0 && (
            <Link
              href="/memory/projects"
              className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            >
              <FolderOpen size={10} aria-hidden="true" />
              {t('nav.projects')}
            </Link>
          )}
        </div>

        {projectsQuery.isLoading ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                className="h-24 animate-pulse rounded-lg border border-border bg-muted/40"
              />
            ))}
          </div>
        ) : recentProjects.length === 0 ? (
          <EmptyState
            icon={FolderOpen}
            title={t('dashboard.recentActivityEmpty')}
            description={t('projects.empty.description')}
          />
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {recentProjects.map((project) => (
              <Link
                key={project.project_id}
                href={`/memory/projects/${encodeURIComponent(project.project_id)}`}
                className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background rounded-lg"
              >
                <ProjectCard project={project} />
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface QuickActionProps {
  href: string;
  icon: React.ComponentType<{ size?: number; strokeWidth?: number }>;
  label: string;
  hint: string;
  tone?: 'primary' | 'default';
}

function QuickAction({
  href,
  icon: Icon,
  label,
  hint,
  tone = 'default',
}: QuickActionProps) {
  return (
    <Link
      href={href}
      className={cn(
        'flex flex-col gap-1 rounded-lg border p-2.5 text-start transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        tone === 'primary'
          ? 'border-primary/40 bg-primary/5 hover:bg-primary/10'
          : 'border-border bg-card hover:bg-muted/40',
      )}
    >
      <div
        className={cn(
          'flex h-6 w-6 items-center justify-center rounded-md',
          tone === 'primary'
            ? 'bg-primary/15 text-primary'
            : 'bg-muted text-muted-foreground',
        )}
        aria-hidden="true"
      >
        <Icon size={12} strokeWidth={2} />
      </div>
      <div>
        <p className="text-[11px] font-medium text-foreground">{label}</p>
        <p className="text-[10px] leading-snug text-muted-foreground">
          {hint}
        </p>
      </div>
    </Link>
  );
}

function KpiSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
      {[0, 1, 2, 3].map((i) => (
        <div
          key={i}
          className="h-16 animate-pulse rounded-lg border border-border bg-muted/40"
        />
      ))}
    </div>
  );
}
