// frontend/src/features/resource-monitor/utils/formatters.ts
/**
 * Formatting helpers for converting raw telemetry numbers into
 * human-readable strings across the Resource Monitor UI.
 */

// ---------------------------------------------------------------------------
// Bytes
// ---------------------------------------------------------------------------

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'] as const;

/**
 * Format a byte count into a compact human-readable string.
 * @example formatBytes(1536) => "1.5 KB"
 */
export function formatBytes(bytes: number | null | undefined, decimals = 1): string {
  if (bytes == null || Number.isNaN(bytes) || bytes < 0) return '—';
  if (bytes === 0) return '0 B';

  const exponent = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    BYTE_UNITS.length - 1,
  );
  const value = bytes / Math.pow(1024, exponent);
  return `${value.toFixed(decimals)} ${BYTE_UNITS[exponent]}`;
}

// ---------------------------------------------------------------------------
// Duration
// ---------------------------------------------------------------------------

/**
 * Format a millisecond duration into a human-readable string.
 * @example formatDuration(90000) => "1m 30s"
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;

  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  const parts: string[] = [];
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  if (seconds > 0 || parts.length === 0) parts.push(`${seconds}s`);
  return parts.join(' ');
}

/**
 * Format a millisecond duration as clock-style HH:MM:SS or MM:SS.
 */
export function formatDurationClock(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms) || ms < 0) return '—';
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return hours > 0
    ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
    : `${pad(minutes)}:${pad(seconds)}`;
}

// ---------------------------------------------------------------------------
// Percentages & CPU
// ---------------------------------------------------------------------------

/**
 * Format a percentage value with a fixed precision.
 * @example formatPercent(34.567) => "34.6%"
 */
export function formatPercent(value: number | null | undefined, decimals = 1): string {
  if (value == null || Number.isNaN(value)) return '—';
  return `${value.toFixed(decimals)}%`;
}

// ---------------------------------------------------------------------------
// Timestamps
// ---------------------------------------------------------------------------

/**
 * Format an ISO timestamp as a short local time string.
 * @example formatTimestamp("2025-01-15T14:30:00Z") => "14:30:00"
 */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    return dt.toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return iso;
  }
}

/**
 * Format an ISO timestamp as a relative "x ago" description.
 */
export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return iso;

  const diffMs = Date.now() - dt.getTime();
  if (diffMs < 0) return 'in the future';
  if (diffMs < 5_000) return 'just now';
  if (diffMs < 60_000) return `${Math.floor(diffMs / 1000)}s ago`;
  if (diffMs < 3_600_000) return `${Math.floor(diffMs / 60_000)}m ago`;
  if (diffMs < 86_400_000) return `${Math.floor(diffMs / 3_600_000)}h ago`;
  return `${Math.floor(diffMs / 86_400_000)}d ago`;
}

/**
 * Format an epoch-milliseconds timestamp for axis labels, honoring a range.
 */
export function formatAxisTimestamp(epochMs: number, range: 'short' | 'long' = 'short'): string {
  const dt = new Date(epochMs);
  if (range === 'long') {
    return dt.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }
  return dt.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
  });
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

/** Truncate a long string with an ellipsis. */
export function truncate(value: string | null | undefined, maxLength = 60): string {
  if (!value) return '—';
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength - 1)}…`;
}
