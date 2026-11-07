// frontend/src/app/stores/_components/SkillCard.tsx

"use client";

import React from "react";
import {
  Bot,
  Info,
  MessageSquare,
  Pencil,
  ShieldCheck,
  Sparkles,
  Tag,
  Trash2,
} from "lucide-react";
import type { SkillSummary } from "../_types/store";
import { SkillIcon } from "./SkillIcon";

export interface SkillCardProps {
  skill: SkillSummary;
  /** True while a toggle request for this specific skill is in flight. */
  isToggling: boolean;
  /** True while a delete request for this specific skill is in flight. */
  isDeleting: boolean;
  onToggle: (skillId: string) => void;
  onDelete: (skillId: string) => void;
  onOpenDetails: (skill: SkillSummary) => void;
  /**
   * Optional: opens the SKILL.md editor for this skill.
   * Only invoked when the skill is user-editable. When omitted, the
   * pencil button is not rendered — this keeps the card reusable in
   * contexts where editing is not available (e.g. read-only embeds).
   */
  onOpenEditor?: (skill: SkillSummary) => void;
}

/**
 * Small inline badge used for chat/agent capability markers.
 * Renders only when the caller already knows the value is meaningful.
 */
function CapabilityChip({
  icon: Icon,
  label,
  tone = "neutral",
  title,
}: {
  icon: React.ComponentType<{ size?: number; className?: string }>;
  label: string;
  tone?: "neutral" | "active" | "muted";
  title?: string;
}) {
  const cls =
    tone === "active"
      ? "text-primary border-primary/30 bg-primary/5"
      : tone === "muted"
      ? "text-muted-foreground border-border bg-muted/40 opacity-60"
      : "text-muted-foreground border-border bg-muted/30";
  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border ${cls}`}
      title={title}
    >
      <Icon size={9} />
      {label}
    </span>
  );
}

/**
 * Single-row representation of an installed skill.
 *
 * Layout:
 *   [icon]  name  [BUILT-IN|CUSTOM] [ACTIVE|DISABLED] [DEPRECATED] [✓ trust] [v1.0.0]
 *           description (line-clamped)
 *           [tag] [tag] [tag]
 *           [chat] [agent]                                       [ⓘ] [🗑] [● toggle]
 *
 * Every block is guarded by an explicit presence check so a sparse
 * payload (custom skill with no tags, no author, no compatibility
 * block) still renders cleanly without empty placeholder rows.
 */
export function SkillCard({
  skill,
  isToggling,
  isDeleting,
  onToggle,
  onDelete,
  onOpenDetails,
  onOpenEditor,
}: SkillCardProps) {
  const isEnabled = skill.enabled;
  const isBuiltin = skill.is_builtin;
  const isDeprecated = skill.is_deprecated;
  const isVerified = skill.trust === "verified";

  const displayLabel = skill.display_name?.trim() || skill.name || skill.id;
  const description =
    skill.short_description?.trim() || skill.description?.trim() || "";

  const tags = Array.isArray(skill.tags) ? skill.tags : [];
  const hasTags = tags.length > 0;

  const hasCapabilities =
    skill.chat_invocable ||
    skill.agent_assignable ||
    skill.workflow_node;

  return (
    <div
      className={`flex items-start gap-4 p-4 rounded-sm border transition-all shadow-md ${
        !isEnabled
          ? "bg-muted/20 border-border/60 opacity-80"
          : "bg-card border-card hover:border-primary/20 hover:shadow-sm"
      }`}
    >
      {/* ── Leading icon ─────────────────────────────────────── */}
      <div className="shrink-0 p-0 flex items-center justify-center m-auto w-[50px] text-primary">
        <SkillIcon skill={skill} size={45} />
      </div>

      {/* ── Main content ─────────────────────────────────────── */}
      <div className="flex-1 min-w-0">
        {/* Header row: identity + actions */}
        <div className="flex items-start justify-between gap-3 mb-1.5">
          <div className="flex items-center gap-2 flex-wrap min-w-0">
            <h3
              className="text-sm font-bold text-foreground font-mono truncate"
              title={`${displayLabel} (${skill.id})`}
            >
              {displayLabel}
            </h3>

            {isBuiltin ? (
              <span className="px-1.5 py-0.5 text-[10px] font-mono uppercase bg-sky-500/10 text-sky-700 dark:text-sky-300 border border-sky-500/20">
                BUILT-IN
              </span>
            ) : (
              <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-primary/10 text-primary border border-primary/20">
                CUSTOM
              </span>
            )}

            {/*
              Router badge — rendered only when the backend reports
              the skill has at least one sub-skill declared. Uses a
              distinct violet color (matching the store's convention
              for composite/derived capabilities) so it is visually
              separable from the identity badges (BUILT-IN / CUSTOM)
              and the status badges (ACTIVE / DISABLED / DEPRECATED).

              The count in the badge matches ``sub_skills_count``,
              which mirrors the length of the on-disk ``sub_skills``
              array. No fetch is required — the field is present on
              every ``SkillSummary`` response from ``GET /api/skills``.
            */}
            {skill.has_sub_skills && (
              <span
                className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-violet-500/10 text-violet-700 dark:text-violet-300 border border-violet-500/20"
                title={`This skill routes to ${skill.sub_skills_count} sub-skill${
                  skill.sub_skills_count === 1 ? "" : "s"
                }.`}
              >
                ROUTER · {skill.sub_skills_count}
              </span>
            )}

            {!isEnabled && (
              <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-muted text-muted-foreground border border-border">
                DISABLED
              </span>
            )}

            {isDeprecated && (
              <span
                className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-amber-500/10 text-amber-700 dark:text-amber-400 border border-amber-500/30"
                title={
                  (skill as any).deprecation_message ||
                  "This skill is deprecated and will not be used by the runtime."
                }
              >
                DEPRECATED
              </span>
            )}

            {isVerified && (
              <span
                className="inline-flex items-center"
                title="Verified publisher"
              >
                <ShieldCheck size={13} className="text-emerald-500" />
              </span>
            )}

            {skill.version && (
              <span className="px-1.5 py-0.5 rounded text-[10px] font-mono text-muted-foreground border border-border bg-muted/50">
                v{skill.version}
              </span>
            )}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={() => onOpenDetails(skill)}
              className="p-1.5 rounded text-muted-foreground hover:text-primary hover:bg-primary/10 transition cursor-pointer"
              title="View details"
              aria-label="View skill details"
            >
              <Info size={14} />
            </button>

            {/* Edit button — rendered only when the skill is user-editable
                AND the parent supplied an editor callback. The backend
                already forces user_editable=false for built-ins, so no
                separate is_builtin check is required here. */}
            {skill.user_editable && onOpenEditor && (
              <button
                type="button"
                onClick={() => onOpenEditor(skill)}
                disabled={isDeleting}
                className="p-1.5 rounded text-muted-foreground hover:text-primary hover:bg-primary/10 transition cursor-pointer disabled:opacity-50"
                title="Edit instructions"
                aria-label="Edit skill instructions"
              >
                <Pencil size={14} />
              </button>
            )}

            {!isBuiltin && (
              <button
                type="button"
                onClick={() => onDelete(skill.id)}
                disabled={isDeleting}
                className="p-1.5 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition cursor-pointer disabled:opacity-50"
                title="Delete skill package"
                aria-label="Delete skill package"
              >
                <Trash2 size={14} />
              </button>
            )}

            <button
              type="button"
              onClick={() => onToggle(skill.id)}
              disabled={isToggling || isDeleting}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none disabled:opacity-60 ${
                isEnabled ? "bg-cgood" : "bg-[#777171]"
              }`}
              title={isEnabled ? "Disable skill" : "Enable skill"}
              aria-label={isEnabled ? "Disable skill" : "Enable skill"}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-background shadow-md ring-0 transition duration-200 ease-in-out ${
                  isEnabled ? "translate-x-4" : "translate-x-0"
                }`}
              />
            </button>
          </div>
        </div>

        {/* Description */}
        {description && (
          <p className="text-xs text-muted-foreground leading-relaxed mb-2 line-clamp-2">
            {description}
          </p>
        )}

        {/* Tags */}
        {hasTags && (
          <div className="flex flex-wrap gap-1.5 mb-2">
            {tags.slice(0, 8).map((tag, idx) => (
              <span
                key={`${tag}-${idx}`}
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono text-muted-foreground bg-muted border border-border"
              >
                <Tag size={9} className="opacity-60" />
                {tag}
              </span>
            ))}
            {tags.length > 8 && (
              <span className="text-[10px] text-muted-foreground self-center">
                +{tags.length - 8}
              </span>
            )}
          </div>
        )}

        {/* Capabilities footer */}
        {hasCapabilities && (
          <div className="flex items-center gap-2 flex-wrap">
            {skill.chat_invocable && (
              <CapabilityChip
                icon={Sparkles}
                label={`invoke /${skill.id}`}
                tone="active"
                title="Can be invoked from the chat composer with a slash command"
              />
            )}
            {skill.agent_assignable && (
              <CapabilityChip
                icon={Bot}
                label="agent-assignable"
                tone="active"
                title="Can be assigned to an agent from its Skills modal"
              />
            )}
            {skill.workflow_node && (
              <CapabilityChip
                icon={MessageSquare}
                label="workflow-node"
                tone="muted"
                title="Reserved: may be used as a workflow node in a future release"
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
