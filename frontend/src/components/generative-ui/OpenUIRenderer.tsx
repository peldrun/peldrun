// frontend/src/components/generative-ui/OpenUIRenderer.tsx
"use client";

import React, { useMemo } from "react";
import dynamic from "next/dynamic";
import { useGenerativeUIStore } from "./store";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
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
import type { CarouselSlide } from "./catalog/PeldrunCarousel";
import type { FormFieldDefinition } from "./types";

const LazyDiagram = dynamic(
  () => import("./catalog/PeldrunDiagram").then((m) => m.PeldrunDiagram),
  {
    loading: () => (
      <div className="my-2.5 rounded-xl border border-border/80 bg-card/50 p-6 flex items-center justify-center gap-2 text-xs text-muted-foreground animate-pulse">
        <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span>Loading workflow diagram engine...</span>
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

const LazyChart = dynamic(
  () => import("./catalog/PeldrunChart").then((m) => m.PeldrunChart),
  {
    loading: () => (
      <div className="my-2 rounded-xl border border-border/80 bg-card/50 p-6 flex items-center justify-center gap-2 text-xs text-muted-foreground animate-pulse">
        <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span>Rendering Recharts analytics canvas...</span>
      </div>
    ),
    ssr: false,
  }
);

const LazyCarousel = dynamic(
  () => import("./catalog/PeldrunCarousel").then((m) => m.PeldrunCarousel),
  {
    loading: () => (
      <div className="my-2.5 rounded-xl border border-border/80 bg-card/50 p-6 flex items-center justify-center gap-2 text-xs text-muted-foreground animate-pulse">
        <span className="w-3.5 h-3.5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        <span>Loading interactive showcase...</span>
      </div>
    ),
    ssr: false,
  }
);

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

function splitEscapedPipes(str: string): string[] {
  return str.split(/(?<!\\)\|/).map((s) => s.replace(/\\\|/g, "|").trim());
}

/**
 * Normalizes lines emitted with bracketed component tags by small LLMs.
 * Example: `[Chart line] Title: ...` -> `Chart line: Title: ...`
 */
function normalizeBracketedLine(raw: string): string {
  return raw.replace(
    /^\[(Card\vert{}Chart\vert{}Table\vert{}Metric\vert{}Badge\vert{}Alert\vert{}Progress\vert{}Form\vert{}Accordion\vert{}Diagram\vert{}Carousel\vert{}MathPlot)([^\]]*)\](?::|\s)*/i,
    "$1$2: "
  );
}

export function OpenUIRenderer({ dslContent }: OpenUIRendererProps): React.JSX.Element {
  const allowedComponents = useGenerativeUIStore((s) => (s as any).allowedComponents || []);
  const isEnabled = useGenerativeUIStore(
    (s) => (s as any).enabled ?? (s as any).isEnabled ?? true
  );

  const parsedElements = useMemo(() => {
    if (isEnabled === false) {
      const sanitizedMarkdown = dslContent
        .replace(/<\/?(?:openui(?:-lang)?|ui|diagram|carousel|mathplot)\b[^>]*>/gi, "")
        .trim();
      return [
        <div key="plain-markdown" className="my-2 leading-relaxed">
          <MarkdownRenderer content={sanitizedMarkdown} />
        </div>,
      ];
    }

    const rawLines = dslContent.split("\n").map((l) => l.trim()).filter(Boolean);
    const elements: React.JSX.Element[] = [];

    let currentCard: CardBuffer | null = null;
    let currentForm: FormBuffer | null = null;
    let currentAccordion: AccordionBuffer | null = null;
    let currentDiagram: DiagramBuffer | null = null;
    let currentCarousel: CarouselBuffer | null = null;
    let metricGroup: React.JSX.Element[] = [];
    let badgeGroup: React.JSX.Element[] = [];
    let actionGroup: React.JSX.Element[] = [];
    let proseLines: string[] = [];
    let tableLines: string[] = [];
    let inTableContext = false;

    const flushProse = () => {
      if (proseLines.length > 0) {
        const md = proseLines.join("\n\n");
        elements.push(
          <div key={`prose-${elements.length}`} className="my-2 leading-relaxed">
            <MarkdownRenderer content={md} />
          </div>
        );
        proseLines = [];
      }
    };

    const flushCard = () => {
      if (currentCard !== null) {
        const c = currentCard;
        elements.push(
          <PeldrunMotionWrapper key={`card-${elements.length}`}>
            <PeldrunCard title={c.title} description={c.desc}>
              {c.items.length > 0 && (
                <div className="space-y-1 mt-1.5">
                  {c.items.map((it, i) => (
                    <div key={`ci-${i}`} className="text-muted-foreground text-xs leading-relaxed">
                      <MarkdownRenderer content={it} />
                    </div>
                  ))}
                </div>
              )}
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
            <LazyDiagram
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
          <LazyCarousel
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

    const flushActions = () => {
      if (actionGroup.length > 0) {
        elements.push(
          <PeldrunMotionWrapper key={`actions-row-${elements.length}`}>
            <div className="flex flex-wrap items-center gap-2 my-2">
              {[...actionGroup]}
            </div>
          </PeldrunMotionWrapper>
        );
        actionGroup = [];
      }
    };

    const flushTable = () => {
      if (tableLines.length > 0) {
        if (allowedComponents.length === 0 || allowedComponents.includes("Table")) {
          const rawRows = tableLines
            .map((r) => {
              if (r.includes("|")) {
                return splitEscapedPipes(r).filter((c, idx, arr) => {
                  if ((idx === 0 || idx === arr.length - 1) && c === "") return false;
                  return true;
                });
              } else {
                return r.split(",").map((c) => c.trim());
              }
            })
            .filter((r) => r.length > 0);

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
      flushProse();
      flushCard();
      flushForm();
      flushAccordion();
      flushDiagram();
      flushCarousel();
      flushMetrics();
      flushBadges();
      flushActions();
      flushTable();
    };

    const createSingleMetric = (content: string, keyIdx: number): React.JSX.Element => {
      let label = "Metric";
      let val = "0";
      let change: string | undefined = undefined;
      let trend: "up" | "down" | "neutral" = "neutral";

      if (content.includes("|")) {
        const parts = splitEscapedPipes(content);
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
      let cleanLine = normalizeBracketedLine(line.trim());

      // Catch and resolve closing XML tags
      if (/^<\/[a-zA-Z0-9_-]+>$/i.test(cleanLine)) {
        const closingTag = cleanLine.slice(2, -1).toLowerCase();
        if (closingTag === "table") flushTable();
        else if (closingTag === "card") flushCard();
        else if (closingTag === "form") flushForm();
        else if (closingTag === "accordion") flushAccordion();
        else if (closingTag === "carousel") flushCarousel();
        else if (closingTag === "diagram") flushDiagram();
        continue;
      }

      if (cleanLine.startsWith("<!--") || /^<\/?(?:openui|ui)\b/i.test(cleanLine)) {
        continue;
      }

      const xmlTitleMatch = cleanLine.match(/<Title\b[^>]*>(.*?)<\/Title>/i);
      if (xmlTitleMatch) {
        flushAllBuffers();
        currentCard = { title: xmlTitleMatch[1].trim(), desc: undefined, items: [] };
        continue;
      }

      if (currentDiagram !== null) {
        if (/<\/Diagram>/i.test(cleanLine)) {
          flushDiagram();
          continue;
        }
        const isNewComponentPrefix = /^(?:Card|Metric|Badge|Alert|Progress|Table|Chart|Carousel|TestResults|Accordion|Form|Field|Plot|MathPlot|Equation|ActionButton|Action)(?::|\s|\[)/i.test(cleanLine);
        if (isNewComponentPrefix) {
          flushDiagram();
        } else {
          currentDiagram.codeLines.push(cleanLine);
          continue;
        }
      }

      // --- 1. Card Component ---
      const xmlCardMatch = cleanLine.match(/<Card\b([^>]*)>/i);
      const isLineCard = /^(?:Card|Executive\s*Summary\s*Card|Server\s*Status\s*Card)(?::|\s)/i.test(cleanLine);
      if ((xmlCardMatch || isLineCard) && (allowedComponents.length === 0 || allowedComponents.includes("Card"))) {
        flushAllBuffers();
        if (xmlCardMatch) {
          const attrs = parseXmlAttributes(xmlCardMatch[1]);
          currentCard = { title: attrs.title || undefined, desc: undefined, items: [] };
        } else {
          const title = cleanLine
            .replace(/^(?:Card|Executive\s*Summary\s*Card|Server\s*Status\s*Card)(?::|\s)*/i, "")
            .trim();
          currentCard = { title: title || undefined, desc: undefined, items: [] };
        }
        continue;
      }

      // --- 2. Alert Component ---
      const isXmlAlert = cleanLine.match(/<Alert\b([^>]*)>(.*?)<\/Alert>/i);
      const isLineAlert = /^Alert(?:\s*\[|\s*\(|\s*:|\s+warning|\s+error|\s+info|\s+success)/i.test(cleanLine);
      if ((isXmlAlert || isLineAlert) && (allowedComponents.length === 0 || allowedComponents.includes("Alert"))) {
        flushAllBuffers();
        let alertType: "info" | "success" | "warning" | "error" = "info";
        let message = cleanLine;

        if (isXmlAlert) {
          const attrs = parseXmlAttributes(isXmlAlert[1]);
          alertType = (attrs.type as any) || "info";
          message = isXmlAlert[2] || attrs.message || "";
        } else {
          const match = cleanLine.match(
            /^Alert(?:\s*(?:\[\[?|\()([a-zA-Z]+)(?:\]\]?\vert{}\))|\s+([a-zA-Z]+))?[:\s]*(.*)$/i
          );
          const rawType = (match?.[1] || match?.[2] || "info").toLowerCase();
          if (rawType === "success" || rawType === "warning" || rawType === "error") {
            alertType = rawType;
          } else {
            alertType = "info";
          }
          message = match?.[3] || cleanLine.replace(/^Alert(?::|\s)*/i, "").trim();
        }

        elements.push(
          <PeldrunMotionWrapper key={`alert-${index}`}>
            <PeldrunAlert message={message} type={alertType} />
          </PeldrunMotionWrapper>
        );
        continue;
      }

      // --- 3. Metric Component ---
      const xmlMetricMatch = cleanLine.match(/<Metric\b([^>]*)\/?>/i);
      const isExplicitMetric = /^Metric(?:\s*\d+)?(?::|\s)/i.test(cleanLine);
      const isNamedMetric =
        /^(?:CPU|RAM|VRAM|P95|Latency|Memory|Disk|Throughput)\b/i.test(cleanLine) &&
        cleanLine.includes(":");
      const pipeSegments = splitEscapedPipes(cleanLine);
      const isMultiMetricLine =
        pipeSegments.length > 1 &&
        pipeSegments.every((s) => s.includes(":")) &&
        pipeSegments.some((s) => /^(?:CPU|RAM|VRAM|P95|Latency|Memory|Disk|Throughput)\b/i.test(s));

      if (
        (xmlMetricMatch || isExplicitMetric || isNamedMetric || isMultiMetricLine) &&
        (allowedComponents.length === 0 || allowedComponents.includes("Metric"))
      ) {
        if (xmlMetricMatch) {
          const attrs = parseXmlAttributes(xmlMetricMatch[1]);
          if (!attrs.name && !attrs.label && !attrs.value) {
            continue;
          }
          flushProse();
          flushCard();
          flushBadges();
          flushActions();
          flushTable();

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
            <PeldrunMetric key={`metric-${index}`} label={label} value={val} change={change} trend={trend} />
          );
        } else {
          flushProse();
          flushCard();
          flushBadges();
          flushActions();
          flushTable();

          if (isMultiMetricLine) {
            pipeSegments.forEach((seg, sIdx) => {
              metricGroup.push(createSingleMetric(seg, index * 10 + sIdx));
            });
          } else {
            const content = isExplicitMetric
              ? cleanLine.replace(/^Metric(?:\s*\d+)?(?::|\s)*/i, "").trim()
              : cleanLine.trim();
            metricGroup.push(createSingleMetric(content, index));
          }
        }
        continue;
      } else {
        flushMetrics();
      }

      // --- 4. Badge Component ---
      const xmlBadgeMatch = cleanLine.match(/<Badge\b([^>]*)>/i);
      const isExplicitBadge = /^Badge(?:\s*\d+)?(?::|\s)/i.test(cleanLine);

      const colonIdx = cleanLine.indexOf(":");
      const isStatusItem =
        colonIdx > 0 &&
        colonIdx < 35 &&
        cleanLine.length - colonIdx < 40 &&
        !cleanLine.slice(0, colonIdx).includes(".") &&
        !cleanLine.toLowerCase().startsWith("alert") &&
        !cleanLine.toLowerCase().startsWith("note") &&
        (/[✅⚠️🟢❌🔴➖]/i.test(cleanLine) ||
          /\b(?:operational|healthy|warning|neutral|standby|idle|active|online|offline|degraded)\b/i.test(
            cleanLine
          ));

      if (
        (xmlBadgeMatch || isExplicitBadge || isStatusItem) &&
        (allowedComponents.length === 0 || allowedComponents.includes("Badge"))
      ) {
        flushProse();
        flushCard();
        flushMetrics();
        flushActions();
        flushTable();

        if (xmlBadgeMatch) {
          const attrs = parseXmlAttributes(xmlBadgeMatch[1]);
          const service = attrs.service || attrs.label || attrs.name || "Service";
          const rawStatus = (attrs.status || attrs.variant || "").toLowerCase();
          let variant: "info" | "success" | "warning" | "error" | "neutral" = "neutral";
          if (rawStatus.includes("operational") || rawStatus.includes("healthy") || rawStatus.includes("success") || rawStatus.includes("✅")) {
            variant = "success";
          } else if (rawStatus.includes("warning") || rawStatus.includes("⚠️")) {
            variant = "warning";
          } else if (rawStatus.includes("error") || rawStatus.includes("failed") || rawStatus.includes("❌")) {
            variant = "error";
          }
          const displayLabel = attrs.status ? `${service}: ${attrs.status}` : service;
          badgeGroup.push(<PeldrunBadge key={`badge-${index}`} label={displayLabel} variant={variant} />);
        } else {
          const badgeContent = cleanLine.replace(/^Badge(?:\s*\d+)?(?::|\s)*/i, "").trim();
          const parts = splitEscapedPipes(badgeContent.replace(/:/g, "|"));
          const label = parts[0] || "Service";
          const rawStatus = (parts.slice(1).join(" ") || "").toLowerCase();

          let variant: "info" | "success" | "warning" | "error" | "neutral" = "neutral";
          if (rawStatus.includes("success") || rawStatus.includes("healthy") || rawStatus.includes("operational") || rawStatus.includes("✅")) {
            variant = "success";
          } else if (rawStatus.includes("warning") || rawStatus.includes("⚠️")) {
            variant = "warning";
          } else if (rawStatus.includes("error") || rawStatus.includes("failed") || rawStatus.includes("❌") || rawStatus.includes("🔴")) {
            variant = "error";
          }
          const displayLabel = parts[1] ? `${label}: ${parts[1]}` : label;
          badgeGroup.push(<PeldrunBadge key={`badge-${index}`} label={displayLabel} variant={variant} />);
        }
        continue;
      } else {
        flushBadges();
      }

      // --- 5. Progress Component ---
      if (
        /^Progress(?::|\s)/i.test(cleanLine) &&
        (allowedComponents.length === 0 || allowedComponents.includes("Progress"))
      ) {
        flushAllBuffers();
        const parts = splitEscapedPipes(cleanLine.replace(/^Progress(?::|\s)*/i, ""));
        const label = parts[0] || "";
        const pct = extractNumericValue(parts[1] || parts[0] || "0");
        elements.push(
          <PeldrunMotionWrapper key={`progress-${index}`}>
            <PeldrunProgress label={label} percentage={pct} />
          </PeldrunMotionWrapper>
        );
        continue;
      }

      // --- 6. Chart Component ---
      const isChartHeader = /(?:^Chart|<Chart\b)/i.test(cleanLine);
      const isImplicitChartLine =
        !cleanLine.startsWith("|") &&
        cleanLine.includes("Title:") &&
        cleanLine.includes("Unit:") &&
        cleanLine.includes(":");

      if (
        (isChartHeader || isImplicitChartLine) &&
        (allowedComponents.length === 0 || allowedComponents.includes("Chart"))
      ) {
        flushAllBuffers();
        let chartType: "bar" | "line" | "donut" | "area" | "pie" = "line";

        const typeMatch = cleanLine.match(
          /Chart(?:\s*(?:\[\[?|\()([a-zA-Z]+)(?:\]\]?\vert{}\))|\s*:?\s*([a-zA-Z]+))/i
        );
        const extracted = (typeMatch?.[1] || typeMatch?.[2] || "").toLowerCase();
        if (
          extracted === "line" ||
          extracted === "donut" ||
          extracted === "pie" ||
          extracted === "area" ||
          extracted === "bar"
        ) {
          chartType = extracted as any;
        } else {
          const lowerLine = cleanLine.toLowerCase();
          if (lowerLine.includes("bar")) chartType = "bar";
          else if (lowerLine.includes("donut") || lowerLine.includes("pie")) chartType = "donut";
          else if (lowerLine.includes("area")) chartType = "area";
          else chartType = "line";
        }

        let chartTitle = "Performance Analytics";
        let chartUnit = "";
        let rawDataSegment = "";

        const metaParts = splitEscapedPipes(cleanLine);
        for (const part of metaParts) {
          if (/^title[:=\s]*/i.test(part)) {
            chartTitle = part.replace(/^title[:=\s]*/i, "").trim();
          } else if (/^unit[:=\s]*/i.test(part)) {
            chartUnit = part.replace(/^unit[:=\s]*/i, "").trim();
          } else if (/^values?[:=\s]*/i.test(part)) {
            rawDataSegment = part.replace(/^values?[:=\s]*/i, "").trim();
          } else if (!rawDataSegment && (part.includes(":") || part.includes(","))) {
            const cleaned = part
              .replace(/<Chart\b[^>]*>/i, "")
              .replace(/^Chart(?:\s*(?:\[\[?|\().*?(?:\]\]?\vert{}\)))?(?::|\s)*/i, "")
              .trim();
            if (
              cleaned &&
              !cleaned.toLowerCase().startsWith("title") &&
              !cleaned.toLowerCase().startsWith("unit")
            ) {
              rawDataSegment = cleaned;
            }
          }
        }

        const dataPoints: { label: string; value: number }[] = [];
        const pairs = rawDataSegment.split(",").map((p) => p.trim()).filter(Boolean);
        pairs.forEach((pair, idx) => {
          const lastColonIdx = pair.lastIndexOf(":");
          const rawLabel = lastColonIdx > -1 ? pair.slice(0, lastColonIdx).trim() : pair;
          const rawVal = lastColonIdx > -1 ? pair.slice(lastColonIdx + 1).trim() : "";
          const numVal = extractNumericValue(rawVal);
          if (rawLabel) {
            dataPoints.push({
              label: rawLabel.replace(/^(?:label\d*[:=\s]*)/i, "") || `Point ${idx + 1}`,
              value: numVal,
            });
          }
        });

        if (dataPoints.length > 0) {
          elements.push(
            <LazyChart
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

      // --- 7. Diagram Component ---
      const isMermaidKeyword =
        /^(?:graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|gitGraph)\b/i.test(
          cleanLine
        );
      const isDiagramHeader =
        /^(?:Diagram|Workflow|Architecture)(?::|\s)/i.test(cleanLine) || /<Diagram\b/i.test(cleanLine);

      if (
        (isDiagramHeader || isMermaidKeyword) &&
        (allowedComponents.length === 0 || allowedComponents.includes("Diagram"))
      ) {
        flushAllBuffers();
        const title = isDiagramHeader
          ? cleanLine
              .replace(/<Diagram\b[^>]*>/i, "")
              .replace(/^(?:Diagram|Workflow|Architecture)(?::|\s)*/i, "")
              .trim()
          : "Workflow Diagram";
        const initialLines = isDiagramHeader ? [] : [cleanLine];
        currentDiagram = { title: title || "Workflow Diagram", codeLines: initialLines };
        continue;
      }

      // --- 8. Carousel Component ---
      const isCarouselHeader = /^Carousel(?::|\s)/i.test(cleanLine) || /<Carousel\b/i.test(cleanLine);
      if (
        isCarouselHeader &&
        (allowedComponents.length === 0 || allowedComponents.includes("Carousel"))
      ) {
        flushAllBuffers();
        const title = cleanLine
          .replace(/<Carousel\b[^>]*>/i, "")
          .replace(/^Carousel(?::|\s)*/i, "")
          .trim();
        currentCarousel = { title: title || "Feature Showcase", slides: [] };
        continue;
      }

      const isSlideLine = /^Slide(?::|\s)/i.test(cleanLine) || /<Slide\b/i.test(cleanLine);
      if (
        isSlideLine &&
        (allowedComponents.length === 0 || allowedComponents.includes("Carousel"))
      ) {
        if (currentCarousel === null) {
          flushAllBuffers();
          currentCarousel = { title: "Comparative Showcase", slides: [] };
        }
        const slideRaw = cleanLine
          .replace(/<Slide\b[^>]*>/i, "")
          .replace(/<\/Slide>/i, "")
          .replace(/^Slide(?::|\s)*/i, "")
          .trim();
        const parts = splitEscapedPipes(slideRaw);
        currentCarousel.slides.push({
          title: parts[0] || `Item ${currentCarousel.slides.length + 1}`,
          description: parts.length > 2 ? parts[1] : undefined,
          content: parts.length > 2 ? parts.slice(2).join(" | ") : parts[1] || parts[0],
        });
        continue;
      }

      // --- 9. Mathematical Plots & Equations ---
      const isMathPlot =
        /^(?:MathPlot|Plot|Equation)(?::|\s)/i.test(cleanLine) || /<MathPlot\b/i.test(cleanLine);

      if (
        isMathPlot &&
        (allowedComponents.length === 0 || allowedComponents.includes("MathPlot"))
      ) {
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
          const parts = splitEscapedPipes(rawContent);
          fn = parts[0]?.replace(/^[yf]\([x]\)\s*=\s*/i, "").trim();
          for (let pIdx = 1; pIdx < parts.length; pIdx++) {
            const p = parts[pIdx];
            if (/range/i.test(p)) {
              const nums = p.match(/-?\d+/g);
              if (nums && nums.length >= 2) {
                range = [parseInt(nums[0], 10), parseInt(nums[1], 10)];
              }
            } else if (/title/i.test(p)) {
              title = p.replace(/^title[:=\s]*/i, "").trim();
            }
          }
        }

        elements.push(
          <LazyMathPlot
            key={`math-${elements.length}`}
            title={title}
            equation={eq}
            functionFormula={fn}
            rangeX={range}
          />
        );
        continue;
      }

      // --- 10. TestResults Component ---
      const xmlTestMatch =
        cleanLine.match(/<TestResults\b([^>]*)>(.*?)<\/TestResults>/i) ||
        cleanLine.match(/<TestResults\b([^>]*)\/?>/i);
      const isLineTest = /^TestResults(?::|\s)/i.test(cleanLine);

      if (
        (xmlTestMatch || isLineTest) &&
        (allowedComponents.length === 0 || allowedComponents.includes("TestResults"))
      ) {
        flushAllBuffers();
        const contentStr = xmlTestMatch ? xmlTestMatch[2] || xmlTestMatch[1] || "" : cleanLine;
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
            <PeldrunTestResults passedCount={passed} failedCount={failed} skippedCount={skipped} />
          </PeldrunMotionWrapper>
        );
        continue;
      }

      // --- 11. Accordion Component ---
      if (
        /^Accordion(?::|\s)/i.test(cleanLine) &&
        (allowedComponents.length === 0 || allowedComponents.includes("Accordion"))
      ) {
        flushAllBuffers();
        const raw = cleanLine.replace(/^Accordion(?::|\s)*/i, "").trim();
        const parts = splitEscapedPipes(raw);
        const items = parts.map((it) => {
          const segs = it.split(":");
          return { title: segs[0] || "Details", content: segs.slice(1).join(":") || "" };
        });
        currentAccordion = { items };
        continue;
      }

      // --- 12. Form Component & Fields ---
      const xmlFormMatch = cleanLine.match(/<Form\b([^>]*)>/i);
      const isLineForm = /^Form(?::|\s)/i.test(cleanLine);

      if (
        (xmlFormMatch || isLineForm) &&
        (allowedComponents.length === 0 || allowedComponents.includes("Form"))
      ) {
        flushAllBuffers();
        let formTitle = "Form";
        let inlineFields: FormFieldDefinition[] = [];

        if (xmlFormMatch) {
          const attrs = parseXmlAttributes(xmlFormMatch[1]);
          formTitle = attrs.title || "Form";
        } else {
          const raw = cleanLine.replace(/^Form(?::|\s)*/i, "").trim();
          const parts = splitEscapedPipes(raw);
          formTitle = parts[0] || "Form";
          if (parts[1]) {
            inlineFields = parts[1].split(",").map((f) => ({
              name: f.trim().toLowerCase().replace(/\s+/g, "_"),
              label: f.trim(),
            }));
          }
        }
        currentForm = { title: formTitle, fields: inlineFields };
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

      // --- 13. ActionButton Component ---
      if (
        /^(?:Action|ActionButton)(?::|\s)/i.test(cleanLine) &&
        (allowedComponents.length === 0 || allowedComponents.includes("ActionButton"))
      ) {
        const actionLabel = cleanLine.replace(/^(?:Action|ActionButton)(?::|\s)*/i, "").trim();
        if (currentForm !== null) {
          currentForm.submitLabel = actionLabel;
          flushForm();
        } else {
          flushProse();
          flushCard();
          flushMetrics();
          flushBadges();
          flushTable();
          actionGroup.push(
            <PeldrunActionButton key={`act-btn-${index}`} label={actionLabel} actionId={`act_${index}`} />
          );
        }
        continue;
      } else {
        flushActions();
      }

      // --- 14. Table Component Detection ---
      const isExplicitTableHeader = /^(?:Table|Table:|<Table>)/i.test(cleanLine);
      if (isExplicitTableHeader) {
        flushAllBuffers();
        inTableContext = true;
        const inlineContent = cleanLine.replace(/^(?:Table:?|<Table>)\s*/i, "").trim();
        if (inlineContent.includes("|") && inlineContent.length > 2) {
          tableLines.push(inlineContent);
        }
        continue;
      }

      const isTableDivider = /^[|\s:-]+$/.test(cleanLine) && cleanLine.includes("-");
      const pipeCount = (cleanLine.match(/(?<!\\)\|/g) || []).length;

      if (inTableContext) {
        if (pipeCount >= 1 || isTableDivider) {
          tableLines.push(cleanLine);
          continue;
        } else {
          flushTable();
        }
      } else if (pipeCount >= 1 && (isTableDivider || cleanLine.includes("|"))) {
        const nextLine = rawLines[index + 1]?.trim() || "";
        const nextIsDivider = /^[|\s:-]+$/.test(nextLine) && nextLine.includes("-");

        if (cleanLine.startsWith("|") || isTableDivider || nextIsDivider) {
          flushProse();
          flushMetrics();
          flushBadges();
          flushActions();
          inTableContext = true;
          tableLines.push(cleanLine);
          continue;
        }
      }

      // --- 15. Markdown Prose & Fallback Buffer ---
      if (cleanLine) {
        if (currentCard !== null) {
          if (!currentCard.desc) {
            currentCard.desc = cleanLine;
          } else {
            currentCard.items.push(cleanLine);
          }
        } else if (currentAccordion !== null) {
          currentAccordion.items.push({ title: "Details", content: cleanLine });
        } else {
          flushMetrics();
          flushBadges();
          flushActions();
          proseLines.push(cleanLine);
        }
      }
    }

    flushAllBuffers();
    return elements;
  }, [dslContent, allowedComponents, isEnabled]);

  return <div className="space-y-2 py-1">{parsedElements}</div>;
}

export default OpenUIRenderer;
