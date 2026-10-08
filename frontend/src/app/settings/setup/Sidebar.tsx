"use client";

import React from "react";
import Link from "next/link";
import {
  Save,
  RefreshCw,
  Cpu,
  Globe,
  Search,
  Box,
  Share2,
  Info,
  RotateCcw,
  AlertCircle,
  Activity,
  ExternalLink,
  Coins,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { SettingsTab } from "./types";

interface SidebarProps {
  activeTab: SettingsTab;
  setActiveTab: (t: SettingsTab) => void;
  handleSave: () => void;
  handleReset: () => void;
  saving: boolean;
  isDirty: boolean;
  saveStatus: { ok: boolean; message: string } | null;
}

export function Sidebar({
  activeTab,
  setActiveTab,
  handleSave,
  handleReset,
  saving,
  isDirty,
  saveStatus,
}: SidebarProps) {
  const tabs: { id: SettingsTab; label: string; icon: React.ReactNode }[] = [
    { id: "llm", label: "Model Hub [LLM]", icon: <Cpu size={14} /> },
    { id: "usage", label: "Usage & Cost Analytics", icon: <Coins size={14} /> },
    { id: "browser", label: "Browser & CDP", icon: <Globe size={14} /> },
    { id: "search", label: "Search Engine", icon: <Search size={14} /> },
    { id: "sandbox", label: "Docker & Daytona", icon: <Box size={14} /> },
    { id: "mcp", label: "MCP & Agents", icon: <Share2 size={14} /> },
    { id: "system", label: "Diagnostics & HW", icon: <Info size={14} /> },
  ];

  return (
    <div className="w-60 border-r border-card bg-sidebg flex flex-col p-0 pt-4 pb-4 space-y-1 shrink-0 select-none">
      <div className="flex items-center justify-between px-3 py-2">
        <span className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">
          Configuration
        </span>
        {isDirty && (
          <span className="inline-flex items-center gap-1 text-[10px] font-medium text-amber-500 bg-amber-500/10 border border-amber-500/20 px-1.5 py-0.5 rounded">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500 animate-pulse" />
            Unsaved
          </span>
        )}
      </div>

      <div className="space-y-1 flex-1">
        {tabs.map((tab) => {
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`w-full flex items-center justify-between px-3 py-2 rounded-0 text-xs font-medium transition-all cursor-pointer ${
                isActive
                  ? "bg-primary text-primary-foreground shadow-peldrun-xs"
                  : "text-muted-foreground hover:text-foreground hover:bg-muted/60"
              }`}
            >
              <div className="flex items-center gap-2.5">
                {tab.icon}
                <span>{tab.label}</span>
              </div>
            </button>
          );
        })}

        {/* Dedicated Live Monitoring Link */}
        <div className="pt-2.5 mt-2.5 border-t border-border/60">
          <span className="text-[10px] font-semibold text-muted-foreground/70 uppercase tracking-wider px-3 mb-1.5 block">
            Live Monitoring
          </span>
          <Link
            href="/status"
            title="Open real-time system health and runtime diagnostics"
            className="w-full flex items-center justify-between px-3 py-2 rounded-0 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-all cursor-pointer group"
          >
            <div className="flex items-center gap-2.5">
              <Activity size={14} className="text-peldrun-accent group-hover:scale-110 transition-transform" />
              <span className="group-hover:text-foreground transition-colors">System Health</span>
            </div>
            <ExternalLink size={12} className="text-muted-foreground/50 group-hover:text-foreground transition-colors" />
          </Link>
        </div>
      </div>

      <div className="pl-3 pr-3 pt-3 border-t border-border space-y-2">
        <div className="flex items-center gap-2">
          <Button
            onClick={handleSave}
            disabled={saving}
            className="primary flex-1 flex items-center justify-center gap-1.5 h-8 bg-primary text-primary-foreground hover:bg-primary/90 font-medium text-xs rounded-0 shadow-peldrun-xs cursor-pointer hover:opacity-90"
          >
            {saving ? <RefreshCw size={13} className="animate-spin" /> : <Save size={13} />}
            <span>{saving ? "Saving..." : "Save Config"}</span>
          </Button>

          {isDirty && (
            <Button
              onClick={handleReset}
              disabled={saving}
              variant="outline"
              title="Reset changes to loaded configuration"
              className="h-8 px-2.5 border-border bg-background hover:bg-muted text-muted-foreground hover:text-foreground cursor-pointer"
            >
              <RotateCcw size={13} />
            </Button>
          )}
        </div>

        {saveStatus && (
          <div
            className={`p-2 rounded-0 text-[11px] leading-tight border flex items-start gap-1.5 ${
              saveStatus.ok
                ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-600 dark:text-emerald-400"
                : "bg-rose-500/10 border-rose-500/30 text-rose-600 dark:text-rose-400"
            }`}
          >
            <AlertCircle size={13} className="shrink-0 mt-0.5" />
            <span>{saveStatus.message}</span>
          </div>
        )}
      </div>
    </div>
  );
}

export default Sidebar;