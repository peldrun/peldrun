import { z } from "zod";

/**
 * Storage Schema definition for PELDRUN-Web.
 * Explicitly separates SSR-critical cookies from client-only local storage.
 */
export const storageSchema = {
  // ------------------------------------------------------------
  // SSR-critical cookies (Layout & rendering preferences)
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
  // Client-only configuration (Local runtime preferences)
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

  active_model: {
    tier: "local",
    schema: z.string().min(1).max(256),
    defaultValue: "qwen3-vl-8b-instruct",
  },

  active_provider: {
    tier: "local",
    schema: z.string().min(1).max(128),
    defaultValue: "LM Studio (Local)",
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

  api_base_url: {
    tier: "local",
    schema: z.string().max(2048),
    defaultValue: "http://127.0.0.1:1234/v1",
  },

  custom_prompt: {
    tier: "local",
    schema: z.string().max(100000),
    defaultValue: "",
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
  return storageSchema[key].tier === "cookie";
}