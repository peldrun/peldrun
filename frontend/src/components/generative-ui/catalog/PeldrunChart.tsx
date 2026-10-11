// frontend/src/components/generative-ui/catalog/PeldrunChart.tsx
"use client";

import React from "react";
import type { PeldrunChartProps } from "../types";

export function PeldrunChart({
  title,
  type = "bar",
  data = [],
}: PeldrunChartProps): React.JSX.Element | null {
  if (!data || data.length === 0) {
    return null;
  }

  // Safe maximum calculation without spread operator to eliminate TS2556
  const maxValue = data.reduce(
    (max, item) => (item.value > max ? item.value : max),
    1
  );

  const chartHeight = 120;
  const chartWidth = 320;
  const padding = 20;

  // Pre-calculate SVG polyline points
  const polylinePoints = data
    .map((point, index) => {
      const divisor = Math.max(data.length - 1, 1);
      const x = padding + (index / divisor) * (chartWidth - padding * 2);
      const y =
        chartHeight - padding - (point.value / maxValue) * (chartHeight - padding * 2);
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <div className="my-2 p-3.5 rounded-xl border border-border/70 bg-card/40 space-y-2">
      {title && (
        <div className="text-xs font-semibold text-foreground tracking-tight">
          {title}
        </div>
      )}
      <div className="w-full overflow-x-auto">
        {type === "bar" ? (
          <div className="flex items-end gap-2 h-28 pt-4 pb-2 px-1">
            {data.map((item, idx) => {
              const heightPercent = Math.round((item.value / maxValue) * 100);
              return (
                <div
                  key={`bar-${idx}`}
                  className="flex-1 flex flex-col items-center gap-1 min-w-[28px]"
                >
                  <span className="text-[9px] font-mono text-muted-foreground">
                    {item.value}
                  </span>
                  <div className="w-full bg-muted rounded-t-sm h-20 flex items-end overflow-hidden">
                    <div
                      className="w-full bg-primary hover:bg-primary/80 transition-all rounded-t-sm"
                      style={{ height: `${heightPercent}%` }}
                    />
                  </div>
                  <span
                    className="text-[10px] text-muted-foreground truncate max-w-[36px]"
                    title={item.label}
                  >
                    {item.label}
                  </span>
                </div>
              );
            })}
          </div>
        ) : (
          <svg
            viewBox={`0 0 ${chartWidth} ${chartHeight}`}
            className="w-full h-28 overflow-visible"
          >
            <polyline
              fill="none"
              stroke="currentColor"
              className="text-primary"
              strokeWidth={2.5}
              strokeLinecap="round"
              strokeLinejoin="round"
              points={polylinePoints}
            />
            {data.map((point, idx) => {
              const divisor = Math.max(data.length - 1, 1);
              const cx = padding + (idx / divisor) * (chartWidth - padding * 2);
              const cy =
                chartHeight - padding - (point.value / maxValue) * (chartHeight - padding * 2);
              return (
                <circle
                  key={`circle-${idx}`}
                  cx={cx}
                  cy={cy}
                  r={3.5}
                  className="fill-background stroke-primary stroke-2"
                />
              );
            })}
          </svg>
        )}
      </div>
    </div>
  );
}
