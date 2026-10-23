// frontend/src/features/resource-monitor/components/RunConsumptionTable.tsx
'use client';

/**
 * Paginated execution run history with animated rows and confidence chips.
 */

import { useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import {
  AlertOctagon,
  Ban,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  Loader2,
  ShieldCheck,
  ShieldHalf,
  ShieldQuestion,
  XCircle,
} from 'lucide-react';
import type {
  ExecutionRun,
  MeasurementConfidence,
  RunStatus,
} from '../types';
import { formatBytes, formatDuration, formatRelativeTime } from '../utils/formatters';
import { Skeleton } from './_motion';
import { RunDetailDrawer } from './RunDetailDrawer';
import { cn } from '@/lib/utils';

interface RunConsumptionTableProps {
  runs: ExecutionRun[];
  isLoading?: boolean;
  pageSize?: number;
}

const STATUS_META: Record<
  RunStatus,
  { label: string; icon: typeof CheckCircle2; className: string }
> = {
  PENDING: {
    label: 'Pending',
    icon: Clock,
    className: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  },
  RUNNING: {
    label: 'Running',
    icon: Loader2,
    className: 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300',
  },
  COMPLETED: {
    label: 'Completed',
    icon: CheckCircle2,
    className: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  },
  FAILED: {
    label: 'Failed',
    icon: XCircle,
    className: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  },
  CANCELLED: {
    label: 'Cancelled',
    icon: Ban,
    className: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  },
  UNEXPECTED_TERMINATION: {
    label: 'Terminated',
    icon: AlertOctagon,
    className: 'bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-900/40 dark:text-fuchsia-300',
  },
};

const CONFIDENCE_META: Record<
  MeasurementConfidence,
  { label: string; icon: typeof ShieldCheck; className: string }
> = {
  DIRECT: {
    label: 'Direct',
    icon: ShieldCheck,
    className: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  },
  PROPORTIONAL: {
    label: 'Proportional',
    icon: ShieldHalf,
    className: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  },
  ESTIMATED: {
    label: 'Estimated',
    icon: ShieldQuestion,
    className: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  },
};

export function RunConsumptionTable({
  runs,
  isLoading,
  pageSize = 15,
}: RunConsumptionTableProps) {
  const [page, setPage] = useState(0);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  const totalPages = Math.max(1, Math.ceil(runs.length / pageSize));
  const pageRuns = runs.slice(page * pageSize, (page + 1) * pageSize);

  if (isLoading && runs.length === 0) {
    return (
      <div className="rounded-2xl border border-slate-200/80 dark:border-slate-800/80 bg-white/70 dark:bg-slate-900/60 backdrop-blur-sm p-6">
        <Skeleton className="h-4 w-32" />
        <div className="mt-4 space-y-2">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="rounded-2xl border border-slate-200/80 dark:border-slate-800/80 bg-white/70 dark:bg-slate-900/60 backdrop-blur-sm overflow-hidden shadow-sm"
    >
      <div className="flex items-center justify-between border-b border-slate-200/80 dark:border-slate-800/80 px-5 py-3">
        <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
          Execution Runs
        </h3>
        <p className="text-xs text-slate-500 dark:text-slate-400">
          {runs.length} run{runs.length === 1 ? '' : 's'} recorded
        </p>
      </div>

      {runs.length === 0 ? (
        <div className="p-10 text-center">
          <Clock className="mx-auto h-8 w-8 text-slate-300 dark:text-slate-600 mb-2" strokeWidth={1.6} />
          <p className="text-sm text-slate-500 dark:text-slate-400">No execution runs recorded yet.</p>
        </div>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-50/70 dark:bg-slate-950/40 text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400">
                <tr>
                  <th className="px-4 py-2.5 text-left font-semibold">Run ID</th>
                  <th className="px-4 py-2.5 text-left font-semibold">Agent</th>
                  <th className="px-4 py-2.5 text-left font-semibold">Status</th>
                  <th className="px-4 py-2.5 text-right font-semibold">Duration</th>
                  <th className="px-4 py-2.5 text-right font-semibold">Peak RSS</th>
                  <th className="px-4 py-2.5 text-right font-semibold">CPU Time</th>
                  <th className="px-4 py-2.5 text-left font-semibold">Confidence</th>
                  <th className="px-4 py-2.5 text-left font-semibold">Started</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800/80">
                <AnimatePresence initial={false}>
                  {pageRuns.map((run) => {
                    const status = STATUS_META[run.status];
                    const confidence = CONFIDENCE_META[run.measurement_confidence];
                    const StatusIcon = status.icon;
                    const ConfIcon = confidence.icon;
                    const isRunning = run.status === 'RUNNING';
                    return (
                      <motion.tr
                        key={run.run_id}
                        layout
                        initial={{ opacity: 0, y: 6 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0 }}
                        transition={{ duration: 0.2 }}
                        onClick={() => setSelectedRunId(run.run_id)}
                        className="cursor-pointer hover:bg-slate-50/60 dark:hover:bg-slate-900/60 transition-colors"
                      >
                        <td className="px-4 py-2.5 font-mono text-xs text-slate-700 dark:text-slate-300">
                          {run.run_id.slice(0, 8)}…
                        </td>
                        <td className="px-4 py-2.5 text-slate-700 dark:text-slate-300">
                          {run.agent_id ?? '—'}
                        </td>
                        <td className="px-4 py-2.5">
                          <span
                            className={cn(
                              'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium',
                              status.className,
                            )}
                          >
                            <StatusIcon
                              className={cn('h-3 w-3', isRunning && 'animate-spin')}
                              strokeWidth={2.4}
                            />
                            {status.label}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 text-right tabular-nums text-slate-700 dark:text-slate-300">
                          {formatDuration(run.duration_ms)}
                        </td>
                        <td className="px-4 py-2.5 text-right tabular-nums text-slate-700 dark:text-slate-300">
                          {formatBytes(run.peak_rss_bytes)}
                        </td>
                        <td className="px-4 py-2.5 text-right tabular-nums text-slate-700 dark:text-slate-300">
                          {formatDuration(run.cumulative_cpu_time_ms)}
                        </td>
                        <td className="px-4 py-2.5">
                          <span
                            className={cn(
                              'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium',
                              confidence.className,
                            )}
                          >
                            <ConfIcon className="h-3 w-3" strokeWidth={2.4} />
                            {confidence.label}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 text-xs text-slate-500 dark:text-slate-400">
                          {formatRelativeTime(run.started_at)}
                        </td>
                      </motion.tr>
                    );
                  })}
                </AnimatePresence>
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-slate-200/80 dark:border-slate-800/80 px-5 py-2.5">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="inline-flex items-center gap-1 rounded-md border border-slate-300 dark:border-slate-700 px-2 py-1 text-xs text-slate-700 dark:text-slate-300 disabled:opacity-40 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors"
              >
                <ChevronLeft className="h-3 w-3" strokeWidth={2.4} />
                Previous
              </button>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                Page {page + 1} of {totalPages}
              </span>
              <button
                type="button"
                onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                disabled={page >= totalPages - 1}
                className="inline-flex items-center gap-1 rounded-md border border-slate-300 dark:border-slate-700 px-2 py-1 text-xs text-slate-700 dark:text-slate-300 disabled:opacity-40 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors"
              >
                Next
                <ChevronRight className="h-3 w-3" strokeWidth={2.4} />
              </button>
            </div>
          )}
        </>
      )}

      <RunDetailDrawer
        runId={selectedRunId}
        open={!!selectedRunId}
        onClose={() => setSelectedRunId(null)}
      />
    </motion.div>
  );
}
