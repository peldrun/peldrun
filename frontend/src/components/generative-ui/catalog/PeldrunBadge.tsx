// frontend/src/components/generative-ui/catalog/PeldrunBadge.tsx
"use client";

import React from "react";
import type { PeldrunBadgeProps } from "../types";

export function PeldrunBadge({
  label,
  variant = "neutral",
}: PeldrunBadgeProps): React.JSX.Element {
  const variantStyles = {
    info: "bg-blue-500/15 text-blue-600 dark:text-blue-400 border-blue-500/20",
    success: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/20",
    warning: "bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/20",
    error: "bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/20",
    neutral: "bg-muted text-muted-foreground border-border",
  }[variant];

  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-mono font-medium border ${variantStyles}`}
    >
      {label}
    </span>
  );
}
