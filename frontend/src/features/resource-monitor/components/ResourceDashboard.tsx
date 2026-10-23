// frontend/src/features/resource-monitor/components/ResourceDashboard.tsx
'use client';

/**
 * Parent layout composing all Resource Monitor sections.
 * Uses AnimatePresence for smooth tab transitions and a rotating refresh
 * icon that reflects loading state.
 */

import { useCallback, useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Loader2,
  RefreshCw,
  Settings as SettingsIcon,
} from 'lucide-react';
import { resourceMonitorClient } from '../api/resource-monitor-client';
import { useCurrentResources } from '../hooks/useCurrentResources';
import { useProcessList } from '../hooks/useProcessList';
import { useResourceHistory } from '../hooks/useResourceHistory';
import type {
  DiagnosticEvent,
  ExecutionRun,
  MonitorSettings,
  MonitorSettingsUpdate,
} from '../types';
import { DiagnosticsAlertBanner } from './DiagnosticsAlertBanner';
import { HostOverviewCards } from './HostOverviewCards';
import { ProcessTreeTable } from './ProcessTreeTable';
import { RetentionSettingsModal } from './RetentionSettingsModal';
import { RunConsumptionTable } from './RunConsumptionTable';
import { TimeseriesCharts } from './TimeseriesCharts';
import { tabTransition } from './_motion';
import { cn } from '@/lib/utils';

type TabKey = 'overview' | 'processes' | 'runs';

const TABS: { key: TabKey; label: string; icon: typeof Activity }[] = [
  { key: 'overview', label: 'Overview', icon: Activity },
  { key: 'processes', label: 'Processes', icon: Cpu },
  { key: 'runs', label: 'Runs', icon: CheckCircle2 },
];

export function ResourceDashboard() {
  const [activeTab, setActiveTab] = useState<TabKey>('overview');
  const [runs, setRuns] = useState<ExecutionRun[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [diagnostics, setDiagnostics] = useState<DiagnosticEvent[]>([]);
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(new Set());
  const [settings, setSettings] = useState<MonitorSettings | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const {
    snapshot,
    isLoading: currentLoading,
    error: currentError,
    refresh: refreshCurrent,
  } = useCurrentResources({ intervalMs: 2000 });

  const { points, range, setRange, isLoading: historyLoading } = useResourceHistory({
    range: '1h',
    refreshIntervalMs: 10_000,
  });

  const {
    processes,
    orphans,
    isLoading: processesLoading,
    terminatingPid,
    terminateOrphan,
    refresh: refreshProcesses,
  } = useProcessList({ refreshIntervalMs: 5000 });

  const reloadSecondary = useCallback(async () => {
    setRunsLoading(true);
    try {
      const [runsData, diagnosticsData, settingsData] = await Promise.all([
        resourceMonitorClient.listRuns({ limit: 50 }),
        resourceMonitorClient.listDiagnosticEvents({ limit: 20 }),
        resourceMonitorClient.getSettings(),
      ]);
      setRuns(runsData);
      setDiagnostics(diagnosticsData);
      setSettings(settingsData);
    } catch {
      /* silent — surfaced by child components */
    } finally {
      setRunsLoading(false);
    }
  }, []);

  useEffect(() => {
    void reloadSecondary();
  }, [reloadSecondary]);

  const visibleDiagnostics = diagnostics.filter((d) => !dismissedIds.has(d.event_id));

  const handleDismissAlert = (eventId: string) => {
    setDismissedIds((prev) => new Set(prev).add(eventId));
  };

  const handleSaveSettings = async (updates: MonitorSettingsUpdate) => {
    const updated = await resourceMonitorClient.updateSettings(updates);
    setSettings(updated);
  };

  const handleManualRefresh = async () => {
    setIsRefreshing(true);
    try {
      await Promise.all([refreshCurrent(), refreshProcesses(), reloadSecondary()]);
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <div className="space-y-5">
      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
        className="flex flex-wrap items-start justify-between gap-3"
      >
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
            Resource Monitor
          </h1>
          <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
            Live hardware telemetry, process tree tracking, and execution attribution
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleManualRefresh}
            disabled={isRefreshing}
            className="group inline-flex items-center gap-1.5 rounded-lg border border-slate-200 dark:border-slate-700 bg-white/60 dark:bg-slate-900/60 px-3 py-1.5 text-sm font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-50 transition-colors"
          >
            <RefreshCw
              className={cn('h-3.5 w-3.5', isRefreshing && 'animate-spin')}
              strokeWidth={2.2}
            />
            Refresh
          </button>
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            disabled={!settings}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 dark:border-slate-700 bg-white/60 dark:bg-slate-900/60 px-3 py-1.5 text-sm font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-50 transition-colors"
          >
            <SettingsIcon className="h-3.5 w-3.5" strokeWidth={2.2} />
            Settings
          </button>
        </div>
      </motion.div>

      {/* Error banner */}
      <AnimatePresence>
        {currentError && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="flex items-start gap-2 rounded-lg border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950/30 px-4 py-2.5 text-sm text-red-700 dark:text-red-300">
              <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" strokeWidth={2.2} />
              <span>Failed to fetch live telemetry: {currentError.message}</span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Diagnostic alerts */}
      <DiagnosticsAlertBanner events={visibleDiagnostics} onDismiss={handleDismissAlert} />

      {/* Overview cards */}
      <HostOverviewCards snapshot={snapshot} isLoading={currentLoading} />

      {/* Tabs */}
      <div className="border-b border-slate-200 dark:border-slate-800">
        <nav className="-mb-px flex gap-1" aria-label="Resource Monitor Sections">
          {TABS.map((tab) => {
            const active = activeTab === tab.key;
            const Icon = tab.icon;
            const showOrphanBadge = tab.key === 'processes' && orphans.length > 0;
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => setActiveTab(tab.key)}
                className={cn(
                  'relative inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium transition-colors',
                  active
                    ? 'text-sky-600 dark:text-sky-400'
                    : 'text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200',
                )}
              >
                <Icon className="h-3.5 w-3.5" strokeWidth={2.2} />
                {tab.label}
                {showOrphanBadge && (
                  <span className="ml-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold text-white">
                    {orphans.length}
                  </span>
                )}
                {active && (
                  <motion.span
                    layoutId="active-tab"
                    className="absolute inset-x-0 -bottom-px h-0.5 rounded-full bg-sky-600 dark:bg-sky-400"
                    transition={{ type: 'spring', stiffness: 380, damping: 30 }}
                  />
                )}
              </button>
            );
          })}
        </nav>
      </div>

      {/* Tab content */}
      <AnimatePresence mode="wait">
        {activeTab === 'overview' && (
          <motion.div
            key="overview"
            initial={tabTransition.initial}
            animate={tabTransition.animate}
            exit={tabTransition.exit}
            transition={tabTransition.transition}
          >
            <TimeseriesCharts
              points={points}
              range={range}
              onRangeChange={setRange}
              isLoading={historyLoading}
            />
          </motion.div>
        )}
        {activeTab === 'processes' && (
          <motion.div
            key="processes"
            initial={tabTransition.initial}
            animate={tabTransition.animate}
            exit={tabTransition.exit}
            transition={tabTransition.transition}
          >
            <ProcessTreeTable
              processes={processes}
              orphans={orphans}
              isLoading={processesLoading}
              terminatingPid={terminatingPid}
              onTerminate={(pid) => terminateOrphan(pid)}
            />
          </motion.div>
        )}
        {activeTab === 'runs' && (
          <motion.div
            key="runs"
            initial={tabTransition.initial}
            animate={tabTransition.animate}
            exit={tabTransition.exit}
            transition={tabTransition.transition}
          >
            <RunConsumptionTable runs={runs} isLoading={runsLoading} />
          </motion.div>
        )}
      </AnimatePresence>

      <RetentionSettingsModal
        open={settingsOpen}
        initialSettings={settings}
        onClose={() => setSettingsOpen(false)}
        onSave={handleSaveSettings}
      />
    </div>
  );
}
