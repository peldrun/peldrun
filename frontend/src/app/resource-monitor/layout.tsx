// frontend/src/app/resource-monitor/layout.tsx
/**
 * Route-level layout for /resource-monitor.
 * Provides page metadata without forcing the dashboard itself into RSC.
 */

import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'Resource Monitor · PELDRUN',
  description:
    'Live hardware telemetry, subprocess tracking, and per-run resource attribution for PELDRUN executions.',
};

export default function ResourceMonitorLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <>{children}</>;
}
