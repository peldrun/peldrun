"use client";

import React, { useState, useEffect } from "react";
import { FileCode, Globe, Download, RefreshCw, ExternalLink, Eye, Code2 } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ArtifactFile {
  name: string;
  path: string;
  size: number;
  type: string;
}

export default function ArtifactsViewer({ activeFile, onSelectFile }: { activeFile?: string; onSelectFile?: (path: string) => void }) {
  const [files, setFiles] = useState<ArtifactFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedArtifact, setSelectedArtifact] = useState<string | null>(activeFile || null);
  const [previewMode, setPreviewMode] = useState<"render" | "code">("render");
  const [fileContent, setFileContent] = useState<string>("");

  const fetchArtifacts = async () => {
    setLoading(true);
    try {
      // Relative path so the request is routed through Next.js rewrites
      // (configured at build time via PELDRUN_BACKEND_URL in
      // next.config.mjs).
      const res = await fetch("/api/files");
      if (res.ok) {
        const data = await res.json();
        const artifactItems = (data.files || []).filter((f: any) =>
          ["html", "markdown", "data", "image"].includes(f.type)
        );
        setFiles(artifactItems);
        if (!selectedArtifact && artifactItems.length > 0) {
          setSelectedArtifact(artifactItems[0].path);
        }
      }
    } catch (e) {
      console.error("Failed to load artifacts", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchArtifacts();
    const interval = setInterval(fetchArtifacts, 3000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (activeFile) {
      setSelectedArtifact(activeFile);
    }
  }, [activeFile]);

  useEffect(() => {
    if (selectedArtifact) {
      fetch(`/api/files/content/${selectedArtifact}`)
        .then((res) => res.json())
        .then((data) => setFileContent(data.content || ""))
        .catch(() => setFileContent(""));
    }
  }, [selectedArtifact]);

  const currentFile = files.find((f) => f.path === selectedArtifact);
  const isHtml = currentFile?.name.endsWith(".html") || currentFile?.name.endsWith(".htm");

  return (
    <div className="flex flex-col h-full bg-[var(--color-surface-1)] text-[var(--color-ink)] font-mono overflow-hidden">
      {/* Top Bar */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-[var(--color-line)] bg-[var(--color-surface-2)]">
        <div className="flex items-center gap-2">
          <Globe size={14} className="text-cyan-400" />
          <span className="text-xs font-bold uppercase tracking-wider">
            Generated Artifacts ({files.length})
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {isHtml && (
            <div className="flex rounded border border-[var(--color-line)] overflow-hidden mr-2">
              <button
                onClick={() => setPreviewMode("render")}
                className={`px-2 py-0.5 text-[10px] cursor-pointer ${
                  previewMode === "render" ? "bg-cyan-600 text-black font-semibold" : "bg-[var(--color-surface-1)] text-[var(--color-ink-muted)]"
                }`}
              >
                Live View
              </button>
              <button
                onClick={() => setPreviewMode("code")}
                className={`px-2 py-0.5 text-[10px] cursor-pointer ${
                  previewMode === "code" ? "bg-cyan-600 text-black font-semibold" : "bg-[var(--color-surface-1)] text-[var(--color-ink-muted)]"
                }`}
              >
                Code
              </button>
            </div>
          )}
          <Button variant="ghost" size="sm" onClick={fetchArtifacts} className="h-7 w-7 p-0 cursor-pointer">
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          </Button>
        </div>
      </div>

      {files.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-xs text-[var(--color-ink-faint)]">
          <Globe size={32} className="text-[var(--color-line)] mb-2 opacity-50" />
          <span>No interactive artifacts generated yet.</span>
        </div>
      ) : (
        <div className="flex flex-col flex-1 overflow-hidden">
          {/* File selector pill bar */}
          <div className="flex items-center gap-1.5 px-3 py-2 border-b border-[var(--color-line-subtle)] bg-[var(--color-surface-1)] overflow-x-auto">
            {files.map((file) => (
              <button
                key={file.path}
                onClick={() => setSelectedArtifact(file.path)}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded text-xs transition cursor-pointer flex-shrink-0 ${
                  selectedArtifact === file.path
                    ? "bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 font-semibold"
                    : "bg-[var(--color-surface-2)] text-[var(--color-ink-muted)] hover:text-[var(--color-ink)] border border-[var(--color-line)]"
                }`}
              >
                <FileCode size={11} />
                <span>{file.name}</span>
              </button>
            ))}
          </div>

          {/* Active Preview Viewport */}
          <div className="flex-1 bg-white overflow-hidden relative">
            {isHtml && previewMode === "render" ? (
              <iframe
                // Relative src is resolved by the browser against the
                // document origin (the Next.js server) and routed to the
                // backend via the /api/:path* rewrite. Using an absolute
                // localhost URL here would bypass the rewrite and break
                // for anyone loading the frontend from a non-localhost
                // origin (staging, LAN access, reverse proxy).
                src={`/api/files/raw/${selectedArtifact}`}
                title="Artifact Preview"
                className="w-full h-full border-0 bg-white"
                sandbox="allow-scripts allow-same-origin"
              />
            ) : (
              <pre className="w-full h-full p-4 text-xs font-mono text-[var(--color-ink)] bg-[var(--color-void)] overflow-auto select-all">
                {fileContent}
              </pre>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
