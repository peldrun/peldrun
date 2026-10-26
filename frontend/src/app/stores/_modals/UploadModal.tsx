// frontend/src/app/stores/_modals/UploadModal.tsx

"use client";

import React from "react";
import { Archive, FileCode2, FileText, Loader2, Upload, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { UploadCategory } from "../_types/store";

/**
 * Props for {@link UploadModal}.
 *
 * The modal is fully controlled: the parent owns the upload category,
 * the selected file, the uploading flag, and the hidden file input ref
 * (all managed by `useToolActions`). This component is presentation
 * only and does not keep any local state.
 */
export interface UploadModalProps {
  open: boolean;
  category: UploadCategory;
  selectedFile: File | null;
  isUploading: boolean;
  /** Ref to the hidden <input type="file"> controlled by the parent. */
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onCategoryChange: (category: UploadCategory) => void;
  onFileSelect: (file: File | null) => void;
  onSubmit: (e: React.FormEvent) => void;
  onClose: () => void;
}

/**
 * Modal used to upload a single-file capability:
 *  - Custom Tool   → .py script or .json manifest
 *  - MCP Extension → .json server manifest
 *
 * The category toggle re-configures the file input's `accept` attribute
 * and clears any previously picked file so users cannot accidentally
 * submit a `.py` as an MCP manifest (and vice-versa).
 */
export function UploadModal({
  open,
  category,
  selectedFile,
  isUploading,
  fileInputRef,
  onCategoryChange,
  onFileSelect,
  onSubmit,
  onClose,
}: UploadModalProps) {
  if (!open) return null;

  const isTool = category === "tool";
  const accept = isTool ? ".py,.json" : ".json,.zip";
  const title = isTool ? "Upload Custom Tool" : "Upload MCP Server / Bundle";

  /**
   * Handle the file input's change event and forward the picked file
   * to the parent. Selecting nothing (cancelled dialog) is a no-op.
   */
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] ?? null;
    if (file) onFileSelect(file);
  };

  /**
   * Open the native file picker when the dropzone is clicked.
   */
  const openPicker = () => fileInputRef.current?.click();

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4">
      <div className="bg-card border border-border rounded-2xl max-w-md w-full flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-muted/30">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
              <Upload size={16} />
            </div>
            <h2 className="text-sm font-bold text-foreground">{title}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition cursor-pointer"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        {/* Body */}
        <form onSubmit={onSubmit} className="p-6 space-y-4 text-xs">
          {/* Category selector */}
          <div>
            <span className="block text-foreground font-medium mb-1">
              Upload Category
            </span>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => onCategoryChange("tool")}
                className={`p-2 rounded-lg border text-center font-medium transition cursor-pointer ${
                  category === "tool"
                    ? "bg-primary/10 border-primary text-primary shadow-xs"
                    : "bg-muted/40 border-border text-muted-foreground hover:text-foreground"
                }`}
              >
                Custom Tool (.py / .json)
              </button>
              <button
                type="button"
                onClick={() => onCategoryChange("extension")}
                className={`p-2 rounded-lg border text-center font-medium transition cursor-pointer ${
                  category === "extension"
                    ? "bg-primary/10 border-primary text-primary shadow-xs"
                    : "bg-muted/40 border-border text-muted-foreground hover:text-foreground"
                }`}
              >
                MCP Server / Bundle (.json / .zip)
              </button>
            </div>
          </div>

          {/* Hidden file input */}
          <input
            ref={fileInputRef}
            type="file"
            accept={accept}
            onChange={handleFileChange}
            className="hidden"
          />

          {/* Dropzone */}
          <div
            onClick={openPicker}
            className="border-2 border-dashed border-border hover:border-primary/50 rounded-sm p-6 flex flex-col items-center justify-center text-center cursor-pointer transition bg-muted/10 hover:bg-muted/30"
          >
            {isTool ? (
              <FileCode2 size={28} className="text-primary mb-2 opacity-80" />
            ) : selectedFile?.name.toLowerCase().endsWith(".zip") ? (
              <Archive size={28} className="text-primary mb-2 opacity-80" />
            ) : (
              <FileText size={28} className="text-primary mb-2 opacity-80" />
            )}

            {selectedFile ? (
              <div>
                <span className="font-semibold text-foreground text-xs block truncate max-w-[240px]">
                  {selectedFile.name}
                </span>
                <span className="text-[10px] text-muted-foreground font-mono">
                  {(selectedFile.size / 1024).toFixed(1)} KB
                </span>
              </div>
            ) : (
              <div>
                <span className="text-xs font-semibold text-foreground block">
                  Click to choose {isTool ? ".py or .json" : ".json or .zip bundle"} file
                </span>
                <span className="text-[10px] text-muted-foreground mt-0.5 block">
                  {isTool
                    ? "File will be verified and hot-reloaded automatically."
                    : "Manifests and .zip bundles are extracted into isolated folders and registered automatically."}
                </span>
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-end gap-2.5 pt-3 border-t border-border">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onClose}
              className="text-xs hover:bg-destructive/10 hover:text-destructive transition-colors cursor-pointer"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              size="sm"
              disabled={!selectedFile || isUploading}
              className="bg-primary hover:bg-primary/90 text-primary hover:text-primary-foreground font-medium text-xs px-4 shadow-xs cursor-pointer disabled:opacity-40"
            >
              {isUploading ? (
                <>
                  <Loader2 size={12} className="mr-1.5 animate-spin" />
                  <span>Uploading...</span>
                </>
              ) : (
                <span>Register Capability</span>
              )}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
