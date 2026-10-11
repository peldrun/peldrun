// frontend/src/components/generative-ui/catalog/PeldrunMetric.tsx
"use client";

import React from "react";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";
import type { PeldrunMetricProps } from "../types";

export function PeldrunMetric({
  label,
  value,
  change,
  trend = "neutral",
  caption,
}: PeldrunMetricProps): React.JSX.Element {
  const renderTrendIcon = () => {
    if (trend === "up") return <TrendingUp size={13} className="text-emerald-500 shrink-0" />;
    if (trend === "down") return <TrendingDown size={13} className="text-red-500 shrink-0" />;
    return <Minus size={13} className="text-muted-foreground shrink-0" />;
  };

  return (
    <div className="p-3 rounded-xl border border-border/70 bg-card/50 shadow-xs space-y-1">
      <div className="text-[11px] font-medium text-muted-foreground truncate">{label}</div>
      <div className="flex items-baseline gap-2">
        <span className="text-lg font-bold text-foreground tracking-tight font-mono">{value}</span>
        {change && (
          <div className="flex items-center gap-0.5 text-[11px] font-semibold font-mono">
            {renderTrendIcon()}
            <span
              className={
                trend === "up"
                  ? "text-emerald-500"
                  : trend === "down"
                  ? "text-red-500"
                  : "text-muted-foreground"
              }
            >
              {change}
            </span>
          </div>
        )}
      </div>
      {caption && <div className="text-[10px] text-muted-foreground">{caption}</div>}
    </div>
  );
}
