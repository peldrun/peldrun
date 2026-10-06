/**
 * frontend/src/components/workspace/files-tab.tsx
 *
 * Authoritative Workspace Files Tree tab.
 * Dynamically builds and renders hierarchical folders from flat relative paths.
 * Supports expanding/collapsing directories, nested file inspection, and direct editing trigger.
 */

"use client";

import React, { useEffect, useState, useMemo } from "react";
import { Folder, FolderOpen, FileText, Download, Trash2, RefreshCw, Filter, ChevronRight, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";

interface FileItem {
  name: string;
  path: string;
  isDir: boolean;
  size?: number;
  extension?: string;
  modified?: number;
  children?: FileItem[];
}

export interface FilesTabProps {
  onSelectFile?: (path: string) => void;
  onOpenFile?: (path: string) => void;
  activeJobId?: string | null;
}

function buildFileTree(flatFiles: FileItem[]): FileItem[] {
  if (flatFiles.length === 0) return [];

  const rootItems: FileItem[] = [];
  const dirMap = new Map<string, FileItem>();

  for (const file of flatFiles) {
    const rawPath = (file.path || file.name).replace(/\\/g, "/").replace(/^\/+/, "");
    const parts = rawPath.split("/").filter(Boolean);

    if (parts.length <= 1) {
      rootItems.push({
        ...file,
        name: parts[0] || file.name,
        path: rawPath,
        isDir: false,
      });
      continue;
    }

    let currentDirPath = "";
    let parentChildren = rootItems;

    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      currentDirPath = currentDirPath ? `${currentDirPath}/${part}` : part;

      let dirNode = dirMap.get(currentDirPath);
      if (!dirNode) {
        dirNode = {
          name: part,
          path: currentDirPath,
          isDir: true,
          children: [],
        };
        dirMap.set(currentDirPath, dirNode);
        parentChildren.push(dirNode);
      }
      parentChildren = dirNode.children!;
    }

    parentChildren.push({
      ...file,
      name: parts[parts.length - 1],
      path: rawPath,
      isDir: false,
    });
  }

  // Sort: folders first, then files alphabetically
  const sortRecursive = (items: FileItem[]) => {
    items.sort((a, b) => {
      if (a.isDir && !b.isDir) return -1;
      if (!a.isDir && b.isDir) return 1;
      return a.name.localeCompare(b.name);
    });
    for (const item of items) {
      if (item.isDir && item.children) {
        sortRecursive(item.children);
      }
    }
  };

  sortRecursive(rootItems);
  return rootItems;
}

export function FilesTab({ onSelectFile, onOpenFile, activeJobId }: FilesTabProps) {
  const [allFiles, setAllFiles] = useState<FileItem[]>([]);
  const [taskFiles, setTaskFiles] = useState<FileItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [filterCurrentOnly, setFilterCurrentOnly] = useState(true);
  const [collapsedDirs, setCollapsedDirs] = useState<Set<string>>(new Set());

  const handleItemClick = (path: string) => {
    if (onSelectFile) onSelectFile(path);
    if (onOpenFile) onOpenFile(path);
  };

  const toggleDir = (dirPath: string) => {
    setCollapsedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(dirPath)) {
        next.delete(dirPath);
      } else {
        next.add(dirPath);
      }
      return next;
    });
  };

  const fetchFiles = async () => {
    setLoading(true);
    try {
      // 1. Fetch generic workspace files
      const resAll = await fetch("/api/files?t=" + Date.now());
      if (resAll.ok) {
        const dataAll = await resAll.json();
        setAllFiles(dataAll.files || []);
      }

      // 2. Fetch active job deliverable files
      if (activeJobId) {
        const resTask = await fetch(`/api/run/jobs/${activeJobId}/files?t=` + Date.now());
        if (resTask.ok) {
          const dataTask = await resTask.json();
          setTaskFiles(dataTask.files || []);
        }
      } else {
        setTaskFiles([]);
      }
    } catch (err) {
      console.error("Failed to load workspace files:", err);
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (filePath: string) => {
    if (!confirm(`Are you sure you want to delete ${filePath}?`)) return;
    try {
      const res = await fetch(`/api/files?path=${encodeURIComponent(filePath)}`, {
        method: "DELETE",
      });
      if (res.ok) {
        fetchFiles();
      }
    } catch (err) {
      console.error("Failed to delete file:", err);
    }
  };

  useEffect(() => {
    fetchFiles();
  }, [activeJobId]);

  useEffect(() => {
    const handleSaved = () => fetchFiles();
    window.addEventListener("peldrun:file-saved", handleSaved);
    window.addEventListener("peldrun:artifact-created", handleSaved);
    return () => {
      window.removeEventListener("peldrun:file-saved", handleSaved);
      window.removeEventListener("peldrun:artifact-created", handleSaved);
    };
  }, []);

  const rawList = filterCurrentOnly && activeJobId ? taskFiles : allFiles;
  const hierarchicalTree = useMemo(() => buildFileTree(rawList), [rawList]);

  const renderTree = (items: FileItem[]) => {
    if (items.length === 0) {
      return (
        <div className="p-6 text-center text-xs text-muted-foreground">
          {filterCurrentOnly
            ? "No deliverables produced in this specific task yet."
            : "No files found in workspace."}
        </div>
      );
    }

    return (
      <ul className="space-y-0.5 select-none">
        {items.map((item) => {
          const isDirCollapsed = collapsedDirs.has(item.path);

          return (
            <li key={item.path} className="text-xs">
              <div className="flex items-center justify-between p-1 rounded hover:bg-muted/50 group transition">
                <div
                  className="flex items-center gap-1.5 truncate cursor-pointer flex-1"
                  onClick={() => {
                    if (item.isDir) {
                      toggleDir(item.path);
                    } else {
                      handleItemClick(item.path);
                    }
                  }}
                >
                  {item.isDir ? (
                    <>
                      {isDirCollapsed ? (
                        <ChevronRight size={12} className="text-muted-foreground shrink-0" />
                      ) : (
                        <ChevronDown size={12} className="text-muted-foreground shrink-0" />
                      )}
                      {isDirCollapsed ? (
                        <Folder size={14} className="text-amber-400 shrink-0" />
                      ) : (
                        <FolderOpen size={14} className="text-amber-400 shrink-0" />
                      )}
                      <span className="font-semibold text-foreground truncate">{item.name}</span>
                    </>
                  ) : (
                    <>
                      <span className="w-3" />
                      <FileText size={14} className="text-primary shrink-0" />
                      <span className="truncate text-foreground/90">{item.name}</span>
                      {item.size !== undefined && (
                        <span className="text-[10px] text-muted-foreground ml-1">
                          ({(item.size / 1024).toFixed(1)} KB)
                        </span>
                      )}
                    </>
                  )}
                </div>

                {!item.isDir && (
                  <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <a
                      href={`/api/files/download?path=${encodeURIComponent(item.path)}`}
                      download
                      className="p-1 text-muted-foreground hover:text-foreground"
                      title="Download"
                    >
                      <Download size={12} />
                    </a>
                    <button
                      onClick={() => handleDelete(item.path)}
                      className="p-1 text-muted-foreground hover:text-destructive"
                      title="Delete"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                )}
              </div>

              {item.isDir && item.children && item.children.length > 0 && !isDirCollapsed && (
                <div className="pl-3.5 border-l border-border/40 ml-2 mt-0.5">
                  {renderTree(item.children)}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    );
  };

  return (
    <div className="flex flex-col h-full bg-background text-foreground font-sans text-xs">
      <div className="flex items-center justify-between p-3 border-b border-border bg-card/40 backdrop-blur-sm">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-xs tracking-wider text-muted-foreground font-mono">
            WORKSPACE FILES
          </span>
          <button
            onClick={() => setFilterCurrentOnly(!filterCurrentOnly)}
            className={`px-1.5 py-0.5 rounded text-[10px] flex items-center gap-1 transition cursor-pointer font-sans ${
              filterCurrentOnly
                ? "bg-primary/20 text-primary border border-primary/40 font-medium"
                : "bg-muted text-muted-foreground hover:text-foreground"
            }`}
            title="Toggle between current task deliverables and global workspace"
          >
            <Filter size={10} />
            {filterCurrentOnly ? "This Task" : "All Workspace"}
          </button>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={fetchFiles}
          disabled={loading}
          className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground cursor-pointer"
          title="Refresh files"
        >
          <RefreshCw size={12} className={loading ? "animate-spin text-primary" : ""} />
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto p-3 font-mono">{renderTree(hierarchicalTree)}</div>
    </div>
  );
}

export default FilesTab;