"use client";

import React, { useState } from "react";
import {
  ChevronDown,
  FileArchive,
  Info,
  Layers,
  Shield,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { BundleEntry } from "@/lib/bundleApi";

export interface BundleCardProps {
  bundle: BundleEntry;
  onActivate: (version: string) => void;
  onDelete: () => void;
  onViewDetails: () => void;
}

export function BundleCard({
  bundle,
  onActivate,
  onDelete,
  onViewDetails,
}: BundleCardProps) {
  const [versionMenuOpen, setVersionMenuOpen] = useState(false);

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const activeRecord = bundle.installed_versions.find(
    (v) => v.version === bundle.active_version
  );

  const isMultiVersion = bundle.installed_versions.length > 1;

  return (
    <div className="p-5 rounded-xl border bg-card shadow-xs hover:shadow-md transition-all flex flex-col justify-between border-border">
      <div>
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="p-2 rounded-lg bg-primary/10 text-primary border border-primary/20 shrink-0">
              <FileArchive size={15} />
            </div>
            <div className="min-w-0">
              <h3 className="text-sm font-bold text-foreground truncate" title={bundle.name}>
                {bundle.name}
              </h3>
              <span className="text-[10px] font-mono text-muted-foreground truncate block">
                {bundle.id}
              </span>
            </div>
          </div>

          {/* Version chip with rollback menu */}
          <div className="relative shrink-0">
            <button
              type="button"
              onClick={() => isMultiVersion && setVersionMenuOpen((v) => !v)}
              className={`flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-mono border transition ${
                isMultiVersion
                  ? "bg-primary/10 text-primary border-primary/30 cursor-pointer hover:bg-primary/20"
                  : "bg-muted text-muted-foreground border-border cursor-default"
              }`}
              title={
                isMultiVersion
                  ? "Multiple versions installed — click to switch"
                  : `Active version ${bundle.active_version}`
              }
            >
              <span>v{bundle.active_version}</span>
              {isMultiVersion && (
                <ChevronDown size={10} className={versionMenuOpen ? "rotate-180 transition" : "transition"} />
              )}
            </button>

            {versionMenuOpen && isMultiVersion && (
              <div className="absolute right-0 top-full mt-1 z-20 w-40 rounded-lg border border-border bg-popover shadow-lg p-1 text-[11px]">
                {bundle.installed_versions
                  .slice()
                  .reverse()
                  .map((v) => (
                    <button
                      key={v.version}
                      type="button"
                      onClick={() => {
                        setVersionMenuOpen(false);
                        if (v.version !== bundle.active_version) {
                          onActivate(v.version);
                        }
                      }}
                      className={`w-full flex items-center justify-between px-2 py-1.5 rounded-md font-mono transition cursor-pointer ${
                        v.version === bundle.active_version
                          ? "bg-primary text-primary-foreground font-semibold"
                          : "hover:bg-muted text-foreground"
                      }`}
                    >
                      <span>v{v.version}</span>
                      {v.version === bundle.active_version && (
                        <span className="text-[9px]">active</span>
                      )}
                    </button>
                  ))}
              </div>
            )}
          </div>
        </div>

        {/* Status badges */}
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <span className="px-2 py-0.5 rounded-md text-[10px] font-mono uppercase bg-primary/10 text-primary border border-primary/20">
            BUNDLE
          </span>
          <span
            className={`px-2 py-0.5 rounded-md text-[10px] font-mono uppercase ${
              bundle.status === "active"
                ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30"
                : "bg-muted text-muted-foreground border border-border"
            }`}
          >
            {bundle.status}
          </span>
          {bundle.safety_level !== "safe" && (
            <span className="px-2 py-0.5 rounded-md text-[10px] font-mono uppercase bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/30">
              {bundle.safety_level}
            </span>
          )}
        </div>

        {/* Description */}
        <p className="text-xs text-muted-foreground line-clamp-2 mb-3 leading-relaxed">
          {bundle.description || "Installed bundle."}
        </p>

        {/* Hash + size */}
        {activeRecord && (
          <div className="flex items-center gap-2 text-[10px] font-mono text-muted-foreground mb-3">
            <span className="px-1.5 py-0.5 rounded bg-muted border border-border truncate">
              {bundle.active_hash}
            </span>
            <span>·</span>
            <span>{formatSize(activeRecord.size_bytes)}</span>
            <span>·</span>
            <span>{activeRecord.file_count} files</span>
          </div>
        )}

        {/* Capabilities */}
        {bundle.capabilities.length > 0 && (
          <div className="mb-3">
            <span className="text-[10px] uppercase font-semibold text-muted-foreground tracking-wider block mb-1.5">
              Capabilities
            </span>
            <div className="flex flex-wrap gap-1">
              {bundle.capabilities.map((cap) => (
                <span
                  key={cap}
                  className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-muted text-muted-foreground border border-border"
                >
                  {cap}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="pt-3 border-t border-border flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-[11px] font-mono text-muted-foreground">
          <Shield size={11} />
          <span>{bundle.installed_versions.length} version{bundle.installed_versions.length !== 1 ? "s" : ""}</span>
        </div>

        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onViewDetails}
            className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
            title="View details"
          >
            <Info size={13} />
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="p-1.5 rounded-lg hover:bg-destructive/10 text-muted-foreground hover:text-destructive transition cursor-pointer"
            title="Delete bundle"
          >
            <Trash2 size={13} />
          </button>
        </div>
      </div>
    </div>
  );
}

export default BundleCard;
