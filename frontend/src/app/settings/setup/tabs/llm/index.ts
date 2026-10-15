// frontend/src/app/settings/setup/tabs/llm/index.ts
"use client";

/* -------------------------------------------------------------------------- */
/* Root component                                                             */
/* -------------------------------------------------------------------------- */

export { LLMTab } from "./LLMTab";

/* -------------------------------------------------------------------------- */
/* Sub-components                                                             */
/* -------------------------------------------------------------------------- */

export { LLMHeader } from "./components/LLMHeader";
export { OverviewTab } from "./components/OverviewTab";
export { LocalEnginesTab } from "./components/LocalEnginesTab";
export { CloudProvidersTab } from "./components/CloudProvidersTab";
export { CustomEndpointsTab } from "./components/CustomEndpointsTab";
export { ConnectivityMatrix } from "./components/ConnectivityMatrix";
export { ContextBudgetPanel } from "./components/ContextBudgetPanel";
export { ProviderIconPicker } from "./components/ProviderIconPicker";
export { ModelItemCard } from "./components/ModelItemCard";
export { CloudProviderCard } from "./components/CloudProviderCard";

/* -------------------------------------------------------------------------- */
/* Domain hooks                                                               */
/* -------------------------------------------------------------------------- */

export { useDiscoveryTarget } from "./hooks/useDiscoveryTarget";
export { useLMStudioModels } from "./hooks/useLMStudioModels";
export { useOllamaModels } from "./hooks/useOllamaModels";
export { useCloudProviders } from "./hooks/useCloudProviders";
export { useCustomEndpoints } from "./hooks/useCustomEndpoints";

/* -------------------------------------------------------------------------- */
/* Constants and helpers                                                      */
/* -------------------------------------------------------------------------- */

export {
  EMPTY_ENDPOINT_FORM,
  PROVIDER_ICON_MAP,
  PROVIDER_LOGO_OPTIONS,
  resolveProviderIcon,
} from "./constants";

export {
  STORAGE_KEYS,
  ensureOllamaV1,
  isDevPortSession,
  readLocalJson,
  removeLocal,
  resolveApiUrl,
  sanitizeTokenInput,
  slugifyProviderName,
  writeLocalJson,
} from "./utils";

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */

export type {
  /* Re-exported shared types */
  HubSubTab,
  CustomEndpoint,
  CloudProviderVaultItem,
  LMStudioSettings,
  OllamaSettings,
  FullAppConfig,
  ApiModeType,

  /* LLM-specific extensions */
  CustomEndpointWithLogo,
  EndpointFormState,
  DiscoveryTarget,
  ModelMetadataEntry,
  ModelMetadataVault,
  InferredCapabilities,

  /* Component prop contracts */
  LLMTabProps,
  LLMHeaderProps,
  OverviewTabProps,
  LocalEnginesTabProps,
  CloudProvidersTabProps,
  CustomEndpointsTabProps,
  ConnectivityMatrixProps,
  ContextBudgetPanelProps,
  ProviderIconPickerProps,
  ModelItemCardProps,
  CloudProviderCardProps,
} from "./types";

/* -------------------------------------------------------------------------- */
/* Hook result types                                                          */
/* -------------------------------------------------------------------------- */

export type {
  UseDiscoveryTargetResult,
} from "./hooks/useDiscoveryTarget";

export type {
  UseLMStudioModelsParams,
  UseLMStudioModelsResult,
} from "./hooks/useLMStudioModels";

export type {
  UseOllamaModelsParams,
  UseOllamaModelsResult,
} from "./hooks/useOllamaModels";

export type {
  UseCloudProvidersParams,
  UseCloudProvidersResult,
} from "./hooks/useCloudProviders";

export type {
  UseCustomEndpointsParams,
  UseCustomEndpointsResult,
} from "./hooks/useCustomEndpoints";

/* -------------------------------------------------------------------------- */
/* Constants re-exported as values (typed)                                    */
/* -------------------------------------------------------------------------- */

export type { badgeOption } from "./constants";
