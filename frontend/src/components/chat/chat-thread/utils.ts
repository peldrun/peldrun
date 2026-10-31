// Path: frontend/src/components/chat/chat-thread/utils.ts
/**
 * Pure helper functions used by the chat thread renderers.
 *
 * These utilities contain no React state or side effects, so they can be
 * safely imported by any sub-component without creating render loops or
 * coupling to the container.
 */

import type { AttachedUserFile } from "./types";

import { StepEvent } from "../chat-timeline";
import { OperationResponse } from "@/features/source-control/api/schemas";

/**
 * Splits a raw prompt into its human-readable display text and a list of
 * attached files. This handles two attachment sources:
 *
 *  1. Explicitly-passed `explicitFiles` (structured attachments).
 *  2. An inline "[Uploaded Files]:" summary block that the backend may
 *     append to the prompt text.
 *
 * A special case clears the placeholder prompt "Inspect workspace files:"
 * so the UI does not display a redundant instruction.
 */
export function parsePromptAndAttachments(
  rawPrompt: string,
  explicitFiles?: AttachedUserFile[]
): { displayText: string; attachedFiles: AttachedUserFile[] } {
  let cleanedText = rawPrompt || "";
  const files: AttachedUserFile[] = explicitFiles ? [...explicitFiles] : [];

  const filesSummaryRegex = /(?:\n\n|\n)?[Uploaded Files]:\n([\s\S]*)$/i;
  const match = cleanedText.match(filesSummaryRegex);

  if (match) {
    const listBlock = match[1];
    cleanedText = cleanedText.replace(filesSummaryRegex, "").trim();

    const lines = listBlock.split("\n");
    for (const line of lines) {
      const lineMatch = line.trim().match(/^-\s*([^\s(]+)/);
      if (lineMatch) {
        const extractedName = lineMatch[1].trim();
        if (extractedName && !files.some((f) => f.name === extractedName)) {
          files.push({ name: extractedName, path: extractedName });
        }
      }
    }
  }

  // [FIX-5.5] Extract @[filename] markers emitted by the composer when the
  // user selects a file from the `@` typeahead menu. Each marker is moved
  // into the `attachedFiles` list so it renders as a chip above the message
  // rather than as inline code (the previous backtick-based behavior).
  const refPattern = /@\[([^\]]+)\]/g;
  const refMatches = [...cleanedText.matchAll(refPattern)];
  for (const m of refMatches) {
    const refName = m[1].trim();
    if (refName && !files.some((f) => f.name === refName)) {
      files.push({ name: refName, path: refName });
    }
  }
  cleanedText = cleanedText.replace(refPattern, "").replace(/\s{2,}/g, " ").trim();

  if (cleanedText === "Inspect workspace files:") {
    cleanedText = "";
  }

  return { displayText: cleanedText, attachedFiles: files };
}

/**
 * Resolves a file's category from its extension and returns booleans for
 * each supported media type, together with an optional preview URL.
 *
 * The `isCode` flag is treated as the fallback category when the file does
 * not match any of the recognized media groups.
 */
export function resolveFileCategory(
  fileName: string,
  customUrl?: string,
  fileId?: string
) {
  const extension = fileName.includes(".")
    ? fileName.slice(fileName.lastIndexOf(".")).toLowerCase()
    : "";

  const isImage = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"].includes(extension);
  const isPdf = extension === ".pdf";
  const isData = [".csv", ".tsv", ".xlsx", ".xls"].includes(extension);
  const isDoc = [".doc", ".docx", ".txt", ".rtf", ".odt", ".md"].includes(extension);
  const isAudioVideo = [".mp3", ".wav", ".ogg", ".mp4", ".mov", ".mkv", ".webm"].includes(extension);
  const isArchive = [".zip", ".tar", ".gz", ".rar", ".7z"].includes(extension);
  const isCode =
    !isImage &&
    !isPdf &&
    !isData &&
    !isDoc &&
    !isAudioVideo &&
    !isArchive;

  // Preview URL resolution order (images only):
  //   1. Explicit custom URL passed by the caller.
  //   2. Temporary artifact store — used by images uploaded via
  //      POST /api/files/upload-temp. This is the only path that
  //      survives a page reload, since data is served by the backend
  //      rather than held in a browser-only blob URL.
  //   3. Legacy workspace raw endpoint — covers attachments uploaded
  //      via POST /api/files/upload (workspace / chat files).
  // Non-images never have a preview URL (they render as icon tiles).
  let previewUrl: string | undefined;
  if (customUrl) {
    previewUrl = customUrl;
  } else if (isImage && fileId) {
    previewUrl = `/api/files/temp/${fileId}`;
  } else if (isImage) {
    previewUrl = `/api/files/raw/${fileName}`;
  } else {
    previewUrl = undefined;
  }

  return {
    isImage,
    isPdf,
    isData,
    isDoc,
    isAudioVideo,
    isArchive,
    isCode,
    previewUrl,
  };
}


export function extractOperationFromSteps(steps: StepEvent[]): OperationResponse | null {
  for (const step of steps) {
    // Identify the request_approval observation by payload shape,
    // not by toolName. The SSE bridge does not guarantee that
    // `tool_name` is present on observation events (peldrun-core may
    // omit it), and the previous check
    // `step.toolName === "source_control"` therefore never matched —
    // which is why the ChatOperationCard never rendered and the
    // Source Control panel stayed idle after a successful
    // request_approval. The payload is self-identifying: only a
    // successful request_approval response contains both
    // `approval_id` and `operation_id`.
    if (step.type !== "observation") continue;
    const raw = typeof step.content === "string" ? step.content.trim() : "";
    if (!raw || !raw.startsWith("{")) continue;
    try {
      const data = JSON.parse(raw);
      if (
        data &&
        typeof data === "object" &&
        typeof data.approval_id === "string" &&
        typeof data.operation_id === "string"
      ) {
        return {
          operation_id: data.operation_id,
          operation_type: data.operation || "push_branch",
          provider: "github",
          repository_id: data.repository_id || "unknown",
          target_branch: data.target_branch || "main",
          current_state: "review_required",
          approval_id: data.approval_id,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          events: [],
          metadata: {},
        };
      }
    } catch {
      // Not JSON; skip.
    }
  }
  return null;
}

export function filterApprovalSteps(steps: StepEvent[]): StepEvent[] {
  return steps.filter((s) => {
    if (s.type !== "observation") return true;
    const raw = typeof s.content === "string" ? s.content.trim() : "";
    if (!raw.startsWith("{") || !raw.includes("approval_id")) return true;
    try {
      const data = JSON.parse(raw);
      // Hide the raw observation from the timeline only when it is a
      // real request_approval payload; the ChatOperationCard will
      // render it prominently.
      return !(data?.approval_id && data?.operation_id);
    } catch {
      return true;
    }
  });
}
