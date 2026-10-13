// Path: frontend/src/components/chat/chat-thread/UserMessageBubble.tsx
"use client";

/**
 * User-authored message bubble with optional file attachments.
 *
 * Features:
 *  - Renders attached files as a horizontal stack of `AttachedFileCard`s.
 *  - Renders the prompt body through the shared `MarkdownRenderer`.
 *  - Collapses long prompts behind an expand/collapse chevron toggle.
 *  - Provides a per-message copy button that reports its state via
 *    `copiedSection` controlled by the parent.
 */

import React, { useState } from "react";
import { Copy, Check, ChevronDown, ChevronUp } from "lucide-react";
import { MarkdownRenderer } from "../markdown-renderer";
import type { AttachedUserFile } from "./types";
import { parsePromptAndAttachments } from "./utils";
import { AttachedFileCard } from "./AttachedFileCard";

export interface UserMessageBubbleProps {
  prompt: string;
  files?: AttachedUserFile[];
  timestamp?: string;
  copyId: string;
  copiedSection: string | null;
  onCopy: (text: string, id: string) => void;
  onSelectFile?: (name: string) => void;
}

export function UserMessageBubble({
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
        <div className="w-auto max-w-[90%] sm:max-w-[85%] rounded-3xl bg-muted/60 bg-user_message text-t_user_message p-3.5 sm:p-4 transition-all relative">
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

export default UserMessageBubble;
