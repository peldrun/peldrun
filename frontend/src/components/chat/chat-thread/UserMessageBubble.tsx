// src/components/chat/chat-thread/UserMessageBubble.tsx
"use client";

import React, { useState } from "react";
import {
  Copy,
  Check,
  ChevronDown,
  ChevronUp,
  BookmarkPlus,
} from "lucide-react";
import { MarkdownRenderer } from "../markdown-renderer";
import type { AttachedUserFile } from "./types";
import { parsePromptAndAttachments } from "./utils";
import { AttachedFileCard } from "./AttachedFileCard";

import { EntryEditor } from "@/features/memory/components/EntryEditor";
import { useMemoryMode } from "@/features/memory/hooks/useMemoryMode";
import { MemoryScope } from "@/features/memory/types/enums";

export interface UserMessageBubbleProps {
  prompt: string;
  files?: AttachedUserFile[];
  timestamp?: string;
  copyId: string;
  copiedSection: string | null;
  onCopy: (text: string, id: string) => void;
  onSelectFile?: (name: string) => void;
  /**
   * Chat identifier used as the target scope when the user saves
   * this message as a memory entry. Optional -- when omitted (e.g.
   * for callers that do not wire memory), the save button is not
   * rendered at all.
   */
  chatId?: string;
}

export function UserMessageBubble({
  prompt,
  files = [],
  timestamp,
  copyId,
  copiedSection,
  onCopy,
  onSelectFile,
  chatId,
}: UserMessageBubbleProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [saveDialogOpen, setSaveDialogOpen] = useState(false);

  const { displayText, attachedFiles } = parsePromptAndAttachments(
    prompt,
    files,
  );

  // ---- Memory mode gate ------------------------------------------
  //
  // `canRead` controls whether the "Save to memory" button is
  // rendered at all: when memory is disabled, the action has no
  // target and showing a permanently-broken button is worse than
  // not showing it. When memory is `read_only`, the button renders
  // but stays disabled with a tooltip explaining why -- the user
  // can see it exists and understands what to fix.
  const { canRead, canWrite } = useMemoryMode();

  const isLongText =
    displayText.length > 220 || displayText.split("\n").length > 4;

  // ---- Save handler ---------------------------------------------
  //
  // Opening the dialog is all this handler does; the actual save
  // is handled by `EntryEditor`'s internal mutations. This keeps
  // the button logic trivial and lets the editor own its entire
  // form/validation/mutation lifecycle (as it does for every
  // other save flow in the feature).
  const openSaveDialog = () => setSaveDialogOpen(true);

  return (
    <>
      <div className="group flex flex-col items-end space-y-2 w-full">
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

        {displayText && (
          <div className="w-auto max-w-[90%] sm:max-w-[85%] rounded-3xl  bg-user_message text-t_user_message p-3.5 sm:p-4 transition-all relative">
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
                  {isExpanded ? (
                    <ChevronUp size={14} />
                  ) : (
                    <ChevronDown size={14} />
                  )}
                </button>
              </div>
            )}
          </div>
        )}

        <div className="date_timestamp flex items-center gap-2 px-1 text-[10px] text-muted-foreground font-mono opacity-0 group-hover:opacity-100 transition-opacity duration-200">
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

          {/* ---- Save to memory ---- */}
          {chatId && canRead && (
            <button
              type="button"
              onClick={openSaveDialog}
              disabled={!canWrite}
              className="p-1 hover:text-foreground text-muted-foreground transition-all cursor-pointer rounded disabled:cursor-not-allowed disabled:opacity-40"
              title={
                canWrite
                  ? "Save this message to memory"
                  : "Memory is read-only -- switch to read & write to save"
              }
              aria-label="Save message to memory"
            >
              <BookmarkPlus size={11} />
            </button>
          )}
        </div>
      </div>

      {/* ---- Save-to-memory dialog ---- */}
      {chatId && canWrite && (
        <EntryEditor
          open={saveDialogOpen}
          onOpenChange={setSaveDialogOpen}
          mode="create"
          scope={{
            scope_type: MemoryScope.CHAT,
            scope_id: chatId,
          }}
          initialContent={displayText}
        />
      )}
    </>
  );
}

export default UserMessageBubble;
