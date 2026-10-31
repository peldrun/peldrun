// frontend/src/app/stores/_modals/SkillUploadModal.tsx

"use client";

import React, { useRef } from "react";
import { FileArchive, Loader2, Upload, X } from "lucide-react";

export interface SkillUploadModalProps {
  open: boolean;
  selectedFile: File | null;
  isUploading: boolean;
  onFileSelect: (file: File | null) => void;
  onSubmit: (e: React.FormEvent) => Promise<void>;
  onClose: () => void;
}

/**
 * Upload modal for skill packages.
 *
 * Unlike the tool / extension UploadModal (which chooses between two
 * categories), this modal is single-purpose: install a ``.zip`` skill
 * package. The file type is validated both here (client-side) and on
 * the backend (defense in depth).
 *
 * UX notes:
 *  - The drop zone is clickable AND accepts drag-and-drop. Both paths
 *    funnel through the same ``onFileSelect`` callback.
 *  - Only ``.zip`` files are accepted at the file picker level; a
 *    dropped file of any type is validated in the handler before
 *    being passed upward.
 *  - Uploading is disabled when no file is selected, to prevent a
 *    pointless POST.
 */
export function SkillUploadModal({
  open,
  selectedFile,
  isUploading,
  onFileSelect,
  onSubmit,
  onClose,
}: SkillUploadModalProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  if (!open) return null;

  const handlePicked = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files && files.length > 0) {
      onFileSelect(files[0]);
    }
    // Reset the input so selecting the same file twice (after a
    // failed upload) still triggers onChange.
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const files = e.dataTransfer.files;
    if (files && files.length > 0) {
      onFileSelect(files[0]);
    }
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const canSubmit = !isUploading && selectedFile !== null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="skill-upload-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !isUploading) onClose();
      }}
    >
      <div className="bg-card rounded-md shadow-2xl w-full max-w-lg flex flex-col border border-border">
        {/* Header */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0 bg-muted/40">
          <div className="flex items-center gap-2 min-w-0">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary shrink-0">
              <Upload size={15} />
            </div>
            <div className="min-w-0">
              <h2
                id="skill-upload-title"
                className="text-sm font-bold text-foreground uppercase tracking-wide"
              >
                Install Skill
              </h2>
              <p className="text-xs text-muted-foreground">
                Upload a .zip package
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={isUploading}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-50 transition"
            aria-label="Close upload modal"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <form
          onSubmit={onSubmit}
          className="flex-1 overflow-y-auto px-6 py-5 scrollbar-thin"
        >
          {/* Drop zone */}
          <div
            role="button"
            tabIndex={0}
            onClick={() => fileInputRef.current?.click()}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                fileInputRef.current?.click();
              }
            }}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            className="border-2 border-dashed border-border rounded-md p-6 text-center cursor-pointer hover:border-primary/40 hover:bg-primary/5 transition-colors focus:outline-none focus:border-primary focus:bg-primary/5"
          >
            <FileArchive
              size={32}
              className="mx-auto text-muted-foreground mb-2"
              aria-hidden="true"
            />
            <p className="text-sm font-medium text-foreground">
              Click to choose a file, or drag one here
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              Only <code className="font-mono">.zip</code> skill packages
              are accepted.
            </p>
            <input
              ref={fileInputRef}
              type="file"
              accept=".zip,application/zip,application/x-zip-compressed"
              onChange={handlePicked}
              className="hidden"
              disabled={isUploading}
            />
          </div>

          {/* Selected file preview */}
          {selectedFile && (
            <div className="mt-4 flex items-center gap-2 p-3 rounded-md border border-border bg-muted/40">
              <FileArchive
                size={16}
                className="text-primary shrink-0"
                aria-hidden="true"
              />
              <div className="flex-1 min-w-0">
                <div className="text-xs font-medium text-foreground truncate">
                  {selectedFile.name}
                </div>
                <div className="text-[10px] text-muted-foreground font-mono">
                  {formatFileSize(selectedFile.size)}
                </div>
              </div>
              {!isUploading && (
                <button
                  type="button"
                  onClick={() => onFileSelect(null)}
                  className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
                  aria-label="Clear selected file"
                >
                  <X size={14} />
                </button>
              )}
            </div>
          )}

          {/* Package requirements help */}
          <div className="mt-4 p-3 rounded-md border border-border bg-muted/20 text-[11px] text-muted-foreground leading-relaxed">
            <strong className="text-foreground">Package contents:</strong>
            <ul className="mt-1 ms-4 list-disc space-y-0.5">
              <li>
                <code className="font-mono">manifest.json</code> at the root
                (required)
              </li>
              <li>
                <code className="font-mono">SKILL.md</code> (or the file
                named in <code className="font-mono">execution.instructions_file</code>)
              </li>
              <li>
                Optional: <code className="font-mono">references/</code>,
                <code className="font-mono"> assets/</code>, and an icon
                file
              </li>
            </ul>
          </div>
        </form>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-border flex items-center justify-end gap-2 shrink-0 bg-muted/20">
          <button
            type="button"
            onClick={onClose}
            disabled={isUploading}
            className="px-3.5 py-1.5 text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-50 cursor-pointer"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={(e) => {
              if (canSubmit) onSubmit(e);
            }}
            disabled={!canSubmit}
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:opacity-90 disabled:opacity-50 transition cursor-pointer"
          >
            {isUploading ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                Installing…
              </>
            ) : (
              <>
                <Upload size={13} />
                Install Skill
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
