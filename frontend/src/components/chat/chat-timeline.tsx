// frontend/src/components/chat/chat-timeline.tsx

"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  BrainCircuit,
  Terminal,
  ChevronDown,
  ChevronRight,
  Server,
} from "lucide-react";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import { CodeBlock } from "@/components/chat/code-block";
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
import type { ImageResultItem, ToolProgress } from "@/lib/types";

export interface StepEvent {
  id: string;
  step: number;
  type:
    | "thought"
    | "tool_call"
    | "observation"
    | "error"
    | "final"
    | "ask_human"
    | "image_results";
  content: string;
  toolName?: string;
  timestamp?: string;

  /** Structured image data — only set when type === "image_results". */
  images?: ImageResultItem[];
  /** Provider name for the image search — only for image_results. */
  imageProvider?: string;
  /** Original query for the image search — only for image_results. */
  imageQuery?: string;

  // -----------------------------------------------------------------
  // Tool Output Streaming (opt-in)
  //
  // Populated only when the backend feature flag
  // PELDRUN_ENABLE_TOOL_OUTPUT_STREAMING is enabled AND the tool
  // opted into the streaming contract. When the flag is off (the
  // default), all four fields below stay undefined and the tool
  // card renders exactly as before.
  //
  // These fields live on `StepEvent` (not on `ToolPair`) because
  // this timeline receives its events directly from `use-sse-stream`
  // rather than from `stream-store`. `stream-store.ToolPair` carries
  // the equivalent fields — see `lib/types.ts` — but the two
  // representations are intentionally decoupled so that changes to
  // one path cannot break the other.
  // -----------------------------------------------------------------

  /**
   * Correlates this StepEvent with the backend's `tool_call_id`.
   * Required so that TOOL_OUTPUT / TOOL_PROGRESS events — which
   * arrive keyed by `tool_call_id` — can be routed to the correct
   * step. Set once, when the `tool_call` event is appended.
   */
  toolCallId?: string;

  /**
   * Accumulated raw stdout + stderr text. Appended in arrival order
   * as TOOL_OUTPUT chunks stream in. Kept after the observation
   * arrives: it is the historical record of what the user watched
   * during execution and is NOT cleared.
   */
  liveOutput?: string;

  /**
   * Per-channel views of `liveOutput`. Same three-key shape as
   * `ToolPair.liveStreams` in `lib/types.ts`, mirroring the
   * backend's `Literal["stdout", "stderr", "output"]`.
   */
  liveStreams?: {
    stdout?: string;
    stderr?: string;
    output?: string;
  };

  /**
   * Latest structured progress update (from TOOL_PROGRESS events).
   * Rendered as a progress bar, NOT appended to the text area.
   */
  progress?: ToolProgress;

  /**
   * [TOOL_CALL_STARTING] True while the framework is still
   * streaming the arguments JSON. When true, the tool card renders
   * a compact "⏳ preparing…" placeholder (no content, no args, no
   * code block) so the user sees activity during the multi-second
   * wait. The same StepEvent is updated in place (isPending: false)
   * when the complete `tool_call` arrives — no re-mount, no
   * flicker.
   */
  isPending?: boolean;
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

function TimelineToolCall({
  evt,
  isToolCompleted = false,
}: {
  evt: StepEvent;
  isToolCompleted?: boolean;
}) {
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

  // -----------------------------------------------------------------
  // File-write visibility (verifiability).
  // -----------------------------------------------------------------
  const rawArgs = evt.content;
  let parsedArgs: Record<string, any> = {};
  try {
    parsedArgs = typeof rawArgs === "string" ? JSON.parse(rawArgs) : rawArgs || {};
  } catch {
    parsedArgs = {};
  }

  const toolKey = (evt.toolName || "").toLowerCase();
  let writtenContent: string | null = null;
  let writtenLabel: string | null = null;
  let writtenLanguage = "text";
  const command = String(parsedArgs.command || parsedArgs.action || "").toLowerCase();
  const filePath = String(parsedArgs.path || parsedArgs.file_path || "").toLowerCase();

  if (toolKey.includes("editor") && command === "create") {
    writtenContent = parsedArgs.file_text ?? null;
    writtenLabel = "Created File Content";
  } else if (toolKey.includes("editor") && command === "str_replace") {
    writtenContent = parsedArgs.new_str ?? null;
    writtenLabel = "Applied Replacement";
  } else if (toolKey.includes("editor") && command === "insert") {
    writtenContent = parsedArgs.new_str ?? null;
    writtenLabel = "Inserted Text";
  } else if (toolKey === "file_ops" && (command === "write" || command === "append")) {
    writtenContent = parsedArgs.content ?? null;
    writtenLabel = command === "write" ? "Written Content" : "Appended Content";
  } else if (toolKey === "file_saver") {
    writtenContent = parsedArgs.content ?? null;
    writtenLabel = "File Content";
  }

  if (filePath.endsWith(".html")) writtenLanguage = "html";
  else if (filePath.endsWith(".css")) writtenLanguage = "css";
  else if (filePath.endsWith(".js")) writtenLanguage = "javascript";
  else if (filePath.endsWith(".json")) writtenLanguage = "json";
  else if (filePath.endsWith(".py")) writtenLanguage = "python";

  // -----------------------------------------------------------------
  // Sub-accordion state for the written file content.
  //
  // UX contract:
  //   • Opens automatically when the tool call first appears.
  //   • Stays open while the tool is executing (isToolCompleted=false).
  //   • Auto-closes 5s after the tool completes (observation arrives),
  //     giving the user enough time to scan the file.
  //   • After auto-close, the user can re-open it with a click.
  //   • A single auto-close per mount — if the user re-opens it
  //     manually, we never auto-close it again.
  //
  // The accordion survives re-renders because ChatTimeline is memoized
  // and the sub-component keeps its own state.
  // -----------------------------------------------------------------
  const [isContentExpanded, setIsContentExpanded] = useState(true);
  const hasAutoClosedRef = useRef(false);

  useEffect(() => {
    if (!writtenContent || !writtenLabel) return;
    if (!isToolCompleted) return;
    if (hasAutoClosedRef.current) return;

    const timer = setTimeout(() => {
      setIsContentExpanded(false);
      hasAutoClosedRef.current = true;
    }, 5000);

    return () => clearTimeout(timer);
  }, [writtenContent, writtenLabel, isToolCompleted]);

  // -----------------------------------------------------------------
  // Tool Output Streaming: display helpers.
  //
  // `hasLiveOutput` guards the pre-block so it is not rendered when
  // streaming produced no chunks (the default state when the backend
  // feature flag is off).
  //
  // `progressPercent` is computed only when both `current` and
  // `total` are finite and `total > 0`. This avoids divide-by-zero
  // and "NaN%" rendering when a tool emits a phase-only progress
  // event (e.g. `{phase: "downloading"}` with no counters).
  // -----------------------------------------------------------------
  const hasLiveOutput = Boolean(evt.liveOutput && evt.liveOutput.length > 0);
  const progress = evt.progress;
  const progressPercent =
    progress &&
    typeof progress.current === "number" &&
    typeof progress.total === "number" &&
    progress.total > 0
      ? Math.max(
          0,
          Math.min(100, Math.round((progress.current / progress.total) * 100)),
        )
      : null;

  return (
    <div className="flex flex-wrap items-start gap-2 p-2.5 rounded-sm bg-custom border border-border text-foreground font-mono text-xs">
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
      <div className="truncate flex-1 min-w-0">
        <span className="font-extrabold text-black mr-1">{label}: </span>
        {evt.isPending ? (
          <span className="inline-flex items-center gap-1.5 text-amber-500">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
            <span className="text-[10px]">preparing…</span>
          </span>
        ) : (
          <span className="text-muted-foreground">{displayContent}</span>
        )}
      </div>

      {/* Sub-accordion: Written File Content.
          Auto-opens when the file content first appears; auto-closes
          once the tool completes (after a 5s grace period). The user
          can always re-open it with a click.
          Hidden entirely while `isPending` — the content has not
          arrived yet. */}
      {!evt.isPending && writtenContent && writtenLabel && (
        <div className="basis-full mt-2 border border-border/40 rounded-md overflow-hidden bg-background/40">
          <button
            type="button"
            onClick={() => {
              setIsContentExpanded((v) => !v);
              // User manually toggled — freeze the auto-close forever.
              hasAutoClosedRef.current = true;
            }}
            className="w-full flex items-center justify-between px-2.5 py-1.5 hover:bg-muted/40 transition-colors cursor-pointer text-[10px] font-mono"
          >
            <div className="flex items-center gap-2 min-w-0">
              <span className="text-emerald-500 font-semibold uppercase whitespace-nowrap">
                ▸ {writtenLabel}
              </span>
              <span className="text-muted-foreground normal-case whitespace-nowrap">
                ({writtenContent.length} chars)
              </span>
              {!isToolCompleted && (
                <span className="inline-flex items-center gap-1 text-amber-500 whitespace-nowrap">
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
                  <span className="text-[9px]">executing…</span>
                </span>
              )}
              {isToolCompleted && (
                <span className="inline-flex items-center gap-1 text-emerald-500 whitespace-nowrap">
                  <span className="text-[9px]">✓ done</span>
                </span>
              )}
            </div>
            <span className="text-muted-foreground flex-shrink-0 ml-2">
              {isContentExpanded ? (
                <ChevronDown size={12} />
              ) : (
                <ChevronRight size={12} />
              )}
            </span>
          </button>

          {isContentExpanded && (
            <div className="p-2 border-t border-border/40">
              <CodeBlock
                code={writtenContent}
                language={writtenLanguage}
                filename={parsedArgs.path || parsedArgs.file_path}
              />
            </div>
          )}
        </div>
      )}

      {/* Progress bar — only rendered when a TOOL_PROGRESS event has
          been received. Placed above the live output so the bar stays
          visible while the text scrolls. */}
      {progress &&
        (progressPercent !== null ||
          progress.phase ||
          progress.message) && (
          <div className="basis-full flex items-center gap-2 text-[10px] pt-1">
            {progressPercent !== null && (
              <div className="flex-1 h-1 rounded bg-border overflow-hidden">
                <div
                  className="h-full bg-primary transition-all"
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
            )}
            <span className="text-muted-foreground whitespace-nowrap">
              {progress.phase ||
                (progressPercent !== null ? `${progressPercent}%` : "")}
              {progress.message ? ` — ${progress.message}` : ""}
            </span>
          </div>
        )}

      {/* Live output — raw chunks accumulated from TOOL_OUTPUT events.
          `basis-full` forces it onto its own row in the flex-wrap
          container. When stderr is present, it is rendered in red
          below stdout; this loses the exact interleaving of the two
          channels but preserves the color distinction, which is the
          most actionable information for the user. */}
      {hasLiveOutput && (
        <pre className="basis-full p-1.5 rounded bg-muted/30 border border-border/40 text-[10px] text-foreground whitespace-pre-wrap max-h-40 overflow-y-auto break-words">
          {evt.liveStreams?.stderr ? (
            <>
              {evt.liveStreams.stdout && (
                <span>{evt.liveStreams.stdout}</span>
              )}
              <span className="text-red-500">{evt.liveStreams.stderr}</span>
            </>
          ) : (
            evt.liveOutput
          )}
        </pre>
      )}
    </div>
  );
}

function ChatTimelineInner({
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

            {/* Image galleries are rendered by ChatThread above the
                timeline, not here. This module deliberately does NOT
                render them, to guarantee they cannot be hidden by any
                accordion state. */}
            {isExpanded && (
              <div className="mt-2.5 space-y-2 pl-1.5 pr-1 animate-in fade-in">
                {stepEvents
                  .filter((e) => e.type !== "image_results")
                  .map((evt) => (
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
                      <TimelineToolCall
                        evt={evt}
                        isToolCompleted={
                          // [FIX-EXECUTING-STUCK] Force-complete when the
                          // run is over. `isRunning` is a prop from the
                          // parent that is `false` once the run reaches
                          // any terminal state (completed, failed,
                          // cancelled). Without this guard, a missing
                          // observation event leaves the card stuck on
                          // "executing…" forever even after the run
                          // finished successfully.
                          //
                          // The `stepEvents.some(...)` check remains
                          // the primary signal during the run itself,
                          // because `isRunning` is `true` until the
                          // whole pipeline finishes.
                          !isRunning ||
                          stepEvents.some(
                            (e) =>
                              e.type === "observation" &&
                              (!e.toolName || e.toolName === evt.toolName)
                          )
                        }
                      />
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

// ---------------------------------------------------------------------------
// Memoization — custom comparison
// ---------------------------------------------------------------------------
//
// Why a custom comparison instead of React.memo's default shallow compare?
//
//   `ChatThread` rebuilds `filteredGroupedSteps` on EVERY render (see the
//   `Object.fromEntries(...)` call there), so the `groupedSteps` prop is a
//   fresh object each time the parent re-renders — even when the underlying
//   content is byte-identical. This happens at least once per second due to
//   `elapsedSeconds` updates, and on every unrelated state change (hover,
//   tooltip, selection, etc.).
//
//   Default shallow comparison would therefore never skip a render, defeating
//   the purpose of memoization. The custom comparator below walks the step
//   array and compares per-event identity + content: O(events) with tiny
//   constants (string identity checks), versus the full render cost of
//   re-running MarkdownRenderer over every historical event.
//
// What this fixes:
//   * Timer ticks (elapsedSeconds updates) no longer trigger a full timeline
//     re-render.
//   * Hover / tooltip / selection state in `ChatThread` no longer cascades
//     into `ChatTimeline`.
//
// What this does NOT fix:
//   * Intra-step streaming (20-40 tokens/second). Each token arrives as a new
//     `StepEvent`, so `groupedSteps` content genuinely changes and the memo
//     correctly re-renders. Per-step memoization is the next optimization.
// ---------------------------------------------------------------------------

function areExpandedStepsEqual(
  a: Record<string, boolean>,
  b: Record<string, boolean>
): boolean {
  if (a === b) return true;
  const aKeys = Object.keys(a);
  const bKeys = Object.keys(b);
  if (aKeys.length !== bKeys.length) return false;
  for (const k of aKeys) {
    if (a[k] !== b[k]) return false;
  }
  return true;
}

function areGroupedStepsEqual(
  a: Record<number, StepEvent[]>,
  b: Record<number, StepEvent[]>
): boolean {
  if (a === b) return true;
  const aKeys = Object.keys(a);
  const bKeys = Object.keys(b);
  if (aKeys.length !== bKeys.length) return false;

  for (const rawKey of aKeys) {
    const key = Number(rawKey);
    const arrA = a[key];
    const arrB = b[key];

    if (!arrB) return false;
    // Fast path: same array reference means the parent did not rebuild this
    // step's event list. This is common for historical steps when only the
    // active step is growing.
    if (arrA === arrB) continue;
    if (arrA.length !== arrB.length) return false;

    for (let i = 0; i < arrA.length; i++) {
      const ea = arrA[i];
      const eb = arrB[i];
      if (
        ea.id !== eb.id ||
        ea.type !== eb.type ||
        ea.content !== eb.content ||
        ea.toolName !== eb.toolName ||
        ea.step !== eb.step ||
        // -----------------------------------------------------------------
        // Tool Output Streaming fields.
        //
        // These checks are mandatory for the memo to remain correct
        // during streaming: the historical fields above never change
        // while a tool runs, so without the live fields the memo
        // would skip every re-render and the user would see nothing
        // until the observation arrives — defeating the entire
        // streaming feature.
        //
        // `liveStreams` is compared field-by-field rather than by
        // reference: the reducer rebuilds the object on every chunk,
        // so a reference check would always fail and force a full
        // re-render on every event, negating the batching done by
        // `useToolStreamBuffer`.
        // -----------------------------------------------------------------
        ea.liveOutput !== eb.liveOutput ||
        ea.liveStreams?.stdout !== eb.liveStreams?.stdout ||
        ea.liveStreams?.stderr !== eb.liveStreams?.stderr ||
        ea.liveStreams?.output !== eb.liveStreams?.output ||
        ea.progress?.current !== eb.progress?.current ||
        ea.progress?.total !== eb.progress?.total ||
        ea.progress?.phase !== eb.progress?.phase ||
        ea.progress?.message !== eb.progress?.message ||
        // [TOOL_CALL_STARTING] The pending → real transition must
        // re-render so the "preparing…" indicator is replaced by
        // the actual args/content. Without this check, React.memo
        // would treat the StepEvent as unchanged and the pending
        // placeholder would linger indefinitely.
        ea.isPending !== eb.isPending
      ) {
        return false;
      }
    }
  }
  return true;
}

function areChatTimelinePropsEqual(
  prev: ChatTimelineProps,
  next: ChatTimelineProps
): boolean {
  if (prev.prefix !== next.prefix) return false;
  if (prev.isCurrentActive !== next.isCurrentActive) return false;
  if (prev.currentStepNum !== next.currentStepNum) return false;
  if (prev.isRunning !== next.isRunning) return false;
  // `toggleStep` must be stable (useCallback with empty deps in ChatThread).
  // If a future refactor breaks that contract, we fall back to re-rendering
  // rather than risk rendering with a stale closure.
  if (prev.toggleStep !== next.toggleStep) return false;
  if (!areExpandedStepsEqual(prev.expandedSteps, next.expandedSteps)) return false;
  if (!areGroupedStepsEqual(prev.groupedSteps, next.groupedSteps)) return false;
  return true;
}

const ChatTimeline = React.memo(
  ChatTimelineInner,
  areChatTimelinePropsEqual
);

ChatTimeline.displayName = "ChatTimeline";

export { ChatTimeline };
export default ChatTimeline;
