// frontend/src/components/system/VersionCard.tsx

"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Package,
  RefreshCw,
  Loader2,
  AlertTriangle,
  CheckCircle2,
} from "lucide-react";

/**
 * Displays the app version and every component version.
 *
 * Fetches from `/api/system/version`, which reads the four
 * VERSION.json files at request time. No build-time pinning, so
 * bumping a version and restarting the backend is enough to make
 * the new value visible here.
 */

interface VersionInfo {
  app: {
    version: string;
    released_at: string | null;
    released_commit: string | null;
  };
  components: Record<
    string,
    { version: string | null; released_commit: string | null }
  >;
  all_in_sync: boolean;
  source: string;
}

type Probe =
  | { state: "loading" }
  | { state: "ok"; info: VersionInfo }
  | { state: "error"; message: string };

const PROBE_TIMEOUT_MS = 5000;

export function VersionCard() {
  const [probe, setProbe] = useState<Probe>({ state: "loading" });

  const run = useCallback(async () => {
    setProbe({ state: "loading" });
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
    try {
      const res = await fetch("/api/system/version", {
        signal: controller.signal,
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const info = (await res.json()) as VersionInfo;
      setProbe({ state: "ok", info });
    } catch (err: any) {
      const message =
        err?.name === "AbortError"
          ? `Probe timed out after ${PROBE_TIMEOUT_MS / 1000}s`
          : err?.message || "Cannot read version info";
      setProbe({ state: "error", message });
    } finally {
      clearTimeout(timer);
    }
  }, []);

  useEffect(() => {
    run();
  }, [run]);

  const isLoading = probe.state === "loading";
  const isOk = probe.state === "ok";

  return (
    <div className="rounded-lg border border-border bg-card shadow-xs">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2.5">
          <div className="p-1.5 rounded-md bg-primary/10 text-primary">
            <Package size={14} />
          </div>
          <div>
            <h3 className="text-sm font-semibold text-foreground">
              Version Information
            </h3>
            <p className="text-[11px] text-muted-foreground">
              App and component versions (from VERSION.json files)
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={run}
          disabled={isLoading}
          className="p-1.5 rounded-md border border-border bg-muted/50 hover:bg-muted text-foreground transition cursor-pointer disabled:opacity-50"
          title="Refresh version info"
        >
          <RefreshCw size={12} className={isLoading ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Body */}
      <div className="p-4 space-y-3 text-xs">
        {isLoading && (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 size={14} className="animate-spin" />
            <span>Reading version info...</span>
          </div>
        )}

        {probe.state === "error" && (
          <div className="flex items-center gap-2 text-destructive">
            <AlertTriangle size={14} />
            <span>{probe.message}</span>
          </div>
        )}

        {isOk && probe.state === "ok" && (
          <>
            {/* App version banner */}
            <div className="flex items-center justify-between rounded-md border border-primary/30 bg-primary/5 px-3 py-2">
              <span className="text-xs font-semibold text-foreground">
                Application
              </span>
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm font-bold text-primary">
                  v{probe.info.app.version}
                </span>
                {probe.info.all_in_sync ? (
                  <CheckCircle2
                    size={12}
                    className="text-emerald-500"
                    aria-label="All component versions in sync"
                  />
                ) : (
                  <AlertTriangle
                    size={12}
                    className="text-amber-500"
                    aria-label="Component versions out of sync"
                  />
                )}
              </div>
            </div>

            {/* Components table */}
            <div className="space-y-1.5">
              <div className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground pb-1">
                Components
              </div>
              {Object.entries(probe.info.components).map(
                ([name, data]) => (
                  <div
                    key={name}
                    className="flex items-center justify-between gap-3 py-1.5 border-b border-border/40 last:border-0"
                  >
                    <span className="font-mono text-foreground">{name}</span>
                    <div className="flex items-center gap-2">
                      {data.version ? (
                        <span className="font-mono text-[11px] text-foreground">
                          v{data.version}
                        </span>
                      ) : (
                        <span className="font-mono text-[11px] text-amber-500">
                          missing
                        </span>
                      )}
                      {data.released_commit && (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          @{data.released_commit}
                        </span>
                      )}
                    </div>
                  </div>
                )
              )}
            </div>

            {/* Footer metadata */}
            <div className="pt-2 border-t border-border/40 text-[10px] text-muted-foreground font-mono space-y-0.5">
              {probe.info.app.released_at && (
                <div>
                  Released:{" "}
                  {new Date(probe.info.app.released_at).toLocaleString()}
                </div>
              )}
              {probe.info.app.released_commit && (
                <div>Commit: {probe.info.app.released_commit}</div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
