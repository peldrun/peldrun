/**
 * frontend/src/components/workspace/logs-tab.tsx
 *
 * Terminal Log Viewer component with dual streaming and disk log capabilities.
 * Hardened to support real-time execution broadcast and session log persistence.
 */

"use client";

import React, { useState, useEffect, useRef, useMemo } from "react";
import {
  Terminal,
  Download,
  Copy,
  Check,
  ArrowDownCircle,
  Search,
  RefreshCw,
} from "lucide-react";
import { useStreamStore } from "@/stores/stream-store";

export interface LogsTabProps {
  activeJobId?: string | null;
  chatId?: string | null;
}

interface LogEntry {
  id: string;
  timestamp: string;
  type: "thought" | "tool_call" | "observation" | "step" | "final" | "error" | "system";
  message: string;
  meta?: any;
}

export function LogsTab({ activeJobId, chatId }: LogsTabProps) {
  const [filterType, setFilterType] = useState<string>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [copied, setCopied] = useState(false);
  const [liveLogs, setLiveLogs] = useState<string[]>([]);
  const [historicalLogs, setHistoricalLogs] = useState<string[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);

  const scrollRef = useRef<HTMLDivElement | null>(null);

  // 1. Resolve unified scope ID for disk and streaming queries
  const effectiveChatId = chatId || activeJobId || null;

  // 2. Read live run from Zustand stream-store if available
  const activeRun = useStreamStore((s) => (activeJobId ? s.runs[activeJobId] : null));

  // 3. Fetch persisted disk chat.log from the backend
  const fetchHistoricalLog = async () => {
    if (!effectiveChatId) return;
    setLoadingHistory(true);
    try {
      const res = await fetch(`/api/chats/${encodeURIComponent(effectiveChatId)}/log?t=${Date.now()}`);
      if (res.ok) {
        const data = await res.json();
        if (data.log && typeof data.log === "string" && data.log.trim()) {
          const lines = data.log.split("\n").filter((l: string) => l.trim().length > 0);
          setHistoricalLogs(lines);
        }
      }
    } catch (err) {
      console.debug("[LogsTab] Failed to fetch disk log:", err);
    } finally {
      setLoadingHistory(false);
    }
  };

  useEffect(() => {
    fetchHistoricalLog();
  }, [effectiveChatId]);

  // 4. Capture real-time live execution logs emitted during the task
  useEffect(() => {
    const handleLiveLog = (e: Event) => {
      const ce = e as CustomEvent<{ line?: string; type?: string; message?: string }>;
      if (ce.detail?.line) {
        setLiveLogs((prev) => [...prev, ce.detail!.line!]);
      } else if (ce.detail?.message) {
        const ts = new Date().toLocaleTimeString();
        const typeStr = (ce.detail.type || "SYSTEM").toUpperCase();
        setLiveLogs((prev) => [...prev, `[${ts}] [${typeStr}] ${ce.detail!.message}`]);
      }
    };

    const handleRunFinished = () => {
      // Re-fetch authoritative disk log once the run finishes
      setTimeout(() => {
        fetchHistoricalLog();
      }, 500);
    };

    window.addEventListener("peldrun:log-entry", handleLiveLog);
    window.addEventListener("peldrun:run-completed", handleRunFinished);

    return () => {
      window.removeEventListener("peldrun:log-entry", handleLiveLog);
      window.removeEventListener("peldrun:run-completed", handleRunFinished);
    };
  }, [effectiveChatId]);

  // 5. Compile Zustand stream items if populated
  const streamEntries: LogEntry[] = useMemo(() => {
    if (!activeRun || !activeRun.steps) return [];
    const entries: LogEntry[] = [];

    activeRun.steps.forEach((group) => {
      const stepTs = group.startedAt ? new Date(group.startedAt).toLocaleTimeString() : "";
      entries.push({
        id: `step-${group.step}`,
        timestamp: stepTs,
        type: "step",
        message: `Step ${group.step} started`,
      });

      if (Array.isArray(group.items)) {
        group.items.forEach((item: any, idx: number) => {
          const itemTs = item.timestamp
            ? new Date(item.timestamp).toLocaleTimeString()
            : stepTs;
          const iType = (item.type || "").toLowerCase();

          if (iType.includes("thought") || item.thought) {
            entries.push({
              id: `${group.step}-thought-${idx}`,
              timestamp: itemTs,
              type: "thought",
              message: item.thought || item.content || "",
            });
          } else if (iType.includes("tool") || item.tool_name) {
            const args = item.tool_args ? JSON.stringify(item.tool_args) : "";
            entries.push({
              id: `${group.step}-tool-${idx}`,
              timestamp: itemTs,
              type: "tool_call",
              message: `${item.tool_name || "tool"}(${args})`,
              meta: item.tool_args,
            });
          } else if (iType.includes("observation") || item.observation) {
            entries.push({
              id: `${group.step}-obs-${idx}`,
              timestamp: itemTs,
              type: "observation",
              message: item.observation || item.content || "",
            });
          } else if (item.content) {
            entries.push({
              id: `${group.step}-item-${idx}`,
              timestamp: itemTs,
              type: "system",
              message: item.content,
            });
          }
        });
      }
    });

    if (activeRun.finalAnswer) {
      entries.push({
        id: "final-answer",
        timestamp: activeRun.lastEventAt ? new Date(activeRun.lastEventAt).toLocaleTimeString() : "",
        type: "final",
        message: activeRun.finalAnswer,
      });
    }

    if (activeRun.error) {
      entries.push({
        id: "run-error",
        timestamp: activeRun.lastEventAt ? new Date(activeRun.lastEventAt).toLocaleTimeString() : "",
        type: "error",
        message: activeRun.error,
      });
    }

    return entries;
  }, [activeRun]);

  // Combine live stream or fallback to persisted disk lines
  const displayLogs = useMemo(() => {
    if (liveLogs.length > 0) {
      return liveLogs;
    }
    if (streamEntries.length > 0) {
      return streamEntries.map((e) => `[${e.timestamp || "LIVE"}] [${e.type.toUpperCase()}] ${e.message}`);
    }
    return historicalLogs;
  }, [liveLogs, streamEntries, historicalLogs]);

  const filteredLogs = useMemo(() => {
    let result = displayLogs;
    if (filterType !== "all") {
      const match = `[${filterType.toUpperCase()}]`;
      result = result.filter((l) => l.includes(match));
    }
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      result = result.filter((l) => l.toLowerCase().includes(q));
    }
    return result;
  }, [displayLogs, filterType, searchQuery]);

  // Auto-scroll logic
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [filteredLogs, autoScroll]);

  const handleCopyLogs = async () => {
    const text = displayLogs.join("\n");
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
    }
  };

  const downloadUrl = effectiveChatId
    ? `/api/chats/${encodeURIComponent(effectiveChatId)}/log/download`
    : null;

  return (
    <div className="flex flex-col h-full bg-[#0d1117] text-slate-200 font-mono text-xs select-text">
      {/* Terminal Toolbar */}
      <div className="h-10 border-b border-[#30363d] bg-[#161b22] px-3 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <Terminal size={14} className="text-emerald-400 shrink-0" />
          <span className="font-semibold text-xs tracking-wider text-slate-200 uppercase font-sans">
            Agent Execution Logs
          </span>
          {(liveLogs.length > 0 || activeRun) && (
            <span className="px-1.5 py-0.2 rounded text-[10px] bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" /> Live Stream
            </span>
          )}
          {!activeRun && liveLogs.length === 0 && historicalLogs.length > 0 && (
            <span className="px-1.5 py-0.2 rounded text-[10px] bg-sky-500/20 text-sky-400 border border-sky-500/30">
              Session Log
            </span>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0 font-sans">
          {/* Filter Pills */}
          <div className="hidden sm:flex items-center gap-0.5 bg-[#0d1117] p-0.5 rounded border border-[#30363d] text-[10px]">
            {["all", "thought", "tool_call", "observation", "error"].map((ft) => (
              <button
                key={ft}
                type="button"
                onClick={() => setFilterType(ft)}
                className={`px-1.5 py-0.5 rounded capitalize transition-colors cursor-pointer ${
                  filterType === ft
                    ? "bg-[#21262d] text-white font-medium shadow-xs"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {ft === "tool_call" ? "Tools" : ft}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => setAutoScroll(!autoScroll)}
            className={`p-1.5 rounded border transition-colors cursor-pointer ${
              autoScroll
                ? "bg-emerald-500/15 border-emerald-500/40 text-emerald-400"
                : "border-[#30363d] text-slate-400 hover:bg-[#21262d]"
            }`}
            title={autoScroll ? "Auto-scroll Enabled" : "Auto-scroll Paused"}
          >
            <ArrowDownCircle size={13} />
          </button>

          <button
            type="button"
            onClick={fetchHistoricalLog}
            disabled={loadingHistory}
            className="p-1.5 rounded border border-[#30363d] text-slate-400 hover:text-white hover:bg-[#21262d] transition-colors cursor-pointer"
            title="Refresh logs from disk"
          >
            <RefreshCw size={13} className={loadingHistory ? "animate-spin text-emerald-400" : ""} />
          </button>

          <button
            type="button"
            onClick={handleCopyLogs}
            disabled={displayLogs.length === 0}
            className="p-1.5 rounded border border-[#30363d] text-slate-400 hover:text-white hover:bg-[#21262d] transition-colors cursor-pointer disabled:opacity-40"
            title="Copy logs to clipboard"
          >
            {copied ? <Check size={13} className="text-emerald-400" /> : <Copy size={13} />}
          </button>

          {downloadUrl && (
            <a
              href={downloadUrl}
              download
              className="p-1.5 rounded border border-[#30363d] text-slate-400 hover:text-white hover:bg-[#21262d] transition-colors cursor-pointer"
              title="Download chat.log from session folder"
            >
              <Download size={13} />
            </a>
          )}
        </div>
      </div>

      {/* Search Input Bar */}
      <div className="px-3 py-1.5 bg-[#161b22]/60 border-b border-[#30363d]/60 flex items-center gap-2">
        <Search size={12} className="text-slate-500" />
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Filter logs by keyword..."
          className="w-full bg-transparent text-[11px] text-slate-200 placeholder:text-slate-500 focus:outline-none"
        />
        <span className="text-[10px] text-slate-500 font-sans shrink-0">
          {filteredLogs.length} {filteredLogs.length === 1 ? "line" : "lines"}
        </span>
      </div>

      {/* Terminal View Content */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3.5 space-y-1 leading-relaxed">
        {filteredLogs.length === 0 ? (
          <div className="h-44 flex flex-col items-center justify-center text-slate-500 text-xs">
            <Terminal size={22} className="opacity-30 mb-2" />
            <span>No log events recorded for this session yet.</span>
            <span className="text-[10px] opacity-70 mt-1">Logs update automatically when the agent runs.</span>
          </div>
        ) : (
          filteredLogs.map((logLine, idx) => {
            const isThought = logLine.includes("[THOUGHT]");
            const isTool = logLine.includes("[TOOL_CALL]");
            const isObs = logLine.includes("[OBSERVATION]");
            const isError = logLine.includes("[ERROR]");
            const isFinal = logLine.includes("[FINAL]");
            const isStep = logLine.includes("[STEP");

            let lineClass = "text-slate-300";
            if (isThought) lineClass = "text-amber-300/90";
            else if (isTool) lineClass = "text-sky-300 font-semibold";
            else if (isObs) lineClass = "text-emerald-300/90";
            else if (isError) lineClass = "text-rose-400 font-semibold";
            else if (isFinal) lineClass = "text-emerald-400 font-bold";
            else if (isStep) lineClass = "text-purple-400";

            return (
              <div key={idx} className={`flex items-start gap-2 text-[11px] hover:bg-[#161b22]/50 px-1 py-0.5 rounded ${lineClass}`}>
                <span className="text-slate-600 select-none text-[10px] w-7 shrink-0 text-right">
                  {idx + 1}
                </span>
                <span className="break-all whitespace-pre-wrap flex-1">{logLine}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

export default LogsTab;