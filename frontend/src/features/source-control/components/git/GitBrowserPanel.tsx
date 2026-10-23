// frontend/src/features/source-control/components/git/GitBrowserPanel.tsx

import React, { useState } from "react";
import {
  CircleDot,
  FileText,
  GitBranch,
  GitPullRequest,
  History,
  Package,
  Tag as TagIcon,
} from "lucide-react";
import { RepositoryBrowser } from "./RepositoryBrowser";
import { CommitHistory } from "./CommitHistory";
import { BranchManager } from "./BranchManager";
import { TagManager } from "./TagManager";
import { PullRequestPanel } from "../github/PullRequestPanel";
import { IssuePanel } from "../github/IssuePanel";
import { ReleaseManager } from "../github/ReleaseManager";
import type { WorkspaceResponse } from "../../api/schemas";

export interface GitBrowserPanelProps {
  workspaceId: string;
  repositoryId: string;
  provider?: string;
  currentBranch: string;
  /** Called when a branch checkout updates the workspace. */
  onWorkspaceUpdated?: (ws: WorkspaceResponse) => void;
}

type Tab =
  | "files"
  | "history"
  | "branches"
  | "pulls"
  | "issues"
  | "tags"
  | "releases";

/**
 * Container for the read-only Git browser tabs.
 *
 * Groups three distinct views (Files, History, Branches) behind a
 * single top-level tab bar. The `browseRef` state is owned here so
 * that switching from History to Files at a specific commit (via
 * the "Browse files at this commit" button) selects the correct
 * ref without a round trip through the parent.
 *
 * DESIGN — no internal router:
 *   Tab state is component-local. This keeps the panel usable in
 *   any layout (side panel, modal, dedicated page) without forcing
 *   the host application to expose URL routes for each tab. If the
 *   application later wants deep-linking, the host can lift
 *   `tab` and `browseRef` into the URL and pass them back as
 *   initial props.
 */
export function GitBrowserPanel({
  workspaceId,
  repositoryId,
  provider = "github",
  currentBranch,
  onWorkspaceUpdated,
}: GitBrowserPanelProps) {
  const [tab, setTab] = useState<Tab>("files");
  const [browseRef, setBrowseRef] = useState("HEAD");
  const [localBranch, setLocalBranch] = useState(currentBranch);

  return (
    <div className="flex flex-col h-full">
      {/* Tabs */}
      <div className="flex items-center gap-1 px-2 py-1.5 border-b border-border bg-gigbg shrink-0">
        <TabButton
          active={tab === "files"}
          onClick={() => setTab("files")}
          icon={<FileText className="w-3.5 h-3.5" />}
          label="Files"
        />
        <TabButton
          active={tab === "history"}
          onClick={() => setTab("history")}
          icon={<History className="w-3.5 h-3.5" />}
          label="History"
        />
        <TabButton
          active={tab === "branches"}
          onClick={() => setTab("branches")}
          icon={<GitBranch className="w-3.5 h-3.5" />}
          label="Branches"
        />
        <TabButton
          active={tab === "pulls"}
          onClick={() => setTab("pulls")}
          icon={<GitPullRequest className="w-3.5 h-3.5" />}
          label="PRs"
        />
        <TabButton
          active={tab === "issues"}
          onClick={() => setTab("issues")}
          icon={<CircleDot className="w-3.5 h-3.5" />}
          label="Issues"
        />
        <TabButton
          active={tab === "tags"}
          onClick={() => setTab("tags")}
          icon={<TagIcon className="w-3.5 h-3.5" />}
          label="Tags"
        />
        <TabButton
          active={tab === "releases"}
          onClick={() => setTab("releases")}
          icon={<Package className="w-3.5 h-3.5" />}
          label="Releases"
        />

        {tab === "files" && (
          <div className="ml-auto flex items-center gap-1.5">
            <span className="text-[10px] text-muted-foreground">
              ref:
            </span>
            <input
              type="text"
              value={browseRef}
              onChange={(e) =>
                setBrowseRef(e.target.value || "HEAD")
              }
              className="text-[11px] w-32 px-1.5 py-0.5 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary font-mono"
              spellCheck={false}
            />
          </div>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        {tab === "files" && (
          <RepositoryBrowser
            workspaceId={workspaceId}
            ref={browseRef}
          />
        )}
        {tab === "history" && (
          <CommitHistory
            workspaceId={workspaceId}
            ref="HEAD"
            onBrowseRef={(sha) => {
              setBrowseRef(sha);
              setTab("files");
            }}
          />
        )}
        {tab === "branches" && (
          <BranchManager
            workspaceId={workspaceId}
            repositoryId={repositoryId}
            provider={provider}
            currentBranch={localBranch}
            onAfterCheckout={(ws) => {
              setLocalBranch(ws.current_branch);
              onWorkspaceUpdated?.(ws);
            }}
          />
        )}
        {tab === "pulls" && (
          <PullRequestPanel
            repositoryId={repositoryId}
            provider={provider}
            currentBranch={localBranch}
          />
        )}
        {tab === "issues" && (
          <IssuePanel
            repositoryId={repositoryId}
            provider={provider}
          />
        )}
        {tab === "tags" && <TagManager workspaceId={workspaceId} />}
        {tab === "releases" && (
          <ReleaseManager
            repositoryId={repositoryId}
            provider={provider}
          />
        )}
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1 px-2 py-1 rounded text-[11px] font-medium transition-colors ${
        active
          ? "bg-primary/10 text-primary"
          : "text-muted-foreground hover:bg-muted"
      }`}
    >
      {icon}
      {label}
    </button>
  );
}
