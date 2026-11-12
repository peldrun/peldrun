// src/features/memory/components/UsageTimeseriesChart.tsx

/**
 * Line chart of token usage over time.
 *
 * Uses `recharts` `<LineChart>`. Two series are drawn:
 *
 *   * Total tokens (primary color).
 *   * Context tokens (secondary color -- the portion of total
 *     actually injected into prompts; the difference is
 *     overhead).
 *
 * The x-axis uses the bucket label as returned by the backend
 * (`"YYYY-MM-DD"` / `"YYYY-Www"` / `"YYYY-MM"`). We do NOT
 * reformat it here -- the caller's `granularity` decision
 * should not be silently overridden by a display tweak, and
 * the ISO shape is unambiguous.
 */

'use client';

import { useTranslation } from 'react-i18next';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Activity } from 'lucide-react';

import type { UsageTimeseriesResponse } from '../types/responses';
import { cn } from '../lib/cn';
import { EmptyState } from './EmptyState';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface UsageTimeseriesChartProps {
  data: UsageTimeseriesResponse;
  /** Height in pixels. Defaults to 240. */
  height?: number;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function UsageTimeseriesChart({
  data,
  height = 240,
  className,
}: UsageTimeseriesChartProps) {
  const { t } = useTranslation('memory');

  if (data.points.length === 0) {
    return (
      <EmptyState
        icon={Activity}
        title={t('usage.empty')}
        className={className}
      />
    );
  }

  return (
    <div className={cn('w-full', className)}>
      <div style={{ width: '100%', height }}>
        <ResponsiveContainer>
          <LineChart
            data={data.points}
            margin={{ top: 4, right: 8, bottom: 4, left: -12 }}
          >
            <CartesianGrid
              strokeDasharray="2 4"
              stroke="hsl(var(--border))"
              vertical={false}
            />
            <XAxis
              dataKey="bucket"
              tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
              tickLine={false}
              axisLine={{ stroke: 'hsl(var(--border))' }}
            />
            <YAxis
              tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
              tickLine={false}
              axisLine={false}
              width={44}
            />
            <Tooltip
              contentStyle={{
                fontSize: 11,
                borderRadius: 6,
                border: '1px solid hsl(var(--border))',
                background: 'hsl(var(--card))',
                color: 'hsl(var(--foreground))',
              }}
            />
            <Legend
              wrapperStyle={{ fontSize: 11 }}
            />
            <Line
              type="monotone"
              dataKey="total_tokens"
              name={t('usage.kpis.totalTokens')}
              stroke="#6366f1"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="context_tokens"
              name={t('usage.kpis.entriesUsed')}
              stroke="#14b8a6"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
