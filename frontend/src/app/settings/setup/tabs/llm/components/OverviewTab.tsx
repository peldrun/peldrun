// frontend/src/app/settings/setup/tabs/llm/components/OverviewTab.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import { Icon } from "@/components/Icon/Icon";
import { resolveProviderIcon } from "../constants";
import { ConnectivityMatrix } from "./ConnectivityMatrix";
import { ContextBudgetPanel } from "./ContextBudgetPanel";
import type { OverviewTabProps } from "../types";

/* -------------------------------------------------------------------------- */
/* Constants                                                                  */
/* -------------------------------------------------------------------------- */

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

/* -------------------------------------------------------------------------- */
/* Engine summary card                                                        */
/* -------------------------------------------------------------------------- */

interface EngineCardProps {
  title: string;
  badgeLabel: string;
  badgeClassName: string;
  accentClassName: string;
  providerId: string;
  providerName: string;
  model: string;
  baseUrl: string;
  isActive?: boolean;
  delay?: number;
}

/**
 * Compact summary card describing one engine slot (primary or vision).
 * Mirrors the original "Primary Autonomous Engine" / "Visual Perception
 * Engine" cards, restyled to match the SearchTab reference design.
 */
function EngineCard({
  title,
  badgeLabel,
  badgeClassName,
  accentClassName,
  providerId,
  providerName,
  model,
  baseUrl,
  isActive = false,
  delay = 0,
}: EngineCardProps) {
  const iconName = resolveProviderIcon(providerId || providerName);

  return (
    <motion.div
      {...FADE_UP}
      transition={{ ...FADE_UP.transition, delay }}
      whileHover={{ y: -2, transition: { duration: 0.15 } }}
      className={`relative p-4 rounded-md border space-y-3 shadow-sm transition-all ${accentClassName}`}
    >
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="shrink-0 flex items-center justify-center text-primary">
            <Icon name={iconName} size={16} title={`${providerName} logo`} />
          </span>
          <span className="text-[10px] font-bold text-primary uppercase tracking-wider">
            {title}
          </span>
        </div>

        <span
          className={`flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-sm border ${badgeClassName}`}
        >
          {isActive && (
            <span className="h-1.5 w-1.5 rounded-full bg-current animate-pulse" />
          )}
          {badgeLabel}
        </span>
      </div>

      <div className="flex items-center justify-between gap-2 flex-wrap">
        <span className="text-sm font-bold font-mono text-foreground truncate">
          {model}
        </span>
        <span className="text-[10px] font-bold px-2 py-0.5 rounded-sm bg-card border border-border text-foreground">
          {providerName}
        </span>
      </div>

      <span
        className="text-[10px] text-muted-foreground font-mono block truncate"
        title={baseUrl}
      >
        {baseUrl}
      </span>
    </motion.div>
  );
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Sub-tab 1 of the LLM settings: high-level overview of the active engines,
 * the connectivity health matrix, and the context budget architecture panel.
 *
 * Purely presentational — all handlers and derived values are supplied by the
 * parent container so that this component stays testable in isolation.
 */
export function OverviewTab({
  config,
  setConfig,
  lmStudioSettings,
  ollamaSettings,
  cloudProviders,
  customEndpoints,
  activateEngine,
  deactivateToDefault,
  testEndpoint,
  effectiveContextWindow,
  effectiveMaxOutput,
  effectiveInputBudget,
  sanitizeTokenInput,
  autoDetectActiveModelLimit,
  handleTestAll,
  testingAll,
}: OverviewTabProps) {
  const primaryProviderName =
    config.llm.provider_name || config.llm.provider || "Unknown";
  const visionProviderName =
    config.llm_vision.provider_name || config.llm_vision.provider || "Unknown";

  return (
    <div className="w-full max-w-7xl space-y-6 pt-2 mx-auto">
      {/* Engine summary cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <EngineCard
          title="Primary Autonomous Engine [llm]"
          badgeLabel="Active"
          badgeClassName="text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/20"
          accentClassName="border-primary/50 bg-primary/5 ring-1 ring-primary/20"
          providerId={config.llm.provider}
          providerName={primaryProviderName}
          model={config.llm.model}
          baseUrl={config.llm.base_url}
          isActive
          delay={0}
        />

        <EngineCard
          title="Visual Perception Engine [llm.vision]"
          badgeLabel="Perception"
          badgeClassName="text-violet-500 bg-violet-500/10 border-violet-500/20"
          accentClassName="border-border bg-card"
          providerId={config.llm_vision.provider}
          providerName={visionProviderName}
          model={config.llm_vision.model}
          baseUrl={config.llm_vision.base_url}
          delay={0.05}
        />
      </div>

      {/* Connectivity matrix */}
      <ConnectivityMatrix
        config={config}
        setConfig={setConfig}
        lmStudioSettings={lmStudioSettings}
        setLmStudioSettings={() => {}}
        ollamaSettings={ollamaSettings}
        setOllamaSettings={() => {}}
        cloudProviders={cloudProviders}
        setCloudProviders={() => {}}
        customEndpoints={customEndpoints}
        setCustomEndpoints={() => {}}
        availableModels={[]}
        setAvailableModels={() => {}}
        fetchingModels={false}
        scanError={null}
        handleFetchModels={() => {}}
        assignDetectedModel={() => {}}
        activateEngine={activateEngine}
        deactivateToDefault={deactivateToDefault}
        testEndpoint={testEndpoint}
        testingAll={testingAll}
        testingId={null}
        handleTestAll={handleTestAll}
        handleTestLMStudio={async () => {}}
        handleTestOllama={async () => {}}
        handleTestCloud={async () => {}}
      />

      {/* Context budget panel */}
      <ContextBudgetPanel
        config={config}
        setConfig={setConfig}
        effectiveContextWindow={effectiveContextWindow}
        effectiveMaxOutput={effectiveMaxOutput}
        effectiveInputBudget={effectiveInputBudget}
        sanitizeTokenInput={sanitizeTokenInput}
        onAutoDetect={autoDetectActiveModelLimit}
      />
    </div>
  );
}
