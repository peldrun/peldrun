// frontend/src/app/stores/_hooks/useBundleActions.ts

"use client";

import {
  deleteBundle as apiDeleteBundle,
  activateVersion as apiActivateVersion,
  type BundleEntry,
} from "@/lib/bundleApi";
import { showToast } from "@/components/ui/ToastNotification";
import { useCallback } from "react";

/**
 * Arguments required by {@link useBundleActions}.
 *
 * - `reload` re-fetches the catalog after a mutating operation.
 * - `detailsBundle` / `setDetailsBundle` let activation update the
 *   currently open drawer in place without a full catalog refresh.
 */
export interface UseBundleActionsArgs {
  reload: () => Promise<void>;
  detailsBundle: BundleEntry | null;
  setDetailsBundle: React.Dispatch<React.SetStateAction<BundleEntry | null>>;
}

/**
 * Return type of {@link useBundleActions}.
 */
export interface UseBundleActionsResult {
  handleBundleDelete: (bundle: BundleEntry) => Promise<void>;
  handleBundleActivate: (
    bundle: BundleEntry,
    version: string
  ) => Promise<void>;
}

/**
 * Encapsulates every action related to installed bundles:
 *  - deleting a bundle (single version or all versions)
 *  - activating a specific version (roll-forward / rollback)
 *
 * Both operations reload the catalog on success so the grid and the
 * drawer reflect the new state.
 */
export function useBundleActions({
  reload,
  detailsBundle,
  setDetailsBundle,
}: UseBundleActionsArgs): UseBundleActionsResult {
  /**
   * Delete a bundle. When the bundle has more than one installed
   * version the confirmation prompt warns that ALL versions will be
   * removed, matching the backend's "purge" behaviour.
   */
  const handleBundleDelete = useCallback(
    async (bundle: BundleEntry) => {
      const hasMultipleVersions = bundle.installed_versions.length > 1;
      const confirmed = confirm(
        hasMultipleVersions
          ? `Delete ALL versions of bundle '${bundle.id}'? This cannot be undone.`
          : `Delete bundle '${bundle.id}' v${bundle.active_version}?`
      );
      if (!confirmed) return;

      try {
        await apiDeleteBundle(bundle.id, hasMultipleVersions);
        showToast.success("Bundle Deleted", `${bundle.id} removed.`);
        await reload();
      } catch (err: any) {
        showToast.error(
          "Delete Failed",
          err?.message || "Could not delete bundle."
        );
      }
    },
    [reload]
  );

  /**
   * Activate a specific version of a bundle.
   * If the details drawer is currently showing that bundle, it is
   * updated in place so the user does not have to reopen it.
   */
  const handleBundleActivate = useCallback(
    async (bundle: BundleEntry, version: string) => {
      try {
        await apiActivateVersion(bundle.id, version);
        showToast.success("Version Activated", `${bundle.id} → v${version}`);
        await reload();
        if (detailsBundle && detailsBundle.id === bundle.id) {
          setDetailsBundle({ ...bundle, active_version: version });
        }
      } catch (err: any) {
        showToast.error(
          "Activation Failed",
          err?.message || "Could not activate."
        );
      }
    },
    [reload, detailsBundle, setDetailsBundle]
  );

  return {
    handleBundleDelete,
    handleBundleActivate,
  };
}
