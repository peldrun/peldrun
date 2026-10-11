// frontend/src/components/generative-ui/store.ts
import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { GenerativeUIConfig, GenerativeUIPreset, GenerativeUITelemetry } from "./types";

interface GenerativeUIState extends GenerativeUIConfig {
  telemetry: GenerativeUITelemetry;
  setEnabled: (enabled: boolean) => void;
  setPreset: (preset: GenerativeUIPreset) => void;
  toggleComponent: (componentName: string, enabled?: boolean) => void;
  setMaxOutputTokens: (budget: number) => void;
  setMaxRetries: (retries: number) => void;
  recordRenderMetric: (success: boolean, latencyMs: number, tokens?: number) => void;
}

export const ALL_SUPPORTED_COMPONENTS = [
  "Card",
  "Metric",
  "Badge",
  "Alert",
  "Progress",
  "Table",
  "Chart",
  "Form",
  "TestResults",
  "Accordion",
  "ActionButton",
  "Diagram",
  "Carousel",
  "MathPlot",
];

const PRESET_COMPONENTS: Record<GenerativeUIPreset, string[]> = {
  economic: ["Card", "Badge", "ActionButton"],
  balanced: [
    "Card",
    "Metric",
    "Badge",
    "Alert",
    "Progress",
    "Table",
    "Chart",
    "TestResults",
    "ActionButton",
    "Diagram",
    "Carousel",
  ],
  full: [...ALL_SUPPORTED_COMPONENTS],
  custom: ["Card", "Metric", "Table", "ActionButton", "Diagram", "Chart"],
};

export const useGenerativeUIStore = create<GenerativeUIState>()(
  persist(
    (set) => ({
      enabled: true,
      preset: "full",
      allowedComponents: PRESET_COMPONENTS.full,
      maxOutputTokens: 2048,
      maxRetries: 1,
      telemetry: {
        totalInvocations: 0,
        successfulRenders: 0,
        fallbacksToMarkdown: 0,
        averageTokens: 0,
        lastRenderLatencyMs: 0,
      },

      setEnabled: (enabled: boolean) => set({ enabled }),

      setPreset: (preset: GenerativeUIPreset) =>
        set((state) => ({
          preset,
          allowedComponents:
            preset === "custom"
              ? state.allowedComponents
              : PRESET_COMPONENTS[preset],
        })),

      toggleComponent: (componentName: string, enabled?: boolean) =>
        set((state) => {
          const exists = state.allowedComponents.includes(componentName);
          const nextAllowed =
            enabled ?? !exists
              ? [...new Set([...state.allowedComponents, componentName])]
              : state.allowedComponents.filter((c) => c !== componentName);
          return { allowedComponents: nextAllowed, preset: "custom" };
        }),

      setMaxOutputTokens: (maxOutputTokens: number) => set({ maxOutputTokens }),

      setMaxRetries: (maxRetries: number) => set({ maxRetries }),

      recordRenderMetric: (success: boolean, latencyMs: number, tokens?: number) =>
        set((state) => {
          const totalInvocations = state.telemetry.totalInvocations + 1;
          const successfulRenders =
            state.telemetry.successfulRenders + (success ? 1 : 0);
          const fallbacksToMarkdown =
            state.telemetry.fallbacksToMarkdown + (success ? 0 : 1);
          const currentAvg = state.telemetry.averageTokens;
          const newTokens = tokens ?? currentAvg;
          const averageTokens = Math.round(
            (currentAvg * (totalInvocations - 1) + newTokens) / totalInvocations
          );

          return {
            telemetry: {
              totalInvocations,
              successfulRenders,
              fallbacksToMarkdown,
              averageTokens,
              lastRenderLatencyMs: latencyMs,
            },
          };
        }),
    }),
    {
      name: "peldrun_generative_ui_settings",
      partialize: (state) => ({
        enabled: state.enabled,
        preset: state.preset,
        allowedComponents: state.allowedComponents,
        maxOutputTokens: state.maxOutputTokens,
        maxRetries: state.maxRetries,
      }),
    }
  )
);

export function getGenerativeUIPayload(): Record<string, unknown> | undefined {
  const state = useGenerativeUIStore.getState();
  if (!state.enabled) {
    return undefined;
  }
  return {
    enabled: true,
    preset: state.preset,
    allowed_components: state.allowedComponents,
    max_output_tokens: state.maxOutputTokens,
    max_retries: state.maxRetries,
  };
}
