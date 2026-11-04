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

import { useCallback, useRef, useState } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import {
  activateConnection as apiActivateConnection,
  activateVersion as apiActivateVersion,
  createConnection as apiCreateConnection,
  deleteCertificate as apiDeleteCertificate,
  deleteConnection as apiDeleteConnection,
  deleteProvider as apiDeleteProvider,
  disableConnection as apiDisableConnection,
  disableProvider as apiDisableProvider,
  enableConnection as apiEnableConnection,
  enableProvider as apiEnableProvider,
  formatProviderError,
  getInstallStatus as apiGetInstallStatus,
  getProvider as apiGetProvider,
  getProviderHealth as apiGetProviderHealth,
  installCertificate as apiInstallCertificate,
  installFromCatalog as apiInstallFromCatalog,
  listCatalog as apiListCatalog,
  listCertificates as apiListCertificates,
  listConnections as apiListConnections,
  refreshCatalog as apiRefreshCatalog,
  testCertificate as apiTestCertificate,
  testConnection as apiTestConnection,
  updateConnection as apiUpdateConnection,
  uploadProvider as apiUploadProvider,
  ProviderApiRequestError,
  type InstallJobStatus,
  type SaveConnectionPayload,
} from "@/lib/providerApi";
import type {
  CertificateStatusReport,
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

  /**
   * Live state of the current install job, or ``null`` when no job is
   * active.
   *
   * Populated by the polling loop that starts after a successful
   * upload. The modal reads ``installJobStatus.logs`` to render a
   * live progress timeline, and ``installJobStatus.status`` to know
   * when the job has finished.
   *
   * Cleared when the modal closes after a successful install, or when
   * ``handleOpenUpload()`` is called to start a new one. It is KEPT
   * on failure so the user can inspect the logs before retrying.
   */
  installJobStatus: InstallJobStatus | null;

  /**
   * The catalog item currently being installed, or ``null``.
   * Drives ``ProviderUploadModal``'s "catalog" mode.
   */
  catalogInstallItem: ProviderCatalogItem | null;

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

  // ── Certificate Manager modal ─────────────────────────────────
  //
  // A separate modal from the connection modal, opened from a
  // dedicated "Certificates" button on the provider card. It manages
  // TLS certificates (root CA bundles, mTLS client certificates)
  // declared by the package's manifest.
  //
  // Installation happens in the MAIN backend process (not the worker
  // subprocess) because the worker's network allowlist restricts
  // outbound requests to the connection's ``base_url`` host — the CA
  // host is different. See ``certificate_manager.py``.

  /**
   * The provider package whose certificate manager is open, or
   * ``null`` when the modal is closed.
   */
  certificateManagerProvider: ProviderPackageEntry | null;
  isCertificateModalOpen: boolean;
  handleOpenCertificateManager: (providerId: string) => void;
  handleCloseCertificateManager: () => void;

  /**
   * The declared certificates with their reconciled status, for the
   * currently open certificate manager.
   */
  certificates: CertificateStatusReport[];
  isLoadingCertificates: boolean;
  certificatesError: string | null;

  /**
   * Which certificate action is in flight, in
   * ``"<action>:<cert_id>"`` format. ``null`` when nothing is running.
   * The Certificate Manager modal uses this to show a spinner on the
   * exact button that was clicked.
   */
  certificateActionId: string | null;

  handleLoadCertificates: (providerId: string) => Promise<void>;
  handleRefreshCertificates: () => Promise<void>;
  handleInstallCertificate: (certificateId: string) => Promise<boolean>;
  handleTestCertificate: (certificateId: string) => Promise<boolean>;
  handleDeleteCertificate: (
    certificateId: string,
    deleteFile: boolean,
  ) => Promise<boolean>;
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

  // Live install job state.
  //
  // The upload endpoint returns immediately with a `job_id`; the
  // actual install (extract + venv + pip + register) runs in the
  // background. We poll `/install-status/{job_id}` every 1.5s and
  // mirror the result into `installJobStatus` so the modal can
  // render the log timeline live.
  const [installJobStatus, setInstallJobStatus] =
    useState<InstallJobStatus | null>(null);

  /**
   * The catalog item currently being installed, or ``null`` when no
   * catalog install is in flight. Drives ``ProviderUploadModal``'s
   * "catalog" mode (hides file picker, shows item name + version).
   * Cleared on success; preserved on failure so the user can read the
   * log before closing.
   */
  const [catalogInstallItem, setCatalogInstallItem] =
    useState<ProviderCatalogItem | null>(null);

  // Interval handle for the polling loop. Stored in a ref so it
  // survives re-renders without triggering them, and so we can
  // clear it from outside the polling closure (e.g. from
  // handleCloseUpload).
  const installPollingRef = useRef<number | null>(null);

  /**
   * Stop the polling loop, if one is active.
   *
   * Safe to call multiple times. Used in three places:
   *   1. From within the polling closure when the job reaches a
   *      terminal state (success or failed).
   *   2. From ``handleCloseUpload`` when the user closes the modal
   *      mid-install.
   *   3. From an unmount effect (added below) as a last-resort
   *      cleanup.
   */
  const stopInstallPolling = useCallback(() => {
    if (installPollingRef.current !== null) {
      window.clearInterval(installPollingRef.current);
      installPollingRef.current = null;
    }
  }, []);

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

  // ── Certificate Manager state ────────────────────────────────────
  const [certificateManagerProvider, setCertificateManagerProvider] =
    useState<ProviderPackageEntry | null>(null);
  const [certificates, setCertificates] = useState<CertificateStatusReport[]>(
    [],
  );
  const [isLoadingCertificates, setIsLoadingCertificates] = useState(false);
  const [certificatesError, setCertificatesError] = useState<string | null>(
    null,
  );
  const [certificateActionId, setCertificateActionId] = useState<
    string | null
  >(null);

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
  // Catalog
  // ═══════════════════════════════════════════════════════════════════

  /**
   * Load the catalog of installable packages.
   *
   * Defined BEFORE handleUploadSubmit because handleUploadSubmit's
   * dependency array references it. In JavaScript, a useCallback's
   * dependency array is evaluated at the moment useCallback runs —
   * if the referenced identifier is not yet initialized (Temporal
   * Dead Zone), the page crashes with a ReferenceError before any
   * render.
   */
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

      stopInstallPolling();
      setUploadError(null);
      setInstallJobStatus(null);
      setIsUploading(true);
      setInstallingCatalogId(itemId);
      setCatalogInstallItem(item);

      let jobId: string;
      try {
        const response = await apiInstallFromCatalog(itemId);
        jobId = response.job_id;
      } catch (err) {
        const message = formatProviderError(err);
        setUploadError(message);
        showToast.error("Install Failed", message);
        setIsUploading(false);
        setInstallingCatalogId(null);
        setCatalogInstallItem(null);
        return;
      }

      const MAX_POLL_DURATION_MS = 11 * 60 * 1000;
      const POLL_INTERVAL_MS = 1500;
      const MAX_CONSECUTIVE_ERRORS = 5;
      const pollStartedAt = Date.now();
      let consecutiveErrors = 0;

      const poll = async () => {
        if (installPollingRef.current === null) return;

        if (Date.now() - pollStartedAt > MAX_POLL_DURATION_MS) {
          stopInstallPolling();
          setUploadError(
            "Install is taking longer than expected. Check the backend logs.",
          );
          showToast.error(
            "Install Timed Out",
            "See backend logs for details.",
          );
          setIsUploading(false);
          setInstallingCatalogId(null);
          setCatalogInstallItem(null);
          return;
        }

        let job;
        try {
          job = await apiGetInstallStatus(jobId);
        } catch (err) {
          consecutiveErrors += 1;
          if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
            stopInstallPolling();
            const message = formatProviderError(err);
            setUploadError(
              `Lost contact with the backend while polling for install status: ${message}`,
            );
            showToast.error("Status Check Failed", message);
            setIsUploading(false);
            setInstallingCatalogId(null);
            setCatalogInstallItem(null);
          }
          return;
        }

        consecutiveErrors = 0;
        setInstallJobStatus(job);

        if (job.status === "success" && job.result) {
          stopInstallPolling();
          setInstallJobStatus(null);
          setUploadError(null);
          setIsUploading(false);
          setInstallingCatalogId(null);
          setCatalogInstallItem(null);

          await reload();
          void handleLoadCatalog();

          showToast.success(
            "Provider Installed",
            `${job.result.display_name || job.result.name || job.result.id} v${job.result.version} is now available.`,
          );

          if (Array.isArray(job.result.warnings)) {
            for (const w of job.result.warnings) {
              if (typeof w === "string" && w.trim()) {
                showToast.info("Install Notice", w);
              }
            }
          }
          return;
        }

        if (job.status === "failed") {
          stopInstallPolling();
          const message = job.error?.error || "Install failed.";
          setUploadError(message);
          showToast.error("Install Failed", message);
          setIsUploading(false);
          setInstallingCatalogId(null);
          // Preserve catalogInstallItem so the modal keeps rendering
          // the log — the user must read what went wrong first.
          return;
        }
      };

      void poll();
      installPollingRef.current = window.setInterval(
        () => void poll(),
        POLL_INTERVAL_MS,
      );
    },
    [catalogItems, reload, handleLoadCatalog, stopInstallPolling],
  );

  // ═══════════════════════════════════════════════════════════════════
  // Upload modal
  // ═══════════════════════════════════════════════════════════════════

  const handleOpenUpload = useCallback(() => {
    // Ensure any prior polling has stopped before opening a fresh
    // modal — otherwise a stale interval could update state from a
    // previous job after the modal reopens.
    stopInstallPolling();
    setUploadSelectedFile(null);
    setUploadInstallDependencies(true);
    setUploadError(null);
    setInstallJobStatus(null);
    setIsUploading(false);
    setIsUploadModalOpen(true);
  }, [stopInstallPolling]);

  const handleCloseUpload = useCallback(() => {
    // Refuse to close while the install is running — the backend
    // task will continue regardless, but hiding the modal would
    // give the user the impression that it stopped.
    if (isUploading) return;

    // Defensive: stop polling even though `isUploading === false`
    // implies it is already stopped. Guards against a future bug
    // where `isUploading` and the polling state could diverge.
    stopInstallPolling();

    setIsUploadModalOpen(false);
    setUploadSelectedFile(null);
    setUploadError(null);
    setInstallJobStatus(null);
    setCatalogInstallItem(null);
  }, [isUploading, stopInstallPolling]);

  const handleUploadSubmit = useCallback(async () => {
    if (!uploadSelectedFile) {
      setUploadError("Please select a .zip file to upload.");
      return;
    }

    // Reset any previous job state.
    stopInstallPolling();
    setUploadError(null);
    setInstallJobStatus(null);
    setIsUploading(true);

    let jobId: string;
    try {
      // Step 1 — upload the ZIP. This returns in <100ms with a
      // job_id; the actual install continues in the background.
      const uploadResult = await apiUploadProvider(
        uploadSelectedFile,
        uploadInstallDependencies,
      );
      jobId = uploadResult.job_id;
    } catch (err) {
      // The upload itself failed (network, bad ZIP, validation).
      // Nothing is running in the background.
      const message = formatProviderError(err);
      setUploadError(message);
      showToast.error("Upload Failed", message);
      setIsUploading(false);
      if (err instanceof ProviderApiRequestError) {
        console.warn(
          "[useProviderActions] Upload error details:",
          err.code,
          err.details,
        );
      }
      return;
    }

    // Step 2 — poll the job status until it reaches a terminal state.
    //
    // Two safety bounds:
    //   * consecutiveErrors — if we fail to poll 5 times in a row,
    //     assume the backend died and stop.
    //   * MAX_POLL_DURATION_MS — hard cap on total polling time.
    //     The backend has its own 10-minute pip timeout, so this
    //     bound (11 minutes) is just a safety net.
    const MAX_POLL_DURATION_MS = 11 * 60 * 1000;
    const POLL_INTERVAL_MS = 1500;
    const MAX_CONSECUTIVE_ERRORS = 5;
    const pollStartedAt = Date.now();
    let consecutiveErrors = 0;

    const poll = async () => {
      // Guard: the interval may fire after we already stopped polling
      // (race with stopInstallPolling). Bail early.
      if (installPollingRef.current === null) return;

      // Guard: hard deadline.
      if (Date.now() - pollStartedAt > MAX_POLL_DURATION_MS) {
        stopInstallPolling();
        setUploadError(
          "Install is taking longer than expected. Check the backend logs.",
        );
        showToast.error("Install Timed Out", "See backend logs for details.");
        setIsUploading(false);
        return;
      }

      let job;
      try {
        job = await apiGetInstallStatus(jobId);
      } catch (err) {
        consecutiveErrors += 1;
        if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
          stopInstallPolling();
          const message = formatProviderError(err);
          setUploadError(
            `Lost contact with the backend while polling for install status: ${message}`,
          );
          showToast.error("Status Check Failed", message);
          setIsUploading(false);
        }
        return;
      }

      // Reset the error counter on a successful poll.
      consecutiveErrors = 0;

      // Update the UI with the latest snapshot.
      setInstallJobStatus(job);

      if (job.status === "success" && job.result) {
        // Terminal success.
        stopInstallPolling();
        setInstallJobStatus(null);
        setUploadError(null);
        setIsUploadModalOpen(false);
        setUploadSelectedFile(null);
        setIsUploading(false);

        await reload();

        showToast.success(
          "Provider Installed",
          `${job.result.display_name || job.result.name || job.result.id} v${job.result.version} is now available.`,
        );

        if (Array.isArray(job.result.warnings)) {
          for (const w of job.result.warnings) {
            if (typeof w === "string" && w.trim()) {
              showToast.info("Install Notice", w);
            }
          }
        }

        void handleLoadCatalog();
        return;
      }

      if (job.status === "failed") {
        // Terminal failure. Keep the modal open with the logs and
        // the error message so the user can inspect what happened
        // and retry.
        stopInstallPolling();
        const message = job.error?.error || "Install failed.";
        setUploadError(message);
        showToast.error("Install Failed", message);
        setIsUploading(false);
        return;
      }

      // job.status === "running" — continue polling.
    };

    // First poll immediately so the modal populates without waiting
    // for the 1.5s interval.
    void poll();

    // Continue polling at a fixed interval.
    installPollingRef.current = window.setInterval(
      () => void poll(),
      POLL_INTERVAL_MS,
    );
  }, [
    uploadSelectedFile,
    uploadInstallDependencies,
    reload,
    handleLoadCatalog,
    stopInstallPolling,
  ]);

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

  // ═══════════════════════════════════════════════════════════════════
  // Certificate Manager
  // ═══════════════════════════════════════════════════════════════════

  /**
   * Load every declared certificate for a package, with its current
   * status.
   *
   * Called by ``handleOpenCertificateManager`` on modal open, and
   * after every mutation. Never throws — a fetch failure sets
   * ``certificatesError`` so the modal renders an inline error with
   * a retry button.
   */
  const handleLoadCertificates = useCallback(
    async (providerId: string) => {
      setIsLoadingCertificates(true);
      setCertificatesError(null);
      try {
        const list = await apiListCertificates(providerId);
        setCertificates(list);
      } catch (err) {
        const message = formatProviderError(err);
        setCertificatesError(message);
        console.warn(
          "[useProviderActions] Failed to load certificates for",
          providerId,
          ":",
          err,
        );
      } finally {
        setIsLoadingCertificates(false);
      }
    },
    [],
  );

  /**
   * Open the Certificate Manager modal for a provider package.
   *
   * Kicks off a background load of the declared certificates so the
   * modal shows the current state as soon as it renders. The load is
   * fire-and-forget — the modal handles ``isLoadingCertificates``.
   */
  const handleOpenCertificateManager = useCallback(
    (providerId: string) => {
      const provider = providers.find((p) => p.id === providerId);
      if (!provider) {
        showToast.error(
          "Certificates Unavailable",
          `Provider "${providerId}" not found.`,
        );
        return;
      }
      setCertificateManagerProvider(provider);
      setCertificates([]);
      setCertificatesError(null);
      void handleLoadCertificates(providerId);
    },
    [providers, handleLoadCertificates],
  );

  const handleCloseCertificateManager = useCallback(() => {
    if (certificateActionId !== null) return; // refuse to close mid-action
    setCertificateManagerProvider(null);
    setCertificates([]);
    setCertificatesError(null);
  }, [certificateActionId]);

  /**
   * Re-fetch the certificate list for the currently-open modal.
   *
   * Used by the "Refresh" button in the modal header. No-op when the
   * modal is closed.
   */
  const handleRefreshCertificates = useCallback(async () => {
    if (!certificateManagerProvider) return;
    await handleLoadCertificates(certificateManagerProvider.id);
  }, [certificateManagerProvider, handleLoadCertificates]);

  /**
   * Download, verify, and install a certificate.
   *
   * After success, reloads both the certificate list (to show the new
   * status) and the provider list (to update the card's
   * ``certificates_status`` badge).
   */
  const handleInstallCertificate = useCallback(
    async (certificateId: string): Promise<boolean> => {
      if (!certificateManagerProvider) return false;
      const providerId = certificateManagerProvider.id;

      setCertificateActionId(`install:${certificateId}`);
      try {
        await apiInstallCertificate(providerId, certificateId);
        await handleLoadCertificates(providerId);
        await reload();
        showToast.success(
          "Certificate Installed",
          "The certificate is now installed on the host.",
        );
        return true;
      } catch (err) {
        showToast.error(
          "Certificate Install Failed",
          formatProviderError(err),
        );
        // Reload anyway — a failed install leaves a "missing" or
        // "invalid" record in the vault that the user needs to see.
        await handleLoadCertificates(providerId);
        return false;
      } finally {
        setCertificateActionId(null);
      }
    },
    [certificateManagerProvider, handleLoadCertificates, reload],
  );

  /**
   * Re-check the status of an installed certificate without
   * re-downloading.
   *
   * Uses the resulting status to pick the toast kind:
   *   - installed → success
   *   - invalid   → warning (with the error message)
   *   - missing   → warning (with the error message)
   *   - expired   → warning
   */
  const handleTestCertificate = useCallback(
    async (certificateId: string): Promise<boolean> => {
      if (!certificateManagerProvider) return false;
      const providerId = certificateManagerProvider.id;

      setCertificateActionId(`test:${certificateId}`);
      try {
        const report = await apiTestCertificate(providerId, certificateId);
        await handleLoadCertificates(providerId);

        const ok = report.status === "installed";
        if (ok) {
          showToast.success(
            "Certificate Valid",
            "The certificate file is present, hash matches, and format is valid.",
          );
        } else {
          showToast.warning(
            "Certificate Problem",
            report.error_message ||
              `Status: ${report.status}. Reinstall the certificate.`,
          );
        }
        return ok;
      } catch (err) {
        showToast.error(
          "Certificate Test Failed",
          formatProviderError(err),
        );
        return false;
      } finally {
        setCertificateActionId(null);
      }
    },
    [certificateManagerProvider, handleLoadCertificates],
  );

  /**
   * Delete a certificate record and (optionally) its file from disk.
   *
   * After success, reloads both the certificate list and the provider
   * list (the card's badge may change).
   */
  const handleDeleteCertificate = useCallback(
    async (
      certificateId: string,
      deleteFile: boolean,
    ): Promise<boolean> => {
      if (!certificateManagerProvider) return false;
      const providerId = certificateManagerProvider.id;

      setCertificateActionId(`delete:${certificateId}`);
      try {
        const result = await apiDeleteCertificate(
          providerId,
          certificateId,
          deleteFile,
        );
        await handleLoadCertificates(providerId);
        await reload();

        if (result.removed) {
          showToast.success(
            "Certificate Removed",
            deleteFile
              ? "The certificate file and record have been removed."
              : "The certificate record has been removed. The file was preserved.",
          );
        } else {
          showToast.info(
            "Nothing to Remove",
            "No installed record existed for this certificate.",
          );
        }
        return true;
      } catch (err) {
        showToast.error(
          "Certificate Delete Failed",
          formatProviderError(err),
        );
        return false;
      } finally {
        setCertificateActionId(null);
      }
    },
    [certificateManagerProvider, handleLoadCertificates, reload],
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
    installJobStatus,
    catalogInstallItem,

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

    // Certificate Manager
    certificateManagerProvider,
    isCertificateModalOpen: certificateManagerProvider !== null,
    handleOpenCertificateManager,
    handleCloseCertificateManager,
    certificates,
    isLoadingCertificates,
    certificatesError,
    certificateActionId,
    handleLoadCertificates,
    handleRefreshCertificates,
    handleInstallCertificate,
    handleTestCertificate,
    handleDeleteCertificate,
  };
}
