// frontend/src/app/stores/_hooks/useStoreData.ts

"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchStoreAgents,
  fetchStoreTools,
  fetchStoreExtensions,
} from "@/lib/chatsApi";
import type {
  AgentManifest,
  ToolDefinition,
  ExtensionDefinition,
} from "@/lib/types";
import { listBundles, type BundleEntry } from "@/lib/bundleApi";
import { showToast } from "@/components/ui/ToastNotification";

/**
 * Return the given API endpoint unchanged.
 *
 * All /api/* requests flow through Next.js rewrites, which are
 * configured at build time in `next.config.mjs` via the
 * PELDRUN_BACKEND_URL environment variable. Returning an absolute URL
 * here (based on `window.location.port`) would break SSR/CSR parity:
 *
 *   - On the server, `window` is undefined, so the function would
 *     return the relative path "/api/...".
 *   - On the client, the same call would return
 *     "http://localhost:8088/api/...".
 *
 * The two results differ, so React reports a hydration mismatch on
 * every attribute derived from this value (e.g. <img src> and
 * mask-image in AgentIcon). Rewrites already route to the configured
 * backend regardless of port, so the absolute URL is unnecessary.
 *
 * The function is kept (rather than inlined) as a single extension
 * point for future cross-cutting concerns (auth headers, tracing)
 * without touching every call site.
 */
export function getApiUrl(endpoint: string): string {
  return endpoint;
}

/**
 * Return type of {@link useStoreData}, re-used by action hooks and sections
 * to describe the shared catalog state without duplicating type literals.
 */
export interface StoreDataState {
  agents: AgentManifest[];
  setAgents: React.Dispatch<React.SetStateAction<AgentManifest[]>>;
  tools: ToolDefinition[];
  setTools: React.Dispatch<React.SetStateAction<ToolDefinition[]>>;
  extensions: ExtensionDefinition[];
  setExtensions: React.Dispatch<React.SetStateAction<ExtensionDefinition[]>>;
  bundles: BundleEntry[];
  setBundles: React.Dispatch<React.SetStateAction<BundleEntry[]>>;
  loading: boolean;
  reload: () => Promise<void>;
}

/**
 * Central data hook for the store page.
 *
 * Responsibilities:
 *  - Owns the four catalog collections (agents, tools, extensions, bundles).
 *  - Exposes their setters so action hooks can apply optimistic updates
 *    without needing to re-fetch the whole catalog.
 *  - Provides a single `reload()` used after destructive or stateful
 *    operations (upload, delete bundle, activate version, ...).
 *  - Fires on mount; any later refresh is explicit via `reload()`.
 */
export function useStoreData(): StoreDataState {
  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [extensions, setExtensions] = useState<ExtensionDefinition[]>([]);
  const [bundles, setBundles] = useState<BundleEntry[]>([]);
  const [loading, setLoading] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [ag, tl, ex, bd] = await Promise.all([
        fetchStoreAgents(),
        fetchStoreTools(),
        fetchStoreExtensions(),
        // Bundles are non-critical: a failure must not break the page.
        listBundles().catch(() => [] as BundleEntry[]),
      ]);
      setAgents(ag || []);
      setTools(tl || []);
      setExtensions(ex || []);
      setBundles(bd || []);
    } catch (err: any) {
      console.error("Failed to load store catalog:", err);
      showToast.error(
        "Store Load Error",
        err?.message || "Failed to load store catalog."
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return {
    agents,
    setAgents,
    tools,
    setTools,
    extensions,
    setExtensions,
    bundles,
    setBundles,
    loading,
    reload,
  };
}
