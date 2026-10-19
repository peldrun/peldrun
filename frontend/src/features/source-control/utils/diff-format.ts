// frontend/src/features/source-control/utils/diff-format.ts

/**
 * Represents the classification of a single line in a unified diff.
 */
export type DiffLineType = "header" | "hunk" | "addition" | "deletion" | "context";

/**
 * Represents a parsed line from a unified diff string, ready for UI rendering.
 */
export interface ParsedDiffLine {
  type: DiffLineType;
  content: string;
  oldLineNumber?: number;
  newLineNumber?: number;
}

/**
 * Parses a raw unified diff string into an array of structured lines.
 * This allows the UI to render line numbers and apply syntax highlighting/colors.
 *
 * @param diffText The raw unified diff string from the backend.
 * @returns An array of ParsedDiffLine objects.
 */
export function parseUnifiedDiff(diffText: string): ParsedDiffLine[] {
  if (!diffText) return [];

  const lines = diffText.split("\n");
  const parsedLines: ParsedDiffLine[] = [];

  let currentOldLine = 0;
  let currentNewLine = 0;

  for (let i = 0; i < lines.length; i++) {
    const rawLine = lines[i];

    // Skip completely empty lines at the end of the diff
    if (i === lines.length - 1 && rawLine === "") continue;

    // 1. Hunk Header (e.g., @@ -12,7 +12,8 @@)
    if (rawLine.startsWith("@@")) {
      const match = rawLine.match(/@@ -(\d+),?\d* \+(\d+),?\d* @@/);
      if (match) {
        currentOldLine = parseInt(match[1], 10);
        currentNewLine = parseInt(match[2], 10);
      }
      parsedLines.push({
        type: "hunk",
        content: rawLine,
      });
      continue;
    }

    // 2. File Headers (e.g., --- a/file.ts, +++ b/file.ts, diff --git)
    if (
      rawLine.startsWith("diff --git") ||
      rawLine.startsWith("index ") ||
      rawLine.startsWith("--- ") ||
      rawLine.startsWith("+++ ")
    ) {
      parsedLines.push({
        type: "header",
        content: rawLine,
      });
      continue;
    }

    // 3. Additions
    if (rawLine.startsWith("+")) {
      parsedLines.push({
        type: "addition",
        content: rawLine.substring(1), // Remove the leading '+'
        newLineNumber: currentNewLine,
      });
      currentNewLine++;
      continue;
    }

    // 4. Deletions
    if (rawLine.startsWith("-")) {
      parsedLines.push({
        type: "deletion",
        content: rawLine.substring(1), // Remove the leading '-'
        oldLineNumber: currentOldLine,
      });
      currentOldLine++;
      continue;
    }

    // 5. Context (Unchanged lines usually start with a space)
    if (rawLine.startsWith(" ") || rawLine === "") {
      parsedLines.push({
        type: "context",
        content: rawLine.substring(1), // Remove the leading space
        oldLineNumber: currentOldLine,
        newLineNumber: currentNewLine,
      });
      currentOldLine++;
      currentNewLine++;
      continue;
    }

    // Fallback for any unexpected line format
    parsedLines.push({
      type: "context",
      content: rawLine,
      oldLineNumber: currentOldLine,
      newLineNumber: currentNewLine,
    });
    currentOldLine++;
    currentNewLine++;
  }

  return parsedLines;
}

/**
 * Formats the addition and deletion counts into a standard short string.
 * e.g., "+42 -8"
 */
export function formatDiffStats(insertions: number, deletions: number): string {
  const parts: string[] = [];
  if (insertions > 0) parts.push(`+${insertions}`);
  if (deletions > 0) parts.push(`-${deletions}`);
  return parts.length > 0 ? parts.join(" ") : "0";
}

/**
 * Returns a user-friendly label for a file change type.
 */
export function formatChangeType(changeType: string): string {
  switch (changeType.toLowerCase()) {
    case "added": return "Added";
    case "modified": return "Modified";
    case "deleted": return "Deleted";
    case "renamed": return "Renamed";
    case "copied": return "Copied";
    default: return "Changed";
  }
}
