// frontend/src/app/integrations/page.tsx

"use client";

import React, { useState } from "react";
import { useProviders } from "../../features/source-control/hooks/useProviders";
import { ProviderConnectionCard } from "../../features/source-control/components/ProviderConnectionCard";
import { ProviderConnectDialog } from "../../features/source-control/components/ProviderConnectDialog";
import { DisconnectProviderDialog } from "../../features/source-control/components/DisconnectProviderDialog";
import { ProviderConfigDialog } from "../../features/source-control/components/ProviderConfigDialog";
import { ProviderDescriptorResponse } from "../../features/source-control/api/schemas";
import { McpOAuthProvidersSection } from "../../features/mcp-oauth/McpOAuthProvidersSection";

/**
 * The Provider Hub page.
 * This is the central location for managing external source control connections.
 * Users can configure OAuth credentials, connect, validate, and disconnect providers.
 */
export default function IntegrationsPage() {
  const {
    providers,
    connections,
    isLoading,
    error,
    connectingProvider,
    savingConfigProvider,
    connectProvider,
    disconnectProvider,
    saveProviderConfig,
    deleteProviderConfig,
    refresh,
  } = useProviders();

  // --- Local State for Dialogs ---
  const [providerToConnect, setProviderToConnect] = useState<ProviderDescriptorResponse | null>(null);
  const [providerToDisconnect, setProviderToDisconnect] = useState<ProviderDescriptorResponse | null>(null);
  const [providerToConfigure, setProviderToConfigure] = useState<ProviderDescriptorResponse | null>(null);

  // Local state to track disconnect loading
  const [isDisconnecting, setIsDisconnecting] = useState(false);

  // --- Handlers ---

  const handleOpenConnect = (provider: ProviderDescriptorResponse) => {
    setProviderToConnect(provider);
  };

  const handleConfirmConnect = async () => {
    if (!providerToConnect) return;
    await connectProvider(providerToConnect.provider_id);
    if (!error) {
       setProviderToConnect(null);
    }
  };

  const handleOpenDisconnect = (providerId: string) => {
    const provider = providers.find((p) => p.provider_id === providerId);
    if (provider) setProviderToDisconnect(provider);
  };

  const handleConfirmDisconnect = async (revokeUpstream: boolean) => {
    if (!providerToDisconnect) return;
    setIsDisconnecting(true);
    try {
      await disconnectProvider(providerToDisconnect.provider_id, revokeUpstream);
      setProviderToDisconnect(null);
    } catch (err) {
      // Error handled by the hook
    } finally {
      setIsDisconnecting(false);
    }
  };

  const handleOpenConfigure = (provider: ProviderDescriptorResponse) => {
    setProviderToConfigure(provider);
  };

  const handleDeleteConfig = async (): Promise<boolean> => {
    if (!providerToConfigure) return false;
    const success = await deleteProviderConfig(providerToConfigure.provider_id);
    if (success) {
      setProviderToConfigure(null);
    }
    return success;
  };

  const handleSaveConfig = async (clientId: string, clientSecret: string): Promise<boolean> => {
    if (!providerToConfigure) return false;

    const success = await saveProviderConfig(providerToConfigure.provider_id, clientId, clientSecret);
    if (success) {
      setProviderToConfigure(null);
    }
    return success;
  };



    // --- Render ---

  return (
    <div className="w-full h-full overflow-y-auto">
      <div className="w-full max-w-[1400px] mx-auto py-10 px-4 sm:px-6 lg:px-8">
      {/* ── Page Header ──────────────────────────────────────── */}
      <div className="mb-10 pb-6 border-b border-gray-200">
        <h1 className="text-3xl font-bold tracking-tight text-gray-900">
          Integrations & Providers
        </h1>
        <p className="mt-2 text-sm text-gray-500 max-w-2xl">
          Manage your external source control connections and shared
          OAuth clients. PELDRUN runs locally, so you must provide your
          own OAuth App credentials. Every remote write still requires
          manual approval.
        </p>
      </div>

      {/* ── Section 1: Source Control Providers ──────────────── */}
      <section className="mb-16">
        <div className="mb-6">
          <h2 className="text-xl font-bold tracking-tight text-gray-900">
            Source Control Providers
          </h2>
          <p className="mt-1 text-sm text-gray-500 max-w-2xl">
            Connect PELDRUN to external code hosting platforms. Every
            remote write is gated by manual approval.
          </p>
        </div>

        {/* Error Banner — scoped to source control so it does not
            overshadow the MCP section below. */}
        {error && (
          <div className="mb-6 rounded-md bg-red-50 p-4">
            <div className="flex">
              <div className="flex-shrink-0">
                <svg className="h-5 w-5 text-red-400" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                </svg>
              </div>
              <div className="ml-3">
                <h3 className="text-sm font-medium text-red-800">Connection Error</h3>
                <div className="mt-2 text-sm text-red-700">
                  <p>{error}</p>
                </div>
                <div className="mt-4">
                  <button
                    type="button"
                    onClick={refresh}
                    className="rounded-md bg-red-50 px-2 py-1.5 text-sm font-medium text-red-800 hover:bg-red-100 focus:outline-none focus:ring-2 focus:ring-red-600 focus:ring-offset-2 focus:ring-offset-red-50"
                  >
                    Try Again
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Loading / Grid / Empty — all three states live inside
            the same section so ordering stays consistent. */}
        {isLoading && !providers.length ? (
          <div className="flex justify-center py-12">
            <svg className="h-8 w-8 animate-spin text-blue-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
          </div>
        ) : providers.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {providers.map((provider) => {
              const connection = connections.find((c) => c.provider === provider.provider_id);
              return (
                <ProviderConnectionCard
                  key={provider.provider_id}
                  provider={provider}
                  connection={connection}
                  isConnecting={connectingProvider === provider.provider_id}
                  onConnect={() => handleOpenConnect(provider)}
                  onDisconnect={handleOpenDisconnect}
                  onValidate={(id) => console.log("Validate:", id)}
                  onConfigure={handleOpenConfigure}
                />
              );
            })}
          </div>
        ) : (
          <div className="text-center py-12 bg-gray-50 rounded-lg border border-dashed border-gray-300">
            <p className="text-gray-500">
              No source control providers are currently available in the system.
            </p>
          </div>
        )}
      </section>

      {/* ── Section 2: MCP OAuth Providers ───────────────────── */}
      <section className="pt-10 border-t border-gray-200">
        <div className="mb-6">
          <h2 className="text-xl font-bold tracking-tight text-gray-900">
            MCP OAuth Providers
          </h2>
          <p className="mt-1 text-sm text-gray-500 max-w-2xl">
            Pre-registered OAuth clients used by MCP servers that do not
            support Dynamic Client Registration (Google Workspace, etc.).
            Configure each provider once — every MCP server from that
            provider inherits the credentials automatically.
          </p>
        </div>
        <McpOAuthProvidersSection />
      </section>

      {/* ── Dialogs ──────────────────────────────────────────── */}
      <ProviderConnectDialog
        isOpen={!!providerToConnect}
        provider={providerToConnect}
        isConnecting={connectingProvider === providerToConnect?.provider_id}
        onClose={() => setProviderToConnect(null)}
        onConfirm={handleConfirmConnect}
      />

      <DisconnectProviderDialog
        isOpen={!!providerToDisconnect}
        providerName={providerToDisconnect?.display_name || ""}
        isDisconnecting={isDisconnecting}
        onClose={() => setProviderToDisconnect(null)}
        onConfirm={handleConfirmDisconnect}
      />

      <ProviderConfigDialog
        isOpen={!!providerToConfigure}
        provider={providerToConfigure}
        isSaving={savingConfigProvider === providerToConfigure?.provider_id}
        onClose={() => setProviderToConfigure(null)}
        onSave={handleSaveConfig}
        onDelete={handleDeleteConfig}
        existingClientId={providerToConfigure?.is_configured ? "••••••••••••••••" : undefined}
      />
      </div>
    </div>
  );
}
