// Path: frontend/src/components/workspace/workspace-panel.tsx

/**
 * frontend/src/components/workspace/workspace-panel.tsx
 *
 * Workspace Panel component hardened against frontend freeze and path divergence.
 * Key enhancements:
 * - Eliminates setState-in-render errors across components.
 * - Enforces Preview stickiness: CSS/JS generation never hijacks preview focus.
 * - Text documents (.txt, .md, .markdown) open under Preview tab.
 * - Editor tab only opens upon deliberate user selection.
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
  GitBranch,
} from "lucide-react";
import { SourceControlPanel } from "@/features/source-control/components/SourceControlPanel";
import { Button } from "@/components/ui/button";
import { PreviewTab } from "@/components/workspace/preview-tab";
import { FilesTab } from "@/components/workspace/files-tab";
import { ArtifactsTab } from "@/components/workspace/artifacts-tab";
import { LogsTab } from "@/components/workspace/logs-tab";
import { EditorTab } from "@/components/workspace/editor-tab";

export type TabId = "preview" | "files" | "artifacts" | "logs" | "editor" | "source-control";

export interface WorkspaceFileItem {
  name: string;
  path: string;
  size?: number;
}

export function getFileCategory(filepath: string): "preview" | "artifacts" | "editor" {
  if (!filepath) return "preview";
  const lower = filepath.toLowerCase();
  const ext = lower.split(".").pop() || "";

  // Web apps, SVGs, and markdown/text documents belong to Preview
  if (["html", "htm", "svg", "md", "markdown", "txt"].includes(ext)) {
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

  const activeTabRef = useRef<TabId>(activeTab);
  activeTabRef.current = activeTab;

  const lastPreviewFileRef = useRef<string | null>(null);

  // Keep track of the latest HTML/preview deliverable path
  if (selectedFilePath && getFileCategory(selectedFilePath) === "preview") {
    lastPreviewFileRef.current = selectedFilePath;
  }

  // Explicit user selection (clicking a file icon, chat deliverable, or tree entry)
  const handleUserSelectFile = useCallback((filePath: string) => {
    if (!filePath) return;
    const cleanPath = filePath.replace(/\\/g, "/");
    setSelectedFilePath(cleanPath);
    setDraftContent(null);

    const category = getFileCategory(cleanPath);
    if (category === "preview") {
      lastPreviewFileRef.current = cleanPath;
      setActiveTab("preview");
    } else if (category === "artifacts") {
      setActiveTab("artifacts");
    } else {
      setActiveTab("editor");
    }
  }, []);

  // Autonomous agent artifact creation (only Preview or Artifacts may open automatically)
  const handleAutoArtifact = useCallback((filePath: string) => {
    if (!filePath) return;
    const cleanPath = filePath.replace(/\\/g, "/");
    const category = getFileCategory(cleanPath);

    if (category === "preview") {
      lastPreviewFileRef.current = cleanPath;
      setSelectedFilePath(cleanPath);
      setActiveTab("preview");
    } else if (category === "artifacts") {
      if (activeTabRef.current !== "preview") {
        setSelectedFilePath(cleanPath);
        setActiveTab("artifacts");
      }
    } else {
      // Code/CSS/JS generation: maintain preview tab and keep previewable deliverable active
    }
  }, []);

  const handleEditorSaved = (savedPath: string) => {
    const cleanPath = savedPath.replace(/\\/g, "/");
    setSelectedFilePath(cleanPath);
    const category = getFileCategory(cleanPath);
    if (category === "preview") {
      lastPreviewFileRef.current = cleanPath;
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
          // 1. Prefer non-empty HTML or preview files
          const webAppFile =
            list.find((f) => f.name.toLowerCase() === "index.html" && (f.size ?? 1) > 0) ||
            list.find((f) => {
              const l = f.name.toLowerCase();
              return (l.endsWith(".html") || l.endsWith(".htm")) && (f.size ?? 1) > 0;
            }) ||
            list.find((f) => getFileCategory(f.path) === "preview" && (f.size ?? 1) > 0) ||
            list.find((f) => f.name.toLowerCase() === "index.html") ||
            list.find((f) => {
              const l = f.name.toLowerCase();
              return l.endsWith(".html") || l.endsWith(".htm");
            }) ||
            list.find((f) => getFileCategory(f.path) === "preview");

          if (webAppFile) {
            hasAutoSelectedRef.current = true;
            lastPreviewFileRef.current = webAppFile.path;
            setSelectedFilePath(webAppFile.path);
            setActiveTab("preview");
            return;
          }

          // 2. Visual media artifact deliverables
          const mediaFile = list.find((f) => getFileCategory(f.path) === "artifacts");
          if (mediaFile) {
            hasAutoSelectedRef.current = true;
            setSelectedFilePath(mediaFile.path);
            setActiveTab("artifacts");
            return;
          }

          // 3. If only code files exist, do not auto-open editor; default to preview or files
          hasAutoSelectedRef.current = true;
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
    lastPreviewFileRef.current = null;
    fetchFiles();
  }, [effectiveScopeId, fetchFiles]);

  // Listen to live push events
  useEffect(() => {
    const handleSwitchTab = (e: Event) => {
      const ce = e as CustomEvent<TabId | { tab?: TabId; file?: string; userInitiated?: boolean }>;
      const targetTab = typeof ce.detail === "string" ? ce.detail : ce.detail?.tab;
      const targetFile = typeof ce.detail === "object" ? ce.detail?.file : undefined;
      const isUserAction = typeof ce.detail === "object" ? Boolean(ce.detail?.userInitiated) : false;

      if (targetFile) {
        if (isUserAction) {
          handleUserSelectFile(targetFile);
        } else {
          handleAutoArtifact(targetFile);
        }
      } else if (targetTab && ["preview", "files", "artifacts", "logs", "editor", "source-control"].includes(targetTab)) {
        if (isUserAction || targetTab === "preview" || targetTab === "artifacts") {
          setActiveTab(targetTab as TabId);
        }
      }
    };

    const handleArtifactCreated = (e: Event) => {
      const ce = e as CustomEvent<{ artifact?: string; path?: string; relative_path?: string; file?: string }>;
      const artPath = (ce.detail?.relative_path || ce.detail?.path || ce.detail?.artifact || ce.detail?.file || "").replace(/\\/g, "/");
      if (artPath) {
        handleAutoArtifact(artPath);
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
  }, [fetchFiles, handleAutoArtifact, handleUserSelectFile]);

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
      handleUserSelectFile(overrideFile);
    }
  }, [overrideFile, handleUserSelectFile]);

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
    { id: "source-control", label: "Source Control", icon: <GitBranch size={14} /> },
    { id: "logs", label: "Logs", icon: <TerminalSquare size={14} /> },
    ...(selectedFilePath || draftContent
      ? [{ id: "editor" as TabId, label: "Editor", icon: <FileCode2 size={14} /> }]
      : []),
  ];

  const previewablePath =
    selectedFilePath && getFileCategory(selectedFilePath) === "preview"
      ? selectedFilePath
      : lastPreviewFileRef.current;

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
          <FilesTab onSelectFile={handleUserSelectFile} activeJobId={effectiveScopeId} />
        )}
        {activeTab === "artifacts" && (
          <ArtifactsTab
            activeJobId={effectiveScopeId}
            chatId={activeChatId}
            selectedFile={selectedFilePath}
          />
        )}
        {activeTab === "source-control" && (
          <SourceControlPanel />
        )}
        {activeTab === "logs" && (
          <LogsTab
            activeJobId={activeJobId || effectiveScopeId}
            chatId={activeChatId || effectiveScopeId}
          />
        )}
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
