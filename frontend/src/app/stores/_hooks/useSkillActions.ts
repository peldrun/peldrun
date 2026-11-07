// frontend/src/app/stores/_hooks/useSkillActions.ts

"use client";

import { useCallback, useState } from "react";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "./useStoreData";
import type {
  CatalogSkillInstallResult,
  SkillCatalogItem,
  SkillCatalogListResponse,
  SkillCatalogRefreshResponse,
  SkillInstallResponse,
  SkillSummary,
  SkillToggleResponse,
} from "../_types/store";
import {
  formatSkillCatalogError,
  installSkillFromCatalog as apiInstallSkillFromCatalog,
  refreshSkillCatalog as apiRefreshSkillCatalog,
} from "@/lib/skillCatalogApi";

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

  // ── Catalog ────────────────────────────────────────────────────
  /** Loaded catalog items, or empty until first load. */
  catalogItems: SkillCatalogItem[];
  isCatalogLoading: boolean;
  isCatalogRefreshing: boolean;
  /** Last catalog load / refresh error, or ``null``. */
  catalogError: string | null;
  /**
   * False when the backend reports ``PELDRUN_SKILL_CATALOG_ENABLED=false``.
   * The SkillsSection uses this to hide the Catalog tab entirely.
   * Defaults to ``true`` so the tab is visible before the first load
   * completes (avoids a flicker on fast networks).
   */
  catalogEnabled: boolean;
  /** Item ID currently being installed, or ``null``. */
  installingCatalogId: string | null;
  handleLoadCatalog: () => Promise<void>;
  handleRefreshCatalog: () => Promise<void>;

  // ── Install modal ─────────────────────────────────────────────
  /**
   * Catalog item to install (drives the install modal), or ``null``.
   * When non-null, ``CatalogSkillInstallModal`` renders.
   */
  catalogInstallItem: SkillCatalogItem | null;
  /**
   * Terminal install result, or ``null`` while running. Mirrors the
   * discriminated union consumed by ``CatalogSkillInstallModal``.
   */
  catalogInstallResult: CatalogSkillInstallResult | null;
  /** Opens the install modal and kicks off the request. */
  handleInstallFromCatalog: (item: SkillCatalogItem) => Promise<void>;
  /** Closes the install modal and clears its state. */
  handleCloseCatalogInstall: () => void;

  // ── Details modal ─────────────────────────────────────────────
  catalogDetailsItem: SkillCatalogItem | null;
  isCatalogDetailsOpen: boolean;
  handleOpenCatalogDetails: (item: SkillCatalogItem) => void;
  handleCloseCatalogDetails: () => void;
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

  // ── Catalog state ─────────────────────────────────────────────
  const [catalogItems, setCatalogItems] = useState<SkillCatalogItem[]>([]);
  const [isCatalogLoading, setIsCatalogLoading] = useState(false);
  const [isCatalogRefreshing, setIsCatalogRefreshing] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogEnabled, setCatalogEnabled] = useState<boolean>(true);

  // ── Catalog install modal state ───────────────────────────────
  const [installingCatalogId, setInstallingCatalogId] = useState<
    string | null
  >(null);
  const [catalogInstallItem, setCatalogInstallItem] =
    useState<SkillCatalogItem | null>(null);
  const [catalogInstallResult, setCatalogInstallResult] =
    useState<CatalogSkillInstallResult | null>(null);

  // ── Catalog details modal state ───────────────────────────────
  const [catalogDetailsItem, setCatalogDetailsItem] =
    useState<SkillCatalogItem | null>(null);

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

  // ── Catalog: load ─────────────────────────────────────────────
  //
  // Uses a direct fetch (rather than ``listSkillCatalog``) because we
  // need to read ``status_info.enabled`` -- the thin API client helper
  // intentionally drops it for cleaner call sites. One request gives
  // us both the items and the kill-switch flag.
  const handleLoadCatalog = useCallback(async () => {
    setIsCatalogLoading(true);
    setCatalogError(null);
    try {
      const res = await fetch(getApiUrl("/api/skills/catalog"));
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        const detail = errBody?.detail;
        const msg =
          (typeof detail === "string" && detail) ||
          (detail && typeof detail === "object" && detail.error) ||
          `HTTP ${res.status}`;
        throw new Error(String(msg));
      }
      const data: SkillCatalogListResponse = await res.json();
      setCatalogItems(Array.isArray(data.items) ? data.items : []);
      // Kill switch: default to true when the backend omits the flag
      // (older deployments or mocked responses).
      setCatalogEnabled(data.status_info?.enabled ?? true);
    } catch (err) {
      setCatalogError(formatSkillCatalogError(err));
    } finally {
      setIsCatalogLoading(false);
    }
  }, []);

  // ── Catalog: refresh (force) ──────────────────────────────────
  //
  // ``refreshSkillCatalog`` does NOT throw on a ``status: "error"``
  // response -- that is a state (remote fetch failed but builtin
  // catalog is still available) which we surface as a warning toast.
  // It DOES throw on network failures and non-2xx responses.
  const handleRefreshCatalog = useCallback(async () => {
    setIsCatalogRefreshing(true);
    setCatalogError(null);
    try {
      const resp: SkillCatalogRefreshResponse = await apiRefreshSkillCatalog();
      if (resp.status === "error") {
        setCatalogError(resp.message);
        showToast.warning("Catalog Refresh Failed", resp.message);
      } else {
        showToast.success(
          "Catalog Refreshed",
          resp.message || `Catalog reloaded (${resp.total} items).`
        );
      }
      // Re-fetch the item list so the UI reflects any change the
      // refresh produced (new items, updated metadata).
      await handleLoadCatalog();
    } catch (err) {
      const msg = formatSkillCatalogError(err);
      setCatalogError(msg);
      showToast.error("Refresh Failed", msg);
    } finally {
      setIsCatalogRefreshing(false);
    }
  }, [handleLoadCatalog]);

  // ── Catalog: install ──────────────────────────────────────────
  //
  // Synchronous backend operation (2-5s typical). The install modal
  // shows a simulated progress UI while the fetch is in-flight, then
  // swaps to a real success/error banner using the response payload
  // (or the thrown SkillCatalogApiError).
  const handleInstallFromCatalog = useCallback(
    async (item: SkillCatalogItem) => {
      // Prevent concurrent installs from multiple card clicks.
      if (installingCatalogId !== null) return;

      // Open the modal in "running" state BEFORE the network call so
      // the user sees immediate feedback.
      setCatalogInstallItem(item);
      setCatalogInstallResult(null);
      setInstallingCatalogId(item.id);

      try {
        const result = await apiInstallSkillFromCatalog(item.id);
        setCatalogInstallResult({
          success: true,
          skillId: result.skill.id,
          version: result.skill.version,
          name: result.skill.name,
        });

        showToast.success(
          result.was_upgrade ? "Skill Updated" : "Skill Installed",
          `${result.skill.name} v${result.skill.version} is now active.`
        );
        if (Array.isArray(result.warnings)) {
          for (const w of result.warnings) {
            if (typeof w === "string" && w.trim()) {
              showToast.info("Install Notice", w);
            }
          }
        }

        // Refetch the installed skills so the Installed tab shows the
        // new skill (and the card's Installed badge updates).
        await reload();
        if (typeof window !== "undefined") {
          window.dispatchEvent(new CustomEvent("peldrun:skills-changed"));
        }
      } catch (err) {
        // Format the error into the discriminated-union failure shape.
        // Keep the same error string the toast shows so the modal's
        // failure banner and the toast agree.
        const msg = formatSkillCatalogError(err);
        const code =
          err && typeof err === "object" && "code" in err
            ? String((err as { code?: unknown }).code)
            : undefined;
        setCatalogInstallResult({ success: false, error: msg, code });
        showToast.error("Install Failed", msg);
      } finally {
        setInstallingCatalogId(null);
      }
    },
    [installingCatalogId, reload]
  );

  // ── Catalog: install modal close ──────────────────────────────
  //
  // Guarded against a close attempt while the fetch is still running
  // -- the modal itself disables the close button, but defensive
  // double-checking keeps the state consistent even if some future
  // caller bypasses the modal.
  const handleCloseCatalogInstall = useCallback(() => {
    if (installingCatalogId !== null) return;
    setCatalogInstallItem(null);
    setCatalogInstallResult(null);
  }, [installingCatalogId]);

  // ── Catalog: details modal open / close ───────────────────────
  const handleOpenCatalogDetails = useCallback((item: SkillCatalogItem) => {
    setCatalogDetailsItem(item);
  }, []);

  const handleCloseCatalogDetails = useCallback(() => {
    setCatalogDetailsItem(null);
  }, []);

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

    // Catalog
    catalogItems,
    isCatalogLoading,
    isCatalogRefreshing,
    catalogError,
    catalogEnabled,
    installingCatalogId,
    handleLoadCatalog,
    handleRefreshCatalog,

    // Install modal
    catalogInstallItem,
    catalogInstallResult,
    handleInstallFromCatalog,
    handleCloseCatalogInstall,

    // Details modal
    catalogDetailsItem,
    isCatalogDetailsOpen: catalogDetailsItem !== null,
    handleOpenCatalogDetails,
    handleCloseCatalogDetails,
  };
}
