"use client";

import React from "react";
import { RefreshCw, Cpu, Activity, HardDrive } from "lucide-react";
import { Button } from "@/components/ui/button";
import { BackendStatusCard } from "@/components/system/BackendStatusCard";
import { VersionCard } from "@/components/system/VersionCard";

interface SystemTabProps {
  systemInfo: any;
  fetchSystemInfo: () => void;
}

export function SystemTab({ systemInfo, fetchSystemInfo }: SystemTabProps) {
  return (
    <div className=" w-full space-y-6 max-auto font-sans pb-6">
      <div className="border-b border-border w-full pb-4 pt-4 bg-custom">
        <div className="flex items-center justify-between pr-4 pl-4">
          <div>
            <div className="flex items-center gap-2">
              <Cpu size={16} className="text-primary" />
              <h2 className="text-sm font-semibold font-heading text-foreground uppercase tracking-wide">
                Hardware Diagnostics & System Telemetry
              </h2>
            </div>
            <p className="text-xs text-muted-foreground mt-0.5">
              Host CPU brand, physical cores, live RAM utilization, and git synchronization state.
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={fetchSystemInfo}
            className="h-8 px-2.5 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer shadow-xs"
          >
            <RefreshCw size={13} className="mr-1.5" />
            <span>Refresh</span>
          </Button>
        </div>
      </div>

      <div className="w-full max-w-7xl space-y-9 pt-8 m-auto">
        {systemInfo ? (
          <div className="space-y-4">
            <div className="p-5 rounded-md bg-card border border-border space-y-3 shadow-sm">
              <div className="flex items-center gap-2">
                <Cpu size={15} className="text-primary" />
                <span className="text-xs font-semibold text-foreground uppercase tracking-wider">
                  Host Hardware Telemetry
                </span>
              </div>

              <div className="p-3.5 rounded-md bg-background border border-border/60 space-y-2.5 text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">Processor (CPU):</span>
                  <span className="text-foreground font-semibold font-mono">{systemInfo.os?.cpu_brand || "Unknown CPU"}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">Physical Cores / Threads:</span>
                  <span className="text-foreground font-semibold font-mono">{systemInfo.os?.cores ?? "N/A"} Cores</span>
                </div>
                <div className="space-y-1 pt-1 border-t border-border/60">
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">System Memory (RAM):</span>
                    <span className="text-foreground font-semibold font-mono">
                      {systemInfo.os?.memory?.available_gb} GB free / {systemInfo.os?.memory?.total_gb} GB total ({systemInfo.os?.memory?.usage_percent}% load)
                    </span>
                  </div>
                  <div className="w-full bg-card rounded-full h-1.5 overflow-hidden mt-1">
                    <div
                      className="bg-peldrun-accent h-full transition-all duration-300"
                      style={{ width: `${systemInfo.os?.memory?.usage_percent || 0}%` }}
                    />
                  </div>
                </div>
              </div>
            </div>

            <div className="p-5 rounded-md bg-card border border-border space-y-3 shadow-sm">
              <div className="flex items-center gap-2">
                <Activity size={15} className="text-amber-500" />
                <span className="text-xs font-semibold text-foreground uppercase tracking-wider">
                  Ecosystem & Repositories
                </span>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs font-mono">
                <div className="p-3.5 rounded-md bg-background border border-border/60 space-y-1">
                  <span className="text-[11px] text-muted-foreground font-bold block font-sans">peldrun Core Engine</span>
                  <div>Commit: <span className="font-bold text-emerald-500">{systemInfo.repositories?.peldrun?.commit || "Detected"}</span></div>
                </div>
                <div className="p-3.5 rounded-md bg-background border border-border/60 space-y-1">
                  <span className="text-[11px] text-muted-foreground font-bold block font-sans">peldrun Web (PWA Frontend)</span>
                  <div>Commit: <span className="font-bold text-primary">{systemInfo.repositories?.peldrun_web?.commit || "Local"}</span></div>
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="p-8 text-center text-xs text-muted-foreground bg-card border border-border rounded-md shadow-sm">
            Loading hardware telemetry...
          </div>
        )}

        {/* Backend connectivity diagnostic - shows live health of the
            active API endpoint. Displays the URL configured at build
            time and confirms which backend actually answered. */}
        <BackendStatusCard />
        <VersionCard />
      </div>
    </div>
  );
}
