// frontend/src/app/resource-monitor/index.ts
/**
 * App-level integration surface for the Resource Monitor.
 *
 * The `/resource-monitor` route itself is served by `page.tsx` in this folder.
 * This barrel exposes the badge + alerts hook for app-level consumers
 * (e.g. the top navigation bar). Feature internals remain under
 * src/features/resource-monitor/ and should be imported directly from there.
 */

export { ResourceMonitorBadge } from './ResourceMonitorBadge';
export type { ResourceMonitorBadgeProps } from './ResourceMonitorBadge';

export { useResourceAlerts } from './useResourceAlerts';
export type {
  UseResourceAlertsOptions,
  UseResourceAlertsResult,
} from './useResourceAlerts';
