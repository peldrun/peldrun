// frontend/src/components/generative-ui/catalog/PeldrunMathPlot.tsx
"use client";

import React, { useEffect, useRef, useState, useId } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";
import functionPlot from "function-plot";
import { Sigma, AlertCircle } from "lucide-react";
import { PeldrunMotionWrapper } from "./PeldrunMotionWrapper";

interface PeldrunMathPlotProps {
  equation?: string;
  functionFormula?: string;
  rangeX?: [number, number];
  title?: string;
}

/**
 * Validates mathematical formulas against a strict whitelist to prevent arbitrary expression evaluation.
 */
function isSafeMathFormula(formula: string): boolean {
  if (!formula || formula.trim().length === 0 || formula.length > 250) {
    return false;
  }

  const cleaned = formula.replace(/\s+/g, "").toLowerCase();

  // Deny any attempt to access execution contexts or keywords
  if (/(?:window|document|constructor|prototype|eval|function|alert|process|global|import|require|__proto__|this|return)/i.test(cleaned)) {
    return false;
  }

  // Strip authorized math functions, constants, variable x, digits, and allowed operators
  const stripped = cleaned
    .replace(/\b(sin|cos|tan|asin|acos|atan|sinh|cosh|tanh|log|ln|exp|sqrt|cbrt|abs|ceil|floor|round|sign|min|max|pow|pi|e)\b/g, "")
    .replace(/x/g, "")
    .replace(/[0-9.]+/g, "")
    .replace(/[\+\-\*\/\^\%\(\)]/g, "");

  // Formula is secure if nothing unauthorized remains
  return stripped.length === 0;
}

export function PeldrunMathPlot({
  equation,
  functionFormula,
  rangeX,
  title,
}: PeldrunMathPlotProps): React.JSX.Element {
  const plotContainerRef = useRef<HTMLDivElement>(null);
  const mathHtmlRef = useRef<HTMLDivElement>(null);
  const [plotError, setPlotError] = useState<string | null>(null);
  const uniqueId = useId().replace(/:/g, "_");

  // Deconstruct array reference into primitive scalars to prevent infinite re-render cycles
  const minX = rangeX?.[0] ?? -10;
  const maxX = rangeX?.[1] ?? 10;

  // Render LaTeX equation via KaTeX with strict sandbox flags
  useEffect(() => {
    if (equation && mathHtmlRef.current) {
      try {
        const rendered = katex.renderToString(equation, {
          displayMode: true,
          throwOnError: false,
          trust: false,
          strict: "ignore",
        });
        mathHtmlRef.current.innerHTML = rendered;
      } catch (err) {
        console.warn("[PeldrunMathPlot] KaTeX render error:", err);
      }
    }
  }, [equation]);

  // Render 2D Function Curve via function-plot
  useEffect(() => {
    if (!functionFormula || !plotContainerRef.current) return;

    if (!isSafeMathFormula(functionFormula)) {
      setPlotError("Formula contains unsupported or unsafe mathematical expressions.");
      return;
    }

    try {
      const isDark = document.documentElement.classList.contains("dark");
      const containerWidth = plotContainerRef.current.clientWidth || 360;

      functionPlot({
        target: plotContainerRef.current,
        width: Math.min(containerWidth, 540),
        height: 240,
        grid: true,
        xAxis: { domain: [minX, maxX] },
        data: [
          {
            fn: functionFormula,
            color: isDark ? "#38bdf8" : "#0284c7",
          },
        ],
      });
      setPlotError(null);
    } catch (err: any) {
      console.warn("[PeldrunMathPlot] function-plot render error:", err);
      setPlotError(err?.message || "Invalid mathematical formula expression.");
    }
  }, [functionFormula, minX, maxX, uniqueId]);

  return (
    <PeldrunMotionWrapper className="my-2.5">
      <div className="rounded-xl border border-border/80 bg-card/60 backdrop-blur-sm p-4 shadow-xs">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border/40 pb-2 mb-3">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
            <Sigma size={15} className="text-primary shrink-0" />
            <span>{title || "Mathematical & Scientific Plot"}</span>
          </div>
        </div>

        {/* KaTeX Display Equation */}
        {equation && (
          <div
            ref={mathHtmlRef}
            className="my-2 overflow-x-auto text-center py-2 bg-muted/20 rounded-lg text-foreground"
          />
        )}

        {/* 2D Interactive Function Plot Canvas */}
        {functionFormula && (
          <div className="mt-3 flex flex-col items-center justify-center">
            {plotError ? (
              <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive text-xs flex items-center gap-2">
                <AlertCircle size={14} />
                <span>{plotError}</span>
              </div>
            ) : (
              <div
                ref={plotContainerRef}
                className="overflow-x-auto w-full flex justify-center [&_svg]:rounded-lg [&_svg]:bg-background/40"
              />
            )}
            <span className="text-[10px] text-muted-foreground mt-1 font-mono">
              Formula: y = {functionFormula}
            </span>
          </div>
        )}
      </div>
    </PeldrunMotionWrapper>
  );
}

export default PeldrunMathPlot;
