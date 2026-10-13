// frontend/src/components/generative-ui/store.ts
//
// Zustand store for rendering settings.
//
// The store persists the user's rendering preferences to localStorage and
// exposes typed actions for the settings UI. It also carries session-scoped
// telemetry counters that are intentionally excluded from persistence.
//
// The store is the single source of truth for which renderers are active.
// MarkdownRenderer, PeldrunDiagram, PeldrunChartBlock, and PeldrunPlainText
// all read from this store to decide whether to render or fall back to a
// plain code block.
//
// Backward compatibility:
//   The previous store persisted an OpenUI-shaped state:
//     { enabled, preset, allowedComponents, maxOutputTokens, maxRetries }
//   On hydration, that shape is migrated to the new shape:
//     { enabled, features, limits, max_retries }
//   The migration is one-shot: the legacy keys are dropped after the first
//   successful rehydration.

"use client";

import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import {
  DEFAULT_SETTINGS,
  EMPTY_TELEMETRY,
  PRESET_DEFINITIONS,
  applyPreset,
  classifyPreset,
  type RenderingFeatureKey,
  type RenderingFeatures,
  type RenderingLimits,
  type RenderingPreset,
  type RenderingSettings,
  type RenderingTelemetry,
} from "./types";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

interface RenderingState extends RenderingSettings {
  /** Session-only counters. Never persisted. */
  telemetry: RenderingTelemetry;

  // ---- Settings actions -----------------------------------------------

  /** Toggle the master switch. */
  setEnabled: (enabled: boolean) => void;

  /** Toggle a single rendering feature. */
  toggleFeature: (key: RenderingFeatureKey) => void;

  /** Set a feature explicitly. */
  setFeature: (key: RenderingFeatureKey, value: boolean) => void;

  /** Update one or more limits. */
  setLimits: (patch: Partial<RenderingLimits>) => void;

  /** Set the retry budget. Clamped to [0, 5]. */
  setMaxRetries: (value: number) => void;

  /** Apply a named preset, preserving `enabled` and `max_retries`. */
  applyPresetById: (id: Exclude<RenderingPreset, "custom">) => void;

  /** Reset every setting to the default. */
  resetSettings: () => void;

  // ---- Telemetry actions ----------------------------------------------

  recordSuccess: (latencyMs: number) => void;
  recordError: (format: "mermaid" | "chart" | "latex") => void;
  resetTelemetry: () => void;
}

// ---------------------------------------------------------------------------
// Legacy shape (used for migration only)
// ---------------------------------------------------------------------------

interface LegacyState {
  enabled?: boolean;
  preset?: string;
  allowedComponents?: string[];
  allowed_components?: string[];
  maxOutputTokens?: number;
  max_output_tokens?: number;
  maxRetries?: number;
  max_retries?: number;
}

function isLegacyState(state: unknown): state is LegacyState {
  if (!state || typeof state !== "object") return false;
  const s = state as Record<string, unknown>;
  return (
    "allowedComponents" in s ||
    "allowed_components" in s ||
    "maxOutputTokens" in s ||
    "max_output_tokens" in s ||
    "preset" in s
  );
}

function migrateLegacyState(legacy: LegacyState): RenderingSettings {
  const enabled = legacy.enabled ?? DEFAULT_SETTINGS.enabled;
  const preset = legacy.preset ?? "balanced";

  const features: RenderingFeatures = { ...DEFAULT_SETTINGS.features };
  if (preset === "economic") {
    features.mermaid = false;
    features.latex = false;
    features.charts = false;
  }

  const limits: RenderingLimits = { ...DEFAULT_SETTINGS.limits };

  const rawTokens =
    legacy.maxOutputTokens ?? legacy.max_output_tokens ?? undefined;
  if (typeof rawTokens === "number" && Number.isFinite(rawTokens)) {
    limits.max_output_tokens = Math.max(512, Math.min(8192, rawTokens));
  }

  const rawRetries = legacy.maxRetries ?? legacy.max_retries ?? undefined;
  const maxRetries =
    typeof rawRetries === "number" && Number.isFinite(rawRetries)
      ? Math.max(0, Math.min(5, rawRetries))
      : DEFAULT_SETTINGS.max_retries;

  return {
    enabled,
    features,
    limits,
    max_retries: maxRetries,
  };
}

// ---------------------------------------------------------------------------
// Clamping helpers
// ---------------------------------------------------------------------------

function clampTokens(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_SETTINGS.limits.max_output_tokens;
  return Math.max(512, Math.min(8192, Math.round(value)));
}

function clampRetries(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_SETTINGS.max_retries;
  return Math.max(0, Math.min(5, Math.round(value)));
}

function clampPositive(value: number, fallback: number): number {
  if (!Number.isFinite(value) || value < 1) return fallback;
  return Math.round(value);
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useGenerativeUIStore = create<RenderingState>()(
  persist(
    (set, get) => ({
      // -------- Persisted settings --------
      ...DEFAULT_SETTINGS,

      // -------- Session telemetry --------
      telemetry: { ...EMPTY_TELEMETRY },

      // -------- Settings actions --------

      setEnabled: (enabled) => {
        set({ enabled: Boolean(enabled) });
      },

      toggleFeature: (key) => {
        set((state) => ({
          features: {
            ...state.features,
            [key]: !state.features[key],
          },
        }));
      },

      setFeature: (key, value) => {
        set((state) => ({
          features: {
            ...state.features,
            [key]: Boolean(value),
          },
        }));
      },

      setLimits: (patch) => {
        set((state) => {
          const next: RenderingLimits = { ...state.limits };

          if (patch.max_mermaid_nodes !== undefined) {
            next.max_mermaid_nodes = clampPositive(
              patch.max_mermaid_nodes,
              state.limits.max_mermaid_nodes,
            );
          }
          if (patch.max_chart_points !== undefined) {
            next.max_chart_points = clampPositive(
              patch.max_chart_points,
              state.limits.max_chart_points,
            );
          }
          if (patch.max_table_rows !== undefined) {
            next.max_table_rows = clampPositive(
              patch.max_table_rows,
              state.limits.max_table_rows,
            );
          }
          if (patch.max_output_tokens !== undefined) {
            next.max_output_tokens = clampTokens(patch.max_output_tokens);
          }

          return { limits: next };
        });
      },

      setMaxRetries: (value) => {
        set({ max_retries: clampRetries(value) });
      },

      applyPresetById: (id) => {
        set((state) => {
          const current: RenderingSettings = {
            enabled: state.enabled,
            features: state.features,
            limits: state.limits,
            max_retries: state.max_retries,
          };
          const next = applyPreset(current, id);
          return {
            features: next.features,
            limits: next.limits,
          };
        });
      },

      resetSettings: () => {
        set({
          enabled: DEFAULT_SETTINGS.enabled,
          features: { ...DEFAULT_SETTINGS.features },
          limits: { ...DEFAULT_SETTINGS.limits },
          max_retries: DEFAULT_SETTINGS.max_retries,
        });
      },

      // -------- Telemetry actions --------

      recordSuccess: (latencyMs) => {
        set((state) => {
          const t = state.telemetry;
          return {
            telemetry: {
              ...t,
              totalRenders: t.totalRenders + 1,
              successfulRenders: t.successfulRenders + 1,
              lastRenderLatencyMs:
                Number.isFinite(latencyMs) && latencyMs >= 0
                  ? Math.round(latencyMs)
                  : t.lastRenderLatencyMs,
            },
          };
        });
      },

      recordError: (format) => {
        set((state) => {
          const t = state.telemetry;
          const next: RenderingTelemetry = {
            ...t,
            totalRenders: t.totalRenders + 1,
          };
          if (format === "mermaid") {
            next.mermaidErrors = t.mermaidErrors + 1;
          } else if (format === "chart") {
            next.chartErrors = t.chartErrors + 1;
          } else if (format === "latex") {
            next.latexErrors = t.latexErrors + 1;
          }
          return { telemetry: next };
        });
      },

      resetTelemetry: () => {
        set({ telemetry: { ...EMPTY_TELEMETRY } });
      },
    }),
    {
      name: "peldrun.rendering-settings",
      version: 2,
      storage: createJSONStorage(() => localStorage),

      partialize: (state) => ({
        enabled: state.enabled,
        features: state.features,
        limits: state.limits,
        max_retries: state.max_retries,
      }),

      migrate: (persistedState: unknown, _version: number) => {
        if (isLegacyState(persistedState)) {
          return migrateLegacyState(persistedState);
        }
        return persistedState as RenderingSettings;
      },

      merge: (persisted, current) => {
        const p = (persisted ?? {}) as Partial<RenderingSettings>;
        return {
          ...current,
          enabled: p.enabled ?? current.enabled,
          features: { ...current.features, ...(p.features ?? {}) },
          limits: { ...current.limits, ...(p.limits ?? {}) },
          max_retries: p.max_retries ?? current.max_retries,
          telemetry: { ...EMPTY_TELEMETRY },
        };
      },
    },
  ),
);

// ---------------------------------------------------------------------------
// Selectors (read helpers)
// ---------------------------------------------------------------------------

/**
 * Returns the settings slice of the store, stripped of actions and telemetry.
 */
export function getRenderingSettings(state: RenderingState): RenderingSettings {
  return {
    enabled: state.enabled,
    features: state.features,
    limits: state.limits,
    max_retries: state.max_retries,
  };
}

/**
 * Returns the currently active preset name, or "custom" when the current
 * feature and limit combination does not match a named preset.
 */
export function getActivePreset(state: RenderingState): RenderingPreset {
  return classifyPreset(getRenderingSettings(state));
}

/**
 * Returns true when the master switch is on and at least one feature that
 * produces visual output (mermaid, latex, charts) is enabled.
 */
export function hasVisualOutputEnabled(state: RenderingState): boolean {
  return (
    state.enabled &&
    (state.features.mermaid || state.features.latex || state.features.charts)
  );
}

/**
 * Returns the effective output-token budget. When the master switch is off,
 * returns the default budget regardless of the persisted limits.
 */
export function getEffectiveMaxTokens(state: RenderingState): number {
  if (!state.enabled) return DEFAULT_SETTINGS.limits.max_output_tokens;
  return state.limits.max_output_tokens;
}

/**
 * Returns true when a specific feature is enabled AND the master switch is
 * on. Use this in renderers to gate a specific format.
 *
 * Example:
 *   const canRenderMermaid = useGenerativeUIStore((s) =>
 *     isFeatureEnabled(s, "mermaid"),
 *   );
 */
export function isFeatureEnabled(
  state: RenderingState,
  key: RenderingFeatureKey,
): boolean {
  if (!state.enabled) return false;
  return state.features[key] === true;
}

// ---------------------------------------------------------------------------
// Backend payload
// ---------------------------------------------------------------------------

/**
 * Returns the payload to send to the backend for the current settings.
 *
 * This is the single source of truth for what the backend receives. It is
 * also exported for use from chat-container and any other request builder.
 *
 * Call with no argument to read the current store state, or pass a state
 * object explicitly for testability.
 */
export function getGenerativeUIPayload(
  state?: RenderingState,
): RenderingSettings {
  const current = state ?? useGenerativeUIStore.getState();
  return getRenderingSettings(current);
}

// ---------------------------------------------------------------------------
// Re-exports for convenience
// ---------------------------------------------------------------------------

export { PRESET_DEFINITIONS };
export type { RenderingPreset };
