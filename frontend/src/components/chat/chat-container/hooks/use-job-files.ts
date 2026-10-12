// Path: frontend/src/components/chat/chat-container/hooks/use-job-files.ts
"use client";

import { useCallback, useState } from "react";
import type { ProducedFile } from "../types";

/**
 * Fetches the files produced by a given job.
 *
 * The backend may return either `delta_files` (incremental list) or `files`
 * (full list). Both shapes are accepted and normalised to a flat array.
 */
export function useJobFiles() {
  const [producedFiles, setProducedFiles] = useState<ProducedFile[]>([]);

  const fetchJobFiles = useCallback(async (jobId: string) => {
    try {
      const res = await fetch(`/api/run/jobs/${jobId}/files`);
      if (res.ok) {
        const data = await res.json();
        const turnFiles: ProducedFile[] =
          data.delta_files !== undefined ? data.delta_files : data.files || [];
        setProducedFiles(turnFiles);
      }
    } catch (e) {
      console.error("Error fetching job files", e);
    }
  }, []);

  return { producedFiles, setProducedFiles, fetchJobFiles };
}
