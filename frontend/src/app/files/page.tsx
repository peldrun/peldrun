"use client";

import React, { useEffect, useState, useMemo } from "react";
import {
  FolderTree,
  Folder,
  FolderOpen,
  File,
  RefreshCw,
  Trash2,
  Download,
  ExternalLink,
  Eye,
  Code2,
  HardDrive,
  Sparkles,
  Layers,
  Globe,
  FileCode,
  FileText,
  Image as ImageIcon,
  Copy,
  Check,
  X,
  AlertTriangle,
  Maximize2
} from "lucide-react";
import { Button } from "@/components/ui/button";

interface ArtifactItem {
  name: string;
  path: string;
  rel_root?: string;
  origin?: "workspace" | "chats" | "projects";
  is_dir: boolean;
  size: number;
  modified: number;
  type: string;
}

interface StorageStats {
  disk: {
    total_gb: number;
    used_gb: number;
    free_gb: number;
    used_pct: number;
  };
  folders: {
    chats: { size: number; count: number; path: string };
    projects: { size: number; count: number; path: string };
    workspace: { size: number; path: string };
  };
  storage_path: string;
  root_path: string;
}

interface ExplorerItem {
  name: string;
  rel_path: string;
  is_dir: boolean;
  size: number;
  modified: number;
  type: string;
}

export default function StorageArtifactsHubPage() {
  const [activeMainTab, setActiveMainTab] = useState<"artifacts" | "storage">("artifacts");
  const [artifactCategory, setArtifactCategory] = useState<"all" | "images" | "files" | "links">("all");

  // Artifacts state
  const [artifacts, setArtifacts] = useState<ArtifactItem[]>([]);
  const [loadingArtifacts, setLoadingArtifacts] = useState(false);

  // Storage state
  const [stats, setStats] = useState<StorageStats | null>(null);
  const [storageFolder, setStorageFolder] = useState<"chats" | "projects" | "workspace">("chats");
  const [explorerItems, setExplorerItems] = useState<ExplorerItem[]>([]);
  const [loadingExplorer, setLoadingExplorer] = useState(false);

  // Modals state
  const [lightboxImg, setLightboxImg] = useState<{ url: string; name: string } | null>(null);
  const [codeModal, setCodeModal] = useState<{ name: string; content: string; lang: string } | null>(null);
  const [copiedCode, setCopiedCode] = useState(false);
  const [confirmModal, setConfirmModal] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    actionLabel: string;
    danger?: boolean;
    onConfirm: () => void;
  } | null>(null);

  const formatSize = (bytes: number): string => {
    if (!bytes || bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
  };

  const fetchStats = async () => {
    try {
      const res = await fetch("/api/storage/stats");
      if (res.ok) {
        const data = await res.json();
        setStats(data);
      }
    } catch (err) {
      console.error("Failed to load storage stats", err);
    }
  };

  const fetchArtifacts = async () => {
    setLoadingArtifacts(true);
    try {
      const res = await fetch("/api/files");
      if (res.ok) {
        const data = await res.json();
        setArtifacts(data.files || []);
      }
    } catch (err) {
      console.error("Failed to load artifacts", err);
    } finally {
      setLoadingArtifacts(false);
    }
  };

  const fetchExplorer = async (folder = storageFolder) => {
    setLoadingExplorer(true);
    try {
      const res = await fetch(`/api/storage/explorer?folder=${folder}`);
      if (res.ok) {
        const data = await res.json();
        setExplorerItems(data.items || []);
      }
    } catch (err) {
      console.error("Failed to load explorer items", err);
    } finally {
      setLoadingExplorer(false);
    }
  };

  useEffect(() => {
    fetchStats();
    fetchArtifacts();
  }, []);

  useEffect(() => {
    if (activeMainTab === "storage") {
      fetchExplorer(storageFolder);
    }
  }, [activeMainTab, storageFolder]);

  const openInWindowsExplorer = async (path: string, isAbsolute = false) => {
    try {
      const res = await fetch("/api/storage/open-path", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, is_absolute: isAbsolute }),
      });
      if (!res.ok) {
        const err = await res.json();
        alert(err.detail || "Failed to open folder");
      }
    } catch (err) {
      console.error("Failed to trigger explorer", err);
    }
  };

  const handleClearChats = () => {
    setConfirmModal({
      isOpen: true,
      title: "Clear All Chat History?",
      message: "This will permanently delete all stored chat sessions from storage/chats. This action cannot be undone.",
      actionLabel: "Yes, Clear All Chats",
      danger: true,
      onConfirm: async () => {
        try {
          await fetch("/api/storage/clear-chats", { method: "POST" });
          fetchStats();
          fetchArtifacts();
          if (storageFolder === "chats") fetchExplorer("chats");
        } catch (err) {
          console.error("Failed to clear chats", err);
        } finally {
          setConfirmModal(null);
        }
      },
    });
  };

  const handleDeleteItem = (path: string, name: string) => {
    setConfirmModal({
      isOpen: true,
      title: `Delete ${name}?`,
      message: `Are you sure you want to delete "${name}"? This file will be permanently removed.`,
      actionLabel: "Delete File",
      danger: true,
      onConfirm: async () => {
        try {
          await fetch(`/api/files?path=${encodeURIComponent(path)}`, { method: "DELETE" });
          fetchArtifacts();
          fetchStats();
          fetchExplorer(storageFolder);
        } catch (err) {
          console.error("Failed to delete item", err);
        } finally {
          setConfirmModal(null);
        }
      },
    });
  };

  const handleOpenCodeModal = async (filePath: string, fileName: string, lang: string) => {
    try {
      const res = await fetch(`/api/files/content?path=${encodeURIComponent(filePath)}`);
      if (res.ok) {
        const data = await res.json();
        setCodeModal({ name: fileName, content: data.content || "", lang });
      }
    } catch (e) {
      console.error("Failed to load code content", e);
    }
  };

  //: Hide dot-prefixed paths (`.git/…`, `.peldrun/…`, `.env`,
  //: `.gitignore`, …) from the artifacts showcase. They are workspace
  //: internals, not user-facing artifacts, and they bury the real
  //: deliverables under dozens of Git plumbing files. The workspace
  //: panel (Files tab) still shows them for debugging.
  const isHiddenPath = (rawPath: string): boolean => {
    if (!rawPath) return false;
    return rawPath
      .split(/[/\\]/)
      .some((segment) => segment.startsWith("."));
  };

  const visibleArtifacts = useMemo(
    () => artifacts.filter((a) => !isHiddenPath(a.path || a.name || "")),
    [artifacts]
  );

  const filteredArtifacts = useMemo(() => {
    if (artifactCategory === "images") {
      return visibleArtifacts.filter((a) => a.type === "image");
    }
    if (artifactCategory === "files") {
      return visibleArtifacts.filter((a) =>
        ["code", "markdown", "data", "file"].includes(a.type)
      );
    }
    if (artifactCategory === "links") {
      return visibleArtifacts.filter((a) => a.type === "html");
    }
    return visibleArtifacts;
  }, [visibleArtifacts, artifactCategory]);

  const counts = useMemo(() => {
    return {
      all: visibleArtifacts.length,
      images: visibleArtifacts.filter((a) => a.type === "image").length,
      files: visibleArtifacts.filter((a) =>
        ["code", "markdown", "data", "file"].includes(a.type)
      ).length,
      links: visibleArtifacts.filter((a) => a.type === "html").length,
    };
  }, [visibleArtifacts]);

  const getLanguageMeta = (filename: string) => {
    const ext = filename.split(".").pop()?.toLowerCase();
    switch (ext) {
      case "py":
        return { label: "Python", bg: "bg-emerald-500/10 text-emerald-500 border-emerald-500/20" };
      case "ts":
      case "tsx":
        return { label: "TypeScript", bg: "bg-blue-500/10 text-blue-500 border-blue-500/20" };
      case "js":
      case "jsx":
        return { label: "JavaScript", bg: "bg-amber-500/10 text-amber-500 border-amber-500/20" };
      case "html":
      case "htm":
        return { label: "HTML5", bg: "bg-orange-500/10 text-orange-500 border-orange-500/20" };
      case "css":
        return { label: "CSS", bg: "bg-sky-500/10 text-sky-500 border-sky-500/20" };
      case "json":
        return { label: "JSON", bg: "bg-yellow-500/10 text-yellow-600 border-yellow-500/20" };
      case "md":
        return { label: "Markdown", bg: "bg-purple-500/10 text-purple-500 border-purple-500/20" };
      case "java":
        return { label: "Java", bg: "bg-red-500/10 text-red-500 border-red-500/20" };
      default:
        return { label: ext?.toUpperCase() || "File", bg: "bg-muted text-muted-foreground border-border" };
    }
  };

  return (
    <div className="flex flex-col flex-1 h-full w-full bg-background text-foreground font-sans overflow-hidden">
      {/* 1. TOP SYSTEM STORAGE BAR */}
      <div className="p-4 border-b border-border bg-card/60 backdrop-blur-md shrink-0">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-xl bg-primary/10 text-primary border border-primary/20 shadow-sm">
              <HardDrive size={20} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="font-heading font-semibold text-sm text-foreground">
                  Storage & Artifacts Hub
                </h1>
                <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-muted text-muted-foreground border border-border">
                  Local Windows Environment
                </span>
              </div>
              <p className="text-xs text-muted-foreground font-mono truncate max-w-xl">
                {stats?.storage_path || "D:\\AI\\PELDRUN-Web\\storage"}
              </p>
            </div>
          </div>

          {/* Quick Action Buttons */}
          <div className="flex items-center gap-2 flex-wrap">
            <Button
              variant="outline"
              size="sm"
              onClick={() => openInWindowsExplorer(stats?.storage_path || "D:\\AI\\PELDRUN-Web\\storage", true)}
              className="h-8 text-xs gap-1.5 border-border hover:bg-muted text-foreground cursor-pointer shadow-xs"
              title="Open Storage folder in foreground Windows Explorer"
            >
              <FolderOpen size={14} className="text-amber-500" />
              <span>Open Storage in Explorer</span>
            </Button>

            <Button
              variant="outline"
              size="sm"
              onClick={handleClearChats}
              className="h-8 text-xs gap-1.5 border-border hover:bg-red-500/10 hover:text-red-500 text-muted-foreground cursor-pointer transition-colors"
              title="Clear all stored chat history"
            >
              <Trash2 size={13} />
              <span>Clear Chats</span>
            </Button>

            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                fetchStats();
                fetchArtifacts();
                if (activeMainTab === "storage") fetchExplorer();
              }}
              className="h-8 text-xs gap-1.5 border-border hover:bg-muted text-foreground cursor-pointer"
            >
              <RefreshCw size={12} className={loadingArtifacts || loadingExplorer ? "animate-spin" : ""} />
              <span>Refresh</span>
            </Button>
          </div>
        </div>

        {/* Disk Space & Folders Gauge */}
        {stats && (
          <div className="mt-3.5 pt-3 border-t border-border/60 flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-4 text-xs font-mono">
            <div className="flex-1 min-w-[280px]">
              <div className="flex justify-between items-center mb-1 text-[11px] text-muted-foreground">
                <span className="flex items-center gap-1.5 font-medium text-foreground">
                  <span>Disk Capacity:</span>
                  <span className="text-primary font-bold">{stats.disk.used_gb} GB used</span>
                  <span>/ {stats.disk.total_gb} GB</span>
                </span>
                <span className="text-muted-foreground">{stats.disk.free_gb} GB Free ({100 - stats.disk.used_pct}%)</span>
              </div>
              <div className="w-full h-2 rounded-full bg-muted overflow-hidden flex">
                <div
                  className="h-full bg-gradient-to-r from-primary to-cyan-500 transition-all duration-500 rounded-full"
                  style={{ width: `${Math.min(stats.disk.used_pct, 100)}%` }}
                />
              </div>
            </div>

            <div className="flex items-center gap-3 shrink-0 flex-wrap">
              <div
                onClick={() => {
                  setActiveMainTab("storage");
                  setStorageFolder("chats");
                }}
                className={`px-2.5 py-1 rounded-md border flex items-center gap-2 cursor-pointer transition ${
                  activeMainTab === "storage" && storageFolder === "chats"
                    ? "bg-cyan-500/10 border-cyan-500/40 text-cyan-400 font-semibold"
                    : "bg-muted/60 hover:bg-muted border-border text-foreground"
                }`}
              >
                <span className="w-2 h-2 rounded-full bg-cyan-400" />
                <span className="text-muted-foreground">Chats:</span>
                <span className="font-semibold">{formatSize(stats.folders.chats.size)}</span>
                <span className="text-[10px] text-muted-foreground">({stats.folders.chats.count})</span>
              </div>

              <div
                onClick={() => {
                  setActiveMainTab("storage");
                  setStorageFolder("projects");
                }}
                className={`px-2.5 py-1 rounded-md border flex items-center gap-2 cursor-pointer transition ${
                  activeMainTab === "storage" && storageFolder === "projects"
                    ? "bg-purple-500/10 border-purple-500/40 text-purple-400 font-semibold"
                    : "bg-muted/60 hover:bg-muted border-border text-foreground"
                }`}
              >
                <span className="w-2 h-2 rounded-full bg-purple-400" />
                <span className="text-muted-foreground">Projects:</span>
                <span className="font-semibold">{formatSize(stats.folders.projects.size)}</span>
                <span className="text-[10px] text-muted-foreground">({stats.folders.projects.count})</span>
              </div>

              <div
                onClick={() => {
                  setActiveMainTab("storage");
                  setStorageFolder("workspace");
                }}
                className={`px-2.5 py-1 rounded-md border flex items-center gap-2 cursor-pointer transition ${
                  activeMainTab === "storage" && storageFolder === "workspace"
                    ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-400 font-semibold"
                    : "bg-muted/60 hover:bg-muted border-border text-foreground"
                }`}
              >
                <span className="w-2 h-2 rounded-full bg-emerald-400" />
                <span className="text-muted-foreground">Workspace:</span>
                <span className="font-semibold">{formatSize(stats.folders.workspace.size)}</span>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 2. MAIN TABS SWITCHER */}
      <div className="px-6 border-b border-border bg-card/20 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-6">
          <button
            onClick={() => setActiveMainTab("artifacts")}
            className={`py-3 text-xs font-medium flex items-center gap-2 border-b-2 transition cursor-pointer ${
              activeMainTab === "artifacts"
                ? "border-primary text-primary font-semibold"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Sparkles size={14} />
            <span>Artifacts Showcase</span>
            <span className="text-[10px] px-1.5 py-0.2 rounded-full bg-muted font-mono">
              {counts.all}
            </span>
          </button>

          <button
            onClick={() => {
              setActiveMainTab("storage");
              fetchExplorer(storageFolder);
            }}
            className={`py-3 text-xs font-medium flex items-center gap-2 border-b-2 transition cursor-pointer ${
              activeMainTab === "storage"
                ? "border-primary text-primary font-semibold"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Layers size={14} />
            <span>Storage Explorer</span>
            <span className="text-[10px] px-1.5 py-0.2 rounded-full bg-muted font-mono">
              {stats ? stats.folders.chats.count + stats.folders.projects.count : "chats / projects"}
            </span>
          </button>
        </div>
      </div>

      {/* 3. CONTENT AREA: TAB 1 (ARTIFACTS SHOWCASE) */}
      {activeMainTab === "artifacts" && (
        <div className="flex-1 flex flex-col overflow-hidden bg-background">
          <div className="px-6 py-2.5 border-b border-border/60 bg-muted/20 flex items-center justify-between shrink-0">
            <div className="flex items-center gap-5 text-xs">
              <button
                onClick={() => setArtifactCategory("all")}
                className={`flex items-center gap-1.5 py-1 font-medium transition cursor-pointer ${
                  artifactCategory === "all"
                    ? "text-foreground font-semibold border-b border-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <span>All</span>
                <span className="text-[10px] font-mono text-muted-foreground">{counts.all}</span>
              </button>

              <button
                onClick={() => setArtifactCategory("images")}
                className={`flex items-center gap-1.5 py-1 font-medium transition cursor-pointer ${
                  artifactCategory === "images"
                    ? "text-foreground font-semibold border-b border-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <span>Images</span>
                <span className="text-[10px] font-mono text-muted-foreground">{counts.images}</span>
              </button>

              <button
                onClick={() => setArtifactCategory("files")}
                className={`flex items-center gap-1.5 py-1 font-medium transition cursor-pointer ${
                  artifactCategory === "files"
                    ? "text-foreground font-semibold border-b border-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <span>Files</span>
                <span className="text-[10px] font-mono text-muted-foreground">{counts.files}</span>
              </button>

              <button
                onClick={() => setArtifactCategory("links")}
                className={`flex items-center gap-1.5 py-1 font-medium transition cursor-pointer ${
                  artifactCategory === "links"
                    ? "text-foreground font-semibold border-b border-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <span>Links</span>
                <span className="text-[10px] font-mono text-muted-foreground">{counts.links}</span>
              </button>
            </div>

            <Button
              variant="ghost"
              size="sm"
              onClick={fetchArtifacts}
              className="h-7 w-7 p-0 cursor-pointer text-muted-foreground hover:text-foreground"
              title="Refresh artifacts"
            >
              <RefreshCw size={13} className={loadingArtifacts ? "animate-spin" : ""} />
            </Button>
          </div>

          <div className="flex-1 overflow-y-auto p-6  mx-auto px-10">
            {loadingArtifacts ? (
              <div className="flex flex-col items-center justify-center h-64 text-muted-foreground text-xs font-mono animate-pulse gap-2">
                <RefreshCw size={18} className="animate-spin text-primary" />
                <span>Scanning all storage and outputs...</span>
              </div>
            ) : filteredArtifacts.length === 0 ? (
              <div className="h-full min-h-[360px] flex flex-col items-center justify-center text-center select-none">
                <h3 className="text-base font-semibold text-foreground mb-1.5">No artifacts found</h3>
                <p className="text-xs text-muted-foreground max-w-sm">
                  Generated images and file outputs will appear here as sessions produce them.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setActiveMainTab("storage");
                    setStorageFolder("chats");
                  }}
                  className="mt-4 text-xs font-mono gap-1.5"
                >
                  <Layers size={13} />
                  <span>Inspect Chat & Project Storage</span>
                </Button>
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {filteredArtifacts.map((item) => {
                  const isImage = item.type === "image";
                  const isHtml = item.type === "html";
                  const isCode = item.type === "code" || item.type === "markdown" || item.type === "data";
                  const rawUrl = `/api/files/raw?path=${encodeURIComponent(item.path)}`;
                  const meta = getLanguageMeta(item.name);

                  return (
                    <div
                      key={item.path}
                      className="group flex flex-col rounded-xl border border-border bg-card hover:border-primary/50 transition-all shadow-sm overflow-hidden"
                    >
                      {/* Image Preview Card */}
                      {isImage && (
                        <div
                          className="relative h-44 w-full bg-muted/40 overflow-hidden cursor-pointer"
                          onClick={() => setLightboxImg({ url: rawUrl, name: item.name })}
                        >
                          <img
                            src={rawUrl}
                            alt={item.name}
                            className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105"
                            loading="lazy"
                          />
                          <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-2 text-white">
                            <span className="p-2 rounded-full bg-black/60 hover:bg-black/80">
                              <Maximize2 size={16} />
                            </span>
                          </div>
                        </div>
                      )}

                      {/* Interactive Web Page Preview */}
                      {isHtml && (
                        <div className="relative h-44 w-full bg-muted/20 border-b border-border overflow-hidden">
                          <iframe
                            src={rawUrl}
                            title={item.name}
                            className="w-full h-full border-0 pointer-events-none scale-90 origin-top-left"
                            sandbox="allow-scripts allow-same-origin"
                          />
                          <div className="absolute top-2 right-2 flex items-center gap-1">
                            <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-orange-500/90 text-white font-semibold shadow">
                              Live Web
                            </span>
                          </div>
                        </div>
                      )}

                      {/* Code / Text File Preview Card */}
                      {isCode && (
                        <div
                          onClick={() => handleOpenCodeModal(item.path, item.name, meta.label)}
                          className="h-32 p-3 bg-muted/20 border-b border-border cursor-pointer flex flex-col justify-between group-hover:bg-muted/30 transition"
                        >
                          <div className="flex items-center justify-between">
                            <span className={`text-[10px] font-mono px-2 py-0.5 rounded border ${meta.bg}`}>
                              {meta.label}
                            </span>
                            <Eye size={13} className="text-muted-foreground group-hover:text-primary transition" />
                          </div>
                          <div className="font-mono text-[11px] text-muted-foreground line-clamp-3 bg-background/50 p-2 rounded border border-border/50">
                            Click to inspect source code and syntax...
                          </div>
                        </div>
                      )}

                      {/* Details & Actions */}
                      <div className="p-3 flex items-center justify-between gap-2">
                        <div className="min-w-0 flex items-center gap-2">
                          {isImage ? (
                            <ImageIcon size={15} className="text-cyan-400 shrink-0" />
                          ) : isHtml ? (
                            <Globe size={15} className="text-orange-400 shrink-0" />
                          ) : (
                            <FileCode size={15} className="text-primary shrink-0" />
                          )}
                          <div className="truncate">
                            <div className="text-xs font-mono font-medium text-foreground truncate" title={item.name}>
                              {item.name}
                            </div>
                            <div className="flex items-center gap-2 text-[10px] text-muted-foreground font-mono">
                              <span>{formatSize(item.size)}</span>
                              {item.origin && (
                                <span className="px-1.5 py-0.2 rounded bg-muted border border-border text-[9px]">
                                  {item.origin}
                                </span>
                              )}
                            </div>
                          </div>
                        </div>

                        <div className="flex items-center gap-1 shrink-0">
                          <button
                            type="button"
                            onClick={() => openInWindowsExplorer(item.path)}
                            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-amber-500 transition cursor-pointer"
                            title="Reveal in Windows Explorer"
                          >
                            <FolderOpen size={13} />
                          </button>

                          {isHtml && (
                            <a
                              href={rawUrl}
                              target="_blank"
                              rel="noreferrer"
                              className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition"
                              title="Open live view in new tab"
                            >
                              <ExternalLink size={13} />
                            </a>
                          )}

                          <a
                            href={`/api/files/download?path=${encodeURIComponent(item.path)}`}
                            download={item.name}
                            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition"
                            title="Download file"
                          >
                            <Download size={13} />
                          </a>

                          <button
                            type="button"
                            onClick={() => handleDeleteItem(item.path, item.name)}
                            className="p-1.5 rounded-md hover:bg-red-500/10 text-muted-foreground hover:text-red-500 transition cursor-pointer"
                            title="Delete artifact"
                          >
                            <Trash2 size={13} />
                          </button>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      )}

      {/* 4. CONTENT AREA: TAB 2 (STORAGE EXPLORER) */}
      {activeMainTab === "storage" && (
        <div className="flex-1 flex flex-col overflow-hidden bg-background">
          <div className="px-6 py-2.5 border-b border-border/60 bg-muted/20 flex items-center justify-between shrink-0">
            <div className="flex items-center gap-3">
              <span className="text-xs text-muted-foreground font-mono">Directory:</span>
              <div className="flex items-center rounded-lg border border-border bg-card p-0.5">
                {(["chats", "projects", "workspace"] as const).map((folder) => (
                  <button
                    key={folder}
                    onClick={() => {
                      setStorageFolder(folder);
                      fetchExplorer(folder);
                    }}
                    className={`px-3 py-1 text-xs rounded-md font-mono transition cursor-pointer ${
                      storageFolder === folder
                        ? "bg-primary text-primary-foreground font-semibold shadow-xs"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    storage/{folder}
                  </button>
                ))}
              </div>
            </div>

            <Button
              variant="outline"
              size="sm"
              onClick={() => openInWindowsExplorer(storageFolder === "workspace" ? "workspace" : `storage/${storageFolder}`)}
              className="h-7 text-xs gap-1 border-border hover:bg-muted text-foreground cursor-pointer"
            >
              <FolderOpen size={12} className="text-amber-500" />
              <span>Open in Windows (Foreground)</span>
            </Button>
          </div>

          <div className="flex-1 overflow-y-auto p-6">
            {loadingExplorer ? (
              <div className="flex items-center justify-center h-48 text-muted-foreground text-xs font-mono animate-pulse">
                Reading storage/{storageFolder}...
              </div>
            ) : explorerItems.length === 0 ? (
              <div className="h-48 flex flex-col items-center justify-center text-center text-muted-foreground">
                <Folder size={28} className="text-muted-foreground/40 mb-2" />
                <span className="text-xs font-mono">No items found in storage/{storageFolder}</span>
              </div>
            ) : (
              <div className="border border-border rounded-lg bg-card overflow-hidden">
                <table className="w-full text-left text-xs font-mono">
                  <thead className="bg-muted/50 border-b border-border text-muted-foreground">
                    <tr>
                      <th className="p-3 font-medium">Name</th>
                      <th className="p-3 font-medium">Type</th>
                      <th className="p-3 font-medium">Size</th>
                      <th className="p-3 font-medium text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {explorerItems.map((item) => (
                      <tr key={item.rel_path} className="hover:bg-muted/20 transition">
                        <td className="p-3 flex items-center gap-2 text-foreground font-medium">
                          {item.is_dir ? (
                            <Folder size={15} className="text-amber-500 shrink-0" />
                          ) : (
                            <File size={15} className="text-muted-foreground shrink-0" />
                          )}
                          <span className="truncate max-w-md">{item.name}</span>
                        </td>
                        <td className="p-3 text-muted-foreground">
                          {item.is_dir ? "Folder" : item.type}
                        </td>
                        <td className="p-3 text-muted-foreground">
                          {formatSize(item.size)}
                        </td>
                        <td className="p-3 text-right">
                          <div className="flex items-center justify-end gap-1.5">
                            <button
                              onClick={() => openInWindowsExplorer(item.rel_path)}
                              className="p-1.5 rounded hover:bg-muted text-muted-foreground hover:text-amber-500 cursor-pointer"
                              title="Reveal in Windows Explorer"
                            >
                              <FolderOpen size={13} />
                            </button>
                            <button
                              onClick={() => handleDeleteItem(item.rel_path, item.name)}
                              className="p-1.5 rounded hover:bg-red-500/10 text-muted-foreground hover:text-red-500 cursor-pointer"
                              title="Delete permanently"
                            >
                              <Trash2 size={13} />
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 5. LIGHTBOX MODAL */}
      {lightboxImg && (
        <div
          className="fixed inset-0 z-50 bg-black/85 backdrop-blur-md flex flex-col items-center justify-center p-4 animate-in fade-in duration-200"
          onClick={() => setLightboxImg(null)}
        >
          <div
            className="relative max-w-5xl max-h-[90vh] w-full flex flex-col items-center"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="w-full flex items-center justify-between pb-3 text-white">
              <span className="font-mono text-xs truncate">{lightboxImg.name}</span>
              <div className="flex items-center gap-2">
                <a
                  href={lightboxImg.url}
                  download={lightboxImg.name}
                  className="p-1.5 rounded-md bg-white/10 hover:bg-white/20 text-white transition"
                  title="Download Image"
                >
                  <Download size={15} />
                </a>
                <button
                  onClick={() => setLightboxImg(null)}
                  className="p-1.5 rounded-md bg-white/10 hover:bg-white/20 text-white cursor-pointer"
                >
                  <X size={16} />
                </button>
              </div>
            </div>

            <img
              src={lightboxImg.url}
              alt={lightboxImg.name}
              className="max-h-[80vh] max-w-full object-contain rounded-lg border border-white/10 shadow-2xl"
            />
          </div>
        </div>
      )}

      {/* 6. CODE MODAL */}
      {codeModal && (
        <div
          className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 animate-in fade-in duration-200"
          onClick={() => setCodeModal(null)}
        >
          <div
            className="w-full max-w-4xl max-h-[85vh] bg-card border border-border rounded-xl shadow-2xl flex flex-col overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-5 py-3 border-b border-border bg-muted/40">
              <div className="flex items-center gap-2">
                <Code2 size={16} className="text-primary" />
                <span className="font-mono text-xs font-semibold text-foreground">{codeModal.name}</span>
                <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-primary/10 text-primary border border-primary/20">
                  {codeModal.lang}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(codeModal.content);
                    setCopiedCode(true);
                    setTimeout(() => setCopiedCode(false), 2000);
                  }}
                  className="px-2.5 py-1 text-xs rounded bg-muted hover:bg-muted/80 text-foreground border border-border flex items-center gap-1.5 cursor-pointer transition"
                >
                  {copiedCode ? <Check size={13} className="text-emerald-500" /> : <Copy size={13} />}
                  <span>{copiedCode ? "Copied!" : "Copy"}</span>
                </button>
                <button
                  onClick={() => setCodeModal(null)}
                  className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground cursor-pointer"
                >
                  <X size={16} />
                </button>
              </div>
            </div>

            <div className="flex-1 overflow-auto p-4 bg-background/95 font-mono text-xs leading-relaxed text-foreground select-all">
              <pre>{codeModal.content}</pre>
            </div>
          </div>
        </div>
      )}

      {/* 7. CONFIRMATION MODAL */}
      {confirmModal && confirmModal.isOpen && (
        <div
          className="fixed inset-0 z-50 bg-black/70 backdrop-blur-xs flex items-center justify-center p-4 animate-in fade-in duration-150"
          onClick={() => setConfirmModal(null)}
        >
          <div
            className="w-full max-w-md bg-card border border-border rounded-xl p-5 shadow-2xl space-y-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start gap-3">
              <div className={`p-2.5 rounded-full ${confirmModal.danger ? "bg-red-500/10 text-red-500" : "bg-primary/10 text-primary"}`}>
                <AlertTriangle size={20} />
              </div>
              <div>
                <h3 className="font-heading text-sm font-semibold text-foreground">
                  {confirmModal.title}
                </h3>
                <p className="text-xs text-muted-foreground mt-1 leading-normal">
                  {confirmModal.message}
                </p>
              </div>
            </div>

            <div className="flex items-center justify-end gap-2 pt-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setConfirmModal(null)}
                className="h-8 text-xs cursor-pointer"
              >
                Cancel
              </Button>
              <Button
                variant={confirmModal.danger ? "danger" : "primary"}
                size="sm"
                onClick={confirmModal.onConfirm}
                className="h-8 text-xs cursor-pointer"
              >
                {confirmModal.actionLabel}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
