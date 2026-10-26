// frontend/src/app/stores/_modals/ToolDetailsModal.tsx

"use client";

import React, { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Code2,
  Cpu,
  ExternalLink,
  FileText,
  Globe,
  Info,
  Key,
  Link as LinkIcon,
  Loader2,
  Lock,
  Package,
  Play,
  Settings,
  Shield,
  Tag,
  User,
  WifiOff,
  X,
  XCircle,
} from "lucide-react";
import type { ToolWithMeta } from "../_components/ToolCard";
import { ToolIcon } from "../_components/ToolIcon";

/* ────────────────────────────────────────────────────────────────
 * Public types
 * ──────────────────────────────────────────────────────────────── */

/**
 * Props for {@link ToolDetailsModal}.
 *
 * The modal is opened by the Tools section when the user clicks the
 * info button on any tool row. It renders the full manifest across
 * three logical views (Overview / Technical / Raw JSON).
 */
export interface ToolDetailsModalProps {
  open: boolean;
  tool: ToolWithMeta | null;
  /**
   * When true, the modal renders a small "loading" indicator next to the
   * title while the full manifest is being fetched in the background.
   * The already-loaded short data stays visible during this time, so the
   * user sees content immediately (progressive enhancement).
   */
  loading?: boolean;
  onClose: () => void;
}

type Tab = "overview" | "technical" | "raw";

/* ────────────────────────────────────────────────────────────────
 * Presentation helpers
 * ──────────────────────────────────────────────────────────────── */

/**
 * A single label/value row used throughout the modal.
 * The caller is expected to have verified the value exists.
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
 * Section container with icon + title. Sections are visually separated
 * by a top border except for the first one.
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
  variant?: "default" | "permission" | "language";
}) {
  const visible = items.slice(0, limit);
  const hidden = items.length - visible.length;

  const chipClass =
    variant === "permission"
      ? "bg-amber-500/10 border-amber-500/30 text-amber-700 dark:text-amber-300"
      : variant === "language"
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
 * Boolean badge used in the security overview.
 */
function BoolBadge({ label, value }: { label: string; value: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono border ${
        value
          ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-700 dark:text-emerald-300"
          : "bg-muted border-border text-muted-foreground"
      }`}
    >
      {value ? <CheckCircle2 size={10} /> : <XCircle size={10} />}
      {label}
    </span>
  );
}

/* ────────────────────────────────────────────────────────────────
 * Tab content components
 * ──────────────────────────────────────────────────────────────── */

/**
 * "Overview" tab — everything a user needs to *understand* the tool:
 * identity, author, publisher, license, links, classification, and a
 * concise security summary. No schemas or runnable examples here —
 * those live in the Technical tab.
 */
function OverviewTab({ t }: { t: ToolWithMeta }) {
  const tags = Array.isArray(t.tags) ? t.tags : [];
  const capabilities = Array.isArray(t.capabilities) ? t.capabilities : [];
  const languages = Array.isArray(t.supported_languages)
    ? t.supported_languages
    : [];
  const permissions = Array.isArray(t.permissions) ? t.permissions : [];
  const screenshots = Array.isArray(t.screenshots) ? t.screenshots : [];
  const replaces = Array.isArray(t.replaces) ? t.replaces : [];

  const hasDescription =
    !!t.short_description ||
    !!t.store_description ||
    !!t.description ||
    !!t.usage_notes;

  const hasAuthor =
    !!t.author?.name ||
    !!t.author?.email ||
    !!t.author?.url ||
    !!t.publisher_id ||
    !!t.trust ||
    !!t.signature;

  const hasVersionLicense =
    !!t.version ||
    !!t.release_date ||
    !!t.stability ||
    !!t.min_app_version ||
    !!t.license ||
    !!t.license_url ||
    !!t.pricing;

  const hasLinks =
    !!t.homepage ||
    !!t.docs_url ||
    !!t.demo_url ||
    !!t.changelog_url ||
    screenshots.length > 0;

  const hasClassification =
    !!t.category ||
    !!t.subcategory ||
    tags.length > 0 ||
    capabilities.length > 0 ||
    languages.length > 0;

  const hasSecurity =
    !!t.safety_level ||
    permissions.length > 0 ||
    t.network_access !== undefined ||
    t.sandbox !== undefined ||
    t.read_only !== undefined ||
    !!t.filesystem_access ||
    t.requires_api_key !== undefined ||
    (Array.isArray(t.required_env_vars) && t.required_env_vars.length > 0) ||
    !!t.rate_limit;

  const hasLifecycle =
    t.deprecated === true || replaces.length > 0 || !!t.replaced_by;

  const hasI18n = !!t.i18n && Object.keys(t.i18n).length > 0;

  return (
    <div>
      {/* Description */}
      {hasDescription && (
        <SectionBlock icon={FileText} title="Description">
          {t.short_description && (
            <p className="text-xs text-foreground mb-2 leading-relaxed">
              {t.short_description}
            </p>
          )}
          {t.store_description && (
            <p className="text-xs text-muted-foreground mb-2 leading-relaxed whitespace-pre-line">
              {t.store_description}
            </p>
          )}
          {!t.short_description && !t.store_description && t.description && (
            <p className="text-xs text-muted-foreground mb-2 leading-relaxed">
              {t.description}
            </p>
          )}
          {t.usage_notes && (
            <div className="mt-2 p-2.5 rounded-lg bg-muted/40 border border-border">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
                Usage Notes
              </div>
              <p className="text-[11px] text-foreground leading-relaxed whitespace-pre-line">
                {t.usage_notes}
              </p>
            </div>
          )}
        </SectionBlock>
      )}

      {/* Author & Publisher */}
      {hasAuthor && (
        <SectionBlock icon={User} title="Author & Publisher">
          {t.author?.name && (
            <DetailRow label="Author" value={t.author.name} />
          )}
          {t.author?.email && (
            <DetailRow
              label="Email"
              value={
                <a
                  href={`mailto:${t.author.email}`}
                  className="text-primary hover:underline"
                >
                  {t.author.email}
                </a>
              }
            />
          )}
          {t.author?.url && <DetailLink label="Website" href={t.author.url} />}
          {t.publisher_id && (
            <DetailRow label="Publisher ID" value={t.publisher_id} mono />
          )}
          {t.trust && (
            <DetailRow
              label="Trust"
              value={
                <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400 font-semibold capitalize">
                  <Shield size={11} />
                  {t.trust}
                </span>
              }
            />
          )}
          {t.signature && (
            <DetailRow
              label="Signature"
              value={
                <span className="block font-mono text-[10px] break-all text-muted-foreground bg-muted/40 border border-border rounded p-1.5 max-h-24 overflow-auto">
                  {t.signature}
                </span>
              }
            />
          )}
        </SectionBlock>
      )}

      {/* Version & License */}
      {hasVersionLicense && (
        <SectionBlock icon={Package} title="Version & License">
          {t.version && <DetailRow label="Version" value={t.version} mono />}
          {t.release_date && (
            <DetailRow label="Release Date" value={t.release_date} />
          )}
          {t.stability && <DetailRow label="Stability" value={t.stability} />}
          {t.min_app_version && (
            <DetailRow
              label="Min App Version"
              value={t.min_app_version}
              mono
            />
          )}
          {t.license && <DetailRow label="License" value={t.license} />}
          {t.license_url && (
            <DetailLink label="License URL" href={t.license_url} />
          )}
          {t.pricing && <DetailRow label="Pricing" value={t.pricing} />}
        </SectionBlock>
      )}

      {/* Links */}
      {hasLinks && (
        <SectionBlock icon={LinkIcon} title="Links">
          {t.homepage && <DetailLink label="Homepage" href={t.homepage} />}
          {t.docs_url && <DetailLink label="Docs" href={t.docs_url} />}
          {t.demo_url && <DetailLink label="Demo" href={t.demo_url} />}
          {t.changelog_url && (
            <DetailLink label="Changelog" href={t.changelog_url} />
          )}
          {screenshots.length > 0 && (
            <DetailRow
              label="Screenshots"
              value={
                <div className="flex flex-col gap-1">
                  {screenshots.map((s) => (
                    <a
                      key={s}
                      href={s}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-primary hover:underline text-[11px] break-all"
                    >
                      {s}
                    </a>
                  ))}
                </div>
              }
            />
          )}
        </SectionBlock>
      )}

      {/* Classification */}
      {hasClassification && (
        <SectionBlock icon={Tag} title="Classification">
          {t.category && (
            <DetailRow
              label="Category"
              value={
                t.subcategory
                  ? `${t.category} / ${t.subcategory}`
                  : t.category
              }
              mono
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
            <div className="mb-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
                Capabilities
              </div>
              <ChipsRow items={capabilities} />
            </div>
          )}
          {languages.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
                Supported Languages
              </div>
              <ChipsRow items={languages} variant="language" />
            </div>
          )}
        </SectionBlock>
      )}

      {/* Security Summary */}
      {hasSecurity && (
        <SectionBlock icon={Lock} title="Security Summary">
          {t.safety_level && (
            <DetailRow label="Safety Level" value={t.safety_level} mono />
          )}
          {permissions.length > 0 && (
            <div className="mb-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
                Permissions
              </div>
              <ChipsRow items={permissions} variant="permission" />
            </div>
          )}
          <div className="flex items-center gap-2 flex-wrap mb-2">
            {t.network_access !== undefined &&
              (t.network_access ? (
                <BoolBadge label="network access" value={true} />
              ) : (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono border bg-muted border-border text-muted-foreground">
                  <WifiOff size={10} />
                  no network access
                </span>
              ))}
            {t.sandbox !== undefined && (
              <BoolBadge label="sandbox" value={!!t.sandbox} />
            )}
            {t.read_only !== undefined && (
              <BoolBadge label="read only" value={!!t.read_only} />
            )}
            {t.requires_api_key !== undefined && (
              <span
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-mono border ${
                  t.requires_api_key
                    ? "bg-amber-500/10 border-amber-500/30 text-amber-700 dark:text-amber-300"
                    : "bg-muted border-border text-muted-foreground"
                }`}
              >
                <Key size={10} />
                {t.requires_api_key ? "requires API key" : "no API key"}
              </span>
            )}
          </div>
          {t.filesystem_access && (
            <DetailRow
              label="Filesystem"
              value={t.filesystem_access}
              mono
            />
          )}
          {Array.isArray(t.required_env_vars) &&
            t.required_env_vars.length > 0 && (
              <DetailRow
                label="Env Vars"
                value={<ChipsRow items={t.required_env_vars} />}
              />
            )}
          {t.rate_limit &&
            (t.rate_limit.requests !== undefined ||
              t.rate_limit.window_seconds !== undefined) && (
              <DetailRow
                label="Rate Limit"
                value={`${t.rate_limit.requests ?? "?"} requests per ${
                  t.rate_limit.window_seconds ?? "?"
                }s`}
                mono
              />
            )}
        </SectionBlock>
      )}

      {/* Lifecycle */}
      {hasLifecycle && (
        <SectionBlock icon={AlertTriangle} title="Lifecycle">
          {t.deprecated && (
            <div className="mb-2 p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-800 dark:text-amber-300">
              <div className="font-semibold text-[11px]">
                This tool is deprecated
              </div>
              {t.deprecation_message && (
                <div className="text-[11px] mt-0.5">
                  {t.deprecation_message}
                </div>
              )}
            </div>
          )}
          {replaces.length > 0 && (
            <div className="mb-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
                Replaces
              </div>
              <ChipsRow items={replaces} />
            </div>
          )}
          {t.replaced_by && (
            <DetailRow label="Replaced By" value={t.replaced_by} mono />
          )}
        </SectionBlock>
      )}

      {/* Translations */}
      {hasI18n && t.i18n && (
        <SectionBlock icon={Globe} title="Translations">
          <div className="space-y-2">
            {Object.entries(t.i18n).map(([lang, data]) => (
              <div
                key={lang}
                className="rounded-lg border border-border bg-muted/20 p-2.5"
              >
                <div className="text-[10px] uppercase tracking-wider font-bold text-primary mb-1">
                  {lang}
                </div>
                {data.display_name && (
                  <div className="text-xs text-foreground mb-0.5">
                    <span className="text-muted-foreground">
                      Display name:{" "}
                    </span>
                    {data.display_name}
                  </div>
                )}
                {data.short_description && (
                  <div className="text-[11px] text-muted-foreground leading-relaxed">
                    {data.short_description}
                  </div>
                )}
                {data.store_description && (
                  <div className="text-[11px] text-muted-foreground leading-relaxed mt-1">
                    {data.store_description}
                  </div>
                )}
              </div>
            ))}
          </div>
        </SectionBlock>
      )}
    </div>
  );
}

/**
 * "Technical" tab — everything a developer needs to *operate* the
 * tool: runtime contract, dependencies, parameters schema, settings
 * schema, and example invocations.
 */
function TechnicalTab({ t }: { t: ToolWithMeta }) {
  const dependencies = Array.isArray(t.dependencies) ? t.dependencies : [];
  const examples = Array.isArray(t.examples) ? t.examples : [];

  const hasRuntime =
    !!t.runtime ||
    !!t.entry_point ||
    !!t.timeout_seconds ||
    !!t.concurrency;

  // Extract settings primitives up-front so the JSX below can rely on
  // real booleans rather than `unknown &&` expressions.
  const settingsSchema = t.settings?.schema;
  const settingsDefaults = t.settings?.defaults;
  const hasSettingsSchema =
    settingsSchema !== undefined && settingsSchema !== null;
  const hasSettingsDefaults =
    settingsDefaults !== undefined && settingsDefaults !== null;
  const hasSettings = hasSettingsSchema || hasSettingsDefaults;

  const parameters = t.parameters;
  const hasParameters = parameters !== undefined && parameters !== null;

  const hasExamples = examples.length > 0;

  const hasContent =
    hasRuntime ||
    dependencies.length > 0 ||
    hasParameters ||
    hasSettings ||
    hasExamples;

  if (!hasContent) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <Cpu size={28} className="text-muted-foreground mb-2 opacity-60" />
        <p className="text-xs text-muted-foreground italic">
          No technical details available for this tool.
        </p>
      </div>
    );
  }

  return (
    <div>
      {/* Runtime */}
      {hasRuntime && (
        <SectionBlock icon={Cpu} title="Runtime">
          {t.runtime && (
            <DetailRow label="Runtime" value={t.runtime} mono />
          )}
          {t.entry_point && (
            <DetailRow label="Entry Point" value={t.entry_point} mono />
          )}
          {t.timeout_seconds && (
            <DetailRow
              label="Timeout"
              value={`${t.timeout_seconds}s`}
              mono
            />
          )}
          {t.concurrency && (
            <DetailRow
              label="Concurrency"
              value={String(t.concurrency)}
              mono
            />
          )}
        </SectionBlock>
      )}

      {/* Dependencies */}
      {dependencies.length > 0 && (
        <SectionBlock icon={Package} title="Dependencies">
          <ChipsRow items={dependencies} />
        </SectionBlock>
      )}

      {/* Parameters schema */}
      {hasParameters && (
        <SectionBlock icon={Code2} title="Parameters Schema">
          <JsonBlock value={parameters} />
        </SectionBlock>
      )}

      {/* Settings schema */}
      {hasSettings && (
        <SectionBlock icon={Settings} title="Settings">
          {hasSettingsSchema && (
            <div className="mb-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
                Schema
              </div>
              <JsonBlock value={settingsSchema} />
            </div>
          )}
          {hasSettingsDefaults && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
                Defaults
              </div>
              <JsonBlock value={settingsDefaults} />
            </div>
          )}
        </SectionBlock>
      )}

      {/* Examples */}
      {hasExamples && (
        <SectionBlock icon={Play} title="Examples">
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
function RawJsonTab({ t }: { t: ToolWithMeta }) {
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
      <JsonBlock value={t} />
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────
 * Main modal
 * ──────────────────────────────────────────────────────────────── */

/**
 * Full manifest viewer for a tool, organised into three tabs:
 *  - Overview  : author, publisher, license, links, classification
 *  - Technical : runtime, dependencies, parameters, settings, examples
 *  - Raw JSON  : the complete manifest
 *
 * The modal closes on Escape and on backdrop click. Body scrolling is
 * locked while open. The active tab resets to "overview" every time a
 * new tool is opened so the user always lands on the summary view.
 */
export function ToolDetailsModal({
  open,
  tool,
  loading = false,
  onClose,
}: ToolDetailsModalProps) {
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

  // Reset to overview whenever a new tool is opened.
  useEffect(() => {
    if (open) setActiveTab("overview");
  }, [open, tool?.id]);

  if (!open || !tool) return null;

  const t = tool;
  const isEnabled = t.is_enabled !== false && t.status !== "disabled";
  const isBuiltin = !!t.is_builtin;
  const displayName =
    t.display_name && t.display_name !== t.name ? t.display_name : null;
  const categoryLabel = t.subcategory
    ? `${t.category ?? ""} / ${t.subcategory}`
    : t.category;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`${t.name} details`}
    >
      <div
        className=" bg-background  rounded-md max-w-3xl w-full max-h-[92vh] flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ── Header ────────────────────────────────────────────── */}
        <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-border text-black bg-rb/10">
          <div className="flex items-start gap-3 min-w-0">
            <div className="p-2.5 shrink-0 flex items-center justify-center">
              <ToolIcon tool={t} size={20} />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2 min-w-0">
                <h2 className="text-base font-bold font-mono truncate">
                  {t.name}
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
                {categoryLabel && (
                  <span className="px-1.5 py-0.5 rounded-sm text-[10px] font-mono uppercase bg-rby/20 text-black border border-rby">
                    {categoryLabel}
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
                {t.version && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-mono text-muted-foreground border border-border bg-muted/50">
                    v{t.version}
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
                {t.deprecated && (
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
          {activeTab === "overview" && <OverviewTab t={t} />}
          {activeTab === "technical" && <TechnicalTab t={t} />}
          {activeTab === "raw" && <RawJsonTab t={t} />}
        </div>

        {/* ── Footer ────────────────────────────────────────────── */}
        <div className="px-6 py-3 border-t border-border bg-muted/20 flex items-center justify-between gap-3">
          <span className="text-[10px] text-muted-foreground font-mono truncate">
            ID: {t.id}
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
