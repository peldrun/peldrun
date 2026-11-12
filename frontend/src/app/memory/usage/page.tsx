// src/app/memory/usage/page.tsx

/**
 * Usage dashboard (`/memory/usage`).
 *
 * Monitoring surface for the memory subsystem. Four sections
 * arranged top-to-bottom:
 *
 *   1. **Period selector** -- day / week / month / all. Drives
 *      every query on the page. Changing it re-fetches all
 *      three, which is correct: the numbers must agree across
 *      sections.
 *
 *   2. **KPI cards** -- runs, tokens, budget utilization, and
 *      drop rate over the selected period. Includes an
 *      automatic budget warning banner when utilization
 *      crosses `settings.token_budget.warn_threshold`.
 *
 *   3. **Time series** -- a line chart of token consumption
 *      per bucket. Granularity is separately selectable
 *      (day / week / month) so a user can zoom out without
 *      changing the period.
 *
 *   4. **Breakdown** -- a pie chart of tokens by dimension
 *      (scope / project / chat). The dimension is separately
 *      selectable so a user can slice the same tokens four
 *      different ways without refetching the summary.
 *
 * Empty / loading / error states
 * ------------------------------
 *
 * The three sections fail INDEPENDENTLY:
 *
 *   * If the summary query fails, the KPI cards show the
 *     error, but the charts still render from their own
 *     queries.
 *
 *   * If the timeseries query fails, the timeseries chart
 *     shows the error, but the KPIs remain.
 *
 *   * A period with zero runs collapses to a single
 *     `<EmptyState>` below the period selector -- there is
 *     no point in showing four cards of zeros.
 *
 * Extra drop-rate warning
 * -----------------------
 *
 * The spec (F7.5) calls out a specific case: when the drop
 * rate exceeds 30%, the token budget may be too small for the
 * workload. `UsageKpiCards` already colors the drop-rate KPI
 * in warning tones at that threshold, but a text banner makes
 * the diagnosis actionable ("consider raising budget").
 * Rendered below the KPI cards when `drop_rate > 0.3`.
 */

'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  BarChart3,
  RefreshCw,
  type LucideIcon,
} from 'lucide-react';

import type {
  UsagePeriod,
  UsageGranularity,
} from '../../../features/memory/types/enums';
import type { MemoryApiError } from '../../../features/memory/types/errors';
import {
  useMemorySettings,
  useUsageBreakdown,
  useUsageSummary,
  useUsageTimeseries,
} from '../../../features/memory/api/queries';
import { cn } from '../../../features/memory/lib/cn';
import { UsageKpiCards } from '../../../features/memory/components/UsageKpiCards';
import { UsageBreakdownChart } from '../../../features/memory/components/UsageBreakdownChart';
import { UsageTimeseriesChart } from '../../../features/memory/components/UsageTimeseriesChart';
import { EmptyState } from '../../../features/memory/components/EmptyState';

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

/**
 * The period options shown in the top selector.
 *
 * Fixed order and labels (from `memory.json`), no dynamic
 * derivation from `ALL_USAGE_PERIODS` -- the display order
 * matters here (ascending span), and `all` is deliberately
 * last because it is the least common view.
 */
const PERIOD_OPTIONS: Array<{ value: UsagePeriod; labelKey: string }> = [
  { value: 'day', labelKey: 'usage.period.day' },
  { value: 'week', labelKey: 'usage.period.week' },
  { value: 'month', labelKey: 'usage.period.month' },
  { value: 'all', labelKey: 'usage.period.all' },
];

/**
 * The granularity options shown in the timeseries header.
 */
const GRANULARITY_OPTIONS: Array<{
  value: UsageGranularity;
  labelKey: string;
}> = [
  { value: 'day', labelKey: 'usage.timeseries.granularity.day' },
  { value: 'week', labelKey: 'usage.timeseries.granularity.week' },
  { value: 'month', labelKey: 'usage.timeseries.granularity.month' },
];

/**
 * The dimension options shown in the breakdown header.
 *
 * `source` is intentionally omitted from the UI -- it is an
 * accepted alias for `scope` on the wire, so exposing both
 * would confuse users. The backend normalizes the response's
 * `by` field to `scope` either way.
 */
const DIMENSION_OPTIONS: Array<{
  value: 'scope' | 'project' | 'chat';
  labelKey: string;
}> = [
  { value: 'scope', labelKey: 'usage.breakdown.dimensions.scope' },
  { value: 'project', labelKey: 'usage.breakdown.dimensions.project' },
  { value: 'chat', labelKey: 'usage.breakdown.dimensions.chat' },
];

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function MemoryUsagePage() {
  const { t } = useTranslation('memory');

  // ---- Local selectors -------------------------------------------
  const [period, setPeriod] = useState<UsagePeriod>('month');
  const [granularity, setGranularity] = useState<UsageGranularity>('day');
  const [breakdownBy, setBreakdownBy] = useState<
    'scope' | 'project' | 'chat'
  >('scope');

  // ---- Queries ---------------------------------------------------
  const settingsQuery = useMemorySettings();
  const summaryQuery = useUsageSummary({
    period,
    chat_id: null,
    project_id: null,
  });
  const timeseriesQuery = useUsageTimeseries({ period, granularity });
  const breakdownQuery = useUsageBreakdown({
    by: breakdownBy,
    period,
    chat_id: null,
    project_id: null,
  });

  const warnThreshold = settingsQuery.data?.token_budget.warn_threshold;
  const summary = summaryQuery.data;
  const isEmpty = summary !== undefined && summary.run_count === 0;

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      {/* ============ Title + period selector ============ */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-foreground">
            {t('usage.title')}
          </h2>
          <p className="text-[11px] text-muted-foreground">
            {t('usage.description')}
          </p>
        </div>

        <div
          className="inline-flex rounded-md border border-border bg-card p-0.5"
          role="group"
          aria-label={t('usage.period.label')}
        >
          {PERIOD_OPTIONS.map((opt) => {
            const active = period === opt.value;
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => setPeriod(opt.value)}
                aria-pressed={active}
                className={cn(
                  'rounded px-2.5 py-1 text-[11px] font-medium transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 focus-visible:ring-offset-background',
                  active
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                )}
              >
                {t(opt.labelKey)}
              </button>
            );
          })}
        </div>
      </div>

      {/* ============ KPI cards ============ */}
      <section>
        {summaryQuery.isLoading ? (
          <KpiSkeleton />
        ) : summaryQuery.error ? (
          <ErrorCard
            error={summaryQuery.error}
            onRetry={() => summaryQuery.refetch()}
          />
        ) : isEmpty ? (
          <EmptyState
            icon={BarChart3}
            title={t('usage.empty')}
            size="spacious"
          />
        ) : summary ? (
          <>
            <UsageKpiCards summary={summary} warnThreshold={warnThreshold} />

            {/* Extra drop-rate banner (F7.5). Distinct from
                the budget-utilization warning inside
                UsageKpiCards -- this one fires only when the
                drop rate alone signals "the budget is too
                small for the workload". */}
            {summary.drop_rate > 0.3 && (
              <p
                className="mt-2 flex items-start gap-1.5 rounded-md border border-amber-300/60 bg-amber-50/60 px-2.5 py-1.5 text-[11px] text-amber-800 dark:border-amber-700/60 dark:bg-amber-900/10 dark:text-amber-300"
                role="status"
              >
                <AlertTriangle
                  size={11}
                  aria-hidden="true"
                  className="mt-0.5 shrink-0"
                />
                <span>
                  {t('usage.budgetWarning', {
                    pct: `${(summary.drop_rate * 100).toFixed(0)}`,
                  })}
                </span>
              </p>
            )}
          </>
        ) : null}
      </section>

      {/* ============ Charts (only when not empty) ============ */}
      {!isEmpty && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {/* ---- Timeseries ---- */}
          <section className="rounded-lg border border-border bg-card p-3">
            <ChartHeader
              title={t('usage.timeseries.title')}
              icon={BarChart3}
              selectId="usage-granularity"
              selectValue={granularity}
              selectLabel={t('usage.timeseries.granularity.day')}
              onSelect={(v) => setGranularity(v as UsageGranularity)}
              options={GRANULARITY_OPTIONS.map((o) => ({
                value: o.value,
                label: t(o.labelKey),
              }))}
            />

            {timeseriesQuery.isLoading ? (
              <ChartSkeleton height={240} />
            ) : timeseriesQuery.error ? (
              <ErrorCard
                error={timeseriesQuery.error}
                onRetry={() => timeseriesQuery.refetch()}
              />
            ) : timeseriesQuery.data ? (
              <UsageTimeseriesChart
                data={timeseriesQuery.data}
                height={240}
              />
            ) : null}
          </section>

          {/* ---- Breakdown ---- */}
          <section className="rounded-lg border border-border bg-card p-3">
            <ChartHeader
              title={t('usage.breakdown.title', {
                dimension: t(
                  `usage.breakdown.dimensions.${breakdownBy}`,
                ),
              })}
              icon={BarChart3}
              selectId="usage-breakdown-by"
              selectValue={breakdownBy}
              selectLabel={t('usage.breakdown.dimensions.scope')}
              onSelect={(v) =>
                setBreakdownBy(v as 'scope' | 'project' | 'chat')
              }
              options={DIMENSION_OPTIONS.map((o) => ({
                value: o.value,
                label: t(o.labelKey),
              }))}
            />

            {breakdownQuery.isLoading ? (
              <ChartSkeleton height={220} />
            ) : breakdownQuery.error ? (
              <ErrorCard
                error={breakdownQuery.error}
                onRetry={() => breakdownQuery.refetch()}
              />
            ) : breakdownQuery.data ? (
              <UsageBreakdownChart
                data={breakdownQuery.data}
                height={220}
              />
            ) : null}
          </section>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface ChartHeaderProps {
  title: string;
  icon: LucideIcon;
  selectId: string;
  selectValue: string;
  selectLabel: string;
  onSelect: (value: string) => void;
  options: Array<{ value: string; label: string }>;
}

function ChartHeader({
  title,
  icon: Icon,
  selectId,
  selectValue,
  selectLabel,
  onSelect,
  options,
}: ChartHeaderProps) {
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <h3 className="inline-flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        <Icon size={11} aria-hidden="true" />
        {title}
      </h3>
      <select
        id={selectId}
        value={selectValue}
        onChange={(e) => onSelect(e.target.value)}
        aria-label={selectLabel}
        className="h-6 rounded border border-border bg-background px-1 text-[10px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

interface ErrorCardProps {
  error: MemoryApiError;
  onRetry: () => void;
}

function ErrorCard({ error, onRetry }: ErrorCardProps) {
  const { t } = useTranslation('memory');
  return (
    <div
      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-[11px] text-destructive"
      role="alert"
    >
      <span className="min-w-0 flex-1 truncate">{error.message}</span>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex items-center gap-1 rounded border border-destructive/40 bg-card px-2 py-0.5 font-medium text-destructive hover:bg-destructive/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive"
      >
        <RefreshCw size={10} aria-hidden="true" />
        {t('common.retry')}
      </button>
    </div>
  );
}

function KpiSkeleton() {
  return (
    <div
      className="grid grid-cols-2 gap-2 lg:grid-cols-4"
      aria-busy="true"
    >
      {[0, 1, 2, 3].map((i) => (
        <div
          key={i}
          className="h-16 animate-pulse rounded-lg border border-border bg-muted/40"
        />
      ))}
    </div>
  );
}

function ChartSkeleton({ height }: { height: number }) {
  return (
    <div
      className="w-full animate-pulse rounded-md bg-muted/40"
      style={{ height }}
      aria-busy="true"
    />
  );
}
