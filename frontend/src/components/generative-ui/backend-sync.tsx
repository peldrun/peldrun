// frontend/src/components/generative-ui/backend-sync.tsx
//
// Bridges the local Zustand store with the server-side persistence layer.
//
// Two independent sync loops:
//   1. Rendering settings    (GET/POST /api/config/generative-ui)
//      - Rarely changes, but critical: must match the server on every load.
//   2. Rendering telemetry   (GET/POST /api/config/generative-ui-telemetry)
//      - Changes on every render, so POST is debounced heavily.
//
// Both loops hydrate from the server on mount and push on change. The
// backend is the authoritative source of truth: a fresh browser (no
// localStorage) sees the same counters as the device that rendered the
// content, because the values come from the disk, not from localStorage.
//
// Reset flow:
//   `resetTelemetry()` in the store zeroes the counters, which the sync
//   then POSTs to the backend. Optionally the store can call
//   DELETE /api/config/generative-ui-telemetry, but the POST of the zeroed
//   state is equivalent and simpler (no extra endpoint call).

"use client";

import { useEffect, useRef } from "react";
import { useGenerativeUIStore, getRenderingSettings } from "./store";
import type { RenderingSettings, RenderingTelemetry } from "./types";

const SETTINGS_URL = "/api/config/generative-ui";
const TELEMETRY_URL = "/api/config/generative-ui-telemetry";

const SETTINGS_DEBOUNCE_MS = 400;
const TELEMETRY_DEBOUNCE_MS = 3000;

function settingsEqual(a: RenderingSettings, b: RenderingSettings): boolean {
  return (
    a.enabled === b.enabled &&
    a.max_retries === b.max_retries &&
    a.features.mermaid === b.features.mermaid &&
    a.features.latex === b.features.latex &&
    a.features.charts === b.features.charts &&
    a.features.code_highlighting === b.features.code_highlighting &&
    a.limits.max_mermaid_nodes === b.limits.max_mermaid_nodes &&
    a.limits.max_chart_points === b.limits.max_chart_points &&
    a.limits.max_table_rows === b.limits.max_table_rows &&
    a.limits.max_output_tokens === b.limits.max_output_tokens
  );
}

function telemetryEqual(a: RenderingTelemetry, b: RenderingTelemetry): boolean {
  return (
    a.totalRenders === b.totalRenders &&
    a.successfulRenders === b.successfulRenders &&
    a.mermaidErrors === b.mermaidErrors &&
    a.chartErrors === b.chartErrors &&
    a.latexErrors === b.latexErrors &&
    a.lastRenderLatencyMs === b.lastRenderLatencyMs
  );
}

export function GenerativeUIBackendSync(): null {
  const settingsHydratedRef = useRef(false);
  const telemetryHydratedRef = useRef(false);
  const settingsDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const telemetryDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ---- Phase 1: hydrate settings from backend --------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(SETTINGS_URL, { cache: "no-store" });
        if (!res.ok) return;
        const body = (await res.json()) as {
          ok?: boolean;
          settings?: Partial<RenderingSettings>;
        };
        if (cancelled || !body?.ok || !body.settings) return;
        const s = body.settings;
        useGenerativeUIStore.setState({
          enabled: typeof s.enabled === "boolean" ? s.enabled : undefined,
          features: s.features ?? undefined,
          limits: s.limits ?? undefined,
          max_retries:
            typeof s.max_retries === "number" ? s.max_retries : undefined,
        } as any);
      } catch {
        /* keep local values */
      } finally {
        if (!cancelled) settingsHydratedRef.current = true;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- Phase 2: hydrate telemetry from backend -------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(TELEMETRY_URL, { cache: "no-store" });
        if (!res.ok) return;
        const body = (await res.json()) as {
          ok?: boolean;
          telemetry?: Partial<RenderingTelemetry>;
        };
        if (cancelled || !body?.ok || !body.telemetry) return;
        const t = body.telemetry;
        useGenerativeUIStore.setState({
          telemetry: {
            totalRenders: t.totalRenders ?? 0,
            successfulRenders: t.successfulRenders ?? 0,
            mermaidErrors: t.mermaidErrors ?? 0,
            chartErrors: t.chartErrors ?? 0,
            latexErrors: t.latexErrors ?? 0,
            lastRenderLatencyMs: t.lastRenderLatencyMs ?? 0,
          },
        });
      } catch {
        /* keep local zeroes */
      } finally {
        if (!cancelled) telemetryHydratedRef.current = true;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- Phase 3: push settings changes to backend -----------------------
  useEffect(() => {
    const unsubscribe = useGenerativeUIStore.subscribe((state, prevState) => {
      if (!settingsHydratedRef.current) return;
      const next = getRenderingSettings(state);
      const prev = getRenderingSettings(prevState);
      if (settingsEqual(next, prev)) return;

      if (settingsDebounceRef.current) clearTimeout(settingsDebounceRef.current);
      settingsDebounceRef.current = setTimeout(async () => {
        try {
          await fetch(SETTINGS_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(next),
          });
        } catch {
          /* best-effort */
        }
      }, SETTINGS_DEBOUNCE_MS);
    });

    return () => {
      unsubscribe();
      if (settingsDebounceRef.current) clearTimeout(settingsDebounceRef.current);
    };
  }, []);

  // ---- Phase 4: push telemetry changes to backend ----------------------
  useEffect(() => {
    const unsubscribe = useGenerativeUIStore.subscribe((state, prevState) => {
      if (!telemetryHydratedRef.current) return;
      const next = state.telemetry;
      const prev = prevState.telemetry;
      if (telemetryEqual(next, prev)) return;

      if (telemetryDebounceRef.current) clearTimeout(telemetryDebounceRef.current);
      telemetryDebounceRef.current = setTimeout(async () => {
        try {
          await fetch(TELEMETRY_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(next),
          });
        } catch {
          /* best-effort */
        }
      }, TELEMETRY_DEBOUNCE_MS);
    });

    return () => {
      unsubscribe();
      if (telemetryDebounceRef.current) clearTimeout(telemetryDebounceRef.current);
    };
  }, []);

  return null;
}

export default GenerativeUIBackendSync;
