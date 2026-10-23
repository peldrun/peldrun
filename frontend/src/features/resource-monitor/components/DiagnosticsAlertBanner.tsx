// frontend/src/features/resource-monitor/components/DiagnosticsAlertBanner.tsx
'use client';

/**
 * Animated alert banner for diagnostic events.
 */

import { AnimatePresence, motion } from 'motion/react';
import { AlertCircle, AlertTriangle, Info, X } from 'lucide-react';
import type { DiagnosticEvent, DiagnosticSeverity } from '../types';
import { formatRelativeTime } from '../utils/formatters';
import { cn } from '@/lib/utils';

interface DiagnosticsAlertBannerProps {
  events: DiagnosticEvent[];
  onDismiss?: (eventId: string) => void;
  maxVisible?: number;
}

const SEVERITY_META: Record<
  DiagnosticSeverity,
  { label: string; icon: typeof AlertCircle; className: string }
> = {
  CRITICAL: {
    label: 'Critical',
    icon: AlertCircle,
    className:
      'bg-red-50 dark:bg-red-950/40 border-red-300 dark:border-red-800 text-red-900 dark:text-red-100',
  },
  WARNING: {
    label: 'Warning',
    icon: AlertTriangle,
    className:
      'bg-amber-50 dark:bg-amber-950/40 border-amber-300 dark:border-amber-800 text-amber-900 dark:text-amber-100',
  },
  INFO: {
    label: 'Info',
    icon: Info,
    className:
      'bg-sky-50 dark:bg-sky-950/40 border-sky-300 dark:border-sky-800 text-sky-900 dark:text-sky-100',
  },
};

export function DiagnosticsAlertBanner({
  events,
  onDismiss,
  maxVisible = 3,
}: DiagnosticsAlertBannerProps) {
  const visible = events.slice(0, maxVisible);
  if (visible.length === 0) return null;

  return (
    <div className="space-y-2">
      <AnimatePresence initial={false}>
        {visible.map((event) => {
          const meta = SEVERITY_META[event.severity] ?? SEVERITY_META.INFO;
          const Icon = meta.icon;
          return (
            <motion.div
              key={event.event_id}
              layout
              initial={{ opacity: 0, y: -8, height: 0 }}
              animate={{ opacity: 1, y: 0, height: 'auto' }}
              exit={{ opacity: 0, x: 30, height: 0 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
              className="overflow-hidden"
            >
              <div
                role="alert"
                className={cn(
                  'flex items-start gap-3 rounded-xl border px-4 py-3',
                  meta.className,
                )}
              >
                <Icon className="mt-0.5 h-4 w-4 shrink-0" strokeWidth={2.4} />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium leading-tight">{event.message}</p>
                  <p className="mt-1 text-[11px] opacity-75">
                    <span className="font-semibold uppercase tracking-wide">{meta.label}</span>
                    {' · '}
                    {event.event_type.replace(/_/g, ' ').toLowerCase()}
                    {' · '}
                    {formatRelativeTime(event.recorded_at)}
                  </p>
                </div>
                {onDismiss && (
                  <button
                    type="button"
                    onClick={() => onDismiss(event.event_id)}
                    aria-label="Dismiss alert"
                    className="rounded-md p-1 hover:bg-black/5 dark:hover:bg-white/10 transition-colors"
                  >
                    <X className="h-3.5 w-3.5" strokeWidth={2.4} />
                  </button>
                )}
              </div>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
