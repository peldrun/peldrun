// frontend/src/components/generative-ui/catalog/PeldrunProgress.tsx
"use client";

import React from "react";
import type { PeldrunProgressProps } from "../types";

export function PeldrunProgress({
  label,
  percentage,
  statusText,
}: PeldrunProgressProps): React.JSX.Element {
  const clamped = Math.min(100, Math.max(0, percentage));

  return (
    <div className="my-2 space-y-1.5 text-xs">
      {(label || statusText) && (
        <div className="flex justify-between items-center text-[11px]">
          {label && <span className="font-medium text-foreground">{label}</span>}
          <span className="font-mono text-muted-foreground">
            {statusText || `${clamped}%`}
          </span>
        </div>
      )}
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted/80 border border-border/50">
        <div
          className="h-full bg-primary transition-all duration-300 rounded-full"
          style={{ width: `${clamped}%` }}
        />
      </div>
    </div>
  );
}
