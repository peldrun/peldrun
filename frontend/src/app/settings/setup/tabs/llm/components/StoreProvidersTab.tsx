// frontend/src/app/settings/setup/tabs/llm/components/StoreProvidersTab.tsx
"use client";

import React, { useState } from "react";
import { motion } from "framer-motion";
import { AlertCircle, Loader2, Package as PackageIcon } from "lucide-react";
import {
  ModelDiscoveryModal,
  type ModelsDiscoverySavePayload,
} from "../../../ModelDiscoveryModal";
import {
  SavedModelsManagerModal,
  type SavedModelsSavePayload,
} from "./SavedModelsManagerModal";
import { PackageCard } from "./PackageCard";
import {
  useProviderPackages,
  type ProviderPackageCard,
} from "../hooks/useProviderPackages";

/* -------------------------------------------------------------------------- */
/* Props                                                                      */
/* -------------------------------------------------------------------------- */

export interface StoreProvidersTabProps {
  activateEngine: (
    providerId: string,
    providerName: string,
    model: string,
    baseUrl: string,
    apiKey: string,
    apiType: string
  ) => void;
  activeProviderId: string;
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Sub-tab: Store Providers.
 *
 * Lists every Provider Package installed from Store → Providers and
 * lets the user:
 *
 *   * Scan the package's available models (via `ModelDiscoveryModal`,
 *     which already handles `/api/config/fetch-models` for external
 *     packages through Branch 0b on the backend).
 *   * Pick which models to expose in the chat model picker.
 *   * Mark the package as the active engine.
 *
 * Storage separation
 * ------------------
 * This tab uses `provider_package_models` (a new key in
 * `models_vault.json`), NOT `cloud_vault`. Keeping them separate is
 * deliberate:
 *   * `cloud_vault` is managed by `useCloudProviders` for OpenAI /
 *     Anthropic / DeepSeek style entries. Mixing in package entries
 *     would collide with its localStorage sync.
 *   * Packages have a different lifecycle: install → connection →
 *     scan → activate. Nothing here mirrors the cloud provider form.
 *
 * The connection credentials themselves still live in
 * `provider_connections` (unchanged). This tab only remembers the
 * model selection per package.
 */
export function StoreProvidersTab({
  activateEngine,
  activeProviderId,
}: StoreProvidersTabProps) {
  const { packages, isLoading, error, saveModelsForPackage } =
    useProviderPackages();

  const [scanning, setScanning] = useState<ProviderPackageCard | null>(null);

  /**
   * Package whose saved-models list is currently being managed via
   * `SavedModelsManagerModal`.
   *
   * Kept strictly separate from `scanning` because the two flows are
   * orthogonal — and because mixing them would introduce a subtle
   * bug: both handlers close their own modal state on save, but if
   * they shared a single `activePackage` variable, a slow discovery
   * scan finishing during a manager save would clear the wrong
   * modal. Two independent pieces of state make that impossible by
   * construction.
   *
   * The object stored here is a SNAPSHOT of the `ProviderPackageCard`
   * at the moment the user clicked "Manage". After a successful save
   * the modal closes, so any staleness in this snapshot is invisible
   * to the user — the next click re-reads the current `packages`
   * array from the hook.
   */
  const [managingPackage, setManagingPackage] =
    useState<ProviderPackageCard | null>(null);

  /* ---------------------- Actions ------------------------------------ */

  const handleSetPrimary = (pkg: ProviderPackageCard) => {
    if (!pkg.model) return;
    // base_url and api_key are empty on purpose — the backend
    // dispatcher resolves them from `provider_connections` at
    // request time. This matches how `CloudProviderCard` activates
    // built-in providers (it passes the actual URL/key because those
    // are user-supplied; for packages, they are managed in the Store).
    activateEngine(pkg.id, pkg.displayName, pkg.model, "", "", "");
  };

  const handleSaveModels = async (
    payload: ModelsDiscoverySavePayload
  ) => {
    if (!scanning) return;

    const selectedModels = payload.models;
    const primary = payload.defaultModel || selectedModels[0] || "";

    try {
      /* Persist ONLY the user's model selection (which models are
         exposed + which one is the default).

         Metadata persistence is intentionally NOT repeated here:
         `ModelDiscoveryModal.fetchModelsFromApi()` already calls
         `/api/config/fetch-models`, whose Step 3 invokes
         `save_metadata_cache_merge(metadata)` on the backend. That
         single call is the canonical write path for
         `storage/models_metadata.json` — every branch (native
         adapter, explicit metadata endpoint, generic `/models`,
         external package) funnels through it, and merge rules
         preserve any user-authored `manual` capability already on
         disk. Adding a second write from the frontend would be
         redundant and could race the scan if the two requests land
         out of order.

         The `facts` map (catalog capability overlay) is likewise
         not re-persisted: the badges it drives are re-fetched
         fresh on every `ModelInfoModal` render, so there is nothing
         to durably store at this layer. */
      await saveModelsForPackage(scanning.id, selectedModels, primary);
    } finally {
      setScanning(null);
    }
  };

  /**
   * Persist the changes from `SavedModelsManagerModal` for a Provider
   * Package.
   *
   * Mirrors `handleSaveModels` structurally, but with two deliberate
   * differences:
   *
   *   1. It does NOT need to touch `models_metadata.json`. Capability
   *      edits (Vision / Tools / Reasoning) travel through the nested
   *      `ModelInfoModal` → `POST /api/config/models-metadata` — a
   *      separate, already-established write path that this handler
   *      must not duplicate.
   *
   *   2. Empty `selectedModels` is legitimate here. Unlike the
   *      discovery modal (where zero selections means "user forgot
   *      to pick"), the manager's "Keep None" flow is an intentional
   *      cleanup — the user is removing every model from the package
   *      (e.g. because the model is no longer available on the
   *      provider). The card will then fall back to its
   *      "No models discovered yet" state until the next scan.
   *
   * `saveModelsForPackage` performs a read-modify-write against
   * `/api/config/vault` with `cache: "no-store"`, so concurrent saves
   * for different packages cannot clobber each other's entries in
   * `provider_package_models`. The same helper serves both this
   * handler and `handleSaveModels` — a single write path, one place
   * to reason about.
   */
  const handleSavePackageModels = async (
    payload: SavedModelsSavePayload
  ) => {
    if (!managingPackage) return;

    const selectedModels = Array.isArray(payload.models)
      ? payload.models
      : [];
    const primary = payload.defaultModel || selectedModels[0] || "";

    try {
      await saveModelsForPackage(
        managingPackage.id,
        selectedModels,
        primary
      );
    } finally {
      setManagingPackage(null);
    }
  };

  /* ---------------------- Render ------------------------------------- */

  // Loading state
  if (isLoading && packages.length === 0) {
    return (
      <div className="w-full max-w-7xl mx-auto pt-6 flex flex-col items-center justify-center gap-3 py-16">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
        <p className="text-xs text-muted-foreground">
          Loading installed provider packages…
        </p>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className="w-full max-w-7xl mx-auto pt-6">
        <div className="flex items-start gap-2 rounded-md border border-rose-500/30 bg-rose-500/5 p-4">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-rose-500" />
          <div>
            <p className="text-sm font-semibold text-rose-600 dark:text-rose-400">
              Failed to load provider packages
            </p>
            <p className="mt-1 text-xs text-muted-foreground">{error}</p>
          </div>
        </div>
      </div>
    );
  }

  // Empty state
  if (packages.length === 0) {
    return (
      <div className="w-full max-w-7xl mx-auto pt-6">
        <div className="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border bg-card p-12 text-center">
          <PackageIcon className="h-10 w-10 text-muted-foreground/40" />
          <div>
            <p className="text-sm font-semibold text-foreground">
              No provider packages installed
            </p>
            <p className="mt-1 max-w-md text-xs text-muted-foreground">
              Install a package from the Store, then return here to
              select its models and activate it as the primary engine.
            </p>
          </div>
          <a
            href="/stores"
            target="_blank"
            rel="noopener noreferrer"
            className="mt-2 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Open Store → Providers
          </a>
        </div>
      </div>
    );
  }

  // Main list
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
      className="w-full max-w-7xl space-y-4 pt-2 mx-auto"
    >
      {/* Info banner */}
      <div className="flex items-start gap-2 rounded-md border border-primary/20 bg-primary/5 p-3">
        <PackageIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
        <p className="text-[11px] leading-relaxed text-foreground/80">
          Packages installed from <strong>Store → Providers</strong>{" "}
          appear here automatically. Manage connections, certificates,
          and uninstalls in the Store; use this tab only to select
          which models are exposed and to activate the primary engine.
        </p>
      </div>

      {/* Package cards */}
      <div className="space-y-3">
        {packages.map((pkg) => (
          <PackageCard
            key={pkg.id}
            package={pkg}
            isPrimary={activeProviderId === pkg.id}
            isScanning={scanning?.id === pkg.id}
            onScanModels={() => setScanning(pkg)}
            onSetPrimary={() => handleSetPrimary(pkg)}
            /* Provide "Manage" only when the package has at least
               one saved model. `PackageCard` already gates the
               button on `hasModels`, but passing `undefined` when
               the list is empty keeps the two components' contract
               unambiguous — and saves a per-render closure
               allocation. */
            onManageModels={
              pkg.savedModels.length > 0
                ? () => setManagingPackage(pkg)
                : undefined
            }
          />
        ))}
      </div>

      {/* Discovery modal (unchanged — reuses the existing one) */}
      {scanning && (
        <ModelDiscoveryModal
          isOpen={true}
          onClose={() => setScanning(null)}
          providerId={scanning.id}
          providerName={scanning.displayName}
          baseUrl=""
          apiKey=""
          providerType=""
          initialSavedModels={scanning.savedModels}
          onSaveModels={handleSaveModels}
        />
      )}

      {/* Saved Models Manager modal — opened from the "Manage" button
          on a package card. Reuses the same nested `ModelInfoModal`
          flow for capability editing as the discovery modal, so the
          user's mental model ("open the info icon to edit caps")
          stays consistent across both flows.

          The modal receives `providerKind="package"` so its header
          subtitle reads "Package Provider • N saved" and the icon is
          the `Package` glyph — matching the visual identity already
          used by `PackageCard`. */}
      {managingPackage && (
        <SavedModelsManagerModal
          isOpen={true}
          onClose={() => setManagingPackage(null)}
          providerId={managingPackage.id}
          providerName={managingPackage.displayName}
          providerKind="package"
          savedModels={managingPackage.savedModels}
          defaultModel={managingPackage.model}
          onSaveModels={handleSavePackageModels}
        />
      )}
    </motion.div>
  );
}
