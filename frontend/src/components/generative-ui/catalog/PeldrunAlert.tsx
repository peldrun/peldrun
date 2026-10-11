// frontend/src/components/generative-ui/catalog/PeldrunAlert.tsx
"use client";

import React from "react";
import { Info, CheckCircle2, AlertTriangle, AlertCircle } from "lucide-react";
import type { PeldrunAlertProps } from "../types";

export function PeldrunAlert({
  message,
  type = "info",
  title,
}: PeldrunAlertProps): React.JSX.Element {
  const typeConfig = {
    info: {
      icon: <Info size={15} className="text-blue-500 shrink-0" />,
      container: "bg-blue-500/10 border-blue-500/20 text-blue-900 dark:text-blue-200",
    },
    success: {
      icon: <CheckCircle2 size={15} className="text-emerald-500 shrink-0" />,
      container: "bg-emerald-500/10 border-emerald-500/20 text-emerald-900 dark:text-emerald-200",
    },
    warning: {
      icon: <AlertTriangle size={15} className="text-amber-500 shrink-0" />,
      container: "bg-amber-500/10 border-amber-500/20 text-amber-900 dark:text-amber-200",
    },
    error: {
      icon: <AlertCircle size={15} className="text-red-500 shrink-0" />,
      container: "bg-red-500/10 border-red-500/20 text-red-900 dark:text-red-200",
    },
  }[type];

  return (
    <div className={`my-2 p-3 rounded-xl border flex items-start gap-2.5 text-xs ${typeConfig.container}`}>
      {typeConfig.icon}
      <div className="space-y-0.5 flex-1 leading-relaxed">
        {title && <div className="font-semibold">{title}</div>}
        <div>{message}</div>
      </div>
    </div>
  );
}
