// frontend/src/app/settings/setup/tabs/SearchTab.tsx
"use client";

import React, { useState, useMemo, useEffect, useRef, useCallback } from "react";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import {
  Search, ExternalLink, Activity, Eye, EyeOff, CheckCircle2, XCircle,
  RefreshCw, Shield, ShieldOff, Sparkles, Gauge, Network, Sliders,
  ChevronRight, ChevronDown, Save, Loader2, AlertCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";

// ---------------------------------------------------------------------------
// Brand icons (unchanged from your version — keep as-is)
// ---------------------------------------------------------------------------
const BrandIcons = {
  DuckDuckGo: (props: React.SVGProps<SVGSVGElement>) => (
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" {...props}>
      <title>DuckDuckGo</title>
      <path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm0 23C5.925 23 1 18.074 1 12S5.926 1 12 1s11 4.925 11 11-4.925 11-11 11zm10.219-11c0 4.805-3.317 8.833-7.786 9.925-.27-.521-.53-1.017-.749-1.438.645.249 1.93.718 2.208.615.376-.144.282-3.149-.14-3.245-.338-.075-1.632.837-2.141 1.209l.034.156c.078.397.144.993.03 1.247-.001.004-.002.01-.004.013a.218.218 0 0 1-.068.088c-.284.188-1.081.284-1.503.188a.516.516 0 0 1-.064-.02c-.694.396-2.01 1.109-2.25.971-.329-.188-.377-2.676-.329-3.288.035-.46 1.653.286 2.442.679.174-.163.602-.272.98-.31-.57-1.389-.99-2.977-.733-4.105 0 .002.002.002.002.002.356.248 2.73 1.05 3.91 1.027 1.18-.024 3.114-.743 2.903-1.323-.212-.58-2.135.51-4.142.324-1.486-.138-1.748-.804-1.42-1.29.414-.611 1.168.116 2.411-.256 1.245-.371 2.987-1.035 3.632-1.397 1.494-.833-.625-1.177-1.125-.947-.474.22-2.123.637-2.889.82.428-1.516-.603-4.149-1.757-5.3-.376-.376-.951-.612-1.603-.736-.25-.344-.654-.671-1.225-.977a5.772 5.772 0 0 0-3.595-.584l-.024.004-.034.004.004.002c-.148.028-.237.08-.357.098.148.016.705.276 1.057.418-.174.068-.412.108-.596.184a.828.828 0 0 0-.204.056c-.173.08-.303.375-.3.515.84-.086 2.082-.026 2.991.246-.644.09-1.235.258-1.661.482-.016.008-.03.018-.048.028-.054.02-.106.042-.152.066-1.367.72-1.971 2.405-1.611 4.424.323 1.824 1.665 8.088 2.29 11.064-3.973-1.4-6.822-5.186-6.822-9.639C1.781 6.356 6.356 1.781 12 1.781S22.219 6.356 22.219 12zM9.095 9.581a.758.758 0 1 0 0 1.516.758.758 0 0 0 0-1.516zm.338.702a.196.196 0 1 1 0-.392.196.196 0 0 1 0 .392zm4.724-1.043a.65.65 0 1 0 0 1.299.65.65 0 0 0 0-1.3zm.29.601a.168.168 0 1 1 0-.336.168.168 0 0 1 0 .336zM9.313 8.146s-.571-.26-1.125.09c-.554.348-.534.704-.534.704s-.294-.656.49-.978c.786-.32 1.17.184 1.17.184zm5.236-.052s-.41-.234-.73-.23c-.654.008-.831.296-.831.296s.11-.688.945-.55a.84.84 0 0 1 .616.484z" fill="currentColor" />
    </svg>
  ),
  SearXNG: (props: React.SVGProps<SVGSVGElement>) => (
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" {...props}>
      <title>SearXNG</title>
      <path d="M13.716 17.261l6.873 6.582L24 20.282l-6.824-6.536a9.11 9.11 0 0 0 1.143-4.43c0-5.055-4.105-9.159-9.16-9.159S0 4.261 0 9.316c0 5.055 4.104 9.159 9.159 9.159a9.11 9.11 0 0 0 4.557-1.214zM9.159 2.773a6.546 6.546 0 0 1 6.543 6.543 6.545 6.545 0 0 1-6.543 6.542 6.545 6.545 0 0 1-6.542-6.542 6.545 6.545 0 0 1 6.542-6.543zM7.26 5.713a4.065 4.065 0 0 1 4.744.747 4.064 4.064 0 0 1 .707 4.749l1.157.611a5.376 5.376 0 0 0-.935-6.282 5.377 5.377 0 0 0-6.274-.987l.601 1.162z" fill="currentColor" />
    </svg>
  ),
  Tavily: (props: React.SVGProps<SVGSVGElement>) => (
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" {...props}>
      <title>Tavily</title>
      <path d="M9.1.503l2.824 4.47a1.078 1.078 0 01-.911 1.655H9.858v6.692h-1.67V0c.35 0 .7.168.912.503z" fill="#8FBCFA" />
      <path d="M4.453 4.974L7.277.503A1.07 1.07 0 018.189 0v13.32a2.633 2.633 0 00-1.67.48V6.628H5.364c-.85 0-1.366-.936-.912-1.654z" fill="#468BFF" />
      <path d="M17.041 17.74h-7.028c.423-.457.67-1.049.7-1.67h12.956c0 .35-.168.7-.502.912l-4.472 2.823a1.078 1.078 0 01-1.654-.911v-1.155z" fill="#FDBB11" />
      <path d="M18.695 12.334l4.47 2.824c.336.212.503.562.503.912H10.713a2.65 2.65 0 00-.493-1.67h6.822v-1.154c0-.85.935-1.366 1.653-.912z" fill="#F6D785" />
      <path d="M4.394 19.605L.316 23.683a1.07 1.07 0 001 .29l5.158-1.165A1.078 1.078 0 007 20.994l-.816-.816 3.073-3.074a1.61 1.61 0 000-2.276l-.042-.043-4.82 4.82z" fill="#FF9A9D" />
      <path d="M3.822 17.817l3.073-3.074a1.61 1.61 0 012.277 0l.042.043-4.818 4.819-4.08 4.079a1.07 1.07 0 01-.289-1l1.165-5.158A1.078 1.078 0 013.006 17l.816.817z" fill="#FE363B" />
    </svg>
  ),
  Brave: (props: React.SVGProps<SVGSVGElement>) => (
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" {...props}>
      <title>Brave</title>
      <path d="M15.68 0l2.096 2.38s1.84-.512 2.709.358c.868.87 1.584 1.638 1.584 1.638l-.562 1.381.715 2.047s-2.104 7.98-2.35 8.955c-.486 1.919-.818 2.66-2.198 3.633-1.38.972-3.884 2.66-4.293 2.916-.409.256-.92.692-1.38.692-.46 0-.97-.436-1.38-.692a185.796 185.796 0 01-4.293-2.916c-1.38-.973-1.712-1.714-2.197-3.633-.247-.975-2.351-8.955-2.351-8.955l.715-2.047-.562-1.381s.716-.768 1.585-1.638c.868-.87 2.708-.358 2.708-.358L8.321 0h7.36zm-3.679 14.936c-.14 0-1.038.317-1.758.69-.72.373-1.242.637-1.409.742-.167.104-.065.301.087.409.152.107 2.194 1.69 2.393 1.866.198.175.489.464.687.464.198 0 .49-.29.688-.464.198-.175 2.24-1.759 2.392-1.866.152-.108.254-.305.087-.41-.167-.104-.689-.368-1.41-.741-.72-.373-1.617-.69-1.757-.69zm0-11.278s-.409.001-1.022.206-1.278.46-1.584.46c-.307 0-2.581-.434-2.581-.434S4.119 7.152 4.119 7.849c0 .697.339.881.68 1.243l2.02 2.149c.192.203.59.511.356 1.066-.235.555-.58 1.26-.196 1.977.384.716 1.042 1.194 1.464 1.115.421-.08 1.412-.598 1.776-.834.364-.237 1.518-1.19 1.518-1.554 0-.365-1.193-1.02-1.413-1.168-.22-.15-1.226-.725-1.247-.95-.02-.227-.012-.293.284-.851.297-.559.831-1.304.742-1.8-.089-.495-.95-.753-1.565-.986-.615-.232-1.799-.671-1.947-.74-.148-.068-.11-.133.339-.175.448-.043 1.719-.212 2.292-.052.573.16 1.552.403 1.632.532.079.13.149.134.067.579-.081.445-.5 2.581-.541 2.96-.04.38-.12.63.288.724.409.094 1.097.256 1.333.256s.924-.162 1.333-.256c.408-.093.329-.344.288-.723-.04-.38-.46-2.516-.541-2.961-.082-.445-.012-.45.067-.579.08-.129 1.059-.372 1.632-.532.573-.16 1.845.009 2.292.052.449.042.487.107.339.175-.148.069-1.332.508-1.947.74-.615.233-1.476.49-1.565.986-.09.496.445 1.241.742 1.8.297.558.304.624.284.85-.02.226-1.026.802-1.247.95-.22.15-1.413.804-1.413 1.169 0 .364 1.154 1.317 1.518 1.554.364.236 1.355.755 1.776.834.422.079 1.08-.4 1.464-1.115.384-.716.039-1.422-.195-1.977-.235-.555.163-.863.355-1.066l2.02-2.149c.341-.362.68-.546.68-1.243 0-.697-2.695-3.96-2.695-3.96s-2.274.436-2.58.436c-.307 0-.972-.256-1.585-.461-.613-.205-1.022-.206-1.022-.206z" fill="currentColor" />
    </svg>
  ),
  Google: (props: React.SVGProps<SVGSVGElement>) => (
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" {...props}>
      <title>Google</title>
      <path d="M11.7 5.735H7.55a.55.55 0 0 0-.55.56v1.52a.55.55 0 0 0 .55.55h2.857a2.963 2.963 0 0 1-3.234 3.006c-2.416 0-3.58-2.048-3.58-4.375 0-2.328 1.25-4.375 3.58-4.375a4.39 4.39 0 0 1 2.846.926.528.528 0 0 0 .776-.237l.68-1.551a.55.55 0 0 0-.162-.68A7.626 7.626 0 0 0 7.216.003C3.29-.008.617 3.041.617 6.996.617 10.95 3.302 14 7.173 14c3.87 0 6.21-2.759 6.21-6.465 0-1.186-.474-1.8-1.682-1.8z" fill="currentColor" />
    </svg>
  ),
  Jina: (props: React.SVGProps<SVGSVGElement>) => (
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" {...props}>
      <title>Jina AI</title>
      <path d="M6.608 21.416a4.608 4.608 0 100-9.217 4.608 4.608 0 000 9.217zM20.894 2.015c.614 0 1.106.492 1.106 1.106v9.002c0 5.13-4.148 9.309-9.217 9.37v-9.355l-.03-9.032c0-.614.491-1.106 1.106-1.106h7.158l-.123.015z" fill="currentColor" />
    </svg>
  ),
  Yandex: (props: React.SVGProps<SVGSVGElement>) => (
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" {...props}>
      <title>Yandex</title>
      <path d="M16.376 12.644L21 2h-3.842l-4.624 10.644h3.842zM13.915 24v-3.733c0-2.822-.352-3.64-1.407-5.988L6.933 2H3l7.124 15.709V24h3.79z" fill="currentColor" />
    </svg>
  ),
} as const;

// ---------------------------------------------------------------------------
// Provider registry
// ---------------------------------------------------------------------------
type ProviderField =
  | { key: string; label: string; type: "text"; placeholder?: string; mono?: boolean }
  | { key: string; label: string; type: "password"; placeholder?: string; secretKey?: string };

interface ProviderDef {
  id: string;
  configKey: string;
  name: string;
  tagline: string;
  iconKey: keyof typeof BrandIcons;
  accent: string;
  fields: ProviderField[];
  costField: boolean;
  testLabel: string;
  docsUrl?: string;
}

const PROVIDERS: readonly ProviderDef[] = [
  { id: "DuckDuckGo", configKey: "duckduckgo", name: "DuckDuckGo", tagline: "Zero-credential fallback engine with privacy-first results", iconKey: "DuckDuckGo", accent: "#DE5833", fields: [], costField: true, testLabel: "Test Reachability", docsUrl: "https://duckduckgo.com" },
  { id: "SearXNG", configKey: "searxng", name: "SearXNG", tagline: "Self-hosted meta-search aggregator", iconKey: "SearXNG", accent: "#3050FF", fields: [{ key: "base_url", label: "Base URL", type: "text", placeholder: "http://localhost:8080", mono: true }], costField: true, testLabel: "Test Reachability", docsUrl: "https://searxng.org" },
  { id: "Tavily", configKey: "tavily", name: "Tavily AI", tagline: "LLM-optimized cloud search API", iconKey: "Tavily", accent: "#E85E32", fields: [{ key: "api_key", label: "API Key", type: "password", placeholder: "tvly-••••••", secretKey: "tavily" }], costField: true, testLabel: "Verify API Key", docsUrl: "https://tavily.com" },
  { id: "Brave", configKey: "brave", name: "Brave Search", tagline: "Independent web index with privacy focus", iconKey: "Brave", accent: "#FB542B", fields: [{ key: "api_key", label: "Subscription Token", type: "password", placeholder: "BSA••••••", secretKey: "brave" }], costField: true, testLabel: "Verify Token", docsUrl: "https://brave.com/search/api/" },
  { id: "Google", configKey: "google", name: "Google CSE", tagline: "Programmable Search Engine (custom sites)", iconKey: "Google", accent: "#4285F4", fields: [{ key: "api_key", label: "API Key", type: "password", placeholder: "AIza••••", secretKey: "google" }, { key: "cx", label: "CX ID", type: "text", placeholder: "0123...", mono: true }], costField: true, testLabel: "Verify CSE", docsUrl: "https://developers.google.com/custom-search" },
  { id: "Jina", configKey: "jina", name: "Jina Reader", tagline: "Markdown-first reader & search endpoint", iconKey: "Jina", accent: "#8B5CF6", fields: [{ key: "api_key", label: "Bearer Token", type: "password", placeholder: "jina_••••", secretKey: "jina" }], costField: true, testLabel: "Test Endpoint", docsUrl: "https://jina.ai" },
  { id: "Yandex", configKey: "yandex", name: "Yandex Search", tagline: "Russian + global index with XML / Cloud v2", iconKey: "Yandex", accent: "#FC3F1D", fields: [{ key: "api_key", label: "API Key", type: "password", placeholder: "AQVN••••", secretKey: "yandex" }, { key: "folder_id", label: "Folder ID", type: "text", placeholder: "b1g••••", mono: true }], costField: true, testLabel: "Verify Key", docsUrl: "https://yandex.com/dev/searchapi/" },
] as const;

const ENGINE_OPTIONS = PROVIDERS.map((p) => p.id);

const resolveApiUrl = (endpoint: string): string => {
  if (typeof window !== "undefined") {
    const port = window.location.port;
    if (port === "3088" || port === "3000") {
      return `http://localhost:8088${endpoint}`;
    }
  }
  return endpoint;
};

const FADE_UP = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

// ---------------------------------------------------------------------------
// Save-state types
// ---------------------------------------------------------------------------
type SaveState = "idle" | "dirty" | "saving" | "saved" | "error";

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
  const [pings, setPings] = useState<
    Record<string, { testing: boolean; ok?: boolean; latency_ms?: number; msg?: string }>
  >({});


  // ── Load authoritative config from backend on mount ────────────
const [configLoaded, setConfigLoaded] = useState(false);

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

      const authoritative = data.search_config || {};
      // Push authoritative config to parent so the rest of the app sees it
      setConfig((prev: any) => ({
        ...prev,
        search: { ...(prev.search || {}), ...authoritative },
      }));

      // Mark as loaded — auto-save can now start
      setConfigLoaded(true);
      // Prime the snapshot so the first effect run doesn't trigger a save
      lastSnapshotRef.current = JSON.stringify(authoritative);
    } catch (err) {
      console.error("Failed to load search config:", err);
      setConfigLoaded(true); // still allow editing
    }
  })();

  return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
}, []);

  // ── Auto-save state ────────────────────────────────────────────────
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [lastSavedAt, setLastSavedAt] = useState<Date | null>(null);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isFirstRender = useRef(true);
  const lastSnapshotRef = useRef<string>("");

  // ----------------------------------------------------------
  // Persist to backend (dedicated endpoint)
  // ----------------------------------------------------------
  const persistSearchConfig = useCallback(
    async (nextSearchConfig: any) => {
      setSaveState("saving");
      try {
        const res = await fetch(resolveApiUrl("/api/search/config"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ search_config: nextSearchConfig }),
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        setSaveState("saved");
        setLastSavedAt(new Date());
        // Reset to idle after a moment so the badge fades back
        setTimeout(() => setSaveState((s) => (s === "saved" ? "idle" : s)), 2500);
      } catch (err) {
        console.error("Failed to save search config:", err);
        setSaveState("error");
      }
    },
    []
  );

  // ----------------------------------------------------------
  // Debounced auto-save on any search config change
  // ----------------------------------------------------------
  useEffect(() => {
    // Serialize to detect real changes
    const snapshot = JSON.stringify(searchConfig);

    if (isFirstRender.current) {
      isFirstRender.current = false;
      lastSnapshotRef.current = snapshot;
      return;
    }

    if (snapshot === lastSnapshotRef.current) return;

    lastSnapshotRef.current = snapshot;
    setSaveState("dirty");

    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      persistSearchConfig(searchConfig);
    }, 800);

    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchConfig]);

  // ----------------------------------------------------------
  // Manual save (bypass debounce)
  // ----------------------------------------------------------
  const handleManualSave = async () => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    await persistSearchConfig(searchConfig);
  };

  // ----------------------------------------------------------
  // Update helpers (still update parent state for consistency)
  // ----------------------------------------------------------
  const updateSearch = (partial: any) => {
    setConfig({
      ...config,
      search: { ...searchConfig, ...partial },
    });
  };

  const updateEngineSubConfig = (engineKey: string, field: string, value: any) => {
    const currentSub = searchConfig[engineKey] || {};
    updateSearch({ [engineKey]: { ...currentSub, [field]: value } });
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

  const handleTestConnection = async (engineId: string) => {
    setPings((prev) => ({ ...prev, [engineId]: { testing: true } }));
    try {
      const res = await fetch(resolveApiUrl("/api/search/test-connection"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: engineId, config: searchConfig }),
      });
      const data = await res.json();
      setPings((prev) => ({
        ...prev,
        [engineId]: { testing: false, ok: data.ok, latency_ms: data.latency_ms, msg: data.message },
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
  const primaryEngine = searchConfig.engine || "DuckDuckGo";
  const fallbackList: string[] = Array.isArray(searchConfig.fallback_engines)
    ? searchConfig.fallback_engines
    : [];

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
            <div className="p-2 rounded-lg bg-primary/10 border border-primary/20">
              <Search size={16} className="text-primary" />
            </div>
            <div>
              <h1 className="text-sm font-semibold font-heading tracking-wide">
                Search Engine Orchestration
              </h1>
              <p className="text-[10px] text-muted-foreground">
                Providers · credentials · pricing · fallback chain
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <SaveIndicator state={saveState} lastSavedAt={lastSavedAt} />
            <Button
              variant="outline"
              onClick={handleManualSave}
              disabled={saveState === "saving"}
              className="h-8 text-xs gap-1.5 cursor-pointer hover:scale-105 transition-transform"
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
                className="h-8 text-xs gap-1.5 border-primary/40 bg-primary/10 hover:bg-primary/20 text-primary cursor-pointer hover:scale-105 transition-transform"
              >
                <Activity size={12} />
                <span className="hidden sm:inline">Telemetry</span>
                <ExternalLink size={11} />
              </Button>
            </Link>
          </div>
        </div>
      </motion.header>

      <main className="max-w-7xl w-full mx-auto p-6 space-y-6 flex-1 relative">
        {/* Summary KPIs */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <SummaryCard
            label="Web Egress"
            value={isWebEgressEnabled ? "Enabled" : "Air-Gapped"}
            icon={isWebEgressEnabled ? <Shield size={18} className="text-emerald-500" /> : <ShieldOff size={18} className="text-rose-500" />}
            accent={isWebEgressEnabled ? "#10b981" : "#f43f5e"}
            subtitle={isWebEgressEnabled ? "Outbound requests permitted" : "All network calls blocked"}
            delay={0}
          />
          <SummaryCard label="Active Providers" value={`${enabledCount} / ${PROVIDERS.length}`} icon={<Network size={18} className="text-primary" />} accent="#3b82f6" subtitle={`${PROVIDERS.length - enabledCount} disabled`} delay={0.05} />
          <SummaryCard label="Primary Engine" value={primaryEngine} icon={<Sparkles size={18} className="text-amber-500" />} accent="#f59e0b" subtitle="First engine queried" delay={0.1} />
          <SummaryCard
            label="Fallback Chain"
            value={fallbackList.length.toString()}
            icon={<Gauge size={18} className="text-cyan-500" />}
            accent="#06b6d4"
            subtitle={fallbackList.length === 0 ? "No fallbacks configured" : fallbackList.join(" → ")}
            delay={0.15}
          />
        </div>

        {/* Warning if DDG is not in fallback chain */}
        {!fallbackList.includes("DuckDuckGo") && primaryEngine !== "DuckDuckGo" && (
          <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} className="overflow-hidden">
            <div className="flex items-start gap-3 p-4 rounded-xl border border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400 text-xs">
              <AlertCircle size={16} className="shrink-0 mt-0.5" />
              <div className="space-y-1">
                <div className="font-semibold">Recommendation: Add DuckDuckGo to the fallback chain</div>
                <div className="opacity-90">
                  DuckDuckGo is the only provider that works without API keys. Without it,
                  all searches fail if every other provider lacks credentials.
                </div>
              </div>
            </div>
          </motion.div>
        )}

        {/* Global Orchestration */}
        <motion.div {...FADE_UP} transition={{ ...FADE_UP.transition, delay: 0.2 }} className="relative p-5 rounded-xl border border-border bg-card shadow-sm space-y-5 overflow-hidden">
          <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-primary/60 to-transparent" />

          <div className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-primary/10 border border-primary/20">
                <Sliders size={16} className="text-primary" />
              </div>
              <div>
                <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">Global Orchestration</h3>
                <p className="text-[11px] text-muted-foreground">Primary engine, fallback sequence, and locale defaults</p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => updateSearch({ enabled: searchConfig.enabled === false ? true : false })}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-[11px] font-semibold transition-all cursor-pointer border ${
                isWebEgressEnabled
                  ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-600 dark:text-emerald-400"
                  : "bg-rose-500/10 border-rose-500/40 text-rose-600 dark:text-rose-400"
              }`}
            >
              <span className="relative flex h-2 w-2">
                {isWebEgressEnabled && (<span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />)}
                <span className={`relative inline-flex rounded-full h-2 w-2 ${isWebEgressEnabled ? "bg-emerald-500" : "bg-rose-500"}`} />
              </span>
              {isWebEgressEnabled ? "Enabled" : "Air-Gapped"}
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-2">Primary Search Engine</label>
              <div className="relative">
                <select
                  value={primaryEngine}
                  onChange={(e) => updateSearch({ engine: e.target.value })}
                  className="w-full bg-background border border-border rounded-lg px-3 py-2 text-xs text-foreground font-medium focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow appearance-none cursor-pointer pr-9"
                >
                  {ENGINE_OPTIONS.map((e) => (<option key={e} value={e}>{e}</option>))}
                </select>
                <ChevronRight size={14} className="absolute right-3 top-1/2 -translate-y-1/2 rotate-90 text-muted-foreground pointer-events-none" />
              </div>
            </div>

            <div>
              <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-2">Fallback Chain</label>
              <div className="flex flex-wrap gap-1.5">
                {ENGINE_OPTIONS.map((eng) => {
                  const isSelected = fallbackList.includes(eng);
                  return (
                    <motion.button
                      key={eng}
                      type="button"
                      whileHover={{ y: -2 }}
                      onClick={() => toggleFallback(eng)}
                      className={`px-2.5 py-1 rounded-lg text-[11px] font-medium cursor-pointer transition-all border ${
                        isSelected
                          ? "bg-primary text-primary-foreground border-primary shadow-md"
                          : "bg-background text-muted-foreground border-border hover:border-primary/40 hover:text-foreground"
                      }`}
                    >
                      {eng}
                    </motion.button>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 pt-4 border-t border-border/60">
            <CompactField label="Retry Delay (s)" value={searchConfig.retry_delay ?? 5} onChange={(v) => updateSearch({ retry_delay: v })} />
            <CompactField label="Max Retries" value={searchConfig.max_retries ?? 3} onChange={(v) => updateSearch({ max_retries: v })} />
            <CompactTextField label="Language" value={searchConfig.lang || "en"} onChange={(v) => updateSearch({ lang: v })} placeholder="en / ar" />
            <CompactTextField label="Country" value={searchConfig.country || "us"} onChange={(v) => updateSearch({ country: v })} placeholder="us / eg" />
          </div>
        </motion.div>

        {/* Provider section header */}
        <motion.div {...FADE_UP} transition={{ ...FADE_UP.transition, delay: 0.3 }} className="flex items-center justify-between flex-wrap gap-3 pt-2">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
              <Network size={16} className="text-emerald-500" />
            </div>
            <div>
              <h3 className="text-xs font-semibold font-heading uppercase tracking-wider">Provider Configuration</h3>
              <p className="text-[11px] text-muted-foreground">{enabledCount} of {PROVIDERS.length} enabled · click any row to expand</p>
            </div>
          </div>
        </motion.div>

        {/* Provider list */}
        <div className="rounded-xl border border-border bg-card shadow-sm overflow-hidden divide-y divide-border/40">
          {PROVIDERS.map((provider, idx) => (
            <ProviderListRow
              key={provider.id}
              provider={provider}
              enabled={isEngineEnabled(provider.configKey)}
              expanded={expandedProvider === provider.id}
              onToggleExpand={() => setExpandedProvider(expandedProvider === provider.id ? null : provider.id)}
              config={searchConfig}
              ping={pings[provider.id]}
              showKeys={showKeys}
              onToggleEnabled={() => updateEngineSubConfig(provider.configKey, "enabled", !isEngineEnabled(provider.configKey))}
              onUpdateField={(field, value) => updateEngineSubConfig(provider.configKey, field, value)}
              onToggleShowKey={toggleShowKey}
              onTest={() => handleTestConnection(provider.id)}
              delay={0.35 + idx * 0.04}
            />
          ))}
        </div>
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Save Indicator
// ---------------------------------------------------------------------------
function SaveIndicator({ state, lastSavedAt }: { state: SaveState; lastSavedAt: Date | null }) {
  const map: Record<SaveState, { label: string; cls: string; icon: React.ReactNode }> = {
    idle: { label: lastSavedAt ? `Saved ${lastSavedAt.toLocaleTimeString()}` : "Auto-save on", cls: "text-muted-foreground border-border/60 bg-background/60", icon: <CheckCircle2 size={11} /> },
    dirty: { label: "Unsaved changes", cls: "text-amber-600 dark:text-amber-400 border-amber-500/40 bg-amber-500/10", icon: <AlertCircle size={11} /> },
    saving: { label: "Saving...", cls: "text-primary border-primary/40 bg-primary/10", icon: <Loader2 size={11} className="animate-spin" /> },
    saved: { label: "All changes saved", cls: "text-emerald-600 dark:text-emerald-400 border-emerald-500/40 bg-emerald-500/10", icon: <CheckCircle2 size={11} /> },
    error: { label: "Save failed — click Save", cls: "text-rose-600 dark:text-rose-400 border-rose-500/40 bg-rose-500/10", icon: <XCircle size={11} /> },
  };
  const cfg = map[state];
  return (
    <motion.span
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      className={`hidden sm:flex items-center gap-1.5 text-[10px] font-mono px-2 py-1 rounded-md border ${cfg.cls}`}
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
      className="group relative p-5 rounded-xl border border-border bg-card overflow-hidden shadow-sm hover:shadow-lg hover:border-primary/30 transition-all"
    >
      <div className="absolute top-0 left-0 right-0 h-[2px] opacity-60 group-hover:opacity-100 transition-opacity" style={{ background: `linear-gradient(90deg, transparent 0%, ${accent} 50%, transparent 100%)` }} />
      <div className="absolute -top-10 -right-10 w-32 h-32 rounded-full opacity-[0.08] group-hover:opacity-[0.15] transition-opacity blur-2xl" style={{ backgroundColor: accent }} />
      <div className="relative flex items-start justify-between">
        <div className="flex-1 space-y-3 min-w-0">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block">{label}</span>
          <div className="text-xl lg:text-2xl font-bold font-mono tabular-nums truncate" style={{ color: accent }}>{value}</div>
          <div className="text-[11px] text-muted-foreground truncate" title={subtitle}>{subtitle}</div>
        </div>
        <div className="p-2.5 rounded-lg shrink-0 ml-2" style={{ backgroundColor: `${accent}15`, border: `1px solid ${accent}30` }}>{icon}</div>
      </div>
    </motion.div>
  );
}

function CompactField({ label, value, onChange }: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <div className="p-3 rounded-lg border border-border/60 bg-background/60 hover:border-primary/30 transition-colors">
      <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5">{label}</label>
      <input type="number" value={value} onChange={(e) => onChange(parseInt(e.target.value, 10) || 0)} className="w-full bg-transparent border-0 outline-none text-xs text-foreground font-mono focus:ring-0" />
    </div>
  );
}

function CompactTextField({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <div className="p-3 rounded-lg border border-border/60 bg-background/60 hover:border-primary/30 transition-colors">
      <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1.5">{label}</label>
      <input type="text" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className="w-full bg-transparent border-0 outline-none text-xs text-foreground font-mono focus:ring-0" />
    </div>
  );
}

interface ProviderListRowProps {
  provider: ProviderDef;
  enabled: boolean;
  expanded: boolean;
  onToggleExpand: () => void;
  config: any;
  ping?: { testing: boolean; ok?: boolean; latency_ms?: number; msg?: string };
  showKeys: Record<string, boolean>;
  onToggleEnabled: () => void;
  onUpdateField: (field: string, value: any) => void;
  onToggleShowKey: (id: string) => void;
  onTest: () => void;
  delay: number;
}

function ProviderListRow({
  provider, enabled, expanded, onToggleExpand, config, ping, showKeys,
  onToggleEnabled, onUpdateField, onToggleShowKey, onTest, delay,
}: ProviderListRowProps) {
  const subConfig = config[provider.configKey] || {};
  const cost = subConfig.cost_per_query ?? 0.0;
  const Icon = BrandIcons[provider.iconKey];

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay, ease: [0.16, 1, 0.3, 1] }}
      className={`group relative transition-colors ${enabled ? "" : "opacity-70"}`}
    >
      {enabled && (
        <div className="absolute left-0 top-0 bottom-0 w-[3px]" style={{ backgroundColor: provider.accent }} />
      )}

      <button type="button" onClick={onToggleExpand} className="w-full flex items-center gap-0 p-4 text-left cursor-pointer hover:bg-muted/30 transition-colors">
        <div className="shrink-0 w-11 h-11 flex items-center justify-center transition-transform" style={{ color: provider.accent }}>
          <Icon width={22} height={22} />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-foreground">{provider.name}</span>
            {provider.docsUrl && (
              <a href={provider.docsUrl} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} className="text-[10px] text-muted-foreground hover:text-primary transition-colors flex items-center gap-0.5">
                <ExternalLink size={9} />
                <span className="hidden sm:inline">docs</span>
              </a>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground truncate mt-0.5">{provider.tagline}</p>
        </div>

        <div className="flex items-center gap-3 shrink-0">
          <span className={`text-[10px] font-mono font-semibold px-2 py-0.5 rounded-md border ${
            enabled ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30" : "bg-muted/50 text-muted-foreground border-border"
          }`}>
            {enabled ? "Active" : "Off"}
          </span>
          <span className="text-[10px] text-muted-foreground font-mono tabular-nums hidden md:inline">${cost.toFixed(4)}/q</span>
          {ping && !ping.testing && (
            <span className={`text-[10px] font-mono flex items-center gap-1 ${ping.ok ? "text-emerald-500" : "text-rose-500"}`}>
              {ping.ok ? <CheckCircle2 size={11} /> : <XCircle size={11} />}
              {ping.ok && typeof ping.latency_ms === "number" ? `${Math.round(ping.latency_ms)}ms` : "Failed"}
            </span>
          )}
          <motion.div animate={{ rotate: expanded ? 180 : 0 }} transition={{ duration: 0.2 }}>
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
            <div className="px-4 pb-4 pl-[68px] space-y-3 bg-muted/10">
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 pt-1">
                {provider.fields.map((field) => {
                  if (field.type === "password") {
                    const isVisible = showKeys[field.secretKey || provider.id];
                    return (
                      <div key={field.key} className="space-y-1">
                        <label className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">{field.label}</label>
                        <div className="relative">
                          <input
                            type={isVisible ? "text" : "password"}
                            value={subConfig[field.key] || ""}
                            onChange={(e) => onUpdateField(field.key, e.target.value)}
                            placeholder={field.placeholder}
                            className="w-full bg-background border border-border rounded-lg px-2.5 py-1.5 text-[11px] text-foreground font-mono pr-7 focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
                          />
                          <button type="button" onClick={() => onToggleShowKey(field.secretKey || provider.id)} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground cursor-pointer">
                            {isVisible ? <EyeOff size={11} /> : <Eye size={11} />}
                          </button>
                        </div>
                      </div>
                    );
                  }
                  return (
                    <div key={field.key} className="space-y-1">
                      <label className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">{field.label}</label>
                      <input
                        type="text"
                        value={subConfig[field.key] || ""}
                        onChange={(e) => onUpdateField(field.key, e.target.value)}
                        placeholder={field.placeholder}
                        className={`w-full bg-background border border-border rounded-lg px-2.5 py-1.5 text-[11px] text-foreground focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow ${field.mono ? "font-mono" : ""}`}
                      />
                    </div>
                  );
                })}

                {provider.costField && (
                  <div className="space-y-1">
                    <label className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">Cost per Query (USD)</label>
                    <input
                      type="number" step="0.001" min={0} value={cost}
                      onChange={(e) => onUpdateField("cost_per_query", parseFloat(e.target.value) || 0.0)}
                      className="w-full bg-background border border-border rounded-lg px-2.5 py-1.5 text-[11px] text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
                    />
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between pt-3 border-t border-border/40 flex-wrap gap-2">
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    onClick={(e) => { e.stopPropagation(); onTest(); }}
                    disabled={ping?.testing}
                    className="h-7 text-[11px] px-2.5 gap-1.5 cursor-pointer hover:scale-105 transition-transform disabled:hover:scale-100"
                  >
                    {ping?.testing ? <RefreshCw size={11} className="animate-spin" /> : <Activity size={11} />}
                    <span>{ping?.testing ? "Testing..." : provider.testLabel}</span>
                  </Button>
                  {ping?.msg && !ping.testing && (
                    <span className={`text-[10px] ${ping.ok ? "text-muted-foreground" : "text-rose-500"}`}>{ping.msg}</span>
                  )}
                </div>

                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); onToggleEnabled(); }}
                  className={`text-[10px] font-mono font-semibold px-2.5 py-1 rounded-md border cursor-pointer transition-all ${
                    enabled
                      ? "bg-rose-500/10 text-rose-600 dark:text-rose-400 border-rose-500/30 hover:bg-rose-500/20"
                      : "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30 hover:bg-emerald-500/20"
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
