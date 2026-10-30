// frontend/src/app/stores/_modals/MCPSettingsModal.tsx
"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Play,
  Save,
  Sliders,
  X,
  XCircle,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "../_hooks/useStoreData";

interface JSONSchemaProperty {
  type?: "string" | "integer" | "number" | "boolean" | "object" | "array";
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  enumNames?: string[];
  minimum?: number;
  maximum?: number;
  properties?: Record<string, JSONSchemaProperty>;
  items?: JSONSchemaProperty;
  secret?: boolean;
}

interface JSONSchema {
  type?: string;
  properties?: Record<string, JSONSchemaProperty>;
  required?: string[];
}

type FormValues = Record<string, unknown>;

export interface MCPSettingsModalProps {
  open: boolean;
  serverId: string | null;
  serverName: string;
  onClose: () => void;
  onSaved?: () => void;
}

function humanizeKey(key: string): string {
  return key.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function getDefaultFor(prop: JSONSchemaProperty | undefined): unknown {
  if (!prop) return undefined;
  if (prop.default !== undefined) return prop.default;
  if (prop.type === "object" && prop.properties) {
    const obj: Record<string, unknown> = {};
    for (const [k, sub] of Object.entries(prop.properties)) {
      const d = getDefaultFor(sub);
      if (d !== undefined) obj[k] = d;
    }
    return Object.keys(obj).length > 0 ? obj : undefined;
  }
  return undefined;
}

function coerceValue(prop: JSONSchemaProperty | undefined, raw: unknown): unknown {
  if (!prop) return raw;

  if (prop.type === "object" && prop.properties) {
    const obj = (raw || {}) as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const [k, sub] of Object.entries(prop.properties)) {
      if (k in obj) out[k] = coerceValue(sub, obj[k]);
    }
    return out;
  }

  if (prop.type === "array") {
    if (typeof raw === "string") {
      return raw.split("\n").map((s) => s.trim()).filter(Boolean);
    }
    return Array.isArray(raw) ? raw : [];
  }

  if (prop.type === "integer") {
    const n = Number(raw);
    return Number.isFinite(n) ? Math.trunc(n) : raw;
  }
  if (prop.type === "number") {
    const n = Number(raw);
    return Number.isFinite(n) ? n : raw;
  }
  if (prop.type === "boolean") return Boolean(raw);
  return raw;
}

export function MCPSettingsModal({
  open,
  serverId,
  serverName,
  onClose,
  onSaved,
}: MCPSettingsModalProps) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [settingsSchema, setSettingsSchema] = useState<JSONSchema | null>(null);
  const [envSchema, setEnvSchema] = useState<JSONSchema | null>(null);
  const [settingsValues, setSettingsValues] = useState<FormValues>({});
  const [envValues, setEnvValues] = useState<FormValues>({});
  const [activeTab, setActiveTab] = useState<"settings" | "env">("settings");

  // ── OAuth state (for OAuth-capable servers only) ──────────────
  // These are populated when /settings reports oauth_capable=true.
  // OAuth servers (Notion, Linear, etc.) have NO env_schema — their
  // credentials are tokens, not env vars. The flow lives entirely
  // in this modal so the user can authorize once from the Store
  // without ever visiting a chat.
  const [oauthCapable, setOauthCapable] = useState(false);
  const [oauthAuthenticated, setOauthAuthenticated] = useState(false);
  const [oauthNeedsRefresh, setOauthNeedsRefresh] = useState(false);
  const [oauthLoading, setOauthLoading] = useState(false);
  const [oauthConnecting, setOauthConnecting] = useState(false);

  // ── OAuth Provider Credentials (pre-registered providers) ────
  // Some providers (Google, GitHub) do not support Dynamic Client
  // Registration — the operator must create an OAuth app manually
  // and paste its credentials. The card below stores these at the
  // PROVIDER level (not per-server), so configuring Google once
  // covers Gmail, Drive, and Calendar simultaneously.
  const [oauthProviderKey, setOauthProviderKey] = useState<string | null>(null);
  const [oauthProviderDisplayName, setOauthProviderDisplayName] = useState("");
  const [oauthProviderReady, setOauthProviderReady] = useState(false);
  const popupRef = useRef<Window | null>(null);
  const popupPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const messageHandlerRef = useRef<((e: MessageEvent) => void) | null>(null);
  const oauthResolvedRef = useRef(false);

  const cleanupOAuthPopup = useCallback(() => {
    if (popupPollRef.current) {
      clearInterval(popupPollRef.current);
      popupPollRef.current = null;
    }
    if (messageHandlerRef.current) {
      window.removeEventListener("message", messageHandlerRef.current);
      messageHandlerRef.current = null;
    }
    if (popupRef.current && !popupRef.current.closed) {
      try {
        popupRef.current.close();
      } catch {
        /* ignore */
      }
    }
    popupRef.current = null;
  }, []);

  const fetchOAuthStatus = useCallback(async () => {
    if (!serverId) return;
    try {
      const res = await fetch(
        getApiUrl(`/api/mcp/servers/${serverId}/oauth/status`)
      );
      if (!res.ok) return;
      const data = await res.json();
      setOauthAuthenticated(Boolean(data.authenticated));
      setOauthNeedsRefresh(Boolean(data.needs_refresh));
    } catch {
      /* best-effort */
    }
  }, [serverId]);

  const handleConnectOAuth = useCallback(async () => {
    if (!serverId) return;
    setOauthConnecting(true);
    oauthResolvedRef.current = false;
    cleanupOAuthPopup();

    try {
      const res = await fetch(
        getApiUrl(`/api/mcp/servers/${serverId}/oauth/start`),
        { method: "POST" }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      const authUrl: string = data?.auth_url;
      if (!authUrl) throw new Error("Backend returned no auth_url.");

      const popup = window.open(
        authUrl,
        "peldrun-mcp-oauth-global",
        "width=600,height=700,menubar=no,toolbar=no,location=yes,status=yes"
      );
      if (!popup) {
        throw new Error(
          "Popup blocked. Allow popups for PELDRUN and try again."
        );
      }
      popupRef.current = popup;

      // postMessage listener for the callback page.
      const onMessage = (event: MessageEvent) => {
        if (event.origin !== window.location.origin) return;
        const payload = event.data;
        if (!payload || typeof payload !== "object") return;
        if (payload.source !== "peldrun-mcp-oauth") return;
        if (payload.server_id !== serverId) return;
        // chat_id is null for global-scope flows — do NOT check it.

        oauthResolvedRef.current = true;
        cleanupOAuthPopup();
        setOauthConnecting(false);

        if (payload.status === "success") {
          showToast.success(
            "OAuth Connected",
            "You can now use this server from any chat."
          );
          fetchOAuthStatus();
        } else {
          showToast.error(
            "OAuth Failed",
            payload.error_message ||
              payload.error_code ||
              "Authentication was not completed."
          );
        }
      };
      window.addEventListener("message", onMessage);
      messageHandlerRef.current = onMessage;

      // Detect popup close (either via callback or user cancellation).
      popupPollRef.current = setInterval(() => {
        if (popupRef.current && popupRef.current.closed) {
          if (popupPollRef.current) {
            clearInterval(popupPollRef.current);
            popupPollRef.current = null;
          }

          // Do not immediately stop the loader. Give the postMessage
          // 800ms to arrive. If it doesn't, start polling the backend
          // to verify if the tokens were saved successfully.
          setTimeout(async () => {
            if (oauthResolvedRef.current) return; // Handled by onMessage

            let isAuth = false;
            for (let i = 0; i < 4; i++) {
              try {
                const statusRes = await fetch(
                  getApiUrl(`/api/mcp/servers/${serverId}/oauth/status`)
                );
                if (statusRes.ok) {
                  const statusData = await statusRes.json();
                  if (statusData.authenticated) {
                    isAuth = true;
                    setOauthAuthenticated(true);
                    setOauthNeedsRefresh(Boolean(statusData.needs_refresh));
                    break;
                  }
                }
              } catch {
                /* ignore */
              }
              await new Promise((r) => setTimeout(r, 1000));
            }

            if (oauthResolvedRef.current) return; // Handled during polling

            oauthResolvedRef.current = true;
            cleanupOAuthPopup();
            setOauthConnecting(false);

            if (isAuth) {
              showToast.success(
                "OAuth Connected",
                "You can now use this server from any chat."
              );
            } else {
              showToast.error(
                "OAuth Failed",
                "Authentication was not completed or the window was closed."
              );
            }
          }, 800);
        }
      }, 500);
    } catch (err: any) {
      setOauthConnecting(false);
      cleanupOAuthPopup();
      showToast.error(
        "OAuth Failed",
        err?.message || "Could not start OAuth flow."
      );
    }
  }, [serverId, cleanupOAuthPopup, fetchOAuthStatus]);

  const handleDisconnectOAuth = useCallback(async () => {
    if (!serverId) return;
    setOauthLoading(true);
    try {
      const res = await fetch(
        getApiUrl(`/api/mcp/servers/${serverId}/oauth`),
        { method: "DELETE" }
      );
      if (res.ok) {
        showToast.success(
          "Disconnected",
          "OAuth tokens removed from this server."
        );
        fetchOAuthStatus();
      } else {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
    } catch (err: any) {
      showToast.error(
        "Disconnect Failed",
        err?.message || "Could not remove OAuth tokens."
      );
    } finally {
      setOauthLoading(false);
    }
  }, [serverId, fetchOAuthStatus]);

  // The actual credential form lives in the Integrations page
  // (frontend/src/features/mcp-oauth/). This modal only shows a
  // banner pointing the user there when the provider is not yet
  // configured. Removing the duplicate form fixes the misleading
  // UX where users saw the same fields in every Google server's
  // Settings tab.

  useEffect(() => {
    if (!open || !serverId) return;
    let cancelled = false;

    setLoading(true);
    setError(null);
    setSettingsSchema(null);
    setEnvSchema(null);
    setSettingsValues({});
    setEnvValues({});
    setTestResult(null);
    // Reset OAuth state on every open so switching between servers
    // does not leak the previous server's connection status.
    setOauthCapable(false);
    setOauthAuthenticated(false);
    setOauthNeedsRefresh(false);
    setOauthLoading(false);
    setOauthConnecting(false);
    // Reset provider credentials state too — the provider key is
    // server-specific and must not persist across modal opens.
    setOauthProviderKey(null);
    setOauthProviderDisplayName("");
    setOauthProviderReady(false);
    cleanupOAuthPopup();

    (async () => {
      try {
        const res = await fetch(getApiUrl(`/api/mcp/servers/${serverId}/settings`));
        if (cancelled) return;

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          setError(err.detail || `Failed to load settings (HTTP ${res.status}).`);
          return;
        }

        const data = await res.json();
        const sSchema: JSONSchema | null = data.settings_schema || null;
        const eSchema: JSONSchema | null = data.env_schema || null;
        const sValues: FormValues = data.settings || {};
        const eValues: FormValues = data.env || {};

        if (sSchema?.properties) {
          for (const [k, prop] of Object.entries(sSchema.properties)) {
            if (!(k in sValues)) {
              const d = getDefaultFor(prop);
              if (d !== undefined) sValues[k] = d;
            }
          }
        }

        if (eSchema?.properties) {
          for (const [k, prop] of Object.entries(eSchema.properties)) {
            if (!(k in eValues)) {
              const d = getDefaultFor(prop);
              if (d !== undefined) eValues[k] = d;
            }
          }
        }

        setSettingsSchema(sSchema);
        setEnvSchema(eSchema);
        setSettingsValues(sValues);
        setEnvValues(eValues);

        // Detect OAuth capability and fetch current connection status
        // (best-effort — a failed status check just leaves the UI in
        // "not connected" state, so the Connect button still shows).
        const isOauthCapable = Boolean(data.oauth_capable);
        setOauthCapable(isOauthCapable);

        if (isOauthCapable && !cancelled) {
          try {
            const statusRes = await fetch(
              getApiUrl(`/api/mcp/servers/${serverId}/oauth/status`)
            );
            if (statusRes.ok && !cancelled) {
              const status = await statusRes.json();
              setOauthAuthenticated(Boolean(status.authenticated));
              setOauthNeedsRefresh(Boolean(status.needs_refresh));
            }
          } catch {
            /* best-effort */
          }

          // Map this server to a pre-registered OAuth provider by
          // matching the provider key against the server URL host.
          // Google Workspace MCP servers share gmailmcp.googleapis.com,
          // drivemcp.googleapis.com, etc. — all of which contain
          // "googleapis.com", the registered provider key.
          try {
            const providersRes = await fetch(
              getApiUrl("/api/mcp/oauth-providers")
            );
            if (providersRes.ok && !cancelled) {
              const providersData = await providersRes.json();
              const providerList = Array.isArray(providersData.providers)
                ? providersData.providers
                : [];
              const serverUrl = String(data.url || "").toLowerCase();
              const matched = providerList.find(
                (p: any) =>
                  p &&
                  typeof p.provider_key === "string" &&
                  serverUrl.includes(p.provider_key.toLowerCase())
              );
              if (matched && !cancelled) {
                setOauthProviderKey(matched.provider_key);
                setOauthProviderDisplayName(
                  matched.display_name || matched.provider_key
                );
                setOauthProviderReady(Boolean(matched.ready));
              }
            }
          } catch {
            /* best-effort — provider setup stays hidden */
          }
        }

        if (!sSchema?.properties && eSchema?.properties) {
          setActiveTab("env");
        } else {
          setActiveTab("settings");
        }
      } catch (exc: any) {
        if (cancelled) return;
        setError(exc?.message || "Network error while loading MCP configuration.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, serverId, cleanupOAuthPopup]);

  // Cleanup OAuth popup when the modal closes or unmounts.
  useEffect(() => {
    if (!open) cleanupOAuthPopup();
    return () => cleanupOAuthPopup();
  }, [open, cleanupOAuthPopup]);

  const handleTestConnection = useCallback(async () => {
    if (!serverId) return;
    setTesting(true);
    setTestResult(null);
    try {
      const res = await fetch(getApiUrl("/api/mcp/test"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ server_id: serverId }),
      });
      const data = await res.json();
      const isOk = data.status === "ok";
      setTestResult({ ok: isOk, message: data.message || "Test completed." });
      if (isOk) {
        showToast.success("Connection Succeeded", data.message);
      } else {
        showToast.warning("Connection Warning", data.message);
      }
    } catch (err: any) {
      setTestResult({ ok: false, message: err.message || "Failed to reach backend." });
      showToast.error("Test Error", err.message);
    } finally {
      setTesting(false);
    }
  }, [serverId]);

  const handleSubmit = useCallback(async () => {
    if (!serverId) return;
    setSaving(true);
    setError(null);

    try {
      const payloadSettings: FormValues = {};
      for (const [key, prop] of Object.entries(settingsSchema?.properties || {})) {
        if (key in settingsValues) payloadSettings[key] = coerceValue(prop, settingsValues[key]);
      }

      const payloadEnv: FormValues = {};
      for (const [key, prop] of Object.entries(envSchema?.properties || {})) {
        if (key in envValues) payloadEnv[key] = coerceValue(prop, envValues[key]);
      }

      const res = await fetch(getApiUrl(`/api/mcp/servers/${serverId}/settings`), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          settings: payloadSettings,
          env: payloadEnv,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const detail = typeof err.detail === "string" ? err.detail : "Configuration save rejected.";
        setError(detail);
        showToast.error("Save Rejected", detail);
        return;
      }

      showToast.success("MCP Server Updated", `${serverName} configuration persisted.`);
      onSaved?.();
      onClose();
    } catch (exc: any) {
      const msg = exc?.message || "Network error while saving.";
      setError(msg);
      showToast.error("Save Failed", msg);
    } finally {
      setSaving(false);
    }
  }, [serverId, settingsSchema, envSchema, settingsValues, envValues, serverName, onSaved, onClose]);

  if (!open) return null;

  const hasSettings = !!(settingsSchema?.properties && Object.keys(settingsSchema.properties).length > 0);
  const hasEnv = !!(envSchema?.properties && Object.keys(envSchema.properties).length > 0);
  const hasAnyFields = hasSettings || hasEnv;
  const canSave = !saving && !loading;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      onClick={(e) => {
        if (e.target === e.currentTarget && !saving) onClose();
      }}
    >
      <div className="bg-card rounded-md shadow-2xl w-full max-w-2xl max-h-[90vh] flex flex-col border border-border">
        {/* Header */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0 bg-muted/40">
          <div className="min-w-0 flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
              <Sliders size={16} />
            </div>
            <div>
              <h2 className="text-sm font-bold text-foreground uppercase tracking-wide">
                MCP Server Configuration
              </h2>
              <p className="text-xs text-muted-foreground truncate">{serverName}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-50 transition"
          >
            <X size={18} />
          </button>
        </div>

        {/* Tab Selector */}
        {hasSettings && hasEnv && (
          <div className="flex border-b border-border bg-muted/20 px-6 pt-2">
            <button
              type="button"
              onClick={() => setActiveTab("settings")}
              className={`px-4 py-2 text-xs font-semibold border-b-2 transition-colors cursor-pointer ${
                activeTab === "settings"
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              Settings & Folders
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("env")}
              className={`px-4 py-2 text-xs font-semibold border-b-2 transition-colors cursor-pointer flex items-center gap-1.5 ${
                activeTab === "env"
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              <KeyRound size={12} />
              Environment Variables
            </button>
          </div>
        )}

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 scrollbar-thin space-y-4">
          {loading && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 size={20} className="animate-spin mr-2" />
              <span className="text-sm">Loading configuration…</span>
            </div>
          )}

          {!loading && error && (
            <div className="flex items-start gap-3 p-3 bg-destructive/10 border border-destructive/30 rounded-sm">
              <AlertTriangle size={16} className="text-destructive shrink-0 mt-0.5" />
              <div className="text-xs text-foreground">{error}</div>
            </div>
          )}

          {/* ── OAuth Provider Banner (pre-registered, not configured) ── */}
          {/* The full setup form lives in the Integrations page. When
              the provider is not configured, this banner redirects the
              user there instead of showing a duplicate form here. */}
          {!loading && oauthCapable && oauthProviderKey && !oauthProviderReady && (
            <div className="p-3 rounded-md border border-amber-500/40 bg-amber-500/5 flex items-start gap-2">
              <AlertTriangle size={14} className="text-amber-600 shrink-0 mt-0.5" />
              <div className="flex-1 text-[11px] text-foreground leading-relaxed">
                <strong>{oauthProviderDisplayName} OAuth client</strong>{" "}
                is not configured yet. Configure it once in the{" "}
                <a
                  href="/integrations#mcp-oauth-providers"
                  className="text-primary underline font-medium hover:opacity-80"
                >
                  Integrations page
                </a>
                . The credentials will be shared by Gmail, Drive,
                Calendar, and every other {oauthProviderDisplayName}
                -based server automatically.
              </div>
            </div>
          )}

          {/* ── OAuth Connection (OAuth-capable servers only) ──── */}
          {!loading && oauthCapable && (
            <OAuthConnectionCard
              authenticated={oauthAuthenticated}
              needsRefresh={oauthNeedsRefresh}
              connecting={oauthConnecting}
              disconnecting={oauthLoading}
              onConnect={handleConnectOAuth}
              onDisconnect={handleDisconnectOAuth}
              requiresProviderSetup={
                !!oauthProviderKey && !oauthProviderReady
              }
            />
          )}

          {!loading && !hasAnyFields && !oauthCapable && (
            <div className="p-8 text-center text-xs text-muted-foreground border border-dashed border-border rounded-md">
              This MCP server declares no additional configurable settings or environment variables.
            </div>
          )}

          {!loading && hasAnyFields && (
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                if (canSave) handleSubmit();
              }}
            >
              {activeTab === "settings" && hasSettings && (
                <div className="space-y-3">
                  {Object.entries(settingsSchema!.properties!).map(([key, prop]) => (
                    <McpFieldRow
                      key={key}
                      fieldKey={key}
                      prop={prop}
                      value={settingsValues[key]}
                      onChange={(k, val) => setSettingsValues((prev) => ({ ...prev, [k]: val }))}
                    />
                  ))}
                </div>
              )}

              {activeTab === "env" && hasEnv && (
                <div className="space-y-3">
                  {Object.entries(envSchema!.properties!).map(([key, prop]) => (
                    <McpFieldRow
                      key={key}
                      fieldKey={key}
                      prop={prop}
                      value={envValues[key]}
                      isEnv
                      onChange={(k, val) => setEnvValues((prev) => ({ ...prev, [k]: val }))}
                    />
                  ))}
                </div>
              )}
            </form>
          )}

          {/* Test connection alert result */}
          {testResult && (
            <div
              className={`p-3 rounded text-xs flex items-start gap-2 border ${
                testResult.ok
                  ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-600 dark:text-emerald-400"
                  : "bg-destructive/10 border-destructive/20 text-destructive"
              }`}
            >
              {testResult.ok ? (
                <CheckCircle2 size={15} className="shrink-0 mt-0.5" />
              ) : (
                <XCircle size={15} className="shrink-0 mt-0.5" />
              )}
              <span className="leading-tight">{testResult.message}</span>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-border flex items-center justify-between gap-2 shrink-0 bg-muted/20">
          <button
            type="button"
            onClick={handleTestConnection}
            disabled={testing || loading || !serverId}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground border border-border rounded-md hover:bg-muted/40 disabled:opacity-50 transition cursor-pointer font-medium"
          >
            {testing ? (
              <>
                <Loader2 size={13} className="animate-spin text-primary" />
                Testing Handshake…
              </>
            ) : (
              <>
                <Play size={12} className="text-primary fill-primary" />
                Test Connection
              </>
            )}
          </button>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="px-3.5 py-1.5 text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-50 cursor-pointer"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSubmit}
              disabled={!canSave}
              className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:opacity-90 disabled:opacity-50 transition cursor-pointer"
            >
              {saving ? (
                <>
                  <Loader2 size={13} className="animate-spin" />
                  Saving…
                </>
              ) : (
                <>
                  <Save size={13} />
                  Save Configuration
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function OAuthConnectionCard({
  authenticated,
  needsRefresh,
  connecting,
  disconnecting,
  onConnect,
  onDisconnect,
  requiresProviderSetup = false,
}: {
  authenticated: boolean;
  needsRefresh: boolean;
  connecting: boolean;
  disconnecting: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  requiresProviderSetup?: boolean;
}) {
  return (
    <div className="p-4 rounded-md border border-border bg-muted/20 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <KeyRound size={15} className="text-primary" />
          <span className="text-xs font-semibold text-foreground">
            OAuth Connection
          </span>
        </div>
        {authenticated ? (
          <span className="flex items-center gap-1 text-[11px] text-emerald-600 dark:text-emerald-400 font-medium">
            <CheckCircle2 size={12} />
            Connected
          </span>
        ) : (
          <span className="text-[11px] text-muted-foreground font-mono">
            Not connected
          </span>
        )}
      </div>

      <p className="text-[11px] text-muted-foreground leading-relaxed">
        {authenticated && !needsRefresh
          ? "This server is authorized. Tokens are encrypted at rest and used automatically by every chat without re-prompting."
          : authenticated && needsRefresh
          ? "The access token is close to expiry. Click Reconnect to refresh it. Tools may fail if the token has already expired."
          : "Authorize this server with the provider once. After connecting, tools are available in every chat without re-prompting."}
      </p>

      <div className="flex items-center gap-2">
        {authenticated && !needsRefresh ? (
          <button
            type="button"
            onClick={onDisconnect}
            disabled={disconnecting}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-destructive/30 bg-destructive/5 text-destructive text-xs font-medium hover:bg-destructive/10 transition disabled:opacity-50"
          >
            {disconnecting ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <XCircle size={13} />
            )}
            Disconnect
          </button>
        ) : (
          <button
            type="button"
            onClick={onConnect}
            disabled={connecting || requiresProviderSetup}
            title={
              requiresProviderSetup
                ? "Configure OAuth client credentials above first"
                : undefined
            }
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-xs font-medium hover:opacity-90 transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {connecting ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <ExternalLink size={13} />
            )}
            {requiresProviderSetup
              ? "Configure credentials first"
              : connecting
              ? "Waiting for browser…"
              : authenticated
              ? "Reconnect"
              : "Connect with OAuth"}
          </button>
        )}
      </div>
    </div>
  );
}



function McpFieldRow({
  fieldKey,
  prop,
  value,
  isEnv = false,
  onChange,
}: {
  fieldKey: string;
  prop: JSONSchemaProperty;
  value: unknown;
  isEnv?: boolean;
  onChange: (key: string, raw: unknown) => void;
}) {
  const [showSecret, setShowSecret] = useState(false);
  const label = prop.title || humanizeKey(fieldKey);
  const description = prop.description;
  const isSecret = prop.secret || fieldKey.toLowerCase().includes("token") || fieldKey.toLowerCase().includes("secret") || fieldKey.toLowerCase().includes("key");

  const baseInputClass =
    "w-full px-3 py-1.5 bg-background border border-border rounded-md text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary font-mono transition";

  if (prop.type === "boolean") {
    return (
      <label className="flex items-center gap-2.5 cursor-pointer select-none">
        <input
          type="checkbox"
          checked={Boolean(value ?? prop.default ?? false)}
          onChange={(e) => onChange(fieldKey, e.target.checked)}
          className="h-4 w-4 rounded border-border text-primary focus:ring-primary cursor-pointer"
        />
        <div>
          <span className="text-xs font-semibold text-foreground">{label}</span>
          {description && <p className="text-[11px] text-muted-foreground mt-0.5">{description}</p>}
        </div>
      </label>
    );
  }

  if (prop.type === "array") {
    const strVal = Array.isArray(value)
      ? value.map((x) => String(x)).join("\n")
      : typeof value === "string"
      ? value
      : "";
    return (
      <div>
        <label className="block text-xs font-semibold text-foreground mb-1">
          {label}
        </label>
        <textarea
          rows={3}
          className={baseInputClass}
          value={strVal}
          onChange={(e) => onChange(fieldKey, e.target.value)}
          placeholder={fieldKey.includes("dir") ? "D:\\AI\\peldrun\nC:\\MyWorkspace" : "One item per line"}
        />
        <p className="text-[10px] text-muted-foreground mt-1">
          {description || "Enter absolute directories or targets, one per line."}
        </p>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <label className="text-xs font-semibold text-foreground">{label}</label>
        {isSecret && (
          <button
            type="button"
            onClick={() => setShowSecret(!showSecret)}
            className="text-[10px] text-muted-foreground hover:text-foreground flex items-center gap-1 cursor-pointer"
          >
            {showSecret ? <EyeOff size={11} /> : <Eye size={11} />}
            <span>{showSecret ? "Hide" : "Show"}</span>
          </button>
        )}
      </div>
      <input
        type={isSecret && !showSecret ? "password" : prop.type === "number" || prop.type === "integer" ? "number" : "text"}
        className={baseInputClass}
        value={String(value ?? prop.default ?? "")}
        onChange={(e) => onChange(fieldKey, e.target.value)}
        placeholder={isEnv ? "ENV_VALUE" : ""}
      />
      {description && <p className="text-[10px] text-muted-foreground mt-1">{description}</p>}
    </div>
  );
}

export default MCPSettingsModal;
