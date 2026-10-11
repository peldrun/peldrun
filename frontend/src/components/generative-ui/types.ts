// frontend/src/components/generative-ui/types.ts
import type React from "react";

export type GenerativeUIPreset = "economic" | "balanced" | "full" | "custom";

export interface GenerativeUIConfig {
  enabled: boolean;
  preset: GenerativeUIPreset;
  allowedComponents: string[];
  maxOutputTokens: number;
  maxRetries: number;
}

export interface GenerativeUITelemetry {
  totalInvocations: number;
  successfulRenders: number;
  fallbacksToMarkdown: number;
  averageTokens: number;
  lastRenderLatencyMs: number;
}

export interface PeldrunCardProps {
  title?: string;
  description?: string;
  status?: "info" | "success" | "warning" | "error";
  children?: React.ReactNode;
}

export interface PeldrunMetricProps {
  label: string;
  value: string | number;
  change?: string;
  trend?: "up" | "down" | "neutral";
  caption?: string;
}

export interface PeldrunBadgeProps {
  label: string;
  variant?: "info" | "success" | "warning" | "error" | "neutral";
}

export interface PeldrunAlertProps {
  message: string;
  type?: "info" | "success" | "warning" | "error";
  title?: string;
}

export interface PeldrunProgressProps {
  label?: string;
  percentage: number;
  statusText?: string;
}

export interface PeldrunTableProps {
  headers: string[];
  rows: (string | number)[][];
  caption?: string;
}

export interface ChartDataPoint {
  label: string;
  value: number;
}

export interface PeldrunChartProps {
  title?: string;
  type?: "area" | "bar" | "line" | "donut" | "pie";
  data: ChartDataPoint[];
  unit?: string;
}

export interface FormFieldDefinition {
  name: string;
  label: string;
  type?: "text" | "number" | "select" | "checkbox";
  options?: string[];
  placeholder?: string;
}

export interface PeldrunFormProps {
  title?: string;
  description?: string;
  fields: FormFieldDefinition[];
  submitLabel?: string;
  onSubmit?: (values: Record<string, string | boolean>) => void;
}

export interface TestResultItem {
  name: string;
  status: "passed" | "failed" | "skipped";
  durationMs?: number;
  error?: string;
}

export interface PeldrunTestResultsProps {
  suiteName?: string;
  passedCount: number;
  failedCount: number;
  skippedCount?: number;
  tests?: TestResultItem[];
}

export interface PeldrunAccordionItem {
  title: string;
  content: string;
}

export interface PeldrunAccordionProps {
  items: PeldrunAccordionItem[];
}

export interface PeldrunActionPayload {
  actionId: string;
  payload?: Record<string, unknown>;
}

export interface PeldrunActionButtonProps {
  label: string;
  actionId: string;
  payload?: Record<string, unknown>;
  variant?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  onExecute?: (action: PeldrunActionPayload) => void;
}

export interface PeldrunDiagramProps {
  code: string;
  title?: string;
}

export interface CarouselSlide {
  title?: string;
  description?: string;
  content: string;
}

export interface PeldrunCarouselProps {
  slides: CarouselSlide[];
  title?: string;
}

export interface PeldrunMathPlotProps {
  equation?: string;
  functionFormula?: string;
  rangeX?: [number, number];
  title?: string;
}
