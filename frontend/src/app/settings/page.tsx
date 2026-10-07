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
      engine: "Google",
      fallback_engines: ["DuckDuckGo", "Baidu", "Bing"],
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

  const getApiUrl = (endpoint: string) => {
    if (typeof window !== "undefined" && (window.location.port === "3088" || window.location.port === "3000")) {
      return `http://localhost:8088${endpoint}`;
    }
    return endpoint;
  };

  const sanitizeTokens = (raw: any): number => {
    const num = parseInt(String(raw).replace(/[^0-9]/g, ""), 10);
    if (isNaN(num) || num <= 0 || num > 2000000) return 8192;
    return num;
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
        search: {
          engine: cfg.search?.engine || "Google",
          fallback_engines: Array.isArray(cfg.search?.fallback_engines)
            ? cfg.search.fallback_engines
            : ["DuckDuckGo", "Baidu", "Bing"],
          retry_delay: cfg.search?.retry_delay ?? 60,
          max_retries: cfg.search?.max_retries ?? 3,
          lang: cfg.search?.lang || "en",
          country: cfg.search?.country || "us",
        },
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

      setLmStudioSettings(storedLM);
      setOllamaSettings(storedOllama);
      setCloudProviders(storedCloud);
      setCustomEndpoints(storedCustom);
      setConfig(loadedConfig);

      initialLoadedRef.current = JSON.stringify({
        config: loadedConfig,
        lmStudioSettings: storedLM,
        ollamaSettings: storedOllama,
        cloudProviders: storedCloud,
        customEndpoints: storedCustom,
      });

      if (typeof window !== "undefined") {
        localStorage.setItem("omweb_active_model", loadedConfig.llm.model);
        localStorage.setItem("omweb_active_provider", loadedConfig.llm.provider_name);
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
        search: config.search,
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
          savedModels: availableModels.length > 0 ? availableModels : lmStudioSettings.savedModels || [],
        };

        const safeOllamaVault: OllamaSettings = {
          ...ollamaSettings,
          savedModels: ollamaSettings.savedModels || [],
        };

        await saveVaultToBackend({
          cloud_vault: cloudProviders,
          custom_endpoints: customEndpoints,
          lmstudio_vault: updatedLM,
          ollama_vault: safeOllamaVault,
          scanned_models: availableModels,
        });

        const finalConfig: FullAppConfig = {
          ...config,
          llm: activeLlm,
          browser: {
            ...config.browser,
            max_content_length: parseInt(String(config.browser.max_content_length), 10) || 2000,
          },
        };

        setConfig(finalConfig);
        setLmStudioSettings(updatedLM);
        setOllamaSettings(safeOllamaVault);

        initialLoadedRef.current = JSON.stringify({
          config: finalConfig,
          lmStudioSettings: updatedLM,
          ollamaSettings: safeOllamaVault,
          cloudProviders,
          customEndpoints,
        });

        setSaveStatus({ ok: true, message: "Configuration & Vault permanently saved!" });
        showToast.success("Saved Successfully", `Active: ${activeLlm.provider_name} (${activeLlm.model})`);

        if (typeof window !== "undefined") {
          localStorage.setItem("omweb_active_model", activeLlm.model);
          localStorage.setItem("omweb_active_provider", activeLlm.provider_name);
          localStorage.setItem(
            "omweb_active_llm_override",
            JSON.stringify({
              model: activeLlm.model,
              provider: activeLlm.provider,
              provider_name: activeLlm.provider_name,
              base_url: activeLlm.base_url,
              api_key: activeLlm.api_key,
              api_type: activeLlm.api_type || "",
            })
          );
          window.dispatchEvent(
            new CustomEvent("omweb:model-change", {
              detail: { model: activeLlm.model, provider_name: activeLlm.provider_name },
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
      </div>
    </div>
  );
}