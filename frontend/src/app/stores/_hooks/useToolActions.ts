// frontend/src/app/stores/_hooks/useToolActions.ts

"use client";

import { useCallback, useRef, useState } from "react";
import type { ToolDefinition } from "@/lib/types";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "./useStoreData";
import type { UploadCategory } from "../_types/store";

/**
 * Arguments required by {@link useToolActions}.
 * The tools collection and setter are injected from `useStoreData` so
 * the hook only owns action-specific state.
 */
export interface UseToolActionsArgs {
  tools: ToolDefinition[];
  setTools: React.Dispatch<React.SetStateAction<ToolDefinition[]>>;
  /** Triggers a full catalog reload after a successful upload. */
  reload: () => Promise<void>;
}

/**
 * Return type of {@link useToolActions}.
 */
export interface UseToolActionsResult {
  // Toggle lifecycle
  togglingToolId: string | null;
  handleToggleTool: (toolId: string) => Promise<void>;

  // Delete
  handleDeleteTool: (toolId: string) => Promise<void>;

  // Upload modal (shared between Tools and Extensions sections)
  isUploadModalOpen: boolean;
  uploadCategory: UploadCategory;
  selectedUploadFile: File | null;
  isUploading: boolean;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  setSelectedUploadFile: (file: File | null) => void;
  setUploadCategory: (category: UploadCategory) => void;
  openUploadModal: (category: UploadCategory) => void;
  closeUploadModal: () => void;
  handleUploadSubmit: (e: React.FormEvent) => Promise<void>;
}

/**
 * Encapsulates every action related to the Tools section:
 * toggling, deleting, and the unified upload modal (used for both
 * custom Python tools and MCP server manifests).
 *
 * The upload state lives here because the modal is a single shared
 * surface on the page; the Extensions section reuses the same handlers
 * by receiving them from the page orchestrator.
 */
export function useToolActions({
  tools,
  setTools,
  reload,
}: UseToolActionsArgs): UseToolActionsResult {
  const [togglingToolId, setTogglingToolId] = useState<string | null>(null);

  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const [uploadCategory, setUploadCategory] = useState<UploadCategory>("tool");
  const [selectedUploadFile, setSelectedUploadFile] = useState<File | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  /**
   * Toggle the enabled/disabled status of a tool.
   */
  const handleToggleTool = useCallback(
    async (toolId: string) => {
      setTogglingToolId(toolId);
      try {
        const res = await fetch(getApiUrl(`/api/store/tools/${toolId}/toggle`), {
          method: "POST",
        });
        if (res.ok) {
          const data = await res.json();
          const updatedTool = data.tool;
          setTools((prev) =>
            prev.map((t) =>
              t.id === toolId
                ? {
                    ...t,
                    is_enabled: updatedTool.is_enabled,
                    status: updatedTool.status,
                  }
                : t
            )
          );
          showToast.success(
            "Tool Status Updated",
            `${updatedTool.name || toolId} is now ${updatedTool.status}`
          );
        } else {
          const err = await res.json().catch(() => ({}));
          showToast.error(
            "Toggle Failed",
            err.detail || "Could not toggle tool status."
          );
        }
      } catch (err: any) {
        showToast.error("Network Error", err.message);
      } finally {
        setTogglingToolId(null);
      }
    },
    [setTools]
  );

  /**
   * Permanently delete a custom tool after confirmation.
   */
  const handleDeleteTool = useCallback(
    async (toolId: string) => {
      if (
        !confirm(
          `Are you sure you want to permanently delete custom tool '${toolId}'?`
        )
      ) {
        return;
      }
      try {
        const res = await fetch(getApiUrl(`/api/store/tools/${toolId}`), {
          method: "DELETE",
        });
        if (res.ok) {
          setTools((prev) => prev.filter((t) => t.id !== toolId));
          showToast.success(
            "Tool Deleted",
            `Custom tool '${toolId}' removed successfully.`
          );
        } else {
          const err = await res.json().catch(() => ({}));
          showToast.error("Delete Failed", err.detail || "Cannot delete tool.");
        }
      } catch (err: any) {
        showToast.error("Delete Error", err.message);
      }
    },
    [setTools]
  );

  /**
   * Open the upload modal with the given category pre-selected.
   * Always resets the file selection so switching categories does not
   * carry a previously picked (now invalid) file.
   */
  const openUploadModal = useCallback((category: UploadCategory) => {
    setUploadCategory(category);
    setSelectedUploadFile(null);
    setIsUploadModalOpen(true);
  }, []);

  /**
   * Close the upload modal without clearing the category (keeps the
   * last-used choice for convenience).
   */
  const closeUploadModal = useCallback(() => {
    setIsUploadModalOpen(false);
  }, []);

  /**
   * Submit the upload modal to the correct backend endpoint.
   * On success the whole catalog is reloaded so the new capability
   * appears immediately in the correct section.
   */
  const handleUploadSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!selectedUploadFile) {
        showToast.warning("No File Selected", "Please choose a file to upload.");
        return;
      }

      setIsUploading(true);
      const formData = new FormData();
      formData.append("file", selectedUploadFile);

      try {
        const endpoint =
          uploadCategory === "tool"
            ? "/api/store/tools/upload"
            : "/api/store/extensions/upload";
        const res = await fetch(getApiUrl(endpoint), {
          method: "POST",
          body: formData,
        });

        if (res.ok) {
          showToast.success(
            uploadCategory === "tool" ? "Tool Uploaded" : "Extension Uploaded",
            `Successfully registered ${selectedUploadFile.name}`
          );
          setIsUploadModalOpen(false);
          setSelectedUploadFile(null);
          await reload();
        } else {
          const err = await res.json().catch(() => ({}));
          showToast.error(
            "Upload Failed",
            err.detail || "Server rejected the upload file."
          );
        }
      } catch (err: any) {
        showToast.error(
          "Upload Error",
          err.message || "Failed to upload file."
        );
      } finally {
        setIsUploading(false);
      }
    },
    [selectedUploadFile, uploadCategory, reload]
  );

  return {
    togglingToolId,
    handleToggleTool,
    handleDeleteTool,
    isUploadModalOpen,
    uploadCategory,
    selectedUploadFile,
    isUploading,
    fileInputRef,
    setSelectedUploadFile,
    setUploadCategory,
    openUploadModal,
    closeUploadModal,
    handleUploadSubmit,
  };
}
