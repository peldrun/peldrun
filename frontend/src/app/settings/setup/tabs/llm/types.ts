// frontend/src/app/settings/setup/tabs/llm/types.ts
"use client";

import type { Dispatch, SetStateAction } from "react";

/**
 * Loosely-typed metadata entry produced by the backend model scanner.
 * Unknown keys are permitted to avoid coupling to backend evolution.
 */
import type { getModelMetadata } from "@/lib/modelMetadata";
import type { InferredCapabilities as InferredCapabilitiesType } from "@/lib/modelMetadata";

/**
 * Central type definitions for the LLM tab module.
 *
 * Shared domain types (FullAppConfig, LMStudioSettings, OllamaSettings,
 * CloudProviderVaultItem, CustomEndpoint, ApiModeType, HubSubTab) are
 * re-exported from the sibling `../types` module to preserve a single
 * source of truth across the entire setup wizard.
 *
 * This file does NOT redefine those shapes — it only adds the extended
 * structures required by the refactored, modular LLM tab.
 */

/* -------------------------------------------------------------------------- */
/* Re-exports of canonical shared types                                       */
/* -------------------------------------------------------------------------- */

export type {
  HubSubTab,
  CustomEndpoint,
  CloudProviderVaultItem,
  LMStudioSettings,
  OllamaSettings,
  FullAppConfig,
  ApiModeType,
} from "../../types";

import type {
  HubSubTab,
  CustomEndpoint,
  CloudProviderVaultItem,
  LMStudioSettings,
  OllamaSettings,
  FullAppConfig,
  ApiModeType,
} from "../../types";

/* -------------------------------------------------------------------------- */
/* Custom Endpoint extension — provider logo persistence                      */
/* -------------------------------------------------------------------------- */

/**
 * Extended custom endpoint shape carrying an optional provider logo key.
 *
 * The logo key is resolved at render time through the icon system
 * (`public/icon/manifest.json`) and is persisted alongside the endpoint so
 * that user-created providers visually match the built-in ones.
 */
export interface CustomEndpointWithLogo extends CustomEndpoint {
  badge?: string;
}

/* -------------------------------------------------------------------------- */
/* Endpoint creation form state                                               */
/* -------------------------------------------------------------------------- */

/**
 * Local form state for the "Add Custom Endpoint" panel.
 * Mirrors the original EMPTY_ENDPOINT_FORM shape plus the new `badge`.
 */
export interface EndpointFormState {
  name: string;
  providerId: string;
  endpointUrl: string;
  apiMode: ApiModeType;
  defaultModel: string;
  contextWindow: string;
  apiKey: string;
  useForNewChats: boolean;
  badge: string;
}

/* -------------------------------------------------------------------------- */
/* Model discovery                                                            */
/* -------------------------------------------------------------------------- */

/**
 * Target descriptor passed to the model discovery modal. Covers both
 * cloud providers and user-defined custom endpoints.
 */
export interface DiscoveryTarget {
  id: string;
  name: string;
  baseUrl: string;
  apiKey: string;
  type?: string;
  savedModels?: string[];
  isCustom?: boolean;
  badge?: string;
}

/* -------------------------------------------------------------------------- */
/* Model metadata vault                                                       */
/* -------------------------------------------------------------------------- */




export type ModelMetadataVault = Record<string, ModelMetadataEntry>;

export interface ModelMetadataEntry {
  type: string;
  key: string;
  id?: string;
  name?: string;
  max_context_length?: number;
  context_length?: number;
  capabilities?: string[];
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Inferred capability descriptor (used by model item cards)                  */
/* -------------------------------------------------------------------------- */

/**
 * Re-exported from `@/lib/modelMetadata` to guarantee a single canonical
 * shape across the app. Local redefinition would drift from the source and
 * cause assignment errors against `inferModelCapabilities`.
 */
export type { InferredCapabilities } from "@/lib/modelMetadata";


/* -------------------------------------------------------------------------- */
/* Root component props                                                       */
/* -------------------------------------------------------------------------- */

export interface LLMTabProps {
  config: FullAppConfig;
  setConfig: Dispatch<SetStateAction<FullAppConfig>>;
  lmStudioSettings: LMStudioSettings;
  setLmStudioSettings: Dispatch<SetStateAction<LMStudioSettings>>;
  ollamaSettings: OllamaSettings;
  setOllamaSettings: Dispatch<SetStateAction<OllamaSettings>>;
  cloudProviders: CloudProviderVaultItem[];
  setCloudProviders: Dispatch<SetStateAction<CloudProviderVaultItem[]>>;
  customEndpoints: CustomEndpoint[];
  setCustomEndpoints: Dispatch<SetStateAction<CustomEndpoint[]>>;
  availableModels: string[];
  setAvailableModels: Dispatch<SetStateAction<string[]>>;
  fetchingModels: boolean;
  scanError: string | null;
  handleFetchModels: () => void;
  assignDetectedModel: (model: string, target: "primary" | "vision") => void;
  activateEngine: (
    providerId: string,
    providerName: string,
    model: string,
    baseUrl: string,
    apiKey: string,
    apiType: string
  ) => void;
  deactivateToDefault: () => void;
  testEndpoint: (
    baseUrl: string,
    apiKey: string,
    model: string,
    apiType?: string
  ) => Promise<{ ok: boolean; message: string; latency?: number }>;
}

/* -------------------------------------------------------------------------- */
/* Sub-component props                                                        */
/* -------------------------------------------------------------------------- */

export interface LLMHeaderProps {
  activeSubTab: HubSubTab;
  onSubTabChange: (tab: HubSubTab) => void;
}

export interface OverviewTabProps extends LLMTabProps {
  metadataVault: ModelMetadataVault;
  autoDetectActiveModelLimit: () => void;
  effectiveContextWindow: number;
  effectiveMaxOutput: number;
  effectiveInputBudget: number;
  sanitizeTokenInput: (raw: unknown, fallback?: number) => number;
  handleTestLMStudio: () => Promise<void>;
  handleTestOllama: () => Promise<void>;
  handleTestCloud: (providerId: string) => Promise<void>;
  handleTestAll: () => Promise<void>;
  testingAll: boolean;
  testingId: string | null;
}

export interface LocalEnginesTabProps extends LLMTabProps {
  metadataVault: ModelMetadataVault;
  newLMModelInput: string;
  setNewLMModelInput: (value: string) => void;
  newOllamaModelInput: string;
  setNewOllamaModelInput: (value: string) => void;
  scanningOllama: boolean;
  testingId: string | null;
  onOpenModelInfo: (model: string) => void;
  handleTestLMStudio: () => Promise<void>;
  handleTestOllama: () => Promise<void>;
  handleAddLMModel: () => void;
  handleRemoveLMModel: (model: string) => void;
  handleAddOllamaModel: () => void;
  handleRemoveOllamaModel: (model: string) => void;
  handleClearOllama: () => void;
  handleScanOllama: () => Promise<void>;
}

export interface CloudProvidersTabProps extends LLMTabProps {
  testingId: string | null;
  showKeys: Record<string, boolean>;
  toggleKey: (id: string) => void;
  handleTestCloud: (providerId: string) => Promise<void>;
  onOpenDiscovery: (target: DiscoveryTarget) => void;
}

export interface CustomEndpointsTabProps extends LLMTabProps {
  endpointForm: EndpointFormState;
  setEndpointForm: Dispatch<SetStateAction<EndpointFormState>>;
  testingId: string | null;
  handleTestCustomForm: () => Promise<void>;
  handleSaveCustomEndpoint: () => void;
  handleDeleteCustomEndpoint: (id: string) => void;
  onOpenDiscovery: (target: DiscoveryTarget) => void;
}

export interface ConnectivityMatrixProps extends LLMTabProps {
  testingAll: boolean;
  testingId: string | null;
  handleTestAll: () => Promise<void>;
  handleTestLMStudio: () => Promise<void>;
  handleTestOllama: () => Promise<void>;
  handleTestCloud: (providerId: string) => Promise<void>;
}

export interface ContextBudgetPanelProps {
  config: FullAppConfig;
  setConfig: Dispatch<SetStateAction<FullAppConfig>>;
  effectiveContextWindow: number;
  effectiveMaxOutput: number;
  effectiveInputBudget: number;
  sanitizeTokenInput: (raw: unknown, fallback?: number) => number;
  onAutoDetect: () => void;
}

export interface ProviderIconPickerProps {
  value: string;
  onChange: (iconName: string) => void;
  accent?: string;
  disabled?: boolean;
}

export interface ModelItemCardProps {
  model: string;
  isActive: boolean;
  accent: string;
  capabilities: InferredCapabilitiesType;
  onSetPrimary: () => void;
  onSetVision?: () => void;
  onRemove: () => void;
  onOpenInfo: () => void;
}

export interface CloudProviderCardProps {
  provider: CloudProviderVaultItem;
  isPrimary: boolean;
  isVision: boolean;
  isTesting: boolean;
  showKey: boolean;
  onToggleKey: () => void;
  onChange: (patch: Partial<CloudProviderVaultItem>) => void;
  onTest: () => void;
  onActivate: () => void;
  onSetVision: () => void;
  onOpenDiscovery: () => void;
}
