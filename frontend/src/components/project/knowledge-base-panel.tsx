// frontend/src/components/project/knowledge-base-panel.tsx

"use client";

/**
 * Knowledge Base panel for a single project.
 *
 * Talks to:
 *   GET    /api/projects/{id}/knowledge/files
 *   POST   /api/projects/{id}/knowledge/files          (multipart)
 *   DELETE /api/projects/{id}/knowledge/files?name=...
 *
 * The backend stores files under `<workspace>/.peldrun/knowledge/`,
 * which the agent reads with its file tools when the user asks
 * about them. Changes here apply on the next agent run — no
 * restart is needed.
 *
 * UI contract:
 *   - The list refreshes after every successful upload / delete.
 *   - Upload skips with a per-file toast instead of failing the
 *     whole batch (matches the backend's `skipped` response).
 *   - Delete requires a confirm dialog, matching the settings
 *     page's existing UX.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import { FileText, Loader2, Trash2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConfirmModal } from "@/components/ui/ConfirmModal";
import { showToast } from "@/components/ui/ToastNotification";

interface KnowledgeFile {
  name: string;
  path: string;
  size: number;
}

const SKIP_REASONS: Record<string, string> = {
  exists: "already exists",
  too_large: "larger than 20 MB",
  limit_reached: "file limit reached",
  invalid_name: "invalid name",
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function KnowledgeBasePanel({ projectId }: { projectId: string }) {
  const base = `/api/projects/${encodeURIComponent(projectId)}/knowledge/files`;
  const { confirm, ConfirmDialog } = useConfirmModal();
  const inputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<KnowledgeFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(base, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setFiles(Array.isArray(data.files) ? data.files : []);
    } catch (err: any) {
      showToast.error("Could not load files", err?.message || "Unknown error");
    } finally {
      setLoading(false);
    }
  }, [base]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleUpload = async (list: FileList | null) => {
    if (!list || list.length === 0) return;
    setUploading(true);
    try {
      const form = new FormData();
      Array.from(list).forEach((f) => form.append("files", f));
      const res = await fetch(base, { method: "POST", body: form });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      const skipped: { name: string; reason: string }[] = data.skipped || [];
      if (data.files?.length) {
        showToast.success(
          "Files added",
          `${data.files.length} file(s) added to the knowledge base.`,
        );
      }
      if (skipped.length) {
        showToast.error(
          "Some files were skipped",
          skipped
            .map((s) => `${s.name}: ${SKIP_REASONS[s.reason] || s.reason}`)
            .join(", "),
        );
      }
      await refresh();
    } catch (err: any) {
      showToast.error("Upload failed", err?.message || "Could not upload files.");
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = ""; // allow re-selecting the same file
    }
  };

  const handleDelete = async (file: KnowledgeFile) => {
    const ok = await confirm({
      title: "Remove file",
      description: `Remove ${file.name} from the knowledge base? The agent will no longer see it.`,
      variant: "danger",
      confirmText: "Remove",
    });
    if (!ok) return;
    try {
      const res = await fetch(
        `${base}?name=${encodeURIComponent(file.name)}`,
        { method: "DELETE" },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      await refresh();
    } catch (err: any) {
      showToast.error("Delete failed", err?.message || "Could not remove the file.");
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">
          {files.length} file(s)
        </span>
        <input
          ref={inputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => handleUpload(e.target.files)}
        />
        <Button
          variant="outline"
          className="gap-2"
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
        >
          {uploading ? (
            <Loader2 size={15} className="animate-spin" />
          ) : (
            <Upload size={15} />
          )}
          Add files
        </Button>
      </div>

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : files.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No files yet. Add specs, guidelines, or reference documents the
          agent should know about.
        </p>
      ) : (
        <ul className="divide-y divide-muted border border-muted rounded-md">
          {files.map((file) => (
            <li
              key={file.path}
              className="flex items-center gap-3 px-3.5 py-2.5"
            >
              <FileText
                size={15}
                className="text-muted-foreground shrink-0"
              />
              <span
                className="text-sm truncate flex-1"
                title={file.path}
              >
                {file.name}
              </span>
              <span className="text-xs text-muted-foreground">
                {formatSize(file.size)}
              </span>
              <button
                type="button"
                aria-label={`Remove ${file.name}`}
                onClick={() => handleDelete(file)}
                className="text-muted-foreground hover:text-destructive"
              >
                <Trash2 size={15} />
              </button>
            </li>
          ))}
        </ul>
      )}
      <ConfirmDialog />
    </div>
  );
}
