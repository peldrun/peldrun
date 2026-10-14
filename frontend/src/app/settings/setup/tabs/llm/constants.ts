// frontend/src/app/settings/setup/tabs/llm/constants.ts
"use client";

import type { EndpointFormState } from "./types";

/* -------------------------------------------------------------------------- */
/* Endpoint form seed                                                         */
/* -------------------------------------------------------------------------- */

/**
 * Neutral seed state for the "Add Custom Endpoint" panel.
 * Extended with `providerLogo` for the visual identity of user-defined providers.
 */
export const EMPTY_ENDPOINT_FORM: EndpointFormState = {
  name: "",
  providerId: "",
  endpointUrl: "",
  apiMode: "Auto-detect",
  defaultModel: "",
  contextWindow: "Auto",
  apiKey: "",
  useForNewChats: true,
  providerLogo: "",
};

/* -------------------------------------------------------------------------- */
/* Provider icon map                                                          */
/* -------------------------------------------------------------------------- */

/**
 * Canonical mapping between provider identifiers and their SVG icon asset
 * inside `public/icon/`. Keys are matched case-insensitively against the
 * provider id/name at render time; the first successful match wins.
 *
 * Icons are referenced by base name only (no extension), matching the
 * convention used by the `Icon` component and `manifest.json`.
 */
export const PROVIDER_ICON_MAP: Readonly<Record<string, string>> = {
  // Local engines
  lmstudio: "lm-studio",
  "lm-studio": "lm-studio",
  ollama: "ollama",

  // Frontier / cloud providers
  openai: "openai-chatgpt",
  chatgpt: "openai-chatgpt",
  anthropic: "anthropic",
  claude: "claude-ai",
  google: "google",
  gemini: "gemini-google",
  "ai-studio": "ai-studio-google",
  "ai-studio-google": "ai-studio-google",
  meta: "meta",
  llama: "meta",
  mistral: "mistral-ai",
  "mistral-ai": "mistral-ai",
  deepseek: "deepseek",
  qwen: "qwen",
  alibaba: "qwen",
  nvidia: "nvidia",
  ibm: "ibm",
  watsonx: "ibm",
  gigachat: "gigachat",
  sber: "gigachat",
  bytedance: "bytedance",
  doubao: "bytedance",
  jina: "jina-ai",
  "jina-ai": "jina-ai",
  liquid: "liquid",
  "essential-ai": "essential-ai",
  essentialai: "essential-ai",
  microsoft: "microsoft",
  azure: "microsoft",
  copilot: "microsoft",
  huggingface: "hugging-face",
  "hugging-face": "hugging-face",
};

/* -------------------------------------------------------------------------- */
/* Curated icon catalogue for the ProviderIconPicker                          */
/* -------------------------------------------------------------------------- */

/**
 * Curated, user-facing subset of `public/icon/` entries that make sense as
 * visual identities for AI providers. Kept intentionally short to avoid
 * overwhelming the picker; users can still reach the full inventory by
 * extending this list at a later time.
 */
export interface ProviderLogoOption {
  /** Icon base name as used by `<Icon name="..." />`. */
  value: string;
  /** Human-readable label rendered inside the picker. */
  label: string;
  /** Provider brand accent, used as a preview tint. */
  accent: string;
}

export const PROVIDER_LOGO_OPTIONS: readonly ProviderLogoOption[] = [
  { value: "openai-chatgpt", label: "OpenAI / ChatGPT", accent: "#10A37F" },
  { value: "anthropic", label: "Anthropic", accent: "#D97757" },
  { value: "claude-ai", label: "Claude AI", accent: "#D97757" },
  { value: "google", label: "Google", accent: "#4285F4" },
  { value: "gemini-google", label: "Gemini", accent: "#4285F4" },
  { value: "ai-studio-google", label: "AI Studio (Google)", accent: "#4285F4" },
  { value: "meta", label: "Meta / LLaMA", accent: "#0668E1" },
  { value: "mistral-ai", label: "Mistral AI", accent: "#FA520F" },
  { value: "deepseek", label: "DeepSeek", accent: "#4D6BFE" },
  { value: "qwen", label: "Qwen (Alibaba)", accent: "#615CED" },
  { value: "nvidia", label: "NVIDIA", accent: "#76B900" },
  { value: "ibm", label: "IBM / watsonx", accent: "#0F62FE" },
  { value: "gigachat", label: "GigaChat", accent: "#00C9A7" },
  { value: "bytedance", label: "ByteDance / Doubao", accent: "#325AB4" },
  { value: "jina-ai", label: "Jina AI", accent: "#8B5CF6" },
  { value: "liquid", label: "Liquid AI", accent: "#00A3FF" },
  { value: "essential-ai", label: "Essential AI", accent: "#6366F1" },
  { value: "microsoft", label: "Microsoft / Azure", accent: "#0078D4" },
  { value: "hugging-face", label: "Hugging Face", accent: "#FFD21E" },
  { value: "lm-studio", label: "LM Studio", accent: "#6366F1" },
  { value: "ollama", label: "Ollama", accent: "#000000" },
  { value: "searxng", label: "SearXNG", accent: "#3050FF" },
  { value: "tavily", label: "Tavily", accent: "#E85E32" },
  { value: "brave", label: "Brave", accent: "#FB542B" },
  { value: "duckduckgo", label: "DuckDuckGo", accent: "#DE5833" },
  { value: "yandex", label: "Yandex", accent: "#FC3F1D" },
];

/* -------------------------------------------------------------------------- */
/* Icon resolution helper                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Resolves a provider identifier or display name to an icon base name.
 *
 * Resolution order:
 *   1. Explicit `PROVIDER_ICON_MAP` lookup on the normalized key.
 *   2. Normalized key itself (for direct matches against the icon inventory).
 *   3. Fallback to `"rect"` — the neutral placeholder shipped with the system.
 */
export function resolveProviderIcon(idOrName?: string | null): string {
  if (!idOrName) return "rect";
  const key = String(idOrName).trim().toLowerCase().replace(/\s+/g, "-");
  return PROVIDER_ICON_MAP[key] ?? key ?? "rect";
}
