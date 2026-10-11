// frontend/src/components/chat/DelegationApprovalCard.tsx
"use client";

import React, { useState } from "react";
import {
  ShieldAlert,
  CheckCircle2,
  XCircle,
  Edit3,
  Bot,
  Clock,
  Send,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { DelegationApprovalRequest } from "@/lib/types";

interface DelegationApprovalCardProps {
  approval: DelegationApprovalRequest;
  onApprove: (approvalId: string, editedInstruction?: string) => Promise<void>;
  onReject: (approvalId: string) => Promise<void>;
  disabled?: boolean;
}

export function DelegationApprovalCard({
  approval,
  onApprove,
  onReject,
  disabled = false,
}: DelegationApprovalCardProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [instructionText, setInstructionText] = useState(approval.instruction);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleConfirmApprove = async () => {
    try {
      setIsSubmitting(true);
      const edited = isEditing && instructionText.trim() !== approval.instruction ? instructionText.trim() : undefined;
      await onApprove(approval.approvalRequestId, edited);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleConfirmReject = async () => {
    try {
      setIsSubmitting(true);
      await onReject(approval.approvalRequestId);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-xs shadow-sm transition-all animate-in fade-in zoom-in-95 my-3">
      <div className="flex items-center justify-between border-b border-amber-500/20 pb-2.5 mb-3">
        <div className="flex items-center gap-2 text-amber-600 dark:text-amber-400 font-semibold">
          <ShieldAlert size={16} className="shrink-0 animate-bounce" />
          <span>Delegation Authorization Gate</span>
        </div>
        <div className="flex items-center gap-1.5 text-[10px] font-mono bg-amber-500/15 text-amber-600 dark:text-amber-400 px-2 py-0.5 rounded-full border border-amber-500/25">
          <Clock size={11} />
          <span>Action Required</span>
        </div>
      </div>

      <div className="space-y-2.5 text-foreground">
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground font-medium">Target Specialist:</span>
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-card border border-border font-semibold text-primary">
            <Bot size={13} />
            <span>{approval.targetAgentId}</span>
          </span>
        </div>

        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-muted-foreground font-medium">Subtask Directive:</span>
            <button
              type="button"
              disabled={disabled || isSubmitting}
              onClick={() => setIsEditing(!isEditing)}
              className="text-[11px] text-primary hover:underline flex items-center gap-1 cursor-pointer"
            >
              <Edit3 size={11} />
              <span>{isEditing ? "Reset to Original" : "Edit Directive"}</span>
            </button>
          </div>

          {isEditing ? (
            <textarea
              value={instructionText}
              onChange={(e) => setInstructionText(e.target.value)}
              disabled={disabled || isSubmitting}
              rows={3}
              className="w-full rounded-lg bg-background border border-border p-2.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-primary shadow-xs font-mono"
              placeholder="Refine subtask directive before delegating..."
            />
          ) : (
            <div className="rounded-lg bg-background/80 border border-border/80 p-2.5 text-xs text-foreground/90 font-mono whitespace-pre-wrap break-words leading-relaxed">
              {instructionText}
            </div>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2 pt-3 mt-3 border-t border-amber-500/20">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || isSubmitting}
          onClick={handleConfirmReject}
          className="text-xs h-8 px-3 border-border hover:bg-muted text-muted-foreground hover:text-foreground cursor-pointer flex items-center gap-1.5"
        >
          <XCircle size={13} />
          <span>Continue Alone (Reject)</span>
        </Button>

        <Button
          type="button"
          size="sm"
          disabled={disabled || isSubmitting || (isEditing && !instructionText.trim())}
          onClick={handleConfirmApprove}
          className="text-xs h-8 px-3.5 bg-primary text-primary-foreground hover:bg-primary/90 cursor-pointer flex items-center gap-1.5 shadow-xs"
        >
          {isEditing ? <Send size={13} /> : <CheckCircle2 size={13} />}
          <span>{isEditing ? "Approve Edited Directive" : "Authorize Specialist"}</span>
        </Button>
      </div>
    </div>
  );
}

export default DelegationApprovalCard;
