// frontend/src/app/stores/_hooks/useSkillActions.ts

"use client";

import { useCallback, useState } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "./useStoreData";
import type {
  SkillInstallResponse,
  SkillSummary,
  SkillToggleResponse,
} from "../_types/store";

/**
 * Arguments required by {@link useSkillActions}.
 *
 * The skills collection and setter are injected from `useStoreData`
 * so the hook only owns action-specific state. `reload` is used after
 * destructive operations (install) so the catalog re-syncs with the
 * backend — optimistic updates are still applied for toggle/delete
 * since those are surgical and fast.
 */
export interface UseSkillActionsArgs {
  skills: SkillSummary[];
  setSkills: React.Dispatch<React.SetStateAction<SkillSummary[]>>;
  reload: () => Promise<void>;
}

/**
 * Return type of {@link useSkillActions}.
 */
export interface UseSkillActionsResult {
  // Toggle lifecycle
  togglingSkillId: string | null;
  handleToggleSkill: (skillId: string) => Promise<void>;

  // Delete lifecycle
  deletingSkillId: string | null;
  handleDeleteSkill: (skillId: string) => Promise<void>;

  // Upload modal
  isUploadModalOpen: boolean;
  selectedUploadFile: File | null;
  isUploading: boolean;
  setSelectedUploadFile: (file: File | null) => void;
  openUploadModal: () => void;
  closeUploadModal: () => void;
  handleUploadSubmit: (e: React.FormEvent) => Promise<void>;

  // Details drawer
  detailsSkill: SkillSummary | null;
  isDetailsDrawerOpen: boolean;
  handleOpenDetails: (skill: SkillSummary) => void;
  handleCloseDetails: () => void;

  // Editor modal
  editorSkill: SkillSummary | null;
  /**
   * Sub-skill ID being edited, or ``null`` when editing the parent
   * router body. The editor modal uses this to switch between the
   * parent /preview + PUT endpoints and the sub-skill equivalents.
   */
  editorSubSkillId: string | null;
  isEditorOpen: boolean;
  handleOpenEditor: (
    skill: SkillSummary,
    subSkillId?: string | null,
  ) => void;
  handleCloseEditor: () => void;
  handleSkillSaved: (updated: SkillSummary) => void;
}

/**
 * Encapsulates every action related to the Skills section:
 *  - Enabling/disabling an installed skill
 *  - Deleting a skill package
 *  - Installing a new skill from a .zip upload
 *  - Opening the details drawer
 *
 * Every mutating action dispatches ``peldrun:skills-changed`` after
 * success so the chat composer and chat-landing re-register their
 * slash commands without a page reload.
 *
 * Failure handling surfaces the exact backend error message when
 * possible; the backend's structured error payload uses
 * ``{ error, code, details }``, which this hook unpacks into a
 * toast-friendly string.
 */
export function useSkillActions({
  skills,
  setSkills,
  reload,
}: UseSkillActionsArgs): UseSkillActionsResult {
  // ── Action state ──────────────────────────────────────────────
  const [togglingSkillId, setTogglingSkillId] = useState<string | null>(null);
  const [deletingSkillId, setDeletingSkillId] = useState<string | null>(null);

  // ── Upload modal state ────────────────────────────────────────
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const [selectedUploadFile, setSelectedUploadFile] = useState<File | null>(
    null
  );
  const [isUploading, setIsUploading] = useState(false);

  // ── Details drawer state ──────────────────────────────────────
  const [detailsSkill, setDetailsSkill] = useState<SkillSummary | null>(null);

  // ── Editor modal state ────────────────────────────────────────
  const [editorSkill, setEditorSkill] = useState<SkillSummary | null>(null);
  const [editorSubSkillId, setEditorSubSkillId] = useState<string | null>(
    null
  );

  // ── Helpers ───────────────────────────────────────────────────
  //
  // The backend returns structured errors as
  // ``{ "error": "...", "code": "...", "details": {...} }``. This
  // helper unwraps that shape into a single human-readable string
  // without losing the code (which we append in parentheses when
  // useful for debugging).
  const _extractError = (payload: any, fallback: string): string => {
    if (!payload) return fallback;
    const detail = payload?.detail ?? payload;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      const err = typeof detail.error === "string" ? detail.error : "";
      const code = typeof detail.code === "string" ? detail.code : "";
      if (err && code) return `${err} [${code}]`;
      if (err) return err;
      if (code) return `${fallback} (${code})`;
    }
    return fallback;
  };

  // ── Toggle enable/disable ─────────────────────────────────────
  const handleToggleSkill = useCallback(
    async (skillId: string) => {
      setTogglingSkillId(skillId);
      try {
        const res = await fetch(
          getApiUrl(`/api/skills/${encodeURIComponent(skillId)}/toggle`),
          { method: "POST" }
        );

        if (res.ok) {
          const data: SkillToggleResponse = await res.json();
          setSkills((prev) =>
            prev.map((s) => (s.id === skillId ? data.skill : s))
          );
          showToast.success(
            "Skill Updated",
            `${data.skill.name || skillId} is now ${data.skill.status}.`
          );
          if (typeof window !== "undefined") {
            window.dispatchEvent(new CustomEvent("peldrun:skills-changed"));
          }
        } else {
          const err = await res.json().catch(() => ({}));
          showToast.error(
            "Toggle Failed",
            _extractError(err, "Could not toggle skill status.")
          );
        }
      } catch (err: any) {
        showToast.error(
          "Network Error",
          err?.message || "Could not toggle skill status."
        );
      } finally {
        setTogglingSkillId(null);
      }
    },
    [setSkills]
  );

  // ── Delete ────────────────────────────────────────────────────
  const handleDeleteSkill = useCallback(
    async (skillId: string) => {
      const skill = skills.find((s) => s.id === skillId);
      const label = skill?.name || skillId;
      if (
        !confirm(
          `Delete skill '${label}'? This removes the package files from disk and cannot be undone.`
        )
      ) {
        return;
      }

      setDeletingSkillId(skillId);
      try {
        const res = await fetch(
          getApiUrl(`/api/skills/${encodeURIComponent(skillId)}`),
          { method: "DELETE" }
        );

        if (res.ok) {
          setSkills((prev) => prev.filter((s) => s.id !== skillId));
          showToast.success(
            "Skill Deleted",
            `'${label}' removed successfully.`
          );
          if (typeof window !== "undefined") {
            window.dispatchEvent(new CustomEvent("peldrun:skills-changed"));
          }
        } else {
          const err = await res.json().catch(() => ({}));
          showToast.error(
            "Delete Failed",
            _extractError(err, "Failed to delete skill.")
          );
        }
      } catch (err: any) {
        showToast.error(
          "Network Error",
          err?.message || "Could not delete skill."
        );
      } finally {
        setDeletingSkillId(null);
      }
    },
    [setSkills, skills]
  );

  // ── Upload modal ──────────────────────────────────────────────
  const openUploadModal = useCallback(() => {
    setSelectedUploadFile(null);
    setIsUploadModalOpen(true);
  }, []);

  const closeUploadModal = useCallback(() => {
    setIsUploadModalOpen(false);
  }, []);

  const handleUploadSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();

      if (!selectedUploadFile) {
        showToast.warning(
          "No File Selected",
          "Please choose a skill .zip package."
        );
        return;
      }

      if (!selectedUploadFile.name.toLowerCase().endsWith(".zip")) {
        showToast.warning(
          "Wrong File Type",
          "Skills must be uploaded as .zip archives."
        );
        return;
      }

      setIsUploading(true);
      try {
        const formData = new FormData();
        formData.append("file", selectedUploadFile);

        const res = await fetch(getApiUrl("/api/skills/install"), {
          method: "POST",
          body: formData,
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          showToast.error(
            "Install Failed",
            _extractError(err, "Server rejected the skill package.")
          );
          return;
        }

        const data: SkillInstallResponse = await res.json();

        showToast.success(
          data.was_upgrade ? "Skill Updated" : "Skill Installed",
          `${data.skill.name} v${data.skill.version} is now active.`
        );

        // Surface warnings (e.g. ID normalization) as info toasts so
        // the operator sees exactly what the backend changed.
        if (Array.isArray(data.warnings)) {
          data.warnings.forEach((w) => {
            if (typeof w === "string" && w.trim()) {
              showToast.info("Install Notice", w);
            }
          });
        }

        setIsUploadModalOpen(false);
        setSelectedUploadFile(null);

        if (typeof window !== "undefined") {
          window.dispatchEvent(new CustomEvent("peldrun:skills-changed"));
        }

        await reload();
      } catch (err: any) {
        showToast.error(
          "Upload Error",
          err?.message || "Failed to upload skill package."
        );
      } finally {
        setIsUploading(false);
      }
    },
    [selectedUploadFile, reload]
  );

  // ── Details drawer ────────────────────────────────────────────
  const handleOpenDetails = useCallback((skill: SkillSummary) => {
    setDetailsSkill(skill);
  }, []);

  const handleCloseDetails = useCallback(() => {
    setDetailsSkill(null);
  }, []);

  // ── Editor modal ──────────────────────────────────────────────
  const handleOpenEditor = useCallback(
    (skill: SkillSummary, subSkillId: string | null = null) => {
      if (!skill.user_editable) {
        showToast.info(
          "Not editable",
          `'${skill.name || skill.id}' is not user-editable.`
        );
        return;
      }
      setEditorSkill(skill);
      setEditorSubSkillId(subSkillId);
    },
    []
  );

  const handleCloseEditor = useCallback(() => {
    setEditorSkill(null);
    setEditorSubSkillId(null);
  }, []);

  const handleSkillSaved = useCallback(
    (updated: SkillSummary) => {
      setSkills((prev) =>
        prev.map((s) => (s.id === updated.id ? updated : s))
      );
    },
    [setSkills]
  );

  return {
    togglingSkillId,
    handleToggleSkill,
    deletingSkillId,
    handleDeleteSkill,
    isUploadModalOpen,
    selectedUploadFile,
    isUploading,
    setSelectedUploadFile,
    openUploadModal,
    closeUploadModal,
    handleUploadSubmit,
    detailsSkill,
    isDetailsDrawerOpen: Boolean(detailsSkill),
    handleOpenDetails,
    handleCloseDetails,
    editorSkill,
    editorSubSkillId,
    isEditorOpen: Boolean(editorSkill),
    handleOpenEditor,
    handleCloseEditor,
    handleSkillSaved,
  };
}
