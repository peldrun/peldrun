"use client";

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  Package,
  RefreshCw,
  Download,
  ExternalLink,
  ZoomIn,
  ZoomOut,
  Maximize2,
  X,
  FileText,
  Image as ImageIcon,
  Video as VideoIcon,
  RotateCcw,
  Eye
} from "lucide-react";

export interface ArtifactFile {
  name: string;
  path: string;
  size: number;
  modified?: number;
  type: "image" | "video" | "pdf";
  rawUrl: string;
  downloadUrl: string;
}

export interface ArtifactsTabProps {
  jobId?: string | null;
  activeJobId?: string | null;
  chatId?: string | null;
  selectedFile?: string | null;
}

// Strictly allow only visual & binary media deliverables in Artifacts
const MEDIA_EXTENSIONS = new Set([
  // Images
  "png", "jpg", "jpeg", "webp", "gif", "svg", "ico", "bmp",
  // Videos
  "mp4", "webm", "ogg", "mov", "avi", "mkv", "m4v",
  // Documents
  "pdf",
]);

export function ArtifactsTab({ jobId, activeJobId, chatId, selectedFile }: ArtifactsTabProps) {
  const effectiveTarget = chatId || jobId || activeJobId || null;
  const [files, setFiles] = useState<ArtifactFile[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [filter, setFilter] = useState<"all" | "images" | "videos" | "docs">("all");
  const [selectedImage, setSelectedImage] = useState<ArtifactFile | null>(null);
  const [zoomLevel, setZoomLevel] = useState<number>(1);
  const highlightedCardRef = useRef<HTMLDivElement | null>(null);

  const detectType = (name: string): "image" | "video" | "pdf" => {
    const ext = name.toLowerCase().split(".").pop() || "";
    if (["mp4", "webm", "ogg", "mov", "avi", "mkv", "m4v"].includes(ext)) return "video";
    if (ext === "pdf") return "pdf";
    return "image";
  };

  const fetchArtifacts = useCallback(async () => {
    if (!effectiveTarget) {
      setFiles([]);
      return;
    }
    setLoading(true);
    try {
      let jobFilesList: ArtifactFile[] = [];
      let res = await fetch(`/api/chats/${encodeURIComponent(effectiveTarget)}/files?t=${Date.now()}`, { cache: "no-store" });

      if (!res.ok) {
        res = await fetch(`/api/run/jobs/${encodeURIComponent(effectiveTarget)}/files?t=${Date.now()}`, { cache: "no-store" });
      }

      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data.files)) {
          // Strictly exclude code/html/css/scripts and only retain media deliverables
          jobFilesList = data.files
            .filter((f: any) => {
              const ext = (f.name || "").toLowerCase().split(".").pop() || "";
              return MEDIA_EXTENSIONS.has(ext);
            })
            .map((f: any) => {
              const rawPath = f.path || f.name;
              const rawUrl = `/api/run/jobs/${encodeURIComponent(effectiveTarget)}/raw/${encodeURIComponent(rawPath)}`;
              return {
                name: f.name,
                path: rawPath,
                size: f.size || 0,
                modified: f.modified || 0,
                type: detectType(f.name),
                rawUrl: rawUrl,
                downloadUrl: rawUrl
              };
            });
        }
      }

      setFiles(jobFilesList);
    } catch (err) {
      console.error("Failed to load chat artifacts", err);
    } finally {
      setLoading(false);
    }
  }, [effectiveTarget]);

  useEffect(() => {
    fetchArtifacts();
    const interval = setInterval(fetchArtifacts, 3000);
    const handleArtifact = () => fetchArtifacts();

    window.addEventListener("peldrun:artifact-created", handleArtifact);
    window.addEventListener("peldrun:file-saved", handleArtifact);

    return () => {
      clearInterval(interval);
      window.removeEventListener("peldrun:artifact-created", handleArtifact);
      window.removeEventListener("peldrun:file-saved", handleArtifact);
    };
  }, [fetchArtifacts]);

  useEffect(() => {
    if (selectedFile && highlightedCardRef.current) {
      highlightedCardRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [selectedFile, files]);

  const filteredFiles = useMemo(() => {
    if (filter === "all") return files;
    if (filter === "images") return files.filter((f) => f.type === "image");
    if (filter === "videos") return files.filter((f) => f.type === "video");
    if (filter === "docs") return files.filter((f) => f.type === "pdf");
    return files;
  }, [files, filter]);

  const formatSize = (bytes: number) => {
    if (!bytes) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
  };

  return (
    <div className="flex flex-col h-full w-full bg-darkest font-sans overflow-hidden">
      {/* Header Bar */}
      <div className="h-10 border-b border-darkest bg-dark backdrop-blur-sm px-4 flex items-center justify-between shrink-0 text-xs">
        <div className="flex items-center gap-2 min-w-0">
          <Package className="w-4 h-4 text-peldrun-accent shrink-0" />
          <span className="font-semibold text-[#ccc]">Media Artifacts</span>
          <span className="px-1.5 py-0.2 rounded text-[10px] bg-muted text-muted-foreground font-mono">
            {files.length}
          </span>
        </div>

        <div className="flex items-center gap-2">
          {/* Filter Pills */}
          <div className="flex items-center gap-1 bg-darkest p-0.5 rounded-md border border-darkest text-[11px]">
            <button
              onClick={() => setFilter("all")}
              className={`px-2 py-0.5 rounded transition ${filter === "all" ? "bg-dark text-[#fff] shadow-xs font-medium" : "text-muted-foreground hover:text-[#fff]"}`}
            >
              All
            </button>
            <button
              onClick={() => setFilter("images")}
              className={`px-2 py-0.5 rounded transition ${filter === "images" ? "bg-dark text-[#fff] shadow-xs font-medium" : "text-muted-foreground hover:text-[#fff]"}`}
            >
              Images
            </button>
            <button
              onClick={() => setFilter("videos")}
              className={`px-2 py-0.5 rounded transition ${filter === "videos" ? "bg-dark text-[#fff] shadow-xs font-medium" : "text-muted-foreground hover:text-[#fff]"}`}
            >
              Videos
            </button>
            <button
              onClick={() => setFilter("docs")}
              className={`px-2 py-0.5 rounded transition ${filter === "docs" ? "bg-dark text-[#fff] shadow-xs font-medium" : "text-muted-foreground hover:text-[#fff]"}`}
            >
              PDFs
            </button>
          </div>

          <button
            onClick={() => fetchArtifacts()}
            disabled={loading}
            title="Refresh artifacts"
            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {/* Main Content Area */}
      <div className="flex-1 overflow-y-auto p-4 md:p-6">
        {filteredFiles.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center p-8 text-muted-foreground">
            <div className="w-12 h-12 rounded-2xl bg-[#00000052] flex items-center justify-center text-peldrun-accent mb-3 shadow-peldrun-sm">
              <Package size={60} />
            </div>
            <div className="font-semibold text-sm text-[#fff] mb-1">No Media Deliverables in this Session</div>
            <p className="max-w-xs text-xs text-muted-foreground leading-relaxed">
              Generated images (.png, .jpg), videos (.mp4), and PDF reports will populate here. Source code and scripts are available in the Files tab.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
            {filteredFiles.map((file) => {
              const isImg = file.type === "image";
              const isVid = file.type === "video";
              const isHighlighted = selectedFile && file.name.toLowerCase() === selectedFile.toLowerCase();

              return (
                <div
                  key={file.name}
                  ref={isHighlighted ? highlightedCardRef : null}
                  className={`group relative flex flex-col rounded-xl border bg-card shadow-peldrun-sm transition-all overflow-hidden ${
                    isHighlighted
                      ? "border-primary ring-2 ring-primary/40 shadow-peldrun-md"
                      : "border-border hover:border-peldrun-accent/40 hover:shadow-peldrun-md"
                  }`}
                >
                  <div className="relative w-full h-36 bg-muted/20 flex items-center justify-center overflow-hidden border-b border-border/50">
                    {isImg ? (
                      <img
                        src={file.rawUrl}
                        alt={file.name}
                        className="w-full h-full object-contain p-2 cursor-pointer transition-transform duration-300 group-hover:scale-105"
                        onClick={() => {
                          setSelectedImage(file);
                          setZoomLevel(1);
                        }}
                      />
                    ) : isVid ? (
                      <video
                        src={file.rawUrl}
                        controls
                        preload="metadata"
                        className="w-full h-full object-contain bg-black/40"
                      />
                    ) : (
                      <div className="flex flex-col items-center gap-2 text-rose-500">
                        <FileText size={40} />
                        <span className="text-[10px] font-mono uppercase bg-rose-500/10 px-1.5 py-0.5 rounded">PDF Document</span>
                      </div>
                    )}

                    {isImg && (
                      <div
                        onClick={() => {
                          setSelectedImage(file);
                          setZoomLevel(1);
                        }}
                        className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-2 cursor-pointer"
                      >
                        <span className="text-[11px] text-white bg-black/70 px-2 py-1 rounded flex items-center gap-1 backdrop-blur-xs">
                          <Eye size={12} /> Inspect
                        </span>
                      </div>
                    )}
                  </div>

                  <div className="p-3 flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <div className="font-mono text-xs font-semibold text-foreground truncate" title={file.name}>
                        {file.name}
                      </div>
                      <div className="text-[10px] text-muted-foreground mt-0.5">
                        {formatSize(file.size)}
                      </div>
                    </div>

                    <div className="flex items-center gap-1 shrink-0">
                      {isImg && (
                        <button
                          onClick={() => {
                            setSelectedImage(file);
                            setZoomLevel(1);
                          }}
                          title="Zoom In"
                          className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
                        >
                          <Maximize2 size={13} />
                        </button>
                      )}
                      <a
                        href={file.rawUrl}
                        target="_blank"
                        rel="noreferrer"
                        title="Open in new window"
                        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
                      >
                        <ExternalLink size={13} />
                      </a>
                      <a
                        href={file.rawUrl}
                        download={file.name}
                        title="Download file"
                        className="p-1.5 rounded-md bg-peldrun-accent/10 hover:bg-peldrun-accent/20 text-peldrun-accent transition cursor-pointer"
                      >
                        <Download size={13} />
                      </a>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Lightbox Zoom Modal (Images Only) */}
      {selectedImage && (
        <div className="fixed inset-0 z-50 bg-black/85 backdrop-blur-md flex flex-col animate-in fade-in duration-200">
          <div className="h-12 border-b border-white/10 px-4 flex items-center justify-between text-white shrink-0">
            <div className="flex items-center gap-2 min-w-0">
              <ImageIcon className="w-4 h-4 text-peldrun-accent shrink-0" />
              <span className="font-mono text-xs truncate max-w-sm">{selectedImage.name}</span>
              <span className="text-[10px] text-white/50 font-mono">({formatSize(selectedImage.size)})</span>
            </div>

            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1 bg-white/10 p-1 rounded-md">
                <button
                  onClick={() => setZoomLevel((z) => Math.max(0.5, z - 0.25))}
                  className="p-1 rounded hover:bg-white/20 text-white/80 hover:text-white transition cursor-pointer"
                  title="Zoom Out"
                >
                  <ZoomOut size={14} />
                </button>
                <span className="text-[11px] font-mono px-2 text-white/90">{Math.round(zoomLevel * 100)}%</span>
                <button
                  onClick={() => setZoomLevel((z) => Math.min(3, z + 0.25))}
                  className="p-1 rounded hover:bg-white/20 text-white/80 hover:text-white transition cursor-pointer"
                  title="Zoom In"
                >
                  <ZoomIn size={14} />
                </button>
                <button
                  onClick={() => setZoomLevel(1)}
                  className="p-1 rounded hover:bg-white/20 text-white/80 hover:text-white transition cursor-pointer"
                  title="Reset Scale"
                >
                  <RotateCcw size={14} />
                </button>
              </div>

              <a
                href={selectedImage.rawUrl}
                download={selectedImage.name}
                className="p-1.5 rounded-md bg-peldrun-accent text-white hover:opacity-90 transition flex items-center gap-1 text-xs cursor-pointer"
              >
                <Download size={13} />
                <span>Download</span>
              </a>

              <button
                onClick={() => setSelectedImage(null)}
                className="p-1.5 rounded-md hover:bg-white/20 text-white/80 hover:text-white transition cursor-pointer"
                title="Close"
              >
                <X size={16} />
              </button>
            </div>
          </div>

          <div
            className="flex-1 overflow-auto flex items-center justify-center p-6 cursor-zoom-out"
            onClick={(e) => {
              if (e.target === e.currentTarget) setSelectedImage(null);
            }}
          >
            <div
              style={{ transform: `scale(${zoomLevel})`, transition: "transform 0.15s ease-out" }}
              className="max-w-4xl max-h-[85vh] select-none shadow-2xl rounded-lg overflow-hidden bg-white/5 border border-white/10"
              onClick={(e) => e.stopPropagation()}
            >
              <img
                src={selectedImage.rawUrl}
                alt={selectedImage.name}
                className="w-full h-full object-contain"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default ArtifactsTab;
