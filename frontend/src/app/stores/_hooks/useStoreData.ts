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
 * Resolve the backend API base URL.
 * When the dev server runs on one of the common local ports, requests
 * are routed to the dedicated backend port (8088). Otherwise the given
 * endpoint is returned unchanged so it flows through the same-origin
 * proxy that Next.js provides.
 *
 * Exported as a standalone helper because every action hook needs it.
 */
export function getApiUrl(endpoint: string): string {
  if (typeof window !== "undefined") {
    const port = window.location.port;
    if (port === "3088" || port === "3000") {
      return `http://localhost:8088${endpoint}`;
    }
  }
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
