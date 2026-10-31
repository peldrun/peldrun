// frontend/src/app/settings/setup/tabs/SearchTab.tsx
"use client";

import React, { useState, useMemo, useEffect, useRef, useCallback } from "react";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import {
  Search, ExternalLink, Activity, Eye, EyeOff, CheckCircle2, XCircle,
  RefreshCw, Shield, ShieldOff, Gauge, Network, Sliders,
  ChevronDown, Save, Loader2, AlertCircle, Lock, Info, Hash,
  Image as ImageIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/Icon/Icon";

// ---------------------------------------------------------------------------
// Provider Presentation Registry (Capabilities sourced from backend API)
// ---------------------------------------------------------------------------
type ProviderField =
  | { key: string; label: string; type: "text"; placeholder?: string; mono?: boolean }
  | { key: string; label: string; type: "password"; placeholder?: string; secretKey?: string }
  | { key: string; label: string; type: "select"; options: string[]; defaultValue?: string };

interface ProviderDef {
  id: string;
  configKey: string;
  name: string;
  tagline: string;
  iconName: string;
  accent: string;
  defaultCost: number;
  lifecycle?: "supported" | "legacy";
  lifecycleNotice?: string;
  consumesQuotaOnTest?: boolean;
  fields: ProviderField[];
  costField: boolean;
  testLabel: string;
  docsUrl?: string;
}

export interface ProviderCapabilityMeta {
  id: string;
  name: string;
  max_results_limit: number;
  configured_max_results: number | null;
  effective_max_results: number;
  inherits_global_default: boolean;
  default_max_results: number;
  limit_type?: string;
  notes?: string;
  requires_api_key?: boolean;
  supports_image_search?: boolean;
  image_max_results_limit?: number;
}

const DEFAULT_GLOBAL_MAX_RESULTS = 5;
const DEFAULT_IMAGE_MAX_RESULTS = 5;
const FALLBACK_HARD_CAP = 20;

// Fallback set of providers known to be image-capable, used only when the
// backend capability registry is unavailable. The authoritative source is
// `capability.supports_image_search` returned by /api/search/providers.
const DEFAULT_IMAGE_CAPABLE_PROVIDERS = new Set<string>([
  "DuckDuckGo",
  "SearXNG",
  "Brave",
  "Google",
]);

const PROVIDERS: readonly ProviderDef[] = [
  {
    id: "DuckDuckGo",
    configKey: "duckduckgo",
    name: "DuckDuckGo",
    tagline: "Zero-credential fallback engine with regional & SafeSearch support",
    iconName: "duckduckgo",
    accent: "#DE5833",
    defaultCost: 0.0,
    consumesQuotaOnTest: false,
    fields: [
      { key: "safesearch", label: "SafeSearch", type: "select", options: ["moderate", "on", "off"], defaultValue: "moderate" },
      { key: "backend", label: "Backend Mode", type: "select", options: ["auto", "ddgs", "html"], defaultValue: "auto" },
    ],
    costField: true,
    testLabel: "Check Reachability",
    docsUrl: "https://github.com/deedy5/ddgs",
  },
  {
    id: "SearXNG",
    configKey: "searxng",
    name: "SearXNG",
    tagline: "Self-hosted meta-search aggregator (requires 'json' in search.formats)",
    iconName: "searxng",
    accent: "#3050FF",
    defaultCost: 0.0,
    consumesQuotaOnTest: false,
    fields: [
      { key: "base_url", label: "Instance Base URL", type: "text", placeholder: "http://localhost:8080", mono: true },
      { key: "categories", label: "Categories", type: "text", placeholder: "general", mono: true },
    ],
    costField: true,
    testLabel: "Check Instance & JSON",
    docsUrl: "https://docs.searxng.org/dev/search_api.html",
  },
  {
    id: "Tavily",
    configKey: "tavily",
    name: "Tavily AI",
    tagline: "LLM-optimized cloud search API with credit usage reporting",
    iconName: "tavily",
    accent: "#E85E32",
    defaultCost: 0.005,
    consumesQuotaOnTest: true,
    fields: [
      { key: "api_key", label: "API Key (Bearer)", type: "password", placeholder: "tvly-••••••", secretKey: "tavily" },
      { key: "search_depth", label: "Search Depth", type: "select", options: ["basic", "advanced", "fast", "ultra-fast"], defaultValue: "basic" },
    ],
    costField: true,
    testLabel: "Verify Key (1 Credit)",
    docsUrl: "https://docs.tavily.com/documentation/api-reference/endpoint/search",
  },
  {
    id: "Brave",
    configKey: "brave",
    name: "Brave Search",
    tagline: "Independent web index with regional & SafeSearch controls",
    iconName: "brave",
    accent: "#FB542B",
    defaultCost: 0.005,
    consumesQuotaOnTest: true,
    fields: [
      { key: "api_key", label: "Subscription Token", type: "password", placeholder: "BSA••••••", secretKey: "brave" },
    ],
    costField: true,
    testLabel: "Verify Token (Uses Quota)",
    docsUrl: "https://api.search.brave.com/app/documentation/web-search/get-started",
  },
  {
    id: "Google",
    configKey: "google",
    name: "Google CSE",
    tagline: "Programmable Search Engine JSON API",
    iconName: "google",
    accent: "#4285F4",
    defaultCost: 0.005,
    lifecycle: "legacy",
    lifecycleNotice:
      "Google Custom Search JSON API is closed to new customers and scheduled for retirement on 2027-01-01. Plan migration to Brave, Tavily, or SearXNG.",
    consumesQuotaOnTest: true,
    fields: [
      { key: "api_key", label: "API Key", type: "password", placeholder: "AIza••••", secretKey: "google" },
      { key: "cx", label: "Search Engine ID (cx)", type: "text", placeholder: "0123...", mono: true },
    ],
    costField: true,
    testLabel: "Verify CSE (Uses Quota)",
    docsUrl: "https://developers.google.com/custom-search/v1/overview",
  },
  {
    id: "Jina",
    configKey: "jina",
    name: "Jina Search",
    tagline: "Markdown web search endpoint via s.jina.ai",
    iconName: "jina-ai",
    accent: "#8B5CF6",
    defaultCost: 0.002,
    consumesQuotaOnTest: true,
    fields: [
      { key: "api_key", label: "Bearer Token", type: "password", placeholder: "jina_••••", secretKey: "jina" },
    ],
    costField: true,
    testLabel: "Verify Key & Search",
    docsUrl: "https://api.jina.ai/docs",
  },
  {
    id: "Yandex",
    configKey: "yandex",
    name: "Yandex Search",
    tagline: "Yandex Cloud Search API v2 (POST rawData) & Classic XML",
    iconName: "yandex",
    accent: "#FC3F1D",
    defaultCost: 0.003,
    consumesQuotaOnTest: true,
    fields: [
      { key: "mode", label: "API Mode", type: "select", options: ["cloud_v2", "classic_xml"], defaultValue: "cloud_v2" },
      { key: "api_key", label: "API Key", type: "password", placeholder: "AQVN••••", secretKey: "yandex" },
      { key: "folder_id", label: "Folder ID (Cloud v2)", type: "text", placeholder: "b1g••••", mono: true },
      { key: "region", label: "Region ID (Optional)", type: "text", placeholder: "213", mono: true },
    ],
    costField: true,
    testLabel: "Verify Credentials (Uses Quota)",
    docsUrl: "https://yandex.cloud/en/docs/search-api/api-ref/SyncSearch",
  },
] as const;

const ENGINE_OPTIONS = PROVIDERS.map((p) => p.id);

const providerIconName = (engineId: string): string =>
  PROVIDERS.find((p) => p.id === engineId)?.iconName || "search";

const providerAccent = (engineId: string): string =>
  PROVIDERS.find((p) => p.id === engineId)?.accent || "#3b82f6";

// Relative path only - SSR/CSR parity. See next.config.mjs.
const resolveApiUrl = (endpoint: string): string => {
  return endpoint;
};

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
type SaveState = "idle" | "dirty" | "saving" | "saved" | "error";

interface DiagnosticState {
  testing: boolean;
  ok?: boolean;
  status?: string;
  reachable?: boolean;
  authenticated?: boolean | null;
  quota_available?: boolean | null;
  consumes_quota?: boolean;
  latency_ms?: number;
  estimated_cost_usd?: number;
  cost_basis?: string;
  requested_max_results?: number;
  effective_max_results?: number;
  max_results_limit?: number;
  msg?: string;
}

// ---------------------------------------------------------------------------
// Validation Helper
// ---------------------------------------------------------------------------
function validatePositiveIntInRange(
  raw: any,
  label: string,
  minVal: number,
  maxVal: number
): string | null {
  if (raw === undefined || raw === null || raw === "") {
    return `${label} is required (integer between ${minVal} and ${maxVal}).`;
  }
  if (typeof raw === "boolean") {
    return `${label} must be an integer between ${minVal} and ${maxVal}.`;
  }
  const num = typeof raw === "number" ? raw : Number(String(raw).trim());
  if (!Number.isInteger(num)) {
    return `${label} must be a whole integer between ${minVal} and ${maxVal}.`;
  }
  if (num < minVal || num > maxVal) {
    return `${label} must be between ${minVal} and ${maxVal}.`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------
export function SearchTab({
  config,
  setConfig,
}: {
  config: any;
  setConfig: React.Dispatch<React.SetStateAction<any>>;
}) {
  const searchConfig = config.search || {};
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({});
  const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
  const [pings, setPings] = useState<Record<string, DiagnosticState>>({});
  const [effectiveCosts, setEffectiveCosts] = useState<Record<string, number>>({});
  const [providerCapabilities, setProviderCapabilities] = useState<
    Record<string, ProviderCapabilityMeta>
  >({});
  const [hardCap, setHardCap] = useState<number>(FALLBACK_HARD_CAP);
  const [capabilitiesError, setCapabilitiesError] = useState<string | null>(null);

  const [saveErrorMsg, setSaveErrorMsg] = useState<string | null>(null);
  const [configErrorBanner, setConfigErrorBanner] = useState<string | null>(null);
  const [primaryDropdownOpen, setPrimaryDropdownOpen] = useState(false);
  const primaryDropdownRef = useRef<HTMLDivElement>(null);

  const [configLoaded, setConfigLoaded] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [lastSavedAt, setLastSavedAt] = useState<Date | null>(null);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isFirstRender = useRef(true);
  const lastSnapshotRef = useRef<string>("");

  // Load authoritative config, provider capabilities, and pricing from backend
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const res = await fetch(resolveApiUrl("/api/search/config"), {
          cache: "no-store",
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (cancelled) return;

        const authoritative =
          data.search_config || data.search || data.config || {};
        if (data.effective_costs) {
          setEffectiveCosts(data.effective_costs);
        }
        if (typeof data.max_results_hard_cap === "number") {
          setHardCap(data.max_results_hard_cap);
        }

        let caps: Record<string, ProviderCapabilityMeta> =
          data.provider_capabilities || {};

        if (!caps || Object.keys(caps).length === 0) {
          try {
            const capRes = await fetch(resolveApiUrl("/api/search/providers"), {
              cache: "no-store",
            });
            if (capRes.ok) {
              const capData = await capRes.json();
              caps = capData.provider_capabilities || {};
              if (typeof capData.max_results_hard_cap === "number") {
                setHardCap(capData.max_results_hard_cap);
              }
            }
          } catch (capErr) {
            console.error("Failed to load /api/search/providers:", capErr);
          }
        }

        if (caps && Object.keys(caps).length > 0) {
          setProviderCapabilities(caps);
          setCapabilitiesError(null);
        } else {
          setCapabilitiesError(
            "Backend provider capability metadata could not be loaded. Result-count limits shown are provisional until backend connectivity is restored."
          );
        }

        if (data.config_error) {
          setConfigErrorBanner(data.config_error);
        } else {
          setConfigErrorBanner(null);
        }

        // Deep-merge: user changes (prev.search) WIN over server values.
        // Only keys missing from prev.search are adopted from the server.
        // Nested provider sub-tables are merged one level deep so a user
        // change to duckduckgo.safesearch does not wipe enabled/backend
        // /timeout that the server returned.
        setConfig((prev: any) => {
          const prevSearch: Record<string, any> = prev.search || {};
          const merged: Record<string, any> = { ...prevSearch };

          for (const [key, serverVal] of Object.entries(authoritative)) {
            const localVal = merged[key];
            if (localVal === undefined || localVal === null) {
              merged[key] = serverVal;
            } else if (
              typeof serverVal === "object" &&
              serverVal !== null &&
              !Array.isArray(serverVal) &&
              typeof localVal === "object" &&
              localVal !== null &&
              !Array.isArray(localVal)
            ) {
              merged[key] = { ...serverVal, ...localVal };
            }
          }

          return { ...prev, search: merged };
        });

        setConfigLoaded(true);
        lastSnapshotRef.current = JSON.stringify(authoritative);
      } catch (err: any) {
        console.error("Failed to load search config:", err);
        setCapabilitiesError(
          `Failed to load authoritative provider capabilities from backend (${err?.message || "network error"}). Values shown are provisional.`
        );
        setConfigLoaded(true);
      }
    })();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Close custom primary dropdown on outside click
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (
        primaryDropdownRef.current &&
        !primaryDropdownRef.current.contains(event.target as Node)
      ) {
        setPrimaryDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Resolve authoritative capability limit for a provider from backend metadata
  const getProviderLimit = useCallback(
    (cfgKey: string): number | null => {
      const cap = providerCapabilities[cfgKey];
      if (cap && typeof cap.max_results_limit === "number") {
        return cap.max_results_limit;
      }
      return null;
    },
    [providerCapabilities]
  );

  // Compute inline validation errors for global and per-provider result counts
  const validationErrors = useMemo(() => {
    const errs: Record<string, string> = {};
    const globalRaw =
      searchConfig.default_max_results !== undefined
        ? searchConfig.default_max_results
        : DEFAULT_GLOBAL_MAX_RESULTS;

    const globalErr = validatePositiveIntInRange(
      globalRaw,
      "Default Results per Search",
      1,
      hardCap
    );
    if (globalErr) {
      errs.default_max_results = globalErr;
    }

    for (const p of PROVIDERS) {
      const sub = searchConfig[p.configKey];
      if (sub && sub.max_results !== undefined && sub.max_results !== null) {
        const capLimit = getProviderLimit(p.configKey) ?? hardCap;
        const provErr = validatePositiveIntInRange(
          sub.max_results,
          `${p.name} Max Results per Query`,
          1,
          capLimit
        );
        if (provErr) {
          errs[p.configKey] = provErr;
        }
      }
    }
    return errs;
  }, [searchConfig, hardCap, getProviderLimit]);

  const hasValidationErrors = Object.keys(validationErrors).length > 0;

  // ----------------------------------------------------------
  // Persist to backend (only marks saved after backend confirms)
  // ----------------------------------------------------------
  const persistSearchConfig = useCallback(
    async (nextSearchConfig: any) => {
      if (hasValidationErrors) {
        const firstErr = Object.values(validationErrors)[0];
        setSaveErrorMsg(firstErr);
        setSaveState("error");
        return;
      }

      setSaveState("saving");
      setSaveErrorMsg(null);
      try {
        const res = await fetch(resolveApiUrl("/api/search/config"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            search_config: nextSearchConfig,
            search: nextSearchConfig,
          }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.ok === false || data.status === "error") {
          throw new Error(data.detail || data.message || `HTTP ${res.status}`);
        }
        if (data.effective_costs) {
          setEffectiveCosts(data.effective_costs);
        }
        if (data.provider_capabilities) {
          setProviderCapabilities(data.provider_capabilities);
          setCapabilitiesError(null);
        }
        if (typeof data.max_results_hard_cap === "number") {
          setHardCap(data.max_results_hard_cap);
        }
        setConfigErrorBanner(null);
        setSaveState("saved");
        setLastSavedAt(new Date());
        setTimeout(() => setSaveState((s) => (s === "saved" ? "idle" : s)), 2500);
      } catch (err: any) {
        console.error("Failed to save search config:", err);
        setSaveErrorMsg(err?.message || "Failed to save configuration.");
        setSaveState("error");
      }
    },
    [hasValidationErrors, validationErrors]
  );

  // ----------------------------------------------------------
  // Debounced auto-save on any valid search config change
  // ----------------------------------------------------------
  useEffect(() => {
    if (!configLoaded) return;
    const snapshot = JSON.stringify(searchConfig);

    if (isFirstRender.current) {
      isFirstRender.current = false;
      lastSnapshotRef.current = snapshot;
      return;
    }

    if (snapshot === lastSnapshotRef.current) return;

    lastSnapshotRef.current = snapshot;

    if (hasValidationErrors) {
      setSaveErrorMsg(Object.values(validationErrors)[0]);
      setSaveState("error");
      return;
    }

    setSaveErrorMsg(null);
    setSaveState("dirty");

    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      persistSearchConfig(searchConfig);
    }, 800);

    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, [searchConfig, configLoaded, hasValidationErrors, validationErrors, persistSearchConfig]);

  const handleManualSave = async () => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    await persistSearchConfig(searchConfig);
  };

  // ----------------------------------------------------------
  // Update helpers
  // ----------------------------------------------------------
  const updateSearch = (partial: any) => {
    setConfig({
      ...config,
      search: { ...searchConfig, ...partial },
    });
  };

  const updateEngineSubConfig = (engineKey: string, field: string, value: any) => {
    const currentSub = { ...(searchConfig[engineKey] || {}) };
    currentSub[field] = value;
    updateSearch({ [engineKey]: currentSub });
  };

  const removeEngineSubConfigKey = (engineKey: string, field: string) => {
    const currentSub = { ...(searchConfig[engineKey] || {}) };
    delete currentSub[field];
    updateSearch({ [engineKey]: currentSub });
  };

  const toggleShowKey = (id: string) => {
    setShowKeys((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const toggleFallback = (eng: string) => {
    const list: string[] = Array.isArray(searchConfig.fallback_engines)
      ? [...searchConfig.fallback_engines]
      : [];
    const index = list.indexOf(eng);
    if (index > -1) list.splice(index, 1);
    else list.push(eng);
    updateSearch({ fallback_engines: list });
  };

  const isEngineEnabled = (cfgKey: string): boolean => {
    const sub = searchConfig[cfgKey];
    if (!sub) return cfgKey === "duckduckgo";
    return sub.enabled !== false;
  };

  const globalDefaultMaxResults: number = useMemo(() => {
    const raw = searchConfig.default_max_results;
    if (typeof raw === "number" && Number.isInteger(raw) && raw >= 1 && raw <= hardCap) {
      return raw;
    }
    return DEFAULT_GLOBAL_MAX_RESULTS;
  }, [searchConfig.default_max_results, hardCap]);

  const handleTestConnection = async (engineId: string) => {
    if (pings[engineId]?.testing) return;
    setPings((prev) => ({ ...prev, [engineId]: { testing: true } }));
    try {
      let res = await fetch(resolveApiUrl("/api/search/test-connection"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: engineId,
          config: searchConfig,
          search_config: searchConfig,
          check_type: "credentials",
        }),
      });

      if (res.status === 404) {
        res = await fetch(resolveApiUrl("/api/search/test"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            provider: engineId,
            config: searchConfig,
            search_config: searchConfig,
          }),
        });
      }

      const data = await res.json();
      setPings((prev) => ({
        ...prev,
        [engineId]: {
          testing: false,
          ok: Boolean(data.ok ?? data.status === "ok"),
          status: data.status,
          reachable: data.reachable,
          authenticated: data.authenticated,
          quota_available: data.quota_available,
          consumes_quota: data.consumes_quota,
          latency_ms: data.latency_ms,
          estimated_cost_usd: data.estimated_cost_usd ?? data.cost_usd,
          cost_basis: data.cost_basis,
          requested_max_results: data.requested_max_results,
          effective_max_results: data.effective_max_results,
          max_results_limit: data.max_results_limit,
          msg: data.message || data.error,
        },
      }));
    } catch (err: any) {
      setPings((prev) => ({
        ...prev,
        [engineId]: { testing: false, ok: false, msg: err.message },
      }));
    }
  };

  const enabledCount = useMemo(
    () => PROVIDERS.filter((p) => isEngineEnabled(p.configKey)).length,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [searchConfig]
  );

  const isWebEgressEnabled = searchConfig.enabled !== false;
  const isPrivacyMode = Boolean(searchConfig.privacy_mode);
  const primaryEngine = searchConfig.engine || "DuckDuckGo";
  const fallbackList: string[] = Array.isArray(searchConfig.fallback_engines)
    ? searchConfig.fallback_engines
    : [];

  // ----------------------------------------------------------
  // Image Search Configuration (independent provider chain)
  // ----------------------------------------------------------
  const imageSearchConfig: Record<string, any> = useMemo(() => {
    return searchConfig.image_search && typeof searchConfig.image_search === "object"
      ? searchConfig.image_search
      : {};
  }, [searchConfig.image_search]);

  const isImageSearchEnabled = imageSearchConfig.enabled !== false;

  const imageGlobalDefaultMaxResults: number = useMemo(() => {
    const raw = imageSearchConfig.default_max_results;
    if (
      typeof raw === "number" &&
      Number.isInteger(raw) &&
      raw >= 1 &&
      raw <= hardCap
    ) {
      return raw;
    }
    return globalDefaultMaxResults > 0
      ? globalDefaultMaxResults
      : DEFAULT_IMAGE_MAX_RESULTS;
  }, [imageSearchConfig.default_max_results, globalDefaultMaxResults, hardCap]);

  const imageMaxResultsCap: number = useMemo(() => {
    const raw = imageSearchConfig.max_results_cap;
    if (
      typeof raw === "number" &&
      Number.isInteger(raw) &&
      raw >= 1 &&
      raw <= hardCap
    ) {
      return raw;
    }
    return hardCap;
  }, [imageSearchConfig.max_results_cap, hardCap]);

  const imagePrimaryEngine: string = useMemo(() => {
    const raw = imageSearchConfig.engine;
    if (typeof raw === "string" && raw.trim()) {
      return raw.trim();
    }
    return ""; // empty = inherit from text chain
  }, [imageSearchConfig.engine]);

  const imageFallbackList: string[] = useMemo(() => {
    const raw = imageSearchConfig.fallback_engines;
    if (Array.isArray(raw)) {
      return raw.filter((x): x is string => typeof x === "string");
    }
    return [];
  }, [imageSearchConfig.fallback_engines]);

  const providerSupportsImages = useCallback(
    (providerId: string): boolean => {
      const cap = providerCapabilities[providerId.toLowerCase()];
      if (cap && typeof cap.supports_image_search === "boolean") {
        return cap.supports_image_search;
      }
      return DEFAULT_IMAGE_CAPABLE_PROVIDERS.has(providerId);
    },
    [providerCapabilities]
  );

  const imageCapableProviderDefs = useMemo(
    () => PROVIDERS.filter((p) => providerSupportsImages(p.id)),
    [providerSupportsImages]
  );

  const effectiveImagePrimaryEngine = imagePrimaryEngine || primaryEngine;
  const imagePrimarySupportsImages = providerSupportsImages(
    effectiveImagePrimaryEngine
  );

  const updateImageSearch = (partial: Record<string, any>) => {
    updateSearch({
      image_search: { ...imageSearchConfig, ...partial },
    });
  };

  const toggleImageFallback = (eng: string) => {
    if (!providerSupportsImages(eng)) return;
    const list = [...imageFallbackList];
    const idx = list.indexOf(eng);
    if (idx > -1) list.splice(idx, 1);
    else list.push(eng);
    updateImageSearch({ fallback_engines: list });
  };

  // ----------------------------------------------------------
  // Render
  // ----------------------------------------------------------
  return (
    <div className="h-full w-full overflow-y-auto bg-background text-foreground font-sans flex flex-col relative">
      {/* Ambient background */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-40 -left-40 w-[500px] h-[500px] rounded-full bg-primary/5 blur-3xl" />
        <div className="absolute top-1/3 -right-40 w-[400px] h-[400px] rounded-full bg-emerald-500/5 blur-3xl" />
        <div className="absolute bottom-0 left-1/3 w-[450px] h-[450px] rounded-full bg-amber-500/5 blur-3xl" />
      </div>

      {/* Header */}
      <motion.header
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="border-b border-border bg-card/60 backdrop-blur-xl px-6 py-4 sticky top-0 z-20"
      >
        <div className="max-w-7xl mx-auto flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-2.5">
            <div className="p-2 icon">
              <Search size={16} className="text-primary" />
            </div>
            <div>
              <h1 className="text-sm font-semibold font-heading tracking-wide">
                Search Engine Orchestration
              </h1>
              <p className="text-[10px] text-muted-foreground">
                First-success failover · per-provider result ceilings · contract diagnostics
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <SaveIndicator
              state={saveState}
              lastSavedAt={lastSavedAt}
              errorMsg={saveErrorMsg}
            />
            <Button
              variant="save"
              onClick={handleManualSave}
              disabled={saveState === "saving" || hasValidationErrors}
              className="save h-8 text-xs gap-1.5 cursor-pointer hover:scale-105 transition-transform disabled:opacity-50"
            >
              {saveState === "saving" ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Save size={12} />
              )}
              <span>{saveState === "saving" ? "Saving..." : "Save"}</span>
            </Button>

            <Link href="/settings/search">
              <Button
                variant="outline"
                className="h-8 text-xs gap-1.5 bg-primary/10 hover:bg-primary/20 text-primary cursor-pointer hover:scale-105 transition-transform"
              >
                <Activity size={12} />
                <span className="hidden sm:inline">Telemetry & Live Test</span>
                <ExternalLink size={11} />
              </Button>
            </Link>
          </div>
        </div>
      </motion.header>

      <main className="max-w-7xl w-full mx-auto p-6 space-y-6 flex-1 relative">
        {/* Config parse error banner if config.toml is malformed */}
        {configErrorBanner && (
          <div className="flex items-start gap-3 p-4 rounded-md border border-rose-500/40 bg-rose-500/10 text-rose-600 dark:text-rose-400 text-xs">
            <XCircle size={16} className="shrink-0 mt-0.5" />
            <div className="space-y-1">
              <div className="font-semibold">
                Configuration Syntax Error in config.toml
              </div>
              <div className="font-mono text-[11px] opacity-90">
                {configErrorBanner}
              </div>
            </div>
          </div>
        )}

        {/* Capability loading failure notice (SR-504 / Section 6.6) */}
        {capabilitiesError && (
          <div
            role="alert"
            className="flex items-start gap-3 p-3.5 rounded-md border border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300 text-xs"
          >
            <AlertCircle size={15} className="shrink-0 mt-0.5" />
            <div>{capabilitiesError}</div>
          </div>
        )}

        {/* Validation error banner on save */}
        {saveErrorMsg && (
          <div
            role="alert"
            className="flex items-start gap-3 p-3.5 rounded-md border border-rose-500/40 bg-rose-500/10 text-rose-600 dark:text-rose-400 text-xs"
          >
            <AlertCircle size={15} className="shrink-0 mt-0.5" />
            <div>{saveErrorMsg}</div>
          </div>
        )}

        {/* Summary KPIs */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <SummaryCard
            label="Web Egress"
            value={isWebEgressEnabled ? "Enabled" : "Air-Gapped"}
            icon={
              isWebEgressEnabled ? (
                <Shield size={18} className="text-emerald-500" />
              ) : (
                <ShieldOff size={18} className="text-rose-500" />
              )
            }
            accent={isWebEgressEnabled ? "#10b981" : "#f43f5e"}
            subtitle={
              isWebEgressEnabled
                ? "Outbound requests permitted"
                : "All network calls blocked"
            }
            delay={0}
          />
          <SummaryCard
            label="Default Results / Search"
            value={`${globalDefaultMaxResults} / query`}
            icon={<Hash size={18} className="text-primary" />}
            accent="#3b82f6"
            subtitle={`Global default & ceiling (range 1..${hardCap})`}
            delay={0.05}
          />
          <SummaryCard
            label="Primary Engine"
            value={primaryEngine}
            icon={
              <Icon
                name={providerIconName(primaryEngine)}
                size={18}
                title={`Primary engine: ${primaryEngine}`}
              />
            }
            accent={providerAccent(primaryEngine)}
            subtitle={`${enabledCount} of ${PROVIDERS.length} providers active`}
            delay={0.1}
          />
          <SummaryCard
            label="Failover Chain"
            value={
              fallbackList.length === 0
                ? "Single-Engine"
                : `${fallbackList.length} Fallback(s)`
            }
            icon={<Gauge size={18} className="text-cyan-500" />}
            accent="#06b6d4"
            subtitle={
              fallbackList.length === 0
                ? "Strict primary-only mode (fallback_engines=[])"
                : fallbackList.join(" → ")
            }
            delay={0.15}
          />
        </div>

        {/* Single-provider mode informational notice */}
        {fallbackList.length === 0 && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            className="overflow-hidden"
          >
            <div className="flex items-start gap-3 p-3.5 rounded-md border border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300 text-xs">
              <Info size={16} className="shrink-0 mt-0.5" />
              <div className="space-y-0.5">
                <div className="font-semibold">
                  Single-Provider Isolation Mode Active (fallback_engines = [])
                </div>
                <div className="opacity-90 text-[11px]">
                  Only <strong>{primaryEngine}</strong> will be called. No secondary
                  provider will be tried automatically on failure, ensuring
                  deterministic single-provider testing and predictable cost.
                </div>
              </div>
            </div>
          </motion.div>
        )}

        {/* Global Orchestration */}
        <motion.div
          {...FADE_UP}
          transition={{ ...FADE_UP.transition, delay: 0.2 }}
          className="relative p-5 rounded-md bg-gigbg shadow-sm space-y-5"
        >
          <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md from-transparent via-primary/60 to-transparent shadow-sm" />

          <div className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex items-center gap-3">
              <div className="p-2 icon">
                <Sliders size={16} className="text-primary" />
              </div>
              <div>
                <h3 className="text-md text-black font-semibold font-heading uppercase tracking-wider">
                  Global Orchestration
                </h3>
                <p className="text-[11px] text-muted-foreground">
                  Primary engine, failover sequence, default result count, deadline
                  budget, and privacy
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              <button
                type="button"
                onClick={() => updateSearch({ privacy_mode: !isPrivacyMode })}
                title="When enabled, the local SQLite usage ledger stores only the SHA-256 query hash instead of plain query text."
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-sm text-[11px] font-semibold transition-all cursor-pointer border ${
                  isPrivacyMode
                    ? "bg-purple-500/10 border-purple-500/40 text-purple-600 dark:text-purple-400"
                    : "bg-background border-border text-muted-foreground hover:text-foreground"
                }`}
              >
                <Lock size={12} />
                <span>
                  {isPrivacyMode
                    ? "Ledger Privacy: Hash-Only"
                    : "Ledger Privacy: Standard"}
                </span>
              </button>

              <button
                type="button"
                onClick={() =>
                  updateSearch({
                    enabled: searchConfig.enabled === false ? true : false,
                  })
                }
                className={`flex items-center gap-2 px-3 py-1.5 rounded-sm text-[11px] font-semibold transition-all cursor-pointer border ${
                  isWebEgressEnabled
                    ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-600 dark:text-emerald-400"
                    : "bg-rose-500/10 border-rose-500/40 text-rose-600 dark:text-rose-400"
                }`}
              >
                <span className="relative flex h-2 w-2">
                  {isWebEgressEnabled && (
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                  )}
                  <span
                    className={`relative inline-flex rounded-full h-2 w-2 ${
                      isWebEgressEnabled ? "bg-emerald-500" : "bg-rose-500"
                    }`}
                  />
                </span>
                {isWebEgressEnabled ? "Enabled" : "Air-Gapped"}
              </button>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            {/* Primary engine: custom dropdown with icons */}
            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-2">
                Primary Search Engine
              </label>
              <div className="relative" ref={primaryDropdownRef}>
                <button
                  type="button"
                  onClick={() => setPrimaryDropdownOpen((o) => !o)}
                  className="w-full bg-background border border-border rounded-sm px-3 py-2 text-xs text-foreground font-medium focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow cursor-pointer flex items-center justify-between gap-2"
                >
                  <span className="flex items-center gap-2 min-w-0">
                    <Icon
                      name={providerIconName(primaryEngine)}
                      size={16}
                      title={primaryEngine}
                    />
                    <span className="truncate">{primaryEngine}</span>
                  </span>
                  <motion.span
                    animate={{ rotate: primaryDropdownOpen ? 180 : 0 }}
                    transition={{ duration: 0.2 }}
                    className="shrink-0"
                  >
                    <ChevronDown size={14} className="text-muted-foreground" />
                  </motion.span>
                </button>

                <AnimatePresence>
                  {primaryDropdownOpen && (
                    <motion.ul
                      initial={{ opacity: 0, y: -4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -4 }}
                      transition={{ duration: 0.15 }}
                      className="absolute z-50 mt-1 w-full bg-card border border-border rounded-sm shadow-lg overflow-hidden"
                      role="listbox"
                    >
                      {PROVIDERS.map((p) => {
                        const selected = primaryEngine === p.id;
                        const capLim = getProviderLimit(p.configKey);
                        return (
                          <li key={p.id}>
                            <button
                              type="button"
                              role="option"
                              aria-selected={selected}
                              onClick={() => {
                                updateSearch({ engine: p.id });
                                setPrimaryDropdownOpen(false);
                              }}
                              className={`w-full flex items-center gap-2.5 px-3 py-2 text-xs cursor-pointer transition-colors text-left ${
                                selected
                                  ? "bg-primary/10 text-foreground"
                                  : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                              }`}
                            >
                              <Icon
                                name={p.iconName}
                                size={16}
                                title={`${p.name} logo`}
                              />
                              <span className="flex-1 truncate font-medium">
                                {p.name}
                              </span>
                              {capLim !== null && (
                                <span className="text-[9px] font-mono text-muted-foreground">
                                  cap {capLim}
                                </span>
                              )}
                              {p.lifecycle === "legacy" && (
                                <span className="text-[9px] font-mono px-1 py-0.5 rounded border border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400">
                                  legacy
                                </span>
                              )}
                              {selected && (
                                <CheckCircle2
                                  size={12}
                                  className="text-primary shrink-0"
                                />
                              )}
                            </button>
                          </li>
                        );
                      })}
                    </motion.ul>
                  )}
                </AnimatePresence>
              </div>
            </div>

            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-2">
                Failover Chain (First-Success · Leave unselected for Single-Engine
                Mode)
              </label>
              <div className="flex flex-wrap gap-1.5">
                {ENGINE_OPTIONS.map((eng) => {
                  const isSelected = fallbackList.includes(eng);
                  return (
                    <motion.button
                      key={eng}
                      type="button"
                      whileHover={{ y: -2 }}
                      onClick={() => toggleFallback(eng)}
                      className={`flex items-center gap-1.5 px-2.5 py-1 rounded-sm text-[11px] font-medium cursor-pointer transition-all border ${
                        isSelected
                          ? "bg-primary text-primary-foreground border-primary shadow-md"
                          : "bg-background text-muted-foreground border-border hover:border-primary/40 hover:text-foreground"
                      }`}
                    >
                      <Icon
                        name={providerIconName(eng)}
                        size={13}
                        title={`${eng} logo`}
                      />
                      <span>{eng}</span>
                    </motion.button>
                  );
                })}
              </div>
            </div>
          </div>

          {/* Global Numeric & ISO Controls including Default Results per Search (SR-501) */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-3 pt-4 border-t border-border/60">
            <div
              className={`p-3 rounded-sm border transition-colors ${
                validationErrors.default_max_results
                  ? "border-rose-500/60 bg-rose-500/5"
                  : "border-border/60 bg-card hover:border-muted/30 hover:shadow-sm"
              }`}
            >
              <label
                htmlFor="global-default-max-results"
                className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5"
              >
                Default Results per Search (1-{hardCap})
              </label>
              <input
                id="global-default-max-results"
                type="number"
                min={1}
                max={hardCap}
                step={1}
                value={
                  searchConfig.default_max_results !== undefined
                    ? searchConfig.default_max_results
                    : DEFAULT_GLOBAL_MAX_RESULTS
                }
                onChange={(e) => {
                  const rawVal = e.target.value;
                  if (rawVal === "") {
                    updateSearch({ default_max_results: "" });
                    return;
                  }
                  const parsed = Number(rawVal);
                  updateSearch({
                    default_max_results: Number.isNaN(parsed) ? rawVal : parsed,
                  });
                }}
                aria-invalid={Boolean(validationErrors.default_max_results)}
                aria-describedby="global-default-max-results-help"
                className="w-full border-0 outline-none text-xs text-foreground font-mono focus:ring-0 bg-transparent hover:bg-muted p-1"
              />
            </div>

            <CompactField
              label="Total Budget (s)"
              value={searchConfig.total_budget_seconds ?? 22}
              min={2}
              max={120}
              onChange={(v) => updateSearch({ total_budget_seconds: v })}
            />
            <CompactField
              label="Transient Retries (0-2)"
              value={searchConfig.max_retries ?? 0}
              min={0}
              max={2}
              onChange={(v) => updateSearch({ max_retries: v })}
            />
            <CompactField
              label="Retry Delay (s)"
              value={searchConfig.retry_delay ?? 2}
              min={0}
              max={10}
              onChange={(v) => updateSearch({ retry_delay: v })}
            />
            <CompactTextField
              label="Language (ISO)"
              value={searchConfig.lang || "en"}
              onChange={(v) => updateSearch({ lang: v })}
              placeholder="en / ar / ru"
            />
            <CompactTextField
              label="Country (ISO)"
              value={searchConfig.country || "us"}
              onChange={(v) => updateSearch({ country: v })}
              placeholder="us / eg / ru"
            />
          </div>

          {/* Helper text and inline validation for Default Results per Search (Section 4.3 / SR-508) */}
          <div className="space-y-1">
            <p
              id="global-default-max-results-help"
              className="text-[11px] text-muted-foreground"
            >
              <strong>Default Results per Search:</strong> Used as the default and
              maximum whenever no provider override is saved. Explicit tool calls may
              request fewer results, never more. The provider-supported limit still
              applies, and actual results returned by an upstream index can be fewer.
            </p>
            {validationErrors.default_max_results && (
              <p
                role="alert"
                className="text-[11px] font-medium text-rose-600 dark:text-rose-400 flex items-center gap-1.5"
              >
                <AlertCircle size={12} />
                <span>{validationErrors.default_max_results}</span>
              </p>
            )}
          </div>
        </motion.div>

        {/* Image Search Configuration */}
        <motion.div
          {...FADE_UP}
          transition={{ ...FADE_UP.transition, delay: 0.25 }}
          className="relative p-5 rounded-md bg-gigbg shadow-sm space-y-5"
        >
          <div className="pointer-events-none absolute top-0 left-0 right-0 h-[2px] rounded-t-md from-transparent via-emerald-500/60 to-transparent shadow-sm" />

          <div className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex items-center gap-3">
              <div className="p-2 icon">
                <ImageIcon size={16} className="text-emerald-500" />
              </div>
              <div>
                <h3 className="text-md text-black font-semibold font-heading uppercase tracking-wider">
                  Image Search Configuration
                </h3>
                <p className="text-[11px] text-muted-foreground">
                  Independent provider chain for image_search &middot; only
                  image-capable providers are used
                </p>
              </div>
            </div>

            <button
              type="button"
              onClick={() =>
                updateImageSearch({ enabled: !isImageSearchEnabled })
              }
              className={`flex items-center gap-2 px-3 py-1.5 rounded-sm text-[11px] font-semibold transition-all cursor-pointer border ${
                isImageSearchEnabled
                  ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-600 dark:text-emerald-400"
                  : "bg-rose-500/10 border-rose-500/40 text-rose-600 dark:text-rose-400"
              }`}
            >
              <span className="relative flex h-2 w-2">
                {isImageSearchEnabled && (
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                )}
                <span
                  className={`relative inline-flex rounded-full h-2 w-2 ${
                    isImageSearchEnabled ? "bg-emerald-500" : "bg-rose-500"
                  }`}
                />
              </span>
              {isImageSearchEnabled ? "Enabled" : "Disabled"}
            </button>
          </div>

          <div className="text-[11px] text-muted-foreground">
            <strong className="text-foreground">Image-capable providers:</strong>{" "}
            {imageCapableProviderDefs.length > 0 ? (
              <span className="font-mono">
                {imageCapableProviderDefs.map((p) => p.name).join(" · ")}
              </span>
            ) : (
              <span className="text-rose-500 font-semibold">
                None configured — image_search will fail until at least one
                image-capable provider is enabled
              </span>
            )}
          </div>

          {!imagePrimarySupportsImages && (
            <div
              role="alert"
              className="flex items-start gap-2 p-2.5 rounded-sm border border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300 text-[11px]"
            >
              <AlertCircle size={14} className="shrink-0 mt-0.5" />
              <span>
                Primary text engine{" "}
                <strong>{effectiveImagePrimaryEngine}</strong> does not support
                images. The factory will automatically skip it and use the
                first image-capable provider in the chain.
              </span>
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            <div className="p-3 rounded-sm border border-border/60 bg-card hover:border-muted/30 hover:shadow-sm transition-colors">
              <label
                htmlFor="image-default-max-results"
                className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5"
              >
                Default Images per Search (1-{hardCap})
              </label>
              <input
                id="image-default-max-results"
                type="number"
                min={1}
                max={hardCap}
                step={1}
                value={imageGlobalDefaultMaxResults}
                onChange={(e) => {
                  const rawVal = e.target.value;
                  if (rawVal === "") {
                    updateImageSearch({ default_max_results: "" });
                    return;
                  }
                  const parsed = Number(rawVal);
                  updateImageSearch({
                    default_max_results: Number.isNaN(parsed) ? rawVal : parsed,
                  });
                }}
                className="w-full border-0 outline-none text-xs text-foreground font-mono focus:ring-0 bg-transparent hover:bg-muted p-1"
              />
            </div>

            <div className="p-3 rounded-sm border border-border/60 bg-card hover:border-muted/30 hover:shadow-sm transition-colors">
              <label
                htmlFor="image-max-results-cap"
                className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5"
              >
                Max Results Cap (1-{hardCap})
              </label>
              <input
                id="image-max-results-cap"
                type="number"
                min={1}
                max={hardCap}
                step={1}
                value={imageMaxResultsCap}
                onChange={(e) => {
                  const rawVal = e.target.value;
                  if (rawVal === "") {
                    updateImageSearch({ max_results_cap: "" });
                    return;
                  }
                  const parsed = Number(rawVal);
                  updateImageSearch({
                    max_results_cap: Number.isNaN(parsed) ? rawVal : parsed,
                  });
                }}
                className="w-full border-0 outline-none text-xs text-foreground font-mono focus:ring-0 bg-transparent hover:bg-muted p-1"
              />
            </div>

            <div className="p-3 rounded-sm border border-border/60 bg-card hover:border-muted/30 hover:shadow-sm transition-colors">
              <label
                htmlFor="image-primary-engine"
                className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5"
              >
                Primary Image Engine (optional override)
              </label>
              <select
                id="image-primary-engine"
                value={imagePrimaryEngine}
                onChange={(e) => {
                  const val = e.target.value;
                  if (val) {
                    updateImageSearch({ engine: val });
                  } else {
                    const next = { ...imageSearchConfig };
                    delete next.engine;
                    updateSearch({ image_search: next });
                  }
                }}
                className="w-full bg-transparent border-0 outline-none text-xs text-foreground font-mono focus:ring-0 p-1 hover:bg-muted cursor-pointer"
              >
                <option value="">
                  Inherit from text chain (auto-filter)
                </option>
                {imageCapableProviderDefs.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-2">
              Image Failover Chain (image-capable only)
            </label>
            {imageCapableProviderDefs.length === 0 ? (
              <p className="text-[11px] text-rose-500">
                No image-capable providers detected. Enable at least one of:
                DuckDuckGo, SearXNG, Brave, Google.
              </p>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {imageCapableProviderDefs.map((p) => {
                  const isSelected = imageFallbackList.includes(p.id);
                  return (
                    <motion.button
                      key={p.id}
                      type="button"
                      whileHover={{ y: -2 }}
                      onClick={() => toggleImageFallback(p.id)}
                      className={`flex items-center gap-1.5 px-2.5 py-1 rounded-sm text-[11px] font-medium cursor-pointer transition-all border ${
                        isSelected
                          ? "bg-emerald-500 text-white border-emerald-500 shadow-md"
                          : "bg-background text-muted-foreground border-border hover:border-emerald-500/40 hover:text-foreground"
                      }`}
                    >
                      <Icon
                        name={providerIconName(p.id)}
                        size={13}
                        title={`${p.name} logo`}
                      />
                      <span>{p.name}</span>
                    </motion.button>
                  );
                })}
              </div>
            )}
            {imagePrimaryEngine && imageFallbackList.length === 0 && (
              <p className="text-[10px] text-amber-600 dark:text-amber-400 mt-2">
                When overriding the primary image engine, the text-level
                fallback_engines list is ignored. Add image fallbacks above to
                enable failover.
              </p>
            )}
          </div>

          <details className="pt-2 border-t border-border/60">
            <summary className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground cursor-pointer select-none">
              Advanced (budget, retries, delay)
            </summary>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-3">
              <CompactField
                label="Image Budget (s)"
                value={imageSearchConfig.total_budget_seconds ?? 20}
                min={2}
                max={120}
                onChange={(v) =>
                  updateImageSearch({ total_budget_seconds: v })
                }
              />
              <CompactField
                label="Image Retries (0-2)"
                value={imageSearchConfig.max_retries ?? 0}
                min={0}
                max={2}
                onChange={(v) => updateImageSearch({ max_retries: v })}
              />
              <CompactField
                label="Image Retry Delay (s)"
                value={imageSearchConfig.retry_delay ?? 1}
                min={0}
                max={10}
                onChange={(v) => updateImageSearch({ retry_delay: v })}
              />
            </div>
          </details>
        </motion.div>

        {/* Provider section header */}
        <motion.div
          {...FADE_UP}
          transition={{ ...FADE_UP.transition, delay: 0.3 }}
          className="flex items-center justify-between flex-wrap gap-3 pt-2"
        >
          <div className="flex items-center gap-3">
            <div className="p-2 icon">
              <Network size={16} className="text-emerald-500" />
            </div>
            <div>
              <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">
                Provider Configuration & Result Limits
              </h3>
              <p className="text-[11px] text-muted-foreground">
                {enabledCount} of {PROVIDERS.length} enabled · provider-supported
                maximums are supplied by the backend capability registry
              </p>
            </div>
          </div>
        </motion.div>

        {/* Provider list */}
        <div className="rounded-md border border-border bg-card shadow-sm overflow-hidden divide-y divide-border/40">
          {PROVIDERS.map((provider, idx) => {
            const sub = searchConfig[provider.configKey] || {};
            const effectiveCost =
              typeof sub.cost_per_query === "number"
                ? sub.cost_per_query
                : effectiveCosts[provider.id] ?? provider.defaultCost;

            const capMeta = providerCapabilities[provider.configKey];
            const providerLimit = getProviderLimit(provider.configKey);

            return (
              <ProviderListRow
                key={provider.id}
                provider={provider}
                capability={capMeta}
                providerLimit={providerLimit}
                hardCap={hardCap}
                globalDefaultMaxResults={globalDefaultMaxResults}
                validationError={validationErrors[provider.configKey] || null}
                enabled={isEngineEnabled(provider.configKey)}
                expanded={expandedProvider === provider.id}
                onToggleExpand={() =>
                  setExpandedProvider(
                    expandedProvider === provider.id ? null : provider.id
                  )
                }
                config={searchConfig}
                effectiveCost={effectiveCost}
                ping={pings[provider.id]}
                showKeys={showKeys}
                onToggleEnabled={() =>
                  updateEngineSubConfig(
                    provider.configKey,
                    "enabled",
                    !isEngineEnabled(provider.configKey)
                  )
                }
                onUpdateField={(field, value) =>
                  updateEngineSubConfig(provider.configKey, field, value)
                }
                onRemoveField={(field) =>
                  removeEngineSubConfigKey(provider.configKey, field)
                }
                onToggleShowKey={toggleShowKey}
                onTest={() => handleTestConnection(provider.id)}
                delay={0.35 + idx * 0.04}
              />
            );
          })}
        </div>
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Save Indicator
// ---------------------------------------------------------------------------
function SaveIndicator({
  state,
  lastSavedAt,
  errorMsg,
}: {
  state: SaveState;
  lastSavedAt: Date | null;
  errorMsg?: string | null;
}) {
  const map: Record<
    SaveState,
    { label: string; cls: string; icon: React.ReactNode }
  > = {
    idle: {
      label: lastSavedAt
        ? `Saved ${lastSavedAt.toLocaleTimeString()}`
        : "Auto-save on",
      cls: "text-muted-foreground bg-custom",
      icon: <CheckCircle2 size={11} />,
    },
    dirty: {
      label: "Unsaved changes",
      cls: "text-amber-600 dark:text-amber-400 border-amber-500/40 bg-amber-500/10",
      icon: <AlertCircle size={11} />,
    },
    saving: {
      label: "Saving...",
      cls: "text-primary border-primary/40 bg-primary/10",
      icon: <Loader2 size={11} className="animate-spin" />,
    },
    saved: {
      label: "Confirmed saved",
      cls: "text-emerald-600 dark:text-emerald-400 border-emerald-500/40 bg-emerald-500/10",
      icon: <CheckCircle2 size={11} />,
    },
    error: {
      label: errorMsg ? "Validation / save error" : "Save failed — click Save",
      cls: "text-rose-600 dark:text-rose-400 border-rose-500/40 bg-rose-500/10",
      icon: <XCircle size={11} />,
    },
  };
  const cfg = map[state];
  return (
    <motion.span
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      className={`hidden sm:flex items-center gap-1.5 text-[10px] font-mono px-2 py-1 rounded-md ${cfg.cls}`}
    >
      {cfg.icon}
      <span>{cfg.label}</span>
    </motion.span>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------
function SummaryCard({ label, value, icon, accent, subtitle, delay = 0 }: any) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, delay, ease: [0.16, 1, 0.3, 1] }}
      whileHover={{ y: -4, transition: { duration: 0.2 } }}
      className="group relative p-5 rounded-md border border-card bg-card overflow-hidden shadow-sm hover:shadow-lg hover:border-primary/30 transition-all"
    >
      <div
        className="absolute top-0 left-0 right-0 h-[1px] opacity-30 group-hover:opacity-100 transition-opacity"
        style={{
          background: `linear-gradient(90deg, transparent 0%, ${accent} 50%, transparent 100%)`,
        }}
      />
      <div
        className="absolute -top-10 -right-10 w-32 h-32 rounded-full opacity-[0.08] group-hover:opacity-[0.15] transition-opacity blur-2xl"
        style={{ backgroundColor: accent }}
      />
      <div className="relative flex items-start justify-between">
        <div className="flex-1 space-y-3 min-w-0">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block">
            {label}
          </span>
          <div
            className="text-xl lg:text-2xl font-bold font-mono tabular-nums truncate"
            style={{ color: accent }}
          >
            {value}
          </div>
          <div
            className="text-[11px] text-muted-foreground truncate"
            title={subtitle}
          >
            {subtitle}
          </div>
        </div>
        <div className="p-2.5 rounded-sm shrink-0 ml-2" style={{ color: accent }}>
          {icon}
        </div>
      </div>
    </motion.div>
  );
}

function CompactField({
  label,
  value,
  min = 0,
  max = 60,
  onChange,
}: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="p-3 rounded-sm border border-border/60 bg-card hover:border-muted/30 hover:shadow-sm transition-colors flex flex-col justify-between">
      <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5">
        {label}
      </label>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => {
          const parsed = parseFloat(e.target.value);
          onChange(
            Number.isNaN(parsed) ? min : Math.max(min, Math.min(max, parsed))
          );
        }}
        className="w-full bg-transparent border-0 outline-none text-xs text-foreground font-mono focus:ring-0 p-1 hover:bg-muted"
      />
    </div>
  );
}

function CompactTextField({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="p-3 rounded-sm border border-border/60 bg-card hover:border-muted/30 hover:shadow-sm transition-colors flex flex-col justify-between">
      <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5">
        {label}
      </label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-transparent border-0 outline-none text-xs text-foreground font-mono focus:ring-0 p-1 hover:bg-muted"
      />
    </div>
  );
}

interface ProviderListRowProps {
  provider: ProviderDef;
  capability?: ProviderCapabilityMeta;
  providerLimit: number | null;
  hardCap: number;
  globalDefaultMaxResults: number;
  validationError: string | null;
  enabled: boolean;
  expanded: boolean;
  onToggleExpand: () => void;
  config: any;
  effectiveCost: number;
  ping?: DiagnosticState;
  showKeys: Record<string, boolean>;
  onToggleEnabled: () => void;
  onUpdateField: (field: string, value: any) => void;
  onRemoveField: (field: string) => void;
  onToggleShowKey: (id: string) => void;
  onTest: () => void;
  delay: number;
}

function ProviderListRow({
  provider,
  capability,
  providerLimit,
  hardCap,
  globalDefaultMaxResults,
  validationError,
  enabled,
  expanded,
  onToggleExpand,
  config,
  effectiveCost,
  ping,
  showKeys,
  onToggleEnabled,
  onUpdateField,
  onRemoveField,
  onToggleShowKey,
  onTest,
  delay,
}: ProviderListRowProps) {
  const subConfig = config[provider.configKey] || {};
  const effectiveCapLimit = providerLimit ?? hardCap;
  const supportsImages =
    capability?.supports_image_search ??
    DEFAULT_IMAGE_CAPABLE_PROVIDERS.has(provider.id);

  const hasExplicitOverride =
    subConfig.max_results !== undefined && subConfig.max_results !== null;

  const configuredCount = hasExplicitOverride
    ? Number(subConfig.max_results)
    : null;

  const effectiveResultCount = useMemo(() => {
    const baseCount =
      configuredCount !== null && !Number.isNaN(configuredCount)
        ? configuredCount
        : globalDefaultMaxResults;
    return Math.max(1, Math.min(baseCount, effectiveCapLimit, hardCap));
  }, [configuredCount, globalDefaultMaxResults, effectiveCapLimit, hardCap]);

  const handleToggleInheritGlobal = (useGlobal: boolean) => {
    if (useGlobal) {
      // SR-506: Remove max_results key from provider settings to preserve inheritance
      onRemoveField("max_results");
    } else {
      const initialOverride = Math.max(
        1,
        Math.min(globalDefaultMaxResults, effectiveCapLimit)
      );
      onUpdateField("max_results", initialOverride);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay, ease: [0.16, 1, 0.3, 1] }}
      className={`group relative transition-colors ${enabled ? "" : "opacity-70"}`}
    >
      {enabled && (
        <div
          className="absolute left-0 top-0 bottom-0 w-[3px]"
          style={{ backgroundColor: provider.accent }}
        />
      )}

      <button
        type="button"
        onClick={onToggleExpand}
        className={`w-full flex items-center gap-0 p-4 text-left cursor-pointer transition-colors border-l-0 ${
          expanded
            ? "bg-primary/10 hover:bg-primary/15"
            : "bg-card hover:bg-muted/70"
        }`}
      >
        <div
          className="shrink-0 w-11 h-11 flex items-center justify-center transition-transform"
          style={{ color: provider.accent }}
        >
          <Icon
            name={provider.iconName}
            size={22}
            title={`${provider.name} logo`}
          />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-foreground">
              {provider.name}
            </span>
            {provider.lifecycle === "legacy" && (
              <span className="text-[9px] font-mono font-semibold px-1.5 py-0.5 rounded border border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400">
                Legacy · Retires 2027-01-01
              </span>
            )}
            {supportsImages ? (
              <span
                className="text-[9px] font-mono font-semibold px-1.5 py-0.5 rounded border border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                title="This provider can be used by the image_search tool"
              >
                Images ✓
              </span>
            ) : (
              <span
                className="text-[9px] font-mono px-1.5 py-0.5 rounded border border-border bg-muted/40 text-muted-foreground"
                title="This provider is text-search only and is automatically skipped by the image_search chain"
              >
                Text only
              </span>
            )}
            <span
              className={`text-[9px] font-mono px-1.5 py-0.5 rounded border ${
                hasExplicitOverride
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border bg-muted/60 text-muted-foreground"
              }`}
              title={
                hasExplicitOverride
                  ? `Explicit provider ceiling: ${effectiveResultCount} results/query`
                  : `Inherits global default (${globalDefaultMaxResults}), clamped to provider limit`
              }
            >
              {hasExplicitOverride
                ? `override ${effectiveResultCount}/q`
                : `effective ${effectiveResultCount}/q (global)`}
            </span>
            <span
              className="text-[9px] font-mono text-muted-foreground px-1.5 py-0.5 rounded bg-muted/60"
              title="Read-only technical limit from backend provider capability registry"
            >
              {providerLimit !== null
                ? `supported max ${providerLimit}/q`
                : `provisional max ${effectiveCapLimit}/q`}
            </span>
            {provider.docsUrl && (
              <a
                href={provider.docsUrl}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="text-[10px] text-muted-foreground hover:text-primary transition-colors flex items-center gap-0.5"
              >
                <ExternalLink size={9} />
                <span className="hidden sm:inline">docs</span>
              </a>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground truncate mt-0.5">
            {provider.tagline}
          </p>
        </div>

        <div className="flex items-center gap-3 shrink-0">
          <span
            className={`text-[8px] font-mono font-semibold px-2 py-0.5 rounded-zs ${
              enabled
                ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30"
                : "bg-custom text-muted-foreground"
            }`}
          >
            {enabled ? "Active" : "Off"}
          </span>
          <span
            className="text-[10px] text-muted-foreground font-mono tabular-nums hidden md:inline"
            title="Estimated cost per query/credit"
          >
            Est. ${Number(effectiveCost).toFixed(4)}/q
          </span>
          {ping && !ping.testing && (
            <span
              className={`text-[10px] font-mono flex items-center gap-1 ${
                ping.ok ? "text-emerald-500" : "text-rose-500"
              }`}
            >
              {ping.ok ? <CheckCircle2 size={11} /> : <XCircle size={11} />}
              {ping.ok && typeof ping.latency_ms === "number"
                ? `${Math.round(ping.latency_ms)}ms`
                : ping.status || "Failed"}
            </span>
          )}
          <motion.div
            animate={{ rotate: expanded ? 180 : 0 }}
            transition={{ duration: 0.2 }}
          >
            <ChevronDown size={16} className="text-muted-foreground" />
          </motion.div>
        </div>
      </button>

      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="details"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            <div className="px-4 pt-4 pb-4 pl-[68px] space-y-4 bg-gigbg">
              {provider.lifecycleNotice && (
                <div className="flex items-start gap-2 p-2.5 rounded-sm border border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300 text-[11px]">
                  <AlertCircle size={14} className="shrink-0 mt-0.5" />
                  <span>{provider.lifecycleNotice}</span>
                </div>
              )}

              {/* Per-Provider Max Results per Query & Inheritance Control (SR-502, SR-503, SR-505, SR-506, SR-508) */}
              <div
                className={`p-3 rounded-sm border space-y-2.5 ${
                  validationError
                    ? "border-rose-500/50 bg-rose-500/5"
                    : "border-border/60 bg-background/50"
                }`}
              >
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div>
                    <label
                      htmlFor={`max-results-${provider.configKey}`}
                      className="text-[10px] font-semibold uppercase tracking-wider text-foreground block"
                    >
                      Max Results per Query
                    </label>
                    <p className="text-[10px] text-muted-foreground">
                      Provider-supported maximum:{" "}
                      <strong className="font-mono text-foreground">
                        {effectiveCapLimit}
                      </strong>{" "}
                      · Effective ceiling:{" "}
                      <strong className="font-mono text-primary">
                        {effectiveResultCount}
                      </strong>{" "}
                      · Actual results can be fewer.
                    </p>
                  </div>

                  <label className="flex items-center gap-2 text-[11px] font-medium cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={!hasExplicitOverride}
                      onChange={(e) => handleToggleInheritGlobal(e.target.checked)}
                      className="rounded border-border text-primary focus:ring-primary/40 cursor-pointer"
                    />
                    <span>
                      Use global default ({globalDefaultMaxResults})
                    </span>
                  </label>
                </div>

                {!hasExplicitOverride ? (
                  <div className="text-[11px] font-mono text-muted-foreground bg-muted/40 px-2.5 py-1.5 rounded-sm border border-border/40 flex items-center justify-between flex-wrap gap-2">
                    <span>
                      Inheriting global default ({globalDefaultMaxResults} results/query)
                    </span>
                    {globalDefaultMaxResults > effectiveCapLimit && (
                      <span className="text-amber-600 dark:text-amber-400">
                        Clamped to provider-supported maximum ({effectiveCapLimit})
                      </span>
                    )}
                  </div>
                ) : (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 items-center">
                    <div className="space-y-1">
                      <span className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground block">
                        Provider Override Ceiling (1..{effectiveCapLimit})
                      </span>
                      <input
                        id={`max-results-${provider.configKey}`}
                        type="number"
                        min={1}
                        max={effectiveCapLimit}
                        step={1}
                        value={subConfig.max_results ?? ""}
                        onChange={(e) => {
                          const rawVal = e.target.value;
                          if (rawVal === "") {
                            onUpdateField("max_results", "");
                            return;
                          }
                          const parsed = Number(rawVal);
                          onUpdateField(
                            "max_results",
                            Number.isNaN(parsed) ? rawVal : parsed
                          );
                        }}
                        aria-invalid={Boolean(validationError)}
                        className="w-full bg-background border border-border rounded-sm px-2.5 py-1.5 text-[11px] text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
                      />
                    </div>
                    <div className="text-[10px] text-muted-foreground space-y-0.5">
                      <div>
                        Tool calls can request fewer than{" "}
                        <strong className="font-mono">{effectiveResultCount}</strong>{" "}
                        results, never more.
                      </div>
                      {capability?.notes && (
                        <div className="opacity-85">{capability.notes}</div>
                      )}
                    </div>
                  </div>
                )}

                {validationError && (
                  <div
                    role="alert"
                    className="text-[11px] font-medium text-rose-600 dark:text-rose-400 flex items-center gap-1.5 pt-1"
                  >
                    <AlertCircle size={12} />
                    <span>{validationError}</span>
                  </div>
                )}
              </div>

              {/* Provider-specific fields & credentials.
                  `pt-3` provides a clear visual separation from the
                  "Max Results per Query" card above. `pt-1` (previous
                  value) was too tight, causing the SafeSearch label
                  to appear clipped against the inherited-default box. */}
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 pt-3">
                {provider.fields.map((field) => {
                  if (field.type === "password") {
                    const isVisible = showKeys[field.secretKey || provider.id];
                    return (
                      <div key={field.key} className="space-y-1">
                        <label className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">
                          {field.label}
                        </label>
                        <div className="relative">
                          <input
                            type={isVisible ? "text" : "password"}
                            value={subConfig[field.key] || ""}
                            onChange={(e) =>
                              onUpdateField(field.key, e.target.value)
                            }
                            placeholder={field.placeholder}
                            className="w-full bg-background border border-border rounded-sm px-2.5 py-1.5 text-[11px] text-foreground font-mono pr-7 focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
                          />
                          <button
                            type="button"
                            onClick={() =>
                              onToggleShowKey(field.secretKey || provider.id)
                            }
                            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground cursor-pointer"
                          >
                            {isVisible ? <EyeOff size={11} /> : <Eye size={11} />}
                          </button>
                        </div>
                      </div>
                    );
                  }

                  if (field.type === "select") {
                    const val =
                      subConfig[field.key] ||
                      field.defaultValue ||
                      field.options[0];
                    return (
                      <div key={field.key} className="space-y-1">
                        <label className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">
                          {field.label}
                        </label>
                        <select
                          value={val}
                          onChange={(e) =>
                            onUpdateField(field.key, e.target.value)
                          }
                          className="w-full bg-background border border-border rounded-sm px-2.5 py-1.5 text-[11px] text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
                        >
                          {field.options.map((opt) => (
                            <option key={opt} value={opt}>
                              {opt}
                            </option>
                          ))}
                        </select>
                      </div>
                    );
                  }

                  return (
                    <div key={field.key} className="space-y-1">
                      <label className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">
                        {field.label}
                      </label>
                      <input
                        type="text"
                        value={subConfig[field.key] || ""}
                        onChange={(e) =>
                          onUpdateField(field.key, e.target.value)
                        }
                        placeholder={field.placeholder}
                        className={`w-full bg-background border border-border rounded-sm px-2.5 py-1.5 text-[11px] text-foreground focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow ${
                          field.mono ? "font-mono" : ""
                        }`}
                      />
                    </div>
                  );
                })}

                {provider.costField && (
                  <div className="space-y-1">
                    <label className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">
                      Estimated Cost per Query (USD)
                    </label>
                    <input
                      type="number"
                      step="0.001"
                      min={0}
                      max={10}
                      value={effectiveCost}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value);
                        onUpdateField(
                          "cost_per_query",
                          Number.isNaN(val) ? 0.0 : Math.max(0.0, val)
                        );
                      }}
                      className="w-full bg-background border border-border rounded-sm px-2.5 py-1.5 text-[11px] text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
                    />
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between pt-3 border-t border-border/40 flex-wrap gap-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <Button
                    variant="outline"
                    onClick={(e) => {
                      e.stopPropagation();
                      onTest();
                    }}
                    disabled={ping?.testing || Boolean(validationError)}
                    className="h-7 text-[11px] px-2.5 gap-1.5 cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
                  >
                    {ping?.testing ? (
                      <RefreshCw size={11} className="animate-spin" />
                    ) : (
                      <Activity size={11} />
                    )}
                    <span>
                      {ping?.testing ? "Testing..." : provider.testLabel}
                    </span>
                  </Button>

                  {provider.consumesQuotaOnTest && (
                    <span className="text-[9px] font-mono text-amber-600 dark:text-amber-400 bg-amber-500/10 border border-amber-500/30 px-1.5 py-0.5 rounded">
                      Billable probe (1 req)
                    </span>
                  )}

                  {ping && !ping.testing && (
                    <div className="flex items-center gap-1.5 flex-wrap text-[10px]">
                      {typeof ping.reachable === "boolean" && (
                        <span
                          className={`font-mono px-1.5 py-0.5 rounded border ${
                            ping.reachable
                              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-500"
                              : "border-rose-500/30 bg-rose-500/10 text-rose-500"
                          }`}
                        >
                          {ping.reachable ? "Reachable" : "Unreachable"}
                        </span>
                      )}
                      {typeof ping.authenticated === "boolean" && (
                        <span
                          className={`font-mono px-1.5 py-0.5 rounded border ${
                            ping.authenticated
                              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-500"
                              : "border-rose-500/30 bg-rose-500/10 text-rose-500"
                          }`}
                        >
                          {ping.authenticated ? "Auth OK" : "Auth Rejected"}
                        </span>
                      )}
                      {typeof ping.effective_max_results === "number" && (
                        <span className="font-mono px-1.5 py-0.5 rounded border border-primary/30 bg-primary/10 text-primary">
                          eff {ping.effective_max_results}/
                          {ping.max_results_limit ?? effectiveCapLimit}
                        </span>
                      )}
                      {ping.msg && (
                        <span
                          className={
                            ping.ok ? "text-muted-foreground" : "text-rose-500"
                          }
                        >
                          {ping.msg}
                        </span>
                      )}
                    </div>
                  )}
                </div>

                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onToggleEnabled();
                  }}
                  className={`text-[10px] font-mono font-semibold px-2.5 py-1 border rounded-xs cursor-pointer transition-all ${
                    enabled
                      ? "bg-rose-500/10 text-rose-600 dark:text-rose-400 hover:border-rose-500/30 hover:bg-rose-500/20"
                      : "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 hover:border-emerald-500/30 hover:bg-emerald-500/20"
                  }`}
                >
                  {enabled ? "Disable Provider" : "Enable Provider"}
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
