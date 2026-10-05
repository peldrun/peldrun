"use client";

import React, { useState, useEffect } from "react";
import { CheckCircle2, AlertCircle, Info, AlertTriangle, X, Zap } from "lucide-react";

export interface ToastMessage {
  id: string;
  type: "success" | "error" | "warning" | "info";
  title: string;
  message?: string;
  latency?: number;
  duration?: number;
}

type ToastListener = (toast: ToastMessage) => void;
const listeners: ToastListener[] = [];

export const showToast = {
  success: (title: string, message?: string, latency?: number) => {
    emitToast({ id: `t_${Date.now()}_${Math.random()}`, type: "success", title, message, latency });
  },
  error: (title: string, message?: string) => {
    emitToast({ id: `t_${Date.now()}_${Math.random()}`, type: "error", title, message, duration: 6000 });
  },
  warning: (title: string, message?: string) => {
    emitToast({ id: `t_${Date.now()}_${Math.random()}`, type: "warning", title, message });
  },
  info: (title: string, message?: string) => {
    emitToast({ id: `t_${Date.now()}_${Math.random()}`, type: "info", title, message });
  },
};

function emitToast(toast: ToastMessage) {
  listeners.forEach((fn) => fn(toast));
}

export function ToastContainer() {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  useEffect(() => {
    const handler = (newToast: ToastMessage) => {
      setToasts((prev) => [...prev, newToast]);
      const timer = setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== newToast.id));
      }, newToast.duration || 4500);
      return () => clearTimeout(timer);
    };

    listeners.push(handler);
    return () => {
      const idx = listeners.indexOf(handler);
      if (idx > -1) listeners.splice(idx, 1);
    };
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-5 right-5 z-[9999] flex flex-col gap-2.5 max-w-md w-full pointer-events-none px-4">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`pointer-events-auto flex items-start gap-3 p-3.5 rounded-lg border shadow-xl backdrop-blur-md transition-all duration-300 animate-in slide-in-from-top-3 ${
            t.type === "success"
              ? "bg-card border-card text-black dark:bg-emerald-950/90"
              : t.type === "error"
              ? "bg-rose-950/85 border-rose-500/40 text-rose-100 dark:bg-rose-950/90"
              : t.type === "warning"
              ? "bg-amber-950/85 border-amber-500/40 text-amber-100 dark:bg-amber-950/90"
              : "bg-slate-900/90 border-slate-700 text-slate-100"
          }`}
        >
          <div className="shrink-0 mt-0.5">
            {t.type === "success" && <CheckCircle2 size={17} className="text-emerald-400" />}
            {t.type === "error" && <AlertCircle size={17} className="text-rose-400" />}
            {t.type === "warning" && <AlertTriangle size={17} className="text-amber-400" />}
            {t.type === "info" && <Info size={17} className="text-sky-400" />}
          </div>

          <div className="flex-1 space-y-0.5 pr-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold">{t.title}</span>
              {t.latency !== undefined && (
                <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.2 rounded font-mono bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
                  <Zap size={9} /> {t.latency} ms
                </span>
              )}
            </div>
            {t.message && <p className="text-[11px] opacity-85 leading-relaxed">{t.message}</p>}
          </div>

          <button
            onClick={() => setToasts((prev) => prev.filter((item) => item.id !== t.id))}
            className="shrink-0 opacity-60 hover:opacity-100 transition-opacity p-0.5 cursor-pointer"
          >
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
