"use client";

import React, { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  RefreshCw,
  X,
  XCircle,
} from "lucide-react";
import {
  activateVersion,
  BundleEntry,
  BundleHealthReport,
  checkBundleHealth,
} from "@/lib/bundleApi";
import { showToast } from "@/components/ui/ToastNotification";

export interface BundleDetailsDrawerProps {
  open: boolean;
  bundle: BundleEntry | null;
  onClose: () => void;
  onUpdated: () => void;
}

export function BundleDetailsDrawer({
  open,
  bundle,
  onClose,
  onUpdated,
}: BundleDetailsDrawerProps) {
  const [health, setHealth] = useState<BundleHealthReport | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);
  const [activatingVersion, setActivatingVersion] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !bundle) {
      setHealth(null);
      return;
    }
    loadHealth();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, bundle?.id, bundle?.active_version]);

  const loadHealth = async () => {
    if (!bundle) return;
    setHealthLoading(true);
    try {
      const report = await checkBundleHealth(bundle.id);
      setHealth(report);
    } catch (err: any) {
      showToast.error("Health check failed", err.message);
    } finally {
      setHealthLoading(false);
    }
  };

  const handleActivate = async (version: string) => {
    if (!bundle) return;
    setActivatingVersion(version);
    try {
      await activateVersion(bundle.id, version);
      showToast.success("Version Activated", `Now running v${version}`);
      onUpdated();
    } catch (err: any) {
      showToast.error("Activation failed", err.message);
    } finally {
      setActivatingVersion(null);
    }
  };

  if (!open || !bundle) return null;

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const formatDate = (iso: string) => {
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  };

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/50 backdrop-blur-xs"
        onClick={onClose}
      />

      {/* Drawer */}
      <div className="fixed right-0 top-0 bottom-0 z-50 w-full max-w-md bg-card border-l border-border shadow-2xl flex flex-col animate-in slide-in-from-right duration-200">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-border bg-muted/20 shrink-0">
          <div className="min-w-0">
            <h2 className="text-sm font-bold text-foreground truncate" title={bundle.name}>
              {bundle.name}
            </h2>
            <span className="text-[10px] font-mono text-muted-foreground">
              {bundle.id} · v{bundle.active_version}
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition cursor-pointer shrink-0"
          >
            <X size={16} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-5 space-y-5 text-xs">
          {/* Description */}
          <div>
            <span className="text-[10px] uppercase font-semibold text-muted-foreground tracking-wider block mb-1.5">
              Description
            </span>
            <p className="text-xs text-foreground/90 leading-relaxed">
              {bundle.description || "—"}
            </p>
          </div>

          {/* Metadata grid */}
          <div className="grid grid-cols-2 gap-3">
            <div className="p-3 rounded-lg bg-muted/30 border border-border">
              <span className="text-[10px] text-muted-foreground block mb-0.5">Safety</span>
              <span className="font-mono text-[11px] text-foreground">{bundle.safety_level}</span>
            </div>
            <div className="p-3 rounded-lg bg-muted/30 border border-border">
              <span className="text-[10px] text-muted-foreground block mb-0.5">Versions</span>
              <span className="font-mono text-[11px] text-foreground">
                {bundle.installed_versions.length}
              </span>
            </div>
            {bundle.author && (
              <div className="p-3 rounded-lg bg-muted/30 border border-border col-span-2">
                <span className="text-[10px] text-muted-foreground block mb-0.5">Author</span>
                <span className="text-[11px] text-foreground">{bundle.author}</span>
              </div>
            )}
          </div>

          {/* Capabilities */}
          {bundle.capabilities.length > 0 && (
            <div>
              <span className="text-[10px] uppercase font-semibold text-muted-foreground tracking-wider block mb-1.5">
                Capabilities
              </span>
              <div className="flex flex-wrap gap-1">
                {bundle.capabilities.map((cap) => (
                  <span
                    key={cap}
                    className="px-2 py-0.5 rounded text-[10px] font-mono bg-muted text-muted-foreground border border-border"
                  >
                    {cap}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Health */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <span className="text-[10px] uppercase font-semibold text-muted-foreground tracking-wider">
                Health
              </span>
              <button
                type="button"
                onClick={loadHealth}
                disabled={healthLoading}
                className="p-1 rounded text-muted-foreground hover:text-foreground transition cursor-pointer disabled:opacity-40"
                title="Re-check"
              >
                <RefreshCw size={11} className={healthLoading ? "animate-spin" : ""} />
              </button>
            </div>

            {healthLoading && !health ? (
              <div className="flex items-center gap-2 text-muted-foreground text-[11px]">
                <Loader2 size={11} className="animate-spin" />
                <span>Checking…</span>
              </div>
            ) : health ? (
              <div
                className={`p-3 rounded-lg border space-y-2 ${
                  health.status === "healthy"
                    ? "bg-emerald-500/5 border-emerald-500/25"
                    : health.status === "degraded"
                    ? "bg-amber-500/5 border-amber-500/25"
                    : "bg-destructive/5 border-destructive/25"
                }`}
              >
                <div className="flex items-center gap-2">
                  {health.status === "healthy" ? (
                    <CheckCircle2 size={13} className="text-emerald-500" />
                  ) : health.status === "degraded" ? (
                    <AlertTriangle size={13} className="text-amber-500" />
                  ) : (
                    <XCircle size={13} className="text-destructive" />
                  )}
                  <span
                    className={`text-[11px] font-semibold ${
                      health.status === "healthy"
                        ? "text-emerald-600 dark:text-emerald-400"
                        : health.status === "degraded"
                        ? "text-amber-600 dark:text-amber-400"
                        : "text-destructive"
                    }`}
                  >
                    {health.status}
                  </span>
                </div>
                <div className="space-y-1">
                  {health.checks.map((c) => (
                    <div
                      key={c.name}
                      className="flex items-center justify-between text-[10px] font-mono"
                    >
                      <span className="text-muted-foreground">{c.name}</span>
                      <span
                        className={
                          c.passed
                            ? "text-emerald-500"
                            : "text-destructive"
                        }
                      >
                        {c.passed ? "✓" : "✗"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>

          {/* Version history */}
          <div>
            <span className="text-[10px] uppercase font-semibold text-muted-foreground tracking-wider block mb-2">
              Version History
            </span>
            <div className="border border-border rounded-lg overflow-hidden">
              <table className="w-full text-[10px] font-mono">
                <thead className="bg-muted/50 text-muted-foreground">
                  <tr>
                    <th className="text-left p-2 font-medium">Version</th>
                    <th className="text-left p-2 font-medium">Size</th>
                    <th className="text-left p-2 font-medium">Installed</th>
                    <th className="text-right p-2 font-medium">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {bundle.installed_versions
                    .slice()
                    .reverse()
                    .map((v) => {
                      const isActive = v.version === bundle.active_version;
                      const isActivating = activatingVersion === v.version;

                      return (
                        <tr
                          key={v.version}
                          className={isActive ? "bg-primary/5" : ""}
                        >
                          <td className="p-2">
                            <span
                              className={
                                isActive ? "text-primary font-semibold" : "text-foreground"
                              }
                            >
                              v{v.version}
                            </span>
                          </td>
                          <td className="p-2 text-muted-foreground">
                            {formatSize(v.size_bytes)}
                          </td>
                          <td className="p-2 text-muted-foreground truncate max-w-[100px]">
                            {formatDate(v.installed_at)}
                          </td>
                          <td className="p-2 text-right">
                            {isActive ? (
                              <span className="text-[9px] text-primary uppercase">
                                Active
                              </span>
                            ) : (
                              <button
                                type="button"
                                onClick={() => handleActivate(v.version)}
                                disabled={isActivating}
                                className="text-[10px] text-primary hover:underline cursor-pointer disabled:opacity-40"
                              >
                                {isActivating ? "…" : "Activate"}
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

export default BundleDetailsDrawer;
