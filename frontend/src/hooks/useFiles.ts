/**
 * frontend/src/hooks/useFiles.ts
 *
 * Authoritative React hook managing workspace files and deliverables under Phase M2.
 * Replaces aggressive interval polling with push synchronization:
 * - Subscribes to live SSE artifact lifecycle custom events.
 * - Manages canonical file identities via relative paths.
 * - Provides non-intrusive reconciliation on demand or reconnection.
 */

"use client";

import { useState, useEffect, useCallback, useRef } from "react";

export interface WorkspaceFile {
  name: string;
  path: string;
  size_bytes?: number;
  mime_type?: string;
  revision?: number;
}

export interface UseFilesReturn {
  files: WorkspaceFile[];
  loading: boolean;
  error: string | null;
  selectedFile: string | null;
  setSelectedFile: (file: string | null) => void;
  reconcile: () => Promise<void>;
}

export function useFiles(scopeId?: string | null): UseFilesReturn {
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);

  const scopeRef = useRef<string | null>(scopeId || null);
  scopeRef.current = scopeId || null;

  // Authoritative reconciliation endpoint fetching cumulative files
  const reconcile = useCallback(async () => {
    const currentScope = scopeRef.current;
    if (!currentScope) {
      setFiles([]);
      return;
    }

    try {
      setLoading(true);
      let res = await fetch(`/api/chats/${currentScope}/files?t=${Date.now()}`);
      if (!res.ok) {
        res = await fetch(`/api/run/jobs/${currentScope}/files?t=${Date.now()}`);
      }

      if (res.ok) {
        const data = await res.json();
        const rawFiles: Array<{ name: string; path?: string; size_bytes?: number }> = data.files || [];
        
        // Normalize canonical relative paths
        const normalized: WorkspaceFile[] = rawFiles.map((f) => ({
          name: f.name,
          path: (f.path || f.name).replace(/\\/g, "/"),
          size_bytes: f.size_bytes,
        }));

        setFiles(normalized);
        setError(null);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to reconcile files";
      setError(msg);
      console.error("[useFiles] Reconciliation error:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial fetch upon scope change
  useEffect(() => {
    if (scopeId) {
      reconcile();
    } else {
      setFiles([]);
    }
  }, [scopeId, reconcile]);

  // Push synchronization event listeners
  useEffect(() => {
    const handleArtifactCreated = (e: Event) => {
      const ce = e as CustomEvent<{
        artifact?: string;
        path?: string;
        relative_path?: string;
        name?: string;
        size_bytes?: number;
        revision?: number;
      }>;
      
      const payload = ce.detail;
      const canonicalPath = (payload?.relative_path || payload?.path || payload?.artifact || "").replace(/\\/g, "/");
      if (!canonicalPath) return;

      const fileName = payload?.name || canonicalPath.split("/").pop() || canonicalPath;

      setFiles((prev) => {
        // Prevent duplicate entries by path
        const exists = prev.some((item) => item.path === canonicalPath);
        if (exists) {
          return prev.map((item) =>
            item.path === canonicalPath
              ? {
                  ...item,
                  size_bytes: payload.size_bytes ?? item.size_bytes,
                  revision: payload.revision ?? item.revision,
                }
              : item
          );
        }
        return [
          ...prev,
          {
            name: fileName,
            path: canonicalPath,
            size_bytes: payload.size_bytes,
            revision: payload.revision || 1,
          },
        ];
      });
    };

    const handleArtifactUpdated = (e: Event) => {
      handleArtifactCreated(e);
    };

    const handleReconcileRequest = () => {
      reconcile();
    };

    window.addEventListener("peldrun:artifact-created", handleArtifactCreated);
    window.addEventListener("peldrun:artifact-updated", handleArtifactUpdated);
    window.addEventListener("peldrun:reconcile-files", handleReconcileRequest);

    return () => {
      window.removeEventListener("peldrun:artifact-created", handleArtifactCreated);
      window.removeEventListener("peldrun:artifact-updated", handleArtifactUpdated);
      window.removeEventListener("peldrun:reconcile-files", handleReconcileRequest);
    };
  }, [reconcile]);

  return {
    files,
    loading,
    error,
    selectedFile,
    setSelectedFile,
    reconcile,
  };
}