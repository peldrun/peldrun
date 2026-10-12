// frontend/src/components/generative-ui/peldrun-library.tsx
"use client";

import React from "react";
import dynamic from "next/dynamic";
import { z } from "zod";
import { defineComponent, createLibrary } from "@openuidev/react-lang";

import { PeldrunCard } from "./catalog/PeldrunCard";
import { PeldrunMetric } from "./catalog/PeldrunMetric";
import { PeldrunBadge } from "./catalog/PeldrunBadge";
import { PeldrunAlert } from "./catalog/PeldrunAlert";
import { PeldrunProgress } from "./catalog/PeldrunProgress";
import { PeldrunTable } from "./catalog/PeldrunTable";
import { PeldrunCarousel } from "./catalog/PeldrunCarousel";
import { PeldrunAccordion } from "./catalog/PeldrunAccordion";
import { PeldrunTestResults } from "./catalog/PeldrunTestResults";
import { PeldrunActionButton } from "./catalog/PeldrunActionButton";
import { PeldrunForm } from "./catalog/PeldrunForm";
import { PeldrunMotionWrapper } from "./catalog/PeldrunMotionWrapper";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";

import type { ChartVariant } from "./catalog/PeldrunShadcnChart";
import type { CarouselSlide } from "./catalog/PeldrunCarousel";
import type { FormFieldDefinition } from "./types";

// ============================================================
// Dynamic imports for heavy rendering engines
// ============================================================

const LazyDiagram = dynamic(
  () => import("./catalog/PeldrunDiagram").then((m) => m.PeldrunDiagram),
  { loading: () => <div className="p-6 text-xs text-muted-foreground animate-pulse">Loading diagram...</div>, ssr: false }
);

const LazyMathPlot = dynamic(
  () => import("./catalog/PeldrunMathPlot").then((m) => m.PeldrunMathPlot),
  { loading: () => <div className="p-6 text-xs text-muted-foreground animate-pulse">Loading math plot...</div>, ssr: false }
);

const LazyShadcnChart = dynamic(
  () => import("./catalog/PeldrunShadcnChart").then((m) => m.PeldrunShadcnChart),
  { loading: () => <div className="p-6 text-xs text-muted-foreground animate-pulse">Loading chart...</div>, ssr: false }
);

// ============================================================
// 1. Zod Schemas (Field order = positional argument order)
// ============================================================

export const StackSchema = z.object({
  children: z.array(z.any()).default([]).describe("Child component references"),
  direction: z.enum(["vertical", "horizontal"]).default("vertical").describe("Layout direction"),
  gap: z.string().optional().describe("Spacing gap"),
});

export const GridSchema = z.object({
  children: z.array(z.any()).default([]).describe("Child component references"),
  columns: z.number().default(2).describe("Number of columns"),
});

export const CardSchema = z.object({
  title: z.string().optional().describe("Card title"),
  description: z.string().optional().describe("Card description"),
  items: z.array(z.string()).default([]).describe("Bullet items"),
});

export const MetricSchema = z.object({
  label: z.string().default("Metric").describe("KPI label"),
  value: z.union([z.string(), z.number()]).default("0").describe("KPI value"),
  change: z.string().optional().describe("Change magnitude"),
  trend: z.enum(["up", "down", "neutral"]).default("neutral").describe("Trend"),
});

export const ChartSchema = z.object({
  type: z.enum(["bar", "line", "donut", "area"]).default("bar"),
  title: z.string().default("Analytics"),
  unit: z.string().optional(),
  data: z.array(z.object({ label: z.string(), value: z.number() })).default([]),
});

export const TableSchema = z.object({
  headers: z.array(z.string()).default(["Item", "Value"]),
  rows: z.array(z.array(z.union([z.string(), z.number()]))).default([]),
});

export const DiagramSchema = z.object({
  title: z.string().optional(),
  code: z.string(),
});

export const MathPlotSchema = z.object({
  title: z.string().optional(),
  formula: z.string().optional(),
  equation: z.string().optional(),
  rangeX: z.tuple([z.number(), z.number()]).default([-10, 10]),
});

export const AlertSchema = z.object({
  type: z.enum(["info", "success", "warning", "error"]).default("info"),
  message: z.string(),
});

export const BadgeSchema = z.object({
  label: z.string(),
  variant: z.enum(["info", "success", "warning", "error", "neutral"]).default("neutral"),
});

export const ProgressSchema = z.object({
  label: z.string().default("Progress"),
  percentage: z.number().min(0).max(100).default(0),
});

export const CarouselSchema = z.object({
  title: z.string().optional(),
  slides: z.array(z.object({ title: z.string(), description: z.string().optional(), content: z.string() })).default([]),
});

export const AccordionSchema = z.object({
  items: z.array(z.object({ title: z.string(), content: z.string() })).default([]),
});

export const TestResultsSchema = z.object({
  passed: z.number().default(0),
  failed: z.number().default(0),
  skipped: z.number().default(0),
});

export const ActionButtonSchema = z.object({
  label: z.string(),
  actionId: z.string(),
});

export const FormFieldSchema = z.object({
  name: z.string(),
  label: z.string(),
  type: z.enum(["text", "number", "email", "password", "select", "checkbox"]).default("text"),
  placeholder: z.string().optional(),
  required: z.boolean().optional(),
  options: z.array(z.string()).optional(),
});

export const FormSchema = z.object({
  title: z.string(),
  fields: z.array(FormFieldSchema).default([]),
  submitLabel: z.string().default("Submit"),
  description: z.string().optional(),
});

// ============================================================
// 2. Component Definitions (CORRECT { props, renderNode } pattern)
// ============================================================

export const Stack = defineComponent({
  name: "Stack",
  description: "Root layout container that stacks UI components vertically or horizontally.",
  props: StackSchema,
  component: ({ props, renderNode }: any) => {
    const p = props ?? {};
    const items = Array.isArray(p.children) ? p.children : p.children ? [p.children] : [];
    const isHorizontal = p.direction === "horizontal";
    return (
      <div className={`flex ${isHorizontal ? "flex-row flex-wrap" : "flex-col"} gap-3.5 w-full my-2`}>
        {items.map((child: any, idx: number) => (
          <div key={`stack-item-${idx}`} className="w-full">
            {renderNode ? renderNode(child) : null}
          </div>
        ))}
      </div>
    );
  },
});

export const Grid = defineComponent({
  name: "Grid",
  description: "Responsive grid container for multi-column layout.",
  props: GridSchema,
  component: ({ props, renderNode }: any) => {
    const p = props ?? {};
    const items = Array.isArray(p.children) ? p.children : p.children ? [p.children] : [];
    const cols = p.columns ?? 2;
    const colClass =
      cols === 1 ? "grid-cols-1"
      : cols === 3 ? "grid-cols-1 md:grid-cols-3"
      : cols === 4 ? "grid-cols-1 md:grid-cols-2 lg:grid-cols-4"
      : "grid-cols-1 md:grid-cols-2";
    return (
      <div className={`grid ${colClass} gap-3.5 w-full my-2`}>
        {items.map((child: any, idx: number) => (
          <div key={`grid-item-${idx}`} className="w-full">
            {renderNode ? renderNode(child) : null}
          </div>
        ))}
      </div>
    );
  },
});

export const Card = defineComponent({
  name: "Card",
  description: "General container card with title, description, and optional nested markdown items.",
  props: CardSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return (
      <PeldrunMotionWrapper>
        <PeldrunCard title={p.title} description={p.description}>
          {p.items && p.items.length > 0 && (
            <div className="space-y-1 mt-1.5 text-xs text-muted-foreground">
              {p.items.map((it: string, idx: number) => (
                <div key={`card-item-${idx}`}><MarkdownRenderer content={it} /></div>
              ))}
            </div>
          )}
        </PeldrunCard>
      </PeldrunMotionWrapper>
    );
  },
});

export const Metric = defineComponent({
  name: "Metric",
  description: "Displays a numeric KPI metric with trend.",
  props: MetricSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return (
      <PeldrunMotionWrapper>
        <PeldrunMetric label={p.label} value={p.value} change={p.change} trend={p.trend} />
      </PeldrunMotionWrapper>
    );
  },
});

export const Chart = defineComponent({
  name: "Chart",
  description: "Renders visual data analytics charts (bar, line, donut, area).",
  props: ChartSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return <LazyShadcnChart type={(p.type as ChartVariant) || "bar"} title={p.title} unit={p.unit} data={Array.isArray(p.data) ? p.data : []} />;
  },
});

export const Table = defineComponent({
  name: "Table",
  description: "Renders tabular structured data.",
  props: TableSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    const safeRows = Array.isArray(p.rows)
      ? p.rows.map((row: any) => Array.isArray(row) ? row.map((c: any) => String(c ?? "")) : [String(row ?? "")])
      : [];
    return (
      <PeldrunMotionWrapper>
        <PeldrunTable headers={p.headers || ["Item", "Value"]} rows={safeRows} />
      </PeldrunMotionWrapper>
    );
  },
});

export const Diagram = defineComponent({
  name: "Diagram",
  description: "Renders Mermaid diagrams.",
  props: DiagramSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return <LazyDiagram title={p.title} code={p.code ?? ""} />;
  },
});

export const MathPlot = defineComponent({
  name: "MathPlot",
  description: "Plots 2D math curves and LaTeX equations.",
  props: MathPlotSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return <LazyMathPlot title={p.title} functionFormula={p.formula} equation={p.equation} rangeX={p.rangeX} />;
  },
});

export const Alert = defineComponent({
  name: "Alert",
  description: "Severity-colored message callout.",
  props: AlertSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return (
      <PeldrunMotionWrapper>
        <PeldrunAlert type={p.type} message={p.message} />
      </PeldrunMotionWrapper>
    );
  },
});

export const Badge = defineComponent({
  name: "Badge",
  description: "Compact status badge.",
  props: BadgeSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return <PeldrunBadge label={p.label} variant={p.variant} />;
  },
});

export const Progress = defineComponent({
  name: "Progress",
  description: "Percentage progress bar.",
  props: ProgressSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return (
      <PeldrunMotionWrapper>
        <PeldrunProgress label={p.label} percentage={p.percentage} />
      </PeldrunMotionWrapper>
    );
  },
});

export const Carousel = defineComponent({
  name: "Carousel",
  description: "Swipeable multi-card showcase.",
  props: CarouselSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return <PeldrunCarousel title={p.title} slides={p.slides as CarouselSlide[]} />;
  },
});

export const Accordion = defineComponent({
  name: "Accordion",
  description: "Collapsible disclosure sections.",
  props: AccordionSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return (
      <PeldrunMotionWrapper>
        <PeldrunAccordion items={p.items} />
      </PeldrunMotionWrapper>
    );
  },
});

export const TestResults = defineComponent({
  name: "TestResults",
  description: "Test execution summary.",
  props: TestResultsSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return (
      <PeldrunMotionWrapper>
        <PeldrunTestResults passedCount={p.passed} failedCount={p.failed} skippedCount={p.skipped} />
      </PeldrunMotionWrapper>
    );
  },
});

export const ActionButton = defineComponent({
  name: "ActionButton",
  description: "Interactive action trigger.",
  props: ActionButtonSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return <PeldrunActionButton label={p.label} actionId={p.actionId} />;
  },
});

export const Form = defineComponent({
  name: "Form",
  description: "Configurable input form.",
  props: FormSchema,
  component: ({ props }: any) => {
    const p = props ?? {};
    return (
      <PeldrunMotionWrapper>
        <PeldrunForm title={p.title} description={p.description} fields={p.fields as FormFieldDefinition[]} submitLabel={p.submitLabel} />
      </PeldrunMotionWrapper>
    );
  },
});

// ============================================================
// 3. Assembled Library
// ============================================================

export const peldrunLibrary = createLibrary({
  root: "Stack",
  components: [
    Stack, Grid, Card, Metric, Chart, Table, Diagram, MathPlot,
    Alert, Badge, Progress, Carousel, Accordion, TestResults, ActionButton, Form,
  ],
});

export default peldrunLibrary;
