// frontend/src/features/resource-monitor/components/RetentionSettingsModal.tsx
'use client';

/**
 * Animated settings modal for tuning sampling and retention.
 */

import { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { AlertCircle, Loader2, Save, X } from 'lucide-react';
import type { MonitorSettings, MonitorSettingsUpdate } from '../types';

interface RetentionSettingsModalProps {
  open: boolean;
  initialSettings: MonitorSettings | null;
  onClose: () => void;
  onSave: (updates: MonitorSettingsUpdate) => Promise<void>;
}

export function RetentionSettingsModal({
  open,
  initialSettings,
  onClose,
  onSave,
}: RetentionSettingsModalProps) {
  const [form, setForm] = useState<MonitorSettingsUpdate>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open && initialSettings) {
      setForm({
        sampling_rate_idle_sec: initialSettings.sampling_idle_interval_sec,
        sampling_rate_active_sec: initialSettings.sampling_active_interval_sec,
        retention_days: initialSettings.retention_days,
        orphan_grace_period_sec: initialSettings.orphan_grace_period_sec,
        cpu_throttle_threshold_percent: initialSettings.cpu_throttle_threshold_percent,
      });
      setError(null);
    }
  }, [open, initialSettings]);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !saving) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, saving, onClose]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await onSave(form);
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          key="backdrop"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
          onClick={() => !saving && onClose()}
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 backdrop-blur-sm p-4"
        >
          <motion.div
            key="modal"
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 8 }}
            transition={{ type: 'spring', stiffness: 320, damping: 30 }}
            onClick={(e) => e.stopPropagation()}
            className="w-full max-w-lg overflow-hidden rounded-2xl bg-white dark:bg-slate-900 shadow-2xl border border-slate-200 dark:border-slate-800"
          >
            <form onSubmit={handleSubmit}>
              <div className="flex items-center justify-between border-b border-slate-200 dark:border-slate-800 px-5 py-3.5">
                <div>
                  <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">
                    Monitor Settings
                  </h2>
                  <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                    Changes apply immediately to the running session.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={onClose}
                  disabled={saving}
                  aria-label="Close"
                  className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors disabled:opacity-50"
                >
                  <X className="h-4 w-4" strokeWidth={2.4} />
                </button>
              </div>

              <div className="space-y-4 px-5 py-4">
                <NumberField
                  label="Idle sampling interval (sec)"
                  description="How often to sample when no runs are active."
                  value={form.sampling_rate_idle_sec}
                  min={1}
                  max={60}
                  onChange={(v) => setForm((f) => ({ ...f, sampling_rate_idle_sec: v }))}
                />
                <NumberField
                  label="Active sampling interval (sec)"
                  description="Higher resolution during active execution."
                  value={form.sampling_rate_active_sec}
                  min={1}
                  max={10}
                  onChange={(v) => setForm((f) => ({ ...f, sampling_rate_active_sec: v }))}
                />
                <NumberField
                  label="Retention (days)"
                  description="How long to keep high-frequency samples."
                  value={form.retention_days}
                  min={1}
                  max={365}
                  onChange={(v) => setForm((f) => ({ ...f, retention_days: v }))}
                />
                <NumberField
                  label="Orphan grace period (sec)"
                  description="Delay before a lingering subprocess is flagged as orphan."
                  value={form.orphan_grace_period_sec}
                  min={5}
                  max={300}
                  onChange={(v) => setForm((f) => ({ ...f, orphan_grace_period_sec: v }))}
                />
                <NumberField
                  label="CPU throttle threshold (%)"
                  description="Relax sampling when host CPU exceeds this value."
                  value={form.cpu_throttle_threshold_percent}
                  min={50}
                  max={99}
                  step={0.5}
                  onChange={(v) =>
                    setForm((f) => ({ ...f, cpu_throttle_threshold_percent: v }))
                  }
                />
                <AnimatePresence>
                  {error && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: 'auto' }}
                      exit={{ opacity: 0, height: 0 }}
                      className="flex items-center gap-1.5 overflow-hidden rounded-md bg-red-50 dark:bg-red-950/40 px-2.5 py-1.5 text-xs text-red-700 dark:text-red-300"
                    >
                      <AlertCircle className="h-3 w-3 shrink-0" strokeWidth={2.4} />
                      {error}
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              <div className="flex items-center justify-end gap-2 border-t border-slate-200 dark:border-slate-800 px-5 py-3">
                <button
                  type="button"
                  onClick={onClose}
                  disabled={saving}
                  className="rounded-lg border border-slate-300 dark:border-slate-700 px-3.5 py-1.5 text-sm font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-50 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={saving}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-sky-600 px-3.5 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-sky-700 disabled:opacity-50 transition-colors"
                >
                  {saving ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={2.4} />
                  ) : (
                    <Save className="h-3.5 w-3.5" strokeWidth={2.4} />
                  )}
                  {saving ? 'Saving…' : 'Save'}
                </button>
              </div>
            </form>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

interface NumberFieldProps {
  label: string;
  description?: string;
  value: number | undefined;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
}

function NumberField({
  label,
  description,
  value,
  min,
  max,
  step,
  onChange,
}: NumberFieldProps) {
  return (
    <label className="block">
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium text-slate-700 dark:text-slate-300">{label}</span>
        <span className="text-[10px] text-slate-400 dark:text-slate-500">
          {min}–{max}
        </span>
      </div>
      <input
        type="number"
        min={min}
        max={max}
        step={step ?? 1}
        value={value ?? ''}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-950 px-3 py-1.5 text-sm text-slate-900 dark:text-slate-100 tabular-nums focus:outline-none focus:ring-2 focus:ring-sky-500/60 focus:border-sky-500 transition-shadow"
      />
      {description && (
        <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">{description}</p>
      )}
    </label>
  );
}
