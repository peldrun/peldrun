// frontend/src/features/resource-monitor/components/TimeseriesCharts.tsx
'use client';

/**
 * Historical & live timeseries charts using Recharts AreaChart.
 * Gradient fills, custom tooltip, and smooth range switch animation.
 */

import { motion } from 'motion/react';
import { Activity, HardDrive } from 'lucide-react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { TimeseriesPoint, TimeseriesRange } from '../types';
import { formatAxisTimestamp, formatBytes, formatPercent } from '../utils/formatters';
import { Skeleton } from './_motion';
import { cn } from '@/lib/utils';

interface TimeseriesChartsProps {
  points: TimeseriesPoint[];
  range: TimeseriesRange;
  onRangeChange: (range: TimeseriesRange) => void;
  isLoading?: boolean;
}

interface ChartRow {
  t: number;
  hostCpu: number;
  hostRam: number;
  peldrunRam: number;
}

const RANGES: ReadonlyArray<{ key: TimeseriesRange; label: string }> = [
  { key: '1h', label: '1H' },
  { key: '6h', label: '6H' },
  { key: '24h', label: '24H' },
  { key: '7d', label: '7D' },
  { key: '30d', label: '30D' },
];

const number = (v: unknown): number => {
  if (typeof v === 'number') return v;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

// ---------------------------------------------------------------------------
// Custom Tooltip
// ---------------------------------------------------------------------------

interface TooltipPayloadItem {
  name: string;
  value: number;
  color: string;
  dataKey: string;
}

interface CustomTooltipProps {
  active?: boolean;
  payload?: TooltipPayloadItem[];
  label?: number;
  valueFormatter: (key: string, value: number) => string;
}

function CustomTooltip({ active, payload, label, valueFormatter }: CustomTooltipProps) {
  if (!active || !payload?.length) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: -4, scale: 0.97 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ duration: 0.14 }}
      className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white/95 dark:bg-slate-900/95 backdrop-blur-md px-3.5 py-2.5 shadow-xl"
    >
      <p className="mb-1.5 text-[11px] font-medium text-slate-500 dark:text-slate-400">
        {new Date(number(label)).toLocaleString()}
      </p>
      <div className="space-y-1">
        {payload.map((item) => (
          <div key={item.dataKey} className="flex items-center gap-2 text-xs">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ backgroundColor: item.color }}
            />
            <span className="text-slate-600 dark:text-slate-300">{item.name}</span>
            <span className="ml-auto pl-3 font-semibold tabular-nums text-slate-900 dark:text-slate-50">
              {valueFormatter(item.dataKey, item.value)}
            </span>
          </div>
        ))}
      </div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Chart Card
// ---------------------------------------------------------------------------

interface ChartCardProps {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}

function ChartCard({ title, icon, children }: ChartCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
      className="rounded-2xl border border-slate-200/80 dark:border-slate-800/80 bg-white/70 dark:bg-slate-900/60 backdrop-blur-sm p-5 shadow-sm"
    >
      <div className="flex items-center gap-2 mb-3">
        <div className="text-slate-400 dark:text-slate-500">{icon}</div>
        <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
          {title}
        </p>
      </div>
      <div className="h-64">{children}</div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

export function TimeseriesCharts({
  points,
  range,
  onRangeChange,
  isLoading,
}: TimeseriesChartsProps) {
  const hasData = Array.isArray(points) && points.length > 0;

  const chartData: ChartRow[] = hasData
    ? points.map((p) => ({
        t: p.timestamp_epoch_ms,
        hostCpu: p.host_cpu_percent,
        hostRam: p.host_ram_used_bytes,
        peldrunRam: p.peldrun_total_rss_bytes,
      }))
    : [];

  const axisMode = range === '7d' || range === '30d' ? 'long' : 'short';

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-sky-600 dark:text-sky-400" strokeWidth={2.2} />
          <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            Resource Timeseries
          </h3>
        </div>
        <div className="inline-flex rounded-lg border border-slate-200 dark:border-slate-700 bg-white/60 dark:bg-slate-900/60 p-0.5">
          {RANGES.map((r) => {
            const active = range === r.key;
            return (
              <button
                key={r.key}
                type="button"
                onClick={() => onRangeChange(r.key)}
                className="relative px-3 py-1 text-xs font-medium rounded-md transition-colors"
              >
                {active && (
                  <motion.span
                    layoutId="range-pill"
                    className="absolute inset-0 rounded-md bg-sky-600 shadow-sm"
                    transition={{ type: 'spring', stiffness: 380, damping: 30 }}
                  />
                )}
                <span
                  className={cn(
                    'relative z-10',
                    active
                      ? 'text-white'
                      : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100',
                  )}
                >
                  {r.label}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Loading */}
      {isLoading && !hasData && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          {[0, 1].map((i) => (
            <div
              key={i}
              className="rounded-2xl border border-slate-200/80 dark:border-slate-800/80 bg-white/70 dark:bg-slate-900/60 p-5"
            >
              <Skeleton className="h-3 w-32 mb-4" />
              <Skeleton className="h-56 w-full" />
            </div>
          ))}
        </div>
      )}

      {/* Empty */}
      {!isLoading && !hasData && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="flex h-64 items-center justify-center rounded-2xl border border-dashed border-slate-300 dark:border-slate-700 text-sm text-slate-500 dark:text-slate-400"
        >
          No timeseries data recorded for this range yet.
        </motion.div>
      )}

      {/* Charts */}
      {hasData && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <ChartCard title="CPU Usage (%)" icon={<Activity className="h-4 w-4" strokeWidth={2.2} />}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
                <defs>
                  <linearGradient id="cpuFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#0ea5e9" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#0ea5e9" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.15)" vertical={false} />
                <XAxis
                  dataKey="t"
                  tickFormatter={(v) => formatAxisTimestamp(number(v), axisMode)}
                  tick={{ fontSize: 11, fill: 'rgb(148, 163, 184)' }}
                  stroke="rgba(148, 163, 184, 0.4)"
                  minTickGap={40}
                />
                <YAxis
                  domain={[0, 100]}
                  tickFormatter={(v) => formatPercent(number(v), 0)}
                  tick={{ fontSize: 11, fill: 'rgb(148, 163, 184)' }}
                  stroke="rgba(148, 163, 184, 0.4)"
                  width={48}
                />
                <Tooltip
                  content={
                    <CustomTooltip
                      valueFormatter={(key, value) => formatPercent(value)}
                    />
                  }
                  cursor={{ stroke: 'rgba(14, 165, 233, 0.35)', strokeWidth: 1 }}
                />
                <Legend
                  wrapperStyle={{ fontSize: 12, paddingTop: 8 }}
                  iconType="circle"
                  iconSize={8}
                />
                <Area
                  type="monotone"
                  dataKey="hostCpu"
                  name="Host CPU"
                  stroke="#0ea5e9"
                  strokeWidth={2}
                  fill="url(#cpuFill)"
                  isAnimationActive
                  animationDuration={600}
                  dot={false}
                  activeDot={{ r: 4, fill: '#0ea5e9', stroke: '#fff', strokeWidth: 2 }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="Memory (Host vs PELDRUN)" icon={<HardDrive className="h-4 w-4" strokeWidth={2.2} />}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
                <defs>
                  <linearGradient id="hostRamFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#8b5cf6" stopOpacity={0.28} />
                    <stop offset="100%" stopColor="#8b5cf6" stopOpacity={0.02} />
                  </linearGradient>
                  <linearGradient id="peldrunRamFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#f59e0b" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.15)" vertical={false} />
                <XAxis
                  dataKey="t"
                  tickFormatter={(v) => formatAxisTimestamp(number(v), axisMode)}
                  tick={{ fontSize: 11, fill: 'rgb(148, 163, 184)' }}
                  stroke="rgba(148, 163, 184, 0.4)"
                  minTickGap={40}
                />
                <YAxis
                  tickFormatter={(v) => formatBytes(number(v), 0)}
                  tick={{ fontSize: 11, fill: 'rgb(148, 163, 184)' }}
                  stroke="rgba(148, 163, 184, 0.4)"
                  width={56}
                />
                <Tooltip
                  content={
                    <CustomTooltip
                      valueFormatter={(key, value) => formatBytes(value)}
                    />
                  }
                  cursor={{ stroke: 'rgba(139, 92, 246, 0.35)', strokeWidth: 1 }}
                />
                <Legend
                  wrapperStyle={{ fontSize: 12, paddingTop: 8 }}
                  iconType="circle"
                  iconSize={8}
                />
                <Area
                  type="monotone"
                  dataKey="hostRam"
                  name="Host RAM"
                  stroke="#8b5cf6"
                  strokeWidth={2}
                  fill="url(#hostRamFill)"
                  isAnimationActive
                  animationDuration={600}
                  dot={false}
                  activeDot={{ r: 4, fill: '#8b5cf6', stroke: '#fff', strokeWidth: 2 }}
                />
                <Area
                  type="monotone"
                  dataKey="peldrunRam"
                  name="PELDRUN Tree"
                  stroke="#f59e0b"
                  strokeWidth={2}
                  fill="url(#peldrunRamFill)"
                  isAnimationActive
                  animationDuration={600}
                  dot={false}
                  activeDot={{ r: 4, fill: '#f59e0b', stroke: '#fff', strokeWidth: 2 }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>
        </div>
      )}
    </div>
  );
}
