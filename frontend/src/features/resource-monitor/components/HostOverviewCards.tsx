// frontend/src/features/resource-monitor/components/HostOverviewCards.tsx
'use client';

/**
 * Live host + PELDRUN resource summary cards.
 * Each card pairs a radial gauge with an animated numeric readout and
 * a Lucide icon — designed to read at a glance while feeling premium.
 */

import { motion } from 'motion/react';
import {
  Cpu,
  HardDrive,
  Info,
  MemoryStick,
  Server,
  TrendingUp,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { CurrentResourceSnapshot } from '../types';
import { formatBytes, formatPercent } from '../utils/formatters';
import {
  AnimatedNumber,
  CircularGauge,
  Skeleton,
  fadeUp,
  staggerContainer,
} from './_motion';

interface HostOverviewCardsProps {
  snapshot: CurrentResourceSnapshot | null;
  isLoading?: boolean;
}

interface MetricCardProps {
  label: string;
  icon: LucideIcon;
  primary: { value: number; format: (v: number) => string; raw: string };
  secondary: string;
  percent: number | null;
  tone: 'emerald' | 'sky' | 'violet' | 'indigo';
  footer?: string;
  tooltip?: string;
}

const TONE_STYLES = {
  emerald: {
    color: '#10b981',
    iconBg: 'bg-emerald-50 dark:bg-emerald-950/40',
    iconColor: 'text-emerald-600 dark:text-emerald-400',
    chip: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  },
  sky: {
    color: '#0ea5e9',
    iconBg: 'bg-sky-50 dark:bg-sky-950/40',
    iconColor: 'text-sky-600 dark:text-sky-400',
    chip: 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300',
  },
  violet: {
    color: '#8b5cf6',
    iconBg: 'bg-violet-50 dark:bg-violet-950/40',
    iconColor: 'text-violet-600 dark:text-violet-400',
    chip: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  },
  indigo: {
    color: '#6366f1',
    iconBg: 'bg-indigo-50 dark:bg-indigo-950/40',
    iconColor: 'text-indigo-600 dark:text-indigo-400',
    chip: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300',
  },
} as const;

function MetricCard({
  label,
  icon: Icon,
  primary,
  secondary,
  percent,
  tone,
  footer,
  tooltip,
}: MetricCardProps) {
  const styles = TONE_STYLES[tone];

  return (
    <motion.div
      variants={fadeUp}
      whileHover={{ y: -2, transition: { duration: 0.18 } }}
      className="group relative rounded-2xl border border-slate-200/80 dark:border-slate-800/80 bg-white/70 dark:bg-slate-900/60 backdrop-blur-sm p-5 shadow-sm hover:shadow-md transition-shadow"
    >
      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2.5" title={tooltip}>
          <div className={`flex h-9 w-9 items-center justify-center rounded-lg ${styles.iconBg}`}>
            <Icon className={`h-4 w-4 ${styles.iconColor}`} strokeWidth={2.2} />
          </div>
          <div className="flex items-center gap-1 cursor-help">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
              {label}
            </span>
            {tooltip && (
              <Info className="h-3 w-3 text-slate-400" strokeWidth={2.4} />
            )}
          </div>
        </div>
        {percent != null && (
          <span
            className={`rounded-full px-2 py-0.5 text-[10px] font-semibold tabular-nums ${styles.chip}`}
          >
            {formatPercent(percent)}
          </span>
        )}
      </div>

      {/* Body */}
      <div className="mt-4 flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50 tabular-nums">
            <AnimatedNumber value={primary.value} format={primary.format} />
          </div>
          <div className="mt-1 truncate text-xs text-slate-500 dark:text-slate-400">
            {secondary}
          </div>
        </div>
        {percent != null && (
          <CircularGauge
            value={percent}
            color={styles.color}
            size={56}
            strokeWidth={5}
          >
            <TrendingUp className="h-4 w-4 text-slate-400 dark:text-slate-500" strokeWidth={2.2} />
          </CircularGauge>
        )}
      </div>

      {footer && (
        <div className="mt-3 border-t border-slate-100 dark:border-slate-800/80 pt-2.5 text-[11px] text-slate-400 dark:text-slate-500 truncate">
          {footer}
        </div>
      )}
    </motion.div>
  );
}

export function HostOverviewCards({ snapshot, isLoading }: HostOverviewCardsProps) {
  if (isLoading && !snapshot) {
    return (
      <motion.div
        initial="hidden"
        animate="visible"
        variants={staggerContainer}
        className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4"
      >
        {[0, 1, 2, 3].map((i) => (
          <motion.div key={i} variants={fadeUp}>
            <div className="rounded-2xl border border-slate-200/80 dark:border-slate-800/80 bg-white/70 dark:bg-slate-900/60 p-5">
              <div className="flex items-center gap-2.5">
                <Skeleton className="h-9 w-9 rounded-lg" />
                <Skeleton className="h-3 w-20" />
              </div>
              <div className="mt-4 space-y-2">
                <Skeleton className="h-7 w-32" />
                <Skeleton className="h-3 w-24" />
              </div>
            </div>
          </motion.div>
        ))}
      </motion.div>
    );
  }

  if (!snapshot) return null;

  const { host, peldrun } = snapshot;
  const ramPercent = host.ram_percent;
  const cpuPercent = host.cpu_percent;
  const diskPercent =
    host.disk_total_bytes && host.disk_used_bytes
      ? (host.disk_used_bytes / host.disk_total_bytes) * 100
      : null;
  const peldrunPercent =
    host.ram_total_bytes > 0
      ? (peldrun.total_rss_bytes / host.ram_total_bytes) * 100
      : null;

  return (
    <motion.div
      initial="hidden"
      animate="visible"
      variants={staggerContainer}
      className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4"
    >
      <MetricCard
        label="Host RAM"
        icon={MemoryStick}
        tone="violet"
        percent={ramPercent}
        tooltip="Physical memory currently in use by the entire system."
        primary={{
          value: host.ram_used_bytes,
          format: (v) => formatBytes(v),
          raw: formatBytes(host.ram_used_bytes),
        }}
        secondary={`${formatBytes(host.ram_available_bytes)} available · ${formatBytes(host.ram_total_bytes)} total`}
      />
      <MetricCard
        label="Host CPU"
        icon={Cpu}
        tone="sky"
        percent={cpuPercent}
        tooltip="Total CPU utilization across all cores."
        primary={{
          value: cpuPercent,
          format: (v) => formatPercent(v),
          raw: formatPercent(cpuPercent),
        }}
        secondary={`${snapshot.active_runs_count} active run${snapshot.active_runs_count === 1 ? '' : 's'}`}
      />
      <MetricCard
        label="PELDRUN RSS"
        icon={Server}
        tone="emerald"
        percent={peldrunPercent}
        tooltip="Resident Set Size — physical memory currently allocated to the PELDRUN process and its children."
        primary={{
          value: peldrun.total_rss_bytes,
          format: (v) => formatBytes(v),
          raw: formatBytes(peldrun.total_rss_bytes),
        }}
        secondary={`Main ${formatBytes(peldrun.main_rss_bytes)} · Children ${formatBytes(peldrun.children_rss_bytes)}`}
        footer={`PID ${peldrun.pid} · ${peldrun.children_count} child process${peldrun.children_count === 1 ? '' : 'es'}`}
      />
      <MetricCard
        label="Workspace Disk"
        icon={HardDrive}
        tone="indigo"
        percent={diskPercent}
        tooltip="Disk space used by the current workspace directory."
        primary={{
          value: host.disk_used_bytes ?? 0,
          format: (v) => formatBytes(v),
          raw: formatBytes(host.disk_used_bytes ?? 0),
        }}
        secondary={
          host.disk_total_bytes
            ? `${formatBytes(host.disk_total_bytes)} total · ${diskPercent != null ? formatPercent(diskPercent) : '—'} used`
            : 'Disk telemetry unavailable'
        }
      />
    </motion.div>
  );
}
