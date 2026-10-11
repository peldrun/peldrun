// frontend/src/components/generative-ui/OpenUIRenderer.tsx
"use client";

import React, { useMemo } from "react";
import { useGenerativeUIStore } from "./store";
import { PeldrunCard } from "./catalog/PeldrunCard";
import { PeldrunTable } from "./catalog/PeldrunTable";
import { PeldrunActionButton } from "./catalog/PeldrunActionButton";
import { PeldrunMetric } from "./catalog/PeldrunMetric";
import { PeldrunBadge } from "./catalog/PeldrunBadge";
import { PeldrunAlert } from "./catalog/PeldrunAlert";
import { PeldrunProgress } from "./catalog/PeldrunProgress";
import { PeldrunForm } from "./catalog/PeldrunForm";
import { PeldrunTestResults } from "./catalog/PeldrunTestResults";
import { PeldrunAccordion } from "./catalog/PeldrunAccordion";
import { PeldrunMotionWrapper } from "./catalog/PeldrunMotionWrapper";
import { PeldrunDiagram } from "./catalog/PeldrunDiagram";
import { PeldrunShadcnChart, type ChartVariant } from "./catalog/PeldrunShadcnChart";
import { PeldrunCarousel, type CarouselSlide } from "./catalog/PeldrunCarousel";
import { PeldrunMathPlot } from "./catalog/PeldrunMathPlot";
import type { FormFieldDefinition } from "./types";

interface OpenUIRendererProps {
  dslContent: string;
}

interface CardBuffer {
  title?: string;
  desc?: string;
  items: string[];
}

interface FormBuffer {
  title: string;
  description?: string;
  fields: FormFieldDefinition[];
  submitLabel?: string;
}

interface AccordionBuffer {
  items: { title: string; content: string }[];
}

interface DiagramBuffer {
  title?: string;
  codeLines: string[];
}

interface CarouselBuffer {
  title?: string;
  slides: CarouselSlide[];
}

function isMarkdownDividerCell(cell: string): boolean {
  const sanitized = cell.replace(/\s+/g, "").replace(/-/g, "").replace(/:/g, "");
  return sanitized.length === 0;
}

function parseXmlAttributes(attrString: string): Record<string, string> {
  const attrs: Record<string, string> = {};
  const attrRegex = /([a-zA-Z0-9_-]+)=(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g;
  let match: RegExpExecArray | null;
  while ((match = attrRegex.exec(attrString)) !== null) {
    const key = match[1].toLowerCase();
    const value = match[2] ?? match[3] ?? match[4] ?? "";
    attrs[key] = value.replace(/\/+$/, "");
  }
  return attrs;
}

function extractNumericValue(raw: string): number {
  if (!raw) return 0;
  const match = raw.match(/-?\d+(?:\.\d+)?/);
  if (!match) return 0;
  let val = parseFloat(match[0]);
  if (/k\b/i.test(raw)) val *= 1000;
  return isNaN(val) ? 0 : val;
}

export function OpenUIRenderer({ dslContent }: OpenUIRendererProps): React.JSX.Element {
  const allowedComponents = useGenerativeUIStore((s) => s.allowedComponents);

  const parsedElements = useMemo(() => {
    const rawLines = dslContent.split("\n").map((l) => l.trim()).filter(Boolean);
    const elements: React.JSX.Element[] = [];

    let currentCard: CardBuffer | null = null;
    let currentForm: FormBuffer | null = null;
    let currentAccordion: AccordionBuffer | null = null;
    let currentDiagram: DiagramBuffer | null = null;
    let currentCarousel: CarouselBuffer | null = null;
    let metricGroup: React.JSX.Element[] = [];
    let badgeGroup: React.JSX.Element[] = [];
    let tableLines: string[] = [];
    let inTableContext = false;

    const flushCard = () => {
      if (currentCard !== null) {
        const c = currentCard;
        elements.push(
          <PeldrunMotionWrapper key={`card-${elements.length}`}>
            <PeldrunCard title={c.title} description={c.desc}>
              {c.items.map((it, i) => (
                <div key={`ci-${i}`} className="text-muted-foreground text-xs leading-relaxed">
                  {it}
                </div>
              ))}
            </PeldrunCard>
          </PeldrunMotionWrapper>
        );
        currentCard = null;
      }
    };

    const flushForm = () => {
      if (currentForm !== null) {
        const f = currentForm;
        const validFields = f.fields.length > 0 ? f.fields : [{ name: "input", label: "Input" }];
        elements.push(
          <PeldrunMotionWrapper key={`form-${elements.length}`}>
            <PeldrunForm
              title={f.title}
              description={f.description}
              fields={validFields}
              submitLabel={f.submitLabel || "Submit"}
            />
          </PeldrunMotionWrapper>
        );
        currentForm = null;
      }
    };

    const flushAccordion = () => {
      if (currentAccordion !== null && currentAccordion.items.length > 0) {
        elements.push(
          <PeldrunMotionWrapper key={`accordion-${elements.length}`}>
            <PeldrunAccordion items={currentAccordion.items} />
          </PeldrunMotionWrapper>
        );
        currentAccordion = null;
      }
    };

    const flushDiagram = () => {
      if (currentDiagram !== null && currentDiagram.codeLines.length > 0) {
        const dCode = currentDiagram.codeLines.join("\n").trim();
        if (dCode) {
          elements.push(
            <PeldrunDiagram
              key={`diag-${elements.length}`}
              title={currentDiagram.title}
              code={dCode}
            />
          );
        }
        currentDiagram = null;
      }
    };

    const flushCarousel = () => {
      if (currentCarousel !== null && currentCarousel.slides.length > 0) {
        elements.push(
          <PeldrunCarousel
            key={`caro-${elements.length}`}
            title={currentCarousel.title}
            slides={currentCarousel.slides}
          />
        );
        currentCarousel = null;
      }
    };

    const flushMetrics = () => {
      if (metricGroup.length > 0) {
        elements.push(
          <PeldrunMotionWrapper key={`metrics-grid-${elements.length}`}>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5 my-2">
              {[...metricGroup]}
            </div>
          </PeldrunMotionWrapper>
        );
        metricGroup = [];
      }
    };

    const flushBadges = () => {
      if (badgeGroup.length > 0) {
        elements.push(
          <PeldrunMotionWrapper key={`badges-row-${elements.length}`}>
            <div className="flex flex-wrap items-center gap-2 my-2">
              {[...badgeGroup]}
            </div>
          </PeldrunMotionWrapper>
        );
        badgeGroup = [];
      }
    };

    const flushTable = () => {
      if (tableLines.length > 0) {
        if (allowedComponents.includes("Table")) {
          const rawRows = tableLines.map((r) => {
            if (r.includes("|")) {
              return r
                .split("|")
                .map((c) => c.trim())
                .filter((c, idx, arr) => {
                  if ((idx === 0 || idx === arr.length - 1) && c === "") return false;
                  return true;
                });
            } else {
              return r.split(",").map((c) => c.trim());
            }
          }).filter((r) => r.length > 0);

          const contentRows = rawRows.filter((r) => !r.every(isMarkdownDividerCell));
          if (contentRows.length > 0) {
            const headers = contentRows[0] || ["Item", "Details"];
            const rows = contentRows.slice(1);
            elements.push(
              <PeldrunMotionWrapper key={`tbl-md-${elements.length}`}>
                <PeldrunTable headers={headers} rows={rows} />
              </PeldrunMotionWrapper>
            );
          }
        }
        tableLines = [];
        inTableContext = false;
      }
    };

    const flushAllBuffers = () => {
      flushCard();
      flushForm();
      flushAccordion();
      flushDiagram();
      flushCarousel();
      flushMetrics();
      flushBadges();
      flushTable();
    };

    const createSingleMetric = (content: string, keyIdx: number): React.JSX.Element => {
      let label = "Metric";
      let val = "0";
      let change: string | undefined = undefined;
      let trend: "up" | "down" | "neutral" = "neutral";

      if (content.includes("|")) {
        const parts = content.split("|").map((p) => p.trim());
        label = parts[0] || "Metric";
        val = parts[1] || "0";
        change = parts[2] || undefined;
        const t = (parts[3] || "").toLowerCase();
        trend =
          t.includes("up") || t.includes("↑")
            ? "up"
            : t.includes("down") || t.includes("↓")
            ? "down"
            : "neutral";
      } else {
        const segs = content
          .replace(/:/g, " | ")
          .replace(/[–—]/g, " | ")
          .replace(/ - /g, " | ")
          .split("|")
          .map((s) => s.trim())
          .filter(Boolean);

        label = segs[0] || "Metric";
        val = segs[1] || "0";

        for (let sIdx = 2; sIdx < segs.length; sIdx++) {
          const seg = segs[sIdx].toLowerCase();
          if (seg.includes("up") || seg.includes("↑")) {
            trend = "up";
          } else if (seg.includes("down") || seg.includes("↓")) {
            trend = "down";
          } else if (!change && !seg.includes("trend")) {
            change = segs[sIdx];
          }
        }

        const parenMatch = val.match(/\((.*?)\)/);
        if (parenMatch) {
          change = change || parenMatch[1];
          val = val.replace(/\(.*?\)/, "").trim();
        }
      }

      if (change && trend === "neutral") {
        if (/[↑+]|up|increase/i.test(change)) trend = "up";
        else if (/[↓-]|down|decrease|improve/i.test(change)) trend = "down";
      }

      return (
        <PeldrunMetric
          key={`metric-${keyIdx}`}
          label={label}
          value={val}
          change={change}
          trend={trend}
        />
      );
    };

    for (let index = 0; index < rawLines.length; index++) {
      const line = rawLines[index];

      // --- 1. Resilient Table Detection (Standard markdown, relaxed pipes, or lines under Table:) ---
      const hasMultiplePipes = (line.match(/\|/g) || []).length >= 1;
      const isTableDivider = /^[|\s:-]+$/.test(line) && line.includes("-");
      const isExplicitTableHeader = /^Table(?::|\s)/i.test(line);

      if (isExplicitTableHeader) {
        flushAllBuffers();
        inTableContext = true;
        const inlineContent = line.replace(/^Table(?::|\s)*/i, "").trim();
        if (inlineContent.includes("|") && inlineContent.length > 3) {
          tableLines.push(inlineContent);
        }
        continue;
      }

      if (hasMultiplePipes || isTableDivider || (inTableContext && line.includes(","))) {
        flushMetrics();
        flushBadges();
        tableLines.push(line);
        continue;
      } else if (tableLines.length > 0) {
        flushTable();
      }

      const cleanLine = line
        .replace(/^\s*[-*•]\s*/, "")
        .replace(/[*_]{1,3}/g, "")
        .trim();

      if (
        cleanLine.startsWith("<!--") ||
        /^(?:Status\s*Badges?|Metric\s*Components?|Executive\s*Summary):?$/i.test(cleanLine)
      ) {
        continue;
      }

      // --- 2. Mermaid Diagram Handling ---
      const isMermaidKeyword = /^(?:graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|gitGraph)\b/i.test(cleanLine);
      const isDiagramHeader = /^(?:Diagram|Workflow|Architecture)(?::|\s)/i.test(cleanLine) || /<Diagram\b/i.test(cleanLine);

      if ((isDiagramHeader || isMermaidKeyword) && allowedComponents.includes("Diagram")) {
        if (isDiagramHeader) {
          flushAllBuffers();
          const title = cleanLine
            .replace(/<Diagram\b[^>]*>/i, "")
            .replace(/^(?:Diagram|Workflow|Architecture)(?::|\s)*/i, "")
            .trim();
          currentDiagram = { title: title || "Workflow Diagram", codeLines: [] };
          continue;
        } else if (currentDiagram === null) {
          flushAllBuffers();
          currentDiagram = { title: "Workflow & Execution Flow", codeLines: [cleanLine] };
          continue;
        }
      }

      if (currentDiagram !== null) {
        if (/<\/Diagram>/i.test(cleanLine)) {
          flushDiagram();
          continue;
        }
        if (
          cleanLine.includes("-->") ||
          cleanLine.includes("---") ||
          cleanLine.includes("->>") ||
          cleanLine.includes("[") ||
          cleanLine.includes("(") ||
          isMermaidKeyword ||
          cleanLine.startsWith("subgraph") ||
          cleanLine.startsWith("end")
        ) {
          currentDiagram.codeLines.push(cleanLine);
          continue;
        } else {
          flushDiagram();
        }
      }

      // --- 3. Carousel Component Handling ---
      const isCarouselHeader = /^Carousel(?::|\s)/i.test(cleanLine) || /<Carousel\b/i.test(cleanLine);
      if (isCarouselHeader && allowedComponents.includes("Carousel")) {
        flushAllBuffers();
        const title = cleanLine.replace(/<Carousel\b[^>]*>/i, "").replace(/^Carousel(?::|\s)*/i, "").trim();
        currentCarousel = { title: title || "Feature Showcase", slides: [] };
        continue;
      }

      const isSlideLine = /^Slide(?::|\s)/i.test(cleanLine) || /<Slide\b/i.test(cleanLine);
      if (isSlideLine && allowedComponents.includes("Carousel")) {
        if (currentCarousel === null) {
          flushAllBuffers();
          currentCarousel = { title: "Comparative Showcase", slides: [] };
        }
        const slideRaw = cleanLine.replace(/<Slide\b[^>]*>/i, "").replace(/<\/Slide>/i, "").replace(/^Slide(?::|\s)*/i, "").trim();
        const parts = slideRaw.split("|").map((p) => p.trim());
        currentCarousel.slides.push({
          title: parts[0] || `Item ${currentCarousel.slides.length + 1}`,
          description: parts.length > 2 ? parts[1] : undefined,
          content: parts.length > 2 ? parts.slice(2).join(" | ") : (parts[1] || parts[0]),
        });
        continue;
      }

      if (/<\/Carousel>/i.test(cleanLine)) {
        flushCarousel();
        continue;
      }

      // --- 4. Mathematical Plots & Equations ---
      const isMathPlot = /^(?:MathPlot|Plot|Equation)(?::|\s)/i.test(cleanLine) || /<MathPlot\b/i.test(cleanLine);
      if (isMathPlot && allowedComponents.includes("MathPlot")) {
        flushAllBuffers();
        const rawContent = cleanLine
          .replace(/<MathPlot\b[^>]*>/i, "")
          .replace(/<\/MathPlot>/i, "")
          .replace(/^(?:MathPlot|Plot|Equation)(?::|\s)*/i, "")
          .trim();

        let eq: string | undefined = undefined;
        let fn: string | undefined = undefined;
        let range: [number, number] = [-10, 10];
        let title: string | undefined = undefined;

        if (cleanLine.toLowerCase().startsWith("equation:") || cleanLine.toLowerCase().startsWith("math:")) {
          eq = rawContent;
          title = "Mathematical Equation";
        } else {
          const parts = rawContent.split("|").map((p) => p.trim());
          fn = parts[0]?.replace(/^[yf]\([x]\)\s*=\s*/i, "").trim();
          for (let pIdx = 1; pIdx < parts.length; pIdx++) {
            const p = parts[pIdx];
            if (/range/i.test(p)) {
              const nums = p.match(/-?\d+/g);
              if (nums && nums.length >= 2) {
                range = [parseInt(nums[0], 10), parseInt(nums[1], 10)];
              }
            } else if (/title/i.test(p)) {
              title = p.replace(/^title[:\s]*/i, "").trim();
            }
          }
        }

        elements.push(
          <PeldrunMathPlot
            key={`math-${elements.length}`}
            title={title}
            equation={eq}
            functionFormula={fn}
            rangeX={range}
          />
        );
        continue;
      }

      // --- 5. Advanced Shadcn Chart Component (Robust Data Extraction) ---
      if (/(?:^Chart|<Chart\b)/i.test(cleanLine) && allowedComponents.includes("Chart")) {
        flushAllBuffers();
        const lowerLine = cleanLine.toLowerCase();
        let chartType: ChartVariant = "area";
        if (lowerLine.includes("bar")) chartType = "bar";
        else if (lowerLine.includes("line")) chartType = "line";
        else if (lowerLine.includes("donut") || lowerLine.includes("pie")) chartType = "donut";

        let chartTitle = "Performance Analytics";
        let chartUnit = "";
        let rawDataSegment = "";

        const metaParts = cleanLine.split("|").map((p) => p.trim());
        for (const part of metaParts) {
          if (/^title[:\s]*/i.test(part)) {
            chartTitle = part.replace(/^title[:\s]*/i, "").trim();
          } else if (/^unit[:\s]*/i.test(part)) {
            chartUnit = part.replace(/^unit[:\s]*/i, "").trim();
          } else if (/^values?[:\s]*/i.test(part)) {
            rawDataSegment = part.replace(/^values?[:\s]*/i, "").trim();
          } else if (!rawDataSegment && (part.includes(":") || part.includes(","))) {
            const cleaned = part
              .replace(/<Chart\b[^>]*>/i, "")
              .replace(/^Chart(?:\s*\[.*?\])?(?::|\s)*/i, "")
              .trim();
            if (cleaned) rawDataSegment = cleaned;
          }
        }

        const dataPoints: { label: string; value: number }[] = [];

        // Check if rawDataSegment is a list of numbers: "180, 260, 420..."
        if (rawDataSegment.includes(",") && !rawDataSegment.includes(":")) {
          const defaultLabels = ["10am", "11am", "12pm", "1pm", "2pm", "3pm", "4pm", "5pm"];
          const values = rawDataSegment.split(",").map((v) => extractNumericValue(v.trim()));
          values.forEach((val, idx) => {
            dataPoints.push({
              label: defaultLabels[idx] || `Point ${idx + 1}`,
              value: val,
            });
          });
        } else {
          // Standard pairs: "Label1:Val1, Label2:Val2"
          const pairs = rawDataSegment.split(",").map((p) => p.trim()).filter(Boolean);
          pairs.forEach((pair, idx) => {
            const pParts = pair.split(":");
            const rawLabel = (pParts[0] || "").replace(/\[.*?\]/g, "").replace(/^label\s*/i, "").trim();
            const rawVal = pParts[1] || "";
            dataPoints.push({
              label: rawLabel || `Point ${idx + 1}`,
              value: extractNumericValue(rawVal),
            });
          });
        }

        if (dataPoints.length > 0) {
          elements.push(
            <PeldrunShadcnChart
              key={`chart-${index}`}
              title={chartTitle}
              type={chartType}
              data={dataPoints}
              unit={chartUnit}
            />
          );
        }
        continue;
      }

      // --- 6. XML / JSX Tag Handling (<Card>, <Metric />, <Badge />, <Alert />) ---
      const xmlCardMatch = cleanLine.match(/<Card\b([^>]*)>/i);
      if (xmlCardMatch && allowedComponents.includes("Card")) {
        flushAllBuffers();
        const attrs = parseXmlAttributes(xmlCardMatch[1]);
        currentCard = { title: attrs.title || undefined, desc: undefined, items: [] };
        continue;
      }
      if (/<\/Card>/i.test(cleanLine)) {
        flushCard();
        continue;
      }

      const xmlMetricMatch = cleanLine.match(/<Metric\b([^>]*)\/?>/i);
      if (xmlMetricMatch && allowedComponents.includes("Metric")) {
        flushCard();
        flushBadges();
        const attrs = parseXmlAttributes(xmlMetricMatch[1]);
        const label = attrs.name || attrs.label || "Metric";
        const val = attrs.value || "0";
        const change = attrs.change || undefined;
        const trendRaw = (attrs.trend || "").toLowerCase();
        const trend: "up" | "down" | "neutral" =
          trendRaw.includes("up") || trendRaw.includes("↑")
            ? "up"
            : trendRaw.includes("down") || trendRaw.includes("↓")
            ? "down"
            : "neutral";

        metricGroup.push(
          <PeldrunMetric
            key={`metric-${index}`}
            label={label}
            value={val}
            change={change}
            trend={trend}
          />
        );
        continue;
      }

      const xmlBadgeMatch = cleanLine.match(/<Badge\b([^>]*)\/?>/i);
      if (xmlBadgeMatch && allowedComponents.includes("Badge")) {
        flushCard();
        flushMetrics();
        const attrs = parseXmlAttributes(xmlBadgeMatch[1]);
        const service = attrs.service || attrs.label || attrs.name || "Service";
        const rawStatus = (attrs.status || attrs.variant || "").toLowerCase();

        let variant: "info" | "success" | "warning" | "error" | "neutral" = "neutral";
        if (
          rawStatus.includes("operational") ||
          rawStatus.includes("healthy") ||
          rawStatus.includes("success") ||
          rawStatus.includes("✅")
        ) {
          variant = "success";
        } else if (rawStatus.includes("warning") || rawStatus.includes("⚠️")) {
          variant = "warning";
        } else if (
          rawStatus.includes("error") ||
          rawStatus.includes("failed") ||
          rawStatus.includes("❌")
        ) {
          variant = "error";
        } else if (rawStatus.includes("standby") || rawStatus.includes("neutral") || rawStatus.includes("idle")) {
          variant = "neutral";
        }

        const displayLabel = attrs.status ? `${service}: ${attrs.status}` : service;
        badgeGroup.push(
          <PeldrunBadge key={`badge-${index}`} label={displayLabel} variant={variant} />
        );
        continue;
      }

      // --- 7. TestResults Detection ---
      const xmlTestMatch = cleanLine.match(/<TestResults\b([^>]*)>(.*?)<\/TestResults>/i) || cleanLine.match(/<TestResults\b([^>]*)\/?>/i);
      const isLineTest = /^TestResults(?::|\s)/i.test(cleanLine);

      if ((xmlTestMatch || isLineTest) && allowedComponents.includes("TestResults")) {
        flushAllBuffers();
        const contentStr = xmlTestMatch ? (xmlTestMatch[2] || xmlTestMatch[1] || "") : cleanLine;

        let passed = 0;
        let failed = 0;
        let skipped = 0;

        const passMatch = contentStr.match(/(?:passed|pass)[:=\s]*(\d+)/i);
        const failMatch = contentStr.match(/(?:failed|fail)[:=\s]*(\d+)/i);
        const skipMatch = contentStr.match(/(?:skipped|skip)[:=\s]*(\d+)/i);

        if (passMatch) passed = parseInt(passMatch[1], 10);
        if (failMatch) failed = parseInt(failMatch[1], 10);
        if (skipMatch) skipped = parseInt(skipMatch[1], 10);

        elements.push(
          <PeldrunMotionWrapper key={`tests-${index}`}>
            <PeldrunTestResults
              passedCount={passed}
              failedCount={failed}
              skippedCount={skipped}
            />
          </PeldrunMotionWrapper>
        );
        continue;
      }

      // --- 8. Alert Component ---
      const isXmlAlert = cleanLine.match(/<Alert\b([^>]*)>(.*?)<\/Alert>/i);
      const isLineAlert = /^Alert(?:\s*\[|\s*\(|\s*:|\s+warning|\s+error|\s+info|\s+success)/i.test(cleanLine);

      if ((isXmlAlert || isLineAlert) && allowedComponents.includes("Alert")) {
        flushAllBuffers();
        let alertType: "info" | "success" | "warning" | "error" = "info";
        let message = cleanLine;

        if (isXmlAlert) {
          const attrs = parseXmlAttributes(isXmlAlert[1]);
          alertType = (attrs.type as any) || "info";
          message = isXmlAlert[2] || attrs.message || "";
        } else {
          const match = cleanLine.match(/^Alert(?:\s*\[(info\vert{}success\vert{}warning\vert{}error)\]|\s*\((info\vert{}success\vert{}warning\vert{}error)\)|\s+(info|success|warning|error))?[:\s]*(.*)$/i);
          const rawType = (match?.[1] || match?.[2] || match?.[3] || "info").toLowerCase();
          alertType = (rawType as any) || "info";
          message = match?.[4] || cleanLine;
        }

        elements.push(
          <PeldrunMotionWrapper key={`alert-${index}`}>
            <PeldrunAlert message={message} type={alertType} />
          </PeldrunMotionWrapper>
        );
        continue;
      }

      // --- 9. Form Component ---
      const xmlFormMatch = cleanLine.match(/<Form\b([^>]*)>/i);
      const isLineForm = /^Form(?::|\s)/i.test(cleanLine);

      if ((xmlFormMatch || isLineForm) && allowedComponents.includes("Form")) {
        flushAllBuffers();
        let formTitle = "Form";
        let inlineFields: FormFieldDefinition[] = [];

        if (xmlFormMatch) {
          const attrs = parseXmlAttributes(xmlFormMatch[1]);
          formTitle = attrs.title || "Form";
        } else {
          const raw = cleanLine.replace(/^Form(?::|\s)*/i, "").trim();
          const parts = raw.split("|").map((p) => p.trim());
          formTitle = parts[0] || "Form";
          if (parts[1]) {
            inlineFields = parts[1].split(",").map((f) => ({
              name: f.trim().toLowerCase().replace(/\s+/g, "_"),
              label: f.trim(),
            }));
          }
        }

        currentForm = {
          title: formTitle,
          fields: inlineFields,
        };
        continue;
      }

      const xmlFieldMatch = cleanLine.match(/<Field\b([^>]*)\/?>/i);
      const isLineField = /^Field(?::|\s)/i.test(cleanLine);

      if ((xmlFieldMatch || isLineField) && currentForm !== null) {
        if (xmlFieldMatch) {
          const attrs = parseXmlAttributes(xmlFieldMatch[1]);
          currentForm.fields.push({
            name: attrs.name || `field_${currentForm.fields.length}`,
            label: attrs.label || attrs.name || "Field",
            type: (attrs.type as any) || "text",
            placeholder: attrs.placeholder,
          });
        } else {
          const rawField = cleanLine.replace(/^Field(?::|\s)*/i, "").trim();
          const fieldLabel = rawField.replace(/\(.*?\)/g, "").trim();
          const parenMatch = rawField.match(/\((.*?)\)/);
          currentForm.fields.push({
            name: fieldLabel.toLowerCase().replace(/[^a-z0-9_]/g, "_"),
            label: fieldLabel || "Field",
            placeholder: parenMatch ? parenMatch[1] : undefined,
          });
        }
        continue;
      }

      // --- 10. Line Card Detection ---
      if (
        /^(?:Card|Executive\s*Summary\s*Card|Server\s*Status\s*Card)(?::|\s)/i.test(cleanLine) &&
        allowedComponents.includes("Card")
      ) {
        flushAllBuffers();
        const title = cleanLine
          .replace(/^(?:Card|Executive\s*Summary\s*Card|Server\s*Status\s*Card)(?::|\s)*/i, "")
          .trim();
        currentCard = { title: title || undefined, desc: undefined, items: [] };
        continue;
      }

      // --- 11. Multi-Metric Single-Line Detection ---
      const pipeSegments = cleanLine.split("|").map((s) => s.trim());
      const isMultiMetricLine =
        pipeSegments.length > 1 &&
        pipeSegments.every((s) => s.includes(":")) &&
        pipeSegments.some((s) => /^(?:CPU|RAM|VRAM|P95|Latency|Memory|Disk|Throughput)\b/i.test(s));

      if (isMultiMetricLine && allowedComponents.includes("Metric")) {
        flushCard();
        flushBadges();
        pipeSegments.forEach((seg, sIdx) => {
          metricGroup.push(createSingleMetric(seg, index * 10 + sIdx));
        });
        continue;
      }

      // --- 12. Single Metric Detection ---
      const isExplicitMetric = /^Metric(?:\s*\d+)?(?::|\s)/i.test(cleanLine);
      const isNamedMetric =
        /^(?:CPU|RAM|VRAM|P95|Latency|Memory|Disk|Throughput)\b/i.test(cleanLine) &&
        cleanLine.includes(":");

      if ((isExplicitMetric || isNamedMetric) && allowedComponents.includes("Metric")) {
        flushCard();
        flushBadges();
        const content = isExplicitMetric
          ? cleanLine.replace(/^Metric(?:\s*\d+)?(?::|\s)*/i, "").trim()
          : cleanLine.trim();

        metricGroup.push(createSingleMetric(content, index));
        continue;
      } else {
        flushMetrics();
      }

      // --- 13. Badge Detection ---
      const isExplicitBadge = /^Badge(?:\s*\d+)?(?::|\s)/i.test(cleanLine);
      const isStatusItem =
        !cleanLine.toLowerCase().startsWith("alert") &&
        cleanLine.includes(":") &&
        (/[✅⚠️🟢❌🔴➖]/i.test(cleanLine) ||
          /Operational|Healthy|Warning|Neutral|Standby|Idle/i.test(cleanLine));

      if ((isExplicitBadge || isStatusItem) && allowedComponents.includes("Badge")) {
        flushCard();
        flushMetrics();

        const badgeContent = cleanLine
          .replace(/^Badge(?:\s*\d+)?(?::|\s)*/i, "")
          .trim();
        const parts = badgeContent
          .replace(/:/g, "|")
          .replace(/[–—]/g, "|")
          .split("|")
          .map((p) => p.trim());

        const label = parts[0] || "Service";
        const rawStatus = (parts.slice(1).join(" ") || "").toLowerCase();

        let variant: "info" | "success" | "warning" | "error" | "neutral" = "neutral";
        if (
          rawStatus.includes("success") ||
          rawStatus.includes("healthy") ||
          rawStatus.includes("operational") ||
          rawStatus.includes("✅")
        ) {
          variant = "success";
        } else if (rawStatus.includes("warning") || rawStatus.includes("⚠️")) {
          variant = "warning";
        } else if (
          rawStatus.includes("error") ||
          rawStatus.includes("failed") ||
          rawStatus.includes("❌") ||
          rawStatus.includes("🔴")
        ) {
          variant = "error";
        } else if (
          rawStatus.includes("info") ||
          rawStatus.includes("standby") ||
          rawStatus.includes("neutral") ||
          rawStatus.includes("idle") ||
          rawStatus.includes("🟢")
        ) {
          variant = "neutral";
        }

        const displayLabel = parts[1] ? `${label}: ${parts[1]}` : label;
        badgeGroup.push(
          <PeldrunBadge key={`badge-${index}`} label={displayLabel} variant={variant} />
        );
        continue;
      } else {
        flushBadges();
      }

      // --- 14. Progress Component ---
      if (/^Progress(?::|\s)/i.test(cleanLine) && allowedComponents.includes("Progress")) {
        flushAllBuffers();
        const parts = cleanLine.replace(/^Progress(?::|\s)*/i, "").split("|").map((p) => p.trim());
        const label = parts[0] || "";
        const pct = extractNumericValue(parts[1] || parts[0] || "0");
        elements.push(
          <PeldrunMotionWrapper key={`progress-${index}`}>
            <PeldrunProgress label={label} percentage={pct} />
          </PeldrunMotionWrapper>
        );
        continue;
      }

      // --- 15. Accordion Component ---
      if (/^Accordion(?::|\s)/i.test(cleanLine) && allowedComponents.includes("Accordion")) {
        flushAllBuffers();
        const raw = cleanLine.replace(/^Accordion(?::|\s)*/i, "").trim();
        const parts = raw.split("|").map((p) => p.trim());
        const items = parts.map((it) => {
          const segs = it.split(":");
          return { title: segs[0] || "Details", content: segs.slice(1).join(":") || "" };
        });
        currentAccordion = { items };
        continue;
      }

      // --- 16. Action Button ---
      if (
        /^(?:Action|ActionButton)(?::|\s)/i.test(cleanLine) &&
        allowedComponents.includes("ActionButton")
      ) {
        const actionLabel = cleanLine.replace(/^(?:Action|ActionButton)(?::|\s)*/i, "").trim();
        if (currentForm !== null) {
          currentForm.submitLabel = actionLabel;
          flushForm();
        } else {
          flushAllBuffers();
          elements.push(
            <PeldrunMotionWrapper key={`act-wrapper-${index}`}>
              <div className="my-1.5 flex gap-2">
                <PeldrunActionButton key={`act-btn-${index}`} label={actionLabel} actionId={`act_${index}`} />
              </div>
            </PeldrunMotionWrapper>
          );
        }
        continue;
      }

      // Fallback
      const strippedHtmlLine = cleanLine.replace(/<\/?(?:p|b|i|span|ul|li)[^>]*>/gi, "").trim();
      if (strippedHtmlLine) {
        if (currentCard !== null) {
          if (!currentCard.desc) {
            currentCard.desc = strippedHtmlLine;
          } else {
            currentCard.items.push(strippedHtmlLine);
          }
        } else if (currentAccordion !== null) {
          currentAccordion.items.push({ title: "Details", content: strippedHtmlLine });
        } else {
          flushAllBuffers();
          elements.push(
            <p key={`p-${index}`} className="text-xs text-foreground/90 my-1 leading-relaxed">
              {strippedHtmlLine}
            </p>
          );
        }
      }
    }

    flushAllBuffers();
    return elements;
  }, [dslContent, allowedComponents]);

  return <div className="space-y-2 py-1">{parsedElements}</div>;
}
