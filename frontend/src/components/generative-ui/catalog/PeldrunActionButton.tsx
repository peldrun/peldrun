// frontend/src/components/generative-ui/catalog/PeldrunActionButton.tsx
"use client";

import React from "react";
import type { PeldrunActionButtonProps } from "../types";

export function PeldrunActionButton({
  label,
  actionId,
  payload = {},
  variant = "primary",
  disabled = false,
  onExecute,
}: PeldrunActionButtonProps): React.JSX.Element {
  const handleClick = () => {
    if (disabled) return;
    if (onExecute) {
      onExecute({ actionId, payload });
    } else {
      console.info(`[GenerativeUI Action] ${actionId}`, payload);
    }
  };

  const variantStyles = {
    primary: "bg-primary text-primary-foreground hover:bg-primary/90",
    secondary: "bg-muted text-foreground hover:bg-muted/80 border border-border",
    danger: "bg-destructive text-destructive-foreground hover:bg-destructive/90",
  }[variant];

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={disabled}
      className={`px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition-all shadow-xs active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed ${variantStyles}`}
    >
      {label}
    </button>
  );
}
