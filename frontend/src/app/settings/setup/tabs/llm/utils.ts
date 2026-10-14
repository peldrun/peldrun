// frontend/src/app/settings/setup/tabs/llm/utils.ts
"use client";

/* -------------------------------------------------------------------------- */
/* Ports and backend resolution                                               */
/* -------------------------------------------------------------------------- */

/**
 * Frontend dev ports that redirect API calls to the backend on port 8088.
 * Mirrors the inline check used across the original LLM tab.
 */
const DEV_FRONTEND_PORTS = new Set(["3088", "3000"]);

/**
 * Backend origin used when the frontend is served from a known dev port.
 * Kept centralised so every call site behaves identically.
 */
const DEV_BACKEND_ORIGIN = "http://localhost:8088";

/**
 * Returns `true` when the current browser session runs on a dev port that
 * requires redirecting API calls to the local backend.
 */
export function isDevPortSession(): boolean {
  if (typeof window === "undefined") return false;
  return DEV_FRONTEND_PORTS.has(window.location.port);
}

/**
 * Resolves a backend endpoint to an absolute URL when needed.
 *
 * In dev sessions (ports 3088 / 3000) the call is redirected to the local
 * backend on port 8088. Otherwise the endpoint is returned unchanged, which
 * keeps the same-origin behaviour in production builds.
 */
export function resolveApiUrl(endpoint: string): string {
  if (isDevPortSession()) {
    return `${DEV_BACKEND_ORIGIN}${endpoint}`;
  }
  return endpoint;
}

/* -------------------------------------------------------------------------- */
/* Token input sanitisation                                                   */
/* -------------------------------------------------------------------------- */

/**
 * Normalises a token-count input coming from a text/number field.
 *
 * Rules (unchanged from the original LLM tab):
 *   - Strips every non-digit character.
 *   - Falls back to `fallback` when the result is empty or non-positive.
 *   - Clamps the result to a hard upper bound of 2,000,000 tokens.
 */
export function sanitizeTokenInput(
  raw: unknown,
  fallback: number = 8192
): number {
  const clean = parseInt(String(raw).replace(/[^0-9]/g, ""), 10);
  if (isNaN(clean) || clean <= 0) return fallback;
  if (clean > 2_000_000) return 2_000_000;
  return clean;
}

/* -------------------------------------------------------------------------- */
/* String helpers                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Produces a URL-safe slug from a free-form provider name.
 * Used when auto-generating a provider id from a display name.
 */
export function slugifyProviderName(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, "-");
}

/**
 * Ensures an Ollama base URL ends with the OpenAI-compatible `/v1` suffix.
 * Idempotent: returns the input unchanged when `/v1` is already present.
 */
export function ensureOllamaV1(baseUrl: string): string {
  if (!baseUrl) return baseUrl;
  return baseUrl.endsWith("/v1") ? baseUrl : `${baseUrl}/v1`;
}

/* -------------------------------------------------------------------------- */
/* LocalStorage keys                                                          */
/* -------------------------------------------------------------------------- */

/**
 * Canonical storage keys used by the LLM tab. Centralised here so that no
 * component defines a hard-coded key string.
 */
export const STORAGE_KEYS = {
  scannedModels: "omweb_scanned_models",
  lmStudioVault: "omweb_lmstudio_vault",
  ollamaVault: "omweb_ollama_vault",
  cloudVault: "omweb_cloud_vault",
  customEndpoints: "omweb_custom_endpoints",
} as const;

/* -------------------------------------------------------------------------- */
/* LocalStorage helpers                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Safely reads and parses a JSON value from localStorage.
 * Returns `fallback` on any parse error or when running on the server.
 */
export function readLocalJson<T>(key: string, fallback: T): T {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

/**
 * Safely serialises and writes a JSON value to localStorage.
 * Silently no-ops on the server or when serialisation fails.
 */
export function writeLocalJson(key: string, value: unknown): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* Non-fatal: quota or private-mode failures are ignored on purpose. */
  }
}

/**
 * Removes a key from localStorage. Safe to call on the server.
 */
export function removeLocal(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* Non-fatal. */
  }
}
