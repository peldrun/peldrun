import type { ModelMetadata } from "./types";

export const METADATA_STORAGE_KEY = "omweb_models_metadata_vault";

export interface InferredCapabilities {
  isVision: boolean;
  isTools: boolean;
  isReasoning: boolean;
  contextDisplay: string | null;
  paramsDisplay: string | null;
  quantDisplay: string | null;
  publisherDisplay: string | null;
  archDisplay: string | null;
  formatDisplay: string | null;
  loadedContext: number | null;
  maxContext: number | null;
}

export function getLocalMetadataVault(): Record<string, ModelMetadata> {
  if (typeof window === "undefined") return {};
  try {
    const raw = localStorage.getItem(METADATA_STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export async function persistMetadataToServer(entries: Record<string, Partial<ModelMetadata>>) {
  try {
    // Relative path so the request is routed through Next.js rewrites
    // (configured at build time via PELDRUN_BACKEND_URL). Using an
    // absolute localhost URL would bypass those rewrites and silently
    // pin the frontend to a hardcoded backend.
    await fetch("/api/config/models-metadata", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ metadata: entries })
    });
  } catch (err) {
    console.warn("Could not sync metadata with backend storage:", err);
  }
}

export function saveLocalMetadataVault(newEntries: Record<string, Partial<ModelMetadata>>, syncServer = true) {
  if (typeof window === "undefined") return;
  try {
    const current = getLocalMetadataVault();
    const updated = { ...current, ...newEntries };
    localStorage.setItem(METADATA_STORAGE_KEY, JSON.stringify(updated));
    window.dispatchEvent(new CustomEvent("omweb:metadata-updated", { detail: updated }));

    if (syncServer) {
      persistMetadataToServer(newEntries);
    }
  } catch (e) {
    console.error("Failed to save models metadata:", e);
  }
}

/**
 * Toggle reasoning mode manually for any model and persist permanently across reloads.
 */
export function toggleModelReasoning(
  modelKey: string | null | undefined,
  forceState?: boolean
): boolean {
  if (typeof window === "undefined") return false;
  if (!modelKey || typeof modelKey !== "string" || !modelKey.trim()) {
    return false;
  }
  const currentVault = getLocalMetadataVault();
  const currentMeta = currentVault[modelKey] || { key: modelKey, type: "llm" };
  const currentCaps = inferModelCapabilities(modelKey, currentMeta);

  const nextState = typeof forceState === "boolean" ? forceState : !currentCaps.isReasoning;

  const updatedMeta: ModelMetadata = {
    ...currentMeta,
    capabilities: {
      vision: currentCaps.isVision,
      trained_for_tool_use: currentCaps.isTools,
      ...currentMeta.capabilities,
      reasoning: nextState
    }
  };

  saveLocalMetadataVault({ [modelKey]: updatedMeta }, true);
  return nextState;
}

export async function fetchServerMetadata(): Promise<Record<string, ModelMetadata>> {
  try {
    // Relative path - see next.config.mjs rewrites.
    const res = await fetch("/api/config/models-metadata");
    if (res.ok) {
      const data = await res.json();
      if (data.ok && data.metadata) {
        // Merge server metadata with local manual overrides preserving user toggles
        const localVault = getLocalMetadataVault();
        const merged: Record<string, ModelMetadata> = { ...data.metadata };

        for (const [key, val] of Object.entries(localVault)) {
          if (val && typeof val.capabilities?.reasoning === "boolean") {
            merged[key] = {
              ...(merged[key] || {}),
              ...val,
              capabilities: {
                ...(merged[key]?.capabilities || {}),
                ...val.capabilities
              }
            };
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

export function getModelMetadata(modelKey: string): ModelMetadata | null {
  const vault = getLocalMetadataVault();
  return vault[modelKey] || null;
}

/**
 * Universal capability inference function conforming to 2026 AI industry standards.
 */
export function inferModelCapabilities(
  modelKey: string | null | undefined,
  explicitMeta?: Partial<ModelMetadata> | null
): InferredCapabilities {
  // The storage schema now allows `active_model` to be `null` when the
  // user has not yet selected a model. Every UI surface that renders
  // model badges (chat landing, composer, engine selector, chat header)
  // calls this function. Rather than forcing each caller to guard
  // manually, we return a neutral capability profile here. This keeps
  // the "unselected" state a first-class concept: no badges shown, no
  // crash, no forced default model.
  if (!modelKey || typeof modelKey !== "string" || !modelKey.trim()) {
    return {
      isVision: false,
      isTools: false,
      isReasoning: false,
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

  const meta = explicitMeta || getModelMetadata(modelKey) || {};
  const lowerKey = modelKey.toLowerCase();

  // 1. Vision Capability
  const isVision = Boolean(
    meta.capabilities?.vision ||
    lowerKey.includes("vision") ||
    lowerKey.includes("-vl") ||
    lowerKey.includes("llava") ||
    lowerKey.includes("omni") ||
    lowerKey.includes("4o") ||
    lowerKey.includes("gemini") ||
    lowerKey.includes("claude-3")
  );

  // 2. Tool / Function Calling Capability
  const isTools = Boolean(
    meta.capabilities?.trained_for_tool_use ||
    lowerKey.includes("instruct") ||
    lowerKey.includes("tool") ||
    lowerKey.includes("function") ||
    lowerKey.includes("-it") ||
    lowerKey.includes("nemotron") ||
    lowerKey.includes("gpt") ||
    lowerKey.includes("claude") ||
    lowerKey.includes("gemini") ||
    lowerKey.includes("qwen") ||
    lowerKey.includes("llama-3")
  );

  // 3. Reasoning / Deep Thinking Capability
  // Explicit manual toggle takes precedence, followed by modern heuristics
  const isReasoning = typeof meta.capabilities?.reasoning === "boolean"
    ? meta.capabilities.reasoning
    : Boolean(
        lowerKey.includes("think") ||
        lowerKey.includes("reason") ||
        lowerKey.includes("-r1") ||
        lowerKey.includes("deepseek-r1") ||
        lowerKey.includes("claude-3-7") ||
        lowerKey.includes("claude-3.7") ||
        lowerKey.includes("gemini-2.0-flash") ||
        lowerKey.includes("gemini-2.5") ||
        lowerKey.includes("qwen-max") ||
        lowerKey.includes("qwq") ||
        lowerKey.includes("o1") ||
        lowerKey.includes("o3") ||
        lowerKey.includes("o4")
      );

  // 4. Context Window resolution with fallback to known standard model specifications
  const loadedContext = meta.loaded_instances && meta.loaded_instances[0]?.config?.context_length
    ? meta.loaded_instances[0].config.context_length
    : null;

  let maxContext = meta.max_context_length || null;
  if (!maxContext) {
    if (lowerKey.includes("gemini-1.5") || lowerKey.includes("gemini-2.0") || lowerKey.includes("gemini-2.5")) maxContext = 1048576;
    else if (lowerKey.includes("claude-3-7") || lowerKey.includes("claude-3.7") || lowerKey.includes("claude-3-5")) maxContext = 200000;
    else if (lowerKey.includes("gpt-4o") || lowerKey.includes("o1") || lowerKey.includes("o3") || lowerKey.includes("o4")) maxContext = 128000;
    else if (lowerKey.includes("deepseek")) maxContext = 131072;
    else if (lowerKey.includes("llama-3.1") || lowerKey.includes("llama-3.2") || lowerKey.includes("llama-3.3")) maxContext = 131072;
    else if (lowerKey.includes("qwen3")) maxContext = 262144;
    else if (lowerKey.includes("qwen2.5")) maxContext = 131072;
    else if (lowerKey.includes("gemma-4")) maxContext = 262144;
    else if (lowerKey.includes("nemotron")) maxContext = 20000;
  }

  let contextDisplay: string | null = null;
  if (loadedContext && maxContext) {
    contextDisplay = `${Math.round(loadedContext / 1024)}K / ${Math.round(maxContext / 1024)}K ctx`;
  } else if (loadedContext) {
    contextDisplay = `${Math.round(loadedContext / 1024)}K ctx`;
  } else if (maxContext) {
    contextDisplay = maxContext >= 1024 ? `${Math.round(maxContext / 1024)}K ctx` : `${maxContext} ctx`;
  }

  // 5. Parameter Count
  let paramsDisplay: string | null = meta.params_string || null;
  if (!paramsDisplay) {
    const match = lowerKey.match(/(\d+(\.\d+)?b)/);
    if (match) paramsDisplay = match[1].toUpperCase();
  }

  // 6. Quantization
  // Use `lowerKey` (already normalized above) instead of `modelKey`
  // directly. This was the source of the runtime crash that broke
  // /chat when `active_model` was null: `modelKey.match(...)` on a
  // null input throws `TypeError: Cannot read properties of null`.
  // `lowerKey` is guaranteed to be a string by the guard at the top
  // of this function.
  let quantDisplay: string | null = meta.quantization?.name || null;
  if (!quantDisplay) {
    const qMatch = lowerKey.match(/(q\d+_[a-z0-9_]+|qat|fp16|bf16|int8|int4)/i);
    if (qMatch) quantDisplay = qMatch[1].toUpperCase();
  }

  return {
    isVision,
    isTools,
    isReasoning,
    contextDisplay,
    paramsDisplay,
    quantDisplay,
    publisherDisplay: meta.publisher || null,
    archDisplay: meta.architecture || null,
    formatDisplay: meta.format ? meta.format.toUpperCase() : null,
    loadedContext,
    maxContext
  };
}
