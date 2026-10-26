// frontend/src/app/stores/_hooks/useExtensionActions.ts

"use client";

import { useCallback, useState } from "react";
import type { ExtensionDefinition } from "@/lib/types";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "./useStoreData";
import type { McpTestResultMap } from "../_types/store";

/**
 * Arguments required by {@link useExtensionActions}.
 * The extensions collection and setter are injected from `useStoreData`
 * so the hook only owns action-specific state.
 */
export interface UseExtensionActionsArgs {
  extensions: ExtensionDefinition[];
  setExtensions: React.Dispatch<React.SetStateAction<ExtensionDefinition[]>>;
}

/**
 * Return type of {@link useExtensionActions}.
 */
export interface UseExtensionActionsResult {
  // Toggle lifecycle
  togglingExtId: string | null;
  handleToggleExtension: (extId: string) => Promise<void>;

  // Connectivity test
  testingExtId: string | null;
  testResults: McpTestResultMap;
  handleTestExtension: (extId: string) => Promise<void>;

  // Deletion lifecycle
  deletingExtId: string | null;
  handleDeleteExtension: (extId: string) => Promise<void>;

  // Configuration modal
  configuringExtension: ExtensionDefinition | null;
  isConfigModalOpen: boolean;
  handleOpenConfigure: (ext: ExtensionDefinition) => void;
  handleCloseConfigure: () => void;
}

/**
 * Encapsulates actions related to the Extensions & MCP section:
 *  - Enabling/disabling an MCP server
 *  - Running a one-shot connectivity test with dynamic tool discovery
 *  - Deleting/purging an MCP bundle or custom server
 *  - Configuring settings and environment variables
 */
export function useExtensionActions({
  extensions,
  setExtensions,
}: UseExtensionActionsArgs): UseExtensionActionsResult {
  const [togglingExtId, setTogglingExtId] = useState<string | null>(null);
  const [testingExtId, setTestingExtId] = useState<string | null>(null);
  const [deletingExtId, setDeletingExtId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<McpTestResultMap>({});
  const [configuringExtension, setConfiguringExtension] = useState<ExtensionDefinition | null>(null);

  /**
   * Toggle the status of an MCP server.
   */
  const handleToggleExtension = useCallback(
    async (extId: string) => {
      setTogglingExtId(extId);
      try {
        const res = await fetch(getApiUrl(`/api/mcp/servers/${extId}/toggle`), {
          method: "POST",
        });
        if (res.ok) {
          const data = await res.json();
          const newStatus = data.server?.status || "ready";
          setExtensions((prev) =>
            prev.map((e) => (e.id === extId ? { ...e, status: newStatus } : e))
          );
          showToast.success("Extension Updated", `${extId} is now ${newStatus}`);
        } else {
          const err = await res.json().catch(() => ({}));
          showToast.error(
            "Toggle Failed",
            err.detail || "Could not toggle extension status."
          );
        }
      } catch (err: any) {
        showToast.error("Network Error", err.message);
      } finally {
        setTogglingExtId(null);
      }
    },
    [setExtensions]
  );

  /**
   * Run an MCP connectivity test against the backend.
   */
  const handleTestExtension = useCallback(async (extId: string) => {
    setTestingExtId(extId);
    try {
      const res = await fetch(getApiUrl("/api/mcp/test"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ server_id: extId }),
      });
      const data = await res.json().catch(() => ({}));
      const isOk = data.status === "ok";
      setTestResults((prev) => ({
        ...prev,
        [extId]: {
          ok: isOk,
          message: data.message || (isOk ? "Connection OK" : "Test failed"),
        },
      }));
      if (isOk) {
        showToast.success("MCP Test Succeeded", data.message);
      } else {
        showToast.warning("MCP Test Warning", data.message);
      }
    } catch (err: any) {
      setTestResults((prev) => ({
        ...prev,
        [extId]: {
          ok: false,
          message: err.message || "Failed to reach backend.",
        },
      }));
      showToast.error("Test Error", err.message || "Failed to reach backend.");
    } finally {
      setTestingExtId(null);
    }
  }, []);

  /**
   * Delete or purge an MCP server / bundle.
   */
  const handleDeleteExtension = useCallback(
    async (extId: string) => {
      setDeletingExtId(extId);
      try {
        const res = await fetch(getApiUrl(`/api/mcp/servers/${extId}`), {
          method: "DELETE",
        });
        if (res.ok) {
          setExtensions((prev) => prev.filter((e) => e.id !== extId));
          showToast.success("MCP Server Removed", `${extId} and associated bundle files purged.`);
        } else {
          const err = await res.json().catch(() => ({}));
          showToast.error("Delete Failed", err.detail || "Failed to delete MCP server.");
        }
      } catch (err: any) {
        showToast.error("Network Error", err.message || "Could not delete MCP server.");
      } finally {
        setDeletingExtId(null);
      }
    },
    [setExtensions]
  );

  const handleOpenConfigure = useCallback((ext: ExtensionDefinition) => {
    setConfiguringExtension(ext);
  }, []);

  const handleCloseConfigure = useCallback(() => {
    setConfiguringExtension(null);
  }, []);

  return {
    togglingExtId,
    handleToggleExtension,
    testingExtId,
    testResults,
    handleTestExtension,
    deletingExtId,
    handleDeleteExtension,
    configuringExtension,
    isConfigModalOpen: Boolean(configuringExtension),
    handleOpenConfigure,
    handleCloseConfigure,
  };
}
