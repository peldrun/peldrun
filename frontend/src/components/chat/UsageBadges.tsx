"use client";

import React, { useState } from "react";
import {
  Coins,
  Clock,
  Zap,
  Gauge,
  Database,
  Brain,
  ChevronDown,
  ChevronUp,
  HelpCircle,
  Sparkles,
} from "lucide-react";
import type { TokenUsageInfo, TurnUsage, ChatUsageSummary } from "@/lib/types";

// =========================================================================
// Pure Formatting Helpers (Reusable across UI & exports)
// =========================================================================

export function formatTokenCount(tokens: number | null | undefined): string {
  if (tokens === null || tokens === undefined) return "—";
  if (tokens >= 1_000_000) {
    return `${(tokens / 1_000_000).toFixed(2)}M`;
  }
  if (tokens >= 1_000) {
    return `${(tokens / 1_000).toFixed(1)}k`;
  }
  return tokens.toLocaleString();
}

export function formatCostUsd(
  cost: number | null | undefined,
  isLocal: boolean = false
): string {
  if (isLocal) return "Free (Local)";
  if (cost === null || cost === undefined) return "—";
  if (cost === 0) return "$0.00";
  if (cost < 0.0001) return `< $0.0001`;
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(3)}`;
}

export function formatLatencyMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || ms <= 0) return "—";
  if (ms >= 1000) {
    return `${(ms / 1000).toFixed(2)}s`;
  }
  return `${Math.round(ms)}ms`;
}

export function formatThroughput(tokPerSec: number | null | undefined): string {
  if (tokPerSec === null || tokPerSec === undefined || tokPerSec <= 0) return "—";
  return `${tokPerSec.toFixed(1)} tok/s`;
}

export function checkIsLocalProvider(
  provider?: string | null,
  baseUrl?: string | null,
  model?: string | null
): boolean {
  const p = (provider || "").toLowerCase();
  const u = (baseUrl || "").toLowerCase();
  const m = (model || "").toLowerCase();

  return (
    p.includes("lmstudio") ||
    p.includes("ollama") ||
    p.includes("local") ||
    u.includes("127.0.0.1") ||
    u.includes("localhost") ||
    u.includes(":1234") ||
    u.includes(":11434") ||
    m.includes("qwen") ||
    m.includes("llama")
  );
}

// =========================================================================
// Atom 1: TokenBadge
// =========================================================================

export interface TokenBadgeProps {
  tokens?: number | null;
  inputTokens?: number | null;
  outputTokens?: number | null;
  cachedTokens?: number | null;
  reasoningTokens?: number | null;
  isEstimated?: boolean;
  className?: string;
  size?: "sm" | "md";
}

export function TokenBadge({
  tokens,
  inputTokens,
  outputTokens,
  cachedTokens,
  reasoningTokens,
  isEstimated = false,
  className = "",
  size = "md",
}: TokenBadgeProps) {
  if (tokens === null || tokens === undefined || tokens <= 0) return null;

  const padClass = size === "sm" ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-xs";

  return (
    <div
      className={`inline-flex items-center gap-1 rounded font-mono font-medium border shadow-xs transition-colors ${
        isEstimated
          ? "bg-amber-500/10 border-amber-500/20 text-amber-600 dark:text-amber-400"
          : "bg-blue-500/10 border-blue-500/20 text-blue-600 dark:text-blue-400"
      } ${padClass} ${className}`}
      title={
        isEstimated
          ? `Estimated tokens: ${tokens} (Input: ${inputTokens || 0}, Output: ${outputTokens || 0})`
          : `Total tokens: ${tokens} (Input: ${inputTokens || 0}, Output: ${outputTokens || 0}${
              cachedTokens ? `, Cached: ${cachedTokens}` : ""
            }${reasoningTokens ? `, Reasoning: ${reasoningTokens}` : ""})`
      }
    >
      <Coins size={size === "sm" ? 10 : 12} className="shrink-0 opacity-80" />
      <span>{formatTokenCount(tokens)} tok</span>

      {cachedTokens && cachedTokens > 0 ? (
        <span
          className="text-[9px] px-1 rounded bg-blue-500/15 text-blue-500 border border-blue-500/30 flex items-center gap-0.5"
          title={`Prompt Cache Hit: ${cachedTokens} tokens`}
        >
          <Database size={8} /> {formatTokenCount(cachedTokens)}
        </span>
      ) : null}

      {reasoningTokens && reasoningTokens > 0 ? (
        <span
          className="text-[9px] px-1 rounded bg-violet-500/15 text-violet-500 border border-violet-500/30 flex items-center gap-0.5"
          title={`Reasoning Thoughts: ${reasoningTokens} tokens`}
        >
          <Brain size={8} /> {formatTokenCount(reasoningTokens)}
        </span>
      ) : null}

      {isEstimated && (
        <span className="text-[9px] px-1 rounded bg-amber-500/20 uppercase font-sans font-bold">
          Est
        </span>
      )}
    </div>
  );
}

// =========================================================================
// Atom 2: CostBadge
// =========================================================================

export interface CostBadgeProps {
  costUsd?: number | null;
  isLocal?: boolean;
  className?: string;
  size?: "sm" | "md";
}

export function CostBadge({
  costUsd,
  isLocal = false,
  className = "",
  size = "md",
}: CostBadgeProps) {
  if (!isLocal && (costUsd === null || costUsd === undefined)) return null;

  const padClass = size === "sm" ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-xs";

  return (
    <div
      className={`inline-flex items-center gap-1 rounded font-mono font-medium border shadow-xs transition-colors ${
        isLocal
          ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-600 dark:text-emerald-400"
          : "bg-emerald-500/10 border-emerald-500/20 text-emerald-600 dark:text-emerald-400"
      } ${padClass} ${className}`}
      title={isLocal ? "Local Hardware Inference (Zero Billing)" : `Calculated Cost: $${costUsd}`}
    >
      <Zap size={size === "sm" ? 10 : 12} className="shrink-0 text-emerald-500 opacity-90" />
      <span>{formatCostUsd(costUsd, isLocal)}</span>
    </div>
  );
}

// =========================================================================
// Atom 3: LatencyBadge
// =========================================================================

export interface LatencyBadgeProps {
  latencyMs?: number | null;
  ttftMs?: number | null;
  tokensPerSecond?: number | null;
  className?: string;
  size?: "sm" | "md";
}

export function LatencyBadge({
  latencyMs,
  ttftMs,
  tokensPerSecond,
  className = "",
  size = "md",
}: LatencyBadgeProps) {
  if (
    (latencyMs === null || latencyMs === undefined || latencyMs <= 0) &&
    (ttftMs === null || ttftMs === undefined || ttftMs <= 0) &&
    (tokensPerSecond === null || tokensPerSecond === undefined || tokensPerSecond <= 0)
  ) {
    return null;
  }

  const padClass = size === "sm" ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-xs";

  return (
    <div
      className={`inline-flex items-center gap-1.5 rounded font-mono font-medium border bg-muted/50 border-border text-muted-foreground shadow-xs transition-colors ${padClass} ${className}`}
      title={`Duration: ${formatLatencyMs(latencyMs)}${
        ttftMs ? ` | TTFT: ${formatLatencyMs(ttftMs)}` : ""
      }${tokensPerSecond ? ` | Throughput: ${formatThroughput(tokensPerSecond)}` : ""}`}
    >
      <Clock size={size === "sm" ? 10 : 12} className="shrink-0 opacity-70" />
      <span>{formatLatencyMs(latencyMs)}</span>

      {ttftMs && ttftMs > 0 ? (
        <span className="text-[9px] text-muted-foreground/80 font-sans">
          (TTFT {formatLatencyMs(ttftMs)})
        </span>
      ) : null}

      {tokensPerSecond && tokensPerSecond > 0 ? (
        <span className="text-[9px] text-primary font-semibold flex items-center gap-0.5 border-l border-border/80 pl-1">
          <Gauge size={8} /> {formatThroughput(tokensPerSecond)}
        </span>
      ) : null}
    </div>
  );
}

// =========================================================================
// Composite: UsageBadgeGroup (Universal Master Component)
// =========================================================================

export interface UsageBadgeGroupProps {
  tokensUsed?: { input?: number; output?: number; total?: number } | null;
  usage?: TokenUsageInfo | null;
  summary?: ChatUsageSummary | null;
  turn?: TurnUsage | null;

  costUsd?: number | null;
  latencyMs?: number | null;
  ttftMs?: number | null;
  tokensPerSecond?: number | null;

  provider?: string | null;
  baseUrl?: string | null;
  model?: string | null;

  variant?: "header" | "card" | "compact";
  showBreakdown?: boolean;
  className?: string;
}

export function UsageBadgeGroup({
  tokensUsed,
  usage,
  summary,
  turn,
  costUsd,
  latencyMs,
  ttftMs,
  tokensPerSecond,
  provider,
  baseUrl,
  model,
  variant = "card",
  showBreakdown = true,
  className = "",
}: UsageBadgeGroupProps) {
  const [expanded, setExpanded] = useState(false);

  // 1. Resolve Token Metrics
  const resolvedTokens =
    tokensUsed?.total ??
    usage?.total_tokens ??
    summary?.total_tokens ??
    turn?.usage?.total_tokens ??
    ((usage?.input_tokens || usage?.output_tokens)
      ? (usage?.input_tokens || 0) + (usage?.output_tokens || 0)
      : null);

  const resolvedInput =
    tokensUsed?.input ??
    usage?.input_tokens ??
    summary?.input_tokens ??
    turn?.usage?.input_tokens ??
    usage?.prompt_tokens ??
    null;

  const resolvedOutput =
    tokensUsed?.output ??
    usage?.output_tokens ??
    summary?.output_tokens ??
    turn?.usage?.output_tokens ??
    usage?.completion_tokens ??
    null;

  const resolvedCached =
    usage?.cached_input_tokens ??
    summary?.cached_input_tokens ??
    turn?.usage?.cached_input_tokens ??
    null;

  const resolvedReasoning =
    usage?.reasoning_output_tokens ??
    summary?.reasoning_output_tokens ??
    turn?.usage?.reasoning_output_tokens ??
    null;

  const resolvedEstimated = Boolean(
    usage?.estimated ??
    (summary?.estimated_tokens && summary.estimated_tokens > 0) ??
    turn?.usage?.estimated
  );

  // 2. Resolve Financial Costs
  const resolvedCost =
    costUsd ??
    summary?.total_cost_usd ??
    turn?.cost_usd ??
    null;

  // 3. Resolve Latency & Throughput
  const resolvedLatency = latencyMs ?? turn?.latency_ms ?? null;
  const resolvedTtft = ttftMs ?? turn?.ttft_ms ?? null;
  const resolvedThroughput = tokensPerSecond ?? turn?.tokens_per_second ?? null;

  // 4. Resolve Local Zero-Billing Heuristic
  const isLocal = checkIsLocalProvider(provider || turn?.provider, baseUrl, model);

  // Defensive Check: Do not render empty containers
  const hasTokens = resolvedTokens !== null && resolvedTokens > 0;
  const hasCost = isLocal || (resolvedCost !== null && resolvedCost !== undefined);
  const hasLatency = resolvedLatency !== null && resolvedLatency > 0;

  if (!hasTokens && !hasCost && !hasLatency) {
    return null;
  }

  const badgeSize = variant === "compact" ? "sm" : "md";

  return (
    <div className={`inline-flex flex-col gap-1 select-none ${className}`}>
      <div className="flex flex-wrap items-center gap-1.5">
        {/* Token Badge */}
        <TokenBadge
          tokens={resolvedTokens}
          inputTokens={resolvedInput}
          outputTokens={resolvedOutput}
          cachedTokens={resolvedCached}
          reasoningTokens={resolvedReasoning}
          isEstimated={resolvedEstimated}
          size={badgeSize}
        />

        {/* Cost Badge */}
        <CostBadge
          costUsd={resolvedCost}
          isLocal={isLocal}
          size={badgeSize}
        />

        {/* Latency / Speed Badge */}
        <LatencyBadge
          latencyMs={resolvedLatency}
          ttftMs={resolvedTtft}
          tokensPerSecond={resolvedThroughput}
          size={badgeSize}
        />

        {/* Toggle Detailed Breakdown Button */}
        {showBreakdown && hasTokens && (
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            className="p-1 rounded text-muted-foreground/70 hover:text-foreground hover:bg-muted/80 transition-colors cursor-pointer"
            title="Toggle Detailed Token & Telemetry Breakdown"
          >
            {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          </button>
        )}
      </div>

      {/* Expandable Accounting Matrix */}
      {expanded && (
        <div className="p-2 mt-1 rounded bg-card border border-border text-[11px] font-mono shadow-xs space-y-1.5 animate-in fade-in-50 duration-150">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-muted-foreground pb-1 border-b border-border/50">
            <div>
              <span className="block text-[9px] uppercase tracking-wider text-muted-foreground/70">
                Input (Prompt)
              </span>
              <span className="font-bold text-foreground">
                {formatTokenCount(resolvedInput)}
              </span>
            </div>

            <div>
              <span className="block text-[9px] uppercase tracking-wider text-muted-foreground/70">
                Output (Completion)
              </span>
              <span className="font-bold text-foreground">
                {formatTokenCount(resolvedOutput)}
              </span>
            </div>

            <div>
              <span className="block text-[9px] uppercase tracking-wider text-muted-foreground/70">
                Prompt Cache
              </span>
              <span className="font-bold text-blue-500">
                {resolvedCached ? formatTokenCount(resolvedCached) : "0"}
              </span>
            </div>

            <div>
              <span className="block text-[9px] uppercase tracking-wider text-muted-foreground/70">
                Reasoning (Thought)
              </span>
              <span className="font-bold text-violet-500">
                {resolvedReasoning ? formatTokenCount(resolvedReasoning) : "0"}
              </span>
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between text-[10px] text-muted-foreground pt-0.5">
            <div className="flex items-center gap-2">
              {model && (
                <span>
                  Model: <strong className="text-foreground">{model}</strong>
                </span>
              )}
              {provider && (
                <span>
                  Provider: <strong className="text-foreground">{provider}</strong>
                </span>
              )}
            </div>

            <div className="flex items-center gap-1 font-sans">
              {resolvedEstimated ? (
                <span className="text-amber-500 flex items-center gap-1">
                  <HelpCircle size={10} /> Estimated via Tokenizer Fallback (Unknown != 0)
                </span>
              ) : (
                <span className="text-emerald-500 flex items-center gap-1">
                  <Sparkles size={10} /> Exact Provider Metrics
                </span>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default UsageBadgeGroup;