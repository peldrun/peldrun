// frontend/src/lib/storage/schema.ts

import { z } from "zod";

/**
 * Storage schema for PELDRUN-Web.
 *
 * Explicitly separates SSR-critical cookies from client-only local
 * storage.
 *
 * Design rule: this schema MUST NOT carry hardcoded defaults for any
 * value whose authoritative source lives on the backend. Doing so
 * silently pins the browser to stale values (a model that was
 * uninstalled, a port that was reassigned, a provider that was
 * replaced). Instead, the corresponding fields are nullable/empty and
 * the application is expected to populate them on first use from the
 * backend configuration endpoint (e.g. `/api/llm/config`) or from the
 * active provider discovery response.
 *
 * Backward compatibility: existing users who already have a concrete
 * value stored in localStorage keep it. Only the *initial* default for
 * new users changes.
 */
export const storageSchema = {
  // ------------------------------------------------------------
  // SSR-critical cookies (layout & rendering preferences)
  // ------------------------------------------------------------
  sidebar_state: {
    tier: "cookie",
    schema: z.enum(["expanded", "collapsed"]),
    defaultValue: "expanded",
  },

  right_panel_open: {
    tier: "cookie",
    schema: z.boolean(),
    defaultValue: false,
  },

  theme: {
    tier: "cookie",
    schema: z.enum(["light", "dark", "system"]),
    defaultValue: "system",
  },

  locale: {
    tier: "cookie",
    schema: z.string().regex(/^[a-z]{2}(?:-[A-Z]{2})?$/),
    defaultValue: "en",
  },

  // ------------------------------------------------------------
  // Client-only configuration (local runtime preferences)
  // ------------------------------------------------------------
  exec_mode: {
    tier: "local",
    schema: z.enum(["agent", "chat"]),
    defaultValue: "agent",
  },

  reasoning_effort: {
    tier: "local",
    schema: z.enum(["none", "low", "medium", "high"]),
    defaultValue: "none",
  },

  // The active model selection. `null` means "not yet selected": the
  // consuming code MUST resolve the effective model from the backend
  // (config.toml / model discovery) instead of assuming any name.
  active_model: {
    tier: "local",
    schema: z.string().max(256).nullable(),
    defaultValue: null,
  },

  // The active provider display label. `null` means "not yet selected":
  // consuming code MUST resolve the effective provider from backend
  // configuration. Do not assume a specific provider is installed.
  active_provider: {
    tier: "local",
    schema: z.string().max(128).nullable(),
    defaultValue: null,
  },

  selected_engine: {
    tier: "local",
    schema: z.string().min(1).max(128),
    defaultValue: "peldrun-agent",
  },

  active_llm_override: {
    tier: "local",
    schema: z.string().max(4096).nullable(),
    defaultValue: null,
  },

  // The LM Studio endpoint override. Empty string means "not
  // overridden": the backend remains the source of truth (see
  // backend/peldrun/llm/providers/lmstudio.py, which honors
  // LMSTUDIO_BASE_URL). Populate this only when the user explicitly
  // chooses to bypass the backend default.
  api_base_url: {
    tier: "local",
    schema: z.string().max(2048),
    defaultValue: "",
  },

  custom_prompt: {
    tier: "local",
    schema: z.string().max(100000),
    defaultValue: "",
  },

  // Multi-agent delegation governance toggle (Phase M3 / PR 6).
  peldrun_delegation_allowed: {
    tier: "local",
    schema: z.boolean(),
    defaultValue: false,
  },
} as const;

export type StorageSchema = typeof storageSchema;
export type StorageKey = keyof StorageSchema;
export type StorageTier = StorageSchema[StorageKey]["tier"];
export type StorageValue<K extends StorageKey> = z.infer<StorageSchema[K]["schema"]>;

export type StorageSnapshot = {
  [K in StorageKey]: StorageValue<K>;
};

export function isCookieKey<K extends StorageKey>(key: K): boolean {
  // Defensive guard against undefined access.
  const definition = storageSchema[key];
  return definition?.tier === "cookie";
}

/**
 * Return true when a stored value carries no useful information and
 * the caller should fall back to the backend-provided default.
 *
 * Convenience helper for consuming code so that the migration away
 * from hardcoded defaults stays consistent across call sites:
 *
 *   const model = isUnset(storage.active_model)
 *     ? await fetchBackendDefaultModel()
 *     : storage.active_model;
 */
export function isUnset(value: unknown): boolean {
  return value === null || value === undefined || value === "";
}
