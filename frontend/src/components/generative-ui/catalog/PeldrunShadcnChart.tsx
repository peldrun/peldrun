// frontend/src/components/generative-ui/catalog/PeldrunShadcnChart.tsx
"use client";

import React, { useMemo, useState, useEffect } from "react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  BarChart,
  Bar,
  LineChart,
  Line,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import { BarChart3, TrendingUp } from "lucide-react";
import { PeldrunMotionWrapper } from "./PeldrunMotionWrapper";

export type ChartVariant = "area" | "bar" | "line" | "donut" | "pie";

interface ChartDataPoint {
  label: string;
  value: number;
  [key: string]: any;
}

interface PeldrunShadcnChartProps {
  title?: string;
  description?: string;
  type?: ChartVariant;
  data: ChartDataPoint[];
  unit?: string;
}

const PALETTE = [
  "#3b82f6", // blue
  "#10b981", // emerald
  "#f59e0b", // amber
  "#8b5cf6", // purple
  "#ec4899", // pink
  "#06b6d4", // cyan
];

export function PeldrunShadcnChart({
  title,
  description,
  type = "area",
  data,
  unit = "",
}: PeldrunShadcnChartProps): React.JSX.Element {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  const chartData = useMemo(() => {
    return (data || []).map((d) => ({
      name: d.label || "Point",
      value: Number(d.value) || 0,
    }));
  }, [data]);

  const summaryTotal = useMemo(() => {
    return chartData.reduce((acc, curr) => acc + curr.value, 0);
  }, [chartData]);

  if (!mounted) {
    return (
      <div className="h-60 rounded-xl border border-border/80 bg-card/40 animate-pulse my-2.5" />
    );
  }

  const renderChartBody = () => {
    switch (type) {
      case "bar":
        return (
          <BarChart data={chartData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.15} vertical={false} />
            <XAxis dataKey="name" stroke="#888888" fontSize={11} tickLine={false} axisLine={false} />
            <YAxis stroke="#888888" fontSize={11} tickLine={false} axisLine={false} />
            <Tooltip
              content={({ active, payload }) => {
                if (active && payload && payload.length) {
                  return (
                    <div className="rounded-lg border border-border bg-card p-2 shadow-md text-xs">
                      <p className="font-semibold text-foreground">{payload[0].payload.name}</p>
                      <p className="text-primary font-mono">
                        {payload[0].value?.toLocaleString()} {unit}
                      </p>
                    </div>
                  );
                }
                return null;
              }}
            />
            <Bar dataKey="value" fill="#3b82f6" radius={[4, 4, 0, 0]} />
          </BarChart>
        );

      case "line":
        return (
          <LineChart data={chartData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.15} vertical={false} />
            <XAxis dataKey="name" stroke="#888888" fontSize={11} tickLine={false} axisLine={false} />
            <YAxis stroke="#888888" fontSize={11} tickLine={false} axisLine={false} />
            <Tooltip
              content={({ active, payload }) => {
                if (active && payload && payload.length) {
                  return (
                    <div className="rounded-lg border border-border bg-card p-2 shadow-md text-xs">
                      <p className="font-semibold text-foreground">{payload[0].payload.name}</p>
                      <p className="text-primary font-mono">
                        {payload[0].value?.toLocaleString()} {unit}
                      </p>
                    </div>
                  );
                }
                return null;
              }}
            />
            <Line
              type="monotone"
              dataKey="value"
              stroke="#3b82f6"
              strokeWidth={2.5}
              dot={{ fill: "#3b82f6", r: 4 }}
              activeDot={{ r: 6 }}
            />
          </LineChart>
        );

      case "donut":
      case "pie":
        return (
          <PieChart>
            <Tooltip
              content={({ active, payload }) => {
                if (active && payload && payload.length) {
                  return (
                    <div className="rounded-lg border border-border bg-card p-2 shadow-md text-xs">
                      <p className="font-semibold text-foreground">{payload[0].name}</p>
                      <p className="text-primary font-mono">
                        {payload[0].value?.toLocaleString()} {unit}
                      </p>
                    </div>
                  );
                }
                return null;
              }}
            />
            <Pie
              data={chartData}
              innerRadius={type === "donut" ? 50 : 0}
              outerRadius={75}
              paddingAngle={type === "donut" ? 4 : 0}
              dataKey="value"
            >
              {chartData.map((_, index) => (
                <Cell key={`cell-${index}`} fill={PALETTE[index % PALETTE.length]} />
              ))}
            </Pie>
          </PieChart>
        );

      case "area":
      default:
        return (
          <AreaChart data={chartData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
            <defs>
              <linearGradient id="peldrunAreaGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.4} />
                <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" opacity={0.15} vertical={false} />
            <XAxis dataKey="name" stroke="#888888" fontSize={11} tickLine={false} axisLine={false} />
            <YAxis stroke="#888888" fontSize={11} tickLine={false} axisLine={false} />
            <Tooltip
              content={({ active, payload }) => {
                if (active && payload && payload.length) {
                  return (
                    <div className="rounded-lg border border-border bg-card p-2 shadow-md text-xs">
                      <p className="font-semibold text-foreground">{payload[0].payload.name}</p>
                      <p className="text-primary font-mono">
                        {payload[0].value?.toLocaleString()} {unit}
                      </p>
                    </div>
                  );
                }
                return null;
              }}
            />
            <Area
              type="monotone"
              dataKey="value"
              stroke="#3b82f6"
              strokeWidth={2}
              fillOpacity={1}
              fill="url(#peldrunAreaGrad)"
            />
          </AreaChart>
        );
    }
  };

  return (
    <PeldrunMotionWrapper className="my-2.5">
      <div className="rounded-xl border border-border/80 bg-card/60 backdrop-blur-sm p-4 shadow-xs">
        {(title || description) && (
          <div className="flex items-center justify-between mb-3 border-b border-border/40 pb-2.5">
            <div>
              {title && (
                <div className="flex items-center gap-1.5 font-semibold text-xs text-foreground">
                  <BarChart3 size={14} className="text-primary" />
                  <span>{title}</span>
                </div>
              )}
              {description && (
                <p className="text-[11px] text-muted-foreground mt-0.5">{description}</p>
              )}
            </div>
            {summaryTotal > 0 && (
              <div className="flex items-center gap-1 text-[11px] font-mono text-muted-foreground bg-muted/40 px-2 py-0.5 rounded-md">
                <TrendingUp size={11} className="text-primary" />
                <span>
                  Total: {summaryTotal.toLocaleString()} {unit}
                </span>
              </div>
            )}
          </div>
        )}

        <div className="h-56 w-full">{renderChartBody()}</div>
      </div>
    </PeldrunMotionWrapper>
  );
}
