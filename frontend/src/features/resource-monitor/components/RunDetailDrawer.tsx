// frontend/src/features/resource-monitor/components/RunDetailDrawer.tsx
'use client';

/**
 * Slide-in drawer displaying deep telemetry for a specific execution run.
 * Features three tabs: Timeline, Tool Breakdown, and Memory over Time.
 */

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion } from 'motion/react';
import {
  Activity,
  BarChart2,
  Clock,
  Cpu,
  Loader2,
  X,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  ShieldAlert,
} from 'lucide-react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  BarChart,
  Bar,
  Cell,
} from 'recharts';

import { resourceMonitorClient } from '../api/resource-monitor-client';
import type { ExecutionRunTimeline, ExecutionSpan, ToolConsumptionSummary } from '../types';
import { formatBytes, formatDuration, formatPercent, formatAxisTimestamp } from '../utils/formatters';
import { cn } from '@/lib/utils';
import { Skeleton } from './_motion';

interface RunDetailDrawerProps {
  runId: string | null;
  open: boolean;
  onClose: () => void;
}

type TabKey = 'timeline' | 'tools' | 'memory';

const TABS: { key: TabKey; label: string; icon: any }[] = [
  { key: 'timeline', label: 'Timeline', icon: Clock },
  { key: 'tools', label: 'Tools', icon: BarChart2 },
  { key: 'memory', label: 'Memory', icon: Activity },
];

export function RunDetailDrawer({ runId, open, onClose }: RunDetailDrawerProps) {
  const [activeTab, setActiveTab] = useState<TabKey>('timeline');
  const [data, setData] = useState<ExecutionRunTimeline | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [mounted, setMounted] = useState(false);

  // Ensure portal only renders on the client after hydration
  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!open || !runId) return;

    let mounted = true;
    setIsLoading(true);
    setError(null);

    resourceMonitorClient
      .getRunTimeline(runId)
      .then((res) => {
        if (mounted) {
          setData(res);
          setIsLoading(false);
        }
      })
      .catch((err) => {
        if (mounted) {
          setError(err);
          setIsLoading(false);
        }
      });

    return () => {
      mounted = false;
    };
  }, [open, runId]);

  // Reset tab when closed
  useEffect(() => {
    if (!open) {
      setTimeout(() => setActiveTab('timeline'), 300);
    }
  }, [open]);

  if (!mounted) return null;

  return createPortal(
    <AnimatePresence>
      {open && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
            className="fixed inset-0 z-40 bg-slate-950/40 backdrop-blur-sm"
          />

          {/* Drawer */}
          <motion.div
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 25, stiffness: 200 }}
            className="fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col bg-white dark:bg-slate-900 shadow-2xl border-l border-slate-200 dark:border-slate-800"
          >
            {/* Header */}
            <div className="flex items-center justify-between border-b border-slate-200 dark:border-slate-800 px-6 py-4">
              <div>
                <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100 flex items-center gap-2">
                  Run Details
                  {data && (
                    <span className="text-xs font-mono font-normal text-slate-500 dark:text-slate-400 bg-slate-100 dark:bg-slate-800 px-2 py-0.5 rounded-md">
                      {data.run.run_id.slice(0, 8)}...
                    </span>
                  )}
                </h2>
                {data && (
                  <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                    Status: <span className="font-medium">{data.run.status}</span> · Duration:{' '}
                    <span className="font-medium">{formatDuration(data.run.duration_ms)}</span>
                  </p>
                )}
              </div>
              <button
                onClick={onClose}
                className="rounded-full p-2 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
              >
                <X className="h-5 w-5 text-slate-500" />
              </button>
            </div>

            {/* Tabs */}
            <div className="flex border-b border-slate-200 dark:border-slate-800 px-4">
              {TABS.map((tab) => {
                const Icon = tab.icon;
                const active = activeTab === tab.key;
                return (
                  <button
                    key={tab.key}
                    onClick={() => setActiveTab(tab.key)}
                    className={cn(
                      'relative flex items-center gap-2 px-4 py-3 text-sm font-medium transition-colors',
                      active
                        ? 'text-sky-600 dark:text-sky-400'
                        : 'text-slate-500 hover:text-slate-800 dark:hover:text-slate-200'
                    )}
                  >
                    <Icon className="h-4 w-4" />
                    {tab.label}
                    {active && (
                      <motion.div
                        layoutId="drawer-tab"
                        className="absolute bottom-0 left-0 right-0 h-0.5 bg-sky-600 dark:bg-sky-400"
                      />
                    )}
                  </button>
                );
              })}
            </div>

            {/* Content Area */}
            <div className="flex-1 overflow-y-auto p-6">
              {isLoading && (
                <div className="space-y-4">
                  <Skeleton className="h-8 w-1/3" />
                  <Skeleton className="h-32 w-full" />
                  <Skeleton className="h-32 w-full" />
                </div>
              )}

              {error && (
                <div className="flex items-center gap-2 rounded-lg bg-red-50 p-4 text-sm text-red-600 dark:bg-red-950/40 dark:text-red-400">
                  <AlertTriangle className="h-5 w-5 shrink-0" />
                  <p>Failed to load run details: {error.message}</p>
                </div>
              )}

              {!isLoading && !error && data && (
                <AnimatePresence mode="wait">
                  <motion.div
                    key={activeTab}
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -10 }}
                    transition={{ duration: 0.2 }}
                  >
                    {activeTab === 'timeline' && <TimelineView data={data} />}
                    {activeTab === 'tools' && <ToolsView spans={data.spans} />}
                    {activeTab === 'memory' && <MemoryView data={data} />}
                  </motion.div>
                </AnimatePresence>
              )}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>,
    document.body
  );
}

// ---------------------------------------------------------------------------
// Timeline View
// ---------------------------------------------------------------------------

function TimelineView({ data }: { data: ExecutionRunTimeline }) {
  if (!data.spans.length) {
    return <EmptyState message="No execution spans recorded for this run." />;
  }

  // Calculate total run duration for percentage sizing
  const startMs = new Date(data.run.started_at).getTime();
  const endMs = data.run.ended_at ? new Date(data.run.ended_at).getTime() : Date.now();
  const totalMs = Math.max(endMs - startMs, 1);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between text-xs text-slate-500 dark:text-slate-400 mb-2">
        <span>Start: {new Date(startMs).toLocaleTimeString()}</span>
        <span>End: {new Date(endMs).toLocaleTimeString()}</span>
      </div>

      <div className="relative pl-6 border-l-2 border-slate-200 dark:border-slate-800 space-y-6">
        {data.spans.map((span, i) => {
          const spanStartMs = new Date(span.started_at).getTime();
          const spanEndMs = span.ended_at ? new Date(span.ended_at).getTime() : spanStartMs + (span.duration_ms || 0);
          const duration = spanEndMs - spanStartMs;
          const leftPercent = Math.max(0, Math.min(100, ((spanStartMs - startMs) / totalMs) * 100));
          const widthPercent = Math.max(1, Math.min(100, (duration / totalMs) * 100));

          const isError = span.status === 'FAILED' || span.status === 'TIMED_OUT';
          const isSuccess = span.status === 'COMPLETED';

          return (
            <div key={span.span_id} className="relative">
              {/* Dot on the timeline */}
              <div className={cn(
                "absolute -left-[31px] top-1.5 h-4 w-4 rounded-full border-2 bg-white dark:bg-slate-900",
                isError ? "border-red-500" : isSuccess ? "border-emerald-500" : "border-sky-500"
              )} />

              <div className="flex items-center justify-between mb-1.5">
                <span className="font-semibold text-sm text-slate-900 dark:text-slate-100">
                  {span.target_name}
                </span>
                <span className="text-xs font-mono text-slate-500">
                  {formatDuration(span.duration_ms)}
                </span>
              </div>

              <div className="flex items-center gap-2 mb-2">
                <span className="text-[10px] font-medium uppercase tracking-wider text-slate-400 bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 rounded">
                  {span.span_type}
                </span>
                {span.rss_peak_bytes && (
                  <span className="text-[10px] text-slate-500 flex items-center gap-1">
                    <Cpu className="h-3 w-3" /> Peak: {formatBytes(span.rss_peak_bytes)}
                  </span>
                )}
              </div>

              {/* Relative position bar */}
              <div className="h-2 w-full bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden relative">
                <motion.div
                  initial={{ width: 0 }}
                  animate={{ width: `${widthPercent}%` }}
                  transition={{ duration: 0.5, delay: i * 0.05 }}
                  className={cn(
                    "absolute top-0 bottom-0 rounded-full",
                    isError ? "bg-red-500" : isSuccess ? "bg-emerald-500" : "bg-sky-500"
                  )}
                  style={{ left: `${leftPercent}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tools View
// ---------------------------------------------------------------------------

function ToolsView({ spans }: { spans: ExecutionSpan[] }) {
  const toolSpans = spans.filter((s) => s.span_type === 'TOOL_CALL');

  if (!toolSpans.length) {
    return <EmptyState message="No tool calls recorded for this run." />;
  }

  const grouped = toolSpans.reduce((acc, span) => {
    const name = span.target_name;
    if (!acc[name]) acc[name] = { calls: 0, totalMs: 0, peakRss: 0 };
    acc[name].calls += 1;
    acc[name].totalMs += span.duration_ms || 0;
    acc[name].peakRss = Math.max(acc[name].peakRss, span.rss_peak_bytes || 0);
    return acc;
  }, {} as Record<string, { calls: number; totalMs: number; peakRss: number }>);

  const chartData = Object.entries(grouped)
    .map(([name, stats]) => ({ name, ...stats }))
    .sort((a, b) => b.totalMs - a.totalMs)
    .slice(0, 10); // Top 10

  const COLORS = ['#0ea5e9', '#8b5cf6', '#f59e0b', '#10b981', '#ef4444', '#6366f1', '#ec4899'];

  return (
    <div className="space-y-8">
      <div>
        <h3 className="text-sm font-semibold mb-4 text-slate-900 dark:text-slate-100">Top Tools by Duration</h3>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData} layout="vertical" margin={{ top: 0, right: 20, left: 20, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="rgba(148,163,184,0.15)" />
              <XAxis type="number" tickFormatter={(v) => formatDuration(v)} tick={{ fontSize: 11 }} />
              <YAxis dataKey="name" type="category" width={100} tick={{ fontSize: 11 }} />
              <Tooltip
                formatter={(val) => {
                  const numericVal = typeof val === 'number' ? val : Number(val) || 0;
                  return [formatDuration(numericVal), 'Total Duration'];
                }}
                contentStyle={{ borderRadius: 8, fontSize: 12 }}
              />
              <Bar dataKey="totalMs" radius={[0, 4, 4, 0]}>
                {chartData.map((_, index) => (
                  <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div>
        <h3 className="text-sm font-semibold mb-3 text-slate-900 dark:text-slate-100">Detailed Breakdown</h3>
        <div className="rounded-lg border border-slate-200 dark:border-slate-800 overflow-hidden">
          <table className="w-full text-sm text-left">
            <thead className="bg-slate-50 dark:bg-slate-950/40 text-xs text-slate-500">
              <tr>
                <th className="px-4 py-2 font-medium">Tool</th>
                <th className="px-4 py-2 font-medium text-right">Calls</th>
                <th className="px-4 py-2 font-medium text-right">Total Time</th>
                <th className="px-4 py-2 font-medium text-right">Peak RSS</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {Object.entries(grouped).sort((a, b) => b[1].totalMs - a[1].totalMs).map(([name, stats]) => (
                <tr key={name} className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
                  <td className="px-4 py-2 font-medium text-slate-700 dark:text-slate-300">{name}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-slate-600 dark:text-slate-400">{stats.calls}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-slate-600 dark:text-slate-400">{formatDuration(stats.totalMs)}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-slate-600 dark:text-slate-400">{formatBytes(stats.peakRss)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Memory View
// ---------------------------------------------------------------------------

function MemoryView({ data }: { data: ExecutionRunTimeline }) {
  if (!data.timeseries.length) {
    return <EmptyState message="No memory telemetry recorded for this run." />;
  }

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">Memory & CPU Over Time</h3>
      <div className="h-72 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data.timeseries} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="colorRss" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#8b5cf6" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="colorCpu" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#0ea5e9" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#0ea5e9" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" vertical={false} />
            <XAxis
              dataKey="timestamp_epoch_ms"
              tickFormatter={(v) => formatAxisTimestamp(v, 'short')}
              tick={{ fontSize: 11 }}
              minTickGap={40}
            />
            <YAxis
              yAxisId="left"
              tickFormatter={(v) => formatBytes(v, 0)}
              tick={{ fontSize: 11 }}
              width={50}
            />
            <YAxis
              yAxisId="right"
              orientation="right"
              tickFormatter={(v) => formatPercent(v, 0)}
              tick={{ fontSize: 11 }}
              width={40}
            />
            <Tooltip
              labelFormatter={(v) => new Date(Number(v) || 0).toLocaleTimeString()}
              formatter={(val, name) => {
                const numericVal = typeof val === 'number' ? val : Number(val) || 0;
                const stringName = typeof name === 'string' ? name : String(name);
                if (stringName === 'RSS') return [formatBytes(numericVal), stringName];
                return [formatPercent(numericVal), stringName];
              }}
              contentStyle={{ borderRadius: 8, fontSize: 12 }}
            />
            <Area
              yAxisId="left"
              type="monotone"
              dataKey="peldrun_total_rss_bytes"
              name="RSS"
              stroke="#8b5cf6"
              strokeWidth={2}
              fillOpacity={1}
              fill="url(#colorRss)"
            />
            <Area
              yAxisId="right"
              type="monotone"
              dataKey="host_cpu_percent"
              name="CPU %"
              stroke="#0ea5e9"
              strokeWidth={2}
              fillOpacity={1}
              fill="url(#colorCpu)"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared Components
// ---------------------------------------------------------------------------

function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex h-40 flex-col items-center justify-center rounded-xl border border-dashed border-slate-300 dark:border-slate-700 text-sm text-slate-500 dark:text-slate-400">
      <ShieldAlert className="h-8 w-8 text-slate-300 dark:text-slate-600 mb-2" />
      {message}
    </div>
  );
}
