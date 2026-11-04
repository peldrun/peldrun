// frontend/src/app/settings/setup/tabs/llm/components/PackageCard.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import {
  AlertCircle,
  CheckCircle2,
  Package as PackageIcon,
  RefreshCw,
  Star,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/Icon/Icon";
import type { ProviderPackageCard } from "../hooks/useProviderPackages";

/* -------------------------------------------------------------------------- */
/* Props                                                                      */
/* -------------------------------------------------------------------------- */

export interface PackageCardProps {
  package: ProviderPackageCard;
  isPrimary: boolean;
  isScanning: boolean;
  onScanModels: () => void;
  onSetPrimary: () => void;
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * One card per installed Provider Package (GigaChat, future packages).
 *
 * Lifecycle:
 *   1. User installs package in Store → Providers.
 *   2. User creates a connection in Store → Providers → Configure.
 *   3. Card appears here automatically (via
 *      `omweb:provider-package-updated` event + initial fetch).
 *   4. User clicks "Scan Models" → discovers models via the package's
 *      adapter → user picks which to expose.
 *   5. User clicks "Set Primary" → chat routes through this package.
 *
 * The card is intentionally read-only for URL / API key. Those live
 * in the connection and are resolved by the backend dispatcher. The
 * only thing this card controls is which models are exposed and
 * whether the package is the active engine.
 */
export function PackageCard({
  package: pkg,
  isPrimary,
  isScanning,
  onScanModels,
  onSetPrimary,
}: PackageCardProps) {
  const hasModels = pkg.savedModels.length > 0;
  const canSetPrimary =
    pkg.hasActiveConnection && hasModels && !isPrimary;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
      whileHover={{ y: -2, transition: { duration: 0.15 } }}
      className="relative p-4 rounded-md border border-card bg-gigbg space-y-3.5 shadow-sm"
    >
      {/* Accent bar for primary state */}
      {isPrimary && (
        <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md bg-gradient-to-r from-emerald-500/60 via-emerald-500 to-emerald-500/60" />
      )}

      {/* ── Header ──────────────────────────────────────────────── */}
      <div className="flex items-center justify-between gap-2 flex-wrap border-b border-border/50 pb-2.5">
        <div className="flex items-center gap-2.5 min-w-0 flex-wrap">
          <span className="shrink-0 flex items-center justify-center text-primary">
            <Icon name={pkg.iconName || "rect"} size={24} title={`${pkg.displayName} logo`} />
          </span>

          <span className="text-xs font-bold text-foreground truncate">
            {pkg.displayName}
          </span>

          <span className="inline-flex items-center gap-1 rounded-sm bg-purple-500/10 text-purple-600 dark:text-purple-300 border border-purple-500/20 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider">
            <PackageIcon size={9} /> Provider Package
          </span>

          {isPrimary && (
            <span className="inline-flex items-center gap-1 text-[10px] font-bold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded-sm border border-emerald-500/30">
              <CheckCircle2 size={11} /> Active Primary
            </span>
          )}

          {!pkg.isEnabled && (
            <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-rose-600 dark:text-rose-400 bg-rose-500/10 px-2 py-0.5 rounded-sm border border-rose-500/20">
              Disabled
            </span>
          )}

          {!pkg.hasActiveConnection && pkg.isEnabled && (
            <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-amber-600 dark:text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded-sm border border-amber-500/20">
              <AlertCircle size={11} /> No active connection
            </span>
          )}
        </div>
      </div>

      {/* ── Description ─────────────────────────────────────────── */}
      {pkg.description && (
        <p className="text-[11px] leading-relaxed text-muted-foreground line-clamp-2">
          {pkg.description}
        </p>
      )}

      {/* ── Connection status ──────────────────────────────────── */}
      <div className="text-[11px] font-mono text-muted-foreground">
        {pkg.hasActiveConnection ? (
          <>
            Connection:{" "}
            <span className="text-foreground font-semibold">
              {pkg.activeConnectionName}
            </span>
          </>
        ) : (
          <>
            Configure a connection in{" "}
            <a
              href="/stores"
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary underline hover:text-primary/80"
            >
              Store → Providers
            </a>{" "}
            first.
          </>
        )}
      </div>

      {/* ── Models summary ─────────────────────────────────────── */}
      {hasModels ? (
        <div className="text-[11px] font-mono">
          <span className="text-muted-foreground">Models: </span>
          <span className="text-foreground font-semibold">
            {pkg.savedModels.length}
          </span>
          <span className="text-muted-foreground">
            {" "}
            ({pkg.savedModels.slice(0, 3).join(", ")}
            {pkg.savedModels.length > 3 ? "…" : ""})
          </span>
        </div>
      ) : (
        <div className="text-[11px] font-mono text-amber-600 dark:text-amber-400">
          No models discovered yet — click "Scan Models".
        </div>
      )}

      {/* ── Actions ────────────────────────────────────────────── */}
      <div className="flex items-center justify-between gap-2 pt-1 flex-wrap">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onScanModels}
          disabled={!pkg.hasActiveConnection || isScanning}
          className="h-7 px-2.5 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100 disabled:opacity-50 flex items-center gap-1.5"
        >
          <RefreshCw
            size={11}
            className={`text-primary ${isScanning ? "animate-spin" : ""}`}
          />
          <span>
            {isScanning
              ? "Scanning…"
              : hasModels
              ? "Rescan Models"
              : "Scan Models"}
          </span>
        </Button>

        <Button
          type="button"
          size="sm"
          onClick={onSetPrimary}
          disabled={!canSetPrimary}
          className="h-7 px-3 text-xs bg-primary text-primary-foreground font-medium cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100 disabled:opacity-50 flex items-center gap-1.5"
        >
          <Star
            size={11}
            className={isPrimary ? "fill-current" : ""}
          />
          <span>{isPrimary ? "Active" : "Set Primary"}</span>
        </Button>
      </div>
    </motion.div>
  );
}
