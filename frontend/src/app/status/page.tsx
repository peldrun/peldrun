"use client";

import React, { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import {
  fetchFullDiagnostics,
  probeAllEndpoints,
  DiagnosticsReport,
  EndpointProbeResult
} from "@/lib/statusApi";
import {
  Activity,
  Cpu,
  HardDrive,
  Server,
  CheckCircle2,
  AlertCircle,
  XCircle,
  RefreshCw,
  Terminal,
  Settings,
  Sparkles,
  ShieldCheck,
  WifiOff
} from "lucide-react";
import { Button } from "@/components/ui/button";


import { BackendStatusCard } from "@/components/system/BackendStatusCard";
import { VersionCard } from "@/components/system/VersionCard";


export default function SystemStatusPage() {
  const [report, setReport] = useState<DiagnosticsReport | null>(null);
  const [probes, setProbes] = useState<EndpointProbeResult[]>([]);
  const [loading, setLoading] = useState<boolean>(true);

  const loadDiagnostics = useCallback(async () => {
    setLoading(true);
    try {
      const [diagData, probeData] = await Promise.all([
        fetchFullDiagnostics(),
        probeAllEndpoints()
      ]);
      setReport(diagData);
      setProbes(probeData);
    } catch (e) {
      console.error("Failed to load diagnostics:", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDiagnostics();
  }, [loadDiagnostics]);

  const mem = report?.systemInfo?.os?.memory;
  const totalGb = mem?.total_gb || 0;
  const availGb = mem?.available_gb || 0;
  const usedGb = totalGb > 0 ? Math.max(0, +(totalGb - availGb).toFixed(1)) : 0;
  const memUsagePercent = mem?.usage_percent || (totalGb > 0 ? Math.round((usedGb / totalGb) * 100) : 0);

  const overallStatus = !report?.isOnline
    ? "offline"
    : report.health?.status === "degraded"
    ? "degraded"
    : report.health?.status === "unhealthy"
    ? "unhealthy"
    : "healthy";

  return (
    <div className="w-full h-full flex flex-col flex-1 bg-background text-foreground overflow-hidden">
      {/* Header Bar */}
      <header className="w-full border-b border-border bg-custom backdrop-blur-md sticky top-0 z-10">
        <div className=" w-full px-4 sm:px-6 lg:px-8 py-4 flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-xl bg-peldrun-accent/10 text-peldrun-accent border border-accent/20 shadow-sm">
              <Activity className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-base font-bold text-foreground tracking-tight">
                  System Health & Diagnostics
                </h1>
                <span
                  className={`px-2 py-0.5 text-[10px] font-mono uppercase font-semibold rounded-full border ${
                    overallStatus === "healthy"
                      ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30"
                      : overallStatus === "degraded"
                      ? "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/30"
                      : "bg-destructive/10 text-destructive border-destructive/30"
                  }`}
                >
                  {overallStatus === "healthy"
                    ? "Operational"
                    : overallStatus === "degraded"
                    ? "Degraded"
                    : "Offline"}
                </span>
              </div>
              <p className="text-xs text-muted-foreground">
                Runtime Engine Readiness, Hardware Resource Monitor & Real-Time Probes
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2.5">
            {report?.latency !== undefined && report.latency > 0 && (
              <span className="hidden sm:inline-flex text-[11px] font-mono text-muted-foreground bg-muted px-2 py-1 rounded-md border border-border">
                Ping: {report.latency}ms
              </span>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={loadDiagnostics}
              disabled={loading}
              className="text-xs border-border bg-muted hover:bg-primary hover:text-primary-foreground text-primary transition shadow-sm h-8"
            >
              <RefreshCw size={13} className={`mr-1.5 ${loading ? "animate-spin" : ""}`} />
              Run Diagnostics
            </Button>
            <Link href="/settings">
              <Button
                size="sm"
                className="primary bg-primary hover:bg-primary/90 text-primary-foreground font-medium text-xs shadow-sm h-8 px-3.5 transition"
              >
                <Settings size={13} className="mr-1.5" />
                Settings
              </Button>
            </Link>
          </div>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="w-full flex-1 overflow-y-auto scrollbar-thin">
        <div className="max-w-[1240px] mx-auto w-full px-4 sm:px-6 lg:px-8 py-6 space-y-6">
          {/* Key Metric Highlights */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {/* Status Card */}
            <div className="p-4 rounded-2xl border border-border bg-card shadow-sm flex flex-col justify-between">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-muted-foreground">Core Status</span>
                {overallStatus === "healthy" ? (
                  <CheckCircle2 size={16} className="text-emerald-500" />
                ) : overallStatus === "degraded" ? (
                  <AlertCircle size={16} className="text-amber-500" />
                ) : (
                  <XCircle size={16} className="text-destructive" />
                )}
              </div>
              <div className="text-lg font-bold text-foreground capitalize">{overallStatus}</div>
              <span className="text-[11px] text-muted-foreground mt-1">
                {report?.health?.server || (report?.isOnline ? "Connected" : "Disconnected")}
              </span>
            </div>

            {/* RAM Usage Card */}
            <div className="p-4 rounded-2xl border border-border bg-card shadow-sm flex flex-col justify-between">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-muted-foreground">Memory Load</span>
                <HardDrive size={16} className="text-peldrun-accent" />
              </div>
              <div className="text-lg font-bold text-foreground">
                {totalGb > 0 ? `${memUsagePercent}%` : "-"}
                {totalGb > 0 && (
                  <span className="text-xs font-normal text-muted-foreground ml-2">
                    ({usedGb} / {totalGb} GB)
                  </span>
                )}
              </div>
              <div className="w-full bg-custom rounded-full h-1.5 mt-2 overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-500 ${
                    memUsagePercent > 90
                      ? "bg-destructive"
                      : memUsagePercent > 70
                      ? "bg-amber-500"
                      : "bg-peldrun-accent"
                  }`}
                  style={{ width: totalGb > 0 ? `${Math.min(100, memUsagePercent)}%` : "0%" }}
                />
              </div>
            </div>

            {/* CPU Brand Card */}
            <div className="p-4 rounded-2xl border border-border bg-card shadow-sm flex flex-col justify-between">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-muted-foreground">CPU Processor</span>
                <Cpu size={16} className="text-peldrun-accent" />
              </div>
              <div className="text-xs font-bold text-foreground truncate" title={report?.systemInfo?.os?.cpu_brand || "-"}>
                {report?.systemInfo?.os?.cpu_brand || "-"}
              </div>
              <span className="text-[11px] text-muted-foreground mt-1">
                {report?.systemInfo?.os?.cores !== undefined ? `${report.systemInfo.os.cores} Physical/Logical Cores` : "-"}
              </span>
            </div>

            {/* LLM Engine Readiness */}
            <div className="p-4 rounded-2xl border border-border bg-card shadow-sm flex flex-col justify-between">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-muted-foreground">LLM Engine</span>
                <Sparkles size={16} className="text-peldrun-accent" />
              </div>
              <div className="text-sm font-bold text-foreground">
                {report?.health?.llm_configured !== undefined
                  ? report.health.llm_configured
                    ? "Configured"
                    : "Not Configured"
                  : "-"}
              </div>
              <span className="text-[11px] text-muted-foreground mt-1">
                {report?.health?.llm_configured
                  ? "Keys & Models Verified"
                  : report?.isOnline
                  ? "Requires Setup in Settings"
                  : "Offline"}
              </span>
            </div>
          </div>

          {/* Detailed Diagnostic Grids */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Engine & Storage Integrity Card */}
            <div className="p-6 rounded-2xl border border-border bg-card shadow-sm space-y-4">
              <div className="flex items-center gap-2.5 pb-3 border-b border-border">
                <div className="p-2 rounded-lg bg-peldrun-accent/10 text-peldrun-accent border border-accent/20">
                  <Server size={16} />
                </div>
                <div>
                  <h3 className="text-sm font-bold text-foreground">peldrun Core & Filesystem Integrity</h3>
                  <p className="text-xs text-muted-foreground">Subsystem binding and workspace storage probes</p>
                </div>
              </div>

              <div className="space-y-3 text-xs font-mono">
                <div className="flex items-center justify-between p-2.5 rounded-xl bg-muted/40 border border-border">
                  <span className="text-muted-foreground">peldrun Engine Linked:</span>
                  <span className="flex items-center gap-1.5 font-semibold text-foreground">
                    {report?.health?.peldrun_linked === true ? (
                      <>
                        <CheckCircle2 size={13} className="text-emerald-500" />
                        Linked & Mounted
                      </>
                    ) : report?.health?.peldrun_linked === false ? (
                      <>
                        <XCircle size={13} className="text-destructive" />
                        Unlinked
                      </>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </span>
                </div>

                <div className="flex items-center justify-between p-2.5 rounded-xl bg-muted/40 border border-border">
                  <span className="text-muted-foreground">Workspaces Storage Access:</span>
                  <span className="flex items-center gap-1.5 font-semibold text-foreground">
                    {report?.health?.workspaces_writable === true ? (
                      <>
                        <CheckCircle2 size={13} className="text-emerald-500" />
                        Read & Write Enabled
                      </>
                    ) : report?.health?.workspaces_writable === false ? (
                      <>
                        <AlertCircle size={13} className="text-amber-500" />
                        Restricted / Read Only
                      </>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </span>
                </div>

                <div className="flex items-center justify-between p-2.5 rounded-xl bg-muted/40 border border-border">
                  <span className="text-muted-foreground">Core Git Commit:</span>
                  <span className="text-foreground font-semibold">
                    {report?.systemInfo?.repositories?.peldrun?.commit || "-"}
                  </span>
                </div>

                <div className="flex items-center justify-between p-2.5 rounded-xl bg-muted/40 border border-border">
                  <span className="text-muted-foreground">Web Gateway Version:</span>
                  <span className="text-foreground font-semibold">
                    {report?.systemInfo?.repositories?.peldrun_web?.version
                      ? `v${report.systemInfo.repositories.peldrun_web.version} (${report.systemInfo.repositories.peldrun_web.commit || "-"})`
                      : "-"}
                  </span>
                </div>
              </div>
            </div>

            {/* Software & Runtime Environment Card */}
            <div className="p-6 rounded-2xl border border-border bg-card shadow-sm space-y-4">
              <div className="flex items-center gap-2.5 pb-3 border-b border-border">
                <div className="p-2 rounded-lg bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 border border-cyan-500/20">
                  <Terminal size={16} />
                </div>
                <div>
                  <h3 className="text-sm font-bold text-foreground">Runtime Environment</h3>
                  <p className="text-xs text-muted-foreground">Operating system, Python interpreter, and paths</p>
                </div>
              </div>

              <div className="space-y-3 text-xs font-mono">
                <div className="flex items-center justify-between p-2.5 rounded-xl bg-muted/40 border border-border">
                  <span className="text-muted-foreground">Operating System:</span>
                  <span className="text-foreground font-semibold">
                    {report?.systemInfo?.os?.system
                      ? `${report.systemInfo.os.system} ${report.systemInfo.os.release} (${report.systemInfo.os.machine})`
                      : "-"}
                  </span>
                </div>

                <div className="flex items-center justify-between p-2.5 rounded-xl bg-muted/40 border border-border">
                  <span className="text-muted-foreground">Python Runtime:</span>
                  <span className="text-foreground font-semibold">
                    {report?.systemInfo?.software?.python
                      ? `v${report.systemInfo.software.python}`
                      : report?.health?.python_version
                      ? `v${report.health.python_version}`
                      : "-"}
                  </span>
                </div>

                <div className="p-2.5 rounded-xl bg-muted/40 border border-border space-y-1">
                  <span className="text-muted-foreground block text-[11px]">Interpreter Executable:</span>
                  <span className="text-foreground block truncate text-[11px]" title={report?.systemInfo?.software?.python_executable || "-"}>
                    {report?.systemInfo?.software?.python_executable || "-"}
                  </span>
                </div>

                <div className="p-2.5 rounded-xl bg-muted/40 border border-border space-y-1">
                  <span className="text-muted-foreground block text-[11px]">Core Repository Path:</span>
                  <span className="text-foreground block truncate text-[11px]" title={report?.systemInfo?.repositories?.peldrun?.path || "-"}>
                    {report?.systemInfo?.repositories?.peldrun?.path || "-"}
                  </span>
                </div>
              </div>
            </div>
          </div>

            {/* Backend & Version diagnostics */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <BackendStatusCard />
              <VersionCard />
            </div>

          {/* 100% Live Dynamic Network Probes Table */}
          <div className="p-6 rounded-2xl border border-border bg-card shadow-sm space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-border">
              <div className="flex items-center gap-2.5">
                <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20">
                  <ShieldCheck size={16} />
                </div>
                <div>
                  <h3 className="text-sm font-bold text-foreground">Live API Probe Audit</h3>
                  <p className="text-xs text-muted-foreground">
                    Real-time network response and latency tests across core endpoints
                  </p>
                </div>
              </div>
              <span className="text-xs font-mono text-muted-foreground">
                Last Tested: {report?.timestamp ? new Date(report.timestamp).toLocaleTimeString() : "-"}
              </span>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-xs font-mono text-left">
                <thead>
                  <tr className="border-b border-border text-muted-foreground">
                    <th className="pb-2.5 font-semibold">Endpoint</th>
                    <th className="pb-2.5 font-semibold">Purpose</th>
                    <th className="pb-2.5 font-semibold">Target Subsystem</th>
                    <th className="pb-2.5 font-semibold text-right">Live Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/60">
                  {probes.length === 0 && loading ? (
                    <tr>
                      <td colSpan={4} className="py-6 text-center text-muted-foreground">
                        <RefreshCw size={14} className="animate-spin inline mr-2 text-peldrun-accent" />
                        Running live network probes...
                      </td>
                    </tr>
                  ) : (
                    probes.map((p) => (
                      <tr key={p.endpoint}>
                        <td className="py-2.5 text-foreground font-semibold">{p.endpoint}</td>
                        <td className="py-2.5 text-muted-foreground">{p.purpose}</td>
                        <td className="py-2.5 text-muted-foreground">{p.target}</td>
                        <td className="py-2.5 text-right">
                          {p.ok ? (
                            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30">
                              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                              {p.status} {p.statusText} ({p.latency}ms)
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] font-semibold bg-destructive/10 text-destructive border border-destructive/30">
                              <span className="h-1.5 w-1.5 rounded-full bg-destructive" />
                              {p.status ? `${p.status}${p.statusText}` : p.statusText} ({p.latency}ms)
                            </span>
                          )}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
