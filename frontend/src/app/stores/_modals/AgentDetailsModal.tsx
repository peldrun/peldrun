// frontend/src/app/stores/_modals/AgentDetailsModal.tsx

"use client";

import React, { useEffect, useMemo, useState } from "react";
import {
  Bot,
  CheckCircle2,
  Code2,
  ExternalLink,
  FileText,
  Globe,
  Info,
  Link as LinkIcon,
  Loader2,
  Package,
  Palette,
  Settings,
  Shield,
  Tag,
  User,
  Wrench,
  X,
  XCircle,
} from "lucide-react";
import { AgentIcon } from "../_components/AgentIcon";

/* ────────────────────────────────────────────────────────────────
 * Public types
 * ──────────────────────────────────────────────────────────────── */

/**
 * Full agent manifest surface accepted by the details modal.
 *
 * Mirrors the shape served by ``GET /api/store/agents/{id}``. Every
 * field beyond the identity block is optional because custom agents
 * uploaded by users may omit most metadata.
 */
export interface AgentWithMeta {
  // ── Identity (always present) ───────────────────────────────
  id: string;
  name: string;
  system_prompt: string;
  tools?: string[];
  max_steps?: number;

  // ── Descriptive ─────────────────────────────────────────────
  role?: string;
  icon?: string;
  core_color?: string;
  description?: string;
  short_description?: string;
  store_description?: string;
  usage_notes?: string;

  // ── Classification ──────────────────────────────────────────
  tags?: string[];
  capabilities?: string[];

  // ── Author & Publisher ──────────────────────────────────────
  author?: {
    name?: string;
    url?: string;
    email?: string;
  };
  publisher_id?: string;
  trust?: string;

  // ── Version & License ───────────────────────────────────────
  version?: string;
  release_date?: string;
  stability?: string;
  min_app_version?: string;
  changelog_url?: string;
  license?: string;
  license_url?: string;

  // ── Links ───────────────────────────────────────────────────
  homepage?: string;
  docs_url?: string;

  // ── Flags ───────────────────────────────────────────────────
  is_builtin?: boolean;
  status?: string;
  deprecated?: boolean;
  deprecation_message?: string | null;

  // ── Parameters & examples ───────────────────────────────────
  parameters?: unknown;
  examples?: Array<{
    input?: unknown;
    output_preview?: string;
  }>;
}

export interface AgentDetailsModalProps {
  open: boolean;
  agent: AgentWithMeta | null;
  /**
   * When true, a small loading indicator appears next to the title
   * while the full manifest is being fetched in the background. The
   * already-loaded short data stays visible (progressive enhancement).
   */
  loading?: boolean;
  onClose: () => void;
}

type Tab = "overview" | "technical" | "raw";

/* ────────────────────────────────────────────────────────────────
 * Presentation helpers (mirrors ToolDetailsModal)
 * ──────────────────────────────────────────────────────────────── */

/**
 * A single label/value row. The caller is expected to have verified
 * the value exists (no null checks inside).
 */
function DetailRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-start gap-3 py-1.5">
      <span className="w-36 shrink-0 text-[11px] uppercase tracking-wider text-muted-foreground font-semibold">
        {label}
      </span>
      <span
        className={`flex-1 text-xs text-foreground break-words ${
          mono ? "font-mono text-[11px]" : ""
        }`}
      >
        {value}
      </span>
    </div>
  );
}

/**
 * External link row with an outbound arrow.
 */
function DetailLink({ label, href }: { label: string; href: string }) {
  return (
    <DetailRow
      label={label}
      value={
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-primary hover:underline break-all"
        >
          <span className="truncate max-w-full">{href}</span>
          <ExternalLink size={11} className="shrink-0" />
        </a>
      }
    />
  );
}

/**
 * Section container with icon + title. Sections are visually
 * separated by a top border except for the first one.
 */
function SectionBlock({
  icon: Icon,
  title,
  children,
}: {
  icon: React.ComponentType<{ size?: number; className?: string }>;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border-t border-border pt-4 mt-4 first:border-0 first:pt-0 first:mt-0">
      <div className="flex items-center gap-2 mb-2">
        <Icon size={13} className="text-primary" />
        <h3 className="text-[11px] uppercase tracking-wider font-bold text-foreground">
          {title}
        </h3>
      </div>
      <div>{children}</div>
    </section>
  );
}

/**
 * Row of chips for an array of short strings with optional overflow.
 */
function ChipsRow({
  items,
  limit = 40,
  variant = "default",
}: {
  items: string[];
  limit?: number;
  variant?: "default" | "capability" | "tool";
}) {
  const visible = items.slice(0, limit);
  const hidden = items.length - visible.length;

  const chipClass =
    variant === "capability"
      ? "bg-purple-500/10 border-purple-500/30 text-purple-700 dark:text-purple-300"
      : variant === "tool"
      ? "bg-sky-500/10 border-sky-500/30 text-sky-700 dark:text-sky-300"
      : "bg-muted border-border text-muted-foreground";

  return (
    <div className="flex flex-wrap gap-1.5">
      {visible.map((item) => (
        <span
          key={item}
          className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono border ${chipClass}`}
        >
          {item}
        </span>
      ))}
      {hidden > 0 && (
        <span className="text-[10px] text-muted-foreground self-center">
          +{hidden}
        </span>
      )}
    </div>
  );
}

/**
 * Formatted JSON block with capped height and monospace font.
 */
function JsonBlock({ value }: { value: unknown }) {
  const text = useMemo(() => {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }, [value]);

  return (
    <pre className="p-3 rounded-lg bg-muted/40 border border-border text-[10px] font-mono text-foreground overflow-auto max-h-72 leading-relaxed whitespace-pre">
      {text}
    </pre>
  );
}

/**
 * Monospace block for the system prompt — same font treatment as a
 * JSON block but with a distinct visual accent so it reads as
 * "instructions" rather than "data".
 */
function PromptBlock({ text }: { text: string }) {
  return (
    <pre className="p-3 rounded-lg bg-primary/5 border border-primary/20 text-[11px] font-mono text-foreground overflow-auto max-h-96 leading-relaxed whitespace-pre-wrap">
      {text}
    </pre>
  );
}

/* ────────────────────────────────────────────────────────────────
 * Tab content components
 * ──────────────────────────────────────────────────────────────── */

/**
 * "Overview" tab — everything a user needs to understand the agent:
 * identity, author, license, links, classification. The system prompt
 * and tool bindings live in the Technical tab.
 */
function OverviewTab({ a }: { a: AgentWithMeta }) {
  const tags = Array.isArray(a.tags) ? a.tags : [];
  const capabilities = Array.isArray(a.capabilities) ? a.capabilities : [];

  const hasDescription =
    !!a.short_description || !!a.store_description || !!a.description;

  const hasAuthor =
    !!a.author?.name ||
    !!a.author?.email ||
    !!a.author?.url ||
    !!a.publisher_id ||
    !!a.trust;

  const hasVersionLicense =
    !!a.version ||
    !!a.release_date ||
    !!a.stability ||
    !!a.min_app_version ||
    !!a.license ||
    !!a.license_url;

  const hasLinks = !!a.homepage || !!a.docs_url || !!a.changelog_url;

  const hasClassification =
    !!a.role || tags.length > 0 || capabilities.length > 0;

  const hasLifecycle = a.deprecated === true;

  return (
    <div>
      {/* Description */}
      {hasDescription && (
        <SectionBlock icon={FileText} title="Description">
          {a.short_description && (
            <p className="text-xs text-foreground mb-2 leading-relaxed">
              {a.short_description}
            </p>
          )}
          {a.store_description && (
            <p className="text-xs text-muted-foreground mb-2 leading-relaxed whitespace-pre-line">
              {a.store_description}
            </p>
          )}
          {!a.short_description && !a.store_description && a.description && (
            <p className="text-xs text-muted-foreground leading-relaxed">
              {a.description}
            </p>
          )}
        </SectionBlock>
      )}

      {/* Author & Publisher */}
      {hasAuthor && (
        <SectionBlock icon={User} title="Author & Publisher">
          {a.author?.name && (
            <DetailRow label="Author" value={a.author.name} />
          )}
          {a.author?.email && (
            <DetailRow
              label="Email"
              value={
                <a
                  href={`mailto:${a.author.email}`}
                  className="text-primary hover:underline"
                >
                  {a.author.email}
                </a>
              }
            />
          )}
          {a.author?.url && <DetailLink label="Website" href={a.author.url} />}
          {a.publisher_id && (
            <DetailRow label="Publisher ID" value={a.publisher_id} mono />
          )}
          {a.trust && (
            <DetailRow
              label="Trust"
              value={
                <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400 font-semibold capitalize">
                  <Shield size={11} />
                  {a.trust}
                </span>
              }
            />
          )}
        </SectionBlock>
      )}

      {/* Version & License */}
      {hasVersionLicense && (
        <SectionBlock icon={Package} title="Version & License">
          {a.version && <DetailRow label="Version" value={a.version} mono />}
          {a.release_date && (
            <DetailRow label="Release Date" value={a.release_date} />
          )}
          {a.stability && <DetailRow label="Stability" value={a.stability} />}
          {a.min_app_version && (
            <DetailRow
              label="Min App Version"
              value={a.min_app_version}
              mono
            />
          )}
          {a.license && <DetailRow label="License" value={a.license} />}
          {a.license_url && (
            <DetailLink label="License URL" href={a.license_url} />
          )}
        </SectionBlock>
      )}

      {/* Links */}
      {hasLinks && (
        <SectionBlock icon={LinkIcon} title="Links">
          {a.homepage && <DetailLink label="Homepage" href={a.homepage} />}
          {a.docs_url && <DetailLink label="Docs" href={a.docs_url} />}
          {a.changelog_url && (
            <DetailLink label="Changelog" href={a.changelog_url} />
          )}
        </SectionBlock>
      )}

      {/* Classification */}
      {hasClassification && (
        <SectionBlock icon={Tag} title="Classification">
          {a.role && <DetailRow label="Role" value={a.role} />}
          {a.core_color && (
            <DetailRow
              label="Core Color"
              value={
                <span className="inline-flex items-center gap-1.5 font-mono text-[11px]">
                  <span
                    className="inline-block h-3 w-3 rounded border border-border"
                    style={{ backgroundColor: a.core_color }}
                    aria-hidden="true"
                  />
                  {a.core_color}
                </span>
              }
            />
          )}
          {tags.length > 0 && (
            <div className="mb-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
                Tags
              </div>
              <ChipsRow items={tags} />
            </div>
          )}
          {capabilities.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
                Capabilities
              </div>
              <ChipsRow items={capabilities} variant="capability" />
            </div>
          )}
        </SectionBlock>
      )}

      {/* Lifecycle */}
      {hasLifecycle && (
        <SectionBlock icon={XCircle} title="Lifecycle">
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-800 dark:text-amber-300">
            <div className="font-semibold text-[11px]">
              This agent is deprecated
            </div>
            {a.deprecation_message && (
              <div className="text-[11px] mt-0.5">
                {a.deprecation_message}
              </div>
            )}
          </div>
        </SectionBlock>
      )}
    </div>
  );
}

/**
 * "Technical" tab — everything a developer needs to understand how
 * the agent is wired: system prompt, tool bindings, parameters, and
 * example invocations.
 */
function TechnicalTab({ a }: { a: AgentWithMeta }) {
  const tools = Array.isArray(a.tools) ? a.tools : [];
  const examples = Array.isArray(a.examples) ? a.examples : [];

  const parameters = a.parameters;
  const hasParameters = parameters !== undefined && parameters !== null;
  const hasExamples = examples.length > 0;
  const hasPrompt = !!a.system_prompt && a.system_prompt.trim().length > 0;

  const hasContent =
    hasPrompt ||
    tools.length > 0 ||
    a.max_steps !== undefined ||
    hasParameters ||
    hasExamples;

  if (!hasContent) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <Code2 size={28} className="text-muted-foreground mb-2 opacity-60" />
        <p className="text-xs text-muted-foreground italic">
          No technical details available for this agent.
        </p>
      </div>
    );
  }

  return (
    <div>
      {/* System Prompt */}
      {hasPrompt && (
        <SectionBlock icon={Bot} title="System Prompt">
          <p className="text-[11px] text-muted-foreground mb-2 leading-relaxed">
            The authoritative behavioural instructions injected into every
            LLM call this agent makes.
          </p>
          <PromptBlock text={a.system_prompt} />
        </SectionBlock>
      )}

      {/* Runtime */}
      {(a.max_steps !== undefined || a.core_color !== undefined) && (
        <SectionBlock icon={Settings} title="Runtime">
          {a.max_steps !== undefined && (
            <DetailRow
              label="Max Steps"
              value={String(a.max_steps)}
              mono
            />
          )}
          {a.core_color && (
            <DetailRow
              label="Core Color"
              value={a.core_color}
              mono
            />
          )}
        </SectionBlock>
      )}

      {/* Tools */}
      {tools.length > 0 && (
        <SectionBlock icon={Wrench} title="Scoped Tools">
          <p className="text-[11px] text-muted-foreground mb-2 leading-relaxed">
            Only these tools are exposed to the agent at runtime. Any tool
            not in this list is hidden from the LLM's function-calling schema.
          </p>
          <ChipsRow items={tools} variant="tool" />
        </SectionBlock>
      )}

      {/* Parameters schema */}
      {hasParameters && (
        <SectionBlock icon={Code2} title="Parameters Schema">
          <JsonBlock value={parameters} />
        </SectionBlock>
      )}

      {/* Usage Notes */}
      {a.usage_notes && (
        <SectionBlock icon={Info} title="Usage Notes">
          <p className="text-[11px] text-foreground leading-relaxed whitespace-pre-line">
            {a.usage_notes}
          </p>
        </SectionBlock>
      )}

      {/* Examples */}
      {hasExamples && (
        <SectionBlock icon={Palette} title="Examples">
          <div className="space-y-2">
            {examples.map((ex, idx) => (
              <div
                key={idx}
                className="rounded-lg border border-border bg-muted/20 overflow-hidden"
              >
                <div className="px-2.5 py-1.5 bg-muted/40 border-b border-border text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">
                  Example {idx + 1}
                </div>
                <div className="p-2.5 space-y-2">
                  {ex.input !== undefined && ex.input !== null && (
                    <div>
                      <div className="text-[10px] text-muted-foreground font-semibold mb-1">
                        Input
                      </div>
                      <JsonBlock value={ex.input} />
                    </div>
                  )}
                  {ex.output_preview && (
                    <div>
                      <div className="text-[10px] text-muted-foreground font-semibold mb-1">
                        Output Preview
                      </div>
                      <pre className="p-2 rounded bg-background border border-border text-[10px] font-mono text-foreground overflow-auto max-h-32 whitespace-pre-wrap">
                        {ex.output_preview}
                      </pre>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </SectionBlock>
      )}
    </div>
  );
}

/**
 * "Raw JSON" tab — full manifest as JSON for advanced inspection.
 */
function RawJsonTab({ a }: { a: AgentWithMeta }) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <Info size={13} className="text-primary" />
        <h3 className="text-[11px] uppercase tracking-wider font-bold text-foreground">
          Complete Manifest
        </h3>
      </div>
      <p className="text-[11px] text-muted-foreground mb-2 leading-relaxed">
        The raw JSON payload exactly as returned by the backend. Useful for
        debugging or copying the manifest elsewhere.
      </p>
      <JsonBlock value={a} />
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────
 * Main modal
 * ──────────────────────────────────────────────────────────────── */

/**
 * Full manifest viewer for an agent, organised into three tabs:
 *  - Overview  : author, publisher, license, links, classification
 *  - Technical : system prompt, tools, parameters, examples
 *  - Raw JSON  : the complete manifest
 *
 * The modal closes on Escape and on backdrop click. Body scrolling is
 * locked while open. The active tab resets to "overview" every time a
 * new agent is opened so the user always lands on the summary view.
 */
export function AgentDetailsModal({
  open,
  agent,
  loading = false,
  onClose,
}: AgentDetailsModalProps) {
  const [activeTab, setActiveTab] = useState<Tab>("overview");

  // Close on Escape + lock body scroll while open.
  useEffect(() => {
    if (!open) return;

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [open, onClose]);

  // Reset to overview whenever a new agent is opened.
  useEffect(() => {
    if (open) setActiveTab("overview");
  }, [open, agent?.id]);

  if (!open || !agent) return null;

  const a = agent;
  const isEnabled = a.status !== "disabled";
  const isBuiltin = !!a.is_builtin;
  const displayName =
    a.role && a.role !== a.name ? a.role : null;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`${a.name} details`}
    >
      <div
        className="bg-background rounded-md max-w-3xl w-full max-h-[92vh] flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ── Header ────────────────────────────────────────────── */}
        <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-border">
          <div className="flex items-start gap-3 min-w-0">
            <div
              className="p-2.5 shrink-0 flex items-center justify-center rounded-lg"
              style={{
                backgroundColor: a.core_color
                  ? `${a.core_color}20`
                  : "rgba(16, 185, 129, 0.1)",
                color: a.core_color || "#10b981",
              }}
            >
              <AgentIcon
                agentId={a.id}
                iconName={a.icon}
                size={20}
              />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2 min-w-0">
                <h2 className="text-base font-bold font-mono truncate">
                  {a.name}
                </h2>
                {loading && (
                  <span
                    className="inline-flex items-center gap-1 text-[10px] font-mono text-muted-foreground shrink-0"
                    title="Loading full manifest..."
                  >
                    <Loader2 size={12} className="animate-spin text-primary" />
                    <span className="hidden sm:inline">loading…</span>
                  </span>
                )}
              </div>
              {displayName && (
                <p className="text-[11px] text-muted-foreground truncate">
                  {displayName}
                </p>
              )}
              <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                {a.role && (
                  <span className="px-1.5 py-0.5 rounded-sm text-[10px] font-mono uppercase bg-muted text-muted-foreground border border-border">
                    {a.role}
                  </span>
                )}
                {isBuiltin ? (
                  <span className="px-1.5 py-0.5 text-[10px] font-mono uppercase bg-sky-500/10 text-sky-700 dark:text-sky-300 border border-sky-500/20">
                    BUILT-IN
                  </span>
                ) : (
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-primary/10 text-primary border border-primary/20">
                    CUSTOM
                  </span>
                )}
                {a.version && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-mono text-muted-foreground border border-border bg-muted/50">
                    v{a.version}
                  </span>
                )}
                <span
                  className={`px-1.5 py-0.5 rounded text-[10px] font-mono uppercase ${
                    isEnabled
                      ? "bg-emerald-500/10 border border-emerald-500/30 text-emerald-700 dark:text-emerald-300"
                      : "bg-muted border border-border text-muted-foreground"
                  }`}
                >
                  {isEnabled ? "ACTIVE" : "DISABLED"}
                </span>
                {a.deprecated && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-amber-500/10 border border-amber-500/30 text-amber-700 dark:text-amber-300">
                    DEPRECATED
                  </span>
                )}
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition cursor-pointer shrink-0"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* ── Tab bar ───────────────────────────────────────────── */}
        <div className="border-b border-border bg-card px-6">
          <div className="flex items-center gap-1">
            {(
              [
                { id: "overview", label: "Overview" },
                { id: "technical", label: "Technical" },
                { id: "raw", label: "Raw JSON" },
              ] as Array<{ id: Tab; label: string }>
            ).map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={`px-3 py-2.5 text-xs font-semibold border-b-2 whitespace-nowrap transition-all cursor-pointer ${
                  activeTab === tab.id
                    ? "border-primary text-primary"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* ── Body ──────────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto p-6 text-xs bg-background">
          {activeTab === "overview" && <OverviewTab a={a} />}
          {activeTab === "technical" && <TechnicalTab a={a} />}
          {activeTab === "raw" && <RawJsonTab a={a} />}
        </div>

        {/* ── Footer ────────────────────────────────────────────── */}
        <div className="px-6 py-3 border-t border-border bg-muted/20 flex items-center justify-between gap-3">
          <span className="text-[10px] text-muted-foreground font-mono truncate">
            ID: {a.id}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="text-xs px-3 py-1.5 rounded-lg bg-muted hover:bg-muted/70 text-foreground border border-border transition cursor-pointer"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
