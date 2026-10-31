// frontend/src/app/stores/_modals/AgentSkillsModal.tsx

"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Check,
  Loader2,
  Save,
  Sparkles,
  Tag,
  X,
  XCircle,
} from "lucide-react";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "../_hooks/useStoreData";

// ────────────────────────────────────────────────────────────────
// Types mirror GET /api/store/agents/{id}/skills
// ────────────────────────────────────────────────────────────────

interface ResolvedSkill {
  id: string;
  resolved: boolean;
  name: string;
  version: string | null;
  enabled: boolean;
  is_deprecated: boolean;
  agent_assignable: boolean;
}

interface AvailableSkill {
  id: string;
  name: string;
  version: string;
  description: string;
  short_description: string;
  status: string;
  enabled: boolean;
  is_deprecated: boolean;
  agent_assignable: boolean;
  chat_invocable: boolean;
  in_manifest: boolean;
  tags: string[];
  has_icon_svg: boolean;
  has_icon_path: boolean;
}

interface AgentSkillsResponse {
  status: string;
  manifest_skills: string[];
  disabled_skills: string[];
  extra_skills: string[];
  effective_skills: string[];
  skills_editable: boolean;
  resolved_skills: ResolvedSkill[];
  available_skills: AvailableSkill[];
}

export interface AgentSkillsModalProps {
  open: boolean;
  agentId: string | null;
  agentName: string;
  onClose: () => void;
  onSaved?: () => void;
}

/**
 * Modal for overriding which skills an agent may use.
 *
 * Layout (mirrors AgentToolsModal):
 *   ┌─ Header: Sparkles + "Agent Skills" + agentName ─────────┐
 *   │                                                          │
 *   │  [Info banner — overrides apply to next task run]        │
 *   │                                                          │
 *   │  ── Manifest Skills ─────────────────────────────────    │
 *   │  [x] code_review        v1.0.0 · review,audit          │
 *   │  [ ] missing_skill      (not in registry)              │
 *   │                                                          │
 *   │  ── Available Store Skills ──────────────────────────    │
 *   │  [ ] test_planning      v2.0.0 · [disabled]            │
 *   │  [x] code_audit         v1.0.0 · assignable            │
 *   │                                                          │
 *   │  ── Effective Preview ───────────────────────────────    │
 *   │  3 skill(s): code_review, code_audit, ...                │
 *   │                                                          │
 *   └─ Cancel · Save Skills ────────────────────────────────────┘
 *
 * Checkbox semantics:
 *   - Manifest skill checked   → active (not in disabled_skills)
 *   - Manifest skill unchecked → excluded (added to disabled_skills)
 *   - Extra skill checked      → added (in extra_skills)
 *   - Extra skill unchecked    → ignored
 *
 * Assignability rules (enforced client-side; backend re-validates):
 *   - Only ``enabled === true`` AND ``agent_assignable === true``
 *     AND ``is_deprecated === false`` skills can be ADDED via extra.
 *   - Removal (disabled_skills) has no such constraint — you can
 *     always exclude a manifest skill.
 */
export function AgentSkillsModal({
  open,
  agentId,
  agentName,
  onClose,
  onSaved,
}: AgentSkillsModalProps) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [manifestSkills, setManifestSkills] = useState<string[]>([]);
  const [availableSkills, setAvailableSkills] = useState<AvailableSkill[]>([]);
  const [resolvedById, setResolvedById] = useState<
    Record<string, ResolvedSkill>
  >({});
  const [disabled, setDisabled] = useState<Set<string>>(new Set());
  const [extra, setExtra] = useState<Set<string>>(new Set());
  const [skillsEditable, setSkillsEditable] = useState(true);

  // ── Load on open ──────────────────────────────────────────────
  useEffect(() => {
    if (!open || !agentId) return;
    let cancelled = false;

    setLoading(true);
    setError(null);
    setManifestSkills([]);
    setAvailableSkills([]);
    setResolvedById({});
    setDisabled(new Set());
    setExtra(new Set());
    setSkillsEditable(true);

    (async () => {
      try {
        const res = await fetch(
          getApiUrl(`/api/store/agents/${encodeURIComponent(agentId)}/skills`)
        );
        if (cancelled) return;

        if (res.status === 404) {
          setError(`Agent '${agentId}' not found.`);
          return;
        }
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setError(
            (body && typeof body.detail === "string" && body.detail) ||
              `Failed to load skills (HTTP ${res.status}).`
          );
          return;
        }

        const data: AgentSkillsResponse = await res.json();

        const resolved: Record<string, ResolvedSkill> = {};
        for (const rs of data.resolved_skills || []) {
          resolved[rs.id] = rs;
        }

        setManifestSkills(data.manifest_skills || []);
        setAvailableSkills(data.available_skills || []);
        setResolvedById(resolved);
        setDisabled(new Set(data.disabled_skills || []));
        setExtra(new Set(data.extra_skills || []));
        setSkillsEditable(Boolean(data.skills_editable));
      } catch (exc: any) {
        if (cancelled) return;
        setError(exc?.message || "Network error while loading skills.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, agentId]);

  // ── Escape + body scroll lock ─────────────────────────────────
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, saving, onClose]);

  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  // ── Toggle handlers ───────────────────────────────────────────
  const toggleManifestSkill = useCallback((skillId: string) => {
    setDisabled((prev) => {
      const next = new Set(prev);
      if (next.has(skillId)) next.delete(skillId);
      else next.add(skillId);
      return next;
    });
  }, []);

  const toggleExtraSkill = useCallback((skillId: string) => {
    setExtra((prev) => {
      const next = new Set(prev);
      if (next.has(skillId)) next.delete(skillId);
      else next.add(skillId);
      return next;
    });
  }, []);

  // ── Derived lists ─────────────────────────────────────────────
  /** Manifest skill rows enriched with resolved metadata. */
  const manifestRows = useMemo(() => {
    const byId = new Map(availableSkills.map((s) => [s.id, s]));
    return manifestSkills.map((id) => {
      const meta = byId.get(id);
      const resolved = resolvedById[id];
      return {
        id,
        name: meta?.name ?? resolved?.name ?? id,
        version: meta?.version ?? resolved?.version ?? null,
        description:
          meta?.short_description || meta?.description || "",
        tags: meta?.tags ?? [],
        enabled: meta?.enabled ?? resolved?.enabled ?? false,
        is_deprecated:
          meta?.is_deprecated ?? resolved?.is_deprecated ?? false,
        inRegistry: Boolean(meta) || Boolean(resolved?.resolved),
      };
    });
  }, [manifestSkills, availableSkills, resolvedById]);

  /** Store skills not already in the manifest. */
  const extraCandidates = useMemo(
    () => availableSkills.filter((s) => !s.in_manifest),
    [availableSkills]
  );

  /** Live preview of what the runtime will use after pending toggles. */
  const effectiveSkills = useMemo(() => {
    const out: string[] = [];
    for (const id of manifestSkills) {
      if (!disabled.has(id)) out.push(id);
    }
    for (const id of extra) {
      if (!out.includes(id)) out.push(id);
    }
    return out;
  }, [manifestSkills, disabled, extra]);

  const disabledCount = disabled.size;
  const extraCount = extra.size;

  // ── Save ──────────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (!agentId) return;
    setSaving(true);
    setError(null);

    try {
      const res = await fetch(
        getApiUrl(
          `/api/store/agents/${encodeURIComponent(agentId)}/skills`
        ),
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            disabled_skills: Array.from(disabled),
            extra_skills: Array.from(extra),
          }),
        }
      );

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail =
          body && typeof body.detail === "object" && body.detail !== null
            ? (body.detail as any).error ||
              (body.detail as any).code ||
              "Save rejected."
            : typeof body.detail === "string"
            ? body.detail
            : `Failed to save (HTTP ${res.status}).`;
        setError(detail);
        showToast.error("Save Rejected", detail);
        return;
      }

      const data: AgentSkillsResponse = await res.json();
      setManifestSkills(data.manifest_skills || []);
      setDisabled(new Set(data.disabled_skills || []));
      setExtra(new Set(data.extra_skills || []));

      showToast.success(
        "Agent Skills Saved",
        `${agentName} updated — ${data.effective_skills?.length ?? 0} skill(s) active.`
      );

      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("peldrun:agent-skills-changed"));
      }

      onSaved?.();
      onClose();
    } catch (exc: any) {
      const msg = exc?.message || "Network error while saving.";
      setError(msg);
      showToast.error("Save Failed", msg);
    } finally {
      setSaving(false);
    }
  }, [agentId, disabled, extra, agentName, onSaved, onClose]);

  if (!open) return null;

  const canSave = !saving && !loading && !error && skillsEditable;

  // ── Render ────────────────────────────────────────────────────
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="agent-skills-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !saving) onClose();
      }}
    >
      <div className="bg-card rounded-md shadow-2xl w-full max-w-3xl max-h-[92vh] flex flex-col border border-border">
        {/* ── Header ──────────────────────────────────────────── */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0 bg-muted/40">
          <div className="flex items-center gap-2 min-w-0">
            <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
              <Sparkles size={15} />
            </div>
            <div className="min-w-0">
              <h2
                id="agent-skills-title"
                className="text-base font-bold text-foreground uppercase tracking-wide"
              >
                Agent Skills
              </h2>
              <p className="text-xs text-muted-foreground truncate">
                {agentName}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 disabled:opacity-50 transition"
            aria-label="Close skills"
          >
            <X size={18} />
          </button>
        </div>

        {/* ── Body ────────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto px-6 py-5 scrollbar-thin space-y-5">
          {loading && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 size={20} className="animate-spin mr-2" />
              <span className="text-sm">Loading skill assignment…</span>
            </div>
          )}

          {!loading && error && (
            <div className="flex items-start gap-3 p-4 bg-destructive/10 border border-destructive/30 rounded-sm">
              <AlertTriangle
                size={18}
                className="text-destructive shrink-0 mt-0.5"
              />
              <div className="text-sm text-foreground">{error}</div>
            </div>
          )}

          {!loading && !error && !skillsEditable && (
            <div className="flex items-start gap-2 p-3 rounded-sm border border-amber-500/40 bg-amber-500/5 text-xs text-foreground">
              <AlertTriangle
                size={14}
                className="text-amber-600 shrink-0 mt-0.5"
              />
              <span>
                This agent has <code className="font-mono">skills_editable: false</code> in its manifest.
                Skill assignment cannot be modified.
              </span>
            </div>
          )}

          {!loading && !error && (
            <>
              {/* Info banner */}
              <div className="flex items-start gap-2 p-3 rounded-sm border border-border bg-muted/30 text-[11px] text-muted-foreground">
                <AlertTriangle
                  size={12}
                  className="shrink-0 mt-0.5 text-primary"
                />
                <span>
                  Skill assignments apply to the <strong>next task run</strong>.
                  The manifest baseline stays unchanged — disabling a skill
                  excludes it from this agent's prompt injection; adding one
                  binds it to every run of this agent until removed.
                </span>
              </div>

              {/* ── Section 1: Manifest Skills ──────────────── */}
              <section>
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-[11px] uppercase tracking-wider font-bold text-foreground">
                    Manifest Skills
                  </h3>
                  <span className="text-[10px] font-mono text-muted-foreground">
                    {manifestRows.length - disabledCount} /{" "}
                    {manifestRows.length} active
                  </span>
                </div>

                {manifestRows.length === 0 && (
                  <p className="text-xs text-muted-foreground italic py-3">
                    This agent declares no manifest skills.
                  </p>
                )}

                <div className="space-y-1.5">
                  {manifestRows.map((row) => {
                    const isActive = !disabled.has(row.id);
                    return (
                      <button
                        key={row.id}
                        type="button"
                        onClick={() => toggleManifestSkill(row.id)}
                        disabled={!skillsEditable}
                        className={`w-full flex items-start gap-3 p-2.5 rounded-sm border text-left transition cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                          isActive
                            ? "bg-primary/5 border-primary/30 hover:bg-primary/10"
                            : "bg-muted/20 border-border hover:bg-muted/40"
                        }`}
                      >
                        <span
                          className={`mt-0.5 flex items-center justify-center shrink-0 w-4 h-4 rounded border ${
                            isActive
                              ? "bg-primary border-primary text-primary-foreground"
                              : "bg-background border-border"
                          }`}
                          aria-hidden="true"
                        >
                          {isActive && <Check size={10} />}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-xs font-mono font-semibold text-foreground truncate">
                              {row.id}
                            </span>
                            {row.version && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-muted border border-border text-muted-foreground">
                                v{row.version}
                              </span>
                            )}
                            {!row.inRegistry && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-amber-500/10 border border-amber-500/30 text-amber-700 dark:text-amber-300 inline-flex items-center gap-0.5">
                                <XCircle size={8} />
                                not in registry
                              </span>
                            )}
                            {row.is_deprecated && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-amber-500/10 border border-amber-500/30 text-amber-700 dark:text-amber-300">
                                deprecated
                              </span>
                            )}
                          </div>
                          {row.description && (
                            <p className="text-[10px] text-muted-foreground mt-0.5 line-clamp-1">
                              {row.description}
                            </p>
                          )}
                          {row.tags.length > 0 && (
                            <div className="flex flex-wrap gap-1 mt-1">
                              {row.tags.slice(0, 5).map((t, i) => (
                                <span
                                  key={`${t}-${i}`}
                                  className="inline-flex items-center gap-0.5 px-1 py-0.5 rounded text-[9px] font-mono bg-muted text-muted-foreground border border-border"
                                >
                                  <Tag size={7} />
                                  {t}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </section>

              {/* ── Section 2: Extra Skills from Store ──────── */}
              <section>
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-[11px] uppercase tracking-wider font-bold text-foreground">
                    Available Store Skills
                  </h3>
                  <span className="text-[10px] font-mono text-muted-foreground">
                    {extraCount} added
                  </span>
                </div>

                {extraCandidates.length === 0 && (
                  <p className="text-xs text-muted-foreground italic py-3">
                    No additional skills are installed and available to add.
                    Install skills from the Skills Library first.
                  </p>
                )}

                <div className="space-y-1.5">
                  {extraCandidates.map((sk) => {
                    const isAdded = extra.has(sk.id);
                    const canAdd =
                      sk.enabled &&
                      sk.agent_assignable &&
                      !sk.is_deprecated;

                    // Blocked skills render as disabled rows with a
                    // reason badge — the user can see them but
                    // cannot select them.
                    const blocked =
                      !canAdd && !isAdded;

                    return (
                      <button
                        key={sk.id}
                        type="button"
                        onClick={() => toggleExtraSkill(sk.id)}
                        disabled={blocked || !skillsEditable}
                        title={
                          blocked
                            ? sk.is_deprecated
                              ? "Deprecated skills cannot be assigned"
                              : !sk.enabled
                              ? "Disabled skills cannot be added — enable it first"
                              : !sk.agent_assignable
                              ? "This skill is not agent-assignable"
                              : undefined
                            : undefined
                        }
                        className={`w-full flex items-start gap-3 p-2.5 rounded-sm border text-left transition ${
                          blocked
                            ? "bg-muted/10 border-border/60 opacity-60 cursor-not-allowed"
                            : isAdded
                            ? "bg-emerald-500/5 border-emerald-500/30 hover:bg-emerald-500/10 cursor-pointer"
                            : "bg-muted/20 border-border hover:bg-muted/40 cursor-pointer"
                        } disabled:opacity-60`}
                      >
                        <span
                          className={`mt-0.5 flex items-center justify-center shrink-0 w-4 h-4 rounded border ${
                            isAdded
                              ? "bg-emerald-500 border-emerald-500 text-white"
                              : "bg-background border-border"
                          }`}
                          aria-hidden="true"
                        >
                          {isAdded && <Check size={10} />}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-xs font-mono font-semibold text-foreground truncate">
                              {sk.id}
                            </span>
                            <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-muted border border-border text-muted-foreground">
                              v{sk.version}
                            </span>
                            {sk.is_deprecated && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-amber-500/10 border border-amber-500/30 text-amber-700 dark:text-amber-300">
                                deprecated
                              </span>
                            )}
                            {!sk.enabled && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-muted border border-border text-muted-foreground">
                                disabled
                              </span>
                            )}
                            {!sk.agent_assignable && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-muted border border-border text-muted-foreground">
                                chat-only
                              </span>
                            )}
                            {sk.chat_invocable && sk.agent_assignable && sk.enabled && (
                              <span className="px-1 py-0.5 rounded text-[9px] font-mono uppercase bg-primary/10 border border-primary/20 text-primary">
                                assignable
                              </span>
                            )}
                          </div>
                          {sk.short_description && (
                            <p className="text-[10px] text-muted-foreground mt-0.5 line-clamp-1">
                              {sk.short_description}
                            </p>
                          )}
                          {sk.tags.length > 0 && (
                            <div className="flex flex-wrap gap-1 mt-1">
                              {sk.tags.slice(0, 5).map((t, i) => (
                                <span
                                  key={`${t}-${i}`}
                                  className="inline-flex items-center gap-0.5 px-1 py-0.5 rounded text-[9px] font-mono bg-muted text-muted-foreground border border-border"
                                >
                                  <Tag size={7} />
                                  {t}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </section>

              {/* ── Section 3: Effective Preview ────────────── */}
              <section className="pt-2 border-t border-border">
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-[11px] uppercase tracking-wider font-bold text-foreground">
                    Effective Preview
                  </h3>
                  <span className="text-[10px] font-mono text-primary">
                    {effectiveSkills.length} skill(s) at runtime
                  </span>
                </div>
                {effectiveSkills.length === 0 ? (
                  <p className="text-xs text-muted-foreground italic py-2">
                    No skills active. This agent will run without injected
                    skill instructions.
                  </p>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {effectiveSkills.map((id) => (
                      <span
                        key={id}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-mono bg-primary/10 border border-primary/30 text-primary"
                      >
                        <Sparkles size={9} />
                        {id}
                      </span>
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
        </div>

        {/* ── Footer ──────────────────────────────────────────── */}
        <div className="px-6 py-4 border-t border-border flex items-center justify-between gap-2 shrink-0">
          <span className="text-[10px] text-muted-foreground font-mono truncate">
            {manifestSkills.length} manifest · {disabledCount} disabled ·{" "}
            {extraCount} extra
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="px-4 py-2 text-sm text-muted-foreground hover:text-foreground transition disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={!canSave}
              className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-sm text-sm font-medium hover:opacity-90 disabled:opacity-50 transition"
            >
              {saving ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  Saving…
                </>
              ) : (
                <>
                  <Save size={14} />
                  Save Skills
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default AgentSkillsModal;
