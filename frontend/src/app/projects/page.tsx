// Path: frontend/src/app/projects/page.tsx
"use client";

/**
 * Projects list page.
 *
 * Reads project metadata from `project-store`, which is populated by
 * `AppDataBootstrap` on app load. No local fetch on mount — the page
 * renders whatever the store already has, and updates in real time as
 * the store changes.
 *
 * ## Design notes
 *
 *   * **No `fetchProjects()` on mount.** The bootstrap provider has
 *     already loaded the projects. Re-fetching here would duplicate
 *     that request on every visit to /projects.
 *
 *   * **No `omweb:refresh-sidebar` custom event.** After Phase F.4 the
 *     Sidebar reads directly from the store; the custom event has no
 *     listener left and would be dead code.
 *
 *   * **409 handling.** The backend now rejects project deletion when
 *     an active run exists in the project (see Phase E.3). This page
 *     surfaces that as a clear toast instead of a silent failure.
 *
 *   * **`default_project` is hidden.** It's the reserved bucket for
 *     standalone chats and must never appear as a user-facing
 *     workspace.
 */

import React, { useState, useEffect } from "react";
import Link from "next/link";
import {
  Folder,
  Plus,
  Trash2,
  Calendar,
  ArrowRight,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useProjectStore, DEFAULT_PROJECT_ID } from "@/stores/project-store";
import { useSessionStore } from "@/stores/session-store";
import { showToast } from "@/components/ui/ToastNotification";


export default function ProjectsPage() {
  // -------------------------------------------------------------------------
  // Store subscriptions
  // -------------------------------------------------------------------------
  const projectsById = useProjectStore((s) => s.projectsById);
  const projectOrder = useProjectStore((s) => s.projectOrder);
  const isInitialized = useProjectStore((s) => s.isInitialized);
  const isLoading = useProjectStore((s) => s.isLoading);
  const loadProjects = useProjectStore((s) => s.loadProjects);
  const upsertProject = useProjectStore((s) => s.upsertProject);
  const removeProject = useProjectStore((s) => s.removeProject);

  // -------------------------------------------------------------------------
  // Local UI state
  // -------------------------------------------------------------------------
  const [nameInput, setNameInput] = useState("");
  const [descInput, setDescInput] = useState("");
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [isCreating, setIsCreating] = useState(false);

  // -------------------------------------------------------------------------
  // Fallback initial load
  //
  // AppDataBootstrap fires loadProjects() at app start. If the user
  // somehow lands on /projects before that request finishes (rare but
  // possible on cold starts), `isInitialized` will still be false.
  // This effect is a defensive no-op-then-fetch: it calls the store's
  // idempotent loader, which returns immediately if the bootstrap
  // already completed the fetch.
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (!isInitialized) {
      void loadProjects();
    }
  }, [isInitialized, loadProjects]);

  // -------------------------------------------------------------------------
  // Derived values
  // -------------------------------------------------------------------------
  const orderedProjects = projectOrder
    .map((id) => projectsById[id])
    .filter((p): p is NonNullable<typeof p> => Boolean(p));

  const displayProjects = orderedProjects.filter(
    (p) => p.id !== DEFAULT_PROJECT_ID,
  );

  const showInitialLoader =
    !isInitialized && isLoading && displayProjects.length === 0;

  // -------------------------------------------------------------------------
  // Create handler
  //
  // Called by the modal form. Updates the store on success; leaves the
  // modal open and shows a toast on failure.
  // -------------------------------------------------------------------------
  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    const name = nameInput.trim();
    if (!name || isCreating) return;

    setIsCreating(true);
    try {
      const res = await fetch("/api/chats/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          description: descInput.trim(),
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      if (data.project) {
        upsertProject(data.project);
        showToast.success(
          "Project Created",
          `Workspace '${data.project.name}' is ready.`,
        );
      } else {
        // Defensive: backend returned success but no project payload.
        // Force a refresh so the list reflects whatever landed.
        void loadProjects(true);
      }

      setNameInput("");
      setDescInput("");
      setIsModalOpen(false);
    } catch (err: any) {
      showToast.error(
        "Creation Failed",
        err?.message || "Could not create project.",
      );
    } finally {
      setIsCreating(false);
    }
  };

  // -------------------------------------------------------------------------
  // Delete handler
  //
  // Uses the store to remove the project AND all its sessions on
  // success. Handles 409 (active run) by leaving everything intact and
  // surfacing a clear toast.
  // -------------------------------------------------------------------------
  const handleDelete = async (projectId: string) => {
    if (
      !confirm(
        "Are you sure you want to delete this project workspace and all its associated data?",
      )
    ) {
      return;
    }

    try {
      const res = await fetch(
        `/api/chats/projects/${encodeURIComponent(projectId)}`,
        { method: "DELETE" },
      );

      if (!res.ok) {
        if (res.status === 409) {
          const body = await res.json().catch(() => ({}));
          showToast.error(
            "Cannot Delete",
            body.detail ||
              "A run is still active in this project. Stop the run before deleting.",
          );
          return;
        }
        throw new Error(`HTTP ${res.status}`);
      }

      // Remove every session belonging to this project from the
      // session store, then the project itself.
      const sessionStore = useSessionStore.getState();
      for (const chat of sessionStore.getOrderedSessions()) {
        if (chat.project_id === projectId) {
          sessionStore.removeSession(chat.id);
        }
      }
      removeProject(projectId);

      showToast.success("Project Deleted", "Workspace removed.");
    } catch (err: any) {
      showToast.error(
        "Delete Failed",
        err?.message || "Could not delete project.",
      );
    }
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------
  return (
    <div className="flex flex-col flex-1 h-full w-full bg-background text-foreground overflow-hidden font-sans">
      <div className="h-14 flex items-center justify-between px-8 border-b border-border bg-card/40 backdrop-blur-sm shrink-0">
        <div className="flex items-center gap-3">
          <Folder className="h-5 w-5 text-primary" />
          <h1 className="font-heading font-semibold text-sm text-foreground">
            Projects Workspace
          </h1>
        </div>
        <Button
          onClick={() => setIsModalOpen(true)}
          className="flex items-center gap-2 h-8 px-3.5 rounded-md bg-primary text-primary-foreground text-xs font-medium transition shadow-xs cursor-pointer"
        >
          <Plus size={14} />
          <span>New Project</span>
        </Button>
      </div>

      <div className="flex-1 p-6 overflow-y-auto bg-background">
        {showInitialLoader ? (
          <div className="flex items-center justify-center gap-2 h-48 text-muted-foreground text-xs font-mono">
            <Loader2 size={14} className="animate-spin text-primary" />
            Loading workspaces from storage...
          </div>
        ) : displayProjects.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 text-center text-muted-foreground space-y-3">
            <div className="w-12 h-12 rounded-2xl bg-card border border-border shadow-xs flex items-center justify-center text-muted-foreground">
              <Folder size={24} />
            </div>
            <div>
              <p className="text-sm font-medium text-foreground">
                No custom projects created yet.
              </p>
              <button
                type="button"
                onClick={() => setIsModalOpen(true)}
                className="text-xs text-primary hover:underline mt-1 cursor-pointer"
              >
                Create your first project workspace
              </button>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {displayProjects.map((proj) => (
              <div
                key={proj.id}
                className="flex flex-col justify-between p-5 rounded-xl border border-border bg-card hover:border-primary/40 transition-all group shadow-xs"
              >
                <div>
                  <div className="flex items-center justify-between mb-3">
                    <span className="text-[10px] text-primary uppercase tracking-wider font-mono font-semibold">
                      {proj.id}
                    </span>
                    <button
                      type="button"
                      onClick={() => handleDelete(proj.id)}
                      className="text-muted-foreground hover:text-destructive transition p-1 cursor-pointer"
                      title="Delete project"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                  <h3 className="text-xs font-semibold text-foreground group-hover:text-primary transition font-heading">
                    {proj.name}
                  </h3>
                  <p className="text-xs text-muted-foreground mt-1 line-clamp-2 font-sans">
                    {proj.description || "No description provided."}
                  </p>
                </div>

                <div className="flex items-center justify-between pt-4 border-t border-border mt-6 text-xs">
                  <span className="text-muted-foreground text-[11px] flex items-center gap-1 font-mono">
                    <Calendar size={11} />{" "}
                    {proj.created_at?.split(" ")[0] || "Recent"}
                  </span>
                  <Link
                    href={`/projects/${proj.id}`}
                    className="flex items-center gap-1 text-primary hover:underline font-medium transition-all"
                  >
                    <span>Open Workspace</span>
                    <ArrowRight size={12} />
                  </Link>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {isModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4">
          <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6 shadow-2xl space-y-4">
            <h2 className="text-sm font-semibold font-heading text-foreground">
              Create New Project
            </h2>
            <form onSubmit={handleCreate} className="space-y-3">
              <div>
                <label className="text-xs text-muted-foreground block mb-1 font-medium">
                  Project Name
                </label>
                <input
                  type="text"
                  required
                  placeholder="e.g. SEO Audit Engine"
                  value={nameInput}
                  onChange={(e) => setNameInput(e.target.value)}
                  disabled={isCreating}
                  className="w-full px-3 py-2 rounded-md bg-background text-xs border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-primary shadow-xs font-sans disabled:opacity-50"
                />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1 font-medium">
                  Description (Optional)
                </label>
                <textarea
                  rows={3}
                  placeholder="Goals and context for this workspace..."
                  value={descInput}
                  onChange={(e) => setDescInput(e.target.value)}
                  disabled={isCreating}
                  className="w-full px-3 py-2 rounded-md bg-background text-xs border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-primary resize-none shadow-xs font-sans disabled:opacity-50"
                />
              </div>
              <div className="flex items-center justify-end gap-2 pt-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setIsModalOpen(false)}
                  disabled={isCreating}
                  className="text-xs cursor-pointer"
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  size="sm"
                  disabled={isCreating || !nameInput.trim()}
                  className="text-xs px-4 bg-primary text-primary-foreground shadow-xs cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {isCreating ? "Creating…" : "Create"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
