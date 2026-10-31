"use client";

import React, { useState, useEffect, useRef } from "react";
import { Sidebar } from "./setup/Sidebar";
import { LLMTab } from "./setup/tabs/LLMTab";
import { UsageTab } from "./setup/tabs/UsageTab";
import { BrowserTab } from "./setup/tabs/BrowserTab";
import { SearchTab } from "./setup/tabs/SearchTab";
import { SandboxTab } from "./setup/tabs/SandboxTab";
import { MCPTab } from "./setup/tabs/MCPTab";
import { SystemTab } from "./setup/tabs/SystemTab";
import { ToastContainer, showToast } from "@/components/ui/ToastNotification";
import { saveLocalMetadataVault, fetchServerMetadata } from "@/lib/modelMetadata";
import { fetchVault, saveVaultToBackend, type VaultData } from "@/lib/vault";

import { GenerativeUITab } from "./setup/tabs/GenerativeUITab";

import {
  INITIAL_CLOUD_PROVIDERS,
  INITIAL_LMSTUDIO_SETTINGS,
  INITIAL_OLLAMA_SETTINGS,
  type SettingsTab,
  type CloudProviderVaultItem,
  type LMStudioSettings,
  type OllamaSettings,
  type CustomEndpoint,
  type FullAppConfig,
} from "./setup/types";

/**
 * Authoritative list of search providers supported by the backend
 * SearchProviderFactory (see backend/peldrun/tools/builtins/
 * search_providers/factory.py and backend/omweb/routers/search_rtr.py).
 *
 * Any provider identifier not present in this list MUST be filtered out
 * before persistence. Otherwise POST /api/config reaches
 * _validate_and_normalize_search_config, which calls
 * SearchProviderFactory.normalize_provider_key(...) and raises
 * "Unsupported search provider '<name>'" — rejecting the whole save.
 */
const SUPPORTED_SEARCH_PROVIDERS: readonly string[] = [
  "DuckDuckGo",
  "SearXNG",
  "Tavily",
  "Brave",
  "Google",
  "Jina",
  "Yandex",
];

const SUPPORTED_SEARCH_PROVIDER_LOOKUP: Map<string, string> = new Map(
  SUPPORTED_SEARCH_PROVIDERS.map((name) => [name.toLowerCase(), name])
);

const DEFAULT_SEARCH_ENGINE = "Google";
const DEFAULT_FALLBACK_ENGINES: readonly string[] = ["DuckDuckGo"];

/**
 * Resolve a raw engine identifier to its canonical supported display name.
 * Falls back to DEFAULT_SEARCH_ENGINE when the input is unknown.
 */
function resolveSupportedEngine(raw: unknown): string {
  if (typeof raw !== "string") return DEFAULT_SEARCH_ENGINE;
  const canonical = SUPPORTED_SEARCH_PROVIDER_LOOKUP.get(raw.trim().toLowerCase());
  return canonical ?? DEFAULT_SEARCH_ENGINE;
}

/**
 * Filter a raw fallback_engines array down to supported, deduplicated
 * providers that are NOT the primary engine. Preserves the input order.
 */
function sanitizeFallbackEngines(raw: unknown, primaryEngine: string): string[] {
  if (!Array.isArray(raw)) return [...DEFAULT_FALLBACK_ENGINES];

  const seen = new Set<string>();
  const cleaned: string[] = [];
  for (const item of raw) {
    if (typeof item !== "string") continue;
    const trimmed = item.trim();
    if (!trimmed) continue;
    const canonical = SUPPORTED_SEARCH_PROVIDER_LOOKUP.get(trimmed.toLowerCase());
    if (!canonical) continue;
    if (canonical === primaryEngine) continue;
    if (seen.has(canonical)) continue;
    seen.add(canonical);
    cleaned.push(canonical);
  }
  return cleaned;
}

/**
 * Sanitize the entire `search` config block so that:
 *   - `engine` always points to a backend-supported provider
 *   - `fallback_engines` never contains unsupported entries (e.g. Baidu/Bing)
 *   - the primary engine is never duplicated in the fallback list
 *
 * Safe to call on any arbitrary shape; unknown fields are passed through
 * untouched so new backend keys (duckduckgo, tavily, privacy_mode, ...)
 * survive the round-trip.
 */
function sanitizeSearchConfigForPersistence(rawSearch: unknown): any {
  if (!rawSearch || typeof rawSearch !== "object") {
    return {
      engine: DEFAULT_SEARCH_ENGINE,
      fallback_engines: [...DEFAULT_FALLBACK_ENGINES],
    };
  }
  const search = rawSearch as Record<string, any>;
  const engine = resolveSupportedEngine(search.engine);
  const fallback_engines = sanitizeFallbackEngines(search.fallback_engines, engine);
  return { ...search, engine, fallback_engines };
}

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<SettingsTab>("llm");
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<{ ok: boolean; message: string } | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);

  // Baseline reference string representing the last persisted configuration snapshot
  const initialLoadedRef = useRef<string>("");

  const [lmStudioSettings, setLmStudioSettings] = useState<LMStudioSettings>(INITIAL_LMSTUDIO_SETTINGS);
  const [ollamaSettings, setOllamaSettings] = useState<OllamaSettings>(INITIAL_OLLAMA_SETTINGS);
  const [cloudProviders, setCloudProviders] = useState<CloudProviderVaultItem[]>(INITIAL_CLOUD_PROVIDERS);

  // Clean dynamic state without hardcoded provider entries
  const [customEndpoints, setCustomEndpoints] = useState<CustomEndpoint[]>([]);

  const [config, setConfig] = useState<FullAppConfig>({
    llm: {
      provider: "lmstudio",
      provider_name: "LM Studio (Local)",
      model: "qwen3-vl-8b-instruct",
      base_url: "http://127.0.0.1:1234/v1",
      api_key: "",
      max_tokens: 8192,
      temperature: 0.0,
      api_type: "",
    },
    llm_vision: {
      provider: "lmstudio",
      provider_name: "LM Studio (Local)",
      model: "qwen3-vl-8b-instruct",
      base_url: "http://127.0.0.1:1234/v1",
      api_key: "",
      max_tokens: 8192,
      temperature: 0.0,
    },
    browser: {
      headless: false,
      disable_security: true,
      chrome_instance_path: "",
      cdp_url: "http://localhost:9222",
      wss_url: "",
      max_content_length: 2000,
      proxy: { server: "", username: "", password: "" },
    },
    search: {
      engine: DEFAULT_SEARCH_ENGINE,
      fallback_engines: [...DEFAULT_FALLBACK_ENGINES],
      retry_delay: 60,
      max_retries: 3,
      lang: "en",
      country: "us",
    },
    sandbox: {
      use_sandbox: false,
      image: "python:3.12-slim",
      work_dir: "/workspace",
      memory_limit: "1g",
      cpu_limit: 2.0,
      timeout: 300,
      network_enabled: false,
    },
    daytona: {
      daytona_api_key: "",
      daytona_server_url: "https://app.daytona.io/api",
      daytona_target: "us",
      sandbox_image_name: "whitezxj/sandbox:0.1.0",
      VNC_password: "",
    },
    mcp: { server_reference: "app.mcp.server" },
    runflow: { use_data_analysis_agent: false },
  });

  const [systemInfo, setSystemInfo] = useState<any>(null);

  // Compute live state serialization snapshot and dirty state
  const currentSnapshot = JSON.stringify({ config, lmStudioSettings, ollamaSettings, cloudProviders, customEndpoints });
  const isDirty = initialLoadedRef.current !== "" && initialLoadedRef.current !== currentSnapshot;

  // All /api/* calls go through Next.js rewrites, which route to the
  // backend configured at build time via PELDRUN_BACKEND_URL
  // (see next.config.mjs). Using an absolute URL here would bypass
  // those rewrites entirely and silently pin the frontend to a
  // specific host/port that may not match the active backend
  // instance in staging, production, or behind a reverse proxy.
  //
  // The function is kept (rather than inlined) as a single extension
  // point for future cross-cutting concerns (auth headers, request
  // tracing) without touching every call site.
  const getApiUrl = (endpoint: string) => {
    return endpoint;
  };

  const sanitizeTokens = (raw: any): number => {
    const num = parseInt(String(raw).replace(/[^0-9]/g, ""), 10);
    if (isNaN(num) || num <= 0 || num > 2000000) return 8192;
    return num;
  };

  // [ENRICH-METADATA] Resolve a provider's metadata endpoint to its
  // canonical URL when the user left the field empty. This prevents
  // models_vault.json from storing `"metadataEndpoint": ""` for providers
  // whose endpoint is computable (LM Studio, Ollama, Google, Anthropic,
  // DeepSeek). OpenAI and unknown custom servers legitimately remain "".
  const resolveMetadataEndpoint = (
    providerId: string,
    baseUrl: string,
    providerName?: string,
    userValue?: string,
  ): string => {
    const explicit = (userValue || "").trim();
    if (explicit) return explicit;

    const id = String(providerId || providerName || "").toLowerCase();
    const base = String(baseUrl || "").trim().replace(/\/+$/, "");

    if (
      id.includes("lmstudio") ||
      id.includes("lm-studio") ||
      id.includes("lm_studio") ||
      base.includes(":1234")
    ) {
      return `${base.replace(/\/v1$/, "")}/api/v0/models`;
    }
    if (id === "ollama" || base.includes(":11434")) {
      return `${base.replace(/\/v1$/, "")}/api/tags`;
    }
    if (id === "google" || id === "gemini" || base.includes("googleapis.com")) {
      const native = base
        .replace(/\/openai\/v1$/, "")
        .replace(/\/openai$/, "")
        .replace(/\/v1beta$/, "")
        .replace(/\/v1$/, "");
      return `${native}/v1beta/models`;
    }
    if (id === "anthropic" || id === "claude" || base.includes("anthropic.com")) {
      return "https://api.anthropic.com/v1/models";
    }
    if (id === "deepseek" || base.includes("deepseek.com")) {
      return "https://api.deepseek.com/models";
    }
    return "";
  };

  // Build an enriched snapshot of all four provider stores with
  // `metadataEndpoint` resolved to real URLs where possible.
  const enrichVaultDefaults = (sources: {
    lmStudioSettings: LMStudioSettings;
    ollamaSettings: OllamaSettings;
    cloudProviders: CloudProviderVaultItem[];
    customEndpoints: CustomEndpoint[];
  }) => {
    const lm: LMStudioSettings = {
      ...sources.lmStudioSettings,
      metadataEndpoint:
        resolveMetadataEndpoint(
          "lmstudio",
          sources.lmStudioSettings.baseUrl,
          "LM Studio",
          sources.lmStudioSettings.metadataEndpoint,
        ) || undefined,
    };

    const ol: OllamaSettings = {
      ...sources.ollamaSettings,
      metadataEndpoint:
        resolveMetadataEndpoint(
          "ollama",
          sources.ollamaSettings.baseUrl,
          "Ollama",
          sources.ollamaSettings.metadataEndpoint,
        ) || undefined,
    };

    const clouds = sources.cloudProviders.map((cp) => ({
      ...cp,
      metadataEndpoint:
        resolveMetadataEndpoint(
          cp.id,
          cp.baseUrl,
          cp.name,
          cp.metadataEndpoint,
        ) || undefined,
    }));

    const customs = sources.customEndpoints.map((ce) => ({
      ...ce,
      metadataEndpoint:
        resolveMetadataEndpoint(
          ce.providerId || ce.id,
          ce.endpointUrl,
          ce.name,
          ce.metadataEndpoint,
        ) || undefined,
    }));

    return { lm, ol, clouds, customs };
  };

  const fetchConfig = async () => {
    try {
      const res = await fetch(getApiUrl("/api/config"));
      const data = res.ok ? await res.json() : {};
      const cfg = data.config || {};

      const vault: VaultData = await fetchVault();

      let storedCloud = INITIAL_CLOUD_PROVIDERS;
      let storedLM = INITIAL_LMSTUDIO_SETTINGS;
      let storedOllama = INITIAL_OLLAMA_SETTINGS;
      let storedCustom: CustomEndpoint[] = [];

      if (vault.scanned_models && Array.isArray(vault.scanned_models) && vault.scanned_models.length > 0) {
        setAvailableModels(vault.scanned_models);
      }

      if (vault.lmstudio_vault) {
        storedLM = { ...INITIAL_LMSTUDIO_SETTINGS, ...vault.lmstudio_vault };
        if (vault.lmstudio_vault.savedModels && vault.lmstudio_vault.savedModels.length > 0) {
          setAvailableModels((prev) => (prev.length > 0 ? prev : vault.lmstudio_vault.savedModels || []));
        }
      }

      if (vault.ollama_vault) {
        storedOllama = { ...INITIAL_OLLAMA_SETTINGS, ...vault.ollama_vault, savedModels: vault.ollama_vault.savedModels || [] };
      }

      if (vault.cloud_vault && Array.isArray(vault.cloud_vault) && vault.cloud_vault.length > 0) {
        const cleanCloud = vault.cloud_vault
          .filter((p: any) => p.id !== "ollama")
          .map((p: any) => {
            if (
              p.id === "deepseek" &&
              (p.baseUrl?.includes("ppinfra") ||
                p.name?.includes("PPIO") ||
                p.model?.includes("ppinfra") ||
                p.model?.includes("0324"))
            ) {
              return {
                ...p,
                name: "DeepSeek",
                baseUrl: "https://api.deepseek.com",
                model: "deepseek-chat",
                popularModels: ["deepseek-chat", "deepseek-reasoner"],
              };
            }
            return p;
          });
        storedCloud = INITIAL_CLOUD_PROVIDERS.map((base) => {
          const match = cleanCloud.find((p: any) => p.id === base.id);
          return match ? { ...base, ...match } : base;
        });
      }

      if (vault.custom_endpoints && Array.isArray(vault.custom_endpoints)) {
        storedCustom = vault.custom_endpoints;
      }

      const loadedConfig: FullAppConfig = {
        llm: {
          provider: cfg.llm?.provider || storedLM.provider || "lmstudio",
          provider_name: cfg.llm?.provider_name || "LM Studio (Local)",
          model: cfg.llm?.model || storedLM.model,
          base_url: cfg.llm?.base_url || storedLM.baseUrl,
          api_key: cfg.llm?.api_key || storedLM.apiKey || "",
          max_tokens: sanitizeTokens(cfg.llm?.max_tokens || 8192),
          temperature: cfg.llm?.temperature ?? 0.0,
          api_type: cfg.llm?.api_type || "",
        },
        llm_vision: {
          provider: cfg["llm.vision"]?.provider || "lmstudio",
          provider_name: cfg["llm.vision"]?.provider_name || "LM Studio (Local)",
          model: cfg["llm.vision"]?.model || cfg.llm_vision?.model || storedLM.model,
          base_url: cfg["llm.vision"]?.base_url || cfg.llm_vision?.base_url || storedLM.baseUrl,
          api_key: cfg["llm.vision"]?.api_key || cfg.llm_vision?.api_key || "",
          max_tokens: sanitizeTokens(cfg["llm.vision"]?.max_tokens || 8192),
          temperature: cfg["llm.vision"]?.temperature ?? 0.0,
        },
        browser: {
          headless: cfg.browser?.headless ?? false,
          disable_security: cfg.browser?.disable_security ?? true,
          chrome_instance_path: cfg.browser?.chrome_instance_path || "",
          cdp_url: cfg.browser?.cdp_url || "http://localhost:9222",
          wss_url: cfg.browser?.wss_url || "",
          max_content_length: parseInt(String(cfg.browser?.max_content_length), 10) || 2000,
          proxy: {
            server: cfg["browser.proxy"]?.server || cfg.browser?.proxy?.server || "",
            username: cfg["browser.proxy"]?.username || cfg.browser?.proxy?.username || "",
            password: cfg["browser.proxy"]?.password || cfg.browser?.proxy?.password || "",
          },
        },
        search: sanitizeSearchConfigForPersistence({
          // Preserve the entire search section from the backend
          // (including duckduckgo, searxng, tavily, default_max_results,
          // privacy_mode, etc.). Without `...(cfg.search)`, fetchConfig
          // would overwrite the full state produced by SearchTab with
          // a partial object of just the six basic fields. That made
          // the global Save button send an incomplete payload and made
          // the UI appear to lose the settings after a page reload.
          //
          // sanitizeSearchConfigForPersistence is applied AFTER the spread
          // so that any legacy on-disk entries (e.g. Baidu, Bing) are
          // stripped before the state is committed to React — otherwise
          // the next "Save Config" click would forward them verbatim to
          // POST /api/config and the backend would reject the entire save.
          ...(cfg.search || {}),
          retry_delay: cfg.search?.retry_delay ?? 60,
          max_retries: cfg.search?.max_retries ?? 3,
          lang: cfg.search?.lang || "en",
          country: cfg.search?.country || "us",
        }),
        sandbox: {
          use_sandbox: cfg.sandbox?.use_sandbox ?? false,
          image: cfg.sandbox?.image || "python:3.12-slim",
          work_dir: "/workspace",
          memory_limit: cfg.sandbox?.memory_limit || "1g",
          cpu_limit: cfg.sandbox?.cpu_limit ?? 2.0,
          timeout: cfg.sandbox?.timeout ?? 300,
          network_enabled: cfg.sandbox?.network_enabled ?? false,
        },
        daytona: {
          daytona_api_key: cfg.daytona?.daytona_api_key || "",
          daytona_server_url: cfg.daytona?.daytona_server_url || "https://app.daytona.io/api",
          daytona_target: cfg.daytona?.daytona_target || "us",
          sandbox_image_name: cfg.daytona?.sandbox_image_name || "whitezxj/sandbox:0.1.0",
          VNC_password: cfg.daytona?.VNC_password || "",
        },
        mcp: { server_reference: cfg.mcp?.server_reference || "app.mcp.server" },
        runflow: { use_data_analysis_agent: cfg.runflow?.use_data_analysis_agent ?? false },
      };

      // Enrich on load: fill empty `metadataEndpoint` fields with the
      // computed default so the UI immediately shows the working URL.
      const enriched = enrichVaultDefaults({
        lmStudioSettings: storedLM,
        ollamaSettings: storedOllama,
        cloudProviders: storedCloud,
        customEndpoints: storedCustom,
      });

      setLmStudioSettings(enriched.lm);
      setOllamaSettings(enriched.ol);
      setCloudProviders(enriched.clouds);
      setCustomEndpoints(enriched.customs);
      setConfig(loadedConfig);

      initialLoadedRef.current = JSON.stringify({
        config: loadedConfig,
        lmStudioSettings: enriched.lm,
        ollamaSettings: enriched.ol,
        cloudProviders: enriched.clouds,
        customEndpoints: enriched.customs,
      });

      if (typeof window !== "undefined") {
        // Persist only meaningful values. Writing "null"/"undefined"
        // would corrupt later reads via useAppStorage("active_model"),
        // because the storage schema now allows null as a legitimate
        // "unselected" state.
        const activeModel = loadedConfig.llm.model;
        const activeProvider = loadedConfig.llm.provider_name;
        if (typeof activeModel === "string" && activeModel.trim()) {
          localStorage.setItem("omweb_active_model", activeModel);
        }
        if (typeof activeProvider === "string" && activeProvider.trim()) {
          localStorage.setItem("omweb_active_provider", activeProvider);
        }
      }
    } catch (e: any) {
      console.error("Failed to load config", e);
      showToast.error("Backend Connection Error", e.message);
    }
  };

  const fetchSystemInfo = async () => {
    try {
      const res = await fetch(getApiUrl("/api/status/system-info"));
      if (res.ok) {
        const data = await res.json();
        setSystemInfo(data);
      }
    } catch (e) {
      console.error("Failed to fetch system info", e);
    }
  };

  useEffect(() => {
    fetchConfig();
    fetchSystemInfo();
    fetchServerMetadata();
  }, []);

  // [AUTO-SAVE-METADATA] Persist `metadataEndpoint` changes automatically.
  //
  // The Metadata Endpoint field is a small, focused setting that users are
  // unlikely to remember to "Save Config" for after editing. Debounced
  // auto-save keeps models_vault.json in sync without requiring an extra
  // click, while the bulk "Save Config" button continues to handle the
  // rest of the configuration as before.
  //
  // Only fires when a metadataEndpoint value is *present* and *differs*
  // from what's already on disk. This avoids spurious POSTs on initial
  // mount or on unrelated state changes.
  const autoSaveTimerRef = useRef<number | null>(null);
  const persistedMetaRef = useRef<string>("");
  useEffect(() => {
    if (initialLoadedRef.current === "") return; // not loaded yet

    const metadataSnapshot = JSON.stringify({
      lm: lmStudioSettings.metadataEndpoint || "",
      ol: ollamaSettings.metadataEndpoint || "",
      cloud: cloudProviders.map((c) => [c.id, c.metadataEndpoint || ""]),
      custom: customEndpoints.map((c) => [c.id, c.metadataEndpoint || ""]),
    });

    if (persistedMetaRef.current === "") {
      // First snapshot after load — remember it, don't trigger a save.
      persistedMetaRef.current = metadataSnapshot;
      return;
    }
    if (persistedMetaRef.current === metadataSnapshot) return;

    if (autoSaveTimerRef.current !== null) {
      window.clearTimeout(autoSaveTimerRef.current);
    }
    autoSaveTimerRef.current = window.setTimeout(async () => {
      persistedMetaRef.current = metadataSnapshot;
      try {
        const enriched = enrichVaultDefaults({
          lmStudioSettings,
          ollamaSettings,
          cloudProviders,
          customEndpoints,
        });
        await saveVaultToBackend({
          lmstudio_vault: enriched.lm,
          ollama_vault: enriched.ol,
          cloud_vault: enriched.clouds,
          custom_endpoints: enriched.customs,
        });
      } catch (err) {
        console.warn("[auto-save metadata] failed:", err);
      }
    }, 800);

    return () => {
      if (autoSaveTimerRef.current !== null) {
        window.clearTimeout(autoSaveTimerRef.current);
      }
    };
  }, [lmStudioSettings, ollamaSettings, cloudProviders, customEndpoints]);

  const handleReset = () => {
    if (initialLoadedRef.current) {
      const parsed = JSON.parse(initialLoadedRef.current);
      setConfig(parsed.config);
      setLmStudioSettings(parsed.lmStudioSettings);
      setOllamaSettings(parsed.ollamaSettings || INITIAL_OLLAMA_SETTINGS);
      setCloudProviders(parsed.cloudProviders);
      setCustomEndpoints(parsed.customEndpoints || []);
      showToast.info("Changes Reverted", "Restored last saved vault configuration.");
    }
  };

  const activateEngine = (
    providerId: string,
    providerName: string,
    model: string,
    baseUrl: string,
    apiKey: string,
    apiType: string
  ) => {
    setConfig((prev) => ({
      ...prev,
      llm: {
        ...prev.llm,
        provider: providerId,
        provider_name: providerName,
        model,
        base_url: baseUrl,
        api_key: apiKey,
        api_type: apiType || "",
        max_tokens: 8192,
      },
    }));

    if (providerId === "lmstudio") {
      setLmStudioSettings((prev) => ({ ...prev, model, baseUrl, apiKey }));
    } else if (providerId === "ollama") {
      setOllamaSettings((prev) => ({ ...prev, model, baseUrl }));
    }

    if (typeof window !== "undefined") {
      localStorage.setItem("omweb_active_model", model);
      localStorage.setItem("omweb_active_provider", providerName);
      localStorage.setItem(
        "omweb_active_llm_override",
        JSON.stringify({
          model,
          provider: providerId,
          provider_name: providerName,
          base_url: baseUrl,
          api_key: apiKey,
          api_type: apiType || "",
        })
      );
      window.dispatchEvent(
        new CustomEvent("omweb:model-change", {
          detail: { model, provider_name: providerName },
        })
      );
    }

    showToast.success("Active Engine Set", `${providerName} (${model}) is now primary.`);
  };

  const deactivateToDefault = () => {
    activateEngine(
      "lmstudio",
      "LM Studio (Local)",
      lmStudioSettings.model,
      lmStudioSettings.baseUrl,
      lmStudioSettings.apiKey,
      ""
    );
    showToast.info("Reverted to Local Engine", "LM Studio (Local GPU) is now active primary.");
  };

  const testEndpoint = async (baseUrl: string, apiKey: string, model: string, apiType?: string) => {
    try {
      const res = await fetch(getApiUrl("/api/config/test-llm"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model, api_type: apiType || "" }),
      });
      const data = await res.json();
      return {
        ok: Boolean(data.ok),
        message: data.message || (data.ok ? "Connected" : "Rejected"),
        latency: data.latency_ms,
      };
    } catch (e: any) {
      return { ok: false, message: e.message || "Endpoint unreachable" };
    }
  };

  const handleFetchModels = async () => {
    setFetchingModels(true);
    setScanError(null);
    try {
      const res = await fetch(getApiUrl("/api/config/fetch-models"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: lmStudioSettings.baseUrl,
          api_key: lmStudioSettings.apiKey,
          provider_id: "lmstudio",
        }),
      });
      const data = await res.json();
      if (data.ok && Array.isArray(data.models) && data.models.length > 0) {
        if (data.models_metadata) saveLocalMetadataVault(data.models_metadata);
        setAvailableModels(data.models);
        const updatedVaultLM: LMStudioSettings = {
          ...lmStudioSettings,
          savedModels: data.models,
        };
        setLmStudioSettings(updatedVaultLM);

        void saveVaultToBackend({
          scanned_models: data.models,
          lmstudio_vault: updatedVaultLM,
        });

        showToast.success("Models Detected", `Retrieved ${data.models.length} local GPU models!`);
      } else {
        const msg = data.message || data.error || "No models returned from local endpoint.";
        setScanError(msg);
        showToast.warning("Scan Notice", msg);
      }
    } catch (e: any) {
      const msg = `Connection failed: ${e.message}`;
      setScanError(msg);
      showToast.error("Scan Failed", msg);
    } finally {
      setFetchingModels(false);
    }
  };

  const assignDetectedModel = (modelName: string, target: "primary" | "vision") => {
    if (target === "primary") {
      setConfig((p) => ({
        ...p,
        llm: {
          ...p.llm,
          model: modelName,
          provider: "lmstudio",
          provider_name: "LM Studio (Local)",
          base_url: lmStudioSettings.baseUrl,
        },
      }));
      const updatedLM: LMStudioSettings = {
        ...lmStudioSettings,
        model: modelName,
        savedModels: lmStudioSettings.savedModels || availableModels || [modelName],
      };
      setLmStudioSettings(updatedLM);

      void saveVaultToBackend({
        lmstudio_vault: updatedLM,
      });

      if (typeof window !== "undefined") {
        localStorage.setItem("omweb_active_model", modelName);
        localStorage.setItem("omweb_active_provider", "LM Studio (Local)");
        localStorage.setItem(
          "omweb_active_llm_override",
          JSON.stringify({
            model: modelName,
            provider: "lmstudio",
            provider_name: "LM Studio (Local)",
            base_url: lmStudioSettings.baseUrl,
            api_key: lmStudioSettings.apiKey,
            api_type: "",
          })
        );
        window.dispatchEvent(
          new CustomEvent("omweb:model-change", {
            detail: { model: modelName, provider_name: "LM Studio (Local)" },
          })
        );
      }
      showToast.success("Primary Model Set", modelName);
    } else {
      setConfig((p) => ({
        ...p,
        llm_vision: {
          ...p.llm_vision,
          model: modelName,
          provider: "lmstudio",
          provider_name: "LM Studio (Local)",
          base_url: lmStudioSettings.baseUrl,
        },
      }));
      showToast.info("Vision Model Set", modelName);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveStatus(null);
    try {
      const activeLlm = { ...config.llm };
      if (activeLlm.provider === "lmstudio") {
        activeLlm.model = lmStudioSettings.model;
        activeLlm.base_url = lmStudioSettings.baseUrl;
        activeLlm.api_key = lmStudioSettings.apiKey;
        activeLlm.provider_name = "LM Studio (Local)";
      } else if (activeLlm.provider === "ollama") {
        activeLlm.model = ollamaSettings.model;
        const b = ollamaSettings.baseUrl.trim().replace(/\/+$/, "");
        activeLlm.base_url = b.endsWith("/v1") ? b : `${b}/v1`;
        activeLlm.api_key = "";
        activeLlm.provider_name = "Ollama (Local)";
      }

      activeLlm.max_tokens = sanitizeTokens(activeLlm.max_tokens);

      const llmDict: any = { ...activeLlm };
      llmDict["vision"] = config.llm_vision;
      llmDict.max_tokens = activeLlm.max_tokens;

      // Final defensive sanitization immediately before the network call.
      // Even if a legacy provider identifier (e.g. Baidu, Bing) somehow
      // still lives in React state — e.g. injected by a hot-reload edge
      // case or an intermediate setState from SearchTab.tsx — it is
      // stripped here so POST /api/config never receives an unsupported
      // provider and never fails with "Unsupported search provider".
      const sanitizedSearch = sanitizeSearchConfigForPersistence(config.search);

      const payload = {
        llm: llmDict,
        browser: {
          headless: config.browser.headless,
          disable_security: config.browser.disable_security,
          chrome_instance_path: config.browser.chrome_instance_path || null,
          cdp_url: config.browser.cdp_url || null,
          wss_url: config.browser.wss_url || null,
          max_content_length: parseInt(String(config.browser.max_content_length), 10) || 2000,
          proxy: config.browser.proxy,
        },
        search: sanitizedSearch,
        sandbox: config.sandbox,
        daytona: config.daytona,
        mcp: config.mcp,
        runflow: config.runflow,
      };

      const res = await fetch(getApiUrl("/api/config"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        const updatedLM: LMStudioSettings = {
          ...lmStudioSettings,
          savedModels:
            availableModels.length > 0
              ? availableModels
              : lmStudioSettings.savedModels || [],
        };

        const safeOllamaVault: OllamaSettings = {
          ...ollamaSettings,
          savedModels: ollamaSettings.savedModels || [],
        };

        // Resolve empty metadata endpoints to computed defaults before
        // persisting, so models_vault.json always stores the real URL.
        const enriched = enrichVaultDefaults({
          lmStudioSettings: updatedLM,
          ollamaSettings: safeOllamaVault,
          cloudProviders,
          customEndpoints,
        });

        await saveVaultToBackend({
          cloud_vault: enriched.clouds,
          custom_endpoints: enriched.customs,
          lmstudio_vault: enriched.lm,
          ollama_vault: enriched.ol,
          scanned_models: availableModels,
        });

        const finalConfig: FullAppConfig = {
          ...config,
          llm: activeLlm,
          search: sanitizedSearch,
          browser: {
            ...config.browser,
            max_content_length:
              parseInt(String(config.browser.max_content_length), 10) || 2000,
          },
        };

        setConfig(finalConfig);
        setLmStudioSettings(enriched.lm);
        setOllamaSettings(enriched.ol);
        setCloudProviders(enriched.clouds);
        setCustomEndpoints(enriched.customs);

        initialLoadedRef.current = JSON.stringify({
          config: finalConfig,
          lmStudioSettings: enriched.lm,
          ollamaSettings: enriched.ol,
          cloudProviders: enriched.clouds,
          customEndpoints: enriched.customs,
        });

        setSaveStatus({ ok: true, message: "Configuration & Vault permanently saved!" });
        showToast.success("Saved Successfully", `Active: ${activeLlm.provider_name} (${activeLlm.model})`);

        if (typeof window !== "undefined") {
          const safeModel =
            typeof activeLlm.model === "string" ? activeLlm.model.trim() : "";
          const safeProvider =
            typeof activeLlm.provider_name === "string"
              ? activeLlm.provider_name.trim()
              : "";

          // Skip individual writes when the value is empty. Writing an
          // empty string here would (a) fail the storage schema's
          // string().min(1) rule on the next read via useAppStorage,
          // and (b) re-trigger "not configured" UI even after a save.
          if (safeModel) {
            localStorage.setItem("omweb_active_model", safeModel);
          }
          if (safeProvider) {
            localStorage.setItem("omweb_active_provider", safeProvider);
          }
          localStorage.setItem(
            "omweb_active_llm_override",
            JSON.stringify({
              model: safeModel || null,
              provider: activeLlm.provider || null,
              provider_name: safeProvider || null,
              base_url: activeLlm.base_url || null,
              api_key: activeLlm.api_key || null,
              api_type: activeLlm.api_type || "",
            })
          );
          window.dispatchEvent(
            new CustomEvent("omweb:model-change", {
              detail: {
                model: safeModel || null,
                provider_name: safeProvider || null,
              },
            })
          );
        }

        setTimeout(() => setSaveStatus(null), 4000);
      } else {
        const errData = await res.json().catch(() => ({}));
        const msg = errData.detail || "Server rejected configuration payload.";
        setSaveStatus({ ok: false, message: msg });
        showToast.error("Save Failed", msg);
      }
    } catch (e: any) {
      setSaveStatus({ ok: false, message: `Save failed: ${e.message}` });
      showToast.error("Save Failed", e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex h-full w-full bg-background text-foreground font-sans overflow-hidden">
      <ToastContainer />

      <Sidebar
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        handleSave={handleSave}
        handleReset={handleReset}
        saving={saving}
        isDirty={isDirty}
        saveStatus={saveStatus}
      />

      <div className="taman w-full flex-1 overflow-y-auto p-0 space-y-6 bg-background">
        {activeTab === "llm" && (
          <LLMTab
            config={config}
            setConfig={setConfig}
            lmStudioSettings={lmStudioSettings}
            setLmStudioSettings={setLmStudioSettings}
            ollamaSettings={ollamaSettings}
            setOllamaSettings={setOllamaSettings}
            cloudProviders={cloudProviders}
            setCloudProviders={setCloudProviders}
            customEndpoints={customEndpoints}
            setCustomEndpoints={setCustomEndpoints}
            availableModels={availableModels}
            setAvailableModels={setAvailableModels}
            fetchingModels={fetchingModels}
            scanError={scanError}
            handleFetchModels={handleFetchModels}
            assignDetectedModel={assignDetectedModel}
            activateEngine={activateEngine}
            deactivateToDefault={deactivateToDefault}
            testEndpoint={testEndpoint}
          />
        )}

        {activeTab === "usage" && <UsageTab />}
        {activeTab === "browser" && <BrowserTab config={config} setConfig={setConfig} />}
        {activeTab === "search" && <SearchTab config={config} setConfig={setConfig} />}
        {activeTab === "sandbox" && <SandboxTab config={config} setConfig={setConfig} />}
        {activeTab === "mcp" && <MCPTab config={config} setConfig={setConfig} />}
        {activeTab === "system" && <SystemTab systemInfo={systemInfo} fetchSystemInfo={fetchSystemInfo} />}
        {activeTab === "generative-ui" && <GenerativeUITab />}
      </div>
    </div>
  );
}
