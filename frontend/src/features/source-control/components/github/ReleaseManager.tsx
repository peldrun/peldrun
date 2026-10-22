// frontend/src/features/source-control/components/github/ReleaseManager.tsx

import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  ExternalLink,
  Loader2,
  Package,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type { ReleaseResponse } from "../../api/schemas";

export interface ReleaseManagerProps {
  repositoryId: string;
  provider?: string;
}

/**
 * GitHub release browser and creation form.
 *
 * This component manages REMOTE releases, not local tags. Creating
 * a release may create the underlying tag on GitHub (if it does
 * not already exist); the tag created by a release is invisible to
 * the local Tag Manager until it is fetched into the workspace.
 *
 * Deleting a release here does NOT delete its tag. That mirrors
 * GitHub's own behavior and is surfaced to the user in the confirm
 * prompt.
 */
export function ReleaseManager({
  repositoryId,
  provider = "github",
}: ReleaseManagerProps) {
  const [releases, setReleases] = useState<ReleaseResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [includeDrafts, setIncludeDrafts] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  const [showCreate, setShowCreate] = useState(false);
  const [newTag, setNewTag] = useState("");
  const [newName, setNewName] = useState("");
  const [newBody, setNewBody] = useState("");
  const [newTarget, setNewTarget] = useState("main");
  const [isDraft, setIsDraft] = useState(false);
  const [isPrerelease, setIsPrerelease] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createSuccess, setCreateSuccess] = useState<string | null>(
    null
  );

  const [deleteLoading, setDeleteLoading] = useState<string | null>(
    null
  );
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const loadReleases = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    sourceControlClient
      .listReleases(repositoryId, {
        include_drafts: includeDrafts,
        provider,
        page_size: 100,
      })
      .then((res) => {
        if (!cancelled) setReleases(res.items);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(
            err instanceof Error
              ? err.message
              : "Failed to load releases."
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repositoryId, provider, includeDrafts]);

  useEffect(() => {
    const cleanup = loadReleases();
    return cleanup;
  }, [loadReleases, reloadKey]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTag.trim() || creating) return;
    setCreating(true);
    setCreateError(null);
    setCreateSuccess(null);
    try {
      const release = await sourceControlClient.createRelease(repositoryId, {
        tag_name: newTag.trim(),
        name: newName.trim(),
        body: newBody,
        target_commitish: newTarget.trim() || "main",
        draft: isDraft,
        prerelease: isPrerelease,
        provider,
      });
      setCreateSuccess(`Created release '${release.name}'.`);
      setNewTag("");
      setNewName("");
      setNewBody("");
      setNewTarget("main");
      setIsDraft(false);
      setIsPrerelease(false);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setCreateError(
        err instanceof Error
          ? err.message
          : "Failed to create release."
      );
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (release: ReleaseResponse) => {
    const confirmed = window.confirm(
      `Delete release '${release.name}'?\n\nThe underlying Git tag '${release.tag_name}' will NOT be deleted.`
    );
    if (!confirmed) return;
    setDeleteLoading(release.release_id);
    setDeleteError(null);
    try {
      await sourceControlClient.deleteRelease(
        repositoryId,
        release.release_id,
        provider
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      setDeleteError(
        err instanceof Error
          ? err.message
          : "Failed to delete release."
      );
    } finally {
      setDeleteLoading(null);
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-card/40 shrink-0 gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Package className="w-3.5 h-3.5 shrink-0 text-muted-foreground" />
          <span className="text-xs font-semibold text-foreground">
            Releases
          </span>
          <span className="text-[10px] text-muted-foreground">
            ({releases.length})
          </span>
        </div>
        <div className="flex items-center gap-1">
          <label className="flex items-center gap-1 text-[11px] cursor-pointer select-none">
            <input
              type="checkbox"
              checked={includeDrafts}
              onChange={(e) => setIncludeDrafts(e.target.checked)}
            />
            Drafts
          </label>
          <button
            type="button"
            onClick={() => setShowCreate((v) => !v)}
            className="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-border hover:bg-muted"
            title="Create release"
          >
            <Plus className="w-3 h-3" />
            New
          </button>
          <button
            type="button"
            onClick={() => setReloadKey((k) => k + 1)}
            disabled={loading}
            className="p-1 rounded hover:bg-muted disabled:opacity-50"
            title="Refresh"
            aria-label="Refresh"
          >
            <RefreshCw
              className={`w-3.5 h-3.5 text-muted-foreground ${
                loading ? "animate-spin" : ""
              }`}
            />
          </button>
        </div>
      </div>

      {/* Create form */}
      {showCreate && (
        <form
          onSubmit={handleCreate}
          className="px-3 py-2 border-b border-border bg-muted/30 space-y-2 shrink-0"
        >
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-[10px] font-medium text-muted-foreground mb-1">
                Tag name <span className="text-red-600">*</span>
              </label>
              <input
                type="text"
                value={newTag}
                onChange={(e) => setNewTag(e.target.value)}
                placeholder="v1.2.3"
                maxLength={200}
                disabled={creating}
                className="w-full text-xs px-2 py-1 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary font-mono"
              />
            </div>
            <div>
              <label className="block text-[10px] font-medium text-muted-foreground mb-1">
                Target branch / SHA
              </label>
              <input
                type="text"
                value={newTarget}
                onChange={(e) => setNewTarget(e.target.value)}
                placeholder="main"
                maxLength={200}
                disabled={creating}
                className="w-full text-xs px-2 py-1 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary font-mono"
              />
            </div>
          </div>
          <div>
            <label className="block text-[10px] font-medium text-muted-foreground mb-1">
              Release title
            </label>
            <input
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="v1.2.3 — My release title"
              maxLength={256}
              disabled={creating}
              className="w-full text-xs px-2 py-1 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          <div>
            <label className="block text-[10px] font-medium text-muted-foreground mb-1">
              Release notes (Markdown)
            </label>
            <textarea
              value={newBody}
              onChange={(e) => setNewBody(e.target.value)}
              rows={4}
              maxLength={125_000}
              disabled={creating}
              placeholder="What's new in this release?"
              className="w-full text-xs px-2 py-1 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary resize-none font-mono"
            />
          </div>
          <div className="flex items-center gap-3 text-[11px]">
            <label className="flex items-center gap-1 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={isDraft}
                onChange={(e) => setIsDraft(e.target.checked)}
                disabled={creating}
              />
              Draft
            </label>
            <label className="flex items-center gap-1 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={isPrerelease}
                onChange={(e) => setIsPrerelease(e.target.checked)}
                disabled={creating}
              />
              Pre-release
            </label>
          </div>
          {createError && (
            <div className="flex items-start gap-1.5 text-[10px] text-red-700">
              <AlertCircle className="w-3 h-3 shrink-0 mt-0.5" />
              <span>{createError}</span>
            </div>
          )}
          {createSuccess && (
            <div className="flex items-start gap-1.5 text-[10px] text-green-700">
              <Check className="w-3 h-3 shrink-0 mt-0.5" />
              <span>{createSuccess}</span>
            </div>
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => setShowCreate(false)}
              disabled={creating}
              className="text-xs px-2 py-1 rounded border border-border hover:bg-muted disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={creating || !newTag.trim()}
              className="text-xs px-2 py-1 rounded bg-primary text-primary-foreground disabled:opacity-50 inline-flex items-center gap-1"
            >
              {creating && <Loader2 className="w-3 h-3 animate-spin" />}
              Publish release
            </button>
          </div>
        </form>
      )}

      {deleteError && (
        <div className="px-3 py-2 border-b border-border bg-red-50 text-[10px] text-red-700 flex items-start gap-1.5 shrink-0">
          <AlertCircle className="w-3 h-3 shrink-0 mt-0.5" />
          <span>{deleteError}</span>
        </div>
      )}

      {/* List */}
      <div className="flex-1 overflow-auto">
        {loading && (
          <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Loading releases…
          </div>
        )}

        {!loading && error && (
          <div className="p-3 flex items-start gap-2 text-xs text-red-700">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {!loading && !error && releases.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full p-8 text-center text-muted-foreground">
            <Package className="w-10 h-10 mb-3 text-muted-foreground/50" />
            <h3 className="text-xs font-semibold text-foreground">
              No releases yet
            </h3>
            <p className="text-[11px] mt-1 max-w-xs leading-relaxed">
              Publish a release to bundle a tag with release notes
              on GitHub.
            </p>
          </div>
        )}

        {!loading && !error && releases.length > 0 && (
          <ul className="divide-y divide-border">
            {releases.map((release) => (
              <li
                key={release.release_id}
                className="flex items-start gap-2 px-3 py-2.5 hover:bg-muted/30"
              >
                <Package
                  className={`w-3.5 h-3.5 shrink-0 mt-0.5 ${
                    release.is_draft
                      ? "text-muted-foreground"
                      : release.is_prerelease
                      ? "text-amber-500"
                      : "text-green-600"
                  }`}
                />
                <div className="flex-1 min-w-0">
                  <div className="text-xs font-medium text-foreground truncate flex items-center gap-1.5 flex-wrap">
                    <span>{release.name || release.tag_name}</span>
                    {release.is_draft && (
                      <span className="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-700">
                        draft
                      </span>
                    )}
                    {release.is_prerelease && (
                      <span className="text-[9px] px-1 py-0.5 rounded bg-amber-100 text-amber-700">
                        pre-release
                      </span>
                    )}
                  </div>
                  <div className="text-[10px] text-muted-foreground flex items-center gap-1.5 flex-wrap mt-0.5">
                    <code className="font-mono">{release.tag_name}</code>
                    {release.author_login && (
                      <>
                        <span>·</span>
                        <span>by {release.author_login}</span>
                      </>
                    )}
                    {release.published_at && (
                      <>
                        <span>·</span>
                        <span>{formatDate(release.published_at)}</span>
                      </>
                    )}
                  </div>
                  {release.body && (
                    <div className="text-[10px] text-muted-foreground mt-1 line-clamp-2 whitespace-pre-wrap">
                      {release.body}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  {release.html_url && (
                    <a
                      href={release.html_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="p-1 rounded hover:bg-muted"
                      title="Open on GitHub"
                      aria-label="Open on GitHub"
                    >
                      <ExternalLink className="w-3.5 h-3.5 text-muted-foreground" />
                    </a>
                  )}
                  <button
                    type="button"
                    onClick={() => handleDelete(release)}
                    disabled={!!deleteLoading}
                    className="p-1 rounded hover:bg-muted text-red-600 disabled:opacity-40"
                    title="Delete release"
                    aria-label="Delete release"
                  >
                    {deleteLoading === release.release_id ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Trash2 className="w-3.5 h-3.5" />
                    )}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
    });
  } catch {
    return iso;
  }
}
