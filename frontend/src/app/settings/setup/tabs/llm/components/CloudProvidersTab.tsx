// frontend/src/app/settings/setup/tabs/llm/components/CloudProvidersTab.tsx
"use client";

import React from "react";
import { showToast } from "@/components/ui/ToastNotification";
import { CloudProviderCard } from "./CloudProviderCard";
import type { CloudProvidersTabProps } from "../types";

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Sub-tab 3 of the LLM settings: cloud provider credential management.
 *
 * Iterates over the vault items and renders one `CloudProviderCard` per
 * provider. All mutations and probes are delegated to the shared
 * `useCloudProviders` hook via the props supplied by the container.
 */
export function CloudProvidersTab({
  config,
  setConfig,
  cloudProviders,
  setCloudProviders,
  activateEngine,
  testingId,
  showKeys,
  toggleKey,
  handleTestCloud,
  onOpenDiscovery,
}: CloudProvidersTabProps) {
  /* -------------------------- Actions ----------------------------------- */

  const handleActivate = (providerId: string) => {
    const cp = cloudProviders.find((p) => p.id === providerId);
    if (!cp) return;
    activateEngine(cp.id, cp.name, cp.model, cp.baseUrl, cp.apiKey, cp.type);
  };

  const handleSetVision = (providerId: string) => {
    const cp = cloudProviders.find((p) => p.id === providerId);
    if (!cp) return;

    setConfig((prev) => ({
      ...prev,
      llm_vision: {
        ...prev.llm_vision,
        provider: cp.id,
        provider_name: cp.name,
        model: cp.model,
        base_url: cp.baseUrl,
        api_key: cp.apiKey,
      },
    }));

    showToast.info("Vision Engine Assigned", `${cp.name} (${cp.model})`);
  };

  const handleUpdateProvider = (
    providerId: string,
    patch: Parameters<typeof setCloudProviders>[0] extends (
      prev: infer _P
    ) => infer _R
      ? any
      : any
  ) => {
    setCloudProviders((prev) =>
      prev.map((item) =>
        item.id === providerId ? { ...item, ...patch } : item
      )
    );
  };

  const handleOpenDiscovery = (providerId: string) => {
    const cp = cloudProviders.find((p) => p.id === providerId);
    if (!cp) return;

    if (!cp.apiKey.trim()) {
      showToast.warning(
        `${cp.name} API Key Required`,
        "Please enter an API key before scanning models."
      );
      return;
    }

    onOpenDiscovery({
      id: cp.id,
      name: cp.name,
      baseUrl: cp.baseUrl,
      apiKey: cp.apiKey,
      type: cp.type,
      savedModels: cp.savedModels || [],
      isCustom: false,
    });
  };

  /* -------------------------- Render ------------------------------------ */

  return (
    <div className="w-full max-w-7xl space-y-5 pt-2 mx-auto">
      {cloudProviders.map((cp) => {
        const isPrimary =
          config.llm.provider === cp.id || config.llm.base_url === cp.baseUrl;
        const isVision =
          config.llm_vision.provider === cp.id ||
          config.llm_vision.base_url === cp.baseUrl;

        return (
          <CloudProviderCard
            key={cp.id}
            provider={cp}
            isPrimary={isPrimary}
            isVision={isVision}
            isTesting={testingId === cp.id}
            showKey={Boolean(showKeys[cp.id])}
            onToggleKey={() => toggleKey(cp.id)}
            onChange={(patch) => handleUpdateProvider(cp.id, patch)}
            onTest={() => handleTestCloud(cp.id)}
            onActivate={() => handleActivate(cp.id)}
            onSetVision={() => handleSetVision(cp.id)}
            onOpenDiscovery={() => handleOpenDiscovery(cp.id)}
          />
        );
      })}
    </div>
  );
}
