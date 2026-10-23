// frontend/src/app/resource-monitor/ResourceMonitorBadge.tsx
'use client';

/**
 * Top-bar status pill for the Resource Monitor.
 *
 * Behaviour:
 * - Healthy (no alerts): subtle neutral pill with pulse dot → conveys "live & OK".
 * - Warning: amber pill with count.
 * - Critical: red pill, pulsing, with count.
 * - Loading: neutral skeleton pill.
 *
 * The pill always links to /resource-monitor for full detail.
 */

import Link from 'next/link';
import { Activity, ShieldAlert } from 'lucide-react';
import { useResourceAlerts } from './useResourceAlerts';
import { cn } from '@/lib/utils';

export interface ResourceMonitorBadgeProps {
  href?: string;
  /** When true (default), render a muted indicator even when clean. */
  showWhenClean?: boolean;
  refreshIntervalMs?: number;
}

export function ResourceMonitorBadge({
  href = '/resource-monitor',
  showWhenClean = true,
  refreshIntervalMs,
}: ResourceMonitorBadgeProps) {
  const { criticalCount, warningCount, totalCount, isLoading, error } = useResourceAlerts({
    limit: 10,
    refreshIntervalMs,
  });

  if (!showWhenClean && totalCount === 0) return null;

  // 1. Loading State
  if (isLoading && totalCount === 0) {
    return (
      <div className="inline-flex h-7 w-20 items-center justify-center rounded-md border border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-800/50 animate-pulse">
        <span className="text-[10px] text-slate-400">Syncing...</span>
      </div>
    );
  }

  // 2. Error State (Monitor Offline)
  if (error) {
    return (
      <Link
        href={href}
        aria-label="Resource monitor offline"
        title="Resource monitor offline — click to troubleshoot"
        className="inline-flex h-7 items-center gap-1.5 rounded-md border border-slate-300 bg-slate-200 text-slate-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400 px-2 text-xs font-medium transition-all hover:bg-slate-300 dark:hover:bg-slate-700"
      >
        <ShieldAlert className="h-3 w-3 opacity-70" strokeWidth={2.4} />
        <span className="hidden sm:inline">Offline</span>
      </Link>
    );
  }

  // 3. Healthy State
  const hasCritical = criticalCount > 0;
  const hasWarning = warningCount > 0;
  const clean = totalCount === 0;

  const tone = hasCritical
    ? 'bg-red-500 text-white border-red-400 hover:bg-red-600'
    : hasWarning
      ? 'bg-amber-500 text-white border-amber-400 hover:bg-amber-600'
      : 'bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700 hover:bg-slate-200 dark:hover:bg-slate-700';

  const tooltip = hasCritical
    ? `${criticalCount} critical diagnostic alert${criticalCount === 1 ? '' : 's'} — click for details`
    : hasWarning
      ? `${warningCount} warning${warningCount === 1 ? '' : 's'} — click for details`
      : 'Resource monitor — all systems healthy';

  return (
    <Link
      href={href}
      aria-label={tooltip}
      title={tooltip}
      className={cn(
        'inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs font-medium transition-all',
        tone,
        hasCritical && 'animate-pulse',
      )}
    >
      {clean ? (
        <>
          {/* Live pulse dot */}
          <span className="relative flex h-1.5 w-1.5">
            <span className="absolute inset-0 animate-ping rounded-full bg-emerald-500 opacity-75" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" />
          </span>
          <Activity className="h-3 w-3" strokeWidth={2.4} />
          <span className="hidden sm:inline">Live</span>
        </>
      ) : (
        <>
          <ShieldAlert className="h-3 w-3" strokeWidth={2.4} />
          <span className="tabular-nums">{totalCount}</span>
        </>
      )}
    </Link>
  );
}
