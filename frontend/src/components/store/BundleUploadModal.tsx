"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  FileArchive,
  Loader2,
  Upload,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/ToastNotification";
import {
  BundleInstallResult,
  uploadBundle,
} from "@/lib/bundleApi";

export interface BundleUploadModalProps {
  open: boolean;
  onClose: () => void;
  onSuccess: (result: BundleInstallResult) => void;
}

type UploadPhase = "idle" | "uploading" | "success" | "error";

export function BundleUploadModal({
  open,
  onClose,
  onSuccess,
}: BundleUploadModalProps) {
  const [file, setFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [phase, setPhase] = useState<UploadPhase>("idle");
  const [progress, setProgress] = useState(0);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [result, setResult] = useState<BundleInstallResult | null>(null);
  const [installRequirements, setInstallRequirements] = useState(true);

  const fileInputRef = useRef<HTMLInputElement>(null);

  // Reset state whenever the modal is reopened
  useEffect(() => {
    if (!open) return;
    setFile(null);
    setPhase("idle");
    setProgress(0);
    setErrorMessage(null);
    setResult(null);
    setInstallRequirements(true);
  }, [open]);

  const pickFile = useCallback((selected: File | null) => {
    if (!selected) return;
    if (!selected.name.toLowerCase().endsWith(".zip")) {
      showToast.warning("Invalid file", "Only .zip bundle files are accepted.");
      return;
    }
    setFile(selected);
    setPhase("idle");
    setErrorMessage(null);
  }, []);

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
    const dropped = e.dataTransfer.files?.[0] || null;
    pickFile(dropped);
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const handleUpload = async () => {
    if (!file) return;
    setPhase("uploading");
    setProgress(0);
    setErrorMessage(null);

    try {
      const installed = await uploadBundle(
        file,
        { installRequirements },
        (pct) => setProgress(pct)
      );
      setResult(installed);
      setPhase("success");
      showToast.success(
        "Bundle Installed",
        `${installed.name} v${installed.version} is now available.`
      );
      onSuccess(installed);
    } catch (err: any) {
      setPhase("error");
      setErrorMessage(err.message || "Upload failed.");
      showToast.error("Upload Failed", err.message || "Please try again.");
    }
  };

  if (!open) return null;

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4">
      <div className="bg-card border border-border rounded-2xl max-w-lg w-full flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-muted/30">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
              <FileArchive size={16} />
            </div>
            <h2 className="text-sm font-bold text-foreground">
              Upload Tool Bundle
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={phase === "uploading"}
            className="p-1 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition cursor-pointer disabled:opacity-40"
          >
            <X size={16} />
          </button>
        </div>

        <div className="p-6 space-y-4 text-xs">
          {/* Drop zone */}
          <input
            ref={fileInputRef}
            type="file"
            accept=".zip"
            onChange={(e) => pickFile(e.target.files?.[0] || null)}
            className="hidden"
          />

          <div
            onClick={() => phase !== "uploading" && fileInputRef.current?.click()}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            className={`border-2 border-dashed rounded-xl p-6 flex flex-col items-center justify-center text-center transition cursor-pointer ${
              isDragging
                ? "border-primary bg-primary/5"
                : "border-border hover:border-primary/50 bg-muted/10 hover:bg-muted/30"
            } ${phase === "uploading" ? "pointer-events-none opacity-60" : ""}`}
          >
            {file ? (
              <>
                <FileArchive size={28} className="text-primary mb-2 opacity-80" />
                <span className="font-semibold text-foreground text-xs truncate max-w-[280px]">
                  {file.name}
                </span>
                <span className="text-[10px] text-muted-foreground font-mono mt-0.5">
                  {formatSize(file.size)}
                </span>
              </>
            ) : (
              <>
                <Upload size={28} className="text-primary mb-2 opacity-80" />
                <span className="text-xs font-semibold text-foreground">
                  Click to select or drag a .zip bundle
                </span>
                <span className="text-[10px] text-muted-foreground mt-1">
                  Contains manifest.json + Python source + optional helpers
                </span>
              </>
            )}
          </div>

          {/* Advanced options */}
          {file && phase === "idle" && (
            <label className="flex items-center gap-2 p-3 rounded-lg bg-muted/20 border border-border cursor-pointer hover:bg-muted/30 transition">
              <input
                type="checkbox"
                checked={installRequirements}
                onChange={(e) => setInstallRequirements(e.target.checked)}
                className="cursor-pointer"
              />
              <span className="text-[11px] text-muted-foreground">
                Auto-install <code className="font-mono">requirements.txt</code> if present
              </span>
            </label>
          )}

          {/* Progress bar */}
          {phase === "uploading" && (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-[11px] text-muted-foreground font-mono">
                <span className="flex items-center gap-1.5">
                  <Loader2 size={11} className="animate-spin text-primary" />
                  <span>Installing…</span>
                </span>
                <span>{progress}%</span>
              </div>
              <div className="w-full h-2 rounded-full bg-muted overflow-hidden">
                <div
                  className="h-full bg-gradient-to-r from-primary to-cyan-500 transition-all duration-200"
                  style={{ width: `${progress}%` }}
                />
              </div>
            </div>
          )}

          {/* Success */}
          {phase === "success" && result && (
            <div className="p-4 rounded-lg bg-emerald-500/5 border border-emerald-500/25 space-y-2">
              <div className="flex items-center gap-2">
                <CheckCircle2 size={14} className="text-emerald-500" />
                <span className="text-xs font-semibold text-emerald-600 dark:text-emerald-400">
                  Installed successfully
                </span>
              </div>
              <div className="text-[11px] font-mono text-muted-foreground space-y-0.5">
                <div>
                  <span className="text-foreground">{result.name}</span>{" "}
                  <span className="text-primary">v{result.version}</span>
                </div>
                <div>hash: {result.hash}</div>
                <div>
                  {result.file_count} files · {formatSize(result.size_bytes)}
                </div>
                {result.install_report.requirements_installed && (
                  <div className="text-emerald-500">
                    ✓ pip install completed
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Error */}
          {phase === "error" && errorMessage && (
            <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/25 flex items-start gap-2">
              <AlertCircle size={14} className="text-destructive shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <div className="text-xs font-semibold text-destructive">
                  Upload failed
                </div>
                <div className="text-[11px] text-muted-foreground mt-0.5 break-words">
                  {errorMessage}
                </div>
              </div>
            </div>
          )}

          {/* Footer */}
          <div className="flex items-center justify-end gap-2.5 pt-3 border-t border-border">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onClose}
              disabled={phase === "uploading"}
              className="text-xs cursor-pointer"
            >
              {phase === "success" ? "Done" : "Cancel"}
            </Button>
            {phase !== "success" && (
              <Button
                type="button"
                size="sm"
                onClick={handleUpload}
                disabled={!file || phase === "uploading"}
                className="bg-primary hover:bg-primary/90 text-primary-foreground font-medium text-xs px-4 shadow-xs cursor-pointer disabled:opacity-40"
              >
                {phase === "uploading" ? (
                  <>
                    <Loader2 size={12} className="mr-1.5 animate-spin" />
                    <span>Installing…</span>
                  </>
                ) : (
                  <span>Install Bundle</span>
                )}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default BundleUploadModal;
