// frontend/src/app/stores/_hooks/useProviderActions.ts
/**
 * Action hook for the Provider Packages section (M9 / M10).
 *
 * Owns every mutation and lifecycle action the Providers UI needs:
 *
 *   • Per-item toggle (enable / disable).
 *   • Delete with confirmation.
 *   • Activate a specific installed version (rollback / roll-forward).
 *   • Open the details drawer (with on-demand manifest + health fetch).
 *   • Upload and install a new package from a ZIP.
 *   • Load and refresh the catalog.
 *   • Install a catalog item.
 *   • Manage CONNECTIONS per package (create / update / delete /
 *     enable / disable / activate / test).
 *   • Open the Configure modal for a package.
 *
 * Design principles
 * -----------------
 *
 * 1. **Optimistic where safe, reload where complex.**
 *    Enable/disable is a single boolean flip — optimistic update wins.
 *    Delete, activate, and install change several fields at once, so
 *    those trigger a full `reload()` on success.
 *
 * 2. **Per-action loading states.**
 *    Every long-running action has its own `xxxId: string | null`
 *    state so the UI can spin the exact card that was clicked.
 *
 * 3. **Structured error handling.**
 *    Every failure path runs through `formatProviderError()` so the
 *    toast shows `message [CODE]`.
 *
 * 4. **Catalog is fetched lazily.**
 *    ``handleLoadCatalog()`` is called by the Providers section on
 *    first render. Between calls the result is cached.
 *
 * 5. **Connections are loaded lazily too.**
 *    ``handleLoadConnections(providerId)`` is called by the Configure
 *    modal on open. Kept separate from the provider list because
 *    not every card needs the connections immediately.
 */

"use client";

import { useCallback, useState } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import {
  activateConnection as apiActivateConnection,
  activateVersion as apiActivateVersion,
  createConnection as apiCreateConnection,
  deleteConnection as apiDeleteConnection,
  deleteProvider as apiDeleteProvider,
  disableConnection as apiDisableConnection,
  disableProvider as apiDisableProvider,
  enableConnection as apiEnableConnection,
  enableProvider as apiEnableProvider,
  formatProviderError,
  getProvider as apiGetProvider,
  getProviderHealth as apiGetProviderHealth,
  installFromCatalog as apiInstallFromCatalog,
  listCatalog as apiListCatalog,
  listConnections as apiListConnections,
  refreshCatalog as apiRefreshCatalog,
  testConnection as apiTestConnection,
  updateConnection as apiUpdateConnection,
  uploadProvider as apiUploadProvider,
  ProviderApiRequestError,
  type SaveConnectionPayload,
} from "@/lib/providerApi";
import type {
  ProviderCatalogItem,
  ProviderConnection,
  ProviderHealthReport,
  ProviderPackageEntry,
} from "../_types/store";


// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface UseProviderActionsArgs {
  /** The current providers list. */
  providers: ProviderPackageEntry[];
  /** Setter for the providers list. */
  setProviders: React.Dispatch<React.SetStateAction<ProviderPackageEntry[]>>;
  /** Full catalog reload. */
  reload: () => Promise<void>;
}

/**
 * Everything the details drawer needs to render.
 */
export interface ProviderDetailState {
  provider: ProviderPackageEntry;
  manifest: Record<string, unknown> | null;
  manifest_unavailable_reason?: string;
  health: ProviderHealthReport | null;
}

export interface UseProviderActionsResult {
  // ── Per-item loading states ────────────────────────────────────
  togglingProviderId: string | null;
  deletingProviderId: string | null;
  activatingProviderId: string | null;
  installingCatalogId: string | null;

  // ── List actions ───────────────────────────────────────────────
  handleToggleProvider: (providerId: string) => Promise<void>;
  handleDeleteProvider: (provider: ProviderPackageEntry) => Promise<void>;
  handleActivateVersion: (
    provider: ProviderPackageEntry,
    version: string,
  ) => Promise<void>;

  // ── Details drawer ─────────────────────────────────────────────
  detailsProvider: ProviderDetailState | null;
  isDetailsDrawerOpen: boolean;
  isDetailsLoading: boolean;
  detailsLoadingId: string | null;
  handleOpenDetails: (providerId: string) => Promise<void>;
  handleCloseDetails: () => void;

  // ── Upload modal ───────────────────────────────────────────────
  isUploadModalOpen: boolean;
  uploadSelectedFile: File | null;
  uploadInstallDependencies: boolean;
  isUploading: boolean;
  uploadError: string | null;
  setUploadSelectedFile: (file: File | null) => void;
  setUploadInstallDependencies: (value: boolean) => void;
  handleOpenUpload: () => void;
  handleCloseUpload: () => void;
  handleUploadSubmit: () => Promise<void>;

  // ── Catalog ────────────────────────────────────────────────────
  catalogItems: ProviderCatalogItem[];
  isCatalogLoading: boolean;
  isRefreshingCatalog: boolean;
  catalogError: string | null;
  handleLoadCatalog: () => Promise<void>;
  handleRefreshCatalog: () => Promise<void>;
  handleInstallFromCatalog: (itemId: string) => Promise<void>;

  // ── Configure modal + connections ──────────────────────────────
  /**
   * The provider package currently being configured, or ``null`` when
   * the Configure modal is closed.
   */
  configureProvider: ProviderPackageEntry | null;
  isConfigureModalOpen: boolean;
  handleOpenConfigure: (providerId: string) => void;
  handleCloseConfigure: () => void;

  /**
   * The saved connections for the package currently being configured.
   * Populated by ``handleLoadConnections`` when the Configure modal
   * opens; cleared when it closes.
   */
  connections: ProviderConnection[];
  isConnectionsLoading: boolean;
  connectionsError: string | null;
  /** ID of the connection currently being acted on (for per-row spinners). */
  connectionActionId: string | null;

  handleLoadConnections: (providerId: string) => Promise<void>;
  handleCreateConnection: (
    providerId: string,
    payload: SaveConnectionPayload,
  ) => Promise<ProviderConnection | null>;
  handleUpdateConnection: (
    providerId: string,
    connectionId: string,
    payload: SaveConnectionPayload,
  ) => Promise<ProviderConnection | null>;
  handleDeleteConnection: (
    providerId: string,
    connectionId: string,
  ) => Promise<boolean>;
  handleEnableConnection: (
    providerId: string,
    connectionId: string,
  ) => Promise<void>;
  handleDisableConnection: (
    providerId: string,
    connectionId: string,
  ) => Promise<void>;
  handleActivateConnection: (
    providerId: string,
    connectionId: string,
  ) => Promise<void>;
  handleTestConnection: (
    providerId: string,
    connectionId: string,
  ) => Promise<{
    ok: boolean;
    message: string;
    latency_ms: number;
  } | null>;
}


// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useProviderActions({
  providers,
  setProviders,
  reload,
}: UseProviderActionsArgs): UseProviderActionsResult {
  // ── Per-action loading states ────────────────────────────────────
  const [togglingProviderId, setTogglingProviderId] = useState<string | null>(
    null,
  );
  const [deletingProviderId, setDeletingProviderId] = useState<string | null>(
    null,
  );
  const [activatingProviderId, setActivatingProviderId] = useState<
    string | null
  >(null);
  const [installingCatalogId, setInstallingCatalogId] = useState<
    string | null
  >(null);

  // ── Details drawer state ─────────────────────────────────────────
  const [detailsProvider, setDetailsProvider] =
    useState<ProviderDetailState | null>(null);
  const [isDetailsLoading, setIsDetailsLoading] = useState(false);
  const [detailsLoadingId, setDetailsLoadingId] = useState<string | null>(
    null,
  );

  // ── Upload modal state ───────────────────────────────────────────
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const [uploadSelectedFile, setUploadSelectedFile] = useState<File | null>(
    null,
  );
  const [uploadInstallDependencies, setUploadInstallDependencies] =
    useState<boolean>(true);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // ── Catalog state ────────────────────────────────────────────────
  const [catalogItems, setCatalogItems] = useState<ProviderCatalogItem[]>([]);
  const [isCatalogLoading, setIsCatalogLoading] = useState(false);
  const [isRefreshingCatalog, setIsRefreshingCatalog] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  // ── Configure modal + connections state ──────────────────────────
  const [configureProvider, setConfigureProvider] =
    useState<ProviderPackageEntry | null>(null);
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [isConnectionsLoading, setIsConnectionsLoading] = useState(false);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [connectionActionId, setConnectionActionId] = useState<string | null>(
    null,
  );

  // ═══════════════════════════════════════════════════════════════════
  // List actions
  // ═══════════════════════════════════════════════════════════════════

  /**
   * Toggle a provider package's enabled state.
   *
   * Optimistic: the switch flips immediately; the state is rolled back
   * only on failure.
   */
  const handleToggleProvider = useCallback(
    async (providerId: string) => {
      const current = providers.find((p) => p.id === providerId);
      if (!current) {
        showToast.error(
          "Toggle Failed",
          `Provider "${providerId}" not found in the current list.`,
        );
        return;
      }

      const wasEnabled = current.enabled;
      setTogglingProviderId(providerId);

      // Optimistic flip.
      setProviders((prev) =>
        prev.map((p) =>
          p.id === providerId ? { ...p, enabled: !wasEnabled } : p,
        ),
      );

      try {
        const updated = wasEnabled
          ? await apiDisableProvider(providerId)
          : await apiEnableProvider(providerId);

        setProviders((prev) =>
          prev.map((p) => (p.id === providerId ? updated : p)),
        );
        showToast.success(
          wasEnabled ? "Provider Disabled" : "Provider Enabled",
          `${updated.display_name || updated.name || providerId} is now ${
            updated.enabled ? "enabled" : "disabled"
          }.`,
        );
      } catch (err) {
        // Rollback the optimistic flip.
        setProviders((prev) =>
          prev.map((p) =>
            p.id === providerId ? { ...p, enabled: wasEnabled } : p,
          ),
        );
        showToast.error("Toggle Failed", formatProviderError(err));
      } finally {
        setTogglingProviderId(null);
      }
    },
    [providers, setProviders],
  );

  /**
   * Delete a provider package with confirmation.
   */
  const handleDeleteProvider = useCallback(
    async (provider: ProviderPackageEntry) => {
      const label = provider.display_name || provider.name || provider.id;
      const versions = provider.installed_versions.length;
      const hasMultipleVersions = versions > 1;

      const confirmed = window.confirm(
        hasMultipleVersions
          ? `Delete ALL ${versions} versions of provider "${label}"? This removes every installed version, every isolated venv, and the registry entry. This cannot be undone.`
          : `Delete provider "${label}" v${provider.active_version}? This removes the installed files, its isolated venv, and the registry entry. This cannot be undone.`,
      );
      if (!confirmed) return;

      setDeletingProviderId(provider.id);
      try {
        await apiDeleteProvider(provider.id);
        await reload();

        showToast.success(
          "Provider Deleted",
          `${label} has been removed.`,
        );

        setDetailsProvider((prev) =>
          prev && prev.provider.id === provider.id ? null : prev,
        );
      } catch (err) {
        showToast.error("Delete Failed", formatProviderError(err));
      } finally {
        setDeletingProviderId(null);
      }
    },
    [reload],
  );

  /**
   * Switch the active version to a previously installed one.
   */
  const handleActivateVersion = useCallback(
    async (provider: ProviderPackageEntry, version: string) => {
      if (version === provider.active_version) {
        showToast.info(
          "Already Active",
          `${provider.display_name || provider.name || provider.id} v${version} is already the active version.`,
        );
        return;
      }

      setActivatingProviderId(provider.id);
      try {
        await apiActivateVersion(provider.id, version);
        await reload();

        showToast.success(
          "Version Activated",
          `${provider.display_name || provider.name || provider.id} → v${version}`,
        );

        if (
          detailsProvider &&
          detailsProvider.provider.id === provider.id
        ) {
          void handleOpenDetails(provider.id);
        }
      } catch (err) {
        showToast.error("Activation Failed", formatProviderError(err));
      } finally {
        setActivatingProviderId(null);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [reload, detailsProvider],
  );

  // ═══════════════════════════════════════════════════════════════════
  // Details drawer
  // ═══════════════════════════════════════════════════════════════════

  const handleOpenDetails = useCallback(async (providerId: string) => {
    setDetailsLoadingId(providerId);
    setIsDetailsLoading(true);

    try {
      const [detail, health] = await Promise.all([
        apiGetProvider(providerId, true),
        apiGetProviderHealth(providerId).catch((err) => {
          console.warn(
            "[useProviderActions] Health fetch failed for",
            providerId,
            ":",
            err,
          );
          return null;
        }),
      ]);

      setDetailsProvider({
        provider: detail.provider,
        manifest: detail.manifest ?? null,
        manifest_unavailable_reason:
          detail.manifest_unavailable_reason,
        health,
      });
    } catch (err) {
      showToast.error(
        "Failed to Load Details",
        formatProviderError(err),
      );
      setDetailsProvider(null);
    } finally {
      setIsDetailsLoading(false);
      setDetailsLoadingId(null);
    }
  }, []);

  const handleCloseDetails = useCallback(() => {
    setDetailsProvider(null);
  }, []);

  // ═══════════════════════════════════════════════════════════════════
  // Upload modal
  // ═══════════════════════════════════════════════════════════════════

  const handleOpenUpload = useCallback(() => {
    setUploadSelectedFile(null);
    setUploadInstallDependencies(true);
    setUploadError(null);
    setIsUploadModalOpen(true);
  }, []);

  const handleCloseUpload = useCallback(() => {
    if (isUploading) return;
    setIsUploadModalOpen(false);
    setUploadSelectedFile(null);
    setUploadError(null);
  }, [isUploading]);

  const handleUploadSubmit = useCallback(async () => {
    if (!uploadSelectedFile) {
      setUploadError("Please select a .zip file to upload.");
      return;
    }

    setUploadError(null);
    setIsUploading(true);

    try {
      const result = await apiUploadProvider(
        uploadSelectedFile,
        uploadInstallDependencies,
      );

      setIsUploadModalOpen(false);
      setUploadSelectedFile(null);
      setUploadError(null);
      await reload();

      showToast.success(
        "Provider Installed",
        `${result.display_name || result.name || result.id} v${result.version} is now available.`,
      );

      if (Array.isArray(result.warnings)) {
        for (const w of result.warnings) {
          if (typeof w === "string" && w.trim()) {
            showToast.info("Install Notice", w);
          }
        }
      }

      void handleLoadCatalog();
    } catch (err) {
      const message = formatProviderError(err);
      setUploadError(message);
      showToast.error("Install Failed", message);

      if (err instanceof ProviderApiRequestError) {
        console.warn(
          "[useProviderActions] Upload error details:",
          err.code,
          err.details,
        );
      }
    } finally {
      setIsUploading(false);
    }
  }, [uploadSelectedFile, uploadInstallDependencies, reload]);

  // ═══════════════════════════════════════════════════════════════════
  // Catalog
  // ═══════════════════════════════════════════════════════════════════

  const handleLoadCatalog = useCallback(async () => {
    setIsCatalogLoading(true);
    setCatalogError(null);

    try {
      const items = await apiListCatalog();
      setCatalogItems(items);
    } catch (err) {
      const message = formatProviderError(err);
      setCatalogError(message);
      console.warn("[useProviderActions] Catalog load failed:", err);
    } finally {
      setIsCatalogLoading(false);
    }
  }, []);

  const handleRefreshCatalog = useCallback(async () => {
    setIsRefreshingCatalog(true);
    try {
      const result = await apiRefreshCatalog();
      await handleLoadCatalog();
      showToast.success(
        "Catalog Refreshed",
        result.message ||
          `Catalog reloaded (${result.total} item${
            result.total === 1 ? "" : "s"
          }).`,
      );
    } catch (err) {
      showToast.error("Refresh Failed", formatProviderError(err));
    } finally {
      setIsRefreshingCatalog(false);
    }
  }, [handleLoadCatalog]);

  const handleInstallFromCatalog = useCallback(
    async (itemId: string) => {
      const item = catalogItems.find((c) => c.id === itemId);
      if (!item) {
        showToast.error(
          "Install Failed",
          `Catalog item "${itemId}" not found.`,
        );
        return;
      }

      if (!item.installable) {
        showToast.warning(
          "Not Installable",
          item.installability_reason ||
            "This catalog item is preview-only.",
        );
        return;
      }

      const warningsBlock =
        item.permission_warnings.length > 0
          ? "\n\nThis package will:\n" +
            item.permission_warnings.map((w) => `• ${w}`).join("\n")
          : "";

      const confirmed = window.confirm(
        `Install "${item.display_name || item.name || item.id}" v${item.version}?${warningsBlock}`,
      );
      if (!confirmed) return;

      setInstallingCatalogId(itemId);
      try {
        const result = await apiInstallFromCatalog(itemId);

        await reload();
        void handleLoadCatalog();

        showToast.success(
          "Provider Installed",
          `${result.display_name || result.name || result.id} v${result.version} is now available.`,
        );

        if (Array.isArray(result.warnings)) {
          for (const w of result.warnings) {
            if (typeof w === "string" && w.trim()) {
              showToast.info("Install Notice", w);
            }
          }
        }
      } catch (err) {
        showToast.error("Install Failed", formatProviderError(err));
      } finally {
        setInstallingCatalogId(null);
      }
    },
    [catalogItems, reload, handleLoadCatalog],
  );

  // ═══════════════════════════════════════════════════════════════════
  // Configure modal + connections
  // ═══════════════════════════════════════════════════════════════════

  /**
   * Load every saved connection for a package.
   *
   * Called by the Configure modal on open, and after every mutation
   * that changes the list (create / delete).
   */
  const handleLoadConnections = useCallback(
    async (providerId: string) => {
      setIsConnectionsLoading(true);
      setConnectionsError(null);
      try {
        const list = await apiListConnections(providerId);
        setConnections(list);
      } catch (err) {
        const message = formatProviderError(err);
        setConnectionsError(message);
        console.warn(
          "[useProviderActions] Failed to load connections for",
          providerId,
          ":",
          err,
        );
      } finally {
        setIsConnectionsLoading(false);
      }
    },
    [],
  );

  /**
   * Open the Configure modal for a provider package.
   *
   * Kicks off a background load of the package's connections so the
   * modal shows the current state as soon as it renders. The load is
   * fire-and-forget — the modal handles ``isConnectionsLoading``.
   */
  const handleOpenConfigure = useCallback(
    (providerId: string) => {
      const provider = providers.find((p) => p.id === providerId);
      if (!provider) {
        showToast.error(
          "Configure Failed",
          `Provider "${providerId}" not found.`,
        );
        return;
      }
      setConfigureProvider(provider);
      setConnections([]);
      setConnectionsError(null);
      void handleLoadConnections(providerId);
    },
    [providers, handleLoadConnections],
  );

  const handleCloseConfigure = useCallback(() => {
    if (connectionActionId !== null) return; // refuse to close mid-action
    setConfigureProvider(null);
    setConnections([]);
    setConnectionsError(null);
  }, [connectionActionId]);

  /**
   * Create a new connection and refresh the list.
   *
   * Returns the saved connection (or ``null`` on failure), which the
   * modal uses to optionally select the new row.
   */
  const handleCreateConnection = useCallback(
    async (
      providerId: string,
      payload: SaveConnectionPayload,
    ): Promise<ProviderConnection | null> => {
      setConnectionActionId("__new__");
      try {
        const saved = await apiCreateConnection(providerId, payload);
        await handleLoadConnections(providerId);
        await reload();
        showToast.success(
          "Connection Saved",
          `"${saved.name}" is ready to use.`,
        );
        return saved;
      } catch (err) {
        showToast.error("Save Failed", formatProviderError(err));
        return null;
      } finally {
        setConnectionActionId(null);
      }
    },
    [handleLoadConnections, reload],
  );

  const handleUpdateConnection = useCallback(
    async (
      providerId: string,
      connectionId: string,
      payload: SaveConnectionPayload,
    ): Promise<ProviderConnection | null> => {
      setConnectionActionId(connectionId);
      try {
        const saved = await apiUpdateConnection(
          providerId,
          connectionId,
          payload,
        );
        await handleLoadConnections(providerId);
        await reload();
        showToast.success(
          "Connection Updated",
          `"${saved.name}" has been saved.`,
        );
        return saved;
      } catch (err) {
        showToast.error("Update Failed", formatProviderError(err));
        return null;
      } finally {
        setConnectionActionId(null);
      }
    },
    [handleLoadConnections, reload],
  );

  const handleDeleteConnection = useCallback(
    async (
      providerId: string,
      connectionId: string,
    ): Promise<boolean> => {
      setConnectionActionId(connectionId);
      try {
        await apiDeleteConnection(providerId, connectionId);
        await handleLoadConnections(providerId);
        await reload();
        showToast.success("Connection Deleted", "The connection was removed.");
        return true;
      } catch (err) {
        showToast.error("Delete Failed", formatProviderError(err));
        return false;
      } finally {
        setConnectionActionId(null);
      }
    },
    [handleLoadConnections, reload],
  );

  const handleEnableConnection = useCallback(
    async (providerId: string, connectionId: string) => {
      setConnectionActionId(connectionId);
      try {
        await apiEnableConnection(providerId, connectionId);
        await handleLoadConnections(providerId);
        await reload();
        showToast.success("Connection Enabled", "Ready to use.");
      } catch (err) {
        showToast.error("Enable Failed", formatProviderError(err));
      } finally {
        setConnectionActionId(null);
      }
    },
    [handleLoadConnections, reload],
  );

  const handleDisableConnection = useCallback(
    async (providerId: string, connectionId: string) => {
      setConnectionActionId(connectionId);
      try {
        await apiDisableConnection(providerId, connectionId);
        await handleLoadConnections(providerId);
        await reload();
        showToast.success("Connection Disabled", "Will not be used.");
      } catch (err) {
        showToast.error("Disable Failed", formatProviderError(err));
      } finally {
        setConnectionActionId(null);
      }
    },
    [handleLoadConnections, reload],
  );

  const handleActivateConnection = useCallback(
    async (providerId: string, connectionId: string) => {
      setConnectionActionId(connectionId);
      try {
        await apiActivateConnection(providerId, connectionId);
        await handleLoadConnections(providerId);
        await reload();
        showToast.success(
          "Connection Activated",
          "This is now the active connection for the package.",
        );
      } catch (err) {
        showToast.error("Activate Failed", formatProviderError(err));
      } finally {
        setConnectionActionId(null);
      }
    },
    [handleLoadConnections, reload],
  );

  const handleTestConnection = useCallback(
    async (providerId: string, connectionId: string) => {
      setConnectionActionId(connectionId);
      try {
        const result = await apiTestConnection(providerId, connectionId);
        if (result.ok) {
          showToast.success(
            "Connection Verified",
            `Responded in ${result.latency_ms.toFixed(0)} ms`,
          );
        } else {
          showToast.warning(
            "Connection Failed",
            result.message || "Probe returned ok=false.",
          );
        }
        return {
          ok: result.ok,
          message: result.message,
          latency_ms: result.latency_ms,
        };
      } catch (err) {
        showToast.error("Test Failed", formatProviderError(err));
        return null;
      } finally {
        setConnectionActionId(null);
      }
    },
    [],
  );

  // ═══════════════════════════════════════════════════════════════════
  // Return
  // ═══════════════════════════════════════════════════════════════════

  return {
    // Per-item loading states
    togglingProviderId,
    deletingProviderId,
    activatingProviderId,
    installingCatalogId,

    // List actions
    handleToggleProvider,
    handleDeleteProvider,
    handleActivateVersion,

    // Details drawer
    detailsProvider,
    isDetailsDrawerOpen: detailsProvider !== null,
    isDetailsLoading,
    detailsLoadingId,
    handleOpenDetails,
    handleCloseDetails,

    // Upload modal
    isUploadModalOpen,
    uploadSelectedFile,
    uploadInstallDependencies,
    isUploading,
    uploadError,
    setUploadSelectedFile,
    setUploadInstallDependencies,
    handleOpenUpload,
    handleCloseUpload,
    handleUploadSubmit,

    // Catalog
    catalogItems,
    isCatalogLoading,
    isRefreshingCatalog,
    catalogError,
    handleLoadCatalog,
    handleRefreshCatalog,
    handleInstallFromCatalog,

    // Configure modal + connections
    configureProvider,
    isConfigureModalOpen: configureProvider !== null,
    handleOpenConfigure,
    handleCloseConfigure,
    connections,
    isConnectionsLoading,
    connectionsError,
    connectionActionId,
    handleLoadConnections,
    handleCreateConnection,
    handleUpdateConnection,
    handleDeleteConnection,
    handleEnableConnection,
    handleDisableConnection,
    handleActivateConnection,
    handleTestConnection,
  };
}
