// frontend/src/app/source-control/page.tsx

"use client";

import React, { useEffect, useMemo } from "react";
import Link from "next/link";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  Code2,
  FileDiff,
  Gauge,
  GitBranch,
  Loader2,
  RotateCcw,
  Settings,
} from "lucide-react";
import { useSourceControlSettings } from "@/features/source-control/state/source-control-settings-store";

export default function SourceControlSettingsPage() {
  const hydrated = useSourceControlSettings((s) => s.hydrated);
  const hydrate = useSourceControlSettings((s) => s.hydrate);
  const saveError = useSourceControlSettings((s) => s.lastError);
  const lastSavedAt = useSourceControlSettings((s) => s.lastSavedAt);

  useEffect(() => {
    if (!hydrated) void hydrate();
  }, [hydrated, hydrate]);

  const enabled = useSourceControlSettings((s) => s.enabled);
  const saving = useSourceControlSettings((s) => s.saving);
  const setEnabled = useSourceControlSettings((s) => s.setEnabled);

  const defaultPanelMode = useSourceControlSettings((s) => s.defaultPanelMode);
  const setDefaultPanelMode = useSourceControlSettings((s) => s.setDefaultPanelMode);
  const defaultSourceControlMode = useSourceControlSettings((s) => s.defaultSourceControlMode);
  const setDefaultSourceControlMode = useSourceControlSettings((s) => s.setDefaultSourceControlMode);
  const autoOpenOnOperation = useSourceControlSettings((s) => s.autoOpenOnOperation);
  const setAutoOpenOnOperation = useSourceControlSettings((s) => s.setAutoOpenOnOperation);
  const diffViewMode = useSourceControlSettings((s) => s.diffViewMode);
  const setDiffViewMode = useSourceControlSettings((s) => s.setDiffViewMode);
  const diffSampleLines = useSourceControlSettings((s) => s.diffSampleLines);
  const setDiffSampleLines = useSourceControlSettings((s) => s.setDiffSampleLines);
  const pollingIntervalMs = useSourceControlSettings((s) => s.pollingIntervalMs);
  const setPollingIntervalMs = useSourceControlSettings((s) => s.setPollingIntervalMs);
  const listPageSize = useSourceControlSettings((s) => s.listPageSize);
  const setListPageSize = useSourceControlSettings((s) => s.setListPageSize);
  const resetToDefaults = useSourceControlSettings((s) => s.resetToDefaults);

  // Human-readable "last saved" label, recomputed whenever the
  // timestamp changes. Uses a short relative form ("just now",
  // "12s ago") so it stays readable without a live ticking clock.
  const lastSavedLabel = useMemo(() => {
    if (!lastSavedAt) return null;
    const seconds = Math.max(0, Math.floor((Date.now() - lastSavedAt) / 1000));
    if (seconds < 5) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const d = new Date(lastSavedAt);
    return d.toLocaleTimeString();
  }, [lastSavedAt]);

  return (
    <div className="h-full w-full overflow-y-auto bg-background">
      <div className="max-w-3xl mx-auto py-8 px-4 sm:px-6 lg:px-8 pb-16">
        {/* Header */}
        <div className="mb-6">
          <Link
            href="/settings"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground mb-3"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            Back to Settings
          </Link>
          <div className="flex items-start gap-3">
            <div className="p-2 rounded-md bg-primary/10 shrink-0">
              <GitBranch className="w-6 h-6 text-primary" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-foreground">
                Source Control
              </h1>
              <p className="mt-1 text-sm text-muted-foreground">
                Enable or disable the Source Control subsystem and tune its behavior.
                GitHub credentials and OAuth connections live in{" "}
                <Link href="/integrations" className="text-primary hover:underline font-medium">
                  Integrations
                </Link>
                .
              </p>
            </div>
          </div>
        </div>

        {/* Save error banner (also raises a toast; the banner stays
            visible for as long as the error is present) */}
        {saveError && (
          <div className="mb-4 flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-xs text-red-800">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <div>
              <p className="font-medium">Failed to save to disk.</p>
              <p className="mt-0.5 text-red-700 font-mono break-all">{saveError}</p>
            </div>
          </div>
        )}

        {/* Master switch */}
        <div className="rounded-sm  bg-gigbg p-5">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-start gap-3 flex-1">
              <Settings className="w-5 h-5 text-primary shrink-0 mt-0.5" />
              <div>
                <h2 className="text-sm font-semibold text-foreground">
                  {enabled ? "Source Control is Enabled" : "Source Control is Disabled"}
                </h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  {enabled
                    ? "The Source Control side panel, review cards, and all repository features are active."
                    : "The GitBranch icon is hidden from the chat header, the panel is never mounted, and review cards are not rendered. Default for users who never write code."}
                </p>
              </div>
            </div>
            <ToggleSwitch
              checked={enabled}
              onChange={setEnabled}
              ariaLabel="Enable Source Control"
            />
          </div>
        </div>

        {/* Disabled banner */}
        {!enabled && (
          <div className="mt-4 flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-amber-700" />
            <p>Settings below are read-only while Source Control is disabled. Enable it above to make changes.</p>
          </div>
        )}

        {/* Sections */}
        <fieldset
          disabled={!enabled}
          className={`border-0 p-0 m-0 min-w-0 transition-opacity ${enabled ? "opacity-100" : "opacity-60"}`}
        >
          <Section icon={<Code2 className="w-4 h-4" />} title="Panel Behavior" description="Choose how the right-hand panel opens and which mode the Source Control tabs start in.">
            <Field label="Default right-hand panel">
              <SegmentedControl
                value={defaultPanelMode}
                onChange={(v) => setDefaultPanelMode(v as "workspace" | "source-control" | "hidden")}
                options={[
                  { value: "workspace", label: "Workspace" },
                  { value: "source-control", label: "Source Control" },
                  { value: "hidden", label: "Hidden" },
                ]}
              />
            </Field>
            <Field label="Default Source Control mode">
              <SegmentedControl
                value={defaultSourceControlMode}
                onChange={(v) => setDefaultSourceControlMode(v as "agent" | "manual" | "browse")}
                options={[
                  { value: "agent", label: "Agent" },
                  { value: "manual", label: "Manual" },
                  { value: "browse", label: "Browse" },
                ]}
              />
            </Field>
            <Field label="Open panel automatically on new operation">
              <ToggleSwitch
                checked={autoOpenOnOperation}
                onChange={setAutoOpenOnOperation}
                ariaLabel="Auto-open panel on new operation"
              />
            </Field>
          </Section>

          <Section icon={<FileDiff className="w-4 h-4" />} title="Diff Viewer" description="Control how code changes are rendered.">
            <Field label="Diff layout">
              <SegmentedControl
                value={diffViewMode}
                onChange={(v) => setDiffViewMode(v as "unified" | "split")}
                options={[
                  { value: "unified", label: "Unified" },
                  { value: "split", label: "Split" },
                ]}
              />
            </Field>
            <Field label="Diff sample lines" hint="Higher values show more of the change but load slower.">
              <NumberInput value={diffSampleLines} onChange={setDiffSampleLines} min={100} max={5000} step={100} suffix="lines" />
            </Field>
          </Section>

          <Section icon={<Gauge className="w-4 h-4" />} title="Performance" description="Tune polling and pagination. The defaults are fine for most users.">
            <Field label="Polling interval" hint="How often to check for operation state changes.">
              <NumberInput value={pollingIntervalMs} onChange={setPollingIntervalMs} min={500} max={30000} step={500} suffix="ms" />
            </Field>
            <Field label="List page size" hint="Rows per page in commits, pull requests, and issues.">
              <NumberInput value={listPageSize} onChange={setListPageSize} min={10} max={100} step={10} suffix="items" />
            </Field>
          </Section>

          <Section icon={<RotateCcw className="w-4 h-4" />} title="Maintenance" description="Reset all settings to their factory defaults.">
            <button
              type="button"
              onClick={() => {
                if (window.confirm("Reset all Source Control settings to their defaults? This includes disabling the subsystem.")) {
                  resetToDefaults();
                }
              }}
              className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded border border-red-300 bg-red-50 text-red-700 hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              Reset to defaults
            </button>
          </Section>
        </fieldset>

        {/* --------------------------------------------------------------
            Sticky save indicator — always visible at the bottom of the
            content. Uses three distinct visual states:
              - idle with no save yet:      "Changes save automatically"
              - idle with a prior save:     "Last saved <label>"
              - saving:                     spinner + "Saving to disk…"
            -------------------------------------------------------------- */}
        <div className="sticky bottom-0 -mx-4 sm:-mx-6 lg:-mx-8 mt-8 px-4 sm:px-6 lg:px-8 py-3 bg-background/95 backdrop-blur border-t border-border">
          {saving ? (
            <div className="flex items-center gap-2 text-xs font-medium text-primary">
              <Loader2 className="w-4 h-4 animate-spin" />
              Saving to disk…
            </div>
          ) : saveError ? (
            <div className="flex items-center gap-2 text-xs font-medium text-red-700">
              <AlertCircle className="w-4 h-4" />
              Last save failed. See the banner above for details.
            </div>
          ) : lastSavedLabel ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Check className="w-4 h-4 text-green-600" />
              <span className="font-medium text-foreground">Saved</span>
              <span>·</span>
              <span>{lastSavedLabel}</span>
            </div>
          ) : (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Check className="w-4 h-4 text-green-600" />
              Changes save automatically to disk.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Building blocks
// ----------------------------------------------------------------------

function Section({ icon, title, description, children }: { icon: React.ReactNode; title: string; description?: string; children: React.ReactNode }) {
  return (
    <section className="mt-6 rounded-sm shadow-md hover:shadow-sm bg-card overflow-hidden">
      <header className="px-5 py-3 border-b border-border bg-black/5">
        <h2 className="text-sm font-semibold text-black flex items-center gap-1.5">
          <span className="text-black">{icon}</span>
          {title}
        </h2>
        {description && <p className="mt-1 text-xs text-black">{description}</p>}
      </header>
      <div className="p-5 space-y-4">{children}</div>
    </section>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-1">
      <div className="flex-1 min-w-0">
        <div className="text-xs font-medium text-foreground">{label}</div>
        {hint && <p className="text-[11px] text-muted-foreground mt-0.5">{hint}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function ToggleSwitch({ checked, onChange, ariaLabel }: { checked: boolean; onChange: (v: boolean) => void; ariaLabel: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:cursor-not-allowed ${checked ? "bg-peldrun-success" : "border border-gigbg bg-black/20"}`}
    >
      <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${checked ? "translate-x-6" : "translate-x-1"}`} />
    </button>
  );
}

function SegmentedControl({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: { value: string; label: string }[] }) {
  return (
    <div className="inline-flex rounded-md bg-muted p-0.5">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          className={`px-2.5 py-1 text-[11px] font-medium rounded transition-colors disabled:cursor-not-allowed ${value === opt.value ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"}`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

function NumberInput({ value, onChange, min, max, step, suffix }: { value: number; onChange: (v: number) => void; min: number; max: number; step: number; suffix: string }) {
  return (
    <div className="flex items-center gap-1">
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          const parsed = Number(e.target.value);
          if (!Number.isNaN(parsed)) onChange(parsed);
        }}
        className="w-20 text-xs px-2 py-1 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary text-right font-mono disabled:cursor-not-allowed disabled:opacity-60"
      />
      <span className="text-[11px] text-muted-foreground">{suffix}</span>
    </div>
  );
}
