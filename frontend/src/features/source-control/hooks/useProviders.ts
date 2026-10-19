// frontend/src/features/source-control/hooks/useProviders.ts

import { useState, useEffect, useCallback, useRef } from "react";
import { sourceControlClient, SourceControlApiError } from "../api/source-control-client";
import { ProviderDescriptorResponse, ConnectionStatusResponse } from "../api/schemas";

/**
 * Custom hook for managing source control providers and OAuth connections.
 * Handles fetching provider metadata, initiating OAuth flows, polling for
 * connection status, saving configuration, and disconnecting providers.
 */
export function useProviders() {
  const [providers, setProviders] = useState<ProviderDescriptorResponse[]>([]);
  const [connections, setConnections] = useState<ConnectionStatusResponse[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Tracks which provider is currently in the process of connecting
  const [connectingProvider, setConnectingProvider] = useState<string | null>(null);

  // Tracks which provider is currently saving its OAuth configuration
  const [savingConfigProvider, setSavingConfigProvider] = useState<string | null>(null);

  // Ref to hold the polling interval ID for cleanup
  const pollingIntervalRef = useRef<NodeJS.Timeout | null>(null);

  /**
   * Fetches both the available providers and their current connection statuses.
   */
  const fetchProvidersAndConnections = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [providersData, connectionsData] = await Promise.all([
        sourceControlClient.listProviders(),
        sourceControlClient.listConnections(),
      ]);
      setProviders(providersData);
      setConnections(connectionsData);
    } catch (err) {
      if (err instanceof SourceControlApiError) {
        setError(err.detail);
      } else {
        setError("Failed to load provider data. Please check your connection.");
      }
    } finally {
      setIsLoading(false);
    }
  }, []);

  // Fetch on mount
  useEffect(() => {
    fetchProvidersAndConnections();
  }, [fetchProvidersAndConnections]);

  /**
   * Saves the user's custom OAuth credentials for a specific provider.
   * Returns true if successful, false otherwise.
   */
  const saveProviderConfig = useCallback(async (
    providerId: string,
    clientId: string,
    clientSecret: string
  ): Promise<boolean> => {
    setSavingConfigProvider(providerId);
    setError(null);

    try {
      await sourceControlClient.saveProviderConfig(providerId, {
        client_id: clientId,
        client_secret: clientSecret,
      });

      // Refresh the providers list to update the `is_configured` flag
      const updatedProviders = await sourceControlClient.listProviders();
      setProviders(updatedProviders);

      return true;
    } catch (err) {
      if (err instanceof SourceControlApiError) {
        setError(err.detail);
      } else {
        setError("Failed to save configuration. Please verify the keys and try again.");
      }
      return false;
    } finally {
      setSavingConfigProvider(null);
    }
  }, []);

  /**
   * Deletes the configuration for a specific provider.
   */
  const deleteProviderConfig = useCallback(async (providerId: string): Promise<boolean> => {
    setSavingConfigProvider(providerId);
    setError(null);
    try {
      await sourceControlClient.deleteProviderConfig(providerId);
      const updatedProviders = await sourceControlClient.listProviders();
      setProviders(updatedProviders);
      return true;
    } catch (err) {
      if (err instanceof SourceControlApiError) {
        setError(err.detail);
      } else {
        setError("Failed to delete configuration.");
      }
      return false;
    } finally {
      setSavingConfigProvider(null);
    }
  }, []);

  /**
   * Initiates the OAuth PKCE flow for a specific provider.
   * Opens the system browser and polls the backend until the connection is established.
   */
  const connectProvider = useCallback(async (providerId: string) => {
    setConnectingProvider(providerId);
    setError(null);

    try {
      // 1. Ask backend to generate the OAuth URL
      const response = await sourceControlClient.beginConnection(providerId, {
        // Redirect URI is omitted here; the backend uses the default loopback callback
      });

      // 2. Open the system browser for the user to authenticate
      window.open(response.authorization_url, "_blank", "noopener,noreferrer");

      // 3. Poll the backend to detect when the callback is received and connection is active
      let attempts = 0;
      const maxAttempts = 60; // 60 * 2s = 120 seconds timeout

      if (pollingIntervalRef.current) clearInterval(pollingIntervalRef.current);

      pollingIntervalRef.current = setInterval(async () => {
        attempts++;
        try {
          const updatedConnections = await sourceControlClient.listConnections();
          setConnections(updatedConnections);

          const isConnected = updatedConnections.some(
            (c) => c.provider === providerId && c.is_connected
          );

          if (isConnected) {
            if (pollingIntervalRef.current) clearInterval(pollingIntervalRef.current);
            setConnectingProvider(null);
            // Refresh providers to update their connected status as well
            const updatedProviders = await sourceControlClient.listProviders();
            setProviders(updatedProviders);
          } else if (attempts >= maxAttempts) {
            if (pollingIntervalRef.current) clearInterval(pollingIntervalRef.current);
            setConnectingProvider(null);
            setError("OAuth timeout. Please try connecting again.");
          }
        } catch (pollErr) {
          // Silently ignore polling errors to avoid flashing error messages
        }
      }, 2000); // Poll every 2 seconds

    } catch (err) {
      setConnectingProvider(null);
      if (err instanceof SourceControlApiError) {
        setError(err.detail);
      } else {
        setError("Failed to initiate OAuth flow.");
      }
    }
  }, []);

  /**
   * Disconnects a provider and revokes upstream tokens if requested.
   */
  const disconnectProvider = useCallback(async (providerId: string, revokeUpstream: boolean = true) => {
    setError(null);
    try {
      await sourceControlClient.disconnectProvider(providerId, {
        revoke_upstream_token: revokeUpstream,
      });
      // Refresh state after successful disconnect
      await fetchProvidersAndConnections();
    } catch (err) {
      if (err instanceof SourceControlApiError) {
        setError(err.detail);
      } else {
        setError("Failed to disconnect provider.");
      }
      throw err; // Re-throw so UI components can close their dialogs if needed
    }
  }, [fetchProvidersAndConnections]);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollingIntervalRef.current) clearInterval(pollingIntervalRef.current);
    };
  }, []);

  return {
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
    refresh: fetchProvidersAndConnections,
  };
}
