// frontend/src/app/stores/_modals/CatalogSkillDetailsModal.tsx
"use client";

import React, { useEffect } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  ExternalLink,
  Eye,
  Loader2,
  ShieldCheck,
  Sparkles,
  Tag,
  X,
} from "lucide-react";
import type {
  SkillCatalogInstallState,
  SkillCatalogItem,
} from "../_types/store";

/**
 * Details modal for a catalog item.
 *
 * Renders ALL data directly from ``SkillCatalogItem`` (no fetch).
 * Catalog items are pure metadata -- unlike an installed skill, they
 * have no on-disk instructions to preview, no sub-skills endpoint to
 * query, and no icon endpoint to hit. That is why this is a separate
 * modal from ``SkillDetailsDrawer`` (which is specialized for the
 * *installed* skill lifecycle and would 404 on a catalog-only item).
 *
 * The install action, when clicked, is expected to:
 *   1. Close this modal (caller's responsibility in ``onInstall``).
 *   2. Open ``CatalogSkillInstallModal`` (also caller's responsibility).
 *
 * Both are handled in ``page.tsx`` -- this modal is presentation-only.
 */
export interface CatalogSkillDetailsModalProps {
  /** Whether the modal is visible. */
  open: boolean;

  /** The catalog item being viewed, or ``null``. */
  item: SkillCatalogItem | null;

  /** Derived install state (matching by ``id`` against installed skills). */
  installState: SkillCatalogInstallState;

  /**
   * True while this item is being installed. Disables the install
   * button and the close button so a mid-install dismiss cannot
   * orphan the modal's state.
   */
  installing: boolean;

  /**
   * Called when the user clicks Install / Update. The caller is
   * expected to close this modal and trigger the install flow.
   */
  onInstall: () => void;

  /** Called when the user dismisses the modal. */
  onClose: () => void;
}

export function CatalogSkillDetailsModal({
  open,
  item,
  installState,
  installing,
  onInstall,
  onClose,
}: CatalogSkillDetailsModalProps) {
  // ESC closes unless an install is in progress.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !installing) onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, installing, onClose]);

  if (!open || !item) return null;

  const displayName = item.display_name || item.name || item.id;
  const isPreviewOnly = !item.installable;
  const hasVersionMismatch =
    installState.isInstalled &&
    installState.installedVersion !== null &&
    installState.installedVersion !== item.version;

  // ── Install button state machine ─────────────────────────────
  //
  // Four mutually-exclusive states drive the footer action button.
  // Matches the logic in ``CatalogSkillCard`` so the two surfaces
  // never disagree about what "install" means for a given item.
  const installButton = (() => {
    if (installing) {
      return (
        <>
          <Loader2 size={13} className="animate-spin" />
          Installing…
        </>
      );
    }
    if (installState.isInstalled) {
      if (hasVersionMismatch) {
        return (
          <>
            <Download size={13} />
            Update to latest
          </>
        );
      }
      return (
        <>
          <CheckCircle2 size={13} />
          Already installed
        </>
      );
    }
    if (!item.installable) {
      return (
        <>
          <Eye size={13} />
          Preview only
        </>
      );
    }
    return (
      <>
        <Download size={13} />
        Install Skill
      </>
    );
  })();

  // The install button is only clickable when there is something to
  // install: not already at the target version, not preview-only,
  // and not mid-install.
  const canInstall =
    item.installable &&
    !installing &&
    !(installState.isInstalled && !hasVersionMismatch);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !installing) onClose();
      }}
    >
      <div className="bg-card rounded-md shadow-2xl w-full max-w-2xl max-h-[90vh] flex flex-col border border-border">
        {/* ── Header ─────────────────────────────────────────── */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0 bg-muted/40">
          <div className="flex items-center gap-3 min-w-0">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary shrink-0">
              <Sparkles size={16} />
            </div>
            <div className="min-w-0">
              <h2
                className="text-base font-bold text-foreground truncate"
                title={displayName}
              >
                {displayName}
              </h2>
              <p className="text-xs text-muted-foreground font-mono">
                {item.id} · v{item.version}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={installing}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground disabled:opacity-50 cursor-pointer"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* ── Body ───────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto px-6 py-5 scrollbar-thin space-y-5">
          {/* Description */}
          <section>
            <h3 className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground mb-2">
              Description
            </h3>
            <p className="text-sm text-foreground leading-relaxed whitespace-pre-wrap">
              {item.description?.trim() || "No description provided."}
            </p>
          </section>

          {/* Status / identity badges */}
          <div className="flex flex-wrap items-center gap-1.5">
            {item.trust === "verified" && (
              <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
                <ShieldCheck size={11} />
                Verified
              </span>
            )}
            {item.has_sub_skills && (
              <span className="inline-flex items-center rounded-full bg-violet-500/10 border border-violet-500/20 px-2 py-0.5 text-[11px] font-medium text-violet-700 dark:text-violet-300">
                Router · {item.sub_skills_count} sub-skills
              </span>
            )}
            {item.stability !== "stable" && (
              <span className="inline-flex items-center rounded-full bg-amber-500/10 border border-amber-500/20 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                {item.stability}
              </span>
            )}
            {isPreviewOnly && (
              <span className="inline-flex items-center rounded-full bg-muted border border-border px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                Preview only
              </span>
            )}
            {installState.isInstalled && (
              <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
                <CheckCircle2 size={11} />
                Installed v{installState.installedVersion}
              </span>
            )}
            {hasVersionMismatch && (
              <span className="inline-flex items-center rounded-full bg-blue-500/10 border border-blue-500/20 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:text-blue-300">
                Update available
              </span>
            )}
          </div>

          {/* ALL install warnings (not just the first) */}
          {item.install_warnings.length > 0 && (
            <section>
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground mb-2">
                Install warnings
              </h3>
              <div className="space-y-1.5">
                {item.install_warnings.map((w, i) => (
                  <div
                    key={i}
                    className="flex items-start gap-2 p-2.5 rounded-md border border-amber-500/30 bg-amber-500/5"
                  >
                    <AlertTriangle
                      size={12}
                      className="text-amber-600 dark:text-amber-400 shrink-0 mt-0.5"
                    />
                    <p className="text-[11px] text-amber-900 dark:text-amber-200 leading-relaxed">
                      {w}
                    </p>
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* Tags */}
          {item.tags.length > 0 && (
            <section>
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground mb-2">
                Tags
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {item.tags.map((tag, i) => (
                  <span
                    key={`${tag}-${i}`}
                    className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-mono text-muted-foreground bg-muted border border-border"
                  >
                    <Tag size={9} />
                    {tag}
                  </span>
                ))}
              </div>
            </section>
          )}

          {/* Publisher */}
          {(item.developer?.name || item.author) && (
            <section>
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground mb-2">
                Publisher
              </h3>
              <p className="text-xs text-foreground inline-flex items-center gap-1">
                {item.developer?.name || item.author}
                {item.developer?.verified && (
                  <ShieldCheck
                    size={11}
                    className="text-emerald-500"
                    aria-label="Verified publisher"
                  />
                )}
              </p>
            </section>
          )}

          {/* Links */}
          {(item.homepage || item.docs_url || item.repository_url) && (
            <section>
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground mb-2">
                Links
              </h3>
              <div className="flex flex-col gap-1.5">
                {item.homepage && (
                  <a
                    href={item.homepage}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-xs text-primary hover:underline inline-flex items-center gap-1"
                  >
                    Homepage <ExternalLink size={11} />
                  </a>
                )}
                {item.docs_url && (
                  <a
                    href={item.docs_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-xs text-primary hover:underline inline-flex items-center gap-1"
                  >
                    Documentation <ExternalLink size={11} />
                  </a>
                )}
                {item.repository_url && (
                  <a
                    href={item.repository_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-xs text-primary hover:underline inline-flex items-center gap-1"
                  >
                    Source repository <ExternalLink size={11} />
                  </a>
                )}
              </div>
            </section>
          )}

          {/* Installability reason — explains why Preview-only items
              cannot be installed. Rendered verbatim from the backend. */}
          <section>
            <h3 className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground mb-2">
              Install status
            </h3>
            <p className="text-xs text-muted-foreground">
              {item.installability_reason}
            </p>
          </section>
        </div>

        {/* ── Footer ─────────────────────────────────────────── */}
        <div className="px-6 py-3 border-t border-border flex items-center justify-between gap-2 shrink-0 bg-muted/20">
          <span className="text-[10px] text-muted-foreground font-mono">
            {item.license || "No license declared"}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={installing}
              className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50 cursor-pointer"
            >
              Close
            </button>
            <button
              type="button"
              onClick={onInstall}
              disabled={!canInstall}
              className={[
                "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                canInstall
                  ? "bg-primary text-primary-foreground hover:bg-primary/90 cursor-pointer"
                  : "cursor-not-allowed bg-muted text-muted-foreground",
              ].join(" ")}
              title={item.installability_reason}
            >
              {installButton}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default CatalogSkillDetailsModal;
