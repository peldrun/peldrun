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
import {
  listProviders,
  type ProviderApiRequestError,
} from "@/lib/providerApi";
import type {
  ProviderPackageEntry,
  SkillSummary,
} from "../_types/store";
import { showToast } from "@/components/ui/ToastNotification";

/**
 * Return the given API endpoint unchanged.
 *
 * All /api/* requests flow through Next.js rewrites, which are
 * configured at build time in `next.config.mjs` via the
 * PELDRUN_BACKEND_URL environment variable. Returning an absolute URL
 * here (based on `window.location.port`) would break SSR/CSR parity.
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
  skills: SkillSummary[];
  setSkills: React.Dispatch<React.SetStateAction<SkillSummary[]>>;
  extensions: ExtensionDefinition[];
  setExtensions: React.Dispatch<React.SetStateAction<ExtensionDefinition[]>>;
  bundles: BundleEntry[];
  setBundles: React.Dispatch<React.SetStateAction<BundleEntry[]>>;
  /**
   * Installed Provider Packages (M9 / M10).
   *
   * Populated from ``GET /api/store/providers`` — every registered
   * package, enabled or disabled. The catalog of *installable*
   * packages is fetched separately by the Providers section (via
   * ``listCatalog()``) because it is a distinct data source and is
   * not needed until the user opens that section.
   *
   * An empty array means "no provider packages installed" or "the
   * request failed silently" (see the reload() comment for why a
   * failure here does not block the rest of the catalog).
   */
  providers: ProviderPackageEntry[];
  setProviders: React.Dispatch<React.SetStateAction<ProviderPackageEntry[]>>;
  loading: boolean;
  reload: () => Promise<void>;
}

/**
 * Fetch the installed skills catalog.
 *
 * Uses ``include_disabled=true`` so the store page shows disabled
 * skills too (the operator needs to see them to re-enable them).
 * Chat-invocation filtering happens elsewhere — this is the
 * management surface.
 */
async function fetchSkills(): Promise<SkillSummary[]> {
  try {
    const res = await fetch("/api/skills?include_disabled=true");
    if (!res.ok) return [];
    const data = await res.json();
    const list = Array.isArray(data?.skills) ? data.skills : [];
    return list as SkillSummary[];
  } catch {
    return [];
  }
}

/**
 * Central data hook for the store page.
 *
 * Responsibilities:
 *  - Owns the six catalog collections (agents, tools, skills,
 *    extensions, bundles, providers).
 *  - Exposes their setters so action hooks can apply optimistic updates
 *    without needing to re-fetch the whole catalog.
 *  - Provides a single `reload()` used after destructive or stateful
 *    operations (upload, delete bundle, activate version, ...).
 *  - Fires on mount; any later refresh is explicit via `reload()`.
 */
export function useStoreData(): StoreDataState {
  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [extensions, setExtensions] = useState<ExtensionDefinition[]>([]);
  const [bundles, setBundles] = useState<BundleEntry[]>([]);
  const [providers, setProviders] = useState<ProviderPackageEntry[]>([]);
  const [loading, setLoading] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [ag, tl, sk, ex, bd, pv] = await Promise.all([
        fetchStoreAgents(),
        fetchStoreTools(),
        fetchSkills(),
        fetchStoreExtensions(),
        // Bundles are non-critical: a failure must not break the page.
        listBundles().catch(() => [] as BundleEntry[]),
        // Providers are non-critical for the same reason: a failure
        // (e.g. a stale registry, an unreachable worker) must not
        // prevent the Agents / Tools / Skills sections from
        // rendering. The failure is logged for diagnostics.
        listProviders().catch((err: ProviderApiRequestError) => {
          console.warn(
            "Provider list failed (non-fatal):",
            err?.code || err?.message || err,
          );
          return [] as ProviderPackageEntry[];
        }),
      ]);
      setAgents(ag || []);
      setTools(tl || []);
      setSkills(sk || []);
      setExtensions(ex || []);
      setBundles(bd || []);
      setProviders(pv || []);
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
    skills,
    setSkills,
    extensions,
    setExtensions,
    bundles,
    setBundles,
    providers,
    setProviders,
    loading,
    reload,
  };
}
