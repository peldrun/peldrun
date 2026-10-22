// frontend/src/features/source-control/components/git/TagManager.tsx

import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  Loader2,
  Plus,
  RefreshCw,
  Tag as TagIcon,
  Trash2,
} from "lucide-react";
import { sourceControlClient } from "../../api/source-control-client";
import type { TagResponse } from "../../api/schemas";

export interface TagManagerProps {
  workspaceId: string;
}

type FilterMode = "all" | "annotated" | "lightweight";

const FILTERS: { key: FilterMode; label: string }[] = [
  { key: "all", label: "All" },
  { key: "annotated", label: "Annotated" },
  { key: "lightweight", label: "Lightweight" },
];

/**
 * Local Git tag browser and creation form.
 *
 * Manages the LOCAL Git tags of a workspace. Deleting a tag here
 * does NOT remove any remote tag (GitHub's tag namespace is
 * separate); a separate remote-delete operation would be required
 * to remove the tag from the origin, and that is out of scope for
 * this phase.
 *
 * The creation form distinguishes annotated tags from lightweight
 * tags by the presence of a message field. This mirrors Git's own
 * convention.
 */
export function TagManager({ workspaceId }: TagManagerProps) {
  const [tags, setTags] = useState<TagResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterMode>("all");
  const [reloadKey, setReloadKey] = useState(0);

  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newMessage, setNewMessage] = useState("");
  const [newTargetSha, setNewTargetSha] = useState("");
  const [forceOverwrite, setForceOverwrite] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createSuccess, setCreateSuccess] = useState<string | null>(
    null
  );

  const [deleteLoading, setDeleteLoading] = useState<string | null>(
    null
  );
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const loadTags = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    sourceControlClient
      .listWorkspaceTags(workspaceId)
      .then((res) => {
        if (!cancelled) setTags(res.items);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Failed to load tags."
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  useEffect(() => {
    const cleanup = loadTags();
    return cleanup;
  }, [loadTags, reloadKey]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim() || creating) return;
    setCreating(true);
    setCreateError(null);
    setCreateSuccess(null);
    try {
      const tag = await sourceControlClient.createWorkspaceTag(workspaceId, {
        name: newName.trim(),
        message: newMessage.trim() || null,
        target_sha: newTargetSha.trim() || null,
        force: forceOverwrite,
      });
      setCreateSuccess(`Created tag '${tag.name}'.`);
      setNewName("");
      setNewMessage("");
      setNewTargetSha("");
      setForceOverwrite(false);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setCreateError(
        err instanceof Error
          ? err.message
          : "Failed to create tag."
      );
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (name: string) => {
    if (!window.confirm(`Delete tag '${name}'?`)) return;
    setDeleteLoading(name);
    setDeleteError(null);
    try {
      await sourceControlClient.deleteWorkspaceTag(workspaceId, name);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setDeleteError(
        err instanceof Error
          ? err.message
          : "Failed to delete tag."
      );
    } finally {
      setDeleteLoading(null);
    }
  };

  const filtered = tags.filter((t) => {
    if (filter === "all") return true;
    if (filter === "annotated") return t.is_annotated;
    return !t.is_annotated;
  });

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-card/40 shrink-0 gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <TagIcon className="w-3.5 h-3.5 shrink-0 text-muted-foreground" />
          <span className="text-xs font-semibold text-foreground">
            Tags
          </span>
          <span className="text-[10px] text-muted-foreground">
            ({tags.length})
          </span>
        </div>
        <div className="flex items-center gap-1">
          <div className="flex items-center gap-1 rounded-md bg-muted p-0.5">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                type="button"
                onClick={() => setFilter(f.key)}
                className={`px-2 py-0.5 text-[11px] font-medium rounded transition-colors ${
                  filter === f.key
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => setShowCreate((v) => !v)}
            className="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-border hover:bg-muted"
            title="Create tag"
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
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="v1.2.3"
                maxLength={200}
                disabled={creating}
                className="w-full text-xs px-2 py-1 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary font-mono"
              />
            </div>
            <div>
              <label className="block text-[10px] font-medium text-muted-foreground mb-1">
                Target SHA (default: HEAD)
              </label>
              <input
                type="text"
                value={newTargetSha}
                onChange={(e) => setNewTargetSha(e.target.value)}
                placeholder="abc123..."
                maxLength={64}
                disabled={creating}
                className="w-full text-xs px-2 py-1 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary font-mono"
              />
            </div>
          </div>
          <div>
            <label className="block text-[10px] font-medium text-muted-foreground mb-1">
              Annotation message (blank = lightweight tag)
            </label>
            <textarea
              value={newMessage}
              onChange={(e) => setNewMessage(e.target.value)}
              rows={2}
              maxLength={65_536}
              disabled={creating}
              placeholder="Release notes or tag description"
              className="w-full text-xs px-2 py-1 rounded border border-border bg-background focus:outline-none focus:ring-1 focus:ring-primary resize-none"
            />
          </div>
          <label className="flex items-center gap-1.5 text-[11px] cursor-pointer select-none">
            <input
              type="checkbox"
              checked={forceOverwrite}
              onChange={(e) => setForceOverwrite(e.target.checked)}
              disabled={creating}
            />
            Overwrite if tag already exists
          </label>
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
              disabled={creating || !newName.trim()}
              className="text-xs px-2 py-1 rounded bg-primary text-primary-foreground disabled:opacity-50 inline-flex items-center gap-1"
            >
              {creating && <Loader2 className="w-3 h-3 animate-spin" />}
              Create tag
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
            Loading tags…
          </div>
        )}

        {!loading && error && (
          <div className="p-3 flex items-start gap-2 text-xs text-red-700">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {!loading && !error && filtered.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full p-8 text-center text-muted-foreground">
            <TagIcon className="w-10 h-10 mb-3 text-muted-foreground/50" />
            <h3 className="text-xs font-semibold text-foreground">
              {tags.length === 0 ? "No tags yet" : "No tags in this filter"}
            </h3>
            <p className="text-[11px] mt-1 max-w-xs leading-relaxed">
              {tags.length === 0
                ? "Create a tag to mark a point in this workspace's history."
                : "Try a different filter."}
            </p>
          </div>
        )}

        {!loading && !error && filtered.length > 0 && (
          <ul className="divide-y divide-border">
            {filtered.map((tag) => (
              <li
                key={tag.name}
                className="flex items-center gap-2 px-3 py-2 hover:bg-muted/30"
              >
                <TagIcon
                  className={`w-3.5 h-3.5 shrink-0 ${
                    tag.is_annotated
                      ? "text-purple-500"
                      : "text-muted-foreground"
                  }`}
                />
                <div className="flex-1 min-w-0">
                  <div className="text-xs font-medium text-foreground truncate flex items-center gap-1.5 flex-wrap">
                    <code className="font-mono">{tag.name}</code>
                    {tag.is_annotated && (
                      <span className="text-[9px] px-1 py-0.5 rounded bg-purple-100 text-purple-700">
                        annotated
                      </span>
                    )}
                  </div>
                  <div className="text-[10px] text-muted-foreground flex items-center gap-1.5 flex-wrap mt-0.5">
                    <code className="font-mono">
                      {tag.sha.slice(0, 8)}
                    </code>
                    {tag.tagger_name && (
                      <>
                        <span>·</span>
                        <span>{tag.tagger_name}</span>
                      </>
                    )}
                    {tag.tagged_at && (
                      <>
                        <span>·</span>
                        <span>{formatDate(tag.tagged_at)}</span>
                      </>
                    )}
                  </div>
                  {tag.message && (
                    <div className="text-[10px] text-muted-foreground mt-0.5 truncate italic">
                      {tag.message}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => handleDelete(tag.name)}
                  disabled={!!deleteLoading}
                  className="p-1 rounded hover:bg-muted text-red-600 disabled:opacity-40 shrink-0"
                  title="Delete tag"
                  aria-label="Delete tag"
                >
                  {deleteLoading === tag.name ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <Trash2 className="w-3.5 h-3.5" />
                  )}
                </button>
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
