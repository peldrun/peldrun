// frontend/src/components/layout/SystemHealthPill.tsx
'use client';

/**
 * Compact icon-only health indicator for the top navigation bar / sidebar.
 *
 * Rendering strategy:
 *   The dropdown is rendered through a React Portal into `document.body`
 *   with `position: fixed`, so it escapes any parent `overflow: hidden`
 *   or `transform` context (common in sidebars and clipped containers).
 *
 * Placement logic (recomputed on every hover / resize / scroll):
 *   Horizontal → stays within viewport, shifts toward the center if needed.
 *   Vertical   → opens below the trigger; flips above when space is short.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion } from 'motion/react';
import Link from 'next/link';
import {
  Activity,
  AlertCircle,
  ArrowUpRight,
  RefreshCw,
  ShieldAlert,
  WifiOff,
} from 'lucide-react';
import { useResourceAlerts } from '@/app/resource-monitor/useResourceAlerts';
import { useServerStatus } from '@/hooks/useServerStatus';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface SystemHealthPillProps {
  /** Destination when the pill is clicked while online. Default: /resource-monitor */
  href?: string;
  /** Polling interval for diagnostic alerts. Defaults to hook's 30s. */
  refreshIntervalMs?: number;
  /** Delay (ms) before showing the dropdown on hover. Default 120. */
  hoverDelay?: number;
}

// ---------------------------------------------------------------------------
// Layout constants
// ---------------------------------------------------------------------------

const DROPDOWN_WIDTH = 256;      // must match the rendered width
const DROPDOWN_MAX_HEIGHT = 240; // conservative estimate for vertical fit test
const VIEWPORT_MARGIN = 12;      // min gap between dropdown edge and viewport
const ANCHOR_GAP = 6;            // gap between trigger and dropdown

interface DropdownCoords {
  top?: number;
  bottom?: number;
  left: number;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SystemHealthPill({
  href = '/resource-monitor',
  refreshIntervalMs,
  hoverDelay = 120,
}: SystemHealthPillProps) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const showTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [isHovered, setIsHovered] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [coords, setCoords] = useState<DropdownCoords>({
    top: 0,
    left: -9999, // off-screen until computed (first frame invisible anyway)
  });

  const { criticalCount, warningCount, totalCount } = useResourceAlerts({
    limit: 10,
    refreshIntervalMs,
  });

  const { status, latency, isChecking, checkNow } = useServerStatus();

  // Enable portals only after client hydration
  useEffect(() => {
    setMounted(true);
  }, []);

  // ── Compute viewport-relative coordinates ──────────────────────────────
  const computePosition = useCallback(() => {
    if (!wrapperRef.current) return;

    const rect = wrapperRef.current.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // Vertical decision: prefer below, flip above when space is short.
    const spaceBelow = vh - rect.bottom - VIEWPORT_MARGIN;
    const spaceAbove = rect.top - VIEWPORT_MARGIN;
    const goAbove = spaceBelow < DROPDOWN_MAX_HEIGHT && spaceAbove > spaceBelow;

    // Horizontal decision: align to the trigger, clamp to viewport.
    let left = rect.left;
    if (left + DROPDOWN_WIDTH > vw - VIEWPORT_MARGIN) {
      left = vw - DROPDOWN_WIDTH - VIEWPORT_MARGIN;
    }
    if (left < VIEWPORT_MARGIN) {
      left = VIEWPORT_MARGIN;
    }

    setCoords({
      top: goAbove ? undefined : rect.bottom + ANCHOR_GAP,
      bottom: goAbove ? vh - rect.top + ANCHOR_GAP : undefined,
      left,
    });
  }, []);

  // ── Hover handlers with debounce ───────────────────────────────────────
  const handleMouseEnter = useCallback(() => {
    if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
    computePosition();
    showTimerRef.current = setTimeout(() => setIsHovered(true), hoverDelay);
  }, [hoverDelay, computePosition]);

  const handleMouseLeave = useCallback(() => {
    if (showTimerRef.current) clearTimeout(showTimerRef.current);
    hideTimerRef.current = setTimeout(() => setIsHovered(false), 80);
  }, []);

  // Keep the dropdown open while hovering over it
  const handleDropdownEnter = useCallback(() => {
    if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
  }, []);

  // Reposition on resize / scroll while open
  useEffect(() => {
    if (!isHovered) return;
    const handle = () => computePosition();
    window.addEventListener('resize', handle);
    window.addEventListener('scroll', handle, true);
    return () => {
      window.removeEventListener('resize', handle);
      window.removeEventListener('scroll', handle, true);
    };
  }, [isHovered, computePosition]);

  // ── Derive visual state ────────────────────────────────────────────────
  const isOffline = status === 'offline';
  const isDegraded = status === 'degraded';
  const isCheckingStatus = status === 'checking';
  const hasCritical = !isOffline && !isDegraded && criticalCount > 0;
  const hasWarning = !isOffline && !isDegraded && warningCount > 0;
  const isHealthy = !isOffline && !isDegraded && !hasCritical && !hasWarning;

  let icon: React.ReactNode = null;
  let iconBg = '';
  let label: string | null = null;
  let labelColor = '';
  let pulseRing: string | null = null;
  let tooltipText = '';

  if (isCheckingStatus) {
    icon = <RefreshCw className="h-3.5 w-3.5 animate-spin" strokeWidth={2.4} />;
    iconBg = 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500';
    tooltipText = 'Checking system status…';
  } else if (isOffline) {
    icon = <WifiOff className="h-3.5 w-3.5" strokeWidth={2.4} />;
    iconBg = 'bg-rose-500 text-white';
    label = 'Offline';
    labelColor = 'text-rose-600 dark:text-rose-400';
    tooltipText = 'Server offline';
  } else if (isDegraded) {
    icon = <AlertCircle className="h-3.5 w-3.5" strokeWidth={2.4} />;
    iconBg = 'bg-amber-500 text-white';
    label = 'Degraded';
    labelColor = 'text-amber-600 dark:text-amber-400';
    tooltipText = `Server degraded${latency > 0 ? ` · ${latency}ms` : ''}`;
  } else if (hasCritical) {
    icon = <ShieldAlert className="h-3.5 w-3.5" strokeWidth={2.4} />;
    iconBg = 'bg-red-500 text-white';
    label = String(totalCount);
    labelColor = 'text-red-600 dark:text-red-400';
    pulseRing = 'bg-red-400';
    tooltipText = `${criticalCount} critical resource alert${criticalCount === 1 ? '' : 's'}`;
  } else if (hasWarning) {
    icon = <ShieldAlert className="h-3.5 w-3.5" strokeWidth={2.4} />;
    iconBg = 'bg-amber-500 text-white';
    label = String(totalCount);
    labelColor = 'text-amber-600 dark:text-amber-400';
    tooltipText = `${warningCount} resource warning${warningCount === 1 ? '' : 's'}`;
  } else {
    icon = <Activity className="h-3.5 w-3.5" strokeWidth={2.6} />;
    iconBg = 'bg-emerald-100 text-emerald-600 dark:bg-emerald-950/50 dark:text-emerald-400';
    pulseRing = 'bg-emerald-400';
    tooltipText = `System healthy${latency > 0 ? ` · ${latency}ms` : ''}`;
  }

  // ── Trigger (button for offline/degraded, link otherwise) ──────────────
  const trigger = (
    <>
      <span
        className={cn(
          'relative flex h-7 w-7 items-center justify-center rounded-md transition-colors',
          iconBg,
        )}
      >
        {pulseRing && (
          <span
            className={cn(
              'absolute inset-0 rounded-md animate-ping opacity-25',
              pulseRing,
            )}
          />
        )}
        <span className="relative">{icon}</span>
      </span>
      {label && (
        <span className={cn('text-[11px] font-semibold tabular-nums', labelColor)}>
          {label}
        </span>
      )}
    </>
  );

  const interactiveClass = cn(
    'inline-flex items-center gap-1.5 rounded-md p-0.5 transition-colors',
    'hover:bg-slate-100 dark:hover:bg-slate-800 cursor-pointer',
  );

  const triggerEl =
    isOffline || isDegraded ? (
      <button
        type="button"
        onClick={() => checkNow()}
        aria-label={tooltipText}
        title={tooltipText}
        className={interactiveClass}
      >
        {trigger}
        {isChecking && (
          <RefreshCw className="h-2.5 w-2.5 animate-spin text-slate-400" strokeWidth={2.4} />
        )}
      </button>
    ) : (
      <Link href={href} aria-label={tooltipText} title={tooltipText} className={interactiveClass}>
        {trigger}
      </Link>
    );

  // ── Dropdown content labels ────────────────────────────────────────────
  const connectionLabel = isOffline
    ? 'Offline'
    : isDegraded
      ? 'Degraded'
      : isCheckingStatus
        ? 'Checking…'
        : 'Online';

  const connectionTone = isOffline
    ? 'text-rose-600 dark:text-rose-400'
    : isDegraded
      ? 'text-amber-600 dark:text-amber-400'
      : 'text-emerald-600 dark:text-emerald-400';

  const monitorLabel = hasCritical
    ? 'Critical'
    : hasWarning
      ? 'Warning'
      : isHealthy
        ? 'Healthy'
        : 'Loading…';

  const monitorTone = hasCritical
    ? 'text-red-600 dark:text-red-400'
    : hasWarning
      ? 'text-amber-600 dark:text-amber-400'
      : isHealthy
        ? 'text-emerald-600 dark:text-emerald-400'
        : 'text-slate-500 dark:text-slate-400';

  // ── Portal-rendered dropdown ───────────────────────────────────────────
  const dropdown = (
    <AnimatePresence>
      {isHovered && (
        <motion.div
          initial={{
            opacity: 0,
            y: coords.top !== undefined ? -4 : 4,
            scale: 0.96,
          }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{
            opacity: 0,
            y: coords.top !== undefined ? -4 : 4,
            scale: 0.96,
          }}
          transition={{ duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
          role="tooltip"
          onMouseEnter={handleDropdownEnter}
          onMouseLeave={handleMouseLeave}
          style={{
            position: 'fixed',
            top: coords.top,
            bottom: coords.bottom,
            left: coords.left,
            width: DROPDOWN_WIDTH,
            zIndex: 9999,
          }}
          className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-xl overflow-y-auto max-h-[calc(100vh-1rem)]"
        >
          {/* Connection section */}
          <div className="px-3 py-2.5 border-b border-slate-100 dark:border-slate-800">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
                Connection
              </span>
              <span className={cn('text-[11px] font-semibold', connectionTone)}>
                {connectionLabel}
              </span>
            </div>
            {latency > 0 && !isOffline && (
              <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                Latency: <span className="font-medium tabular-nums">{latency}ms</span>
              </div>
            )}
          </div>

          {/* Resource Monitor section */}
          <div className="px-3 py-2.5">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
                Resource Monitor
              </span>
              <span className={cn('text-[11px] font-semibold', monitorTone)}>
                {monitorLabel}
              </span>
            </div>

            {hasCritical || hasWarning ? (
              <div className="mt-1.5 space-y-1">
                {criticalCount > 0 && (
                  <div className="flex items-center gap-1.5 text-[11px] text-slate-600 dark:text-slate-400">
                    <span className="h-1.5 w-1.5 rounded-full bg-red-500" />
                    {criticalCount} critical alert{criticalCount === 1 ? '' : 's'}
                  </div>
                )}
                {warningCount > 0 && (
                  <div className="flex items-center gap-1.5 text-[11px] text-slate-600 dark:text-slate-400">
                    <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
                    {warningCount} warning{warningCount === 1 ? '' : 's'}
                  </div>
                )}
              </div>
            ) : isHealthy ? (
              <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-slate-500 dark:text-slate-400">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                No active alerts
              </div>
            ) : (
              <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-slate-500 dark:text-slate-400">
                <RefreshCw className="h-2.5 w-2.5 animate-spin" />
                Loading diagnostics…
              </div>
            )}

            <Link
              href={href}
              className="mt-2.5 inline-flex items-center gap-1 text-[11px] font-medium text-sky-600 dark:text-sky-400 hover:underline"
            >
              Open dashboard
              <ArrowUpRight className="h-3 w-3" strokeWidth={2.4} />
            </Link>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );

  return (
    <div
      ref={wrapperRef}
      className="relative inline-flex"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {triggerEl}
      {mounted && createPortal(dropdown, document.body)}
    </div>
  );
}

export default SystemHealthPill;

// ---------------------------------------------------------------------------
// Backward compatibility re-exports
// ---------------------------------------------------------------------------

export { ResourceMonitorBadge } from '@/app/resource-monitor/ResourceMonitorBadge';
export type { ResourceMonitorBadgeProps } from '@/app/resource-monitor/ResourceMonitorBadge';

export { ServerStatusIndicator } from './ServerStatusIndicator';
