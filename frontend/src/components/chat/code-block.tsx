"use client";

import React, { useState } from "react";
import { Highlight, themes } from "prism-react-renderer";
import { Check, Copy, Download, FileCode, ChevronDown, ChevronUp, Terminal, PanelRightOpen } from "lucide-react";

export interface CodeBlockProps {
  code: string;
  language?: string;
  filename?: string;
  className?: string;
}

const languageAliases: Record<string, string> = {
  py: "python",
  python: "python",
  js: "javascript",
  javascript: "javascript",
  ts: "typescript",
  typescript: "typescript",
  jsx: "jsx",
  tsx: "tsx",
  html: "markup",
  xml: "markup",
  svg: "markup",
  css: "css",
  json: "json",
  sh: "bash",
  bash: "bash",
  shell: "bash",
  zsh: "bash",
  powershell: "bash",
  ps1: "bash",
  sql: "sql",
  yaml: "yaml",
  yml: "yaml",
  md: "markdown",
  markdown: "markdown",
  rust: "rust",
  rs: "rust",
  go: "go",
  c: "c",
  cpp: "cpp",
};

const extensionMap: Record<string, string> = {
  python: "py",
  javascript: "js",
  typescript: "ts",
  jsx: "jsx",
  tsx: "tsx",
  markup: "html",
  css: "css",
  json: "json",
  bash: "sh",
  sql: "sql",
  yaml: "yaml",
  markdown: "md",
  rust: "rs",
  go: "go",
  c: "c",
  cpp: "cpp",
};

export function CodeBlock({
  code = "",
  language = "text",
  filename,
  className = "",
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);

  const cleanCode = typeof code === "string" ? code.trim() : String(code || "");
  const lines = cleanCode.split("\n");
  const isLong = lines.length > 20;

  const normalizedLang = languageAliases[language.toLowerCase()] || "javascript";
  const displayLang = (language || "TEXT").toUpperCase();
  const fileExt = extensionMap[normalizedLang] || "txt";
  const defaultFilename = filename || `snippet.${fileExt}`;

  const handleCopy = async () => {
    if (!cleanCode) return;
    try {
      if (typeof window !== "undefined" && navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(cleanCode);
      } else {
        const textArea = document.createElement("textarea");
        textArea.value = cleanCode;
        textArea.style.position = "fixed";
        textArea.style.left = "-999999px";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand("copy");
        textArea.remove();
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Clipboard copy failed:", err);
    }
  };

  const handleDownload = () => {
    if (!cleanCode) return;
    const blob = new Blob([cleanCode], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = defaultFilename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const handleOpenInSandbox = () => {
    if (!cleanCode) return;
    if (typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("peldrun:open-in-sandbox", {
          detail: {
            code: cleanCode,
            language: normalizedLang,
            filename: defaultFilename,
          },
        })
      );
    }
  };

  const HighlightComponent = Highlight as any;

  return (
    <div dir="ltr" className={`my-3 overflow-hidden rounded-sm border border-border bg-[#0d1117] text-slate-100 shadow-peldrun-sm font-mono ${className}`}>
      <div className="flex items-center justify-between px-3.5 py-2 border-b border-border/60 bg-[#161b22]">
        <div className="flex items-center gap-2 truncate">
          {normalizedLang === "bash" ? (
            <Terminal size={13} className="text-primary flex-shrink-0" />
          ) : (
            <FileCode size={13} className="text-card flex-shrink-0" />
          )}
          <span className="font-mono text-xs font-semibold text-muted-foreground/90 truncate max-w-[180px] sm:max-w-none">
            {defaultFilename}
          </span>
          <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-muted/10 text-card ">
            {displayLang}
          </span>
          <span className="hidden sm:inline-block text-[11px] font-mono text-muted-foreground">
            ({lines.length} lines)
          </span>
        </div>

        <div className="flex items-center gap-1.5 flex-shrink-0">
          <button
            type="button"
            onClick={handleOpenInSandbox}
            className="flex items-center gap-1 px-2 py-1 rounded text-[11px] text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-all cursor-pointer"
            title="Open snippet in Workspace Sandbox"
          >
            <PanelRightOpen size={12} className="text-primary" />
            <span className="hidden sm:inline">Sandbox</span>
          </button>

          <button
            type="button"
            onClick={handleDownload}
            className="flex items-center gap-1 px-2 py-1 rounded text-[11px] text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-all cursor-pointer"
            title="Download Snippet"
          >
            <Download size={12} />
            <span className="hidden sm:inline">Save</span>
          </button>

          <button
            type="button"
            onClick={handleCopy}
            className="flex items-center gap-1 px-2.5 py-1 rounded text-[11px] font-medium bg-mist-950 hover:bg-muted text-muted-foreground hover:text-foreground transition-all cursor-pointer border border-border/50 shadow-peldrun-xs"
            title="Copy Code to Clipboard"
          >
            {copied ? (
              <>
                <Check size={12} className="text-peldrun-success" />
                <span className="text-peldrun-success font-mono">Copied</span>
              </>
            ) : (
              <>
                <Copy size={12} />
                <span className="font-mono">Copy</span>
              </>
            )}
          </button>
        </div>
      </div>

      <div className={`relative transition-all ${isLong && !isExpanded ? "max-h-[380px] overflow-y-auto" : "overflow-x-auto"}`}>
        <HighlightComponent
          theme={themes.nightOwl}
          code={cleanCode}
          language={normalizedLang}
        >
          {({ className: highlightClass, style, tokens, getLineProps, getTokenProps }: any) => (
            <pre
              className={`${highlightClass} p-3.5 text-xs font-mono leading-relaxed bg-transparent m-0 min-w-full`}
              style={style}
            >
              {tokens.map((line: any, i: number) => (
                <div key={i} {...getLineProps({ line })} className="table-row">
                  <span className="table-cell select-none pr-4 text-right text-[11px] font-mono opacity-35 w-8">
                    {i + 1}
                  </span>
                  <span className="table-cell whitespace-pre">
                    {line.map((token: any, key: number) => (
                      <span key={key} {...getTokenProps({ token })} />
                    ))}
                  </span>
                </div>
              ))}
            </pre>
          )}
        </HighlightComponent>
      </div>

      {isLong && (
        <div className="flex items-center justify-center p-1.5 border-t border-border/40 bg-[#161b22]/70">
          <button
            type="button"
            onClick={() => setIsExpanded(!isExpanded)}
            className="flex items-center gap-1.5 text-xs font-medium text-primary hover:underline transition-all cursor-pointer py-0.5"
          >
            {isExpanded ? (
              <>
                <ChevronUp size={13} />
                <span>Collapse snippet</span>
              </>
            ) : (
              <>
                <ChevronDown size={13} />
                <span>Expand full view ({lines.length} lines)</span>
              </>
            )}
          </button>
        </div>
      )}
    </div>
  );
}

export default CodeBlock;
