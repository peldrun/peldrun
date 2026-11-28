// frontend/src/app/projects/[projectId]/prompts/page.tsx

"use client";

/**
 * Project Prompts & Knowledge Page.
 *
 * Standalone page (separate from Settings) for two project-scoped
 * features that affect every reply:
 *
 *   1. Project Instructions — a user-authored text block injected
 *      into the system prompt of every reply inside this project.
 *      Controlled by an on/off switch so the text can be kept
 *      around without being applied.
 *
 *   2. Knowledge Base — reference files stored under
 *      `<workspace>/.peldrun/knowledge/`. The agent sees the file
 *      listing on every agent-mode run and can open any file it
 *      needs with its file tools.
 *
 * Layout mirrors the Settings page (same Section wrapper, same
 * save bar pattern) so the two pages feel like siblings.
 *
 * IMPORTANT: MAX_INSTRUCTIONS_CHARS here mirrors the backend
 * constant in `omweb/projects/project_prompting.py`. Keep both
 * values in sync when changing the limit.
 */

import React, { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { motion, AnimatePresence } from "motion/react";
import {
  ScrollText,
  BookOpen,
  Loader2,
  ArrowLeft,
  Save,
  AlertCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { KnowledgeBasePanel } from "@/components/project/knowledge-base-panel";
import { showToast, ToastContainer } from "@/components/ui/ToastNotification";
import { useProjectStore } from "@/stores/project-store";
import type { Project } from "@/lib/chatsApi";

/* ───────── Animation primitives (same as Settings) ───────── */

const EASE_OUT: [number, number, number, number] = [0.16, 1, 0.3, 1];

const fadeUp = {
  hidden: { opacity: 0, y: 14 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.45, ease: EASE_OUT },
  },
};

const stagger = {
  hidden: {},
  visible: { transition: { staggerChildren: 0.07, delayChildren: 0.05 } },
};

/* ───────── Constants ───────── */

// Keep in sync with MAX_INSTRUCTIONS_CHARS in the backend.
const MAX_INSTRUCTIONS_CHARS = 8000;

/* ───────── Section wrapper (visual parity with Settings) ───────── */

function Section({ children }: { children: React.ReactNode }) {
  return (
    <motion.section
      variants={fadeUp}
      className="bg-card border border-card rounded-md p-6 shadow-md relative overflow-hidden"
    >
      {children}
    </motion.section>
  );
}

/* ───────── Page ───────── */

export default function ProjectPromptsPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.projectId as string;

  const { getProject, upsertProject, loadProjects } = useProjectStore();

  const [project, setProject] = useState<Project | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const [instructions, setInstructions] = useState("");
  const [instructionsEnabled, setInstructionsEnabled] = useState(false);

  const [isSaving, setIsSaving] = useState(false);
  const [hasChanges, setHasChanges] = useState(false);

  /* ── Load ── */
  useEffect(() => {
    const load = async () => {
      if (!projectId) return;
      let current = getProject(projectId);
      if (!current) {
        await loadProjects(true);
        current = getProject(projectId);
      }
      if (current) {
        setProject(current);
        setInstructions(current.project_instructions || "");
        setInstructionsEnabled(
          current.project_instructions_enabled === true,
        );
      }
      setIsLoading(false);
    };
    load();
  }, [projectId, getProject, loadProjects]);

  /* ── Change detection ── */
  useEffect(() => {
    if (!project) return;
    const changed =
      instructions !== (project.project_instructions || "") ||
      instructionsEnabled !==
        (project.project_instructions_enabled === true);
    setHasChanges(changed);
  }, [instructions, instructionsEnabled, project]);

  /* ── Save ── */
  const handleSave = async () => {
    if (!projectId) return;
    setIsSaving(true);
    try {
      const trimmed = instructions.trim();
      const res = await fetch(`/api/projects/${projectId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_instructions: trimmed,
          project_instructions_enabled: instructionsEnabled,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const updated = await res.json();
      const merged: Project = { ...project!, ...updated };
      setProject(merged);
      upsertProject(merged);
      setInstructions(trimmed);
      setHasChanges(false);
      showToast.success(
        "Saved",
        instructionsEnabled
          ? "Project instructions will be applied on the next reply."
          : "Instructions are saved but disabled.",
      );
    } catch (err: any) {
      console.error("[ProjectPrompts] Save failed:", err);
      showToast.error(
        "Save failed",
        err?.message || "Could not save project instructions.",
      );
    } finally {
      setIsSaving(false);
    }
  };

  /* ── Loading ── */
  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 size={26} className="animate-spin text-accent" />
      </div>
    );
  }

  /* ── Not found ── */
  if (!project) {
    return (
      <div className="h-full overflow-y-auto">
        <div className="container mx-auto px-4 py-16 max-w-2xl">
          <div className="bg-card border border-border rounded-2xl p-8 text-center shadow-sm">
            <AlertCircle
              size={28}
              className="text-destructive mx-auto mb-4"
            />
            <h2 className="text-xl font-semibold mb-2">
              Project Not Found
            </h2>
            <Button onClick={() => router.push("/projects")}>
              <ArrowLeft size={16} className="mr-2" />
              Back to Projects
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      className="w-full overflow-y-auto bg-background"
      style={{ height: "calc(100vh - 57px)" }}
    >
      {/* Header */}
      <div className="mb-2 w-full p-3 border border-b-border bg-muted">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-3">
            <ScrollText size={33} className="text-black mt-1.5" />
            <div>
              <h1 className="text-2xl font-bold tracking-tight">
                Prompts & Knowledge
              </h1>
              <p className="text-xs text-muted-foreground mt-0.5">
                Instructions and reference files applied to every reply
                in this project.
              </p>
            </div>
          </div>

          <AnimatePresence mode="wait">
            {hasChanges && (
              <motion.div
                key="unsaved"
                initial={{ opacity: 0, y: -6, scale: 0.96 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: -6, scale: 0.96 }}
                className="flex items-center gap-2"
              >
                <div className="flex items-center gap-1.5 px-2.5 py-1 bg-amber-500/10 border border-amber-500/30 rounded-full text-xs text-amber-600 dark:text-amber-400">
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                  Unsaved changes
                </div>
                <Button
                  onClick={handleSave}
                  disabled={isSaving}
                  className="gap-2"
                >
                  {isSaving ? (
                    <Loader2 size={15} className="animate-spin" />
                  ) : (
                    <Save size={15} />
                  )}
                  Save
                </Button>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <div className="mt-4">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => router.push(`/projects/${projectId}`)}
            className="gap-1.5 -ml-2 text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft size={15} />
            Back to dashboard
          </Button>
        </div>
      </div>

      <div className="container mx-auto px-4 py-8">
        <motion.div
          variants={stagger}
          initial="hidden"
          animate="visible"
          className="space-y-6"
        >
          {/* ── Project Instructions ── */}
          <Section>
            <div className="flex items-center justify-between mb-5">
              <div className="flex items-center gap-2">
                <ScrollText size={16} className="text-br" />
                <h2 className="text-sm font-semibold uppercase tracking-wider text-black">
                  Project Instructions
                </h2>
              </div>
              <Switch
                checked={instructionsEnabled}
                onCheckedChange={setInstructionsEnabled}
                label="Enable project instructions"
              />
            </div>

            <textarea
              value={instructions}
              onChange={(e) =>
                setInstructions(
                  e.target.value.slice(0, MAX_INSTRUCTIONS_CHARS),
                )
              }
              rows={10}
              className="w-full px-3.5 py-2.5 bg-muted border border-muted rounded-md text-sm
                         focus:outline-none focus:ring-1 focus:ring-card focus:border-rb/10
                         transition-shadow resize-y placeholder:text-muted-foreground/60"
              placeholder="e.g. Always answer in Arabic. Follow our naming conventions. Ask before deleting files."
            />

            <div className="flex items-center justify-between mt-1.5 text-xs text-muted-foreground">
              <span>
                {!instructionsEnabled
                  ? "Off: your text is saved but not used."
                  : instructions.trim()
                    ? "On: added to every reply in this project."
                    : "On, but empty: nothing is added."}
              </span>
              <span>
                {instructions.length} / {MAX_INSTRUCTIONS_CHARS}
              </span>
            </div>
          </Section>

          {/* ── Knowledge Base ── */}
          <Section>
            <div className="flex items-center gap-2 mb-2">
              <BookOpen size={16} className="text-br" />
              <h2 className="text-sm font-semibold uppercase tracking-wider text-black">
                Knowledge Base
              </h2>
            </div>
            <p className="text-xs text-muted-foreground mb-4">
              Files the agent can read when you ask about them, or when it
              needs background on this project. Stored in{" "}
              <code className="px-1 py-0.5 bg-muted rounded text-[11px]">
                .peldrun/knowledge
              </code>{" "}
              inside the project workspace. Changes here apply on the next
              agent run — no restart needed.
            </p>
            <KnowledgeBasePanel projectId={projectId} />
          </Section>

          {/* Bottom Save bar (mirrors Settings page) */}
          <AnimatePresence>
            {hasChanges && (
              <motion.div
                initial={{ opacity: 0, y: 8, height: 0 }}
                animate={{ opacity: 1, y: 0, height: "auto" }}
                exit={{ opacity: 0, y: 8, height: 0 }}
                transition={{ duration: 0.25, ease: EASE_OUT }}
                className="flex justify-end overflow-hidden"
              >
                <Button
                  onClick={handleSave}
                  disabled={isSaving}
                  className="gap-2"
                >
                  {isSaving ? (
                    <Loader2 size={15} className="animate-spin" />
                  ) : (
                    <Save size={15} />
                  )}
                  Save Changes
                </Button>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>

        <ToastContainer />
      </div>
    </div>
  );
}
