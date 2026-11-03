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
    <main className="ResourceDashboard mx-auto w-full overflow-hidden relative flex">
      <ResourceDashboard />
    </main>
  );
}
