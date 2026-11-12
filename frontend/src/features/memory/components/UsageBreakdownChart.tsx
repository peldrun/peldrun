// src/features/memory/components/UsageBreakdownChart.tsx

/**
 * Pie chart of token usage by a selected dimension.
 *
 * Uses `recharts` `<PieChart>` (spec §4.3). Renders one slice
 * per bucket in the response, colored by the dimension's
 * palette, plus a legend with percentages.
 *
 * The palette
 * -----------
 *
 * Hardcoded per dimension so that the same dimension always
 * gets the same colors across renders and across sessions.
 * Stable colors are important for at-a-glance recognition --
 * if "user scope" is emerald on Monday and rose on Tuesday,
 * users lose the ability to read the chart quickly.
 *
 * Empty state
 * -----------
 *
 * The backend returns `buckets: []` when no usage is recorded
 * in the period. We render an `<EmptyState>` in that case
 * rather than an empty pie chart, which would look like a bug.
 */

'use client';

import { useTranslation } from 'react-i18next';
import {
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
} from 'recharts';

import type { UsageBreakdownResponse } from '../types/responses';
import { cn } from '../lib/cn';
import { EmptyState } from './EmptyState';
import { BarChart3 } from 'lucide-react';

// ---------------------------------------------------------------------------
// Palettes
// ---------------------------------------------------------------------------

/**
 * A stable color per (dimension, key). Fallback is a hue
 * rotation for unknown keys -- dimensions evolve on the
 * backend, so we cannot enumerate every possible `key`.
 *
 * Colors are chosen for maximum separation on both light and
 * dark backgrounds. They match the badge palettes elsewhere
 * in the feature where the meaning overlaps (e.g. user-scope
 * tokens use the same indigo as the user-scope badge).
 */
const DIMENSION_COLORS: Record<string, string[]> = {
  scope: ['#6366f1', '#0ea5e9', '#8b5cf6', '#14b8a6'], // user, project, chat, agent
  project: [
    '#0ea5e9',
    '#6366f1',
    '#8b5cf6',
    '#14b8a6',
    '#f59e0b',
    '#ec4899',
    '#10b981',
  ],
  chat: [
    '#8b5cf6',
    '#6366f1',
    '#0ea5e9',
    '#14b8a6',
    '#f59e0b',
    '#ec4899',
    '#10b981',
  ],
};

const FALLBACK_COLORS = [
  '#6366f1',
  '#0ea5e9',
  '#8b5cf6',
  '#14b8a6',
  '#f59e0b',
  '#ec4899',
  '#10b981',
  '#ef4444',
];

function colorFor(dimension: string, index: number): string {
  const palette = DIMENSION_COLORS[dimension] ?? FALLBACK_COLORS;
  return palette[index % palette.length];
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface UsageBreakdownChartProps {
  data: UsageBreakdownResponse;
  /** Height in pixels. Defaults to 220. */
  height?: number;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function UsageBreakdownChart({
  data,
  height = 220,
  className,
}: UsageBreakdownChartProps) {
  const { t } = useTranslation('memory');

  if (data.buckets.length === 0) {
    return (
      <EmptyState
        icon={BarChart3}
        title={t('usage.empty')}
        className={className}
      />
    );
  }

  // Prepare rows for recharts. The `name` label is localized
  // for scope buckets (`user` -> "Personal") and left raw for
  // project/chat (the raw key is the project id / chat id).
  const rows = data.buckets.map((b, i) => ({
    name:
      data.by === 'scope'
        ? t(`scope.${b.key}`, { defaultValue: b.key })
        : b.key,
    value: b.tokens,
    pct: b.pct,
    fill: colorFor(data.by, i),
  }));

  return (
    <div className={cn('w-full', className)}>
      <div style={{ width: '100%', height }}>
        <ResponsiveContainer>
          <PieChart>
            <Pie
              data={rows}
              dataKey="value"
              nameKey="name"
              innerRadius="45%"
              outerRadius="75%"
              paddingAngle={1}
              strokeWidth={0}
              isAnimationActive={false}
            >
              {rows.map((row, i) => (
                <Cell key={i} fill={row.fill} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={{
                fontSize: 11,
                borderRadius: 6,
                border: '1px solid hsl(var(--border))',
                background: 'hsl(var(--card))',
                color: 'hsl(var(--foreground))',
              }}
              formatter={(value) => [`${String(value)} tokens`, ''] as [string, string]}
            />
            <Legend
              wrapperStyle={{ fontSize: 11 }}
              formatter={(value: string, entry) => {
                const payload = entry?.payload as
                  | { pct?: number; value?: number }
                  | undefined;
                const pct = payload?.pct;
                return pct !== undefined
                  ? `${value} (${pct.toFixed(1)}%)`
                  : value;
              }}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
