// frontend/src/lib/modelMetadata.ts

import type { ModelMetadata } from "./types";

export const METADATA_STORAGE_KEY = "omweb_models_metadata_vault";

/* -------------------------------------------------------------------------- */
/* Source vocabulary                                                          */
/* -------------------------------------------------------------------------- */

export type CapabilitySource =
  | "manual"
  | "provider_live"
  | "model_metadata"
  | "provider_metadata"
  | "huggingface_config"
  | "external_registry"
  | "provider_catalog"
  | "capability_probe"
  | "runtime_observation"
  | "unknown";

/* -------------------------------------------------------------------------- */
/* Capability facts (from /api/catalog/models/{id})                           */
/* -------------------------------------------------------------------------- */

export interface CapabilityFact {
  value: boolean;
  source: CapabilitySource | string;
  known: boolean;
}

export interface ModelFacts {
  ok: boolean;
  provider_id: string;
  model_id: string;

  vision: CapabilityFact;
  tools: CapabilityFact;
  reasoning: CapabilityFact;

  reasoning_mode: string;
  reasoning_effort_levels: string[];
  reasoning_default_effort: string | null;
  reasoning_min_budget_tokens: number | null;
  reasoning_max_budget_tokens: number | null;

  structured_output_mode: string;

  audio: CapabilityFact;
  modalities_input: string[];
  modalities_output: string[];

  context_window: number | null;
  context_source: string | null;

  external_matches: Record<string, string>;
}

/* -------------------------------------------------------------------------- */
/* InferredCapabilities (sync, from local vault)                              */
/* -------------------------------------------------------------------------- */

export interface InferredCapabilities {
  /**
   * Canonical provider identifier (e.g. "ollama", "lmstudio", "openai").
   * Populated from the caller's providerId argument. Used by the Info
   * Modal to distinguish "Run Scan Models" (actionable for Ollama /
   * LM Studio / Google) from "Provider does not advertise capabilities"
   * (OpenAI / Anthropic / DeepSeek).
   */
  provider_id: string | null;

  isVision: boolean;
  isTools: boolean;
  isReasoning: boolean;

  visionState: "supported" | "unsupported" | "unknown";
  toolsState: "supported" | "unsupported" | "unknown";
  reasoningState: "supported" | "unsupported" | "unknown";

  visionSource: CapabilitySource;
  toolsSource: CapabilitySource;
  reasoningSource: CapabilitySource;

  reasoningMode: string;
  reasoningEffortLevels: string[];
  reasoningDefaultEffort: string | null;
  reasoningMinBudgetTokens: number | null;
  reasoningMaxBudgetTokens: number | null;

  matchedMetadataKey: string | null;

  contextDisplay: string | null;
  paramsDisplay: string | null;
  quantDisplay: string | null;
  publisherDisplay: string | null;
  archDisplay: string | null;
  formatDisplay: string | null;
  loadedContext: number | null;
  maxContext: number | null;
}

/* -------------------------------------------------------------------------- */
/* Composite metadata key helpers                                             */
/* -------------------------------------------------------------------------- */

export function buildModelMetadataKey(
  providerId: string | null | undefined,
  modelId: string,
  connectionId?: string | null
): string {
  const provider = (providerId || "").trim().toLowerCase();
  const model = (modelId || "").trim();
  const conn = (connectionId || "").trim();
  if (conn) return `${provider}::${conn}::${model}`;
  if (provider) return `${provider}::${model}`;
  return model;
}

export function getMetadataCandidateKeys(
  providerId: string | null | undefined,
  modelId: string,
  connectionId?: string | null
): string[] {
  const provider = (providerId || "").trim().toLowerCase();
  const model = (modelId || "").trim();
  const conn = (connectionId || "").trim();
  const keys: string[] = [];
  const push = (k: string) => {
    if (k && !keys.includes(k)) keys.push(k);
  };

  const modelVariants: string[] = [model];
  if (model.includes("/")) {
    const short = model.split("/").pop();
    if (short && short !== model) modelVariants.push(short);
  } else if (model) {
    modelVariants.push(`models/${model}`);
  }

  if (conn) for (const m of modelVariants) push(`${provider}::${conn}::${m}`);
  if (provider) for (const m of modelVariants) push(`${provider}::${m}`);
  for (const m of modelVariants) push(m);

  return keys;
}

export function lookupModelMetadata(
  vault: Record<string, ModelMetadata>,
  providerId: string | null | undefined,
  modelId: string,
  connectionId?: string | null
): { meta: ModelMetadata | null; matchedKey: string | null } {
  const candidates = getMetadataCandidateKeys(providerId, modelId, connectionId);
  for (const key of candidates) {
    const entry = vault[key];
    if (entry && typeof entry === "object") {
      return { meta: entry, matchedKey: key };
    }
  }
  return { meta: null, matchedKey: null };
}

/* -------------------------------------------------------------------------- */
/* Vault I/O                                                                  */
/* -------------------------------------------------------------------------- */

export function getLocalMetadataVault(): Record<string, ModelMetadata> {
  if (typeof window === "undefined") return {};
  try {
    const raw = localStorage.getItem(METADATA_STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export async function persistMetadataToServer(
  entries: Record<string, Partial<ModelMetadata>>
) {
  try {
    await fetch("/api/config/models-metadata", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ metadata: entries }),
    });
  } catch (err) {
    console.warn("Could not sync metadata with backend storage:", err);
  }
}

export function saveLocalMetadataVault(
  newEntries: Record<string, Partial<ModelMetadata>>,
  syncServer = true
) {
  if (typeof window === "undefined") return;
  try {
    const current = getLocalMetadataVault();
    const updated = { ...current, ...newEntries };
    localStorage.setItem(METADATA_STORAGE_KEY, JSON.stringify(updated));
    window.dispatchEvent(
      new CustomEvent("omweb:metadata-updated", { detail: updated })
    );
    if (syncServer) {
      persistMetadataToServer(newEntries);
    }
  } catch (e) {
    console.error("Failed to save models metadata:", e);
  }
}

/* -------------------------------------------------------------------------- */
/* Fetch merged facts from the backend catalog                                */
/* -------------------------------------------------------------------------- */

/**
 * Fetch capability facts from the backend catalog layer.
 *
 * The backend merges the local vault (which includes the last Scan
 * Models results with `provider_live` sources), the LiteLLM external
 * registry, and HuggingFace model configs into a single response with
 * per-field source annotations. This function never triggers a
 * provider query — that flow is triggered explicitly by Scan Models.
 *
 * Returns `null` on any failure (network, 4xx, 5xx, malformed JSON).
 */
export async function fetchModelFacts(
  providerId: string,
  modelId: string,
  connectionId?: string | null
): Promise<ModelFacts | null> {
  if (!providerId || !modelId) return null;
  try {
    const params = new URLSearchParams({ provider: providerId });
    if (connectionId) params.set("connection_id", connectionId);
    const url = `/api/catalog/models/${encodeURIComponent(
      modelId
    )}?${params.toString()}`;
    const res = await fetch(url);
    if (!res.ok) return null;
    const data = (await res.json()) as ModelFacts;
    return data && data.ok ? data : null;
  } catch (err) {
    console.warn(`fetchModelFacts failed for ${providerId}/${modelId}:`, err);
    return null;
  }
}

/* -------------------------------------------------------------------------- */
/* User override writers                                                      */
/* -------------------------------------------------------------------------- */

function toggleBooleanCapability(
  modelKey: string,
  field: "vision" | "trained_for_tool_use",
  forceState?: boolean
): boolean {
  const vault = getLocalMetadataVault();
  const currentMeta = vault[modelKey] || { key: modelKey, type: "llm" };
  const currentCaps = inferModelCapabilities(modelKey, currentMeta);
  const currentBool =
    field === "vision" ? currentCaps.isVision : currentCaps.isTools;
  const nextState = typeof forceState === "boolean" ? forceState : !currentBool;

  const currentSources =
    ((currentMeta as unknown as { capability_sources?: Record<string, string> })
      .capability_sources) || {};

  const updatedMeta = {
    ...currentMeta,
    capabilities: {
      ...currentMeta.capabilities,
      [field]: nextState,
    },
    capability_sources: {
      ...currentSources,
      [field]: "manual",
    },
  } as unknown as ModelMetadata;

  saveLocalMetadataVault({ [modelKey]: updatedMeta }, true);
  return nextState;
}

export function toggleModelVision(
  modelKey: string | null | undefined,
  forceState?: boolean
): boolean {
  if (typeof window === "undefined") return false;
  if (!modelKey || !modelKey.trim()) return false;
  return toggleBooleanCapability(modelKey, "vision", forceState);
}

export function toggleModelTools(
  modelKey: string | null | undefined,
  forceState?: boolean
): boolean {
  if (typeof window === "undefined") return false;
  if (!modelKey || !modelKey.trim()) return false;
  return toggleBooleanCapability(modelKey, "trained_for_tool_use", forceState);
}

export function toggleModelReasoning(
  modelKey: string | null | undefined,
  forceState?: boolean
): boolean {
  if (typeof window === "undefined") return false;
  if (!modelKey || !modelKey.trim()) return false;

  const currentVault = getLocalMetadataVault();
  const currentMeta = currentVault[modelKey] || { key: modelKey, type: "llm" };
  const currentCaps = inferModelCapabilities(modelKey, currentMeta);
  const nextState =
    typeof forceState === "boolean" ? forceState : !currentCaps.isReasoning;

  const currentDetails =
    ((currentMeta as unknown as { capability_details?: Record<string, unknown> })
      .capability_details) || {};
  const currentReasoningDetail =
    (currentDetails.reasoning as Record<string, unknown> | undefined) || {};

  const currentMode =
    typeof currentReasoningDetail.mode === "string"
      ? currentReasoningDetail.mode
      : "toggle";
  const effectiveMode =
    currentMode === "automatic" || currentMode === "adaptive"
      ? "toggle"
      : currentMode;

  const updatedMeta = {
    ...currentMeta,
    capabilities: {
      ...currentMeta.capabilities,
      reasoning: nextState,
    },
    capability_sources: {
      ...((currentMeta as unknown as {
        capability_sources?: Record<string, string>;
      }).capability_sources || {}),
      reasoning: "manual",
    },
    capability_details: {
      ...currentDetails,
      reasoning: {
        ...currentReasoningDetail,
        mode: effectiveMode,
        state: nextState ? "supported" : "unsupported",
      },
    },
  } as unknown as ModelMetadata;

  saveLocalMetadataVault({ [modelKey]: updatedMeta }, true);
  return nextState;
}

export function setModelReasoningEffort(
  modelKey: string | null | undefined,
  effort: string
): void {
  if (!modelKey || !modelKey.trim() || !effort) return;

  const vault = getLocalMetadataVault();
  const currentMeta = vault[modelKey] || { key: modelKey, type: "llm" };
  const currentDetails =
    ((currentMeta as unknown as { capability_details?: Record<string, unknown> })
      .capability_details) || {};
  const currentReasoningDetail =
    (currentDetails.reasoning as Record<string, unknown> | undefined) || {};

  const updatedMeta = {
    ...currentMeta,
    capabilities: {
      ...currentMeta.capabilities,
      reasoning: true,
    },
    capability_sources: {
      ...((currentMeta as unknown as {
        capability_sources?: Record<string, string>;
      }).capability_sources || {}),
      reasoning: "manual",
    },
    capability_details: {
      ...currentDetails,
      reasoning: {
        ...currentReasoningDetail,
        mode: "effort",
        effort,
        state: "supported",
      },
    },
  } as unknown as ModelMetadata;

  saveLocalMetadataVault({ [modelKey]: updatedMeta }, true);
}

/* -------------------------------------------------------------------------- */
/* Server sync                                                                */
/* -------------------------------------------------------------------------- */

export async function fetchServerMetadata(): Promise<
  Record<string, ModelMetadata>
> {
  try {
    const res = await fetch("/api/config/models-metadata");
    if (res.ok) {
      const data = await res.json();
      if (data.ok && data.metadata) {
        const localVault = getLocalMetadataVault();
        const merged: Record<string, ModelMetadata> = { ...data.metadata };

        for (const [key, val] of Object.entries(localVault)) {
          if (!val) continue;
          const localSources =
            (val as unknown as { capability_sources?: Record<string, string> })
              .capability_sources || {};
          const hasManualOverride =
            localSources.vision === "manual" ||
            localSources.trained_for_tool_use === "manual" ||
            localSources.reasoning === "manual";
          if (hasManualOverride) {
            merged[key] = {
              ...(merged[key] || {}),
              ...val,
              capabilities: {
                ...(merged[key]?.capabilities || {}),
                ...val.capabilities,
              },
              capability_sources: {
                ...(((merged[key] as unknown as {
                  capability_sources?: Record<string, string>;
                }).capability_sources) || {}),
                ...localSources,
              },
              capability_details: {
                ...(((merged[key] as unknown as {
                  capability_details?: Record<string, unknown>;
                }).capability_details) || {}),
                ...(((val as unknown as {
                  capability_details?: Record<string, unknown>;
                }).capability_details) || {}),
              },
            } as ModelMetadata;
          }
        }

        saveLocalMetadataVault(merged, false);
        return merged;
      }
    }
  } catch (err) {
    console.warn("Could not load backend metadata cache:", err);
  }
  return getLocalMetadataVault();
}

export function getModelMetadata(
  modelKey: string,
  providerId?: string | null,
  connectionId?: string | null
): ModelMetadata | null {
  const vault = getLocalMetadataVault();
  if (providerId) {
    const { meta } = lookupModelMetadata(
      vault,
      providerId,
      modelKey,
      connectionId
    );
    if (meta) return meta;
  }
  return vault[modelKey] || null;
}

/* -------------------------------------------------------------------------- */
/* Metadata reader (sync, from vault only)                                    */
/* -------------------------------------------------------------------------- */

/**
 * Read capability information for a model FROM THE LOCAL VAULT ONLY.
 *
 * No network, no heuristics, no name-pattern matching. Every field
 * comes from one of:
 *   - `capabilities` + `capability_sources` written by Scan Models
 *     (source = "provider_live") or by the user via the modal
 *     (source = "manual") or by legacy saveLocalMetadataVault (source
 *     = "provider_catalog" / "model_metadata").
 *   - `capability_details.reasoning` for the reasoning control mode.
 *
 * When a field is absent, the state is `"unknown"` and the boolean is
 * `false`. The UI shows an Enable button — it never fabricates a
 * badge.
 *
 * For the richer merged view (with LiteLLM/HF fallbacks), use the
 * async `fetchModelFacts()` function which hits `/api/catalog/...`.
 */
export function inferModelCapabilities(
  modelKey: string | null | undefined,
  explicitMeta?: Partial<ModelMetadata> | null,
  providerId?: string | null,
  connectionId?: string | null
): InferredCapabilities {
  // Null guard.
  if (!modelKey || typeof modelKey !== "string" || !modelKey.trim()) {
    return {
      provider_id: providerId || null,
      isVision: false,
      isTools: false,
      isReasoning: false,
      visionState: "unknown",
      toolsState: "unknown",
      reasoningState: "unknown",
      visionSource: "unknown",
      toolsSource: "unknown",
      reasoningSource: "unknown",
      reasoningMode: "unreported",
      reasoningEffortLevels: [],
      reasoningDefaultEffort: null,
      reasoningMinBudgetTokens: null,
      reasoningMaxBudgetTokens: null,
      matchedMetadataKey: null,
      contextDisplay: null,
      paramsDisplay: null,
      quantDisplay: null,
      publisherDisplay: null,
      archDisplay: null,
      formatDisplay: null,
      loadedContext: null,
      maxContext: null,
    };
  }

  // Metadata resolution (composite → legacy).
  let meta: Partial<ModelMetadata> = (explicitMeta ||
    {}) as Partial<ModelMetadata>;
  let matchedKey: string | null = null;

  if (explicitMeta) {
    matchedKey = modelKey;
  } else if (providerId) {
    const vault = getLocalMetadataVault();
    const lookup = lookupModelMetadata(vault, providerId, modelKey, connectionId);
    if (lookup.meta) {
      meta = lookup.meta as Partial<ModelMetadata>;
      matchedKey = lookup.matchedKey;
    } else {
      const legacyEntry = getModelMetadata(modelKey);
      if (legacyEntry) {
        meta = legacyEntry as Partial<ModelMetadata>;
        matchedKey = modelKey;
      }
    }
  } else {
    const legacyEntry = getModelMetadata(modelKey);
    if (legacyEntry) {
      meta = legacyEntry as Partial<ModelMetadata>;
      matchedKey = modelKey;
    }
  }

  const explicitSources = ((meta as unknown as {
    capability_sources?: Record<string, string>;
  }).capability_sources || {}) as Record<string, string>;

  const explicitDetails = ((meta as unknown as {
    capability_details?: Record<string, unknown>;
  }).capability_details || {}) as Record<string, unknown>;

  // ------------------------------------------------------------------
  // Three-valued state helpers.
  //
  // `undefined` (unknown) is NOT the same as `false` (explicitly
  // unsupported). Providers that only return {id, object} never set
  // these booleans — the user must be able to enable them manually.
  // ------------------------------------------------------------------
  const visionBool: boolean | undefined =
    typeof meta.capabilities?.vision === "boolean"
      ? meta.capabilities.vision
      : undefined;
  const toolsBool: boolean | undefined =
    typeof meta.capabilities?.trained_for_tool_use === "boolean"
      ? meta.capabilities.trained_for_tool_use
      : undefined;
  const reasoningBool: boolean | undefined =
    typeof meta.capabilities?.reasoning === "boolean"
      ? meta.capabilities.reasoning
      : undefined;

  const asState = (
    v: boolean | undefined
  ): "supported" | "unsupported" | "unknown" =>
    v === true ? "supported" : v === false ? "unsupported" : "unknown";

  const visionState = asState(visionBool);
  const toolsState = asState(toolsBool);
  const reasoningState = asState(reasoningBool);

  // ------------------------------------------------------------------
  // Reasoning mode resolution.
  // ------------------------------------------------------------------
  const reasoningDetail =
    (explicitDetails.reasoning as Record<string, unknown> | undefined) || {};
  const explicitMode =
    typeof reasoningDetail.mode === "string" ? reasoningDetail.mode : "";

  let reasoningMode: string;
  if (explicitMode && explicitMode !== "") {
    reasoningMode = explicitMode;
  } else if (reasoningBool === true) {
    reasoningMode = "toggle";
  } else if (reasoningBool === false) {
    reasoningMode = "none";
  } else {
    reasoningMode = "unreported";
  }

  const isReasoning = reasoningBool === true;
  const isVision = visionBool === true;
  const isTools = toolsBool === true;

  // ------------------------------------------------------------------
  // Source annotations — only meaningful when the value is known.
  // ------------------------------------------------------------------
  const asSource = (
    bool: boolean | undefined,
    source: string | undefined
  ): CapabilitySource => {
    if (bool === undefined) return "unknown";
    return ((source as CapabilitySource) || "manual") as CapabilitySource;
  };

  const visionSource = asSource(visionBool, explicitSources.vision);
  const toolsSource = asSource(toolsBool, explicitSources.trained_for_tool_use);
  const reasoningSource = asSource(reasoningBool, explicitSources.reasoning);

  // ------------------------------------------------------------------
  // Reasoning sub-config (from metadata only).
  // ------------------------------------------------------------------
  const effortLevels: string[] = Array.isArray(reasoningDetail.effort_levels)
    ? (reasoningDetail.effort_levels as unknown[]).filter(
        (v): v is string => typeof v === "string"
      )
    : [];
  const defaultEffort: string | null =
    typeof reasoningDetail.default_effort === "string"
      ? (reasoningDetail.default_effort as string)
      : null;
  const minBudget: number | null =
    typeof reasoningDetail.min_budget_tokens === "number"
      ? (reasoningDetail.min_budget_tokens as number)
      : null;
  const maxBudget: number | null =
    typeof reasoningDetail.max_budget_tokens === "number"
      ? (reasoningDetail.max_budget_tokens as number)
      : null;

  // ------------------------------------------------------------------
  // Informational fields — ONLY from metadata.
  // ------------------------------------------------------------------
  const loadedContext: number | null =
    ((meta.loaded_instances as unknown as Array<{
      config?: { context_length?: number };
    }> | undefined)?.[0]?.config?.context_length) ?? null;

  const maxContext: number | null =
    (meta.max_context_length as number | undefined) ?? null;

  let contextDisplay: string | null = null;
  if (loadedContext && maxContext) {
    contextDisplay = `${Math.round(loadedContext / 1024)}K / ${Math.round(
      maxContext / 1024
    )}K ctx`;
  } else if (loadedContext) {
    contextDisplay = `${Math.round(loadedContext / 1024)}K ctx`;
  } else if (maxContext) {
    contextDisplay =
      maxContext >= 1024
        ? `${Math.round(maxContext / 1024)}K ctx`
        : `${maxContext} ctx`;
  }

  const paramsDisplay: string | null =
    (meta.params_string as string | undefined) || null;
  const quantDisplay: string | null =
    ((meta.quantization as { name?: string } | undefined)?.name) || null;

  return {
    provider_id: providerId || null,
    isVision,
    isTools,
    isReasoning,
    visionState,
    toolsState,
    reasoningState,
    visionSource,
    toolsSource,
    reasoningSource,
    reasoningMode,
    reasoningEffortLevels: effortLevels,
    reasoningDefaultEffort: defaultEffort,
    reasoningMinBudgetTokens: minBudget,
    reasoningMaxBudgetTokens: maxBudget,
    matchedMetadataKey: matchedKey,
    contextDisplay,
    paramsDisplay,
    quantDisplay,
    publisherDisplay: (meta.publisher as string | undefined) || null,
    archDisplay: (meta.architecture as string | undefined) || null,
    formatDisplay: (meta.format as string | undefined)
      ? (meta.format as string).toUpperCase()
      : null,
    loadedContext,
    maxContext,
  };
}
