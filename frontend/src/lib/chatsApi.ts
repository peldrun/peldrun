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

export interface FetchChatsOptions {
  projectId?: string;
  includeArchived?: boolean;
  pinnedOnly?: boolean;
  search?: string;
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
export async function fetchChats(optionsOrProjectId?: FetchChatsOptions | string): Promise<ChatSession[]> {
  try {
    let projectId: string | undefined;
    let includeArchived = false;
    let pinnedOnly = false;
    let search: string | undefined;

    if (typeof optionsOrProjectId === "string") {
      projectId = optionsOrProjectId;
    } else if (optionsOrProjectId) {
      projectId = optionsOrProjectId.projectId;
      includeArchived = !!optionsOrProjectId.includeArchived;
      pinnedOnly = !!optionsOrProjectId.pinnedOnly;
      search = optionsOrProjectId.search;
    }

    const params = new URLSearchParams();
    if (projectId) params.append("project_id", projectId);
    if (includeArchived) params.append("include_archived", "true");
    if (pinnedOnly) params.append("pinned_only", "true");
    if (search) params.append("search", search);

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
    body: JSON.stringify(payload)
  });
  if (!res.ok) return null;
  const data = await res.json();
  return data.agent || null;
}

export async function updateStoreAgent(agentId: string, payload: AgentUpsertPayload): Promise<AgentManifest | null> {
  const res = await fetch(`/api/store/agents/${encodeURIComponent(agentId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) return null;
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
