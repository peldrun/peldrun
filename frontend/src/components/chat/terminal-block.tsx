// frontend/src/components/chat/terminal-block.tsx
//
// Shell command block — a Windows-Terminal / PowerShell-inspired
// container for displaying shell commands that an agent executed.
//
// Design goals:
//   * Feel like a real terminal window (window chrome, prompt prefix,
//     monospace body) so the user instantly recognizes "this is a
//     shell command that ran".
//   * Auto-detect the shell type from the command content so the label
//     and prompt match what actually executed on the host.
//   * Preserve the copy/download contract of CodeBlock: copying gives
//     the raw command WITHOUT the prompt prefix, so users can paste it
//     straight into a terminal.
//   * Collapse long commands after 8 lines with an Expand button.

"use client";

import React, { useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  Download,
  Power,
} from "lucide-react";

export type ShellKind = "powershell" | "cmd" | "bash";
export type ShellSelection = ShellKind | "auto";

export interface TerminalBlockProps {
  /** The raw shell command text (one or more lines). */
  command: string;
  /**
   * Which shell to render as. "auto" (default) inspects the command
   * content to pick the most accurate label and theme.
   */
  shell?: ShellSelection;
  /** Working directory shown in the prompt prefix. Default: "workspace". */
  cwd?: string;
  /** Filename used when downloading. Defaults per shell kind. */
  filename?: string;
  className?: string;
}

interface ShellMeta {
  label: string;
  prompt: string;
  accent: string;
  bgColor: string;
  headerBg: string;
  borderColor: string;
  promptColor: string;
  defaultFilename: string;
}

// Windows Terminal / PowerShell-inspired palette.
// - powershell: the classic #012456 blue of Windows PowerShell.
// - cmd / bash: Windows Terminal "Campbell" dark palette.
const SHELL_META: Record<ShellKind, ShellMeta> = {
  powershell: {
    label: "Windows PowerShell",
    prompt: "PS",
    accent: "#5391fe",
    bgColor: "#012456",
    headerBg: "#001833",
    borderColor: "#0a3060",
    promptColor: "#5391fe",
    defaultFilename: "command.ps1",
  },
  cmd: {
    label: "Command Prompt",
    prompt: ">",
    accent: "#cccccc",
    bgColor: "#0c0c0c",
    headerBg: "#1a1a1a",
    borderColor: "#2a2a2a",
    promptColor: "#cccccc",
    defaultFilename: "command.bat",
  },
  bash: {
    label: "Terminal",
    prompt: "$",
    accent: "#4ec9b0",
    bgColor: "#0c0c0c",
    headerBg: "#1a1a1a",
    borderColor: "#2a2a2a",
    promptColor: "#4ec9b0",
    defaultFilename: "command.sh",
  },
};

/**
 * Pick the most likely shell kind for a command.
 *
 * Detection strategy (best-effort; only affects the label and theme,
 * never behavior):
 *   * `powershell` / `pwsh` prefix       → powershell
 *   * Windows cmd-specific syntax         → cmd
 *     (`type nul`, `%VAR%`, `cmd /c`, `D:\...`)
 *   * anything else                       → bash (generic Terminal)
 */
function detectShellKind(
  command: string,
  requested: ShellSelection,
): ShellKind {
  if (requested !== "auto") return requested;
  const lower = (command || "").toLowerCase().trim();

  if (
    lower.startsWith("powershell") ||
    lower.startsWith("pwsh") ||
    lower.includes("get-childitem") ||
    lower.includes("set-location") ||
    lower.includes("invoke-webrequest")
  ) {
    return "powershell";
  }

  if (
    lower.startsWith("type nul") ||
    /^[a-z]:\\/.test(lower) ||
    lower.includes("%userprofile%") ||
    lower.includes("%appdata%") ||
    lower.includes("cmd /c") ||
    lower.includes("cmd.exe")
  ) {
    return "cmd";
  }

  return "bash";
}

export function TerminalBlock({
  command = "",
  shell = "auto",
  cwd,
  filename,
  className = "",
}: TerminalBlockProps): React.JSX.Element {
  const [copied, setCopied] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);

  const cleanCommand = String(command ?? "").trim();
  const lines = cleanCommand.split("\n");
  const lineCount = lines.length;
  const isLong = lineCount > 8 || cleanCommand.length > 240;

  const kind: ShellKind = detectShellKind(cleanCommand, shell);
  const meta = SHELL_META[kind];
  const effectiveFilename = filename || meta.defaultFilename;
  const effectiveCwd = cwd || "workspace";

  // Prompt prefix differs per shell:
  //   PowerShell → "PS C:\workspace>"
  //   cmd        → "C:\workspace>"
  //   bash       → "$"
  const promptPrefix =
    kind === "powershell"
      ? `PS C:\\${effectiveCwd}>`
      : kind === "cmd"
      ? `C:\\${effectiveCwd}>`
      : "$";

  const handleCopy = async () => {
    if (!cleanCommand) return;
    try {
      if (typeof window !== "undefined" && navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(cleanCommand);
      } else {
        // Fallback for non-secure contexts (older browsers, HTTP).
        const ta = document.createElement("textarea");
        ta.value = cleanCommand;
        ta.style.position = "fixed";
        ta.style.left = "-999999px";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Clipboard copy failed:", err);
    }
  };

  const handleDownload = () => {
    if (!cleanCommand) return;
    const blob = new Blob([cleanCommand], {
      type: "text/plain;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = effectiveFilename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const visibleLines = isLong && !isExpanded ? lines.slice(0, 8) : lines;
  const hiddenLineCount = lineCount - visibleLines.length;

  return (
    <div
      dir="ltr"
      className={`my-3 overflow-hidden rounded-lg border shadow-md font-mono ${className}`}
      style={{ borderColor: meta.borderColor }}
    >
      {/* Window chrome / title bar */}
      <div
        className="flex items-center justify-between px-3 py-1.5"
        style={{ backgroundColor: meta.headerBg }}
      >
        <div className="flex items-center gap-2 min-w-0">
          {/* Traffic-light dots (modern terminal aesthetic) */}
          <div className="flex items-center gap-1.5 flex-shrink-0">
            <span className="w-2.5 h-2.5 rounded-full bg-[#ff5f57]" />
            <span className="w-2.5 h-2.5 rounded-full bg-[#febc2e]" />
            <span className="w-2.5 h-2.5 rounded-full bg-[#28c840]" />
          </div>

          {/* Shell identity pill */}
          <div
            className="flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] font-semibold flex-shrink-0"
            style={{
              backgroundColor: `${meta.accent}22`,
              color: meta.accent,
            }}
          >
            <Power size={10} />
            <span>{meta.label}</span>
          </div>

          <span className="text-[10px] text-slate-400/70 truncate hidden sm:inline">
            {lineCount} line{lineCount === 1 ? "" : "s"}
          </span>
        </div>

        <div className="flex items-center gap-1.5 flex-shrink-0">
          <button
            type="button"
            onClick={handleDownload}
            className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] text-slate-300 hover:text-white hover:bg-white/10 transition-all cursor-pointer"
            title={`Download as ${effectiveFilename}`}
          >
            <Download size={11} />
            <span className="hidden sm:inline">Save</span>
          </button>

          <button
            type="button"
            onClick={handleCopy}
            className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] text-slate-300 hover:text-white hover:bg-white/10 transition-all cursor-pointer"
            title="Copy command to clipboard (without prompt prefix)"
          >
            {copied ? (
              <>
                <Check size={11} className="text-emerald-400" />
                <span className="text-emerald-400">Copied</span>
              </>
            ) : (
              <>
                <Copy size={11} />
                <span>Copy</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Terminal body */}
      <div
        className="p-3 overflow-x-auto text-[11px] leading-relaxed"
        style={{ backgroundColor: meta.bgColor, color: "#eeeeee" }}
      >
        <pre className="m-0 whitespace-pre font-mono">
          {visibleLines.map((line, idx) => (
            <div key={idx} className="flex">
              {/* Prompt prefix — visible on the first line only.
                  Subsequent lines are indented to align with the first
                  command character, giving the appearance of a wrapped
                  multi-line command inside a real terminal. */}
              <span
                className="select-none flex-shrink-0 mr-2 font-semibold"
                style={{ color: meta.promptColor }}
              >
                {idx === 0 ? promptPrefix : " ".repeat(promptPrefix.length)}
              </span>
              <span className="whitespace-pre-wrap break-words">
                {line}
              </span>
            </div>
          ))}
        </pre>

        {hiddenLineCount > 0 && (
          <div className="text-[10px] text-slate-400/70 mt-1 italic">
            … {hiddenLineCount} more line
            {hiddenLineCount === 1 ? "" : "s"} hidden
          </div>
        )}
      </div>

      {/* Expand / collapse for long commands */}
      {isLong && (
        <div
          className="flex items-center justify-center p-1.5 border-t"
          style={{
            backgroundColor: meta.headerBg,
            borderColor: meta.borderColor,
          }}
        >
          <button
            type="button"
            onClick={() => setIsExpanded(!isExpanded)}
            className="flex items-center gap-1.5 text-[11px] font-medium hover:underline transition-all cursor-pointer py-0.5"
            style={{ color: meta.accent }}
          >
            {isExpanded ? (
              <>
                <ChevronUp size={12} />
                <span>Collapse command</span>
              </>
            ) : (
              <>
                <ChevronDown size={12} />
                <span>
                  Expand full command ({lineCount} lines)
                </span>
              </>
            )}
          </button>
        </div>
      )}
    </div>
  );
}

export default TerminalBlock;
