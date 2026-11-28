// frontend/src/app/projects/[projectId]/page.tsx
"use client";

import React, { useEffect, useState } from "react";
import { useParams, useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import { motion, AnimatePresence } from "motion/react";
import {
  Calendar,
  MessageSquare,
  CheckSquare,
  Folder,
  Settings,
  GitBranch,
  FileText,
  Loader2,
  ArrowLeft,
  ArrowRight,
  Edit,
  Trash2,
  PanelRightClose,
  PanelRightOpen,
  FolderTree,
  ChevronRight,
  ScrollText,
} from "lucide-react";
import { ProjectSourceControlPanel } from "@/features/source-control/components/project-source-control-panel";

import { Button } from "@/components/ui/button";
import { useProjectStore } from "@/stores/project-store";
import { ProjectWorkspacePanel } from "@/components/workspace/project-workspace-panel";
import { Project, WorkspaceType } from "@/features/workspace/types";

/* ───────── Animation primitives ───────── */

const EASE_OUT: [number, number, number, number] = [0.16, 1, 0.3, 1];

const PAGE_LABELS: Record<string, string> = {
  chat: "Conversations",
  tasks: "Tasks",
  notebook: "Notebook",
  "source-control": "Source Control",
  migration: "Migration",
  settings: "Settings",
};

const fadeUp = {
  hidden: { opacity: 0, y: 14 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.45, ease: EASE_OUT } },
};

const fadeIn = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: { duration: 0.35, ease: EASE_OUT } },
};

const stagger = {
  hidden: {},
  visible: { transition: { staggerChildren: 0.06, delayChildren: 0.05 } },
};

/* ───────── Page ───────── */

export default function ProjectDashboardPage() {
  const params = useParams();
  const router = useRouter();
  const pathname = usePathname();
  const projectId = params.projectId as string;

  const { getProject, loadProjects } = useProjectStore();

  const mapToFullProject = (storedProject: any): Project => ({
    ...storedProject,
    workspace_path: storedProject.workspace_path || "",
    workspace_type:
      (storedProject.workspace_type as WorkspaceType) || "internal",
    updated_at: storedProject.updated_at || storedProject.created_at,
  });

  const [project, setProject] = useState<Project | null>(() => {
    if (typeof window === "undefined") return null;
    const stored = useProjectStore.getState().getProject(projectId);
    return stored ? mapToFullProject(stored) : null;
  });
  const [hasResolved, setHasResolved] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return useProjectStore.getState().getProject(projectId) != null;
  });
  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");

  // Initialize from localStorage to persist state across F5, default to null (closed)
  const [rightPanel, setRightPanel] = useState<"workspace" | "source-control" | null>(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("project_right_panel_state");
      if (saved === "workspace" || saved === "source-control") {
        return saved;
      }
    }
    return null; // Default to closed
  });

  // Remember the last opened panel to restore it correctly when re-opening
  const [lastOpenedPanel, setLastOpenedPanel] = useState<"workspace" | "source-control">("workspace");

  // Helper to update state and persist to localStorage
  const handleSetRightPanel = (panel: "workspace" | "source-control" | null) => {
    if (panel === null) {
      // Save the current panel before closing so we can restore it later
      if (rightPanel) {
        setLastOpenedPanel(rightPanel);
      }
      setRightPanel(null);
      if (typeof window !== "undefined") {
        localStorage.removeItem("project_right_panel_state");
      }
    } else {
      setRightPanel(panel);
      setLastOpenedPanel(panel);
      if (typeof window !== "undefined") {
        localStorage.setItem("project_right_panel_state", panel);
      }
    }
  };

  // ── Live stats: sessions / tasks / files counts ──
  // `null` means "loading" and renders as "…" in the UI,
  // distinguishing "not yet fetched" from "genuinely zero".
  const [sessionsCount, setSessionsCount] = useState<number | null>(null);
  const [tasksCount, setTasksCount] = useState<number | null>(null);
  const [filesCount, setFilesCount] = useState<number | null>(null);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;

    const fetchStats = async () => {
      // ── Sessions ──
      // Primary: paginated chat list — `total` reflects the full count
      // even when `limit=1` is used. Fallback: project detail endpoint,
      // whose `chats` array length is the count.
      // Chats live under /api/chats (chats.router is mounted with
      // prefix="/api/chats" in main.py). The paginated list endpoint
      // returns a `total` field; the project detail endpoint returns
      // a `chats` array whose length is the count.
      let sessions: number | null = null;
      try {
        const res = await fetch(
          `/api/chats/?project_id=${encodeURIComponent(projectId)}&limit=1`,
          { credentials: "include", headers: { Accept: "application/json" } }
        );
        if (res.ok) {
          const data = await res.json();
          if (typeof data?.total === "number") sessions = data.total;
        }
        if (sessions === null) {
          const res2 = await fetch(
            `/api/chats/projects/${projectId}`,
            { credentials: "include", headers: { Accept: "application/json" } }
          );
          if (res2.ok) {
            const data2 = await res2.json();
            if (Array.isArray(data2?.chats)) sessions = data2.chats.length;
          }
        }
      } catch {
        /* non-fatal */
      }
      if (!cancelled) setSessionsCount(sessions ?? 0);

      // ── Tasks ──
      let tasks: number | null = null;
      try {
        const res = await fetch(`/api/projects/${projectId}/tasks`, {
          credentials: "include",
          headers: { Accept: "application/json" },
        });
        if (res.ok) {
          const data = await res.json();
          if (Array.isArray(data)) tasks = data.length;
        }
      } catch {
        /* non-fatal */
      }
      if (!cancelled) setTasksCount(tasks ?? 0);

      // ── Files ──
      let files: number | null = null;
      try {
        const res = await fetch(
          `/api/projects/${projectId}/workspace/files`,
          { credentials: "include", headers: { Accept: "application/json" } }
        );
        if (res.ok) {
          const data = await res.json();
          if (Array.isArray(data)) files = data.length;
        }
      } catch {
        /* non-fatal */
      }
      if (!cancelled) setFilesCount(files ?? 0);
    };

    fetchStats();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  useEffect(() => {
    if (!projectId || hasResolved) return;

    let cancelled = false;
    (async () => {
      try {
        await loadProjects(true);
      } catch (error) {
        console.error("[ProjectDashboard] Failed to load projects:", error);
      }
      if (cancelled) return;

      const currentProject = getProject(projectId);
      setProject(currentProject ? mapToFullProject(currentProject) : null);
      setHasResolved(true);
    })();

    return () => {
      cancelled = true;
    };
  }, [projectId, getProject, loadProjects, hasResolved]);

  /* ── Loading ── */
  if (!hasResolved && !project) {
    return (
      <div className="flex h-screen items-center justify-center">
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="flex flex-col items-center gap-4"
        >
          <div className="relative">
            <motion.div
              className="absolute inset-0 rounded-full bg-accent/25 blur-xl"
              animate={{ scale: [1, 1.5, 1], opacity: [0.4, 0.75, 0.4] }}
              transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
            />
            <Loader2 size={30} className="animate-spin text-accent relative" />
          </div>
          <motion.p
            className="text-sm text-muted-foreground"
            animate={{ opacity: [0.5, 1, 0.5] }}
            transition={{ duration: 1.6, repeat: Infinity }}
          >
            Loading project…
          </motion.p>
        </motion.div>
      </div>
    );
  }

  /* ── Not found ── */
  if (!project) {
    return (
      <motion.div
        className="container mx-auto px-4 py-16 max-w-2xl"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: EASE_OUT }}
      >
        <div className="bg-card border border-border rounded-xl p-8 text-center shadow-sm">
          <motion.div
            initial={{ scale: 0.8, rotate: -6 }}
            animate={{ scale: 1, rotate: 0 }}
            transition={{ type: "spring", stiffness: 220, damping: 18 }}
            className="inline-flex p-4 bg-muted rounded-full mb-4"
          >
            <Folder size={32} className="text-muted-foreground" />
          </motion.div>
          <h2 className="text-xl font-semibold mb-2">Project Not Found</h2>
          <p className="text-muted-foreground mb-6">
            The project you're looking for doesn't exist or has been deleted.
          </p>
          <Button onClick={() => router.push("/projects")}>
            <ArrowLeft size={16} className="mr-2" />
            Back to Projects
          </Button>
        </div>
      </motion.div>
    );
  }

  /* ── Handlers ── */
  const handleEdit = () => {
    setEditName(project.name);
    setEditDescription(project.description || "");
    setIsEditing(true);
  };

  const handleSave = async () => {
    if (!editName.trim()) {
      alert("Project name cannot be empty.");
      return;
    }
    try {
      const res = await fetch(`/api/projects/${projectId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: editName.trim(),
          description: editDescription.trim(),
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const updated = await res.json();
      // Merge the authoritative response into both local state and
      // the store so the sidebar reflects the rename immediately.
      const merged: Project = { ...project!, ...updated };
      setProject(merged);
      useProjectStore.getState().upsertProject(merged);
      setIsEditing(false);
    } catch (err: any) {
      console.error("[ProjectDashboard] Save failed:", err);
      alert(err?.message || "Failed to save project. Please try again.");
    }
  };

  const handleDelete = async () => {
    if (
      !confirm(
        `Are you sure you want to delete "${project.name}"? This action cannot be undone.`
      )
    )
      return;
    try {
      // DELETE lives on chats.router at /api/chats/projects/{id}.
      // projects.py has no DELETE handler for the root project path,
      // so calling /api/projects/{id} would return 405.
      const res = await fetch(`/api/chats/projects/${projectId}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        // 409: an active run is still writing to the workspace.
        // Surface the backend's detailed message instead of a
        // generic error so the user knows what to do.
        if (res.status === 409) {
          const body = await res.json().catch(() => ({}));
          alert(
            body.detail ||
              "Cannot delete: a run is still active. Stop it first."
          );
          return;
        }
        throw new Error(`HTTP ${res.status}`);
      }
      useProjectStore.getState().removeProject(projectId);
      router.push("/projects");
    } catch (error: any) {
      console.error("[ProjectDashboard] Delete failed:", error);
      alert(error?.message || "Failed to delete project. Please try again.");
    }
  };

  const formatDate = (dateString: string) =>
    new Date(dateString).toLocaleDateString("en-US", {
      year: "numeric",
      month: "long",
      day: "numeric",
    });

  const breadcrumbs = React.useMemo(() => {
    const items: Array<{ label: string; href: string | null }> = [
      { label: "Projects", href: "/projects" },
    ];
    if (project) {
      items.push({ label: project.name, href: `/projects/${projectId}` });
    }
    const rest = pathname.split("/").filter(Boolean).slice(2);
    if (rest.length > 0) {
      const currentPage = rest[0];
      const label = PAGE_LABELS[currentPage] || currentPage;
      items.push({ label, href: null });
    }
    return items;
  }, [pathname, project, projectId]);

  /* ── Config ── */
  const stats = [
    {
      icon: MessageSquare,
      label: "Sessions",
      value: sessionsCount === null ? "…" : String(sessionsCount),
      hint: "Active conversations",
      color: "text-blue-500",
    },
    {
      icon: CheckSquare,
      label: "Tasks",
      value: tasksCount === null ? "…" : String(tasksCount),
      hint: "Pending tasks",
      color: "text-emerald-500",
    },
    {
      icon: Folder,
      label: "Files",
      value: filesCount === null ? "…" : String(filesCount),
      hint: "Workspace files",
      color: "text-amber-500",
    },
    {
      icon: Calendar,
      label: "Created",
      value: formatDate(project.created_at),
      hint: "Project creation date",
      color: "text-violet-500",
      isDate: true,
    },
  ];

  const actions = [
    {
      icon: MessageSquare,
      title: "Conversations & Chats",
      desc: "Begin a new conversation",
      href: `/projects/${projectId}/chat`,
      color: "text-blue-500 group-hover:bg-blue-500/20",
    },
    {
      icon: CheckSquare,
      title: "Manage Tasks",
      desc: "View and organize tasks",
      href: `/projects/${projectId}/tasks`,
      color:
        "text-emerald-500 group-hover:bg-emerald-500/20",
    },
    {
      icon: FileText,
      title: "Notebook",
      desc: "Project notes and docs",
      href: `/projects/${projectId}/notebook`,
      color:
        "text-violet-500 group-hover:bg-violet-500/20",
    },
    {
      icon: GitBranch,
      title: "Source Control",
      desc: "Git repositories",
      href: `/projects/${projectId}/source-control`,
      color:
        "text-amber-500 group-hover:bg-amber-500/20",
    },
    // NEW: Prompts & Knowledge is a sibling of Settings, not a
    // section inside it. Kept in Quick Actions so it is reachable
    // from the dashboard without opening the settings page first.
    {
      icon: ScrollText,
      title: "Prompts & Knowledge",
      desc: "Instructions and reference files",
      href: `/projects/${projectId}/prompts`,
      color:
        "text-rose-500 group-hover:bg-rose-500/20",
    },
  ];

  const infoRows = [
    {
      icon: Folder,
      label: "Workspace Path",
      value: (
        <span className="font-mono text-xs break-all">
          {project.workspace_path || "Not configured"}
        </span>
      ),
    },
    {
      icon: Settings,
      label: "Workspace Type",
      value: (
        <span className="capitalize">
          {project.workspace_type || "internal"}
        </span>
      ),
    },
    {
      icon: Calendar,
      label: "Last Updated",
      value: project.updated_at ? formatDate(project.updated_at) : "Never",
    },
  ];

  return (
    <div className="flex h-screen">
      {/* ── Main content ── */}
      <motion.div
        className="flex-1 flex flex-col overflow-auto min-w-0"
        variants={stagger}
        initial="hidden"
        animate="visible"
      >
        {/* Breadcrumbs */}
        <motion.div
          className="border-b border-border bg-custom px-6 py-3"
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3, ease: EASE_OUT }}
        >
          <nav className="flex items-center gap-1.5 text-sm">
            {breadcrumbs.map((item, index) => (
              <React.Fragment key={index}>
                {index > 0 && (
                  <ChevronRight
                    size={13}
                    className="text-muted-foreground/60 shrink-0"
                  />
                )}
                <motion.span
                  initial={{ opacity: 0, x: -4 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{
                    delay: index * 0.05,
                    duration: 0.3,
                    ease: EASE_OUT,
                  }}
                >
                  {item.href ? (
                    <Link
                      href={item.href}
                      className="text-muted-foreground hover:text-foreground transition-colors"
                    >
                      {item.label}
                    </Link>
                  ) : (
                    <span className="text-foreground font-medium">
                      {item.label}
                    </span>
                  )}
                </motion.span>
              </React.Fragment>
            ))}
          </nav>
        </motion.div>

        {/* Dashboard Content Container */}
        <div className="flex flex-col w-full max-w-7xl m-auto">
          {/* Header */}
          <motion.div
            variants={fadeIn}
            className="border-b border-card bg-background p-6"
          >
            <div className="flex items-start justify-between mb-6 gap-4">
            <div className="flex-1 min-w-0">
              <AnimatePresence mode="wait" initial={false}>
                {isEditing ? (
                  <motion.div
                    key="edit"
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -6 }}
                    transition={{ duration: 0.2 }}
                    className="space-y-3"
                  >
                    <input
                      type="text"
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      className="w-full text-2xl font-bold bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-ring/40 transition-shadow"
                      autoFocus
                    />
                    <textarea
                      value={editDescription}
                      onChange={(e) => setEditDescription(e.target.value)}
                      placeholder="Project description…"
                      className="w-full bg-background border border-border rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring/40 transition-shadow"
                      rows={3}
                    />
                    <div className="flex gap-2">
                      <Button onClick={handleSave} size="sm">
                        Save
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setIsEditing(false)}
                      >
                        Cancel
                      </Button>
                    </div>
                  </motion.div>
                ) : (
                  <motion.div
                    key="view"
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -6 }}
                    transition={{ duration: 0.2 }}
                  >
                    <h1 className="text-2xl font-bold mb-1 truncate">
                      {project.name}
                    </h1>
                    <p className="text-muted-foreground line-clamp-2">
                      {project.description || "No description"}
                    </p>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>

            {!isEditing && (
              <motion.div
                initial={{ opacity: 0, x: 10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.15, duration: 0.4, ease: EASE_OUT }}
                className="flex items-center gap-1.5 shrink-0"
              >
                <Button variant="ghost" size="sm" onClick={handleEdit}>
                  <Edit size={15} className="mr-1.5" />
                  Edit
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-destructive hover:text-destructive"
                  onClick={handleDelete}
                >
                  <Trash2 size={15} className="mr-1.5" />
                  Delete
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    router.push(`/projects/${projectId}/settings`)
                  }
                >
                  <Settings size={15} className="mr-1.5" />
                  Settings
                </Button>

                <AnimatePresence>
                  {rightPanel === null && (
                    <motion.div
                      initial={{ opacity: 0, scale: 0.92 }}
                      animate={{ opacity: 1, scale: 1 }}
                      exit={{ opacity: 0, scale: 0.92 }}
                      transition={{
                        type: "spring",
                        stiffness: 320,
                        damping: 24,
                      }}
                    >
                      <Button
                        variant="nonestyle"
                        size="sm"
                        onClick={() => handleSetRightPanel(lastOpenedPanel)}
                        className="gap-1.5 ml-2"
                        title="Open Last Used Panel"
                      >
                        <PanelRightOpen size={15} />

                      </Button>
                    </motion.div>
                  )}
                </AnimatePresence>
              </motion.div>
            )}
          </div>

          {/* Stats */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {stats.map((stat) => {
              const Icon = stat.icon;
              return (
                <motion.div
                  key={stat.label}
                  variants={fadeUp}
                  whileHover={{
                    y: -2,
                    transition: { duration: 0.18, ease: "easeOut" },
                  }}
                  className="group bg-custom border border-custom rounded-md p-4 transition-colors hover:border-rb/40 shadow-sm"
                >
                  <div className="flex items-center gap-2 mb-3">
                    <div className={`p-1.5 rounded-sm ${stat.color}`}>
                      <Icon size={14} />
                    </div>
                    <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                      {stat.label}
                    </span>
                  </div>
                  <div
                    className={
                      stat.isDate
                        ? "text-sm font-semibold"
                        : "text-2xl font-bold tracking-tight"
                    }
                  >
                    {stat.value}
                  </div>
                  <div className="text-xs text-muted-foreground mt-1">
                    {stat.hint}
                  </div>
                </motion.div>
              );
            })}
          </div>
        </motion.div>

        {/* Quick actions */}
        <motion.div
          variants={fadeUp}
          className="p-6 border-b border-card bg-muted rounded-sm"
        >
          <h2 className="text-xs font-semibold mb-3 text-muted-foreground uppercase tracking-wider">
            Quick Actions
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {actions.map((action) => {
              const Icon = action.icon;
              return (
                <motion.button
                  key={action.title}
                  variants={fadeUp}
                  whileHover={{
                    y: -3,
                    transition: { duration: 0.18, ease: "easeOut" },
                  }}
                  whileTap={{ scale: 0.98 }}
                  onClick={() => router.push(action.href)}
                  className="group relative bg-card border border-card rounded-md p-4 text-left hover:border-rby/40 shadow-sm hover:shadow-md transition-all cursor-pointer"
                >
                  <div className="flex items-start justify-between mb-3">
                    <div
                      className={`p-2 rounded-md transition-colors ${action.color}`}
                    >
                      <Icon size={16} />
                    </div>
                    <ArrowRight
                      size={15}
                      className="text-muted-foreground opacity-0 -translate-x-1 group-hover:opacity-100 group-hover:translate-x-0 transition-all duration-200"
                    />
                  </div>
                  <div className="font-semibold text-sm mb-0.5">
                    {action.title}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {action.desc}
                  </div>
                </motion.button>
              );
            })}
          </div>
        </motion.div>

        {/* Info */}
        <motion.div variants={fadeUp} className="p-6">
          <h2 className="text-xs font-semibold mb-3 text-black uppercase tracking-wider">
            Project Information
          </h2>
          <div className="bg-card border border-card rounded-md p-5 space-y-4 shadow-sm">
            {infoRows.map((row, i) => {
              const Icon = row.icon;
              return (
                <motion.div
                  key={row.label}
                  initial={{ opacity: 0, x: -6 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{
                    delay: 0.25 + i * 0.06,
                    duration: 0.35,
                    ease: EASE_OUT,
                  }}
                  className="flex items-center gap-3"
                >
                  <div className="mt-0.5 shrink-0">
                    <Icon size={13} className="text-muted-foreground" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium">{row.label}</div>
                    <div className="text-xs text-muted-foreground mt-1 bg-muted/60 p-1">
                      {row.value}
                    </div>
                  </div>
                </motion.div>
              );
            })}
          </div>
        </motion.div>
        </div>
      </motion.div>

      {/* ── Right panel ── */}
      <AnimatePresence initial={false}>
        {rightPanel !== null && (
          <motion.aside
            key="right-panel"
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: "40%", opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ duration: 0.32, ease: EASE_OUT }}
            className="hidden lg:flex flex-col shrink-0 border-l border-border bg-background overflow-auto"
          >
            {/* Toggle header */}
            <div className="h-12 border-b border-border bg-custom px-3 flex items-center justify-between shrink-0">
              <div className="relative flex items-center bg-black/10 rounded-lg border border-border p-0.5">
                {(
                  [
                    { key: "workspace", label: "Workspace", Icon: FolderTree },
                    {
                      key: "source-control",
                      label: "Source Control",
                      Icon: GitBranch,
                    },
                  ] as const
                ).map(({ key, label, Icon }) => {
                  const isActive = rightPanel === key;
                  return (
                    <button
                      key={key}
                      type="button"
                      onClick={() => handleSetRightPanel(key)}
                      className={`relative px-2.5 py-1.5 rounded-md transition-colors flex items-center
                        gap-1.5 text-xs cursor-pointer whitespace-nowrap ${
                        isActive
                          ? "text-foreground font-medium"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {isActive && (
                        <motion.div
                          layoutId="panel-tab-pill"
                          className="absolute inset-0 bg-card rounded-md shadow-sm"
                          transition={{
                            type: "spring",
                            stiffness: 400,
                            damping: 30,
                          }}
                        />
                      )}
                      <span className="relative flex items-center gap-1.5">
                        <Icon size={13} />
                        <span>{label}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
              <motion.button
                type="button"
                onClick={() => handleSetRightPanel(null)}
                whileHover={{ scale: 1.06 }}
                whileTap={{ scale: 0.94 }}
                className="p-1.5 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-colors cursor-pointer"
                title="Close Right Panel"
              >
                <PanelRightClose size={16} />
              </motion.button>
            </div>

            {/* Panel content */}
            <div className="flex-1 min-h-0 overflow-hidden relative">
              <AnimatePresence mode="wait" initial={false}>
                <motion.div
                  key={rightPanel}
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -6 }}
                  transition={{ duration: 0.18, ease: "easeOut" }}
                  className="h-full"
                >
                  {rightPanel === "workspace" && (
                    <ProjectWorkspacePanel projectId={projectId} />
                  )}
                  {rightPanel === "source-control" && (
                    <ProjectSourceControlPanel projectId={projectId} />
                  )}
                </motion.div>
              </AnimatePresence>
            </div>
          </motion.aside>
        )}
      </AnimatePresence>
    </div>
  );
}
