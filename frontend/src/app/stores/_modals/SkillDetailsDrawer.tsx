// frontend/src/app/stores/_modals/SkillDetailsDrawer.tsx

"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  BookOpen,
  Bot,
  CheckCircle2,
  ExternalLink,
  FileText,
  Loader2,
  Network,
  ShieldCheck,
  Sparkles,
  Tag,
  X,
} from "lucide-react";
import type {
  SkillPreview,
  SkillSummary,
  SubSkillSummary,
} from "../_types/store";
import { getApiUrl } from "../_hooks/useStoreData";

export interface SkillDetailsDrawerProps {
  open: boolean;
  skill: SkillSummary | null;
  onClose: () => void;
}

/**
 * Slide-in drawer showing the full details of a skill:
 *
 *   - Header: name, version, trust badge, capability chips
 *   - Left column: description, tags, capabilities, examples
 *   - Right column: metadata (author, license, links)
 *   - Bottom: the SKILL.md instructions text (collapsible, loaded on
 *     open via /api/skills/{id}/preview)
 *
 * The drawer is read-only by design — actions (toggle, delete) live
 * in the SkillCard so the drawer is safe to open without worrying
 * about accidental state mutation.
 */
export function SkillDetailsDrawer({
  open,
  skill,
  onClose,
}: SkillDetailsDrawerProps) {
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [instructionsExpanded, setInstructionsExpanded] = useState(false);

  // Sub-skills state — only populated for router skills. A non-router
  // skill never triggers a fetch; the section itself is not rendered.
  const [subSkills, setSubSkills] = useState<SubSkillSummary[]>([]);
  const [loadingSubSkills, setLoadingSubSkills] = useState(false);
  const [subSkillsError, setSubSkillsError] = useState<string | null>(null);

  // Ref so the fetch effect can cancel in-flight requests when the
  // drawer closes or the user switches to another skill mid-load.
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open || !skill) {
      // Cancel any in-flight request when the drawer closes.
      abortRef.current?.abort();
      abortRef.current = null;
      return;
    }

    // Reset state on every open so switching skills does not leak
    // the previous skill's preview OR sub-skills list.
    setPreview(null);
    setPreviewError(null);
    setInstructionsExpanded(false);
    setLoadingPreview(true);

    setSubSkills([]);
    setSubSkillsError(null);
    setLoadingSubSkills(Boolean(skill.has_sub_skills));

    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    // ── Preview fetch (always) ────────────────────────────────────
    (async () => {
      try {
        const res = await fetch(
          getApiUrl(`/api/skills/${encodeURIComponent(skill.id)}/preview`),
          { signal: ctrl.signal }
        );
        if (!res.ok) {
          if (res.status === 404) {
            setPreviewError("This skill's instructions could not be loaded.");
          } else {
            const err = await res.json().catch(() => ({}));
            const detail =
              (err && typeof err.detail === "string" && err.detail) ||
              `HTTP ${res.status}`;
            setPreviewError(detail);
          }
          return;
        }
        const data: SkillPreview = await res.json();
        setPreview(data);
      } catch (e: any) {
        if (e?.name === "AbortError") return;
        setPreviewError(e?.message || "Failed to load instructions.");
      } finally {
        setLoadingPreview(false);
      }
    })();

    // ── Sub-skills fetch (router skills only) ─────────────────────
    // Skipped entirely for non-router skills — the flag from the
    // list response is authoritative and saves one HTTP round-trip
    // for the ~95% case.
    if (skill.has_sub_skills) {
      (async () => {
        try {
          const res = await fetch(
            getApiUrl(
              `/api/skills/${encodeURIComponent(skill.id)}/sub-skills`
            ),
            { signal: ctrl.signal }
          );
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            const detail =
              (err?.detail && typeof err.detail === "object"
                ? err.detail.error
                : err?.detail) || `HTTP ${res.status}`;
            throw new Error(detail);
          }
          const data = await res.json();
          const list: SubSkillSummary[] = Array.isArray(data?.sub_skills)
            ? data.sub_skills
            : [];
          setSubSkills(list);
        } catch (e: any) {
          if (e?.name === "AbortError") return;
          setSubSkillsError(e?.message || "Failed to load sub-skills.");
        } finally {
          setLoadingSubSkills(false);
        }
      })();
    }

    return () => {
      ctrl.abort();
    };
  }, [open, skill]);

  // ESC to close.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || !skill) return null;

  const displayName = skill.display_name?.trim() || skill.name || skill.id;
  const shortDesc =
    skill.short_description?.trim() || skill.description?.trim() || "";
  const longDesc =
    (skill as any).store_description ||
    skill.description ||
    "";
  const author = skill.author || {};
  const tags = Array.isArray(skill.tags) ? skill.tags : [];
  const isVerified = skill.trust === "verified";

  const manifest = (skill as any).manifest || {};
  const examples = Array.isArray(manifest.examples) ? manifest.examples : [];
  const capabilityFlags = {
    chat_invocable: Boolean(skill.chat_invocable),
    agent_assignable: Boolean(skill.agent_assignable),
    workflow_node: Boolean(skill.workflow_node),
  };

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-background/80 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-2xl h-full bg-card shadow-2xl flex flex-col border-l border-border animate-in slide-in-from-right duration-200">
        {/* Header */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between bg-muted/20 shrink-0">
          <button
            type="button"
            onClick={onClose}
            className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition cursor-pointer"
          >
            <X size={16} /> Close
          </button>
          <span className="text-[11px] font-mono text-muted-foreground">
            {skill.id}
          </span>
        </div>

        {/* Body (scrollable) */}
        <div className="flex-1 overflow-y-auto scrollbar-thin">
          <div className="px-8 py-6 max-w-2xl mx-auto">
            {/* Title area */}
            <div className="mb-6">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h1 className="text-2xl font-bold text-foreground flex items-center gap-2">
                    {displayName}
                    {isVerified && (
                      <span title="Verified publisher" className="inline-flex">
                        <ShieldCheck size={20} className="text-emerald-500" />
                      </span>
                    )}
                  </h1>
                  <p className="text-[13px] font-mono text-muted-foreground mt-1">
                    {skill.id} · v{skill.version}
                  </p>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  {skill.is_builtin ? (
                    <span className="px-2 py-0.5 rounded text-[10px] font-mono uppercase bg-sky-500/10 text-sky-700 dark:text-sky-300 border border-sky-500/20">
                      BUILT-IN
                    </span>
                  ) : (
                    <span className="px-2 py-0.5 rounded text-[10px] font-mono uppercase bg-primary/10 text-primary border border-primary/20">
                      CUSTOM
                    </span>
                  )}
                  <span
                    className={`px-2 py-0.5 rounded text-[10px] font-mono uppercase ${
                      skill.enabled
                        ? "bg-emerald-500/10 text-emerald-600 border border-emerald-500/30"
                        : "bg-muted text-muted-foreground border border-border"
                    }`}
                  >
                    {skill.enabled ? "ACTIVE" : "DISABLED"}
                  </span>
                </div>
              </div>
              {shortDesc && (
                <p className="text-sm text-muted-foreground mt-3 leading-relaxed">
                  {shortDesc}
                </p>
              )}
            </div>

            {/* Capability chips */}
            <div className="flex flex-wrap gap-1.5 mb-6">
              {capabilityFlags.chat_invocable && (
                <span className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] bg-primary/10 border border-primary/30 text-primary">
                  <Sparkles size={11} />
                  Chat invocable (<code className="font-mono">/{skill.id}</code>)
                </span>
              )}
              {capabilityFlags.agent_assignable && (
                <span className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] bg-primary/10 border border-primary/30 text-primary">
                  <Bot size={11} />
                  Agent assignable
                </span>
              )}
              {capabilityFlags.workflow_node && (
                <span className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] bg-muted border border-border text-muted-foreground">
                  Workflow node (reserved)
                </span>
              )}
              {skill.is_deprecated && (
                <span className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] bg-amber-500/10 border border-amber-500/30 text-amber-700 dark:text-amber-400">
                  Deprecated
                </span>
              )}
            </div>

            {/* ── Sub-skills (router skills only) ─────────────────
                Rendered only when the skill declares at least one
                sub-skill. For non-router skills this entire block
                is skipped, so the drawer layout is unchanged for
                the ~95% case. */}
            {skill.has_sub_skills && (
              <section className="mb-8">
                <div className="flex items-center justify-between mb-3">
                  <h2 className="text-xs font-bold text-foreground uppercase tracking-wider flex items-center gap-1.5">
                    <Network size={13} />
                    Sub-Skills
                    <span className="ml-1 px-1.5 py-0.5 rounded text-[10px] font-mono bg-violet-500/10 text-violet-700 dark:text-violet-300 border border-violet-500/20">
                      {skill.sub_skills_count}
                    </span>
                  </h2>
                  <span className="text-[10px] font-mono text-muted-foreground">
                    Loaded via <code>load_sub_skill</code>
                  </span>
                </div>

                {loadingSubSkills && (
                  <div className="flex items-center gap-2 text-xs text-muted-foreground py-3">
                    <Loader2 size={14} className="animate-spin" />
                    Loading sub-skills…
                  </div>
                )}

                {!loadingSubSkills && subSkillsError && (
                  <div className="text-xs text-destructive p-3 rounded-md border border-destructive/30 bg-destructive/5">
                    {subSkillsError}
                  </div>
                )}

                {!loadingSubSkills && !subSkillsError && subSkills.length > 0 && (
                  <div className="flex flex-col gap-2">
                    {subSkills.map((sub) => (
                      <div
                        key={sub.id}
                        className="p-3 rounded-md border border-border bg-muted/20 hover:bg-muted/30 transition"
                      >
                        <div className="flex items-center justify-between gap-2 mb-1">
                          <code className="text-[12px] font-mono font-semibold text-foreground">
                            {sub.id}
                          </code>
                          {sub.capabilities?.exclusive && (
                            <span
                              className="px-1.5 py-0.5 rounded text-[9px] font-mono uppercase bg-amber-500/10 text-amber-700 dark:text-amber-400 border border-amber-500/20"
                              title="Must be loaded alone (exclusive)"
                            >
                              EXCLUSIVE
                            </span>
                          )}
                        </div>
                        <p className="text-xs text-muted-foreground leading-relaxed mb-2">
                          {sub.description}
                        </p>
                        {Array.isArray(sub.triggers) && sub.triggers.length > 0 && (
                          <div className="flex flex-wrap gap-1">
                            {sub.triggers.map((trig, i) => (
                              <span
                                key={`${trig}-${i}`}
                                className="px-1.5 py-0.5 rounded text-[10px] font-mono text-muted-foreground bg-background border border-border"
                              >
                                {trig}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {!loadingSubSkills && !subSkillsError && subSkills.length === 0 && (
                  <div className="text-xs text-muted-foreground p-3 rounded-md border border-dashed border-border bg-muted/10">
                    No sub-skills declared in the manifest.
                  </div>
                )}
              </section>
            )}

            {/* Two-column: About + Metadata */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-8 mb-8">
              {/* Left column: About */}
              <div className="md:col-span-2 space-y-6">
                {longDesc && (
                  <section>
                    <h2 className="text-xs font-bold text-foreground uppercase tracking-wider mb-2">
                      About
                    </h2>
                    <p className="text-sm text-muted-foreground leading-relaxed whitespace-pre-wrap">
                      {longDesc}
                    </p>
                  </section>
                )}

                {tags.length > 0 && (
                  <section>
                    <h2 className="text-xs font-bold text-foreground uppercase tracking-wider mb-2">
                      Tags
                    </h2>
                    <div className="flex flex-wrap gap-1.5">
                      {tags.map((t) => (
                        <span
                          key={t}
                          className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-mono text-muted-foreground bg-muted border border-border"
                        >
                          <Tag size={9} />
                          {t}
                        </span>
                      ))}
                    </div>
                  </section>
                )}

                {examples.length > 0 && (
                  <section>
                    <h2 className="text-xs font-bold text-foreground uppercase tracking-wider mb-2">
                      Examples
                    </h2>
                    <div className="space-y-2">
                      {examples.map((ex: any, i: number) => (
                        <div
                          key={i}
                          className="p-3 rounded-md border border-border bg-muted/20 text-xs"
                        >
                          {ex?.description && (
                            <div className="font-semibold text-foreground mb-1">
                              {ex.description}
                            </div>
                          )}
                          {ex?.invocation && (
                            <div className="font-mono text-[11px] text-primary mb-1">
                              {ex.invocation}
                            </div>
                          )}
                          {ex?.context && (
                            <p className="text-muted-foreground mb-1">
                              {ex.context}
                            </p>
                          )}
                          {ex?.expected_outcome && (
                            <p className="text-muted-foreground italic">
                              → {ex.expected_outcome}
                            </p>
                          )}
                        </div>
                      ))}
                    </div>
                  </section>
                )}
              </div>

              {/* Right column: Metadata */}
              <div className="space-y-5">
                {author.name && (
                  <div>
                    <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider mb-1">
                      Author
                    </h4>
                    {author.url ? (
                      <a
                        href={author.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-sm text-primary hover:underline inline-flex items-center gap-1"
                      >
                        {author.name} <ExternalLink size={11} />
                      </a>
                    ) : (
                      <p className="text-sm text-foreground">{author.name}</p>
                    )}
                  </div>
                )}

                {skill.license && (
                  <div>
                    <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider mb-1">
                      License
                    </h4>
                    <p className="text-sm text-foreground">{skill.license}</p>
                  </div>
                )}

                {skill.release_date && (
                  <div>
                    <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider mb-1">
                      Released
                    </h4>
                    <p className="text-sm text-foreground font-mono">
                      {skill.release_date}
                    </p>
                  </div>
                )}

                {skill.stability && (
                  <div>
                    <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider mb-1">
                      Stability
                    </h4>
                    <p className="text-sm text-foreground capitalize">
                      {skill.stability}
                    </p>
                  </div>
                )}

                {(skill.homepage || skill.docs_url) && (
                  <div>
                    <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider mb-1">
                      Links
                    </h4>
                    <div className="flex flex-col gap-1.5 text-xs">
                      {skill.homepage && (
                        <a
                          href={skill.homepage}
                          target="_blank"
                          rel="noreferrer"
                          className="text-primary hover:underline inline-flex items-center gap-1"
                        >
                          Homepage <ExternalLink size={10} />
                        </a>
                      )}
                      {skill.docs_url && (
                        <a
                          href={skill.docs_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-primary hover:underline inline-flex items-center gap-1"
                        >
                          Documentation <ExternalLink size={10} />
                        </a>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Instructions preview */}
            <section className="border-t border-border pt-6">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-xs font-bold text-foreground uppercase tracking-wider flex items-center gap-1.5">
                  <BookOpen size={13} />
                  Instructions
                </h2>
                {preview && (
                  <span className="text-[10px] font-mono text-muted-foreground">
                    {preview.instructions_length.toLocaleString()} chars
                  </span>
                )}
              </div>

              {loadingPreview && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground py-4">
                  <Loader2 size={14} className="animate-spin" />
                  Loading instructions…
                </div>
              )}

              {!loadingPreview && previewError && (
                <div className="text-xs text-destructive p-3 rounded-md border border-destructive/30 bg-destructive/5">
                  {previewError}
                </div>
              )}

              {!loadingPreview && preview && (
                <div>
                  <pre
                    className={`p-3 rounded-md border border-border bg-background text-[11px] font-mono text-foreground overflow-x-auto whitespace-pre-wrap break-words leading-relaxed ${
                      instructionsExpanded
                        ? "max-h-none"
                        : "max-h-64 overflow-hidden"
                    }`}
                  >
                    {preview.instructions}
                  </pre>
                  {preview.instructions_length > 1200 && (
                    <button
                      type="button"
                      onClick={() =>
                        setInstructionsExpanded((v) => !v)
                      }
                      className="mt-2 text-xs text-primary hover:underline cursor-pointer"
                    >
                      {instructionsExpanded
                        ? "Show less"
                        : "Show full instructions"}
                    </button>
                  )}
                </div>
              )}
            </section>
          </div>
        </div>

        {/* Footer note */}
        <div className="p-4 border-t border-border bg-muted/20 shrink-0">
          <p className="text-[10px] text-muted-foreground leading-relaxed text-center">
            Skills are instructional only. They cannot execute code or
            access the network beyond what the agent's tools already
            allow. Enable or disable a skill from the Skills Library.
          </p>
        </div>
      </div>
    </div>
  );
}
