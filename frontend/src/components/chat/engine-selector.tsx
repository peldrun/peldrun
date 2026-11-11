// frontend/src/components/chat/engine-selector.tsx

"use client";

import React, { useState, useEffect, useRef } from "react";
import Link from "next/link";
import {
  Cloud,
  Terminal,
  Server,
  ChevronDown,
  Check,
  Brain,
  Eye,
  Wrench,
  Package,
} from "lucide-react";
import { Icon } from "@/components/Icon/Icon";
import { inferModelCapabilities, fetchServerMetadata } from "@/lib/modelMetadata";
import { fetchVault } from "@/lib/vault";
import { useAppStorage } from "@/hooks/use-app-storage";
import { storage } from "@/lib/storage";

export interface EngineOption {
  id: string;
  name: string;
  mode: "agent" | "chat";
  description?: string;
  icon?: any;
  [key: string]: any;
}

export interface ProviderPayload {
  model: string;
  provider: string;
  provider_name: string;
  base_url: string;
  api_key: string;
  api_type: string;
}

export interface EngineSelectorProps {
  /**
   * Controls the dropdown orientation.
   *   - "up":   always open upward
   *   - "down": always open downward
   *   - "auto": compute from the trigger's viewport position
   */
  direction?: "up" | "down" | "auto";
}

export function EngineSelector({ direction = "auto" }: EngineSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [openUpward, setOpenUpward] = useState(false);

  // Both values are sourced from `useAppStorage`, whose schema allows
  // `null` to represent the "no selection" state. Every downstream
  // use must handle null explicitly.
  const [activeModel, setActiveModel] = useAppStorage("active_model");
  const [activeProvider, setActiveProvider] = useAppStorage("active_provider");
  const [, setActiveLlmOverride] = useAppStorage("active_llm_override");

  const [activeCloudProviders, setActiveCloudProviders] = useState<any[]>([]);
  const [activeCustomEndpoints, setActiveCustomEndpoints] = useState<any[]>([]);
  const [activeProviderPackages, setActiveProviderPackages] = useState<
    Array<{
      id: string;
      displayName: string;
      iconName: string;
      models: string[];
    }>
  >([]);
  const [lmStudioItem, setLmStudioItem] = useState<any>(null);
  const [localGPUModels, setLocalGPUModels] = useState<string[]>([]);
  const [ollamaItem, setOllamaItem] = useState<any>(null);
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});
  const [mounted, setMounted] = useState(false);

  const dropdownRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  // Defer capability-badge rendering until after hydration to avoid
  // SSR/CSR mismatches on the trigger button. `metadataVault` is
  // populated client-side only, so badges must be suppressed during
  // server render.
  useEffect(() => {
    setMounted(true);
  }, []);

  /**
   * Load the local provider snapshots (LM Studio, Ollama), cloud
   * providers, and custom endpoints from localStorage.
   *
   * localStorage is populated by `syncLocalStorage()` inside
   * `lib/vault.ts` on every successful `fetchVault()` / save cycle,
   * so the stored values always mirror `backend/data/models_vault.json`
   * verbatim — including the `badge` field that this component reads
   * to render per-provider icons.
   */
  const loadRealProviders = () => {
    if (typeof window === "undefined") return;

    try {
      const storedLM = JSON.parse(
        localStorage.getItem("omweb_lmstudio_vault") || "null"
      );
      const scanned = JSON.parse(
        localStorage.getItem("omweb_scanned_models") || "[]"
      );
      const baseModel = storedLM?.model || "qwen3-vl-8b-instruct";
      const combined = Array.from(
        new Set([
          baseModel,
          ...(Array.isArray(scanned) ? scanned : []),
          ...(storedLM?.savedModels && Array.isArray(storedLM.savedModels)
            ? storedLM.savedModels
            : []),
        ])
      ).filter(Boolean);

      setLocalGPUModels(combined);
      setLmStudioItem(
        storedLM || {
          baseUrl: "http://127.0.0.1:1234/v1",
          model: baseModel,
          apiKey: "",
        }
      );
    } catch (e) {}

    try {
      const storedOllama = JSON.parse(
        localStorage.getItem("omweb_ollama_vault") || "null"
      );
      if (
        storedOllama &&
        Array.isArray(storedOllama.savedModels) &&
        storedOllama.savedModels.length > 0
      ) {
        setOllamaItem(storedOllama);
      } else {
        setOllamaItem(null);
      }
    } catch (e) {
      setOllamaItem(null);
    }

    try {
      const storedCloud = JSON.parse(
        localStorage.getItem("omweb_cloud_vault") || "[]"
      );
      if (Array.isArray(storedCloud)) {
        // Exclude Ollama (managed separately above) and any provider
        // that has no configured API key — an empty-key entry is a
        // placeholder the user never completed.
        const configuredOnly = storedCloud.filter(
          (cp: any) =>
            cp.id !== "ollama" &&
            typeof cp.apiKey === "string" &&
            cp.apiKey.trim() !== ""
        );
        setActiveCloudProviders(configuredOnly);
      } else {
        setActiveCloudProviders([]);
      }
    } catch (e) {
      setActiveCloudProviders([]);
    }

    try {
      const storedCustom = JSON.parse(
        localStorage.getItem("omweb_custom_endpoints") || "[]"
      );
      if (Array.isArray(storedCustom)) {
        setActiveCustomEndpoints(storedCustom);
      } else {
        setActiveCustomEndpoints([]);
      }
    } catch (e) {
      setActiveCustomEndpoints([]);
    }
  };

  /**
   * Load Provider Packages installed via Store → Providers.
   *
   * Unlike cloud providers, package credentials live in
   * `provider_connections` and are resolved by the backend dispatcher
   * at request time — which is why `base_url` and `api_key` are sent
   * as empty strings when a package model is activated below.
   *
   * A package is exposed in the selector only when ALL hold true:
   *   1. The package is enabled in the registry.
   *   2. It has an active and enabled connection.
   *   3. It has at least one saved model (from a prior Scan Models).
   */
  const loadProviderPackages = async () => {
    if (typeof window === "undefined") return;
    try {
      const [pkgsRes, vaultRes] = await Promise.all([
        fetch("/api/store/providers").then((r) => r.json()),
        fetch("/api/config/vault").then((r) => r.json()),
      ]);

      const providers: any[] = pkgsRes.providers || [];
      const connections: any[] = vaultRes.provider_connections || [];
      const savedByPackage: Record<
        string,
        { savedModels?: string[]; model?: string }
      > = vaultRes.provider_package_models || {};

      const usable = providers
        .filter((p) => p.enabled)
        .map((p) => {
          const conn = connections.find(
            (c) =>
              c.provider_package_id === p.id && c.is_active && c.enabled
          );
          if (!conn) return null;
          const saved = savedByPackage[p.id] || {};
          const models = Array.isArray(saved.savedModels)
            ? saved.savedModels
            : [];
          if (models.length === 0) return null;
          return {
            id: p.id as string,
            displayName: (p.display_name || p.name || p.id) as string,
            // The `badge` field lives on the CONNECTION record, not
            // on the package registry entry from `/api/store/providers`.
            // `save_provider_connection()` in `config_rtr.py` writes it
            // from the manifest icon on every save, so `conn.badge` is
            // the authoritative value. `p.badge` and `p.icon` remain as
            // secondary sources for installs whose connection predates
            // badge auto-fill; `p.id` is a final fallback used only
            // when the backend failed to persist a badge (a bug being
            // tracked separately). Without this fix the icon name
            // collapses to the provider id, which the icon registry
            // may not recognise.
            iconName: (
              (conn as any)?.badge ||
              p.badge ||
              p.icon ||
              p.id
            ) as string,
            models,
          };
        })
        .filter(
          (
            item
          ): item is {
            id: string;
            displayName: string;
            iconName: string;
            models: string[];
          } => item !== null
        );

      setActiveProviderPackages(usable);
    } catch {
      setActiveProviderPackages([]);
    }
  };

  useEffect(() => {
    loadRealProviders();
    void loadProviderPackages();
    fetchVault().then(() => loadRealProviders());
    fetchServerMetadata().then((data) => setMetadataVault(data || {}));

    const onMetadataUpdate = (e: any) => setMetadataVault(e.detail || {});
    window.addEventListener("omweb:metadata-updated", onMetadataUpdate);

    const onVaultUpdate = () => {
      loadRealProviders();
      void loadProviderPackages();
    };
    window.addEventListener("omweb:vault-updated", onVaultUpdate);

    // Broadcast emitted by the Store after install / uninstall /
    // connection changes. Re-fetching here keeps the selector in sync
    // without requiring a page refresh.
    const onPackageUpdate = () => {
      void loadProviderPackages();
    };
    window.addEventListener(
      "omweb:provider-package-updated",
      onPackageUpdate
    );

    const onModelChange = (e: any) => {
      if (e.detail?.model) setActiveModel(e.detail.model);
      if (e.detail?.provider_name) setActiveProvider(e.detail.provider_name);
    };
    window.addEventListener("omweb:model-change", onModelChange);

    return () => {
      window.removeEventListener("omweb:model-change", onModelChange);
      window.removeEventListener("omweb:metadata-updated", onMetadataUpdate);
      window.removeEventListener("omweb:vault-updated", onVaultUpdate);
      window.removeEventListener(
        "omweb:provider-package-updated",
        onPackageUpdate
      );
    };
  }, [setActiveModel, setActiveProvider]);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(e.target as Node)
      ) {
        setIsOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const handleToggle = () => {
    if (!isOpen) {
      // Refresh every data source on open so the dropdown never shows
      // a stale snapshot after an out-of-band vault change.
      loadRealProviders();
      void loadProviderPackages();
      fetchVault().then(() => loadRealProviders());
      fetchServerMetadata().then((data) => setMetadataVault(data || {}));

      if (direction === "up") {
        setOpenUpward(true);
      } else if (direction === "down") {
        setOpenUpward(false);
      } else if (buttonRef.current) {
        // Auto direction: open upward when there is not enough space
        // below the trigger to render the full dropdown (~380px).
        const rect = buttonRef.current.getBoundingClientRect();
        const spaceBelow = window.innerHeight - rect.bottom;
        setOpenUpward(spaceBelow < 380);
      }
    }
    setIsOpen(!isOpen);
  };

  /**
   * Persist the selected engine and broadcast the change.
   *
   * The base URL is normalised here so downstream consumers — the
   * agent dispatcher in particular — always receive a canonical `/v1`
   * suffix regardless of what shape the provider vault stored.
   */
  const handleSelectEngine = (payload: ProviderPayload) => {
    let cleanBaseUrl = (payload.base_url || "").trim().replace(/\/+$/, "");
    if (cleanBaseUrl.endsWith("/api/v1")) {
      cleanBaseUrl = cleanBaseUrl.slice(0, -7) + "/v1";
    } else if (cleanBaseUrl.endsWith("/api")) {
      cleanBaseUrl = cleanBaseUrl.slice(0, -4) + "/v1";
    }

    const cleanPayload: ProviderPayload = {
      ...payload,
      base_url: cleanBaseUrl,
    };

    const payloadJson = JSON.stringify(cleanPayload);
    setActiveModel(cleanPayload.model);
    setActiveProvider(cleanPayload.provider_name);
    setActiveLlmOverride(payloadJson);

    storage.set("active_model", cleanPayload.model);
    storage.set("active_provider", cleanPayload.provider_name);
    storage.set("active_llm_override", payloadJson);

    if (typeof window !== "undefined") {
      localStorage.setItem("omweb_active_model", cleanPayload.model);
      localStorage.setItem("omweb_active_provider", cleanPayload.provider_name);
      localStorage.setItem("omweb_active_llm_override", payloadJson);
      window.dispatchEvent(
        new CustomEvent("omweb:model-change", { detail: cleanPayload })
      );
    }

    setIsOpen(false);
  };

  /**
   * Render the capability badges (reasoning / vision / tools) for a
   * given model key.
   *
   * Returns null when the model is unselected or has no capability
   * flags so the trigger button remains visually clean. Hydration is
   * guarded by `mounted` because the badge calculation depends on
   * `metadataVault`, which is only populated client-side.
   */
  const renderModelBadges = (modelKey: string | null | undefined) => {
    if (!mounted || !modelKey) return null;
    const caps = inferModelCapabilities(modelKey, metadataVault[modelKey]);
    if (!caps.isReasoning && !caps.isVision && !caps.isTools) return null;

    return (
      <span className="inline-flex items-center gap-1 shrink-0 ml-1 font-sans">
        {caps.isReasoning && (
          <span
            title="Reasoning / Deep Thinking"
            className="flex items-center justify-center p-0.5 rounded bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/30"
          >
            <Brain size={10} />
          </span>
        )}
        {caps.isVision && (
          <span
            title="Vision & Multimodal"
            className="flex items-center justify-center p-0.5 rounded bg-violet-500/15 text-violet-600 dark:text-violet-400 border border-violet-500/30"
          >
            <Eye size={10} />
          </span>
        )}
        {caps.isTools && (
          <span
            title="Tools & Function Calling"
            className="flex items-center justify-center p-0.5 rounded bg-blue-500/15 text-blue-600 dark:text-blue-400 border border-blue-500/30"
          >
            <Wrench size={10} />
          </span>
        )}
      </span>
    );
  };

  /**
   * Render a provider icon from the shared icon registry when a badge
   * identifier is available. Falls back to a caller-supplied lucide
   * icon so providers that predate the badge field — or whose manifest
   * does not declare one — still render something meaningful.
   *
   * The `badge` value is the canonical icon name stored on the
   * provider record. For cloud providers it originates from
   * `backend/data/models_vault.json`; for local providers it is
   * sourced from `lmstudio_vault.badge` / `ollama_vault.badge`; for
   * packages it is auto-filled by `config_rtr.save_provider_connection()`
   * from the manifest and written onto the connection record.
   *
   * Reading `badge` from the data (rather than hardcoding an icon
   * name at the call site) keeps the icon in sync with whatever the
   * vault declares — no code change is required when a provider
   * updates its badge identifier.
   */
  const renderProviderIcon = (
    badge: string | null | undefined,
    fallback: React.ReactNode
  ) => {
    if (typeof badge === "string" && badge.trim() !== "") {
      return <Icon name={badge.trim()} size={18} />;
    }
    return fallback;
  };

  // Null-safe derived labels for the trigger button.
  const displayProvider = activeProvider || "No provider";
  const displayModel = activeModel || "Not selected";

  return (
    <div className="relative inline-block text-left" ref={dropdownRef}>
      <button
        ref={buttonRef}
        type="button"
        onClick={handleToggle}
        className="inline-flex items-center gap-2 h-7 px-2.5 text-xs font-mono rounded-md border border-border bg-card hover:bg-muted text-foreground transition-all shadow-peldrun-xs cursor-pointer"
        title="Select Active AI Provider & Model"
      >
        <span className="relative flex h-2 w-2 shrink-0">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
        </span>
        <span className="font-semibold text-primary truncate max-w-[130px]">
          {displayProvider}:
        </span>
        <span className="text-foreground truncate max-w-[140px]">
          {displayModel}
        </span>
        {renderModelBadges(activeModel)}
        <ChevronDown
          size={11}
          className={`text-muted-foreground transition-transform shrink-0 ${
            isOpen ? "rotate-180" : ""
          }`}
        />
      </button>

      {isOpen && (
        <div
          className={`absolute left-0 w-96 bg-muted border border-card rounded-lg shadow-md p-2 z-50 space-y-2.5
            max-h-96 overflow-y-auto font-sans animate-in fade-in ${
            openUpward ? "bottom-full mb-1.5" : "top-full mt-1.5"
          }`}
        >
          <div className="px-2 py-1 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider border-b border-border/60 flex items-center justify-between">
            <span>Verified AI Providers & Models</span>
            <Server size={11} />
          </div>

          {/* ── LM Studio (local GPU) ───────────────────────────── */}
          <div className="rounded-md bg-card border border-card p-1.5 space-y-1 shadow-sm">
            <div className="flex items-center justify-between px-1 py-0.5 text-[11px] font-semibold text-foreground">
              <span className="flex items-center gap-1.5">
                {renderProviderIcon(
                  lmStudioItem?.badge,
                  <Icon name="lm-studio" size={18} />
                )}
                <span>LM Studio (Local GPU)</span>
              </span>
              <span className="text-[10px] text-muted-foreground font-mono">
                {localGPUModels.length}{" "}
                {localGPUModels.length === 1 ? "model" : "models"}
              </span>
            </div>

            <div className="space-y-0.5 pl-3">
              {localGPUModels.map((m) => {
                // `activeProvider` may be null; `(activeProvider || "")`
                // makes the `.includes()` guard null-safe.
                const isSelected =
                  (activeProvider || "").includes("LM Studio") &&
                  activeModel === m;
                return (
                  <button
                    key={m}
                    type="button"
                    onClick={() =>
                      handleSelectEngine({
                        model: m,
                        provider: "lmstudio",
                        provider_name: "LM Studio (Local)",
                        base_url:
                          lmStudioItem?.baseUrl ||
                          "http://127.0.0.1:1234/v1",
                        api_key: lmStudioItem?.apiKey || "",
                        api_type: "",
                      })
                    }
                    className={`w-full flex items-center justify-between px-2 py-1 rounded text-xs text-left transition-colors cursor-pointer ${
                      isSelected
                        ? "bg-primary/10 text-primary font-medium border border-primary/30"
                        : "hover:bg-muted text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    <div className="flex items-center gap-1.5 truncate flex-1 min-w-0 mr-1.5">
                      <span className="font-mono truncate">{m}</span>
                      {renderModelBadges(m)}
                    </div>
                    {isSelected && (
                      <Check size={12} className="text-primary shrink-0 ml-1" />
                    )}
                  </button>
                );
              })}
            </div>
          </div>

          {/* ── Ollama (local server) ───────────────────────────── */}
          {ollamaItem &&
            ollamaItem.savedModels &&
            ollamaItem.savedModels.length > 0 && (
              <div className="rounded-md bg-card border border-card p-1.5 space-y-1">
                <div className="flex items-center justify-between px-1 py-0.5 text-[11px] font-semibold text-foreground">
                  <span className="flex items-center gap-1.5">
                    {renderProviderIcon(
                      ollamaItem?.badge,
                      <Icon name="ollama" size={18} />
                    )}
                    <span>Ollama (Local Server)</span>
                  </span>
                  <span className="text-[10px] text-muted-foreground font-mono">
                    {ollamaItem.savedModels.length}{" "}
                    {ollamaItem.savedModels.length === 1 ? "model" : "models"}
                  </span>
                </div>

                <div className="space-y-0.5 pl-3">
                  {ollamaItem.savedModels.map((m: string) => {
                    const isSelected =
                      (activeProvider || "").includes("Ollama") &&
                      activeModel === m;
                    const bUrl = ollamaItem.baseUrl.endsWith("/v1")
                      ? ollamaItem.baseUrl
                      : `${ollamaItem.baseUrl}/v1`;
                    return (
                      <button
                        key={m}
                        type="button"
                        onClick={() =>
                          handleSelectEngine({
                            model: m,
                            provider: "ollama",
                            provider_name: "Ollama (Local)",
                            base_url: bUrl,
                            api_key: "",
                            api_type: "",
                          })
                        }
                        className={`w-full flex items-center justify-between px-2 py-1 rounded text-xs text-left transition-colors cursor-pointer ${
                          isSelected
                            ? "bg-primary/10 text-primary font-medium border border-primary/30"
                            : "hover:bg-muted text-muted-foreground hover:text-foreground"
                        }`}
                      >
                        <div className="flex items-center gap-1.5 truncate flex-1 min-w-0 mr-1.5">
                          <span className="font-mono truncate">{m}</span>
                          {renderModelBadges(m)}
                        </div>
                        {isSelected && (
                          <Check
                            size={12}
                            className="text-primary shrink-0 ml-1"
                          />
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

          {/* ── Cloud providers (badge-driven icons) ─────────────── */}
          {activeCloudProviders.length > 0 && (
            <div className="pt-1 border-t border-border/40 space-y-1.5">
              <span className="text-[9px] font-bold text-muted-foreground uppercase px-2 block mb-1">
                Cloud Providers & Saved Models
              </span>

              {activeCloudProviders.map((cp) => {
                const modelsList =
                  cp.savedModels && cp.savedModels.length > 0
                    ? cp.savedModels
                    : [cp.model];
                return (
                  <div
                    key={cp.id}
                    className="rounded-md bg-card border border-card p-1.5 space-y-1"
                  >
                    <div className="flex items-center justify-between px-1 py-0.5 text-[11px] font-semibold text-foreground">
                      <span className="flex items-center gap-1.5">
                        {renderProviderIcon(
                          cp.badge,
                          <Cloud size={18} className="text-sky-500" />
                        )}
                        <span>{cp.name}</span>
                      </span>
                      <span className="text-[10px] text-muted-foreground font-mono">
                        {modelsList.length}{" "}
                        {modelsList.length === 1 ? "model" : "models"}
                      </span>
                    </div>

                    <div className="space-y-0.5 pl-3">
                      {modelsList.map((m: string) => {
                        const isSelected =
                          activeProvider === cp.name && activeModel === m;
                        return (
                          <button
                            key={m}
                            type="button"
                            onClick={() =>
                              handleSelectEngine({
                                model: m,
                                provider: cp.id,
                                provider_name: cp.name,
                                base_url: cp.baseUrl,
                                api_key: cp.apiKey,
                                api_type: cp.type || "",
                              })
                            }
                            className={`w-full flex items-center justify-between px-2 py-1 rounded text-xs text-left transition-colors cursor-pointer ${
                              isSelected
                                ? "bg-primary/10 text-primary font-medium border border-primary/30"
                                : "hover:bg-muted text-muted-foreground hover:text-foreground"
                            }`}
                          >
                            <div className="flex items-center gap-1.5 truncate flex-1 min-w-0 mr-1.5">
                              <span className="font-mono truncate">{m}</span>
                              {renderModelBadges(m)}
                            </div>
                            {isSelected && (
                              <Check
                                size={12}
                                className="text-primary shrink-0 ml-1"
                              />
                            )}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* ── Custom endpoints ─────────────────────────────────── */}
          {activeCustomEndpoints.length > 0 && (
            <div className="pt-1 border-t border-border/40 space-y-1.5">
              <span className="text-[9px] font-bold text-muted-foreground uppercase px-2 block mb-1">
                Custom Endpoints
              </span>

              {activeCustomEndpoints.map((ce) => {
                const modelsList =
                  ce.savedModels && ce.savedModels.length > 0
                    ? ce.savedModels
                    : [ce.defaultModel];
                return (
                  <div
                    key={ce.id}
                    className="rounded-md bg-card border border-card p-1.5 space-y-1"
                  >
                    <div className="flex items-center justify-between px-1 py-0.5 text-[11px] font-semibold text-foreground">
                      <span className="flex items-center gap-1.5">
                        {renderProviderIcon(
                          ce.badge,
                          <Terminal size={12} className="text-amber-500" />
                        )}
                        <span>{ce.name}</span>
                      </span>
                      <span className="text-[10px] text-muted-foreground font-mono">
                        {modelsList.length}{" "}
                        {modelsList.length === 1 ? "model" : "models"}
                      </span>
                    </div>

                    <div className="space-y-0.5 pl-3">
                      {modelsList.map((m: string) => {
                        const isSelected =
                          activeProvider === ce.name && activeModel === m;
                        return (
                          <button
                            key={m}
                            type="button"
                            onClick={() =>
                              handleSelectEngine({
                                model: m,
                                provider: ce.providerId,
                                provider_name: ce.name,
                                base_url: ce.endpointUrl,
                                api_key: ce.apiKey || "",
                                api_type: "",
                              })
                            }
                            className={`w-full flex items-center justify-between px-2 py-1 rounded text-xs text-left transition-colors cursor-pointer ${
                              isSelected
                                ? "bg-primary/10 text-primary font-medium border border-primary/30"
                                : "hover:bg-muted text-muted-foreground hover:text-foreground"
                            }`}
                          >
                            <div className="flex items-center gap-1.5 truncate flex-1 min-w-0 mr-1.5">
                              <span className="font-mono truncate">{m}</span>
                              {renderModelBadges(m)}
                            </div>
                            {isSelected && (
                              <Check
                                size={12}
                                className="text-primary shrink-0 ml-1"
                              />
                            )}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* ── Store provider packages ──────────────────────────── */}
          {activeProviderPackages.length > 0 && (
            <div className="pt-1 border-t border-border/40 space-y-1.5">
              <span className="text-[9px] font-bold text-muted-foreground uppercase px-2 block mb-1">
                Store Provider Packages
              </span>

              {activeProviderPackages.map((pkg) => (
                <div
                  key={pkg.id}
                  className="rounded-md bg-card border border-card p-1.5 space-y-1"
                >
                  <div className="flex items-center justify-between px-1 py-0.5 text-[11px] font-semibold text-foreground">
                    <span className="flex items-center gap-1.5">
                      {renderProviderIcon(
                        pkg.iconName,
                        <Package size={12} className="text-purple-500" />
                      )}
                      <span>{pkg.displayName}</span>
                    </span>
                    <span className="text-[10px] text-muted-foreground font-mono">
                      {pkg.models.length}{" "}
                      {pkg.models.length === 1 ? "model" : "models"}
                    </span>
                  </div>

                  <div className="space-y-0.5 pl-3">
                    {pkg.models.map((m) => {
                      const isSelected =
                        activeProvider === pkg.displayName &&
                        activeModel === m;
                      return (
                        <button
                          key={m}
                          type="button"
                          onClick={() =>
                            handleSelectEngine({
                              model: m,
                              provider: pkg.id,
                              provider_name: pkg.displayName,
                              // Intentionally empty: the backend
                              // dispatcher resolves base_url and
                              // api_key from `provider_connections`
                              // at request time.
                              base_url: "",
                              api_key: "",
                              api_type: "",
                            })
                          }
                          className={`w-full flex items-center justify-between px-2 py-1 rounded text-xs text-left transition-colors cursor-pointer ${
                            isSelected
                              ? "bg-primary/10 text-primary font-medium border border-primary/30"
                              : "hover:bg-muted text-muted-foreground hover:text-foreground"
                          }`}
                        >
                          <div className="flex items-center gap-1.5 truncate flex-1 min-w-0 mr-1.5">
                            <span className="font-mono truncate">{m}</span>
                            {renderModelBadges(m)}
                          </div>
                          {isSelected && (
                            <Check
                              size={12}
                              className="text-primary shrink-0 ml-1"
                            />
                          )}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default EngineSelector;
