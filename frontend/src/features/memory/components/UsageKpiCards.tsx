// src/features/memory/components/UsageKpiCards.tsx

/**
 * The usage dashboard's four KPI cards.
 *
 * Reads a summary object (already fetched by the caller) and
 * renders one card per metric:
 *
 *   * Runs           -- total runs in the period.
 *   * Total tokens   -- tokens spent on memory (context + overhead).
 *   * Budget used    -- `budget_utilization` as a percentage.
 *   * Drop rate      -- fraction of relevant entries dropped for
 *                       budget reasons.
 *
 * Also renders a "budget warning" band when `budget_utilization`
 * exceeds the settings' `warn_threshold`. The threshold is
 * passed in (rather than fetched here) so the caller decides
 * whether to surface the warning -- a test or a compact summary
 * might want the cards without the banner.
 *
 * Number formatting
 * -----------------
 *
 * Token counts are abbreviated (1.2K, 15.2K, 1.2M) via
 * `formatTokens` below. Percentages are rounded to whole
 * integers unless the value is below 10, where one decimal
 * matters for readability (e.g. "3.4%" vs. "3%").
 */

'use client';

import { useTranslation } from 'react-i18next';
import {
  Activity,
  AlertTriangle,
  Coins,
  Gauge,
  TrendingDown,
} from 'lucide-react';

import type { UsageSummaryResponse } from '../types/responses';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function formatTokens(
  n: number,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  if (!Number.isFinite(n)) return '—';
  if (n < 1000) return String(n);
  if (n < 1_000_000) {
    return t('format.tokensK', { value: (n / 1000).toFixed(1) });
  }
  return t('format.tokensM', { value: (n / 1_000_000).toFixed(2) });
}

function formatPct(n: number): string {
  if (!Number.isFinite(n)) return '—';
  const pct = n * 100;
  if (pct >= 10) return `${pct.toFixed(0)}%`;
  return `${pct.toFixed(1)}%`;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface UsageKpiCardsProps {
  summary: UsageSummaryResponse;

  /**
   * The fraction (0..1) of budget utilization above which the
   * "budget warning" band appears. Pass `warn_threshold` from
   * settings. When `undefined`, the band is never shown.
   */
  warnThreshold?: number;

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function UsageKpiCards({
  summary,
  warnThreshold,
  className,
}: UsageKpiCardsProps) {
  const { t } = useTranslation('memory');

  const showWarning =
    warnThreshold !== undefined &&
    summary.budget_utilization >= warnThreshold;

  return (
    <div className={cn('space-y-2', className)}>
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Kpi
          icon={Activity}
          label={t('usage.kpis.runs')}
          value={String(summary.run_count)}
          tone="default"
        />
        <Kpi
          icon={Coins}
          label={t('usage.kpis.totalTokens')}
          value={formatTokens(summary.total_tokens, t)}
          tone="primary"
        />
        <Kpi
          icon={Gauge}
          label={t('usage.kpis.budgetUtil')}
          value={formatPct(summary.budget_utilization)}
          tone={
            summary.budget_utilization >= 0.8
              ? 'warning'
              : summary.budget_utilization >= 0.5
                ? 'caution'
                : 'success'
          }
        />
        <Kpi
          icon={TrendingDown}
          label={t('usage.kpis.dropRate')}
          value={formatPct(summary.drop_rate)}
          tone={summary.drop_rate >= 0.3 ? 'warning' : 'default'}
        />
      </div>

      {showWarning && (
        <p className="flex items-start gap-1.5 rounded-md border border-amber-300/60 bg-amber-50/60 px-2.5 py-1.5 text-[11px] text-amber-800 dark:border-amber-700/60 dark:bg-amber-900/10 dark:text-amber-300">
          <AlertTriangle size={11} aria-hidden="true" className="mt-0.5 shrink-0" />
          <span>
            {t('usage.budgetWarning', {
              pct: formatPct(summary.budget_utilization),
            })}
          </span>
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// One KPI card
// ---------------------------------------------------------------------------

interface KpiProps {
  icon: React.ComponentType<{ size?: number; strokeWidth?: number }>;
  label: string;
  value: string;
  tone: 'default' | 'primary' | 'success' | 'caution' | 'warning';
}

function Kpi({ icon: Icon, label, value, tone }: KpiProps) {
  const toneClasses = {
    default: 'text-foreground',
    primary: 'text-primary',
    success: 'text-emerald-600 dark:text-emerald-400',
    caution: 'text-amber-600 dark:text-amber-400',
    warning: 'text-red-600 dark:text-red-400',
  } as const;

  return (
    <div className="rounded-lg border border-border bg-card p-2.5">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
        <Icon size={11} aria-hidden="true" />
        {label}
      </div>
      <p
        className={cn(
          'mt-1 text-lg font-semibold tabular-nums',
          toneClasses[tone],
        )}
      >
        {value}
      </p>
    </div>
  );
}
