// frontend/src/app/resource-monitor/page.tsx
'use client';

/**
 * Standalone Resource Monitor page.
 * Route: /resource-monitor
 *
 * Renders the full dashboard: live host/process cards, timeseries charts,
 * process tree with orphan remediation, run history, and settings modal.
 */

import { ResourceDashboard } from '@/features/resource-monitor/components/ResourceDashboard';

export default function ResourceMonitorPage() {
  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <ResourceDashboard />
    </main>
  );
}
