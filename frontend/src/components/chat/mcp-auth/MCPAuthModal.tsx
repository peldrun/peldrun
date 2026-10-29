// frontend/src/components/chat/mcp-auth/MCPAuthModal.tsx
"use client";

/**
 * Per-chat MCP authentication modal.
 *
 * Opened by the ChatContainer when the backend emits an SSE
 * `mcp_auth_required` event. Presents three decisions:
 *
 *   1. Use Global Settings   ->  POST .../use-global
 *   2. One-time Credentials  ->  POST .../ephemeral   (env form)
 *   3. Authenticate & Save   ->  OAuth popup          (if supported)
 *                            ->  POST .../token      (env form otherwise)
 *
 * After any successful decision the caller's `onSuccess` fires, which
 * closes the modal and re-issues POST /api/run with the same prompt so
 * a fresh job starts with the newly-recorded credentials.
 *
 * The OAuth path opens a popup at the URL returned by
 * POST .../start. Completion is signalled via postMessage from the
 * callback page (see backend routers/mcp_auth.py) with
 * `{source: "peldrun-mcp-oauth", status: "success"|"error", ...}`.
 * The postMessage handler verifies:
 *   - event.origin === window.location.origin
 *   - data.source === "peldrun-mcp-oauth"
 *   - data.chat_id === this chatId
 *   - data.server_id === this payload.serverId
 * which together guarantee that only the callback of the popup we
 * opened can complete this flow.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Eye,
  EyeOff,
  Globe,
  Loader2,
  Lock,
  X,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";
import type { McpAuthRequiredPayload } from "../chat-container/hooks/use-sse-stream";

// ---------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------

type FlowPhase = "choose" | "env-form" | "oauth-waiting";

interface JSONSchemaProperty {
  type?: "string" | "integer" | "number" | "boolean";
  title?: string;
  description?: string;
  default?: unknown;
  secret?: boolean;
}

interface JSONSchema {
  type?: string;
  properties?: Record<string, JSONSchemaProperty>;
  required?: string[];
}

export interface MCPAuthModalProps {
  open: boolean;
  payload: McpAuthRequiredPayload | null;
  chatId: string;
  /** User dismissed the modal without authenticating. */
  onClose: () => void;
  /** Auth decision recorded successfully - caller should re-run. */
  onSuccess: () => void;
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

const OAUTH_MESSAGE_SOURCE = "peldrun-mcp-oauth";
const POPUP_POLL_INTERVAL_MS = 800;

function humanizeKey(key: string): string {
  return key.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function isSecretField(
  fieldKey: string,
  prop: JSONSchemaProperty | undefined
): boolean {
  if (prop?.secret) return true;
  return /token|secret|key|password/i.test(fieldKey);
}

// ---------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------

export function MCPAuthModal({
  open,
  payload,
  chatId,
  onClose,
  onSuccess,
}: MCPAuthModalProps) {
  // Flow state -------------------------------------------------------
  const [phase, setPhase] = useState<FlowPhase>("choose");
  const [ephemeralMode, setEphemeralMode] = useState(false);
  const [envValues, setEnvValues] = useState<Record<string, string>>({});
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // OAuth popup lifecycle -------------------------------------------
  const popupRef = useRef<Window | null>(null);
  const popupPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const cleanupPopup = useCallback(() => {
    if (popupPollRef.current) {
      clearInterval(popupPollRef.current);
      popupPollRef.current = null;
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

  // Reset internal state whenever a new payload arrives --------------
  useEffect(() => {
    if (!payload) return;
    setPhase("choose");
    setEphemeralMode(false);
    setEnvValues({});
    setShowSecrets({});
    setErrorMessage(null);
    setSaving(false);
    cleanupPopup();
  }, [payload, cleanupPopup]);

  // Kill popup when modal closes ------------------------------------
  useEffect(() => {
    if (!open) cleanupPopup();
  }, [open, cleanupPopup]);

  // Cleanup on unmount ----------------------------------------------
  useEffect(() => {
    return () => cleanupPopup();
  }, [cleanupPopup]);

  // postMessage listener for OAuth callback -------------------------
  useEffect(() => {
    if (phase !== "oauth-waiting" || !payload) return;

    const handler = (event: MessageEvent) => {
      // Security: same-origin only. The callback page is served by
      // the same PELDRUN instance that opened the popup, so a strict
      // origin check is both safe and sufficient.
      if (event.origin !== window.location.origin) return;
      const data = event.data;
      if (!data || typeof data !== "object") return;
      if (data.source !== OAUTH_MESSAGE_SOURCE) return;
      if (data.chat_id !== chatId) return;
      if (data.server_id !== payload.serverId) return;

      cleanupPopup();

      if (data.status === "success") {
        showToast.success(
          "Authentication Succeeded",
          "Resuming the task with the newly-saved credentials."
        );
        onSuccess();
      } else {
        setErrorMessage(
          String(
            data.error_message ||
              data.error_code ||
              "Authentication was not completed."
          )
        );
        setPhase("choose");
      }
    };

    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [phase, payload, chatId, onSuccess, cleanupPopup]);

  // ESC to close (unless busy) --------------------------------------
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !saving && phase !== "oauth-waiting") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, saving, phase, onClose]);

  // API actions -----------------------------------------------------

  const startOAuth = useCallback(async () => {
    if (!payload) return;
    setErrorMessage(null);
    setSaving(true);
    try {
      const res = await fetch(
        `/api/chats/${encodeURIComponent(chatId)}/mcp-auth/${encodeURIComponent(payload.serverId)}/start`,
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
        "peldrun-mcp-auth",
        "width=600,height=700,menubar=no,toolbar=no,location=yes,status=yes"
      );
      if (!popup) {
        throw new Error(
          "Popup blocked. Please allow popups for PELDRUN and try again."
        );
      }

      popupRef.current = popup;
      setPhase("oauth-waiting");

      // Detect user closing the popup before completing auth.
      // On success/error the postMessage handler calls
      // cleanupPopup(), which clears this interval before it can
      // fire - so a fire here always means "user cancelled".
      popupPollRef.current = setInterval(() => {
        if (popupRef.current && popupRef.current.closed) {
          cleanupPopup();
          setErrorMessage(
            "Authentication window was closed before completion."
          );
          setPhase("choose");
        }
      }, POPUP_POLL_INTERVAL_MS);
    } catch (err: any) {
      setErrorMessage(err?.message || "Failed to start OAuth flow.");
    } finally {
      setSaving(false);
    }
  }, [payload, chatId, cleanupPopup]);

  const submitEnv = useCallback(
    async (ephemeral: boolean) => {
      if (!payload) return;

      const schema = (payload.envSchema || {}) as JSONSchema;
      const required = schema.required || [];
      for (const field of required) {
        if (!(envValues[field] || "").trim()) {
          setErrorMessage(`"${humanizeKey(field)}" is required.`);
          return;
        }
      }

      setSaving(true);
      setErrorMessage(null);
      try {
        const endpoint = ephemeral ? "ephemeral" : "token";
        const body = ephemeral
          ? { env: envValues }
          : { mode: "custom", env: envValues };

        const res = await fetch(
          `/api/chats/${encodeURIComponent(chatId)}/mcp-auth/${encodeURIComponent(payload.serverId)}/${endpoint}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }

        showToast.success(
          ephemeral ? "One-time Credentials Applied" : "Credentials Saved",
          "Resuming the task with the new credentials."
        );
        onSuccess();
      } catch (err: any) {
        setErrorMessage(err?.message || "Failed to save credentials.");
      } finally {
        setSaving(false);
      }
    },
    [payload, chatId, envValues, onSuccess]
  );

  const saveUseGlobal = useCallback(async () => {
    if (!payload) return;
    setSaving(true);
    setErrorMessage(null);
    try {
      const res = await fetch(
        `/api/chats/${encodeURIComponent(chatId)}/mcp-auth/${encodeURIComponent(payload.serverId)}/use-global`,
        { method: "POST" }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      showToast.success(
        "Using Global Settings",
        "Resuming the task with the server-wide credentials."
      );
      onSuccess();
    } catch (err: any) {
      setErrorMessage(err?.message || "Failed to record your choice.");
    } finally {
      setSaving(false);
    }
  }, [payload, chatId, onSuccess]);

  const openEnvForm = useCallback((ephemeral: boolean) => {
    setEphemeralMode(ephemeral);
    setPhase("env-form");
    setErrorMessage(null);
  }, []);

  // Render guards ---------------------------------------------------
  if (!open || !payload) return null;

  const schema = (payload.envSchema || {}) as JSONSchema;
  const properties = schema.properties || {};
  const propertyKeys = Object.keys(properties);
  const hasEnvFields = propertyKeys.length > 0;
  const oauthSupported = payload.oauthSupported;

  const canClose = !saving && phase !== "oauth-waiting";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="mcp-auth-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && canClose) onClose();
      }}
    >
      <div className="bg-card rounded-md shadow-2xl w-full max-w-xl max-h-[90vh] flex flex-col border border-border">
        {/* Header */}
        <div className="px-5 py-3.5 border-b border-border flex items-center justify-between shrink-0 bg-muted/40">
          <div className="min-w-0 flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary shrink-0">
              <Lock size={16} />
            </div>
            <div className="min-w-0">
              <h2
                id="mcp-auth-title"
                className="text-sm font-bold text-foreground"
              >
                MCP Authentication Required
              </h2>
              <p className="text-xs text-muted-foreground truncate">
                {payload.serverName}{" "}
                <span className="font-mono text-[10px] opacity-70">
                  ({payload.serverId})
                </span>
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={!canClose}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-40 disabled:cursor-not-allowed transition"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 scrollbar-thin space-y-4">
          {errorMessage && (
            <div className="flex items-start gap-2.5 p-3 bg-destructive/10 border border-destructive/30 rounded-sm">
              <AlertTriangle
                size={15}
                className="text-destructive shrink-0 mt-0.5"
              />
              <div className="text-xs text-foreground leading-relaxed">
                {errorMessage}
              </div>
            </div>
          )}

          {phase === "choose" && (
            <>
              <p className="text-xs text-muted-foreground leading-relaxed">
                This MCP server needs credentials to act on your behalf.
                Choose how to supply them for{" "}
                <span className="font-mono text-[11px] text-foreground">
                  {payload.serverId}
                </span>{" "}
                in this chat.
              </p>

              <div className="space-y-2.5">
                <OptionCard
                  icon={<Globe size={16} />}
                  title="Use Global Settings"
                  description="Fall back to the credentials configured in the MCP Settings store."
                  onClick={saveUseGlobal}
                  disabled={saving}
                  variant="secondary"
                />

                <OptionCard
                  icon={<Clock size={16} />}
                  title="One-time for This Chat"
                  description="Provide credentials for this chat only. Never written to disk; discarded on restart."
                  onClick={() => openEnvForm(true)}
                  disabled={saving || !hasEnvFields}
                  disabledHint={
                    !hasEnvFields
                      ? "This server does not declare any credential fields."
                      : undefined
                  }
                  variant="secondary"
                />

                {oauthSupported ? (
                  <OptionCard
                    icon={<CheckCircle2 size={16} />}
                    title="Authenticate & Save"
                    description="Open the provider's sign-in page in a popup and store the returned tokens securely for this chat."
                    onClick={startOAuth}
                    disabled={saving}
                    variant="primary"
                  />
                ) : (
                  <OptionCard
                    icon={<CheckCircle2 size={16} />}
                    title="Save Credentials for This Chat"
                    description="Enter credentials and store them encrypted for future runs of this chat."
                    onClick={() => openEnvForm(false)}
                    disabled={saving || !hasEnvFields}
                    disabledHint={
                      !hasEnvFields
                        ? "This server does not declare any credential fields."
                        : undefined
                    }
                    variant="primary"
                  />
                )}
              </div>
            </>
          )}

          {phase === "env-form" && (
            <>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setPhase("choose");
                    setErrorMessage(null);
                  }}
                  disabled={saving}
                  className="text-[11px] text-muted-foreground hover:text-foreground transition disabled:opacity-50"
                >
                  ← Back
                </button>
                <span className="text-[11px] text-muted-foreground">
                  {ephemeralMode
                    ? "One-time credentials - not saved to disk"
                    : "Saved credentials - encrypted at rest"}
                </span>
              </div>

              <div className="space-y-3">
                {propertyKeys.map((key) => {
                  const prop = properties[key] || {};
                  const secret = isSecretField(key, prop);
                  const visible = showSecrets[key] === true;
                  return (
                    <div key={key}>
                      <div className="flex items-center justify-between mb-1">
                        <label className="text-xs font-semibold text-foreground">
                          {prop.title || humanizeKey(key)}
                          {schema.required?.includes(key) && (
                            <span className="text-destructive ml-0.5">*</span>
                          )}
                        </label>
                        {secret && (
                          <button
                            type="button"
                            onClick={() =>
                              setShowSecrets((prev) => ({
                                ...prev,
                                [key]: !prev[key],
                              }))
                            }
                            className="text-[10px] text-muted-foreground hover:text-foreground flex items-center gap-1"
                          >
                            {visible ? <EyeOff size={11} /> : <Eye size={11} />}
                            <span>{visible ? "Hide" : "Show"}</span>
                          </button>
                        )}
                      </div>
                      <input
                        type={secret && !visible ? "password" : "text"}
                        value={
                          envValues[key] ??
                          (prop.default !== undefined
                            ? String(prop.default)
                            : "")
                        }
                        onChange={(e) =>
                          setEnvValues((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        disabled={saving}
                        placeholder={
                          prop.type === "string" && !secret ? "" : "••••••••"
                        }
                        className="w-full px-3 py-1.5 bg-background border border-border rounded-md text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary font-mono transition disabled:opacity-60"
                      />
                      {prop.description && (
                        <p className="text-[10px] text-muted-foreground mt-1">
                          {prop.description}
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {phase === "oauth-waiting" && (
            <div className="py-8 flex flex-col items-center justify-center text-center gap-3">
              <Loader2
                size={28}
                className="text-primary animate-spin"
                aria-hidden="true"
              />
              <div>
                <p className="text-sm font-semibold text-foreground">
                  Waiting for authentication…
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  Complete the sign-in in the popup window. This dialog will
                  close automatically once the flow finishes.
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  cleanupPopup();
                  setPhase("choose");
                  setErrorMessage(
                    "Authentication was cancelled from the PELDRUN dialog."
                  );
                }}
                className="text-[11px] text-muted-foreground hover:text-foreground transition underline-offset-2 hover:underline"
              >
                Cancel and go back
              </button>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-border flex items-center justify-end gap-2 shrink-0 bg-muted/20">
          {phase === "env-form" ? (
            <>
              <button
                type="button"
                onClick={() => {
                  setPhase("choose");
                  setErrorMessage(null);
                }}
                disabled={saving}
                className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-50"
              >
                Back
              </button>
              <button
                type="button"
                onClick={() => submitEnv(ephemeralMode)}
                disabled={saving}
                className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:opacity-90 disabled:opacity-50 transition"
              >
                {saving ? (
                  <>
                    <Loader2 size={13} className="animate-spin" />
                    Saving…
                  </>
                ) : (
                  <>
                    <CheckCircle2 size={13} />
                    {ephemeralMode
                      ? "Apply One-time Credentials"
                      : "Save Credentials"}
                  </>
                )}
              </button>
            </>
          ) : phase === "oauth-waiting" ? (
            <button
              type="button"
              onClick={() => {
                cleanupPopup();
                setPhase("choose");
                setErrorMessage(
                  "Authentication was cancelled from the PELDRUN dialog."
                );
              }}
              className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition"
            >
              Cancel
            </button>
          ) : (
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-50"
            >
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------
// OptionCard - a clickable card used on the "choose" screen
// ---------------------------------------------------------------------

function OptionCard({
  icon,
  title,
  description,
  onClick,
  disabled,
  disabledHint,
  variant,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  onClick: () => void;
  disabled?: boolean;
  disabledHint?: string;
  variant: "primary" | "secondary";
}) {
  const base =
    "w-full text-left p-3 rounded-md border transition-all flex items-start gap-3";
  const styles =
    variant === "primary"
      ? "border-primary/40 bg-primary/5 hover:bg-primary/10 hover:border-primary"
      : "border-border bg-card hover:bg-muted/30 hover:border-primary/30";
  const disabledStyles = disabled
    ? "opacity-50 cursor-not-allowed"
    : "cursor-pointer";

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`${base} ${styles} ${disabledStyles}`}
      title={disabledHint}
    >
      <div
        className={`mt-0.5 shrink-0 rounded-md p-1.5 ${
          variant === "primary"
            ? "bg-primary/15 text-primary"
            : "bg-muted text-muted-foreground"
        }`}
      >
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-xs font-semibold text-foreground">{title}</div>
        <div className="text-[11px] text-muted-foreground mt-0.5 leading-relaxed">
          {description}
        </div>
      </div>
    </button>
  );
}

export default MCPAuthModal;
