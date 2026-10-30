// frontend/src/components/chat/chat-timeline.tsx

"use client";

import React, { useEffect, useState } from "react";
import {
  BrainCircuit,
  Terminal,
  ChevronDown,
  ChevronRight,
  Server,
} from "lucide-react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import { ToolIcon } from "@/app/stores/_components/ToolIcon";
import {
  resolveToolLabel,
  fetchToolManifest,
  formatToolArgs,
  ToolManifest,
} from "@/lib/tool-icon-registry";
import {
  parseMcpToolName,
  fetchMcpServerIcon,
} from "@/components/chat/tool-call-card";

export interface StepEvent {
  id: string;
  step: number;
  type: "thought" | "tool_call" | "observation" | "error" | "final" | "ask_human";
  content: string;
  toolName?: string;
  timestamp?: string;
}

interface ChatTimelineProps {
  prefix: string;
  groupedSteps: Record<number, StepEvent[]>;
  isCurrentActive?: boolean;
  currentStepNum: number;
  isRunning: boolean;
  expandedSteps: Record<string, boolean>;
  toggleStep: (key: string) => void;
}

function TimelineToolCall({ evt }: { evt: StepEvent }) {
  const [manifest, setManifest] = useState<ToolManifest | null>(null);
  // MCP server icon — same caching contract as ToolCallCard.
  // Populated async; falls back to <Server> icon when the manifest
  // declares neither icon_svg nor icon_url.
  const [mcpIcon, setMcpIcon] = useState<{
    icon_svg?: string;
    icon_url?: string;
  } | null>(null);

  // Detect MCP tool ids (mcp__server__tool) so we can route the
  // icon lookup to the correct endpoint:
  //   - MCP tool        → /api/mcp/servers/{server_id} (manifest)
  //   - Built-in tool   → /api/store/tools/{tool_id}  (tool manifest)
  // Without this routing, MCP tools generate spurious 404s on
  // /api/store/tools/mcp__*/icon.svg and the card shows no icon.
  const mcpInfo = evt.toolName
    ? parseMcpToolName(evt.toolName)
    : { isMcp: false };

  useEffect(() => {
    if (!evt.toolName) return;
    let cancelled = false;

    if (mcpInfo.isMcp && mcpInfo.serverId) {
      // MCP path: fetch the server manifest, extract its icon.
      fetchMcpServerIcon(mcpInfo.serverId).then((info) => {
        if (!cancelled) setMcpIcon(info);
      });
    } else {
      // Built-in path: fetch the tool manifest as before.
      fetchToolManifest(evt.toolName).then((m) => {
        if (!cancelled && m) setManifest(m);
      });
    }

    return () => { cancelled = true; };
  }, [evt.toolName, mcpInfo.isMcp, mcpInfo.serverId]);

  if (!evt.toolName) return null;

  // For MCP tools, prefer the remote name (portion after the final
  // `__`) — this matches the label shown in ToolCallCard and
  // reflects what the server actually calls the tool.
  const label = mcpInfo.isMcp
    ? mcpInfo.remoteName || evt.toolName
    : manifest
    ? manifest.display_name || manifest.name
    : resolveToolLabel(evt.toolName);

  // Format the raw JSON string into a clean readable string
  const displayContent = formatToolArgs(evt.toolName, evt.content);

  return (
    <div className="flex items-start gap-2 p-2.5 rounded-sm bg-custom border border-border text-foreground font-mono text-xs">
      <div className="mt-0.5 flex-shrink-0 text-foreground">
        {mcpInfo.isMcp ? (
          mcpIcon?.icon_svg ? (
            // Inline SVG from the server manifest — preferred.
            // currentColor makes it inherit the primary accent,
            // matching the MCP visual language elsewhere.
            <div
              className="bg-transparent text-black inline-flex
              items-center justify-center w-5 h-5 overflow-hidden [&>svg]:w-full [&>svg]:h-full [&>svg]:block"
              dangerouslySetInnerHTML={{ __html: mcpIcon.icon_svg }}
            />
          ) : mcpIcon?.icon_url ? (
            // External icon URL — fallback if no inline SVG.
            <div className="p-0.5 rounded bg-primary/10 border border-primary/20 inline-flex items-center justify-center w-4 h-4 overflow-hidden">
              <img
                src={mcpIcon.icon_url}
                alt=""
                className="w-full h-full object-contain"
                loading="lazy"
              />
            </div>
          ) : (
            // Last resort — generic server icon.
            <div className="p-0.5 rounded bg-primary/10 text-primary border border-primary/20">
              <Server size={12} />
            </div>
          )
        ) : (
          <ToolIcon
            tool={{
              id: evt.toolName,
              name: label,
              description: "",
              category: manifest?.category || "",
              icon: manifest?.icon
            }}
            size={14}
          />
        )}
      </div>
      <div className="truncate">
        <span className="font-extrabold text-black mr-1">{label}: </span>
        <span className="text-muted-foreground">{displayContent}</span>
      </div>
    </div>
  );
}

export function ChatTimeline({
  prefix,
  groupedSteps,
  isCurrentActive = false,
  currentStepNum,
  isRunning,
  expandedSteps,
  toggleStep,
}: ChatTimelineProps) {
  const entries = Object.entries(groupedSteps);
  if (entries.length === 0) return null;

  return (
    <div className="relative pl-5 ml-3 border-l-2 border-border/70 space-y-4 my-3 font-sans">
      {entries.map(([stepNumStr, stepEvents]) => {
        const stepNum = parseInt(stepNumStr, 10);
        const key = `${prefix}-${stepNum}`;
        const isExpanded = expandedSteps[key] === true;
        const isLatestStep = isCurrentActive && stepNum === currentStepNum && isRunning;

        const toolsUsed = Array.from(
          new Set(
            stepEvents
              .filter((e) => e.toolName)
              .map((e) => resolveToolLabel(e.toolName!))
          )
        );

        return (
          <div key={key} className="relative group">
            <div
              className={`absolute -left-[27px] top-1 w-3.5 h-3.5 rounded-full border-2 bg-background flex items-center justify-center transition-all ${
                isLatestStep ? "border-primary ring-2 ring-primary/40 scale-110" : "border-emerald-500/80"
              }`}
            >
              {isLatestStep ? (
                <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse" />
              ) : (
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
              )}
            </div>

            <div className="flex items-center justify-between py-1 px-2.5 -ml-1 rounded-md hover:bg-muted/40 transition-colors">
              <button
                type="button"
                onClick={() => toggleStep(key)}
                className="flex-1 flex items-center gap-2 text-left cursor-pointer"
              >
                <span className="text-xs font-semibold text-foreground">Step {stepNum}</span>
                <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-muted text-muted-foreground border border-border/50">{stepEvents.length} events</span>
                {toolsUsed.length > 0 && (
                  <span className="hidden sm:inline-flex items-center gap-1 text-[11px] font-mono text-muted-foreground truncate max-w-[240px]">
                    • {toolsUsed.join(", ")}
                  </span>
                )}
              </button>

              <button
                type="button"
                onClick={() => toggleStep(key)}
                className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground cursor-pointer transition-colors ml-2"
              >
                <span>{isExpanded ? "Collapse" : "View reasoning"}</span>
                {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              </button>
            </div>

            {isExpanded && (
              <div className="mt-2.5 space-y-2 pl-1.5 pr-1 animate-in fade-in">
                {stepEvents.map((evt) => (
                  <div key={evt.id} className="text-xs space-y-1">
                    {evt.type === "thought" && (
                      <div className="flex items-start gap-2.5 p-3 rounded-lg bg-card border border-border/80 text-foreground shadow-peldrun-xs">
                        <BrainCircuit size={15} className="text-peldrun-accent mt-0.5 flex-shrink-0" />
                        <div className="flex-1 min-w-0">
                          <MarkdownRenderer content={evt.content} />
                        </div>
                      </div>
                    )}

                    {evt.type === "tool_call" && evt.toolName && (
                      <TimelineToolCall evt={evt} />
                    )}

                    {evt.type === "observation" && (
                      <div className="p-2.5 text-xs font-mono text-foreground/90 bg-muted/20 rounded-md border border-border flex items-start gap-2">
                        <Terminal size={13} className="mt-0.5 flex-shrink-0 text-peldrun-success" />
                        <div className="flex-1 min-w-0 overflow-x-auto">
                          <MarkdownRenderer content={evt.content} />
                        </div>
                      </div>
                    )}

                    {evt.type === "error" && (
                      <div className="p-2.5 text-xs text-peldrun-error bg-peldrun-error/10 rounded-lg border border-peldrun-error/20 flex items-start gap-2">
                        <span className="whitespace-pre-wrap">{evt.content}</span>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default ChatTimeline;
