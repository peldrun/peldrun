// frontend/src/features/source-control/components/git/RepositoryBrowser.tsx

import React, { useEffect, useRef, useState } from "react";
import { Tree, NodeRendererProps } from "react-arborist";
import {
  AlertCircle,
  File,
  Folder,
  FolderOpen,
  RefreshCw,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type { TreeNodeResponse } from "../../api/schemas";
import { FileViewer } from "./FileViewer";

export interface RepositoryBrowserProps {
  workspaceId: string;
  /**
   * Git ref to browse. Defaults to `HEAD`. The parent component is
   * expected to pass a different value when the user wants to
   * browse a tag or a specific commit from the history view.
   */
  ref?: string;
}

/**
 * Two-pane repository browser.
 *
 * The left pane shows the hierarchical file tree at `ref`; the
 * right pane shows the content of the selected file. Directory
 * selection toggles the node; file selection loads the file in the
 * viewer.
 *
 * DESIGN — committed content only:
 *   Both panes read COMMITTED content (`git ls-tree`, `git show`),
 *   not the working tree. Users who want to browse uncommitted
 *   edits should use the Diff & Review view instead. This keeps
 *   the Browser fast and cacheable: the same `(workspace, ref)`
 *   pair always yields the same tree.
 *
 * DESIGN — Tree component sizing:
 *   react-arborist requires explicit `width` and `height` numeric
 *   values for virtualization. We observe the container with a
 *   `ResizeObserver` so the tree adapts to the panel size without
 *   hardcoded pixel values. The initial dimensions are only used
 *   for the first render before the observer fires.
 */
export function RepositoryBrowser({
  workspaceId,
  ref = "HEAD",
}: RepositoryBrowserProps) {
  const [tree, setTree] = useState<TreeNodeResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const [dimensions, setDimensions] = useState({
    width: 280,
    height: 600,
  });

  // Observe the tree pane size so react-arborist gets real
  // width/height. A hardcoded 300x600 is unusable on smaller
  // windows; ResizeObserver keeps the tree usable at every size.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const update = () => {
      const rect = el.getBoundingClientRect();
      setDimensions({
        width: Math.max(180, Math.floor(rect.width)),
        height: Math.max(200, Math.floor(rect.height)),
      });
    };

    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Clear the selected file whenever the workspace or ref changes,
  // so a file that no longer exists at the new ref is not shown.
  useEffect(() => {
    setSelectedFile(null);
  }, [workspaceId, ref]);

  // Load the tree.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    sourceControlClient
      .getFileTree(workspaceId, ref)
      .then((data) => {
        if (!cancelled) setTree(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message =
            err instanceof Error ? err.message : "Failed to load tree.";
          setError(message);
          setTree([]);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [workspaceId, ref, reloadKey]);

  return (
    <div className="flex h-full">
      {/* Left pane: tree */}
      <div className="w-72 shrink-0 border-r border-border flex flex-col bg-card/20">
        {/* Toolbar */}
        <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
          <div className="text-xs font-semibold text-foreground truncate">
            Files
            <span className="ml-2 text-[10px] font-normal text-muted-foreground">
              @ {ref}
            </span>
          </div>
          <button
            type="button"
            onClick={() => setReloadKey((k) => k + 1)}
            disabled={loading}
            className="p-1 rounded hover:bg-muted disabled:opacity-50"
            title="Reload tree"
            aria-label="Reload tree"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 text-muted-foreground ${
                loading ? "animate-spin" : ""
              }`}
            />
          </button>
        </div>

        {/* Tree container (observed by ResizeObserver) */}
        <div ref={containerRef} className="flex-1 overflow-hidden">
          {loading && (
            <div className="p-3 text-xs text-muted-foreground">
              Loading…
            </div>
          )}

          {!loading && error && (
            <div className="p-3 flex items-start gap-1.5 text-xs text-red-700">
              <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          {!loading && !error && tree.length === 0 && (
            <div className="p-3 text-xs text-muted-foreground">
              Empty repository — no files at this ref.
            </div>
          )}

          {!loading && !error && tree.length > 0 && (
            <Tree<TreeNodeResponse>
              data={tree}
              idAccessor="path"
              openByDefault={false}
              width={dimensions.width}
              height={dimensions.height}
              indent={16}
              rowHeight={26}
              overscanCount={10}
              onSelect={(nodes) => {
                const node = nodes[0];
                if (node?.data.type === "file") {
                  setSelectedFile(node.data.path);
                }
              }}
            >
              {Node}
            </Tree>
          )}
        </div>
      </div>

      {/* Right pane: viewer */}
      <div className="flex-1 overflow-hidden">
        {selectedFile ? (
          <FileViewer
            workspaceId={workspaceId}
            ref={ref}
            path={selectedFile}
          />
        ) : (
          <div className="flex items-center justify-center h-full text-xs text-muted-foreground">
            Select a file to view its content.
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Node renderer for react-arborist.
 *
 * The `style` prop is provided by the library and MUST be applied
 * to the row container; without it, virtualization breaks and rows
 * overlap.
 *
 * Click behavior:
 *   - Directories toggle (expand/collapse).
 *   - Files call `node.select()`, which triggers the Tree's
 *     `onSelect` callback and updates `selectedFile`.
 */
function Node({
  node,
  style,
  dragHandle,
}: NodeRendererProps<TreeNodeResponse>) {
  const isDirectory = node.data.type === "directory";
  const icon = isDirectory ? (
    node.isOpen ? (
      <FolderOpen className="w-3.5 h-3.5 text-blue-500 shrink-0" />
    ) : (
      <Folder className="w-3.5 h-3.5 text-blue-500 shrink-0" />
    )
  ) : (
    <File className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
  );

  const handleClick = () => {
    if (isDirectory) {
      node.toggle();
    } else {
      node.select();
    }
  };

  return (
    <div
      style={style}
      ref={dragHandle}
      onClick={handleClick}
      className={`flex items-center gap-1.5 px-1 text-xs cursor-pointer rounded select-none ${
        node.isSelected
          ? "bg-primary/10 text-foreground"
          : "hover:bg-muted/60"
      }`}
    >
      {icon}
      <span className="truncate">{node.data.name}</span>
    </div>
  );
}
