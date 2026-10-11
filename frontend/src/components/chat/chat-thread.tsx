// Path: frontend/src/components/chat/chat-thread.tsx

"use client";

import React, { useState, useRef, useEffect } from "react";
import {
  Copy,
  Check,
  HelpCircle,
  CheckCircle2,
  MessageSquare,
  ChevronDown,
  ChevronUp,
  Code2,
  FileText,
  Table,
  Film,
  Archive,
  ImageIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ChatTimeline, StepEvent } from "./chat-timeline";
import { ChatDeliverable } from "./chat-deliverable";
import { MarkdownRenderer } from "./markdown-renderer";
import AIThinkingLoader from "@/components/AIThinkingLoader";

export interface AttachedUserFile {
  name: string;
  path?: string;
  url?: string;
  type?: string;
}

export interface ChatTurn {
  id: string;
  jobId: string;
  prompt: string;
  timestamp: string;
  steps: StepEvent[];
  finalResult: string | null;
  status: "completed" | "failed";
  tokensUsed?: { input: number; output: number; total: number };
  producedFiles?: { name: string; path: string }[];
  attachedFiles?: AttachedUserFile[];
  model?: string;
  mode?: "agent" | "chat";
  usage?: {
    cost_usd: number;
    latency_ms: number;
    tokens_per_second: number;
    provider: string;
    model: string;
  };
}

interface ChatThreadProps {
  historyTurns: ChatTurn[];
  submittedPrompt: string;
  submittedFiles?: AttachedUserFile[];
  sessionTimestamp: string;
  humanQuery: string | null;
  humanAnswer: string;
  setHumanAnswer: (val: string) => void;
  onSendHumanAnswer: (customAnswer?: string) => void;
  execMode: "agent" | "chat";
  activeGroupedSteps: Record<number, StepEvent[]>;
  currentStepNum: number;
  status: "idle" | "running" | "completed" | "failed";
  elapsedSeconds: number;
  finalResult: string | null;
  producedFiles: { name: string; path: string }[];
  copiedSection: string | null;
  copyText: (text: string, id: string) => void;
  expandedSteps: Record<string, boolean>;
  toggleStep: (key: string) => void;
  onSelectFile: (fileName: string) => void;
  showRawTrace: boolean;
  setShowRawTrace: (val: boolean) => void;
  getLiveStatusMessage: () => string;
  chatScrollBottomRef: React.RefObject<HTMLDivElement | null>;
  activeModelName?: string;
  tokensUsed?: { input: number; output: number; total: number };
  costUsd?: number | null;
  latencyMs?: number | null;
  tokensPerSecond?: number | null;
}

function parsePromptAndAttachments(
  rawPrompt: string,
  explicitFiles?: AttachedUserFile[]
): { displayText: string; attachedFiles: AttachedUserFile[] } {
  let cleanedText = rawPrompt || "";
  const files: AttachedUserFile[] = explicitFiles ? [...explicitFiles] : [];

  const filesSummaryRegex = /(?:\n\n|\n)?\[Uploaded Files\]:\n([\s\S]*)$/i;
  const match = cleanedText.match(filesSummaryRegex);

  if (match) {
    const listBlock = match[1];
    cleanedText = cleanedText.replace(filesSummaryRegex, "").trim();

    const lines = listBlock.split("\n");
    for (const line of lines) {
      const lineMatch = line.trim().match(/^-\s*([^\s(]+)/);
      if (lineMatch) {
        const extractedName = lineMatch[1].trim();
        if (extractedName && !files.some((f) => f.name === extractedName)) {
          files.push({ name: extractedName, path: extractedName });
        }
      }
    }
  }

  if (cleanedText === "Inspect workspace files:") {
    cleanedText = "";
  }

  return { displayText: cleanedText, attachedFiles: files };
}

function resolveFileCategory(fileName: string, customUrl?: string) {
  const extension = fileName.includes(".")
    ? fileName.slice(fileName.lastIndexOf(".")).toLowerCase()
    : "";

  const isImage = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"].includes(extension);
  const isPdf = extension === ".pdf";
  const isData = [".csv", ".tsv", ".xlsx", ".xls"].includes(extension);
  const isDoc = [".doc", ".docx", ".txt", ".rtf", ".odt", ".md"].includes(extension);
  const isAudioVideo = [".mp3", ".wav", ".ogg", ".mp4", ".mov", ".mkv", ".webm"].includes(extension);
  const isArchive = [".zip", ".tar", ".gz", ".rar", ".7z"].includes(extension);
  const isCode =
    !isImage &&
    !isPdf &&
    !isData &&
    !isDoc &&
    !isAudioVideo &&
    !isArchive;

  const previewUrl = customUrl || (isImage ? `/api/files/raw/${fileName}` : undefined);

  return {
    isImage,
    isPdf,
    isData,
    isDoc,
    isAudioVideo,
    isArchive,
    isCode,
    previewUrl,
  };
}

interface AttachedCardProps {
  file: AttachedUserFile;
  onSelectFile?: (name: string) => void;
}

function AttachedFileCard({ file, onSelectFile }: AttachedCardProps) {
  const info = resolveFileCategory(file.name, file.url);

  if (info.isImage && info.previewUrl) {
    return (
      <div
        onClick={() => onSelectFile && onSelectFile(file.name)}
        className="w-24 h-24 sm:w-28 sm:h-28 rounded-2xl border border-border/50 overflow-hidden bg-muted/40 relative shadow-xs hover:border-primary/50 transition-all flex items-center justify-center group cursor-pointer"
        title={file.name}
      >
        <img
          src={info.previewUrl}
          alt={file.name}
          className="w-full h-full object-cover rounded-2xl"
          onError={(e) => {
            (e.currentTarget as HTMLElement).style.display = "none";
          }}
        />
        <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-end p-1.5 pointer-events-none">
          <span className="text-[10px] text-white truncate max-w-full font-mono">{file.name}</span>
        </div>
      </div>
    );
  }

  return (
    <div
      onClick={() => onSelectFile && onSelectFile(file.name)}
      className="w-24 h-24 sm:w-28 sm:h-28 rounded-2xl bg-muted/70 dark:bg-muted/30 border border-border/50 flex flex-col justify-between p-2.5 relative shadow-xs hover:border-primary/40 transition-all select-none overflow-hidden cursor-pointer group"
      title={file.name}
    >
      <div className="self-end w-5 h-5 rounded-md flex items-center justify-center shadow-xs bg-[#8b2626] text-white">
        {info.isCode && <Code2 size={12} strokeWidth={2.5} />}
        {info.isPdf && <FileText size={12} strokeWidth={2.5} />}
        {info.isData && <Table size={12} strokeWidth={2.5} />}
        {info.isDoc && <FileText size={12} strokeWidth={2.5} />}
        {info.isAudioVideo && <Film size={12} strokeWidth={2.5} />}
        {info.isArchive && <Archive size={12} strokeWidth={2.5} />}
        {!info.isCode && !info.isPdf && !info.isData && !info.isDoc && !info.isAudioVideo && !info.isArchive && (
          <Code2 size={12} strokeWidth={2.5} />
        )}
      </div>

      <div className="text-[11px] sm:text-xs text-foreground font-medium truncate max-w-full text-center tracking-tight px-1 font-sans">
        {file.name}
      </div>
    </div>
  );
}

interface UserMessageBubbleProps {
  prompt: string;
  files?: AttachedUserFile[];
  timestamp?: string;
  copyId: string;
  copiedSection: string | null;
  onCopy: (text: string, id: string) => void;
  onSelectFile?: (name: string) => void;
}

function UserMessageBubble({
  prompt,
  files = [],
  timestamp,
  copyId,
  copiedSection,
  onCopy,
  onSelectFile,
}: UserMessageBubbleProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const { displayText, attachedFiles } = parsePromptAndAttachments(prompt, files);

  const isLongText = displayText.length > 220 || displayText.split("\n").length > 4;

  return (
    <div className="flex flex-col items-end space-y-2 w-full">
      {/* Attached Files Grid */}
      {attachedFiles.length > 0 && (
        <div className="flex flex-wrap gap-2.5 justify-end max-w-[90%]">
          {attachedFiles.map((file, idx) => (
            <AttachedFileCard
              key={`${file.name}-${idx}`}
              file={file}
              onSelectFile={onSelectFile}
            />
          ))}
        </div>
      )}

      {/* User Text Bubble with Collapsible Logic */}
      {displayText && (
        <div className="w-auto max-w-[90%] sm:max-w-[85%] rounded-3xl bg-muted/60 bg-user_message  text-t_user_message p-3.5 sm:p-4 transition-all relative">
          <div
            className={`overflow-hidden transition-all duration-200 ${
              isLongText && !isExpanded ? "max-h-24 relative" : "max-h-none"
            }`}
          >
            <MarkdownRenderer content={displayText} />
            {isLongText && !isExpanded && (
              <div className="absolute bottom-0 left-0 right-0 h-10 bg-gradient-to-t from-user_message/90 to-transparent pointer-events-none" />
            )}
          </div>

          {isLongText && (
            <div className="flex justify-start pt-2">
              <button
                type="button"
                onClick={() => setIsExpanded(!isExpanded)}
                className="w-6 h-6 rounded-full bg-background border border-border shadow-xs flex items-center justify-center text-muted-foreground hover:text-foreground cursor-pointer transition-transform hover:scale-105 active:scale-95"
                title={isExpanded ? "Collapse message" : "Expand message"}
              >
                {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Metadata & Copy Action */}
      <div className="flex items-center gap-2 px-1 text-[10px] text-muted-foreground font-mono">
        {timestamp && <span>{timestamp}</span>}
        <button
          type="button"
          onClick={() => onCopy(displayText || prompt, copyId)}
          className="p-1 hover:text-foreground text-muted-foreground transition-all cursor-pointer rounded"
          title="Copy prompt"
        >
          {copiedSection === copyId ? (
            <Check size={11} className="text-emerald-500" />
          ) : (
            <Copy size={11} />
          )}
        </button>
      </div>
    </div>
  );
}

export function ChatThread({
  historyTurns,
  submittedPrompt,
  submittedFiles = [],
  sessionTimestamp,
  humanQuery,
  humanAnswer,
  setHumanAnswer,
  onSendHumanAnswer,
  execMode,
  activeGroupedSteps,
  currentStepNum,
  status,
  elapsedSeconds,
  finalResult,
  producedFiles,
  copiedSection,
  copyText,
  expandedSteps,
  toggleStep,
  onSelectFile,
  showRawTrace,
  setShowRawTrace,
  getLiveStatusMessage,
  chatScrollBottomRef,
  activeModelName = "Assistant",
  tokensUsed,
  costUsd,
  latencyMs,
  tokensPerSecond,
}: ChatThreadProps) {
  const activePromptAnchorRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (submittedPrompt) {
      activePromptAnchorRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [submittedPrompt]);

  const groupStepEvents = (evts: StepEvent[]) => {
    return evts.reduce((acc, s) => {
      const isVisible = (s.content && s.content.trim() !== "") || Boolean(s.toolName);
      if (!isVisible) return acc;
      if (!acc[s.step]) acc[s.step] = [];
      acc[s.step].push(s);
      return acc;
    }, {} as Record<number, StepEvent[]>);
  };

  const isCurrentRawTrace = Boolean(
    finalResult &&
    (finalResult.includes("Observed output of cmd") || finalResult.startsWith("Step 1:"))
  );

  const activeStepsList = Object.values(activeGroupedSteps).flat();
  const currentLastThought = [...activeStepsList]
    .reverse()
    .find((s) => s.type === "thought" && s.content && !s.content.startsWith("Step ") && !s.content.startsWith("terminate("));

  const pendingHumanCall = [...activeStepsList].reverse().find((s) => {
    return (
      (s.toolName === "ask_human" || s.toolName === "human_input") &&
      s.type === "tool_call"
    );
  });

  let detectedPrompt = humanQuery;
  let detectedType = "text";
  let detectedOptions: string[] = [];

  if (pendingHumanCall && pendingHumanCall.content) {
    try {
      const parsedArgs = JSON.parse(pendingHumanCall.content);
      if (parsedArgs && typeof parsedArgs === "object") {
        detectedPrompt = parsedArgs.prompt || parsedArgs.query || detectedPrompt;
        detectedType = (parsedArgs.input_type || "text").toLowerCase();
        if (Array.isArray(parsedArgs.options) && parsedArgs.options.length > 0) {
          detectedOptions = parsedArgs.options.map(String);
        } else if (detectedType === "confirm" || detectedType === "boolean") {
          detectedOptions = ["Yes", "No"];
        }
      }
    } catch {
      if (!detectedPrompt) {
        detectedPrompt = pendingHumanCall.content;
      }
    }
  }

  const isHumanWaiting = Boolean(detectedPrompt && status === "running");

  return (
    <div className="flex-1 overflow-y-auto p-4 sm:p-5 font-sans">
      <div className="w-full max-w-[900px] mx-auto space-y-6">
        {/* Historical Turns */}
        {historyTurns.map((turn, tIdx) => {
          const turnMode = turn.mode || (turn.steps && turn.steps.length > 0 ? "agent" : "chat");
          const isAgentTurn = turnMode === "agent";
          const isTurnRawTrace = Boolean(
            turn.finalResult &&
            (turn.finalResult.includes("Observed output of cmd") || turn.finalResult.startsWith("Step 1:"))
          );
          const turnLastThought = [...turn.steps]
            .reverse()
            .find((s) => s.type === "thought" && s.content && !s.content.startsWith("Step ") && !s.content.startsWith("terminate("));

          const turnGrouped = groupStepEvents(turn.steps);

          return (
            <div key={turn.id || `turn-${tIdx}`} className="space-y-4 pb-4">
              {/* Rich User Message with Markdown, Links, Attached Cards & Collapsible View */}
              <UserMessageBubble
                prompt={turn.prompt}
                files={turn.attachedFiles}
                timestamp={turn.timestamp}
                copyId={`turn-prompt-${tIdx}`}
                copiedSection={copiedSection}
                onCopy={copyText}
                onSelectFile={onSelectFile}
              />

              {/* Execution Steps */}
              {isAgentTurn && turnGrouped && Object.keys(turnGrouped).length > 0 && (
                <ChatTimeline
                  prefix={`turn-${tIdx}`}
                  groupedSteps={turnGrouped}
                  isCurrentActive={false}
                  currentStepNum={0}
                  isRunning={false}
                  expandedSteps={expandedSteps}
                  toggleStep={toggleStep}
                />
              )}

              {/* Final Result with Historical Telemetry Badges */}
              {turn.finalResult && (
                <ChatDeliverable
                  finalResult={turn.finalResult}
                  status={turn.status}
                  turnIndex={tIdx}
                  isTurnRawTrace={isTurnRawTrace}
                  lastThoughtContent={turnLastThought ? turnLastThought.content : undefined}
                  producedFiles={turn.producedFiles}
                  onSelectFile={onSelectFile}
                  copiedSection={copiedSection}
                  onCopy={copyText}
                  execMode={isAgentTurn ? "agent" : "chat"}
                  showRawTrace={showRawTrace}
                  setShowRawTrace={setShowRawTrace}
                  modelName={turn.model || activeModelName}
                  timestamp={turn.timestamp}
                  tokensUsed={turn.tokensUsed}
                  costUsd={turn.usage?.cost_usd}
                  latencyMs={turn.usage?.latency_ms}
                  tokensPerSecond={turn.usage?.tokens_per_second}
                />
              )}
            </div>
          );
        })}

        {/* Current Active Turn Prompt */}
        {submittedPrompt && (
          <div ref={activePromptAnchorRef} className="space-y-4">
            <UserMessageBubble
              prompt={submittedPrompt}
              files={submittedFiles}
              timestamp={sessionTimestamp}
              copyId="user-prompt"
              copiedSection={copiedSection}
              onCopy={copyText}
              onSelectFile={onSelectFile}
            />
          </div>
        )}

        {/* Active Stepper (Agent Mode) */}
        {execMode === "agent" && activeGroupedSteps && Object.keys(activeGroupedSteps).length > 0 && (
          <ChatTimeline
            prefix="active"
            groupedSteps={activeGroupedSteps}
            isCurrentActive={true}
            currentStepNum={currentStepNum}
            isRunning={status === "running"}
            expandedSteps={expandedSteps}
            toggleStep={toggleStep}
          />
        )}

        {/* Interactive Human-in-the-Loop Dialog Suspension Card */}
        {isHumanWaiting && (
          <div className="p-4 sm:p-5 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs space-y-3.5 shadow-sm transition-all">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-amber-600 dark:text-amber-400 font-semibold text-xs">
                <HelpCircle size={15} className="shrink-0 animate-bounce" />
                <span>Agent Paused & Waiting for Your Decision:</span>
              </div>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/20 font-bold">
                Action Required
              </span>
            </div>

            <div className="p-3 rounded-lg bg-background border border-border text-foreground text-sm font-medium leading-relaxed whitespace-pre-wrap">
              {detectedPrompt}
            </div>

            {/* Clickable Quick Action Option Buttons */}
            {detectedOptions.length > 0 && (
              <div className="space-y-1.5">
                <span className="text-[11px] font-medium text-muted-foreground block">
                  Select an option to proceed:
                </span>
                <div className="flex flex-wrap items-center gap-2">
                  {detectedOptions.map((opt) => (
                    <button
                      key={opt}
                      type="button"
                      onClick={() => onSendHumanAnswer(opt)}
                      className="px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-primary text-primary-foreground hover:bg-primary/90 transition-all shadow-xs cursor-pointer flex items-center gap-1.5 active:scale-95"
                    >
                      <CheckCircle2 size={13} />
                      <span>{opt}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* Freeform Text Input Response */}
            <div className="flex items-center gap-2 pt-1">
              <div className="relative flex-1">
                <input
                  type="text"
                  value={humanAnswer}
                  onChange={(e) => setHumanAnswer(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && onSendHumanAnswer()}
                  placeholder={
                    detectedOptions.length > 0
                      ? "Or type a custom response to the agent..."
                      : "Type your response to the agent..."
                  }
                  className="w-full bg-background border border-border rounded-lg pl-3 pr-8 py-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-primary shadow-xs"
                />
                <MessageSquare size={13} className="absolute right-2.5 top-2.5 text-muted-foreground/60 pointer-events-none" />
              </div>
              <Button
                variant="primary"
                onClick={() => onSendHumanAnswer()}
                disabled={!humanAnswer.trim()}
                className="text-xs px-4 h-8.5 rounded-lg cursor-pointer font-medium"
              >
                Send
              </Button>
            </div>
          </div>
        )}

        {/* Live Running Indicator */}
        {status === "running" && !isHumanWaiting && (
          <div className="flex items-center justify-between px-3.5 py-2.5 bg-muted/40 text-foreground transition-all">
            <div className="flex items-center gap-2.5 text-xs">
              <AIThinkingLoader
                message={getLiveStatusMessage()}
                color="blue"
                speed={5}
                textAnimation="shimmer"
                showDots={false}
              />
            </div>
            <span className="text-[11px] font-mono px-2 py-0.5 rounded-md bg-background border border-border text-muted-foreground">
              {elapsedSeconds}s
            </span>
          </div>
        )}

        {/* Current Turn Final Deliverable */}
        {finalResult && (
          <ChatDeliverable
            finalResult={finalResult}
            status={status === "failed" ? "failed" : "completed"}
            isTurnRawTrace={isCurrentRawTrace}
            lastThoughtContent={currentLastThought ? currentLastThought.content : undefined}
            producedFiles={producedFiles}
            onSelectFile={onSelectFile}
            copiedSection={copiedSection}
            onCopy={copyText}
            execMode={execMode}
            showRawTrace={showRawTrace}
            setShowRawTrace={setShowRawTrace}
            modelName={activeModelName}
            timestamp={sessionTimestamp}
            tokensUsed={tokensUsed}
            costUsd={costUsd}
            latencyMs={latencyMs}
            tokensPerSecond={tokensPerSecond}
          />
        )}

        <div ref={chatScrollBottomRef} />
      </div>
    </div>
  );
}

export default ChatThread;
