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

  // Tools management modal
  managingToolsExtension: ExtensionDefinition | null;
  isToolsModalOpen: boolean;
  handleOpenManageTools: (ext: ExtensionDefinition) => void;
  handleCloseManageTools: () => void;

    // Details Drawer
  detailsExtension: ExtensionDefinition | null;
  isDetailsDrawerOpen: boolean;
  handleOpenDetails: (ext: ExtensionDefinition) => void;
  handleCloseDetails: () => void;

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
  const [managingToolsExtension, setManagingToolsExtension] = useState<ExtensionDefinition | null>(null);
  const [detailsExtension, setDetailsExtension] = useState<ExtensionDefinition | null>(null);

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
  const handleTestExtension = useCallback(
    async (extId: string) => {
      setTestingExtId(extId);

      // Pre-flight: OAuth servers NEVER pass "Test Connection"
      // because the endpoint requires a Bearer token that only
      // exists after the OAuth flow completes. Sending a naked
      // request produces a scary 401 in the logs and a red
      // warning in the UI — both of which look like bugs to the
      // user. Short-circuit with a friendly info toast that
      // points to the correct flow (test from within a chat).
      const ext = extensions.find((e) => e.id === extId) as any;
      const authStrategy = String(ext?.auth_strategy || "").toLowerCase();
      const isOAuthServer = authStrategy === "oauth";

      if (isOAuthServer) {
        // Classify as "deferred" rather than "success". The badge
        // for a deferred result is amber ("OAuth server"), which
        // correctly signals "not verified from here" instead of
        // lying with a green "Connected" badge. `ok: true` is kept
        // for backward compatibility with any consumer that only
        // reads the boolean — it means "no blocking problem to
        // report", which is accurate for a deferred check.
        setTestResults((prev) => ({
          ...prev,
          [extId]: {
            ok: true,
            kind: "deferred",
            message:
              "OAuth server — connect from Chat to authenticate and verify.",
          },
        }));
        showToast.info(
          "OAuth Server",
          "This server authenticates through OAuth. Open a chat and use its tools — the connection is verified there, not here."
        );
        setTestingExtId(null);
        return;
      }

      try {
        const res = await fetch(getApiUrl("/api/mcp/test"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ server_id: extId }),
        });
        const data = await res.json().catch(() => ({}));
        const isOk = data.status === "ok";

        // Even non-OAuth servers can return 401 if credentials
        // are missing globally. Surface that as a hint instead of
        // a raw error, so the user knows the path forward.
        const isAuthMissing =
          typeof data.message === "string" &&
          /401|unauthori[sz]ed/i.test(data.message);

        setTestResults((prev) => ({
          ...prev,
          [extId]: {
            ok: isOk,
            kind: isOk ? "success" : "failure",
            message: isAuthMissing
              ? "Requires credentials — set them in Settings first."
              : data.message || (isOk ? "Connection OK" : "Test failed"),
          },
        }));

        if (isOk) {
          showToast.success("MCP Test Succeeded", data.message);
        } else if (isAuthMissing) {
          showToast.info(
            "Credentials Required",
            "This server needs credentials before it can be tested. Open Settings and save them, then retry."
          );
        } else {
          showToast.warning("MCP Test Warning", data.message);
        }
      } catch (err: any) {
        setTestResults((prev) => ({
          ...prev,
          [extId]: {
            ok: false,
            kind: "failure",
            message: err.message || "Failed to reach backend.",
          },
        }));
        showToast.error(
          "Test Error",
          err.message || "Failed to reach backend."
        );
      } finally {
        setTestingExtId(null);
      }
    },
    [extensions]
  );

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

  const handleOpenManageTools = useCallback((ext: ExtensionDefinition) => {
    setManagingToolsExtension(ext);
  }, []);

  const handleCloseManageTools = useCallback(() => {
    setManagingToolsExtension(null);
  }, []);

  const handleOpenDetails = useCallback((ext: ExtensionDefinition) => {
    setDetailsExtension(ext);
  }, []);

  const handleCloseDetails = useCallback(() => {
    setDetailsExtension(null);
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
    // Tools management modal
    managingToolsExtension: managingToolsExtension,
    isToolsModalOpen: Boolean(managingToolsExtension),
    handleOpenManageTools,
    handleCloseManageTools,
    // Details Drawer
    detailsExtension,
    isDetailsDrawerOpen: Boolean(detailsExtension),
    handleOpenDetails,
    handleCloseDetails,
  };
}
