// Path: frontend/src/components/chat/chat-thread/utils.ts
/**
 * Pure helper functions used by the chat thread renderers.
 *
 * These utilities contain no React state or side effects, so they can be
 * safely imported by any sub-component without creating render loops or
 * coupling to the container.
 */

import type { AttachedUserFile } from "./types";

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
export function resolveFileCategory(fileName: string, customUrl?: string) {
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

  const previewUrl = customUrl || (isImage ? `/api/files/raw/${fileName}` : undefined);

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
