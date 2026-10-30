// frontend/src/features/mcp-oauth/McpOAuthProvidersSection.tsx

"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  KeyRound,
  Loader2,
  Save,
  Shield,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";

// ─────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────

export interface McpOAuthProvider {
  provider_key: string;
  display_name: string;
  configured_via_ui: boolean;
  configured_via_env: boolean;
  ready: boolean;
  client_id_env: string;
  client_secret_env: string;
}

// ─────────────────────────────────────────────────────────────────
// Data hook (inline to keep the feature self-contained)
// ─────────────────────────────────────────────────────────────────

function useMcpOAuthProviders() {
  const [providers, setProviders] = useState<McpOAuthProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/mcp/oauth-providers", {
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setProviders(Array.isArray(data.providers) ? data.providers : []);
    } catch (err: any) {
      setError(err?.message || "Could not load OAuth providers.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { providers, loading, error, refresh };
}

// ─────────────────────────────────────────────────────────────────
// Section component (mounted in integrations/page.tsx)
// ─────────────────────────────────────────────────────────────────

export function McpOAuthProvidersSection() {
  const { providers, loading, error, refresh } = useMcpOAuthProviders();
  const [configuring, setConfiguring] = useState<McpOAuthProvider | null>(
    null
  );

  const handleDelete = async (provider: McpOAuthProvider) => {
    if (
      !confirm(
        `Remove the stored ${provider.display_name} OAuth client ` +
          `credentials?\n\nServers using them will need to be ` +
          `re-authorized after new credentials are saved.`
      )
    ) {
      return;
    }
    try {
      const res = await fetch(
        `/api/mcp/oauth-providers/${provider.provider_key}`,
        { method: "DELETE" }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      showToast.success(
        "Credentials Removed",
        `${provider.display_name} OAuth client deleted.`
      );
      await refresh();
    } catch (err: any) {
      showToast.error(
        "Delete Failed",
        err?.message || "Could not remove credentials."
      );
    }
  };

  if (loading && providers.length === 0) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground gap-2 text-sm">
        <Loader2 size={16} className="animate-spin text-primary" />
        Loading OAuth providers…
      </div>
    );
  }

  if (error && providers.length === 0) {
    return (
      <div className="p-4 rounded-md border border-destructive/30 bg-destructive/5 text-xs text-destructive">
        {error}
        <button
          type="button"
          onClick={refresh}
          className="ml-2 underline font-medium"
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <>
      {/* List layout — one row per provider. Rows stack vertically so
          the description text has enough horizontal space to breathe,
          and the "Configure / Remove / Details" actions align to the
          right edge for a scannable, tool-like feel. */}
      <div className="flex flex-col gap-3">
        {providers.map((provider) => (
          <McpOAuthProviderRow
            key={provider.provider_key}
            provider={provider}
            onConfigure={() => setConfiguring(provider)}
            onDelete={() => handleDelete(provider)}
          />
        ))}
      </div>

      <McpOAuthProviderConfigDialog
        provider={configuring}
        onClose={() => setConfiguring(null)}
        onSaved={refresh}
      />
    </>
  );
}

// ─────────────────────────────────────────────────────────────────
// Row (was Card)
// ─────────────────────────────────────────────────────────────────

function McpOAuthProviderRow({
  provider,
  onConfigure,
  onDelete,
}: {
  provider: McpOAuthProvider;
  onConfigure: () => void;
  onDelete: () => void;
}) {
  const [showDetail, setShowDetail] = useState(false);

  return (
    <div className="rounded-lg border border-gray-200 bg-white hover:shadow-sm transition-shadow">
      <div className="p-4 flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-4">
        {/* Icon + name + key + status badges */}
        <div className="flex items-center gap-3 min-w-0 sm:flex-1">
          <div className="p-2 rounded-md bg-primary/10 text-primary shrink-0">
            <Shield size={16} />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-sm font-bold text-gray-900">
                {provider.display_name}
              </h3>
              {provider.ready ? (
                <span className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200">
                  <CheckCircle2 size={10} />
                  Ready
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200">
                  <XCircle size={10} />
                  Not configured
                </span>
              )}
              {provider.configured_via_env && (
                <span
                  className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200"
                  title={`Configured via env vars: ${provider.client_id_env}`}
                >
                  env
                </span>
              )}
            </div>
            <p className="text-[11px] text-gray-500 font-mono truncate">
              {provider.provider_key}
            </p>
          </div>
        </div>

        {/* Description — sits in the middle on wide screens, wraps
            below on narrow ones. flex-1 keeps it flexible. */}
        <p className="text-xs text-gray-600 leading-relaxed sm:flex-[2] sm:px-2 min-w-0">
          OAuth 2.0 client used by MCP servers that require
          pre-registered credentials. Configure it once — every MCP
          server from this provider inherits the credentials
          automatically.
        </p>

        {/* Actions */}
        <div className="flex items-center gap-2 shrink-0 sm:self-start sm:pt-0.5">
          <button
            type="button"
            onClick={onConfigure}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-xs font-medium hover:opacity-90 transition"
          >
            <KeyRound size={12} />
            {provider.configured_via_ui ? "Update" : "Configure"}
          </button>
          {provider.configured_via_ui && (
            <button
              type="button"
              onClick={onDelete}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-destructive/30 bg-destructive/5 text-destructive text-xs font-medium hover:bg-destructive/10 transition"
              title="Remove stored credentials"
            >
              <Trash2 size={12} />
              Remove
            </button>
          )}
          <button
            type="button"
            onClick={() => setShowDetail((v) => !v)}
            className="text-[11px] text-gray-500 hover:text-gray-900 px-2"
          >
            {showDetail ? "Hide" : "Details"}
          </button>
        </div>
      </div>

      {showDetail && (
        <div className="px-4 pb-4 pt-1 border-t border-gray-100 mt-2">
          <div className="text-[11px] text-gray-600 bg-gray-50 rounded p-2 space-y-1">
            <p>
              <strong>UI-stored:</strong>{" "}
              {provider.configured_via_ui ? "yes" : "no"}
            </p>
            <p>
              <strong>Env fallback:</strong>{" "}
              <code className="font-mono">
                {provider.client_id_env} / {provider.client_secret_env}
              </code>
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Config dialog
// ─────────────────────────────────────────────────────────────────

function McpOAuthProviderConfigDialog({
  provider,
  onClose,
  onSaved,
}: {
  provider: McpOAuthProvider | null;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}) {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (provider) {
      setClientId("");
      setClientSecret("");
      setShowSecret(false);
    }
  }, [provider]);

  if (!provider) return null;

  const handleSubmit = async () => {
    const cid = clientId.trim();
    const csec = clientSecret.trim();
    if (!cid || !csec) return;

    setSaving(true);
    try {
      const res = await fetch(
        `/api/mcp/oauth-providers/${provider.provider_key}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            client_id: cid,
            client_secret: csec,
          }),
        }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      showToast.success(
        "Credentials Saved",
        `${provider.display_name} OAuth client configured.`
      );
      await onSaved();
      onClose();
    } catch (err: any) {
      showToast.error(
        "Save Failed",
        err?.message || "Could not save credentials."
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !saving) onClose();
      }}
    >
      <div className="bg-card rounded-md shadow-2xl w-full max-w-lg border border-border">
        {/* Header */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
              <KeyRound size={16} />
            </div>
            <div>
              <h2 className="text-sm font-bold text-foreground">
                {provider.display_name} OAuth Client
              </h2>
              <p className="text-[11px] text-muted-foreground font-mono">
                {provider.provider_key}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4">
          <p className="text-[12px] text-muted-foreground leading-relaxed">
            {provider.display_name} does not support Dynamic Client
            Registration. Create an OAuth 2.0 Client (Web application) in
            the {provider.display_name} developer console, add the
            following redirect URI, then paste the credentials below.
          </p>
          <div className="bg-muted/40 border border-border rounded p-2">
            <code className="text-[11px] font-mono text-foreground block break-all">
              http://127.0.0.1:3088/api/mcp/oauth/callback
            </code>
          </div>

          <div>
            <label className="text-[10px] font-semibold text-foreground block mb-1">
              Client ID
            </label>
            <input
              type="text"
              value={clientId}
              onChange={(e) => setClientId(e.target.value)}
              placeholder={
                provider.configured_via_ui
                  ? "Enter new client ID to replace stored one"
                  : "e.g. 123456789-xxxx.apps.googleusercontent.com"
              }
              className="w-full px-3 py-2 bg-background border border-border rounded-md text-xs font-mono focus:outline-none focus:border-primary"
            />
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-[10px] font-semibold text-foreground">
                Client Secret
              </label>
              <button
                type="button"
                onClick={() => setShowSecret(!showSecret)}
                className="text-[10px] text-muted-foreground hover:text-foreground"
              >
                {showSecret ? "Hide" : "Show"}
              </button>
            </div>
            <input
              type={showSecret ? "text" : "password"}
              value={clientSecret}
              onChange={(e) => setClientSecret(e.target.value)}
              placeholder="GOCSPX-..."
              className="w-full px-3 py-2 bg-background border border-border rounded-md text-xs font-mono focus:outline-none focus:border-primary"
            />
          </div>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-border flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="px-3.5 py-1.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={saving || !clientId.trim() || !clientSecret.trim()}
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:opacity-90 disabled:opacity-50"
          >
            {saving ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                Saving…
              </>
            ) : (
              <>
                <Save size={13} />
                Save Credentials
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
