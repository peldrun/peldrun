import { AgentManifest, ToolDefinition, ExtensionDefinition, AgentUpsertPayload } from "./types";

// Re-export store types for backwards compatibility across all components
export type { AgentManifest, ToolDefinition, ExtensionDefinition, AgentUpsertPayload };

export interface Project {
  id: string;
  name: string;
  description?: string;
  created_at: string;
}

export interface ChatSession {
  id: string;
  project_id?: string;
  title: string;
  job_id: string;
  prompt: string;
  agent_id?: string;
  created_at: string | number;
  updated_at?: string | number;
  status: string;
  is_pinned?: boolean;
  pinned?: boolean;
  is_archived?: boolean;
  pinned_at?: string;
  archived_at?: string;
}

export interface StorageStats {
  total_chats: number;
  active_chats: number;
  pinned_chats: number;
  archived_chats: number;
  disk_size_bytes: number;
  disk_size_mb: number;
}

export interface SweepReport {
  status: string;
  valid_chats_count: number;
  removed_orphans_count: number;
  removed_folders: string[];
  freed_bytes: number;
  freed_mb: number;
}

export interface PurgeReport {
  status: string;
  message?: string;
  deleted_sessions_count?: number;
  purged_jobs_count?: number;
  freed_mb?: number;
}

export type ChatTimeRange =
  | "today"
  | "yesterday"
  | "last_7_days"
  | "last_30_days"
  | "older";

export interface FetchChatsOptions {
  projectId?: string;
  includeArchived?: boolean;
  pinnedOnly?: boolean;
  archivedOnly?: boolean;
  search?: string;
  /** Time bucket filter applied server-side before pagination. */
  timeRange?: ChatTimeRange;
  /** Max items to return in a single page. Clamped to [1, MAX_CHATS_PAGE_LIMIT]. */
  limit?: number;
  /** Opaque cursor returned by a previous paginated call. */
  cursor?: string;
  /** Offset-based pagination fallback for backends without cursor support. */
  offset?: number;
}

/** Default page size used when callers omit `limit`. */
export const DEFAULT_CHATS_PAGE_LIMIT = 50;

/** Hard upper bound to protect the backend from oversized requests. */
export const MAX_CHATS_PAGE_LIMIT = 200;

/** Result of a paginated `fetchChatsPage` call. */
export interface PaginatedChats {
  items: ChatSession[];
  /** Cursor for the next page, or null when the last page has been reached. */
  nextCursor: string | null;
  /** Total items across all pages when the backend reports it, else null. */
  total: number | null;
  /** True when more pages are available. */
  hasMore: boolean;
}

/**
 * Compute the effective page limit, clamped to the documented bounds.
 * Exported so callers can normalize user input consistently.
 */
export function clampChatsPageLimit(raw: number | undefined): number {
  if (raw === undefined || raw === null || !Number.isFinite(raw)) {
    return DEFAULT_CHATS_PAGE_LIMIT;
  }
  const floored = Math.floor(raw);
  return Math.min(Math.max(1, floored), MAX_CHATS_PAGE_LIMIT);
}

// Endpoint helper for consistent relative URL resolution
export function getApiUrl(endpoint: string): string {
  return endpoint;
}

// ==========================================
// Date Parsing & Categorization Helpers
// ==========================================
export function parseChatDate(val?: string | number | null): Date {
  if (!val) return new Date(0);
  if (typeof val === "number") {
    return new Date(val > 1e11 ? val : val * 1000);
  }
  const str = String(val).trim();
  if (!str || str === "null" || str === "undefined") return new Date(0);

  const num = Number(str);
  if (!isNaN(num) && num > 0 && /^\d+(\.\d+)?$/.test(str)) {
    return new Date(num > 1e11 ? num : num * 1000);
  }
  const isoStr = str.replace(" ", "T");
  const d = new Date(isoStr);
  return isNaN(d.getTime()) ? new Date(0) : d;
}

export function formatChatDateTime(val?: string | number | null): string {
  const d = parseChatDate(val);
  if (d.getTime() === 0) return "Recent";

  const now = new Date();
  const isToday =
    d.getDate() === now.getDate() &&
    d.getMonth() === now.getMonth() &&
    d.getFullYear() === now.getFullYear();

  const timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  if (isToday) {
    return `Today, ${timeStr}`;
  }

  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const isYesterday =
    d.getDate() === yesterday.getDate() &&
    d.getMonth() === yesterday.getMonth() &&
    d.getFullYear() === yesterday.getFullYear();

  if (isYesterday) {
    return `Yesterday, ${timeStr}`;
  }

  return d.toLocaleDateString([], {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export type ChatTimeBucket = "today" | "this_week" | "older";

export function categorizeChatTimeBucket(val?: string | number | null): ChatTimeBucket {
  const d = parseChatDate(val);
  if (d.getTime() === 0) return "today";

  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();

  if (t >= startOfToday) {
    return "today";
  }

  const sevenDaysAgo = startOfToday - 6 * 24 * 60 * 60 * 1000;
  if (t >= sevenDaysAgo) {
    return "this_week";
  }

  return "older";
}

// ==========================================
// Projects API
// ==========================================
export async function fetchProjects(): Promise<Project[]> {
  try {
    const res = await fetch("/api/chats/projects", { cache: "no-store" });
    if (!res.ok) return [];
    const data = await res.json();
    return data.projects || [];
  } catch (e) {
    console.error("fetchProjects error:", e);
    return [];
  }
}

export async function createProject(name: string, description?: string): Promise<Project | null> {
  try {
    const res = await fetch("/api/chats/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description: description || "" })
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data.project || null;
  } catch (err) {
    console.error("createProject error:", err);
    return null;
  }
}

export async function deleteProject(projectId: string): Promise<boolean> {
  try {
    const res = await fetch(`/api/chats/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
    return res.ok;
  } catch (err) {
    console.error("deleteProject error:", err);
    return false;
  }
}

// ==========================================
// Chats API
// ==========================================
export async function fetchChats(
  optionsOrProjectId?: FetchChatsOptions | string
): Promise<ChatSession[]> {
  try {
    const opts: FetchChatsOptions =
      typeof optionsOrProjectId === "string"
        ? { projectId: optionsOrProjectId }
        : optionsOrProjectId ?? {};

    const params = new URLSearchParams();
    if (opts.projectId) params.append("project_id", opts.projectId);
    if (opts.includeArchived) params.append("include_archived", "true");
    if (opts.pinnedOnly) params.append("pinned_only", "true");
    if (opts.archivedOnly) params.append("archived_only", "true");
    if (opts.search) params.append("search", opts.search);

    // Optional pagination parameters: only sent when the caller
    // provides them so that existing callers keep the exact same
    // behavior (fetch all) against backends that do not implement
    // pagination.
    if (opts.limit !== undefined) {
      params.append("limit", String(clampChatsPageLimit(opts.limit)));
    }
    if (opts.cursor) {
      params.append("cursor", opts.cursor);
    }
    if (opts.offset !== undefined && opts.offset >= 0) {
      params.append("offset", String(Math.floor(opts.offset)));
    }

    const qs = params.toString() ? `?${params.toString()}` : "";
    const res = await fetch(`/api/chats${qs}`, { cache: "no-store" });
    if (!res.ok) return [];
    const data = await res.json();
    return data.chats || [];
  } catch (err) {
    console.error("fetchChats error:", err);
    return [];
  }
}

/**
 * Cursor-based paginated variant of `fetchChats`.
 *
 * Returns a page of chats together with pagination metadata. Backends
 * that do not implement `limit`/`cursor`/`offset` are supported
 * transparently: the whole result set is returned and `hasMore` is
 * false, so callers can fall back to single-page rendering without
 * branching on backend capabilities.
 *
 * Typical usage in an infinite-scroll list:
 *
 *   let cursor: string | null = null;
 *   do {
 *     const page = await fetchChatsPage({ limit: 50, cursor: cursor ?? undefined });
 *     append(page.items);
 *     cursor = page.nextCursor;
 *   } while (cursor);
 */
export async function fetchChatsPage(
  options: FetchChatsOptions = {}
): Promise<PaginatedChats> {
  const empty: PaginatedChats = {
    items: [],
    nextCursor: null,
    total: null,
    hasMore: false,
  };

  try {
    const params = new URLSearchParams();
    if (options.projectId) params.append("project_id", options.projectId);
    if (options.includeArchived) params.append("include_archived", "true");
    if (options.pinnedOnly) params.append("pinned_only", "true");
    if (options.archivedOnly) params.append("archived_only", "true");
    if (options.search) params.append("search", options.search);
    if (options.timeRange) params.append("time_range", options.timeRange);

    const limit = clampChatsPageLimit(options.limit);
    params.append("limit", String(limit));
    if (options.cursor) params.append("cursor", options.cursor);
    if (options.offset !== undefined && options.offset >= 0) {
      params.append("offset", String(Math.floor(options.offset)));
    }

    const qs = params.toString() ? `?${params.toString()}` : "";
    const res = await fetch(`/api/chats${qs}`, { cache: "no-store" });
    if (!res.ok) return empty;

    const data = await res.json();
    const items: ChatSession[] = Array.isArray(data.chats) ? data.chats : [];

    // Normalize cursor / total across common backend shapes.
    const nextCursor: string | null =
      typeof data.next_cursor === "string"
        ? data.next_cursor
        : typeof data.nextCursor === "string"
        ? data.nextCursor
        : null;

    const total: number | null =
      typeof data.total === "number" ? data.total : null;

    const offset = options.offset ?? 0;
    const backendHasMore =
      typeof data.has_more === "boolean" ? (data.has_more as boolean) : null;
    const hasMore =
      backendHasMore !== null
        ? backendHasMore
        : nextCursor !== null ||
          (total !== null && offset + items.length < total);

    return { items, nextCursor, total, hasMore };
  } catch (err) {
    console.error("fetchChatsPage error:", err);
    return empty;
  }
}

export async function deleteAllChats(): Promise<PurgeReport | null> {
  try {
    const res = await fetch("/api/chats/all", { method: "DELETE" });
    if (!res.ok) return null;
    const data = await res.json();
    return data.report || data || { status: "ok" };
  } catch (err) {
    console.error("deleteAllChats error:", err);
    return null;
  }
}

export async function deleteChat(chatId: string): Promise<boolean> {
  try {
    const res = await fetch(`/api/chats/${encodeURIComponent(chatId)}`, { method: "DELETE" });
    return res.ok;
  } catch (err) {
    console.error("deleteChat error:", err);
    return false;
  }
}

export async function renameChat(chatId: string, title: string): Promise<ChatSession | null> {
  try {
    const res = await fetch(`/api/chats/${encodeURIComponent(chatId)}/rename`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title })
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data.chat || null;
  } catch (err) {
    console.error("renameChat error:", err);
    return null;
  }
}

export async function togglePinChat(chatId: string): Promise<ChatSession | null> {
  try {
    const res = await fetch(`/api/chats/${encodeURIComponent(chatId)}/pin`, { method: "POST" });
    if (!res.ok) return null;
    const data = await res.json();
    return data.chat || null;
  } catch (err) {
    console.error("togglePinChat error:", err);
    return null;
  }
}

export async function toggleArchiveChat(chatId: string): Promise<ChatSession | null> {
  try {
    const res = await fetch(`/api/chats/${encodeURIComponent(chatId)}/archive`, { method: "POST" });
    if (!res.ok) return null;
    const data = await res.json();
    return data.chat || null;
  } catch (err) {
    console.error("toggleArchiveChat error:", err);
    return null;
  }
}

export async function fetchStorageStats(): Promise<StorageStats | null> {
  try {
    const res = await fetch("/api/chats/storage/stats", { cache: "no-store" });
    if (!res.ok) return null;
    const data = await res.json();
    return data.stats || null;
  } catch (err) {
    console.error("fetchStorageStats error:", err);
    return null;
  }
}

export async function sweepStorageOrphans(): Promise<SweepReport | null> {
  try {
    const res = await fetch("/api/chats/storage/sweep", { method: "POST" });
    if (!res.ok) return null;
    const data = await res.json();
    return data.report || null;
  } catch (err) {
    console.error("sweepStorageOrphans error:", err);
    return null;
  }
}

// ==========================================
// Stores & Extensions API Client Methods
// ==========================================
export async function fetchStoreAgents(): Promise<AgentManifest[]> {
  const res = await fetch("/api/store/agents", { cache: "no-store" });
  if (!res.ok) return [];
  const data = await res.json();
  return data.agents || [];
}

export async function fetchStoreAgent(agentId: string): Promise<AgentManifest | null> {
  const res = await fetch(`/api/store/agents/${encodeURIComponent(agentId)}`);
  if (!res.ok) return null;
  const data = await res.json();
  return data.agent || null;
}

export async function createStoreAgent(payload: AgentUpsertPayload): Promise<AgentManifest | null> {
  const res = await fetch("/api/store/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    // Surface the backend validation error instead of silently
    // returning null. FastAPI returns {detail: [...]} or {detail: "..."}.
    const body = await res.json().catch(() => ({}));
    let detail: string = `HTTP ${res.status}`;
    if (typeof body.detail === "string") {
      detail = body.detail;
    } else if (Array.isArray(body.detail)) {
      // Pydantic field errors: [{loc, msg, type}, ...]
      detail = body.detail
        .map((e: any) => {
          const loc = Array.isArray(e.loc) ? e.loc.slice(1).join(".") : "";
          return loc ? `${loc}: ${e.msg}` : e.msg;
        })
        .join("; ");
    }
    throw new Error(detail);
  }

  const data = await res.json();
  return data.agent || null;
}

export async function updateStoreAgent(agentId: string, payload: AgentUpsertPayload): Promise<AgentManifest | null> {
  const res = await fetch(`/api/store/agents/${encodeURIComponent(agentId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    let detail: string = `HTTP ${res.status}`;
    if (typeof body.detail === "string") {
      detail = body.detail;
    } else if (Array.isArray(body.detail)) {
      detail = body.detail
        .map((e: any) => {
          const loc = Array.isArray(e.loc) ? e.loc.slice(1).join(".") : "";
          return loc ? `${loc}: ${e.msg}` : e.msg;
        })
        .join("; ");
    }
    throw new Error(detail);
  }

  const data = await res.json();
  return data.agent || null;
}

export async function deleteStoreAgent(agentId: string): Promise<boolean> {
  const res = await fetch(`/api/store/agents/${encodeURIComponent(agentId)}`, {
    method: "DELETE"
  });
  return res.ok;
}

export async function fetchStoreTools(): Promise<ToolDefinition[]> {
  const res = await fetch("/api/store/tools", { cache: "no-store" });
  if (!res.ok) return [];
  const data = await res.json();
  return data.tools || [];
}

export async function fetchStoreExtensions(): Promise<ExtensionDefinition[]> {
  const res = await fetch("/api/store/extensions", { cache: "no-store" });
  if (!res.ok) return [];
  const data = await res.json();
  return data.extensions || [];
}


export async function fetchToolManifest(
  toolId: string
): Promise<Record<string, unknown> | null> {
  try {
    const res = await fetch(`/api/store/tools/${toolId}`, { cache: "no-store" });
    if (!res.ok) return null;
    const data = await res.json();
    return data.tool || null;
  } catch { return null; }
}
