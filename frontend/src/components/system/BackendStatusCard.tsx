// frontend/src/components/system/BackendStatusCard.tsx

"use client";

import { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  XCircle,
  Loader2,
  RefreshCw,
  Server,
} from "lucide-react";

/**
 * Diagnostic card showing which backend instance the browser is
 * actually talking to.
 *
 * Two data sources are combined:
 *   1. NEXT_PUBLIC_PELDRUN_BACKEND_URL - URL configured at build
 *      time in next.config.mjs.
 *   2. /api/system/health - runtime response from the backend
 *      that answered the request.
 */

interface HealthResponse {
  status: string;
  service: string;
  version: string;
  python_version?: string;
  platform?: string;
  hostname?: string;
  pid?: number;
  started_at?: string;
  server_time?: string;
}

interface InfoResponse {
  status: string;
  service: string;
  version: string;
  host_header?: string;
  client_host?: string;
  server_time?: string;
}

type Probe =
  | { state: "loading" }
  | { state: "ok"; health: HealthResponse; info: InfoResponse }
  | { state: "error"; message: string };

const PROBE_TIMEOUT_MS = 5000;

const CONFIGURED_BACKEND =
  process.env.NEXT_PUBLIC_PELDRUN_BACKEND_URL ?? "(not configured)";

export function BackendStatusCard() {
  const [probe, setProbe] = useState<Probe>({ state: "loading" });

  const runProbe = useCallback(async () => {
    setProbe({ state: "loading" });
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);

    try {
      const [healthRes, infoRes] = await Promise.all([
        fetch("/api/system/health", {
          signal: controller.signal,
          cache: "no-store",
        }),
        fetch("/api/system/info", {
          signal: controller.signal,
          cache: "no-store",
        }),
      ]);

      if (!healthRes.ok || !infoRes.ok) {
        throw new Error(
          `Backend returned HTTP ${healthRes.status}/${infoRes.status}`
        );
      }

      const health = (await healthRes.json()) as HealthResponse;
      const info = (await infoRes.json()) as InfoResponse;
      setProbe({ state: "ok", health, info });
    } catch (err: any) {
      const message =
        err?.name === "AbortError"
          ? `Probe timed out after ${PROBE_TIMEOUT_MS / 1000}s`
          : err?.message || "Cannot reach backend";
      setProbe({ state: "error", message });
    } finally {
      clearTimeout(timer);
    }
  }, []);

  useEffect(() => {
    runProbe();
  }, [runProbe]);

  const isOk = probe.state === "ok";
  const isLoading = probe.state === "loading";

  return (
    <div className="rounded-lg border border-border bg-card shadow-xs">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2.5">
          <div className="p-1.5 rounded-md bg-primary/10 text-primary">
            <Server size={14} />
          </div>
          <div>
            <h3 className="text-sm font-semibold text-foreground">
              Backend Connection
            </h3>
            <p className="text-[11px] text-muted-foreground">
              Live diagnostic of the API endpoint this browser is using
            </p>
          </div>
        </div>

        <button
          type="button"
          onClick={runProbe}
          disabled={isLoading}
          className="p-1.5 rounded-md border border-border bg-muted/50 hover:bg-muted text-foreground transition cursor-pointer disabled:opacity-50"
          title="Re-run probe"
        >
          <RefreshCw size={12} className={isLoading ? "animate-spin" : ""} />
        </button>
      </div>

      <div className="p-4 space-y-3 text-xs">
        {isLoading ? (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 size={14} className="animate-spin" />
            <span>Probing backend...</span>
          </div>
        ) : isOk ? (
          <div className="flex items-center gap-2 text-emerald-600 dark:text-emerald-400">
            <CheckCircle2 size={14} />
            <span className="font-medium">Backend reachable and healthy</span>
          </div>
        ) : (
          <div className="flex items-center gap-2 text-destructive">
            <XCircle size={14} />
            <span className="font-medium">
              {probe.state === "error" ? probe.message : "Unknown state"}
            </span>
          </div>
        )}

        <Row label="Configured target" value={CONFIGURED_BACKEND} mono />

        {isOk && probe.state === "ok" && (
          <>
            <Row
              label="Service"
              value={`${probe.health.service} v${probe.health.version}`}
            />
            {probe.health.hostname && (
              <Row
                label="Hostname / PID"
                value={`${probe.health.hostname} (pid ${probe.health.pid})`}
                mono
              />
            )}
            {probe.health.server_time && (
              <Row
                label="Server time"
                value={new Date(probe.health.server_time).toLocaleString()}
              />
            )}
            {probe.info.host_header && (
              <Row
                label="Host header received"
                value={probe.info.host_header}
                mono
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

interface RowProps {
  label: string;
  value: string;
  mono?: boolean;
}

function Row({ label, value, mono }: RowProps) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span
        className={`text-foreground text-right break-all ${
          mono ? "font-mono text-[11px]" : ""
        }`}
      >
        {value}
      </span>
    </div>
  );
}
