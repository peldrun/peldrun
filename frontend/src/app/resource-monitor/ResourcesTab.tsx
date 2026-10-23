// frontend/src/app/resource-monitor/ResourcesTab.tsx
'use client';

/**
 * Settings tab wrapper around the Resource Dashboard.
 * Mount it from app/settings/setup/page.tsx WITHOUT touching UsageTab.tsx.
 */

import { ResourceDashboard } from '@/features/resource-monitor/components/ResourceDashboard';

export interface ResourcesTabProps {
  /** When true (default), renders with embedded padding for tab containers. */
  embedded?: boolean;
}

export function ResourcesTab({ embedded = true }: ResourcesTabProps) {
  return (
    <section
      aria-labelledby="resources-tab-heading"
      className={embedded ? 'p-4' : 'p-6'}
    >
      <header className="mb-4">
        <h2
          id="resources-tab-heading"
          className="text-xl font-semibold text-gray-900 dark:text-gray-100"
        >
          Resource Monitor
        </h2>
        <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
          Live hardware telemetry, subprocess tracking, and per-run resource
          attribution.
        </p>
      </header>
      <ResourceDashboard />
    </section>
  );
}
