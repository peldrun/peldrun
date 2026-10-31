// frontend/src/app/stores/_sections/SkillsSection.tsx

"use client";

import React, { useMemo } from "react";
import { Sparkles, Upload } from "lucide-react";
import type { SkillSummary, SkillFilter } from "../_types/store";
import { SkillCard } from "../_components/SkillCard";
import type { SectionHeaderConfig } from "../_config/sections";

/**
 * Props for {@link SkillsSection}.
 *
 * All action handlers are forwarded from the page orchestrator,
 * which owns `useSkillActions`. The section itself is stateless —
 * every mutation is delegated upward.
 */
export interface SkillsSectionProps {
  skills: SkillSummary[];
  searchQuery: string;
  skillFilter: SkillFilter;
  togglingSkillId: string | null;
  deletingSkillId: string | null;
  onToggleSkill: (skillId: string) => void;
  onDeleteSkill: (skillId: string) => void;
  onOpenDetails: (skill: SkillSummary) => void;
  onOpenUpload: () => void;
}

/**
 * Build the section-specific header configuration for the Skills section.
 *
 * A single action: install a skill from a .zip package. The runtime
 * filters installed skills by capability flags; the header does not
 * expose those — they belong in the details drawer.
 */
export function getSkillsHeaderConfig(ctx: {
  onInstallSkill: () => void;
}): SectionHeaderConfig {
  return {
    title: "Skills Library",
    subtitle: "Reusable instruction packs for agents and chat",
    badge: "SKILLS",
    actions: [
      {
        id: "install-skill",
        label: "Install Skill (.zip)",
        icon: Upload,
        variant: "primary",
        onClick: ctx.onInstallSkill,
      },
    ],
  };
}

/**
 * Skills section content.
 *
 * Layout:
 *   A vertical list of {@link SkillCard} rows, matching the Tools
 *   section's list layout. Skills carry dense metadata (tags,
 *   capabilities, trust) that reads better as a row than as a card.
 *
 * Filtering:
 *   1. Free-text search matches across id, name, display_name,
 *      description, and tags — case-insensitive.
 *   2. Section filter narrows by origin: ``builtin`` vs ``custom``.
 *
 * The empty state distinguishes between "no skills installed at all"
 * (encourages the install action) and "no skills match the filter"
 * (encourages clearing the search).
 */
export function SkillsSection({
  skills,
  searchQuery,
  skillFilter,
  togglingSkillId,
  deletingSkillId,
  onToggleSkill,
  onDeleteSkill,
  onOpenDetails,
  onOpenUpload,
}: SkillsSectionProps) {
  const filteredSkills = useMemo(() => {
    const q = searchQuery.toLowerCase().trim();
    return skills.filter((sk) => {
      if (skillFilter === "builtin" && !sk.is_builtin) return false;
      if (skillFilter === "custom" && sk.is_builtin) return false;

      if (!q) return true;

      const haystacks: string[] = [
        sk.id,
        sk.name,
        sk.display_name,
        sk.description,
        sk.short_description,
        ...(Array.isArray(sk.tags) ? sk.tags : []),
      ].filter((s): s is string => typeof s === "string");

      return haystacks.some((s) => s.toLowerCase().includes(q));
    });
  }, [skills, searchQuery, skillFilter]);

  // ── Empty states ───────────────────────────────────────────────
  if (skills.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
        <Sparkles className="h-10 w-10 text-muted-foreground mb-3" />
        <h3 className="text-sm font-semibold text-foreground">
          No skills installed yet
        </h3>
        <p className="text-xs text-muted-foreground max-w-md mt-1 mb-4">
          Skills are reusable instruction packs. Install one to invoke it
          from chat with <code className="font-mono">/skill_id</code>, or
          assign it to an agent so its guidance runs on every task.
        </p>
        <button
          type="button"
          onClick={onOpenUpload}
          className="inline-flex items-center gap-1.5 px-3.5 py-1.5 bg-primary text-primary-foreground rounded-md text-xs font-medium hover:opacity-90 transition cursor-pointer"
        >
          <Upload size={13} />
          Install Skill (.zip)
        </button>
      </div>
    );
  }

  if (filteredSkills.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
        <Sparkles className="h-10 w-10 text-muted-foreground mb-3" />
        <h3 className="text-sm font-semibold text-foreground">
          No skills match your criteria
        </h3>
        <p className="text-xs text-muted-foreground max-w-md mt-1">
          Try adjusting your search query or switching the filter.
        </p>
      </div>
    );
  }

  // ── List ──────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-3">
      {filteredSkills.map((sk) => (
        <SkillCard
          key={sk.id}
          skill={sk}
          isToggling={togglingSkillId === sk.id}
          isDeleting={deletingSkillId === sk.id}
          onToggle={onToggleSkill}
          onDelete={onDeleteSkill}
          onOpenDetails={onOpenDetails}
        />
      ))}
    </div>
  );
}
