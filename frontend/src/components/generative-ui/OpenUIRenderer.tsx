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

interface OpenUIRendererProps {
  dslContent: string;
}

interface CardBuffer {
  title?: string;
  desc?: string;
  items: string[];
}

// Safely identify markdown table divider cells without triggering Tailwind arbitrary property scanner
function isMarkdownDividerCell(cell: string): boolean {
  const sanitized = cell.replace(/\s+/g, "").replace(/-/g, "").replace(/:/g, "");
  return sanitized.length === 0;
}

export function OpenUIRenderer({ dslContent }: OpenUIRendererProps): React.JSX.Element {
  const allowedComponents = useGenerativeUIStore((s) => s.allowedComponents);

  const parsedElements = useMemo(() => {
    const rawLines = dslContent.split("\n").map((l) => l.trim()).filter(Boolean);
    const elements: React.JSX.Element[] = [];

    let currentCard: CardBuffer | null = null;
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
        tableLines = [];
      }
    };

    const flushAllBuffers = () => {
      flushCard();
      flushMetrics();
      flushBadges();
      flushTable();
    };

    for (let index = 0; index < rawLines.length; index++) {
      const line = rawLines[index];

      // Detect markdown table rows
      if (line.startsWith("|") && line.endsWith("|")) {
        flushMetrics();
        flushBadges();
        tableLines.push(line);
        continue;
      } else {
        flushTable();
      }

      // Strip markdown bold/italic asterisks and list bullets
      const cleanLine = line
        .replace(/^\s*[-*•]\s*/, "")
        .replace(/[*_]{1,3}/g, "")
        .trim();

      // Skip descriptive headers and comments
      if (
        cleanLine.startsWith("<!--") ||
        /^(?:Status\s*Badges?|Metric\s*Components?|Executive\s*Summary):?$/i.test(cleanLine)
      ) {
        continue;
      }

      // 1. Card Component Detection
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

      // 2. Metric Component Detection
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
          if (segs.length >= 3) {
            for (let sIdx = 2; sIdx < segs.length; sIdx++) {
              const seg = segs[sIdx];
              if (/trend/i.test(seg)) {
                trend = /up|↑/i.test(seg) ? "up" : /down|↓/i.test(seg) ? "down" : "neutral";
              } else {
                change = seg;
              }
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
      } else {
        flushMetrics();
      }

      // 3. Badge Component Detection
      const isExplicitBadge = /^Badge(?:\s*\d+)?(?::|\s)/i.test(cleanLine);
      const isStatusItem =
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
        } else if (rawStatus.includes("info") || rawStatus.includes("🟢")) {
          variant = "info";
        }

        const displayLabel = parts[1] ? `${label}: ${parts[1]}` : label;

        badgeGroup.push(
          <PeldrunBadge key={`badge-${index}`} label={displayLabel} variant={variant} />
        );
        continue;
      } else {
        flushBadges();
      }

      // 4. Alert Component Detection
      if (/^Alert/i.test(cleanLine) && allowedComponents.includes("Alert")) {
        flushAllBuffers();
        const match = cleanLine.match(/^Alert(?:\s*\((info\vert{}success\vert{}warning\vert{}error)\))?(?::|\s)*(.*)$/i);
        const alertType =
          (match?.[1]?.toLowerCase() as "info" | "success" | "warning" | "error") || "info";
        const message = match?.[2] || cleanLine;
        elements.push(<PeldrunAlert key={`alert-${index}`} type={alertType} message={message} />);
        continue;
      }

      // 5. Progress Component Detection
      if (/^Progress(?::|\s)/i.test(cleanLine) && allowedComponents.includes("Progress")) {
        flushAllBuffers();
        const parts = cleanLine.replace(/^Progress(?::|\s)*/i, "").split("|").map((p) => p.trim());
        const label = parts[0] || "";
        const pct = parseInt(parts[1] || parts[0] || "0", 10);
        elements.push(
          <PeldrunProgress key={`progress-${index}`} label={label} percentage={pct} />
        );
        continue;
      }

      // 6. Explicit DSL Table Detection
      if (/^Table(?::|\s)/i.test(cleanLine) && allowedComponents.includes("Table")) {
        flushAllBuffers();
        const raw = cleanLine.replace(/^Table(?::|\s)*/i, "").trim();
        const parts = raw.split("|").map((p) => p.trim());
        const headers = parts[0] ? parts[0].split(",").map((h) => h.trim()) : ["Key", "Value"];
        const rows = parts.slice(1).map((r) => r.split(",").map((c) => c.trim()));
        elements.push(<PeldrunTable key={`tbl-${index}`} headers={headers} rows={rows} />);
        continue;
      }

      // 7. Chart Component Detection
      if (/^Chart/i.test(cleanLine) && allowedComponents.includes("Chart")) {
        flushAllBuffers();
        const isLine = /Chart(?:\s*\((line)\)|\s*[line])/i.test(cleanLine);
        const chartType: "bar" | "line" = isLine ? "line" : "bar";
        const pointsRaw = cleanLine.replace(/^Chart(?:\s*\[(bar\vert{}line)\])?(?::|\s)*/i, "");
        const data = pointsRaw.split(",").map((pair) => {
          const parts = pair.split(":");
          return { label: (parts[0] || "").trim(), value: Number(parts[1]) || 0 };
        });
        elements.push(<PeldrunChart key={`chart-${index}`} type={chartType} data={data} />);
        continue;
      }

      // 8. TestResults Component Detection
      if (/^TestResults(?::|\s)/i.test(cleanLine) && allowedComponents.includes("TestResults")) {
        flushAllBuffers();
        const parts = cleanLine.replace(/^TestResults(?::|\s)*/i, "").split(",").map((p) => p.trim());
        let passed = 0;
        let failed = 0;
        let skipped = 0;
        parts.forEach((p) => {
          const subParts = p.split(":");
          const k = (subParts[0] || "").trim().toLowerCase();
          const count = Number(subParts[1]) || 0;
          if (k.includes("pass")) passed = count;
          else if (k.includes("fail")) failed = count;
          else if (k.includes("skip")) skipped = count;
        });
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

      // 9. Form Component Detection
      if (/^Form(?::|\s)/i.test(cleanLine) && allowedComponents.includes("Form")) {
        flushAllBuffers();
        const parts = cleanLine.replace(/^Form(?::|\s)*/i, "").split("|").map((p) => p.trim());
        const title = parts[0] || "Form";
        const fieldNames = parts[1] ? parts[1].split(",").map((f) => f.trim()) : ["Input"];
        const fields = fieldNames.map((fn) => ({
          name: fn.toLowerCase().replace(/\s+/g, "_"),
          label: fn,
        }));
        elements.push(<PeldrunForm key={`form-${index}`} title={title} fields={fields} />);
        continue;
      }

      // 10. Accordion Component Detection
      if (/^Accordion(?::|\s)/i.test(cleanLine) && allowedComponents.includes("Accordion")) {
        flushAllBuffers();
        const rawItems = cleanLine.replace(/^Accordion(?::|\s)*/i, "").split("|").map((p) => p.trim());
        const items = rawItems.map((it) => {
          const parts = it.split(":");
          return { title: parts[0] || "Item", content: parts.slice(1).join(":") || "" };
        });
        elements.push(<PeldrunAccordion key={`accordion-${index}`} items={items} />);
        continue;
      }

      // 11. Action Button Detection
      if (
        /^(?:Action|ActionButton)(?::|\s)/i.test(cleanLine) &&
        allowedComponents.includes("ActionButton")
      ) {
        flushAllBuffers();
        const actionLabel = cleanLine.replace(/^(?:Action|ActionButton)(?::|\s)*/i, "").trim();
        elements.push(
          <div key={`act-${index}`} className="my-1.5 flex gap-2">
            <PeldrunActionButton label={actionLabel} actionId={`act_${index}`} />
          </div>
        );
        continue;
      }

      // Fallback: Append text to active card or render as paragraph
      if (currentCard !== null) {
        if (!currentCard.desc) {
          currentCard.desc = cleanLine;
        } else {
          currentCard.items.push(cleanLine);
        }
      } else {
        elements.push(
          <p key={`p-${index}`} className="text-xs text-foreground/90 my-1 leading-relaxed">
            {cleanLine}
          </p>
        );
      }
    }

    flushAllBuffers();
    return elements;
  }, [dslContent, allowedComponents]);

  return <div className="space-y-2 py-1">{parsedElements}</div>;
}
