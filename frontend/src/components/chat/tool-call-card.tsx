// frontend/src/components/chat/tool-call-card.tsx

"use client";

import React, { useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Copy,
  EyeOff,
  HelpCircle,
  RotateCw,
  Server,
  XCircle,
} from "lucide-react";
import type { ToolPair } from "@/lib/types";
import { showToast } from "@/components/ui/ToastNotification";
import { resolveToolLabel, fetchToolManifest, ToolManifest } from "@/lib/tool-icon-registry";
import { ToolIcon } from "@/app/stores/_components/ToolIcon";

function truncate(value: unknown, max = 80): string {
  if (typeof value !== "string") return "";
  const s = value.replace(/\s+/g, " ").trim();
  if (s.length <= max) return s;
  return s.slice(0, max - 1) + "…";
}

export function parseMcpToolName(rawName: string): {
  isMcp: boolean;
  serverId?: string;
  remoteName?: string;
} {
  const raw = (rawName || "").trim();
  if (raw.startsWith("mcp__")) {
    const parts = raw.split("__");
    if (parts.length >= 3) {
      return {
        isMcp: true,
        serverId: parts[1],
        remoteName: parts.slice(2).join("__"),
      };
    }
  } else if (raw.startsWith("mcp::")) {
    const parts = raw.split("::");
    if (parts.length >= 3) {
      return {
        isMcp: true,
        serverId: parts[1],
        remoteName: parts.slice(2).join("::"),
      };
    }
  }
  return { isMcp: false };
}

function buildArgsPreview(
  toolName: string,
  args: Record<string, unknown> | undefined,
): string {
  if (!args || typeof args !== "object") return "";
  const mcp = parseMcpToolName(toolName);
  const targetName = mcp.isMcp && mcp.remoteName ? mcp.remoteName : toolName;
  const key = (targetName || "").toLowerCase().trim();

  const pick = (...names: string[]): string => {
    for (const n of names) {
      const v = args[n];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    return "";
  };

  if (key === "bash" || key === "shell" || key === "sh") {
    const cmd = pick("command", "cmd");
    return cmd ? `$ ${truncate(cmd, 90)}` : "";
  }
  if (key.startsWith("python")) {
    const code = pick("code", "script", "command");
    if (!code) return "";
    return truncate(code.split(/\r?\n/)[0].trim(), 90);
  }
  if (key.includes("search")) {
    const q = pick("query", "q", "search_query");
    return q ? `"${truncate(q, 80)}"` : "";
  }
  if (key.includes("http") || key.includes("fetch")) {
    const url = pick("url", "endpoint");
    return url ? truncate(url, 90) : "";
  }
  if (key.includes("editor")) {
    const cmd = pick("command");
    const path = pick("path", "file", "filename");
    if (cmd && path) return `${cmd} ${truncate(path, 60)}`;
    return path ? truncate(path, 90) : cmd;
  }
  if (key.includes("file")) {
    const f = pick("path", "file", "filename");
    return f ? truncate(f, 90) : "";
  }
  if (key.includes("browser")) {
    return pick("action", "operation");
  }
  if (key.includes("human")) {
    const q = pick("prompt", "question", "query");
    return q ? `"${truncate(q, 80)}"` : "";
  }
  if (key.includes("source_control")) {
    const action = pick("action");
    const repo = pick("repo", "repository", "workspace");
    if (action && repo) return `${action} · ${truncate(repo, 60)}`;
    return action || (repo ? truncate(repo, 90) : "");
  }
  if (key.includes("delegate")) {
    const target = pick("target_agent_id", "agent_id");
    const prompt = pick("subtask_prompt", "prompt", "task");
    if (target && prompt) return `${target} · "${truncate(prompt, 60)}"`;
    return target || (prompt ? `"${truncate(prompt, 80)}"` : "");
  }

  for (const v of Object.values(args)) {
    if (typeof v === "string" && v.trim()) return truncate(v, 90);
  }
  return "";
}

function formatSize(text: string | null | undefined): string {
  if (typeof text !== "string") return "";
  const bytes = new Blob([text]).size;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function countLines(text: string | null | undefined): number {
  if (typeof text !== "string" || !text) return 0;
  return text.split(/\r?\n/).length;
}

function friendlyErrorTitle(raw: string): string {
  const s = (raw || "").toLowerCase();
  if (s.includes("connection refused") || s.includes("connecterror") || s.includes("10061")) return "Could not reach the service";
  if (s.includes("timeout") || s.includes("timed out")) return "Operation timed out";
  if (s.includes("not found") || s.includes("404")) return "Resource not found";
  if (s.includes("unauthorized") || s.includes("401") || s.includes("invalid api key")) return "Authentication failed";
  if (s.includes("permission denied") || s.includes("403")) return "Permission denied";
  if (s.includes("rate limit") || s.includes("429")) return "Rate limit exceeded";
  if (s.includes("not found in active collection")) return "Tool was disabled or removed";
  return "Execution error";
}

const disabledToolsCache = new Map<string, { at: number; tools: Set<string> }>();
const DISABLED_CACHE_TTL_MS = 30_000;

async function fetchDisabledTools(agentId: string): Promise<Set<string>> {
  const now = Date.now();
  const cached = disabledToolsCache.get(agentId);
  if (cached && now - cached.at < DISABLED_CACHE_TTL_MS) {
    return cached.tools;
  }

  try {
    const base = "";
    const res = await fetch(`${base}/api/store/agents/${encodeURIComponent(agentId)}/tools`, { cache: "no-store" });
    if (!res.ok) {
      disabledToolsCache.set(agentId, { at: now, tools: new Set() });
      return new Set();
    }
    const data = await res.json();
    const tools = new Set<string>(Array.isArray(data?.disabled_tools) ? data.disabled_tools : []);
    disabledToolsCache.set(agentId, { at: now, tools });
    return tools;
  } catch {
    disabledToolsCache.set(agentId, { at: now, tools: new Set() });
    return new Set();
  }
}

/**
 * Set of MCP server IDs currently DISABLED for this agent.
 *
 * An MCP tool ``mcp__server__tool`` is considered disabled for the
 * agent when either:
 *   - its server id appears here (user disabled it via AgentMcpModal
 *     → ``disabled_mcp_servers``), OR
 *   - the server's manifest ``status`` is not ``"active"`` (global
 *     toggle from the Extensions section).
 *
 * Both conditions are folded into the returned set so downstream
 * callers only need a single membership check.
 */
const disabledMcpServersCache = new Map<string, { at: number; servers: Set<string> }>();

async function fetchDisabledMcpServers(agentId: string): Promise<Set<string>> {
  const now = Date.now();
  const cached = disabledMcpServersCache.get(agentId);
  if (cached && now - cached.at < DISABLED_CACHE_TTL_MS) {
    return cached.servers;
  }

  const disabled = new Set<string>();
  try {
    const base = "";

    // Two sources must be merged:
    //   1. Per-agent disabled_mcp_servers from agent_settings.json.
    //   2. Global server status from the extensions registry.
    const [agentMcpRes, allMcpRes] = await Promise.all([
      fetch(`${base}/api/store/agents/${encodeURIComponent(agentId)}/mcp`, { cache: "no-store" }),
      fetch(`${base}/api/mcp/servers`, { cache: "no-store" }),
    ]);

    if (agentMcpRes.ok) {
      const data = await agentMcpRes.json();
      const list = Array.isArray(data?.disabled_mcp_servers) ? data.disabled_mcp_servers : [];
      for (const s of list) disabled.add(String(s));
    }

    if (allMcpRes.ok) {
      const data = await allMcpRes.json();
      const servers = Array.isArray(data?.servers) ? data.servers : [];
      for (const s of servers) {
        if (s && s.id && s.status && s.status !== "active") {
          disabled.add(String(s.id));
        }
      }
    }

    disabledMcpServersCache.set(agentId, { at: now, servers: disabled });
    return disabled;
  } catch {
    disabledMcpServersCache.set(agentId, { at: now, servers: disabled });
    return disabled;
  }
}

/**
 * Cache of MCP server icon data, keyed by server_id.
 *
 * Fetches the server's manifest once (5-minute TTL) to extract its
 * `icon_svg` (preferred) or `icon_url` (fallback). A chat turn can
 * render several tool cards from the same server, so caching is
 * essential to avoid N identical network requests.
 *
 * On any failure, the cache stores an empty object so subsequent
 * calls within the TTL window short-circuit without retrying. The
 * caller falls back to the generic <Server> icon in that case.
 */
const mcpServerIconCache = new Map<
  string,
  { at: number; data: { icon_svg?: string; icon_url?: string } }
>();
const MCP_ICON_CACHE_TTL_MS = 5 * 60_000;

export async function fetchMcpServerIcon(
  serverId: string,
): Promise<{ icon_svg?: string; icon_url?: string }> {
  const now = Date.now();
  const cached = mcpServerIconCache.get(serverId);
  if (cached && now - cached.at < MCP_ICON_CACHE_TTL_MS) {
    return cached.data;
  }

  try {
    const res = await fetch(
      `/api/mcp/servers/${encodeURIComponent(serverId)}`,
      { cache: "no-store" },
    );
    if (!res.ok) {
      mcpServerIconCache.set(serverId, { at: now, data: {} });
      return {};
    }
    const json = await res.json();
    const server = json?.server || {};
    const data = {
      icon_svg:
        typeof server.icon_svg === "string" ? server.icon_svg : undefined,
      icon_url:
        typeof server.icon_url === "string" ? server.icon_url : undefined,
    };
    mcpServerIconCache.set(serverId, { at: now, data });
    return data;
  } catch {
    mcpServerIconCache.set(serverId, { at: now, data: {} });
    return {};
  }
}

export interface ToolCallCardProps {
  tool: ToolPair;
  agentId?: string;
}

export function ToolCallCard({ tool, agentId }: ToolCallCardProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const [copied, setCopied] = useState(false);
  const [manifest, setManifest] = useState<ToolManifest | null>(null);
  // MCP server icon — populated async; falls back to <Server> icon
  // when the manifest declares neither `icon_svg` nor `icon_url`.
  const [mcpIcon, setMcpIcon] = useState<{
    icon_svg?: string;
    icon_url?: string;
  } | null>(null);

  const mcpInfo = parseMcpToolName(tool.tool);

  // Fetch the real manifest to get display_name or name for non-MCP tools
  useEffect(() => {
    if (mcpInfo.isMcp) return;
    let cancelled = false;
    fetchToolManifest(tool.tool).then((m) => {
      if (!cancelled && m) setManifest(m);
    });
    return () => { cancelled = true; };
  }, [tool.tool, mcpInfo.isMcp]);

  // Fetch the MCP server's icon (svg preferred, url fallback).
  // Independent of `agentId` — we want the icon visible even when
  // no agent context is provided (e.g. standalone tool cards in
  // exports or previews).
  useEffect(() => {
    if (!mcpInfo.isMcp || !mcpInfo.serverId) return;
    let cancelled = false;
    fetchMcpServerIcon(mcpInfo.serverId).then((info) => {
      if (!cancelled) setMcpIcon(info);
    });
    return () => { cancelled = true; };
  }, [mcpInfo.isMcp, mcpInfo.serverId]);

  const label = mcpInfo.isMcp
    ? (mcpInfo.remoteName || tool.tool)
    : manifest
    ? (manifest.display_name || manifest.name)
    : resolveToolLabel(tool.tool);

  const isRetrying = tool.status === "retrying";
  const isFailed = tool.isError || tool.status === "failed";
  const isWaitingInput = tool.status === "waiting_for_input" || tool.requiresInput;

  const preview = buildArgsPreview(tool.tool, tool.args);
  const outputText = typeof tool.output === "string" ? tool.output : null;
  const outputSize = outputText ? formatSize(outputText) : "";
  const outputLines = outputText ? countLines(outputText) : 0;

  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    (async () => {
      if (mcpInfo.isMcp && mcpInfo.serverId) {
        // MCP tool: its disabled-ness depends on the server, not
        // the tool id itself. Check the merged set that combines
        // per-agent ``disabled_mcp_servers`` with the server's
        // global ``status``.
        const disabledServers = await fetchDisabledMcpServers(agentId);
        if (!cancelled) setDisabled(disabledServers.has(mcpInfo.serverId));
      } else {
        // Standard tool: check ``disabled_tools`` for the tool id.
        const set = await fetchDisabledTools(agentId);
        if (!cancelled) setDisabled(set.has(tool.tool));
      }
    })();
    return () => { cancelled = true; };
  }, [agentId, tool.tool, mcpInfo.isMcp, mcpInfo.serverId]);

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!outputText) return;
    try {
      await navigator.clipboard.writeText(outputText);
      setCopied(true);
      showToast.success("Copied", "Tool output copied to clipboard.");
      setTimeout(() => setCopied(false), 1500);
    } catch {
      showToast.error("Copy Failed", "Could not access the clipboard.");
    }
  };

  const renderStatus = () => {
    if (isRetrying) {
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-amber-500/15 text-amber-500 border border-amber-500/30">
          <RotateCw size={10} className="animate-spin" />
          <span>retry{tool.attempt && tool.maxAttempts ? ` ${tool.attempt}/${tool.maxAttempts}` : ""}</span>
        </span>
      );
    }
    if (isWaitingInput) {
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-blue-500/15 text-blue-500 border border-blue-500/30">
          <HelpCircle size={10} /><span>waiting</span>
        </span>
      );
    }
    if (isFailed) {
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-red-500/15 text-red-500 border border-red-500/30">
          <XCircle size={10} /><span>failed</span>
        </span>
      );
    }
    if (outputText) {
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-emerald-500/15 text-emerald-500 border border-emerald-500/30">
          <CheckCircle2 size={10} /><span>done</span>
        </span>
      );
    }
    return null;
  };

  return (
    <div
      className={`my-1.5 rounded-lg border text-xs overflow-hidden shadow-peldrun-xs transition-colors ${
        isRetrying ? "border-amber-500/40 bg-amber-500/5"
        : isFailed ? "border-red-500/40 bg-red-500/5"
        : isWaitingInput ? "border-blue-500/40 bg-blue-500/5"
        : "border-border bg-card/60"
      }`}
    >
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 hover:bg-muted/40 transition-colors cursor-pointer select-none"
        aria-expanded={isOpen}
      >
        <div className="flex items-center gap-2 truncate min-w-0 flex-1">
          <div className="flex-shrink-0 text-foreground">
            {mcpInfo.isMcp ? (
              mcpIcon?.icon_svg ? (
                // Preferred: inline SVG from the manifest. `currentColor`
                // is primary so the icon matches the MCP visual language
                // used elsewhere (ExtensionCard, MCPDetailDrawer). The
                // SVG is scaled to fill a 16×16 box via arbitrary child
                // selectors, keeping the card layout stable regardless
                // of the source SVG's own dimensions.
                <div
                  className="p-0.5 rounded bg-primary/10 text-primary border border-primary/20 inline-flex items-center justify-center w-5 h-5 overflow-hidden [&>svg]:w-full [&>svg]:h-full [&>svg]:block"
                  dangerouslySetInnerHTML={{ __html: mcpIcon.icon_svg }}
                />
              ) : mcpIcon?.icon_url ? (
                // Fallback: external icon URL, same box dimensions.
                <div className="p-0.5 rounded bg-primary/10 border border-primary/20 inline-flex items-center justify-center w-5 h-5 overflow-hidden">
                  <img
                    src={mcpIcon.icon_url}
                    alt=""
                    className="w-full h-full object-contain"
                    loading="lazy"
                  />
                </div>
              ) : (
                // Last resort: generic server icon (current behaviour).
                <div className="p-0.5 rounded bg-primary/10 text-primary border border-primary/20">
                  <Server size={12} />
                </div>
              )
            ) : (
              <ToolIcon
                tool={{
                  id: tool.tool,
                  name: label,
                  description: "",
                  category: manifest?.category || "",
                  icon: manifest?.icon,
                }}
                size={14}
              />
            )}
          </div>
          <span className="font-semibold text-[11px] text-foreground flex-shrink-0">{label}</span>

          {mcpInfo.isMcp && mcpInfo.serverId && (
            <span className="inline-flex items-center gap-1 px-1.5 py-0.2 rounded text-[9px] font-mono uppercase bg-primary/10 text-primary border border-primary/20 flex-shrink-0">
              <Server size={8} />
              <span>{mcpInfo.serverId}</span>
            </span>
          )}

          {!mcpInfo.isMcp && label.toLowerCase() !== tool.tool.toLowerCase() && (
            <span className="text-[10px] font-mono text-muted-foreground/60 flex-shrink-0 hidden sm:inline">{tool.tool}</span>
          )}

          {preview && (
            <span className="text-[10px] text-muted-foreground truncate font-mono min-w-0 flex-1 hidden md:inline" title={preview}>· {preview}</span>
          )}

          {disabled && (
            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-muted text-muted-foreground border border-border flex-shrink-0">
              <EyeOff size={10} /><span className="hidden sm:inline">disabled</span>
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {renderStatus()}
          {tool.durationMs !== null && tool.durationMs !== undefined && (
            <span className="flex items-center gap-1 text-[10px] font-mono text-muted-foreground">
              <Clock size={11} />{tool.durationMs}ms
            </span>
          )}
          {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </div>
      </button>

      {isOpen && (
        <div className="p-3 space-y-2 border-t border-border/60 bg-background/60">
          {mcpInfo.isMcp && (
            <div className="p-2 rounded bg-primary/5 border border-primary/20 flex items-center justify-between text-[10px] font-mono">
              <div className="flex items-center gap-1.5 text-primary font-semibold">
                <Server size={11} />
                <span>Protocol: Model Context Protocol (MCP)</span>
              </div>
              <span className="text-muted-foreground">Server: {mcpInfo.serverId}</span>
            </div>
          )}

          {preview && (
            <div>
              <div className="text-[9px] font-mono text-muted-foreground uppercase mb-1">Summary</div>
              <div className="text-[11px] font-mono text-foreground bg-muted/40 border border-border/50 rounded px-2 py-1 break-all">{preview}</div>
            </div>
          )}

          {tool.args && Object.keys(tool.args).length > 0 && (
            <div>
              <div className="text-[9px] font-mono text-muted-foreground uppercase mb-1">Parameters</div>
              <pre className="p-2 rounded-md bg-muted/50 border border-border/50 text-foreground font-mono text-[10px] overflow-x-auto max-h-48">{JSON.stringify(tool.args, null, 2)}</pre>
            </div>
          )}

          {tool.error && (
            <div>
              <div className="text-[9px] font-mono text-amber-500 uppercase mb-1 flex items-center gap-1.5">
                <AlertCircle size={10} />{isRetrying ? "Retry Diagnostic" : friendlyErrorTitle(tool.error)}
              </div>
              <pre className="p-2 rounded-md bg-amber-500/10 border border-amber-500/20 text-amber-300 font-mono text-[10px] whitespace-pre-wrap leading-relaxed max-h-36 overflow-auto">{tool.error}</pre>
            </div>
          )}

          {outputText !== null && outputText !== undefined && (
            <div>
              <div className="flex items-center justify-between mb-1 gap-2">
                <div className={`text-[9px] font-mono uppercase flex items-center gap-1.5 ${isFailed ? "text-red-500" : "text-emerald-500"}`}>
                  {isFailed ? <><XCircle size={10} />Failure Output</> : <><CheckCircle2 size={10} />Result</>}
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[9px] font-mono text-muted-foreground">{outputLines} line{outputLines === 1 ? "" : "s"} · {outputSize}</span>
                  <button
                    type="button"
                    onClick={handleCopy}
                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-mono text-muted-foreground hover:text-foreground hover:bg-muted/60 border border-border transition cursor-pointer"
                    title="Copy output to clipboard"
                  >
                    {copied ? <><Check size={10} className="text-emerald-500" /><span className="text-emerald-500">copied</span></> : <><Copy size={10} /><span>copy</span></>}
                  </button>
                </div>
              </div>
              <pre className={`p-2 rounded-md border font-mono text-[10px] whitespace-pre-wrap leading-relaxed overflow-x-auto max-h-56 ${isFailed ? "bg-red-500/10 border-red-500/20 text-red-300" : "bg-muted/40 border-border/50 text-foreground"}`}>{outputText}</pre>
            </div>
          )}

          {disabled && (
            <div className="flex items-start gap-2 p-2 rounded-md bg-muted/40 border border-border text-[10px] text-muted-foreground">
              <EyeOff size={11} className="shrink-0 mt-0.5" />
              <span>This tool is currently disabled for this agent. It will not be available on the next task run unless re-enabled from the agent's Tools panel.</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default ToolCallCard;
