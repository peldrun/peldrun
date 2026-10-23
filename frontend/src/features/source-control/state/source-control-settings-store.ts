// frontend/src/features/source-control/state/source-control-settings-store.ts

import { create } from "zustand";

/**
 * User-configurable settings for the Source Control subsystem.
 *
 * Persistence: `backend/data/source_control/settings.json` via
 * `GET/PUT /api/source-control/settings`.
 *
 * SAVE BEHAVIOR — debounced, with user feedback:
 *
 *   Every setter schedules a PUT instead of firing one immediately.
 *   The scheduler coalesces consecutive changes into a single
 *   request 300 ms after the last change. This has two benefits:
 *
 *     - Changing 3 fields in a row makes 1 PUT, not 3 (the backend
 *       logs one audit event, not three).
 *     - The toast stack stays readable: the user sees one "Saved"
 *       confirmation per editing session, not one per keystroke.
 *
 *   The `saving` flag is set to true the moment a change is queued
 *   (not after the debounce fires), so the UI can show "Saving…"
 *   immediately. It flips back to false only after the response
 *   arrives — success or failure.
 *
 * TOASTS:
 *
 *   On success: `showToast.success("Settings saved", "Changes are on disk.", latencyMs)`
 *   On failure: `showToast.error("Failed to save settings", errorMessage)`
 *
 *   The toast module is imported lazily inside `pushPatch` so this
 *   store can be imported on the server (e.g. by tests or by a
 *   server component that reads default settings) without pulling
 *   in a client-only toast dependency.
 */
export type PanelMode = "workspace" | "source-control" | "hidden";
export type SourceControlMode = "agent" | "manual" | "browse";
export type DiffViewMode = "unified" | "split";

export interface SourceControlSettings {
  enabled: boolean;
  defaultPanelMode: PanelMode;
  defaultSourceControlMode: SourceControlMode;
  autoOpenOnOperation: boolean;
  diffViewMode: DiffViewMode;
  diffSampleLines: number;
  pollingIntervalMs: number;
  listPageSize: number;

  hydrated: boolean;
  saving: boolean;
  lastError: string | null;
  lastSavedAt: number | null;

  hydrate: () => Promise<void>;
  setEnabled: (v: boolean) => void;
  setDefaultPanelMode: (v: PanelMode) => void;
  setDefaultSourceControlMode: (v: SourceControlMode) => void;
  setAutoOpenOnOperation: (v: boolean) => void;
  setDiffViewMode: (v: DiffViewMode) => void;
  setDiffSampleLines: (v: number) => void;
  setPollingIntervalMs: (v: number) => void;
  setListPageSize: (v: number) => void;
  resetToDefaults: () => void;
}

const DEFAULT_SETTINGS = {
  enabled: false,
  defaultPanelMode: "workspace" as PanelMode,
  defaultSourceControlMode: "browse" as SourceControlMode,
  autoOpenOnOperation: true,
  diffViewMode: "unified" as DiffViewMode,
  diffSampleLines: 500,
  pollingIntervalMs: 3000,
  listPageSize: 30,
};

const API_BASE = "/api/source-control";
const DEBOUNCE_MS = 300;

interface ServerSettings {
  enabled: boolean;
  default_panel_mode: PanelMode;
  default_source_control_mode: SourceControlMode;
  auto_open_on_operation: boolean;
  diff_view_mode: DiffViewMode;
  diff_sample_lines: number;
  polling_interval_ms: number;
  list_page_size: number;
}

type WirePatch = Partial<ServerSettings>;
type LocalPatch = Partial<
  Omit<SourceControlSettings, "hydrated" | "saving" | "lastError" | "lastSavedAt">
>;

function toWire(patch: LocalPatch): WirePatch {
  const out: WirePatch = {};
  if (patch.enabled !== undefined) out.enabled = patch.enabled;
  if (patch.defaultPanelMode !== undefined)
    out.default_panel_mode = patch.defaultPanelMode;
  if (patch.defaultSourceControlMode !== undefined)
    out.default_source_control_mode = patch.defaultSourceControlMode;
  if (patch.autoOpenOnOperation !== undefined)
    out.auto_open_on_operation = patch.autoOpenOnOperation;
  if (patch.diffViewMode !== undefined)
    out.diff_view_mode = patch.diffViewMode;
  if (patch.diffSampleLines !== undefined)
    out.diff_sample_lines = patch.diffSampleLines;
  if (patch.pollingIntervalMs !== undefined)
    out.polling_interval_ms = patch.pollingIntervalMs;
  if (patch.listPageSize !== undefined)
    out.list_page_size = patch.listPageSize;
  return out;
}

function fromWire(wire: ServerSettings) {
  return {
    enabled: wire.enabled,
    defaultPanelMode: wire.default_panel_mode,
    defaultSourceControlMode: wire.default_source_control_mode,
    autoOpenOnOperation: wire.auto_open_on_operation,
    diffViewMode: wire.diff_view_mode,
    diffSampleLines: wire.diff_sample_lines,
    pollingIntervalMs: wire.polling_interval_ms,
    listPageSize: wire.list_page_size,
  };
}

export const useSourceControlSettings = create<SourceControlSettings>(
  (set) => ({
    ...DEFAULT_SETTINGS,
    hydrated: false,
    saving: false,
    lastError: null,
    lastSavedAt: null,

    hydrate: async () => {
      try {
        const res = await fetch(`${API_BASE}/settings`, {
          credentials: "same-origin",
        });
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const data = (await res.json()) as ServerSettings;
        set({ ...fromWire(data), hydrated: true, lastError: null });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error("[source-control-settings] hydrate failed:", msg);
        set({
          hydrated: true,
          lastError: `Failed to load settings: ${msg}`,
        });
      }
    },

    setEnabled: (enabled) => {
      set({ enabled });
      schedulePush({ enabled });
    },
    setDefaultPanelMode: (defaultPanelMode) => {
      set({ defaultPanelMode });
      schedulePush({ defaultPanelMode });
    },
    setDefaultSourceControlMode: (defaultSourceControlMode) => {
      set({ defaultSourceControlMode });
      schedulePush({ defaultSourceControlMode });
    },
    setAutoOpenOnOperation: (autoOpenOnOperation) => {
      set({ autoOpenOnOperation });
      schedulePush({ autoOpenOnOperation });
    },
    setDiffViewMode: (diffViewMode) => {
      set({ diffViewMode });
      schedulePush({ diffViewMode });
    },
    setDiffSampleLines: (diffSampleLines) => {
      const clamped = Math.min(5000, Math.max(100, diffSampleLines));
      set({ diffSampleLines: clamped });
      schedulePush({ diffSampleLines: clamped });
    },
    setPollingIntervalMs: (pollingIntervalMs) => {
      const clamped = Math.min(30000, Math.max(500, pollingIntervalMs));
      set({ pollingIntervalMs: clamped });
      schedulePush({ pollingIntervalMs: clamped });
    },
    setListPageSize: (listPageSize) => {
      const clamped = Math.min(100, Math.max(10, listPageSize));
      set({ listPageSize: clamped });
      schedulePush({ listPageSize: clamped });
    },
    resetToDefaults: () => {
      set({ ...DEFAULT_SETTINGS });
      schedulePush(DEFAULT_SETTINGS);
    },
  })
);

// ----------------------------------------------------------------------
// Debounced save queue
// ----------------------------------------------------------------------

let pendingPatch: WirePatch = {};
let patchTimer: ReturnType<typeof setTimeout> | null = null;
let batchStartedAt: number | null = null;

/**
 * Coalesce consecutive changes into a single PUT.
 *
 * Sets `saving: true` immediately (so the UI reflects the pending
 * write) and resets it to false only after the response arrives.
 */
function schedulePush(patch: LocalPatch): void {
  pendingPatch = { ...pendingPatch, ...toWire(patch) };
  if (batchStartedAt === null) batchStartedAt = Date.now();
  useSourceControlSettings.setState({ saving: true, lastError: null });

  if (patchTimer) clearTimeout(patchTimer);
  patchTimer = setTimeout(() => {
    const toSend = pendingPatch;
    const startedAt = batchStartedAt ?? Date.now();
    pendingPatch = {};
    patchTimer = null;
    batchStartedAt = null;
    void pushPatch(toSend, startedAt);
  }, DEBOUNCE_MS);
}

async function pushPatch(patch: WirePatch, startedAt: number): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(patch),
    });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body && typeof body.detail === "string") detail = body.detail;
      } catch {
        /* body was not JSON */
      }
      throw new Error(detail);
    }
    const data = (await res.json()) as ServerSettings;

    useSourceControlSettings.setState({
      ...fromWire(data),
      saving: false,
      lastError: null,
      lastSavedAt: Date.now(),
    });

    // Success toast — fires once per debounced batch, not per change.
    // The latency gives the user a subtle signal that the write was
    // real and how long it took.
    try {
      const { showToast } = await import(
        "@/components/ui/ToastNotification"
      );
      showToast.success(
        "Settings saved",
        "Your changes are on disk.",
        Date.now() - startedAt
      );
    } catch {
      /* toast module unavailable (SSR); the store state is still correct */
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[source-control-settings] PUT failed:", msg);
    useSourceControlSettings.setState({
      saving: false,
      lastError: `Save failed: ${msg}`,
    });
    try {
      const { showToast } = await import(
        "@/components/ui/ToastNotification"
      );
      showToast.error("Failed to save settings", msg);
    } catch {
      /* toast module unavailable (SSR) */
    }
  }
}
