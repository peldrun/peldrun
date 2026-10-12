// frontend/src/components/generative-ui/peldrun-library.tsx
"use client";

import React from "react";
import dynamic from "next/dynamic";
import { z } from "zod";

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

// Dynamic imports for resource-heavy engines
const LazyDiagram = dynamic(
  () => import("./catalog/PeldrunDiagram").then((m) => m.PeldrunDiagram),
  {
    loading: () => (
      <div className="my-2.5 rounded-xl border border-border/80 bg-card/50 p-6 flex items-center justify-center gap-2 text-xs text-muted-foreground animate-pulse">
        <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span>Loading diagram engine...</span>
      </div>
    ),
    ssr: false,
  }
);

const LazyMathPlot = dynamic(
  () => import("./catalog/PeldrunMathPlot").then((m) => m.PeldrunMathPlot),
  {
    loading: () => (
      <div className="my-2.5 rounded-xl border border-border/80 bg-card/50 p-6 flex items-center justify-center gap-2 text-xs text-muted-foreground animate-pulse">
        <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span>Loading mathematical plotting engine...</span>
      </div>
    ),
    ssr: false,
  }
);

const LazyShadcnChart = dynamic(
  () => import("./catalog/PeldrunShadcnChart").then((m) => m.PeldrunShadcnChart),
  {
    loading: () => (
      <div className="my-2 rounded-xl border border-border/80 bg-card/50 p-6 flex items-center justify-center gap-2 text-xs text-muted-foreground animate-pulse">
        <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span>Rendering visual analytics chart...</span>
      </div>
    ),
    ssr: false,
  }
);

// ============================================================
// 1. Zod Component Schemas
// ============================================================

export const MetricSchema = z.object({
  label: z.string().default("Metric"),
  value: z.union([z.string(), z.number()]).default("0"),
  change: z.string().optional(),
  trend: z.enum(["up", "down", "neutral"]).default("neutral"),
});

export const BadgeSchema = z.object({
  label: z.string(),
  variant: z.enum(["info", "success", "warning", "error", "neutral"]).default("neutral"),
});

export const AlertSchema = z.object({
  message: z.string(),
  type: z.enum(["info", "success", "warning", "error"]).default("info"),
});

export const ProgressSchema = z.object({
  label: z.string().default("Progress"),
  percentage: z.number().min(0).max(100).default(0),
});

export const TableSchema = z.object({
  headers: z.array(z.string()).default(["Item", "Value"]),
  rows: z.array(z.array(z.string())).default([]),
});

export const ChartSchema = z.object({
  title: z.string().default("Performance Analytics"),
  type: z.enum(["bar", "line", "donut", "area"]).default("bar"),
  data: z.array(
    z.object({
      label: z.string(),
      value: z.number(),
    })
  ).default([]),
  unit: z.string().optional(),
});

export const DiagramSchema = z.object({
  title: z.string().optional(),
  code: z.string(),
});

export const MathPlotSchema = z.object({
  title: z.string().optional(),
  functionFormula: z.string().optional(),
  equation: z.string().optional(),
  rangeX: z.tuple([z.number(), z.number()]).default([-10, 10]),
});

export const CarouselSchema = z.object({
  title: z.string().optional(),
  slides: z.array(
    z.object({
      title: z.string(),
      description: z.string().optional(),
      content: z.string(),
    })
  ).default([]),
});

export const AccordionSchema = z.object({
  items: z.array(
    z.object({
      title: z.string(),
      content: z.string(),
    })
  ).default([]),
});

export const TestResultsSchema = z.object({
  passedCount: z.number().default(0),
  failedCount: z.number().default(0),
  skippedCount: z.number().default(0),
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
  description: z.string().optional(),
  fields: z.array(FormFieldSchema).default([]),
  submitLabel: z.string().default("Submit"),
});

export const CardSchema = z.object({
  title: z.string().optional(),
  description: z.string().optional(),
  items: z.array(z.string()).default([]),
});

// ============================================================
// 2. Component Contract & Definition Builder
// ============================================================

export interface ComponentDefinition {
  name: string;
  description: string;
  schema: z.ZodTypeAny;
  render: (props: any) => React.JSX.Element;
}

function createComponentDefinition<T extends z.ZodTypeAny>(def: {
  name: string;
  description: string;
  schema: T;
  render: (props: z.infer<T>) => React.JSX.Element;
}): ComponentDefinition {
  return {
    name: def.name,
    description: def.description,
    schema: def.schema,
    render: (props: unknown) => def.render(props as z.infer<T>),
  };
}

// ============================================================
// 3. Peldrun Component Registry Map
// ============================================================

export const peldrunComponentRegistry: Record<string, ComponentDefinition> = {
  Metric: createComponentDefinition({
    name: "Metric",
    description: "Displays a numeric KPI metric with change magnitude and trend indicator.",
    schema: MetricSchema,
    render: (props) => (
      <PeldrunMotionWrapper>
        <PeldrunMetric
          label={props.label}
          value={props.value}
          change={props.change}
          trend={props.trend}
        />
      </PeldrunMotionWrapper>
    ),
  }),

  Badge: createComponentDefinition({
    name: "Badge",
    description: "Displays a compact status badge or service state indicator.",
    schema: BadgeSchema,
    render: (props) => (
      <PeldrunBadge label={props.label} variant={props.variant} />
    ),
  }),

  Alert: createComponentDefinition({
    name: "Alert",
    description: "Displays a prominent message callout with contextual severity coloring.",
    schema: AlertSchema,
    render: (props) => (
      <PeldrunMotionWrapper>
        <PeldrunAlert message={props.message} type={props.type} />
      </PeldrunMotionWrapper>
    ),
  }),

  Progress: createComponentDefinition({
    name: "Progress",
    description: "Visualizes completion percentage or resource capacity.",
    schema: ProgressSchema,
    render: (props) => (
      <PeldrunMotionWrapper>
        <PeldrunProgress label={props.label} percentage={props.percentage} />
      </PeldrunMotionWrapper>
    ),
  }),

  Table: createComponentDefinition({
    name: "Table",
    description: "Renders tabular structured data with headers and cell rows.",
    schema: TableSchema,
    render: (props) => (
      <PeldrunMotionWrapper>
        <PeldrunTable headers={props.headers} rows={props.rows} />
      </PeldrunMotionWrapper>
    ),
  }),

  Chart: createComponentDefinition({
    name: "Chart",
    description: "Renders visual data charts (bar, line, donut, area).",
    schema: ChartSchema,
    render: (props) => (
      <LazyShadcnChart
        title={props.title}
        type={props.type as ChartVariant}
        data={props.data}
        unit={props.unit}
      />
    ),
  }),

  Diagram: createComponentDefinition({
    name: "Diagram",
    description: "Renders visual flowcharts and architecture diagrams via Mermaid syntax.",
    schema: DiagramSchema,
    render: (props) => (
      <LazyDiagram title={props.title} code={props.code} />
    ),
  }),

  MathPlot: createComponentDefinition({
    name: "MathPlot",
    description: "Plots mathematical 2D function curves and scientific LaTeX formulas.",
    schema: MathPlotSchema,
    render: (props) => (
      <LazyMathPlot
        title={props.title}
        functionFormula={props.functionFormula}
        equation={props.equation}
        rangeX={props.rangeX}
      />
    ),
  }),

  Carousel: createComponentDefinition({
    name: "Carousel",
    description: "Displays multi-card showcases in an interactive swipeable slider.",
    schema: CarouselSchema,
    render: (props) => (
      <PeldrunCarousel title={props.title} slides={props.slides as CarouselSlide[]} />
    ),
  }),

  Accordion: createComponentDefinition({
    name: "Accordion",
    description: "Collapsible disclosure sections for progressive detail disclosure.",
    schema: AccordionSchema,
    render: (props) => (
      <PeldrunMotionWrapper>
        <PeldrunAccordion items={props.items} />
      </PeldrunMotionWrapper>
    ),
  }),

  TestResults: createComponentDefinition({
    name: "TestResults",
    description: "Displays test execution summary with passed, failed, and skipped metrics.",
    schema: TestResultsSchema,
    render: (props) => (
      <PeldrunMotionWrapper>
        <PeldrunTestResults
          passedCount={props.passedCount}
          failedCount={props.failedCount}
          skippedCount={props.skippedCount}
        />
      </PeldrunMotionWrapper>
    ),
  }),

  ActionButton: createComponentDefinition({
    name: "ActionButton",
    description: "Interactive button triggering predefined agent workflows or tools.",
    schema: ActionButtonSchema,
    render: (props) => (
      <PeldrunActionButton label={props.label} actionId={props.actionId} />
    ),
  }),

  Form: createComponentDefinition({
    name: "Form",
    description: "Configurable user input form with typed input fields.",
    schema: FormSchema,
    render: (props) => (
      <PeldrunMotionWrapper>
        <PeldrunForm
          title={props.title}
          description={props.description}
          fields={props.fields as FormFieldDefinition[]}
          submitLabel={props.submitLabel}
        />
      </PeldrunMotionWrapper>
    ),
  }),

  Card: createComponentDefinition({
    name: "Card",
    description: "General container card with title, description, and nested elements.",
    schema: CardSchema,
    render: (props) => (
      <PeldrunMotionWrapper>
        <PeldrunCard title={props.title} description={props.description}>
          {props.items && props.items.length > 0 && (
            <div className="space-y-1 mt-1.5 text-xs text-muted-foreground">
              {props.items.map((it, idx) => (
                <div key={`card-item-${idx}`}>
                  <MarkdownRenderer content={it} />
                </div>
              ))}
            </div>
          )}
        </PeldrunCard>
      </PeldrunMotionWrapper>
    ),
  }),
};

/**
 * Validates raw payload against a registered component schema and renders it.
 */
export function renderPeldrunComponent(componentName: string, rawProps: unknown): React.JSX.Element | null {
  const definition = peldrunComponentRegistry[componentName];
  if (!definition) {
    return null;
  }

  const parseResult = definition.schema.safeParse(rawProps);
  if (!parseResult.success) {
    console.warn(`[PeldrunLibrary] Schema validation failed for component ${componentName}:`, parseResult.error);
    return null;
  }

  return definition.render(parseResult.data);
}
