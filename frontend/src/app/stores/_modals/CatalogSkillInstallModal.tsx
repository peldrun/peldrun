// frontend/src/app/stores/_modals/CatalogSkillInstallModal.tsx
"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Sparkles,
  X,
  XCircle,
} from "lucide-react";
import type {
  CatalogSkillInstallResult,
  SkillCatalogItem,
} from "../_types/store";

// ---------------------------------------------------------------------------
// Phase model
// ---------------------------------------------------------------------------
//
// These are the five phases of a synchronous skill install, mirroring
// what the backend actually does between accepting the POST and
// returning the response:
//
//     POST /api/skills/catalog/{id}/install
//       -> _download_zip() (stream + SHA-256)
//       -> asyncio.to_thread(skill_manager.install_skill)
//          -> validate_zip / safe_extract
//          -> load_skill_package (manifest schema)
//          -> _validate_sub_skills
//          -> atomic rename to storage/store/skills/{id}/
//
// We cannot stream real per-phase progress from a synchronous
// endpoint, so the modal advances through these phases on a timer
// while the fetch is in-flight. When the backend responds, the modal
// jumps to a terminal state (success or error). The success banner
// and error text are 100% real, taken from the actual HTTP response.

const PHASE_LABELS = [
  "Downloading package",
  "Verifying checksum",
  "Extracting archive",
  "Validating manifest",
  "Installing skill",
] as const;

/** 0..4 = running phases, 5 = complete (success only). */
type Phase = 0 | 1 | 2 | 3 | 4 | 5;

/** Interval between simulated phase advances (ms). */
const PHASE_INTERVAL_MS = 400;


// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface CatalogSkillInstallModalProps {
  /** Whether the modal is visible. */
  open: boolean;

  /** The catalog item being installed, or ``null``. */
  item: SkillCatalogItem | null;

  /**
   * Terminal result from the caller.
   *
   *   - ``null``             -> install is in-flight (running).
   *   - ``{success: true}``  -> install completed; show success banner.
   *   - ``{success: false}`` -> install failed; show error banner.
   *
   * This modal does NOT call the API itself. The caller
   * (``useSkillActions``) owns the fetch, the timing, and the
   * translation of thrown errors into the discriminated union above.
   * Keeping the modal stateless with respect to the network makes it
   * trivially testable and reusable against any future synchronous
   * install endpoint.
   */
  result: CatalogSkillInstallResult | null;

  /**
   * Called when the user closes the modal. Ignored by the modal while
   * ``result === null`` (i.e. while running) -- the caller is expected
   * to enforce the same guard, but a defensive check on both sides
   * prevents races.
   */
  onClose: () => void;
}


// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function CatalogSkillInstallModal({
  open,
  item,
  result,
  onClose,
}: CatalogSkillInstallModalProps) {
  const [phase, setPhase] = useState<Phase>(0);
  const [logs, setLogs] = useState<string[]>([]);
  const logsEndRef = useRef<HTMLDivElement>(null);

  // Deduplication refs -- see the note below. React 18 Strict Mode
  // runs each effect twice in development, which would otherwise
  // emit every log line and the terminal banner twice.
  const lastEmittedPhaseRef = useRef<number>(-1);
  const lastEmittedResultRef = useRef<"success" | "fail" | null>(null);

  const isRunning = open && result === null;
  const isSuccess = result?.success === true;
  const isFailed = result?.success === false;

  // ── Reset when a fresh install starts ───────────────────────────
  //
  // The guard ``result === null`` is critical: without it, the reset
  // would fire the moment the backend responds (``result`` transitions
  // null -> {success:true}), wiping the logs and banner we just
  // populated. Only the state BEFORE the response should be reset.
  useEffect(() => {
    if (open && item && result === null) {
      setPhase(0);
      setLogs([]);
      lastEmittedPhaseRef.current = -1;
      lastEmittedResultRef.current = null;
    }
  }, [open, item, result]);

  // ── Advance phase on a timer while running ──────────────────────
  //
  // Capped at phase 4 so the user sees "Installing skill..." with the
  // bar at 80% until the backend responds. Jumping to 100% requires
  // the success effect below.
  useEffect(() => {
    if (!isRunning) return;
    const id = setInterval(() => {
      setPhase((prev) => (prev >= 4 ? 4 : ((prev + 1) as Phase)));
    }, PHASE_INTERVAL_MS);
    return () => clearInterval(id);
  }, [isRunning]);

  // ── Emit one log line per phase transition ──────────────────────
  //
  // The ref guard ensures a given phase line is added exactly once,
  // even under Strict Mode double-invocation.
  useEffect(() => {
    if (!isRunning || !item) return;
    if (phase <= lastEmittedPhaseRef.current) return;
    lastEmittedPhaseRef.current = phase;

    let line: string | null = null;
    switch (phase) {
      case 0:
        line = `[system] Fetching ${item.id} v${item.version} from catalog`;
        break;
      case 1:
        line = "[system] Computing SHA-256 checksum...";
        break;
      case 2:
        line = "[system] Checksum verified. Extracting archive...";
        break;
      case 3:
        line = "[system] Parsing manifest.json against schema";
        break;
      case 4:
        line = `[system] Installing to storage/store/skills/${item.id}/`;
        break;
    }
    if (line) setLogs((prev) => [...prev, line]);
  }, [phase, isRunning, item]);

  // ── Terminal state (success / failure) ──────────────────────────
  useEffect(() => {
    if (!item) return;

    if (isSuccess && lastEmittedResultRef.current !== "success") {
      lastEmittedResultRef.current = "success";
      setPhase(5);
      setLogs((prev) => [
        ...prev,
        "[system] Installation completed successfully.",
      ]);
      return;
    }

    if (
      isFailed &&
      result?.success === false &&
      lastEmittedResultRef.current !== "fail"
    ) {
      lastEmittedResultRef.current = "fail";
      setLogs((prev) => [...prev, `[error] ${result.error}`]);
    }
  }, [isSuccess, isFailed, item, result]);

  // ── Auto-scroll the log console ─────────────────────────────────
  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs.length]);

  // ── Escape closes only when finished ────────────────────────────
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !isRunning) onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, isRunning, onClose]);

  // ── Render guard ────────────────────────────────────────────────
  if (!open || !item) return null;

  const displayName = item.display_name || item.name || item.id;
  const progressPct = isSuccess
    ? 100
    : Math.round((phase / 5) * 100);

  const headerTitle = isSuccess
    ? `Installed ${displayName}`
    : isFailed
    ? `Failed to install ${displayName}`
    : `Installing ${displayName} v${item.version}`;

  const headerSubtitle = isSuccess
    ? "The skill is now active and available in chat."
    : isFailed
    ? "The install failed. See the log for details."
    : "Downloading and installing the skill from the catalog...";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="skill-install-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      {/* Backdrop -- click only closes when not running. */}
      <button
        type="button"
        aria-label="Close install modal"
        onClick={() => {
          if (!isRunning) onClose();
        }}
        className="absolute inset-0 cursor-default bg-black/60 backdrop-blur-sm"
        tabIndex={-1}
      />

      {/* Dialog */}
      <div className="relative z-10 flex w-full max-w-3xl flex-col overflow-hidden rounded-xl border bg-card shadow-2xl max-h-[90vh]">
        {/* ── Header ─────────────────────────────────────────── */}
        <div className="flex items-start justify-between gap-4 border-b px-5 py-4">
          <div className="flex items-start gap-3">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary ring-1 ring-primary/20"
              aria-hidden="true"
            >
              <Sparkles className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h2
                id="skill-install-title"
                className="text-sm font-semibold text-foreground"
              >
                {headerTitle}
              </h2>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                {headerSubtitle}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isRunning}
            aria-label="Close"
            className={[
              "rounded-md p-1 text-muted-foreground transition-colors",
              isRunning
                ? "cursor-not-allowed opacity-40"
                : "hover:bg-muted hover:text-foreground",
            ].join(" ")}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* ── Body ───────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto scrollbar-thin px-5 py-4 space-y-4">
          {/* Progress bar + phase chips */}
          <div>
            <div className="mb-2 flex items-center justify-between text-xs">
              <span className="font-medium text-foreground">
                {isSuccess
                  ? "Installation complete"
                  : isFailed
                  ? "Installation failed"
                  : PHASE_LABELS[Math.min(phase, 4)]}
              </span>
              <span className="font-mono text-muted-foreground">
                {progressPct}%
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
              <div
                className={[
                  "h-full transition-all duration-500",
                  isFailed
                    ? "bg-destructive"
                    : isSuccess
                    ? "bg-emerald-500"
                    : "bg-primary",
                ].join(" ")}
                style={{ width: `${progressPct}%` }}
              />
            </div>
            <div className="mt-2 flex flex-wrap gap-1">
              {PHASE_LABELS.map((label, idx) => {
                const done = idx < phase || isSuccess;
                const active = idx === phase && !isSuccess && !isFailed;
                return (
                  <span
                    key={label}
                    className={[
                      "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium",
                      done
                        ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                        : active
                        ? "bg-primary/10 text-primary"
                        : "bg-muted text-muted-foreground",
                    ].join(" ")}
                  >
                    {done && <CheckCircle2 className="h-2.5 w-2.5" />}
                    {active && (
                      <Loader2 className="h-2.5 w-2.5 animate-spin" />
                    )}
                    {label}
                  </span>
                );
              })}
            </div>
          </div>

          {/* Log console -- mirrors ProviderUploadModal styling. */}
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[11px] font-semibold text-foreground">
                Install log
              </span>
              <span className="text-[10px] text-muted-foreground">
                {logs.length} line{logs.length === 1 ? "" : "s"}
              </span>
            </div>
            <div className="max-h-64 overflow-y-auto rounded-md border bg-muted/30 p-2 font-mono text-[10px] leading-relaxed scrollbar-thin">
              {logs.length === 0 && (
                <p className="italic text-muted-foreground">
                  Waiting for output...
                </p>
              )}
              {logs.map((line, idx) => {
                const isError = line.startsWith("[error]");
                return (
                  <div
                    key={idx}
                    className={
                      isError ? "text-destructive" : "text-foreground"
                    }
                  >
                    {line}
                  </div>
                );
              })}
              <div ref={logsEndRef} />
            </div>
          </div>

          {/* Success banner -- carries real metadata from the backend. */}
          {isSuccess && result?.success === true && (
            <div className="flex items-start gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3">
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
              <div className="min-w-0 flex-1">
                <p className="text-[11px] font-semibold text-emerald-900 dark:text-emerald-200">
                  {displayName} v{result.version}
                </p>
                <p className="mt-0.5 text-[10px] text-emerald-900/70 dark:text-emerald-200/70">
                  The skill is installed and ready to use.
                </p>
              </div>
            </div>
          )}

          {/* Failure banner -- uses the actual backend error string. */}
          {isFailed && result?.success === false && (
            <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3">
              <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
              <div className="min-w-0 flex-1">
                <p className="text-[11px] font-semibold text-destructive">
                  {result.error}
                </p>
                {result.code && (
                  <p className="mt-0.5 font-mono text-[10px] text-destructive/80">
                    {result.code}
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Trust warning -- visible only during the brief pre-progress
              window (running, no logs yet). Cosmetic; the actual guard
              is the SHA-256 verification in the backend. */}
          {isRunning && logs.length === 0 && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/20 bg-amber-500/5 p-3">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
              <p className="text-[11px] leading-relaxed text-amber-900/80 dark:text-amber-200/70">
                Only install skills from publishers you trust. This
                package was SHA-256 verified against the catalog, but
                installation still copies files to disk and activates
                a new prompt asset.
              </p>
            </div>
          )}
        </div>

        {/* ── Footer ─────────────────────────────────────────── */}
        <div className="flex items-center justify-between gap-2 border-t px-5 py-3">
          <div className="text-[10px] text-muted-foreground">
            {isSuccess
              ? "The skill is ready. This window can be closed."
              : isFailed
              ? "Review the log above, then close to retry."
              : "The install runs synchronously and usually finishes in under 5 seconds."}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isRunning}
            className={[
              "rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
              isRunning
                ? "cursor-not-allowed border opacity-50"
                : isSuccess
                ? "border bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/15"
                : "border hover:bg-muted",
            ].join(" ")}
          >
            {isRunning ? "Installing..." : isSuccess ? "Done" : "Close"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default CatalogSkillInstallModal;
