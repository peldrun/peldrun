"use client";

import React, { useState, useEffect, useCallback, useMemo } from "react";
import { Monitor, RefreshCw, ExternalLink, Code2, Sparkles, FileText, Image as ImageIcon, AlignLeft } from "lucide-react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";

export interface PreviewTabProps {
  jobId?: string | null;
  activeJobId?: string | null;
  currentHtmlPath?: string | null;
  filePath?: string | null;
  overrideFile?: string | null;
}

export function PreviewTab({
  jobId,
  activeJobId,
  currentHtmlPath,
  filePath,
  overrideFile,
}: PreviewTabProps) {
  const effectiveJobId = jobId || activeJobId || null;
  const [autoFile, setAutoFile] = useState<string | null>(null);
  const explicitFile = overrideFile || currentHtmlPath || filePath || null;
  const effectiveFile = explicitFile || autoFile;

  const [rawContent, setRawContent] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(false);
  const [refreshKey, setRefreshKey] = useState<number>(0);

  const lowerFile = (effectiveFile || "").toLowerCase();
  const isMarkdown = lowerFile.endsWith(".md") || lowerFile.endsWith(".markdown");
  const isTxt = lowerFile.endsWith(".txt") || lowerFile.endsWith(".text");
  const isSvg = lowerFile.endsWith(".svg");
  const isHtml = lowerFile.endsWith(".html") || lowerFile.endsWith(".htm");
  const isPreviewable = isMarkdown || isTxt || isSvg || isHtml;

  useEffect(() => {
    const handleSaved = (e: Event) => {
      const ce = e as CustomEvent<{ path?: string }>;
      if (!effectiveFile || !ce.detail?.path || ce.detail.path.endsWith(effectiveFile) || effectiveFile.endsWith(ce.detail.path)) {
        setRefreshKey((k) => k + 1);
      }
    };

    const handleArtifact = () => {
      setRefreshKey((k) => k + 1);
    };

    window.addEventListener("peldrun:file-saved", handleSaved);
    window.addEventListener("peldrun:artifact-created", handleArtifact);
    return () => {
      window.removeEventListener("peldrun:file-saved", handleSaved);
      window.removeEventListener("peldrun:artifact-created", handleArtifact);
    };
  }, [effectiveFile]);

  const scanForPreviewableDeliverables = useCallback(() => {
    if (effectiveJobId && !explicitFile) {
      fetch(`/api/run/jobs/${effectiveJobId}/files?t=${Date.now()}`, { cache: "no-store" })
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (data?.files && Array.isArray(data.files)) {
            const bestFile =
              data.files.find((f: any) => f.name.toLowerCase() === "index.html") ||
              data.files.find((f: any) => {
                const l = f.name.toLowerCase();
                return l.endsWith(".html") || l.endsWith(".htm");
              }) ||
              data.files.find((f: any) => {
                const l = f.name.toLowerCase();
                return l.endsWith(".md") || l.endsWith(".markdown");
              }) ||
              data.files.find((f: any) => {
                const l = f.name.toLowerCase();
                return l.endsWith(".txt") || l.endsWith(".text");
              }) ||
              data.files.find((f: any) => f.name.toLowerCase().endsWith(".svg"));

            if (bestFile && bestFile.name !== autoFile) {
              setAutoFile(bestFile.name);
            }
          }
        })
        .catch(() => {});
    }
  }, [effectiveJobId, explicitFile, autoFile]);

  useEffect(() => {
    scanForPreviewableDeliverables();
    const interval = setInterval(scanForPreviewableDeliverables, 3500);
    return () => clearInterval(interval);
  }, [scanForPreviewableDeliverables]);

  const fetchContent = useCallback(async () => {
    // Strictly prevent binary image files from being fetched as text
    if (!effectiveFile || !isPreviewable) {
      setRawContent("");
      return;
    }

    setLoading(true);
    try {
      let res: Response | null = null;
      if (effectiveJobId) {
        res = await fetch(`/api/run/jobs/${effectiveJobId}/content?path=${encodeURIComponent(effectiveFile)}&t=${Date.now()}`, {
          cache: "no-store",
        });
      }

      if (!res || !res.ok) {
        res = await fetch(`/api/files/content?path=${encodeURIComponent(effectiveFile)}&t=${Date.now()}`, {
          cache: "no-store",
        });
      }

      if (res && res.ok) {
        const data = await res.json();
        setRawContent(data.content || "");
      } else {
        setRawContent("");
      }
    } catch (e) {
      console.error("Failed to load preview content", e);
      setRawContent("");
    } finally {
      setLoading(false);
    }
  }, [effectiveFile, effectiveJobId, isPreviewable, refreshKey]);

  useEffect(() => {
    fetchContent();
  }, [fetchContent]);

  const sandboxedHtml = useMemo(() => {
    if (!rawContent || !isHtml) return "";
    const baseHref = effectiveJobId ? `/api/run/jobs/${effectiveJobId}/raw/` : `/api/files/raw/`;
    const baseTag = `<base href="${baseHref}">`;

    if (rawContent.includes("<head>")) {
      return rawContent.replace("<head>", `<head>${baseTag}`);
    }
    return `${baseTag}${rawContent}`;
  }, [rawContent, isHtml, effectiveJobId]);

  if (!effectiveFile || !isPreviewable || (!rawContent && !loading)) {
    return (
      <div className="flex flex-col items-center justify-center h-full w-full bg-custom text-muted-foreground p-8 select-none font-sans">
        <div className="flex flex-col items-center max-w-sm text-center space-y-4">
          <div className="p-4 rounded-2xl bg-card border border-border shadow-peldrun-md relative">
            <Code2 className="w-10 h-10 text-peldrun-accent" />
            <Sparkles className="w-4 h-4 text-peldrun-accent absolute top-2 right-2 animate-pulse" />
          </div>
          <div>
            <h3 className="text-sm font-semibold font-heading text-foreground">Sandbox Standby</h3>
            <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
              Live web applications (.html, .svg), Markdown documents (.md), and plain text files (.txt) will render here in real time.
            </p>
          </div>
          {loading && (
            <div className="flex items-center gap-2 text-xs text-peldrun-accent font-medium">
              <RefreshCw className="w-3.5 h-3.5 animate-spin" />
              <span>Loading deliverables...</span>
            </div>
          )}
        </div>
      </div>
    );
  }

  const rawUrl = effectiveJobId
    ? `/api/run/jobs/${effectiveJobId}/raw/${effectiveFile}`
    : `/api/files/raw/${effectiveFile}`;

  const renderBadge = () => {
    if (isHtml) return <span className="px-1.5 py-0.5 rounded text-[10px] bg-peldrun-success/15 text-peldrun-success border border-peldrun-success/30 font-mono">HTML App</span>;
    if (isMarkdown) return <span className="px-1.5 py-0.5 rounded text-[10px] bg-primary/15 text-primary border border-primary/30 font-mono">Markdown</span>;
    if (isTxt) return <span className="px-1.5 py-0.5 rounded text-[10px] bg-peldrun-warning/15 text-peldrun-warning border border-peldrun-warning/30 font-mono">Plain Text</span>;
    if (isSvg) return <span className="px-1.5 py-0.5 rounded text-[10px] bg-peldrun-info/15 text-peldrun-info border border-peldrun-info/30 font-mono">Vector SVG</span>;
    return <span className="px-1.5 py-0.5 rounded text-[10px] bg-muted text-muted-foreground font-mono">Preview</span>;
  };

  return (
    <div className="flex flex-col h-full w-full bg-custom overflow-hidden font-sans">
      <div className="h-10 border-b border-border bg-card/60 backdrop-blur-sm px-4 flex items-center justify-between shrink-0 text-xs">
        <div className="flex items-center gap-2 text-foreground min-w-0">
          {isMarkdown ? (
            <FileText className="w-4 h-4 text-primary shrink-0" />
          ) : isTxt ? (
            <AlignLeft className="w-4 h-4 text-peldrun-warning shrink-0" />
          ) : isSvg ? (
            <ImageIcon className="w-4 h-4 text-peldrun-info shrink-0" />
          ) : (
            <Monitor className="w-4 h-4 text-peldrun-accent shrink-0" />
          )}
          <span className="font-mono text-xs font-medium truncate max-w-[200px]">{effectiveFile}</span>
          {renderBadge()}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          <button
            type="button"
            onClick={() => setRefreshKey((k) => k + 1)}
            disabled={loading}
            title="Refresh Live Preview"
            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>

          <a
            href={rawUrl}
            target="_blank"
            rel="noopener noreferrer"
            title="Open artifact in new window"
            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center gap-1 text-[11px] cursor-pointer"
          >
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        </div>
      </div>

      <div className="flex-1 w-full h-full relative overflow-hidden bg-background">
        {isMarkdown && (
          <div className="w-full h-full overflow-y-auto p-6 md:p-8 bg-card/40">
            <div className="max-w-3xl mx-auto rounded-sm border border-border/80 bg-card p-6 shadow-peldrun-sm">
              <MarkdownRenderer content={rawContent} />
            </div>
          </div>
        )}

        {isTxt && (
          <div className="w-full h-full overflow-y-auto p-6 md:p-8 bg-card/40">
            <div className="max-w-3xl mx-auto rounded-sm border border-border/80 bg-card p-6 shadow-peldrun-sm">
              <pre className="whitespace-pre-wrap break-words font-mono text-xs md:text-sm leading-relaxed text-foreground/90 select-text">
                {rawContent}
              </pre>
            </div>
          </div>
        )}

        {isSvg && (
          <div className="w-full h-full flex items-center justify-center p-8 bg-slate-950/40 overflow-auto">
            <div
              className="max-w-full max-h-full flex items-center justify-center p-4 rounded-sm border border-border/60 bg-card shadow-peldrun-md"
              dangerouslySetInnerHTML={{ __html: rawContent }}
            />
          </div>
        )}

        {isHtml && (
          <div className="w-full h-full bg-white relative overflow-hidden">
            <iframe
              key={`${effectiveJobId}-${effectiveFile}-${refreshKey}`}
              title="Sandbox Preview"
              srcDoc={sandboxedHtml}
              sandbox="allow-scripts allow-modals allow-forms allow-same-origin"
              className="w-full h-full border-0"
            />
          </div>
        )}
      </div>
    </div>
  );
}

export default PreviewTab;