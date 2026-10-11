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
import { PeldrunChart } from "./catalog/PeldrunChart";
import { PeldrunForm } from "./catalog/PeldrunForm";
import { PeldrunTestResults } from "./catalog/PeldrunTestResults";
import { PeldrunAccordion } from "./catalog/PeldrunAccordion";
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
    let metricGroup: React.JSX.Element[] = [];
    let badgeGroup: React.JSX.Element[] = [];
    let tableLines: string[] = [];

    const flushCard = () => {
      if (currentCard !== null) {
        const c = currentCard;
        elements.push(
          <PeldrunCard key={`card-${elements.length}`} title={c.title} description={c.desc}>
            {c.items.map((it, i) => (
              <div key={`ci-${i}`} className="text-muted-foreground text-xs leading-relaxed">
                {it}
              </div>
            ))}
          </PeldrunCard>
        );
        currentCard = null;
      }
    };

    const flushForm = () => {
      if (currentForm !== null) {
        const f = currentForm;
        const validFields = f.fields.length > 0 ? f.fields : [{ name: "input", label: "Input" }];
        elements.push(
          <PeldrunForm
            key={`form-${elements.length}`}
            title={f.title}
            description={f.description}
            fields={validFields}
            submitLabel={f.submitLabel || "Submit"}
          />
        );
        currentForm = null;
      }
    };

    const flushAccordion = () => {
      if (currentAccordion !== null && currentAccordion.items.length > 0) {
        elements.push(
          <PeldrunAccordion key={`accordion-${elements.length}`} items={currentAccordion.items} />
        );
        currentAccordion = null;
      }
    };

    const flushMetrics = () => {
      if (metricGroup.length > 0) {
        elements.push(
          <div
            key={`metrics-grid-${elements.length}`}
            className="grid grid-cols-1 sm:grid-cols-3 gap-2.5 my-2"
          >
            {[...metricGroup]}
          </div>
        );
        metricGroup = [];
      }
    };

    const flushBadges = () => {
      if (badgeGroup.length > 0) {
        elements.push(
          <div
            key={`badges-row-${elements.length}`}
            className="flex flex-wrap items-center gap-2 my-2"
          >
            {[...badgeGroup]}
          </div>
        );
        badgeGroup = [];
      }
    };

    const flushTable = () => {
      if (tableLines.length > 0) {
        if (allowedComponents.includes("Table")) {
          const rawRows = tableLines
            .map((r) =>
              r
                .split("|")
                .map((c) => c.trim())
                .filter((_, idx, arr) => idx > 0 && idx < arr.length - 1)
            )
            .filter((r) => r.length > 0);

          const contentRows = rawRows.filter((r) => !r.every(isMarkdownDividerCell));
          if (contentRows.length > 0) {
            const headers = contentRows[0] || ["Key", "Value"];
            const rows = contentRows.slice(1);
            elements.push(
              <PeldrunTable key={`tbl-md-${elements.length}`} headers={headers} rows={rows} />
            );
          }
        }
        tableLines = [];
      }
    };

    const flushAllBuffers = () => {
      flushCard();
      flushForm();
      flushAccordion();
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

      // Markdown Table Lines Accumulator
      if (line.startsWith("|") && line.endsWith("|")) {
        flushMetrics();
        flushBadges();
        tableLines.push(line);
        continue;
      } else {
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

      // 1. XML / JSX Tag Handling
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

      // 2. TestResults Detection (XML & Line formats)
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
          <PeldrunTestResults
            key={`tests-${index}`}
            passedCount={passed}
            failedCount={failed}
            skippedCount={skipped}
          />
        );
        continue;
      }

      // 3. Alert Component (Checked BEFORE Badge)
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

        elements.push(<PeldrunAlert key={`alert-${index}`} message={message} type={alertType} />);
        continue;
      }

      // 4. Form Component (Buffered multi-line fields support)
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

      // Collect Field into active Form Buffer
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

      // 5. Line Card Detection
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

      // 6. Multi-Metric Single-Line Detection
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

      // 7. Single Metric Detection
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

      // 8. Badge Detection
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

      // 9. Progress Component
      if (/^Progress(?::|\s)/i.test(cleanLine) && allowedComponents.includes("Progress")) {
        flushAllBuffers();
        const parts = cleanLine.replace(/^Progress(?::|\s)*/i, "").split("|").map((p) => p.trim());
        const label = parts[0] || "";
        const pct = extractNumericValue(parts[1] || parts[0] || "0");
        elements.push(
          <PeldrunProgress key={`progress-${index}`} label={label} percentage={pct} />
        );
        continue;
      }

      // 10. Table DSL Component (Avoids empty ghost headers)
      if (/^Table(?::|\s)/i.test(cleanLine) && allowedComponents.includes("Table")) {
        const raw = cleanLine.replace(/^Table(?::|\s)*/i, "").trim();
        if (raw.includes("|")) {
          flushAllBuffers();
          const parts = raw.split("|").map((p) => p.trim());
          const headers = parts[0] ? parts[0].split(",").map((h) => h.trim()) : ["Key", "Value"];
          const rows = parts.slice(1).map((r) => r.split(",").map((c) => c.trim()));
          elements.push(<PeldrunTable key={`tbl-${index}`} headers={headers} rows={rows} />);
        }
        // If it was just "Table:" with no columns, silently ignore it; markdown table below will parse it
        continue;
      }

      // 11. Chart Component (Resilient Number & Prefix Extraction)
      if (/(?:^Chart|<Chart\b)/i.test(cleanLine) && allowedComponents.includes("Chart")) {
        flushAllBuffers();
        const isLine = /line/i.test(cleanLine);
        const chartType: "bar" | "line" = isLine ? "line" : "bar";

        // Clean prefixes, XML tags, and bracket indicators
        const cleanedData = cleanLine
          .replace(/<Chart\b[^>]*>/i, "")
          .replace(/<\/Chart>/i, "")
          .replace(/^Chart(?:\s*\[(?:bar\vert{}line)\])?(?:\s*\((?:bar\vert{}line)\))?[:\s]*/i, "")
          .replace(/^\[(?:bar\vert{}line)\][:\s]*/i, "")
          .trim();

        const dataPoints = cleanedData.split(",").map((pair) => {
          const parts = pair.split(":");
          const rawLabel = (parts[0] || "").replace(/\[(?:bar\vert{}line)\]/gi, "").trim();
          const rawVal = parts[1] || "";
          return {
            label: rawLabel || "Point",
            value: extractNumericValue(rawVal),
          };
        }).filter((dp) => dp.label && dp.value >= 0);

        if (dataPoints.length > 0) {
          elements.push(<PeldrunChart key={`chart-${index}`} type={chartType} data={dataPoints} />);
        }
        continue;
      }

      // 12. Accordion Component
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

      // 13. Action Button
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
            <div key={`act-${index}`} className="my-1.5 flex gap-2">
              <PeldrunActionButton key={`act-btn-${index}`} label={actionLabel} actionId={`act_${index}`} />
            </div>
          );
        }
        continue;
      }

      // Fallback: Attach plain text or HTML list item to active Card or render as clean paragraph
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
