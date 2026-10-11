// frontend/src/components/generative-ui/catalog/PeldrunCard.tsx
"use client";

import React from "react";
import { Info, CheckCircle2, AlertTriangle, XCircle } from "lucide-react";
import type { PeldrunCardProps } from "../types";

export function PeldrunCard({
  title,
  description,
  status = "info",
  children,
}: PeldrunCardProps): React.JSX.Element {
  const getStatusIcon = () => {
    switch (status) {
      case "success":
        return <CheckCircle2 size={16} className="text-emerald-500 shrink-0" />;
      case "warning":
        return <AlertTriangle size={16} className="text-amber-500 shrink-0" />;
      case "error":
        return <XCircle size={16} className="text-red-500 shrink-0" />;
      default:
        return <Info size={16} className="text-blue-500 shrink-0" />;
    }
  };

  return (
    <div className="my-2 p-3.5 rounded-xl border border-border/70 bg-card/60 backdrop-blur-xs text-foreground shadow-xs transition-all">
      {(title || status) && (
        <div className="flex items-center gap-2 mb-1.5">
          {getStatusIcon()}
          {title && <span className="font-semibold text-xs text-foreground tracking-tight">{title}</span>}
        </div>
      )}
      {description && <p className="text-xs text-muted-foreground leading-relaxed mb-2">{description}</p>}
      {children && <div className="mt-2 text-xs">{children}</div>}
    </div>
  );
}
