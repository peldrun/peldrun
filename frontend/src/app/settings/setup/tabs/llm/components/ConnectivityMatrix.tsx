// frontend/src/app/settings/setup/tabs/llm/components/ConnectivityMatrix.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import { Activity, RefreshCw, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/Icon/Icon";
import { resolveProviderIcon } from "../constants";
import type {
  ConnectivityMatrixProps,
  CustomEndpointWithLogo,
  CloudProviderVaultItem,
} from "../types";

/* -------------------------------------------------------------------------- */
/* Internal primitives                                                        */
/* -------------------------------------------------------------------------- */

type HealthState = "online" | "offline" | "untested";

/** Colored status indicator with a subtle ring for the "online" state. */
function StatusDot({ state }: { state: HealthState }) {
  const color =
    state === "online"
      ? "bg-emerald-500"
      : state === "offline"
      ? "bg-rose-500"
      : "bg-amber-400";

  return (
    <span className="relative flex h-2.5 w-2.5 shrink-0" aria-hidden="true">
      <span className={`inline-flex rounded-full h-2.5 w-2.5 ${color}`} />
    </span>
  );
}

/** Compact latency readout shown only when a provider is online. */
function LatencyBadge({ ms }: { ms?: number }) {
  if (typeof ms !== "number") return null;
  return (
    <span className="text-[10px] text-emerald-600 dark:text-emerald-400 font-mono font-semibold tabular-nums">
      {ms} ms
    </span>
  );
}

/** Active-primary badge, shared by every provider row. */
function ActiveBadge() {
  return (
    <span className="px-2 py-0.5 rounded-sm text-[10px] font-bold bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30 whitespace-nowrap">
      Active Primary
    </span>
  );
}

/** Small "Activate" action with the reference hover behaviour. */
function ActivateButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="px-2 py-0.5 rounded-sm text-[10px] bg-primary/10 hover:bg-primary/20 text-primary font-medium cursor-pointer transition-colors hover:scale-105"
    >
      Activate
    </button>
  );
}

/** Cloud-only "Revert" action that restores the local LM Studio engine. */
function RevertButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="Deactivate and revert to local LM Studio"
      className="px-2 py-0.5 rounded-sm text-[10px] bg-muted hover:bg-muted/80 text-muted-foreground hover:text-foreground flex items-center gap-1 cursor-pointer transition-colors"
    >
      <RotateCcw size={10} />
      <span>Revert</span>
    </button>
  );
}

/** Single row inside the connectivity matrix. */
interface MatrixRowProps {
  iconName: string;
  label: string;
  subLabel?: string;
  state: HealthState;
  latency?: number;
  isPrimary: boolean;
  onActivate?: () => void;
  onRevert?: () => void;
}

function MatrixRow({
  iconName,
  label,
  subLabel,
  state,
  latency,
  isPrimary,
  onActivate,
  onRevert,
}: MatrixRowProps) {
  return (
    <div className="py-2.5 flex items-center justify-between gap-3">
      <div className="flex items-center gap-2 min-w-0">
        <StatusDot state={state} />
        <span className="shrink-0 flex items-center justify-center text-muted-foreground rounded rounded-Xl bg-muted p-1">
          <Icon name={iconName} size={14}    title={`${label} logo`} />
        </span>
        <span className="font-semibold text-foreground truncate">{label}</span>
        {subLabel && (
          <span
            className="text-[10px] text-muted-foreground font-mono truncate"
            title={subLabel}
          >
            ({subLabel})
          </span>
        )}
      </div>

      <div className="flex items-center gap-2 shrink-0">
        {state === "online" && <LatencyBadge ms={latency} />}

        {isPrimary ? (
          onRevert ? (
            <div className="flex items-center gap-1.5">
              <ActiveBadge />
              <RevertButton onClick={onRevert} />
            </div>
          ) : (
            <ActiveBadge />
          )
        ) : (
          onActivate && <ActivateButton onClick={onActivate} />
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

/**
 * Unified health matrix for every configured engine:
 *   - LM Studio (local),
 *   - Ollama (local),
 *   - cloud providers (vault items),
 *   - user-defined custom endpoints.
 *
 * Each row displays a truthful status dot, optional latency, and a one-click
 * activation control. Cloud rows also expose a "Revert" action that resets
 * the primary engine to the local LM Studio default.
 */
export function ConnectivityMatrix({
  config,
  lmStudioSettings,
  ollamaSettings,
  cloudProviders,
  customEndpoints,
  activateEngine,
  deactivateToDefault,
  testingAll,
  handleTestAll,
}: ConnectivityMatrixProps) {
  /* ---------------------- Derived state for LM Studio ------------------- */
  const lmStudioIsPrimary =
    config.llm.provider === "lmstudio" ||
    config.llm.base_url === lmStudioSettings.baseUrl;

  /* ---------------------- Derived state for Ollama ---------------------- */
  const ollamaIsPrimary = config.llm.provider === "ollama";

  return (
    <motion.div
      {...FADE_UP}
      className="relative p-5 rounded-md border border-border bg-card shadow-sm space-y-4"
    >
      <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md bg-gradient-to-r from-transparent via-primary/60 to-transparent" />

      {/* Header */}
      <div className="flex items-center justify-between border-b border-border/60 pb-3 flex-wrap gap-2">
        <div className="flex items-center gap-2.5">
          <div className="p-2 icon">
            <Activity size={16} className="text-primary" />
          </div>
          <div>
            <span className="text-xs font-bold text-foreground uppercase tracking-wider block">
              Connectivity Health Matrix
            </span>
            <span className="text-[10px] text-muted-foreground">
              Truthful live verification. Switch active primary with one click.
            </span>
          </div>
        </div>

        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={handleTestAll}
          disabled={testingAll}
          className="h-7 text-xs border-border bg-background hover:bg-muted text-foreground cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100 flex items-center gap-1.5"
        >
          <RefreshCw
            size={11}
            className={`text-primary ${testingAll ? "animate-spin" : ""}`}
          />
          <span>{testingAll ? "Pinging Endpoints…" : "Test All Endpoints"}</span>
        </Button>
      </div>

      {/* Rows */}
      <div className="divide-y divide-border/60 text-xs">
        {/* LM Studio */}
        <MatrixRow
          iconName="lm-studio"
          label="LM Studio (Local GPU)"
          subLabel={lmStudioSettings.model || "Not configured"}
          state={(lmStudioSettings.status as HealthState) || "untested"}
          latency={lmStudioSettings.latency}
          isPrimary={lmStudioIsPrimary}
          onActivate={
            lmStudioIsPrimary
              ? undefined
              : () =>
                  activateEngine(
                    "lmstudio",
                    "LM Studio (Local)",
                    lmStudioSettings.model,
                    lmStudioSettings.baseUrl,
                    lmStudioSettings.apiKey,
                    ""
                  )
          }
        />

        {/* Ollama */}
        <MatrixRow
          iconName="ollama-mono"
          label="Ollama (Local Server)"
          subLabel={ollamaSettings.model || "Not configured"}
          state={(ollamaSettings.status as HealthState) || "untested"}
          latency={ollamaSettings.latency}
          isPrimary={ollamaIsPrimary}
          onActivate={
            ollamaIsPrimary || !ollamaSettings.model
              ? undefined
              : () => {
                  const baseUrl = ollamaSettings.baseUrl.endsWith("/v1")
                    ? ollamaSettings.baseUrl
                    : `${ollamaSettings.baseUrl}/v1`;
                  activateEngine(
                    "ollama",
                    "Ollama (Local)",
                    ollamaSettings.model,
                    baseUrl,
                    "",
                    ""
                  );
                }
          }
        />

        {/* Cloud providers */}
        {cloudProviders.map((cp: CloudProviderVaultItem) => {
          const isPrimary =
            config.llm.provider === cp.id || config.llm.base_url === cp.baseUrl;
          const iconName = resolveProviderIcon(cp.id || cp.name);
          return (
            <MatrixRow
              key={cp.id}
              iconName={iconName}
              label={cp.name}
              subLabel={cp.model}
              state={(cp.status as HealthState) || "untested"}
              latency={cp.latency}
              isPrimary={isPrimary}
              onActivate={
                isPrimary
                  ? undefined
                  : () =>
                      activateEngine(
                        cp.id,
                        cp.name,
                        cp.model,
                        cp.baseUrl,
                        cp.apiKey,
                        cp.type
                      )
              }
              onRevert={isPrimary ? deactivateToDefault : undefined}
            />
          );
        })}

        {/* Custom endpoints */}
        {customEndpoints.map((ce) => {
          const isPrimary = config.llm.base_url === ce.endpointUrl;
          const withLogo = ce as CustomEndpointWithLogo;
          const iconName = withLogo.providerLogo
            ? withLogo.providerLogo
            : resolveProviderIcon(ce.providerId || ce.name);
          return (
            <MatrixRow
              key={ce.id}
              iconName={iconName}
              label={ce.name}
              subLabel={ce.defaultModel}
              state="untested"
              isPrimary={isPrimary}
              onActivate={
                isPrimary
                  ? undefined
                  : () =>
                      activateEngine(
                        ce.providerId,
                        ce.name,
                        ce.defaultModel,
                        ce.endpointUrl,
                        ce.apiKey,
                        ""
                      )
              }
            />
          );
        })}
      </div>
    </motion.div>
  );
}
