// frontend/src/features/source-control/components/git/FileViewer.tsx

import React, { useEffect, useState } from "react";
import { Highlight, themes } from "prism-react-renderer";
import { AlertCircle, FileText } from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type { FileContentAtRefResponse } from "../../api/schemas";

export interface FileViewerProps {
  workspaceId: string;
  ref: string;
  path: string;
}

/**
 * Renders the content of a single file at a specific Git ref.
 *
 * The component fetches the COMMITTED content (not the working-tree
 * content) via the backend and renders it with syntax highlighting.
 * It handles four states:
 *
 *   - Loading: a placeholder.
 *   - Error: a diagnostic banner with the message from the backend.
 *   - Binary: a placeholder instead of attempting to render bytes.
 *   - No selection: a hint prompting the user to pick a file.
 *
 * Stale-response handling:
 *   If the user clicks several files quickly, only the last
 *   selection's response is applied to component state. This is
 *   implemented with a per-effect `cancelled` flag rather than an
 *   AbortController, because `sourceControlClient.getFileContent`
 *   does not accept an AbortSignal. Adding abort support to the
 *   client is a separate concern (it would also need to propagate
 *   through `request<T>` and every call site), so it is
 *   intentionally out of scope here.
 */
export function FileViewer({ workspaceId, ref, path }: FileViewerProps) {
  const [data, setData] = useState<FileContentAtRefResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!workspaceId || !ref || !path) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

    sourceControlClient
      .getFileContent(workspaceId, ref, path)
      .then((res) => {
        if (!cancelled) {
          setData(res);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message =
            err instanceof Error ? err.message : "Failed to load file.";
          setError(message);
          setData(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [workspaceId, ref, path]);

  const language = detectLanguage(path);

  return (
    <div className="flex flex-col h-full bg-background">
      {/* Header: path + ref + size */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-border bg-card/40 shrink-0">
        <FileText className="w-4 h-4 text-muted-foreground shrink-0" />
        <span className="text-xs font-medium text-foreground truncate">
          {path}
        </span>
        <span className="text-[10px] text-muted-foreground ml-auto shrink-0">
          {ref}
          {data && !data.is_binary && ` · ${formatBytes(data.size_bytes)}`}
          {data?.is_binary && " · binary"}
        </span>
      </div>

      {/* Truncation banner */}
      {data?.is_truncated && !data.is_binary && (
        <div className="flex items-center gap-2 px-4 py-2 border-b border-yellow-300 bg-yellow-50 text-yellow-800 text-xs shrink-0">
          <AlertCircle className="w-3.5 h-3.5 shrink-0" />
          <span>
            This file is too large and was truncated. Only the first part
            is shown.
          </span>
        </div>
      )}

      {/* Body */}
      <div className="flex-1 overflow-auto ">
        {loading && (
          <div className="p-4 text-xs text-muted-foreground">
            Loading file…
          </div>
        )}

        {!loading && error && (
          <div className="p-4 flex items-start gap-2 text-xs text-red-700">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {!loading && !error && data?.is_binary && (
          <div className="p-4 text-xs text-muted-foreground">
            Binary file · {formatBytes(data.size_bytes)} — content preview
            is not available.
          </div>
        )}

        {!loading && !error && data && !data.is_binary && (
          <Highlight
            theme={themes.vsDark}
            code={data.content ?? ""}
            language={language}
          >
            {({ className, style, tokens, getLineProps, getTokenProps }) => (
              <pre
                className={className}
                style={{
                  ...style,
                  margin: 0,
                  padding: "1rem",
                  fontSize: "0.8125rem",
                  lineHeight: "1.5",
                  overflowX: "auto",
                }}
              >
                {tokens.map((line, i) => {
                  const lineProps = getLineProps({ line });
                  return (
                    <div
                      key={i}
                      {...lineProps}
                      style={{ ...lineProps.style, display: "flex" }}
                    >
                      <span
                        style={{
                          minWidth: "2.5em",
                          paddingRight: "1em",
                          color: "rgba(148, 163, 184, 0.5)",
                          userSelect: "none",
                          textAlign: "right",
                          flexShrink: 0,
                        }}
                      >
                        {i + 1}
                      </span>
                      <span style={{ flex: 1 }}>
                        {line.map((token, key) => {
                          const tokenProps = getTokenProps({ token });
                          return <span key={key} {...tokenProps} />;
                        })}
                      </span>
                    </div>
                  );
                })}
              </pre>
            )}
          </Highlight>
        )}

        {!loading && !error && !data && (
          <div className="p-4 text-xs text-muted-foreground">
            Select a file to view its content.
          </div>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Language detection
// ----------------------------------------------------------------------

const EXTENSION_LANGUAGE_MAP: Record<string, string> = {
  // JavaScript / TypeScript
  js: "javascript",
  jsx: "jsx",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "tsx",

  // Python
  py: "python",
  pyi: "python",

  // Data / configuration
  json: "json",
  jsonc: "json",
  json5: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  ini: "ini",
  conf: "ini",
  env: "bash",

  // Markup / styles
  md: "markdown",
  mdx: "markdown",
  html: "markup",
  htm: "markup",
  xml: "markup",
  svg: "markup",
  css: "css",
  scss: "scss",
  sass: "sass",
  less: "less",

  // Shell
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  fish: "bash",
  ps1: "powershell",
  psm1: "powershell",

  // Systems
  go: "go",
  rs: "rust",
  c: "c",
  h: "c",
  cpp: "cpp",
  cxx: "cpp",
  cc: "cpp",
  hpp: "cpp",
  hxx: "cpp",

  // JVM / .NET
  java: "java",
  kt: "kotlin",
  kts: "kotlin",
  scala: "scala",
  cs: "csharp",
  fs: "fsharp",

  // Scripting
  rb: "ruby",
  php: "php",
  pl: "perl",
  lua: "lua",
  r: "r",

  // Mobile
  swift: "swift",
  dart: "dart",

  // Query / schema
  sql: "sql",
  graphql: "graphql",
  gql: "graphql",
  proto: "protobuf",

  // Infrastructure
  tf: "hcl",
  hcl: "hcl",

  // Diff
  diff: "diff",
  patch: "diff",

  // Documentation
  rst: "markdown",
  txt: "text",
  log: "text",
};

/**
 * Best-effort language detection from a file path.
 *
 * Detection is by extension, with a handful of well-known
 * extensionless filenames handled explicitly. Unknown extensions
 * fall back to "text", which Prism renders as plain preformatted
 * content. The function is pure and cheap, so calling it during
 * render is fine.
 */
function detectLanguage(path: string): string {
  const lower = path.toLowerCase();
  const basename = lower.split("/").pop() ?? lower;

  // Extensionless well-known filenames.
  if (basename === "dockerfile") return "docker";
  if (basename === "makefile") return "makefile";
  if (basename === "cmakelists.txt") return "cmake";
  if (basename === ".gitignore") return "bash";
  if (basename === ".env" || basename.startsWith(".env.")) return "bash";

  const dotIndex = basename.lastIndexOf(".");
  if (dotIndex < 0) return "text";
  const ext = basename.slice(dotIndex + 1);
  return EXTENSION_LANGUAGE_MAP[ext] ?? "text";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}
