// frontend/src/features/resource-monitor/components/ProcessTreeTable.tsx
'use client';

/**
 * Interactive process tree table with animated row entry, orphan highlighting,
 * and a graceful confirmation flow for termination.
 */

import { useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { AlertTriangle, CheckCircle2, Loader2, Skull, X } from 'lucide-react';
import type { ProcessInstance, TerminateOrphanResponse } from '../types';
import { formatRelativeTime, truncate } from '../utils/formatters';
import { Skeleton } from './_motion';
import { cn } from '@/lib/utils';

interface ProcessTreeTableProps {
  processes: ProcessInstance[];
  orphans: ProcessInstance[];
  isLoading?: boolean;
  terminatingPid?: number | null;
  onTerminate: (pid: number) => Promise<TerminateOrphanResponse>;
}

export function ProcessTreeTable({
  processes,
  orphans,
  isLoading,
  terminatingPid,
  onTerminate,
}: ProcessTreeTableProps) {
  const [confirmPid, setConfirmPid] = useState<number | null>(null);
  const [feedback, setFeedback] = useState<{ type: 'success' | 'error'; message: string } | null>(null);

  const handleTerminate = async (pid: number) => {
    try {
      const result = await onTerminate(pid);
      setFeedback({ type: 'success', message: result.message });
    } catch (err) {
      setFeedback({ type: 'error', message: (err as Error).message });
    } finally {
      setConfirmPid(null);
      setTimeout(() => setFeedback(null), 4000);
    }
  };

  if (isLoading && processes.length === 0) {
    return (
      <div className="rounded-2xl border border-slate-200/80 dark:border-slate-800/80 bg-white/70 dark:bg-slate-900/60 backdrop-blur-sm p-6">
        <Skeleton className="h-4 w-40" />
        <div className="mt-4 space-y-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      </div>
    );
  }

  if (processes.length === 0) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="rounded-2xl border border-dashed border-slate-300 dark:border-slate-700 p-10 text-center"
      >
        <CheckCircle2 className="mx-auto h-8 w-8 text-emerald-500 mb-2" strokeWidth={1.8} />
        <p className="text-sm font-medium text-slate-700 dark:text-slate-300">All clear</p>
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
          No tracked subprocesses for this session.
        </p>
      </motion.div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="rounded-2xl border border-slate-200/80 dark:border-slate-800/80 bg-white/70 dark:bg-slate-900/60 backdrop-blur-sm overflow-hidden shadow-sm"
    >
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200/80 dark:border-slate-800/80 px-5 py-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            Process Tree
          </h3>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            {processes.length} process{processes.length === 1 ? '' : 'es'} tracked
            {orphans.length > 0 && (
              <span className="ml-2 inline-flex items-center gap-1 text-red-600 dark:text-red-400 font-medium">
                <AlertTriangle className="h-3 w-3" strokeWidth={2.4} />
                {orphans.length} orphan{orphans.length === 1 ? '' : 's'}
              </span>
            )}
          </p>
        </div>
        <AnimatePresence>
          {feedback && (
            <motion.div
              initial={{ opacity: 0, x: 8 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: 8 }}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium',
                feedback.type === 'success'
                  ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'
                  : 'bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300',
              )}
            >
              {feedback.type === 'success' ? (
                <CheckCircle2 className="h-3 w-3" strokeWidth={2.4} />
              ) : (
                <AlertTriangle className="h-3 w-3" strokeWidth={2.4} />
              )}
              {feedback.message}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50/70 dark:bg-slate-950/40 text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400">
            <tr>
              <th className="px-4 py-2.5 text-left font-semibold">PID / PPID</th>
              <th className="px-4 py-2.5 text-left font-semibold">Name</th>
              <th className="px-4 py-2.5 text-left font-semibold">Type</th>
              <th className="px-4 py-2.5 text-left font-semibold">Created</th>
              <th className="px-4 py-2.5 text-left font-semibold">Status</th>
              <th className="px-4 py-2.5 text-right font-semibold">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800/80">
            <AnimatePresence initial={false}>
              {processes.map((p) => {
                const isOrphan = p.is_orphan && p.terminated_at === null;
                const isTerminated = p.terminated_at !== null;
                return (
                  <motion.tr
                    key={p.process_instance_id}
                    layout
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, x: -20 }}
                    transition={{ duration: 0.22 }}
                    className={cn(
                      'transition-colors',
                      isOrphan
                        ? 'bg-red-50/60 dark:bg-red-950/20'
                        : isTerminated
                          ? 'opacity-60'
                          : 'hover:bg-slate-50/60 dark:hover:bg-slate-900/60',
                    )}
                  >
                    <td className="px-4 py-2.5 font-mono text-xs text-slate-700 dark:text-slate-300">
                      {p.pid}
                      <span className="text-slate-400"> / {p.ppid}</span>
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="font-medium text-slate-900 dark:text-slate-100">
                        {p.name}
                      </div>
                      {p.command_line && (
                        <div className="text-xs text-slate-500 dark:text-slate-400">
                          {truncate(p.command_line, 70)}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <span className="inline-flex items-center rounded-md bg-slate-100 dark:bg-slate-800 px-2 py-0.5 text-[11px] font-medium text-slate-700 dark:text-slate-300">
                        {p.process_type}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-xs text-slate-500 dark:text-slate-400">
                      {formatRelativeTime(p.created_at)}
                    </td>
                    <td className="px-4 py-2.5">
                      {isTerminated ? (
                        <span className="text-xs text-slate-400 dark:text-slate-500">
                          Terminated
                          {typeof p.exit_code === 'number' && ` (exit ${p.exit_code})`}
                        </span>
                      ) : isOrphan ? (
                        <span className="inline-flex items-center gap-1 rounded-full bg-red-100 dark:bg-red-900/40 px-2 py-0.5 text-[11px] font-semibold text-red-700 dark:text-red-300">
                          <AlertTriangle className="h-3 w-3" strokeWidth={2.4} />
                          Orphan
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 dark:bg-emerald-900/40 px-2 py-0.5 text-[11px] font-semibold text-emerald-700 dark:text-emerald-300">
                          <span className="relative flex h-1.5 w-1.5">
                            <span className="absolute inset-0 animate-ping rounded-full bg-emerald-400 opacity-75" />
                            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" />
                          </span>
                          Active
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <AnimatePresence mode="wait" initial={false}>
                        {isOrphan &&
                          (confirmPid === p.pid ? (
                            <motion.div
                              key="confirm"
                              initial={{ opacity: 0, scale: 0.9 }}
                              animate={{ opacity: 1, scale: 1 }}
                              exit={{ opacity: 0, scale: 0.9 }}
                              className="inline-flex items-center gap-1"
                            >
                              <button
                                type="button"
                                onClick={() => handleTerminate(p.pid)}
                                disabled={terminatingPid === p.pid}
                                className="inline-flex items-center gap-1 rounded-md bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50 transition-colors"
                              >
                                {terminatingPid === p.pid ? (
                                  <Loader2 className="h-3 w-3 animate-spin" strokeWidth={2.4} />
                                ) : (
                                  <Skull className="h-3 w-3" strokeWidth={2.4} />
                                )}
                                Kill
                              </button>
                              <button
                                type="button"
                                onClick={() => setConfirmPid(null)}
                                className="inline-flex items-center rounded-md border border-slate-300 dark:border-slate-700 px-2 py-1 text-xs text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors"
                              >
                                <X className="h-3 w-3" strokeWidth={2.4} />
                              </button>
                            </motion.div>
                          ) : (
                            <motion.button
                              key="trigger"
                              initial={{ opacity: 0 }}
                              animate={{ opacity: 1 }}
                              exit={{ opacity: 0 }}
                              type="button"
                              onClick={() => setConfirmPid(p.pid)}
                              className="inline-flex items-center gap-1 rounded-md border border-red-300 dark:border-red-800 px-2 py-1 text-xs font-medium text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-950/40 transition-colors"
                            >
                              <Skull className="h-3 w-3" strokeWidth={2.4} />
                              Terminate
                            </motion.button>
                          ))}
                      </AnimatePresence>
                    </td>
                  </motion.tr>
                );
              })}
            </AnimatePresence>
          </tbody>
        </table>
      </div>
    </motion.div>
  );
}
