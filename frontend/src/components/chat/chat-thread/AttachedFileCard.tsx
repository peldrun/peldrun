// Path: frontend/src/components/chat/chat-thread/AttachedFileCard.tsx
"use client";

/**
 * Presentational card representing a single file attached to a user message.
 *
 * Images render as an inline thumbnail; all other file types render as a
 * compact icon tile. Both variants delegate click handling to the optional
 * `onSelectFile` callback supplied by the parent.
 */

import React from "react";
import { Code2, FileText, Table, Film, Archive } from "lucide-react";
import type { AttachedUserFile } from "./types";
import { resolveFileCategory } from "./utils";

export interface AttachedCardProps {
  file: AttachedUserFile;
  onSelectFile?: (name: string) => void;
}

export function AttachedFileCard({ file, onSelectFile }: AttachedCardProps) {
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

export default AttachedFileCard;
