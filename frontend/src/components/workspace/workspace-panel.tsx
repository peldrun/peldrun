/**
 * frontend/src/components/workspace/workspace-panel.tsx
 *
 * Workspace Panel component hardened against frontend freeze and path divergence.
 * Key enhancements:
 * - Completely eliminates re-render storms by removing selectedFilePath from fetch effect dependency array.
 * - Uses authoritative relative paths (f.path) instead of flat filenames (f.name).
 * - Prioritizes deliverables with size > 0 over empty placeholder files.
 * - Auto-selects the primary web deliverable once per scope without infinite fetch cascading.
 */

"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  Monitor,
  FolderTree,
  Package,
  TerminalSquare,
  FileCode2,
  Download,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { PreviewTab } from "./preview-tab";
import { FilesTab } from "./files-tab";
import { ArtifactsTab } from "./artifacts-tab";
import { LogsTab } from "./logs-tab";
import { EditorTab } from "./editor-tab";

export type TabId = "preview" | "files" | "artifacts" | "logs" | "editor";

export interface WorkspaceFileItem {
  name: string;
  path: string;
  size?: number;
}

export function getFileCategory(filepath: string): "preview" | "artifacts" | "editor" {
  if (!filepath) return "preview";
  const lower = filepath.toLowerCase();
  const ext = lower.split(".").pop() || "";

  // Web apps, SVGs, and markdown documents belong to Preview
  if (["html", "htm", "svg", "md", "markdown"].includes(ext)) {
    return "preview";
  }

  // Strictly visual & binary media deliverables belong to Artifacts
  if (
    [
      "png", "jpg", "jpeg", "webp", "gif", "ico", "bmp",
      "mp4", "webm", "ogg", "mov", "avi", "mkv", "m4v",
      "pdf",
    ].includes(ext)
  ) {
    return "artifacts";
  }

  // Code scripts, styles, data sheets, and config belong to Editor & Files
  return "editor";
}

export interface WorkspacePanelProps {
  activeJobId?: string | null;
  activeChatId?: string | null;
  overrideFile?: string | null;
  overrideDraft?: { filename: string; content: string } | null;
}

export function WorkspacePanel({
  activeJobId,
  activeChatId,
  overrideFile,
  overrideDraft,
}: WorkspacePanelProps) {
  const [activeTab, setActiveTab] = useState<TabId>("preview");
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [draftContent, setDraftContent] = useState<string | null>(null);
  const [jobFiles, setJobFiles] = useState<WorkspaceFileItem[]>([]);
  const [filesLoading, setFilesLoading] = useState<boolean>(false);
  const [exporting, setExporting] = useState<boolean>(false);

  const effectiveScopeId = activeChatId || activeJobId || null;
  const hasAutoSelectedRef = useRef<boolean>(false);
  const selectedFilePathRef = useRef<string | null>(null);
  selectedFilePathRef.current = selectedFilePath;

  const handleSelectArtifact = useCallback((filePath: string) => {
    if (!filePath) return;
    const cleanPath = filePath.replace(/\\/g, "/");
    setSelectedFilePath(cleanPath);
    setDraftContent(null);

    const category = getFileCategory(cleanPath);
    if (category === "preview") {
      setActiveTab("preview");
    } else if (category === "artifacts") {
      setActiveTab("artifacts");
    } else {
      setActiveTab("editor");
    }
  }, []);

  const handleEditorSaved = (savedPath: string) => {
    const cleanPath = savedPath.replace(/\\/g, "/");
    setSelectedFilePath(cleanPath);
    const category = getFileCategory(cleanPath);
    if (category === "preview") {
      setActiveTab("preview");
    }
  };

  // Reconcile and fetch files without triggering re-render cascades
  const fetchFiles = useCallback(async () => {
    if (!effectiveScopeId) {
      setJobFiles([]);
      return;
    }

    try {
      setFilesLoading(true);
      let res = await fetch(`/api/chats/${effectiveScopeId}/files?t=${Date.now()}`);
      if (!res.ok) {
        res = await fetch(`/api/run/jobs/${effectiveScopeId}/files?t=${Date.now()}`);
      }

      if (res.ok) {
        const data = await res.json();
        const list: WorkspaceFileItem[] = data.files || [];
        setJobFiles(list);

        // Auto-select once upon initial load if nothing is selected
        if (!hasAutoSelectedRef.current && !selectedFilePathRef.current && list.length > 0) {
          // 1. Prefer non-empty HTML files
          const webAppFile =
            list.find((f) => f.name.toLowerCase() === "index.html" && (f.size ?? 1) > 0) ||
            list.find((f) => {
              const l = f.name.toLowerCase();
              return (l.endsWith(".html") || l.endsWith(".htm")) && (f.size ?? 1) > 0;
            }) ||
            list.find((f) => f.name.toLowerCase() === "index.html") ||
            list.find((f) => {
              const l = f.name.toLowerCase();
              return l.endsWith(".html") || l.endsWith(".htm");
            });

          if (webAppFile) {
            hasAutoSelectedRef.current = true;
            setSelectedFilePath(webAppFile.path);
            setActiveTab("preview");
            return;
          }

          // 2. Media artifact deliverables
          const mediaFile = list.find((f) => getFileCategory(f.path) === "artifacts");
          if (mediaFile) {
            hasAutoSelectedRef.current = true;
            setSelectedFilePath(mediaFile.path);
            setActiveTab("artifacts");
            return;
          }

          // 3. Fallback to the first available non-empty file
          hasAutoSelectedRef.current = true;
          setSelectedFilePath(list[0].path);
          setActiveTab("editor");
        }
      }
    } catch (err) {
      console.error("[WorkspacePanel] Failed to fetch workspace files:", err);
    } finally {
      setFilesLoading(false);
    }
  }, [effectiveScopeId]);

  // Reset auto-select flag and trigger fetch when switching chats/jobs
  useEffect(() => {
    hasAutoSelectedRef.current = false;
    setSelectedFilePath(null);
    fetchFiles();
  }, [effectiveScopeId, fetchFiles]);

  // Listen to live push events
  useEffect(() => {
    const handleSwitchTab = (e: Event) => {
      const ce = e as CustomEvent<TabId | { tab: TabId; file?: string }>;
      const targetTab = typeof ce.detail === "string" ? ce.detail : ce.detail?.tab;
      const targetFile = typeof ce.detail === "object" ? ce.detail?.file : undefined;

      if (targetFile) {
        setSelectedFilePath(targetFile.replace(/\\/g, "/"));
      }
      if (targetTab && ["preview", "files", "artifacts", "logs", "editor"].includes(targetTab)) {
        setActiveTab(targetTab as TabId);
      }
    };

    const handleArtifactCreated = (e: Event) => {
      const ce = e as CustomEvent<{ artifact?: string; path?: string; relative_path?: string }>;
      const artPath = (ce.detail?.relative_path || ce.detail?.path || ce.detail?.artifact || "").replace(/\\/g, "/");
      if (artPath) {
        setSelectedFilePath(artPath);
        const cat = getFileCategory(artPath);
        if (cat === "artifacts") {
          setActiveTab("artifacts");
        } else if (cat === "preview") {
          setActiveTab("preview");
        }
        // Refresh list to display newly discovered artifact
        fetchFiles();
      }
    };

    window.addEventListener("peldrun:switch-tab", handleSwitchTab);
    window.addEventListener("peldrun:artifact-created", handleArtifactCreated);
    window.addEventListener("peldrun:artifact-updated", handleArtifactCreated);

    return () => {
      window.removeEventListener("peldrun:switch-tab", handleSwitchTab);
      window.removeEventListener("peldrun:artifact-created", handleArtifactCreated);
      window.removeEventListener("peldrun:artifact-updated", handleArtifactCreated);
    };
  }, [fetchFiles]);

  const handleExportZip = async () => {
    if (!effectiveScopeId) return;
    setExporting(true);
    try {
      const res = await fetch(`/api/run/jobs/${effectiveScopeId}/download-zip`);
      if (!res.ok) {
        throw new Error(`Failed to export ZIP (HTTP ${res.status})`);
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `workspace_${effectiveScopeId}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Export workspace ZIP failed:", err);
    } finally {
      setExporting(false);
    }
  };

  useEffect(() => {
    if (overrideFile) {
      handleSelectArtifact(overrideFile);
    }
  }, [overrideFile, handleSelectArtifact]);

  useEffect(() => {
    if (overrideDraft) {
      setSelectedFilePath(overrideDraft.filename);
      setDraftContent(overrideDraft.content);
      setActiveTab("editor");
    }
  }, [overrideDraft]);

  const tabs: { id: TabId; label: string; icon: React.ReactNode }[] = [
    { id: "preview", label: "Preview", icon: <Monitor size={14} /> },
    { id: "files", label: "Files", icon: <FolderTree size={14} /> },
    { id: "artifacts", label: "Artifacts", icon: <Package size={14} /> },
    { id: "logs", label: "Logs", icon: <TerminalSquare size={14} /> },
    ...(selectedFilePath || draftContent
      ? [{ id: "editor" as TabId, label: "Editor", icon: <FileCode2 size={14} /> }]
      : []),
  ];

  const previewablePath =
    selectedFilePath && getFileCategory(selectedFilePath) === "preview"
      ? selectedFilePath
      : null;

  return (
    <div className="flex flex-col h-full bg-custom border-l border-border font-sans">
      <div className="h-12 border-b border-border bg-card/40 backdrop-blur-sm px-4 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-1 overflow-x-auto">
          {tabs.map((tab) => {
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-all cursor-pointer ${
                  isActive
                    ? "bg-primary text-primary-foreground shadow-peldrun-xs"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted/60"
                }`}
              >
                {tab.icon}
                <span>{tab.label}</span>
              </button>
            );
          })}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {selectedFilePath && (
            <span className="text-[11px] font-mono text-muted-foreground truncate max-w-[130px] hidden sm:inline" title={selectedFilePath}>
              {selectedFilePath}
            </span>
          )}

          {effectiveScopeId && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => fetchFiles()}
              disabled={filesLoading}
              className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground cursor-pointer"
              title="Refresh workspace files"
            >
              <RefreshCw size={12} className={filesLoading ? "animate-spin text-primary" : ""} />
            </Button>
          )}

          {effectiveScopeId && jobFiles.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleExportZip}
              disabled={exporting}
              className="h-7 px-2.5 text-xs font-sans gap-1.5 cursor-pointer text-muted-foreground hover:text-foreground border-border/60 hover:bg-muted/60 shadow-peldrun-xs"
              title="Export all session files as ZIP"
            >
              {exporting ? (
                <>
                  <Loader2 size={12} className="animate-spin text-primary" />
                  <span>Exporting...</span>
                </>
              ) : (
                <>
                  <Download size={12} className="text-primary" />
                  <span>Export ZIP</span>
                </>
              )}
            </Button>
          )}
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-hidden bg-background">
        {activeTab === "preview" && (
          <PreviewTab currentHtmlPath={previewablePath} activeJobId={effectiveScopeId} />
        )}
        {activeTab === "files" && (
          <FilesTab onSelectFile={handleSelectArtifact} activeJobId={effectiveScopeId} />
        )}
        {activeTab === "artifacts" && (
          <ArtifactsTab
            activeJobId={effectiveScopeId}
            chatId={activeChatId}
            selectedFile={selectedFilePath}
          />
        )}
        {activeTab === "logs" && <LogsTab activeJobId={effectiveScopeId} chatId={activeChatId} />}
        {activeTab === "editor" && (
          <EditorTab
            filePath={selectedFilePath}
            initialContent={draftContent}
            activeJobId={effectiveScopeId}
            onSave={handleEditorSaved}
          />
        )}
      </div>
    </div>
  );
}

export default WorkspacePanel;