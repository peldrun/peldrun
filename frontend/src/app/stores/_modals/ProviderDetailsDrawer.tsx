// frontend/src/app/stores/_modals/ProviderDetailsDrawer.tsx
"use client";

import React, { useMemo, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  Copy,
  Eye,
  FileCode2,
  HeartPulse,
  Layers,
  ListChecks,
  Plug,
  Radio,
  Shield,
  Sparkles,
  Trash2,
  Wrench,
  X,
  XCircle,
} from "lucide-react";
import type {
  ProviderHealthCheck,
  ProviderHealthReport,
  ProviderPackageEntry,
  ProviderPackageVersionRecord,
  ProviderTransport,
} from "../_types/store";

/**
 * Details drawer for a single installed Provider Package.
 *
 * Opens from the "Details" action on ``ProviderPackageCard``. Displays
 * everything the user needs to make a decision about the package:
 *
 *   • Overview: description, author, homepage, tags, source.
 *   • Versions: every installed version with an Activate action.
 *   • Features: the manifest's declared capability flags.
 *   • Permissions: what the package is allowed to do at runtime.
 *   • Manifest: the raw on-disk ``manifest.json``.
 *   • Health: filesystem checks on the active version.
 *
 * Tabs are rendered as plain buttons (not a real ARIA tablist) so
 * the drawer works identically inside a portal or a fixed drawer
 * without needing focus management infrastructure. Each tab's
 * content is rendered only when selected — the Manifest tab, for
 * instance, does not appear in the DOM until the user clicks it,
 * keeping the initial drawer paint cheap.
 *
 * The drawer is a controlled component: ``open`` and the data
 * (``provider``, ``health``) are owned by the caller
 * (``useProviderActions``). All actions (delete, toggle, activate
 * version) are delegated to props so this file stays purely
 * presentational.
 */
export interface ProviderDetailsDrawerProps {
  /** Whether the drawer is visible. */
  open: boolean;
  /**
   * The full detail state (provider + manifest + health), or
   * ``null`` when no drawer is open. The ``provider`` entry is
   * always present when ``open`` is true; ``manifest`` and
   * ``health`` may be null (not fetched yet, or unavailable).
   */
  details: {
    provider: ProviderPackageEntry;
    manifest: Record<string, unknown> | null;
    manifest_unavailable_reason?: string;
    health: ProviderHealthReport | null;
  } | null;
  /** True while the drawer's detail fetch is in flight. */
  isLoading: boolean;
  /** Called when the user dismisses the drawer. */
  onClose: () => void;
  /** Called when the user clicks Activate for a specific version. */
  onActivateVersion: (
    provider: ProviderPackageEntry,
    version: string,
  ) => void;
  /** Called when the user clicks Enable/Disable. */
  onToggleEnabled: (providerId: string) => void;
  /** Called when the user clicks Delete. */
  onDelete: (provider: ProviderPackageEntry) => void;
  /** Currently-busy flags for per-action spinners. */
  toggling: boolean;
  deleting: boolean;
  activating: boolean;
}


// ---------------------------------------------------------------------------
// Presentation helpers (reused across tabs)
// ---------------------------------------------------------------------------

const TRANSPORT_LABELS: Record<ProviderTransport, string> = {
  native: "Built-in",
  compatible: "Compatible",
  custom: "Custom",
};

const TRANSPORT_BADGE_CLASSES: Record<ProviderTransport, string> = {
  native:
    "bg-blue-500/10 text-blue-700 dark:text-blue-300 border-blue-500/20",
  compatible:
    "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/20",
  custom:
    "bg-purple-500/10 text-purple-700 dark:text-purple-300 border-purple-500/20",
};

/**
 * Human labels for every feature flag the manifest can declare.
 * Anything not in this map is rendered as-is with underscores
 * replaced by spaces — a defensive fallback for future additions.
 */
const FEATURE_LABELS: Record<string, string> = {
  model_discovery: "Model discovery",
  native_streaming: "Native streaming",
  buffered_streaming: "Buffered streaming",
  tool_calling: "Tool calling",
  parallel_tool_calls: "Parallel tool calls",
  forced_tool_choice: "Forced tool choice",
  vision: "Vision (images)",
  audio: "Audio input",
  structured_output: "Structured output",
  usage_reporting: "Usage reporting",
  reasoning: "Reasoning",
  prompt_caching: "Prompt caching",
};

/**
 * Human labels for every runtime permission a manifest can declare.
 * Mirrors ``omweb/provider_packages/schema.py::PermissionKind``.
 */
const PERMISSION_LABELS: Record<string, string> = {
  "network.connect":
    "Make outbound HTTP requests to the connection's endpoint",
  "secrets.read:connection":
    "Read the API key of its own connection",
  "models.discover": "Query the model discovery endpoint",
  "llm.generate": "Send LLM generation requests",
  "llm.stream": "Open streaming responses",
  "usage.read": "Read usage data for its own invocations",
  "workspace.read": "Read workspace files (forbidden)",
  "workspace.write": "Write workspace files (forbidden)",
  "shell.execute": "Spawn shell processes (forbidden)",
};

/**
 * Check if a permission is sensitive (needs a prominent warning).
 * Mirrors ``omweb/provider_packages/schema.py::SENSITIVE_PERMISSIONS``.
 */
function isSensitivePermission(perm: string): boolean {
  return (
    perm === "network.connect" || perm === "secrets.read:connection"
  );
}

/**
 * Format an ISO timestamp into a compact, human-readable form:
 * ``"2024-12-14 09:37"``. Never throws — an invalid timestamp is
 * returned unchanged.
 */
function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const pad = (n: number) => String(n).padStart(2, "0");
    return (
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}`
    );
  } catch {
    return iso;
  }
}

/**
 * Format a byte count as ``"1.2 MB"``.
 */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return unitIndex === 0
    ? `${Math.round(value)} ${units[unitIndex]}`
    : `${value.toFixed(1)} ${units[unitIndex]}`;
}

/**
 * Get the extracted permission strings from a manifest dict.
 * The manifest stores permissions as a nested object
 * (``{network, secrets, workspace_access, shell_execute}``); this
 * helper flattens it to the same string form the backend uses.
 */
function extractPermissionStrings(
  manifest: Record<string, unknown> | null,
): string[] {
  if (!manifest) return [];
  const perms = manifest.permissions;
  if (!perms || typeof perms !== "object") return [];

  const obj = perms as Record<string, unknown>;
  const out: string[] = [];

  if (obj.network === "connection_endpoint") {
    out.push("network.connect");
  }
  const secrets = obj.secrets;
  if (Array.isArray(secrets) && secrets.includes("connection.api_key")) {
    out.push("secrets.read:connection");
  }
  if (obj.workspace_access === true) {
    out.push("workspace.read");
    out.push("workspace.write");
  }
  if (obj.shell_execute === true) {
    out.push("shell.execute");
  }
  return out;
}

/**
 * Extract feature flags from the provider entry (already flattened
 * by the registry). Returns ``[key, enabled]`` pairs sorted by key
 * so the display order is stable across renders.
 */
function sortedFeatures(
  features: Record<string, boolean> | undefined,
): Array<[string, boolean]> {
  if (!features) return [];
  return Object.entries(features).sort(([a], [b]) => a.localeCompare(b));
}


// ---------------------------------------------------------------------------
// Tab types
// ---------------------------------------------------------------------------

type TabId =
  | "overview"
  | "versions"
  | "features"
  | "permissions"
  | "manifest"
  | "health";

const TABS: Array<{ id: TabId; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: "overview", label: "Overview", icon: Plug },
  { id: "versions", label: "Versions", icon: Layers },
  { id: "features", label: "Features", icon: Wrench },
  { id: "permissions", label: "Permissions", icon: Shield },
  { id: "manifest", label: "Manifest", icon: FileCode2 },
  { id: "health", label: "Health", icon: HeartPulse },
];


// ---------------------------------------------------------------------------
// Sub-tab components
// ---------------------------------------------------------------------------

function OverviewTab({
  provider,
}: {
  provider: ProviderPackageEntry;
}) {
  return (
    <div className="space-y-4">
      <Section title="Description">
        <p className="text-xs leading-relaxed text-muted-foreground">
          {provider.description?.trim() || "No description provided."}
        </p>
      </Section>

      <Section title="Identity">
        <div className="grid grid-cols-2 gap-3 text-xs">
          <Field label="Provider ID" value={provider.id} mono />
          <Field label="Active version" value={`v${provider.active_version}`} />
          <Field
            label="Transport"
            value={TRANSPORT_LABELS[provider.transport] ?? provider.transport}
          />
          <Field label="Source" value={provider.source} />
          {provider.api_contract && (
            <Field
              label="API contract"
              value={provider.api_contract}
              mono
              className="col-span-2"
            />
          )}
        </div>
      </Section>

      {(provider.author || provider.homepage) && (
        <Section title="Publisher">
          <div className="space-y-1.5 text-xs">
            {provider.author && (
              <p className="text-muted-foreground">
                <span className="font-medium text-foreground">Author:</span>{" "}
                {provider.author}
              </p>
            )}
            {provider.homepage && (
              <p className="text-muted-foreground">
                <span className="font-medium text-foreground">Homepage:</span>{" "}
                <a
                  href={provider.homepage}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-primary underline-offset-2 hover:underline"
                >
                  {provider.homepage}
                </a>
              </p>
            )}
          </div>
        </Section>
      )}

      {provider.tags.length > 0 && (
        <Section title="Tags">
          <div className="flex flex-wrap gap-1.5">
            {provider.tags.map((tag) => (
              <span
                key={tag}
                className="rounded-md bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
              >
                {tag}
              </span>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}


function VersionsTab({
  provider,
  activating,
  onActivateVersion,
}: {
  provider: ProviderPackageEntry;
  activating: boolean;
  onActivateVersion: (
    provider: ProviderPackageEntry,
    version: string,
  ) => void;
}) {
  if (provider.installed_versions.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No versions installed.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {provider.installed_versions.map((record) => (
        <VersionRow
          key={record.version}
          record={record}
          isActive={record.version === provider.active_version}
          activating={activating}
          onActivate={() => onActivateVersion(provider, record.version)}
        />
      ))}
    </div>
  );
}

function VersionRow({
  record,
  isActive,
  activating,
  onActivate,
}: {
  record: ProviderPackageVersionRecord;
  isActive: boolean;
  activating: boolean;
  onActivate: () => void;
}) {
  return (
    <div
      className={[
        "rounded-lg border p-3 transition-colors",
        isActive
          ? "border-emerald-500/40 bg-emerald-500/5"
          : "border-border bg-card",
      ].join(" ")}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs font-semibold text-foreground">
              v{record.version}
            </span>
            {isActive && (
              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-300">
                <CheckCircle2 className="h-2.5 w-2.5" />
                Active
              </span>
            )}
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Installed {formatTimestamp(record.installed_at)} ·{" "}
            {formatBytes(record.size_bytes)} · {record.file_count} file
            {record.file_count === 1 ? "" : "s"}
          </p>
          <p className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground/70">
            {record.hash.slice(0, 16)}…
          </p>
        </div>
        {!isActive && (
          <button
            type="button"
            onClick={onActivate}
            disabled={activating}
            className={[
              "shrink-0 rounded-md border px-2.5 py-1 text-[11px] font-medium transition-colors",
              activating
                ? "cursor-wait opacity-60"
                : "hover:border-primary/40 hover:bg-primary/5 hover:text-primary",
            ].join(" ")}
          >
            Activate
          </button>
        )}
      </div>
    </div>
  );
}


function FeaturesTab({
  features,
}: {
  features: Record<string, boolean> | undefined;
}) {
  const rows = sortedFeatures(features);
  if (rows.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        This package does not declare any features.
      </p>
    );
  }

  return (
    <div className="divide-y rounded-lg border">
      {rows.map(([key, enabled]) => {
        const label = FEATURE_LABELS[key] ?? key.replace(/_/g, " ");
        return (
          <div
            key={key}
            className="flex items-center justify-between gap-3 px-3 py-2"
          >
            <span className="text-xs text-foreground">{label}</span>
            {enabled ? (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
                <CheckCircle2 className="h-3 w-3" />
                Supported
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground">
                <XCircle className="h-3 w-3" />
                Not supported
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}


function PermissionsTab({
  manifest,
}: {
  manifest: Record<string, unknown> | null;
}) {
  const permissions = extractPermissionStrings(manifest);

  if (permissions.length === 0) {
    return (
      <div className="space-y-2">
        <p className="text-xs text-muted-foreground">
          This package declares no runtime permissions.
        </p>
        <p className="text-[11px] text-muted-foreground/70">
          Without <code className="rounded bg-muted px-1">network.connect</code>,
          every network request will be denied by the runtime.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {permissions.map((perm) => {
        const label = PERMISSION_LABELS[perm] ?? perm;
        const sensitive = isSensitivePermission(perm);
        return (
          <div
            key={perm}
            className={[
              "rounded-lg border p-3",
              sensitive
                ? "border-amber-500/30 bg-amber-500/5"
                : "border-border bg-card",
            ].join(" ")}
          >
            <div className="flex items-start gap-2">
              {sensitive ? (
                <Shield className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
              ) : (
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
              )}
              <div className="min-w-0 flex-1">
                <p className="font-mono text-[11px] font-medium text-foreground">
                  {perm}
                </p>
                <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                  {label}
                </p>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}


function ManifestTab({
  manifest,
  unavailableReason,
}: {
  manifest: Record<string, unknown> | null;
  unavailableReason?: string;
}) {
  const [copied, setCopied] = useState(false);

  const json = useMemo(() => {
    if (!manifest) return "";
    try {
      return JSON.stringify(manifest, null, 2);
    } catch {
      return "<manifest could not be serialized>";
    }
  }, [manifest]);

  const handleCopy = async () => {
    if (!json) return;
    try {
      await navigator.clipboard.writeText(json);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API can fail on insecure origins; silently ignore.
    }
  };

  if (!manifest) {
    return (
      <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
        <div className="flex items-start gap-2">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium text-amber-900 dark:text-amber-200">
              Manifest unavailable
            </p>
            <p className="mt-0.5 text-[11px] leading-relaxed text-amber-900/80 dark:text-amber-200/70">
              {unavailableReason ||
                "The on-disk manifest.json for the active version could not be read. The package may need to be reinstalled."}
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-end">
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <Copy className="h-3 w-3" />
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="max-h-[420px] overflow-auto rounded-lg border bg-muted/30 p-3 font-mono text-[10px] leading-relaxed text-foreground">
        {json}
      </pre>
    </div>
  );
}


function HealthTab({
  health,
}: {
  health: ProviderHealthReport | null;
}) {
  if (!health) {
    return (
      <p className="text-xs text-muted-foreground">
        Health report not available. The package may be uninstalled or
        the health endpoint could not be reached.
      </p>
    );
  }

  const statusStyles: Record<string, string> = {
    healthy:
      "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    degraded:
      "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
    broken:
      "border-destructive/30 bg-destructive/10 text-destructive",
  };

  const statusBadgeClass =
    statusStyles[health.status] ?? statusStyles.broken;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span
          className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${statusBadgeClass}`}
        >
          <HeartPulse className="h-3 w-3" />
          {health.status.charAt(0).toUpperCase() + health.status.slice(1)}
        </span>
        <span className="text-[11px] text-muted-foreground">
          v{health.version}
        </span>
      </div>

      <div className="divide-y rounded-lg border">
        {health.checks.map((check) => (
          <HealthCheckRow key={check.name} check={check} />
        ))}
      </div>
    </div>
  );
}

function HealthCheckRow({ check }: { check: ProviderHealthCheck }) {
  return (
    <div className="flex items-start gap-2 px-3 py-2">
      {check.passed ? (
        <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
      ) : (
        <XCircle className="mt-0.5 h-3 w-3 shrink-0 text-destructive" />
      )}
      <div className="min-w-0 flex-1">
        <p className="font-mono text-[11px] font-medium text-foreground">
          {check.name}
        </p>
        <p className="mt-0.5 truncate text-[10px] text-muted-foreground">
          {check.message || "—"}
        </p>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Shared layout primitives
// ---------------------------------------------------------------------------

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground/80">
        {title}
      </h4>
      {children}
    </section>
  );
}

function Field({
  label,
  value,
  mono = false,
  className = "",
}: {
  label: string;
  value: string;
  mono?: boolean;
  className?: string;
}) {
  return (
    <div className={className}>
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground/70">
        {label}
      </p>
      <p
        className={[
          "mt-0.5 text-xs text-foreground",
          mono ? "font-mono" : "",
        ].join(" ")}
      >
        {value}
      </p>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Main drawer
// ---------------------------------------------------------------------------

export function ProviderDetailsDrawer({
  open,
  details,
  isLoading,
  onClose,
  onActivateVersion,
  onToggleEnabled,
  onDelete,
  toggling,
  deleting,
  activating,
}: ProviderDetailsDrawerProps) {
  const [activeTab, setActiveTab] = useState<TabId>("overview");

  if (!open) return null;

  // While loading, the drawer shows a skeleton. This happens on the
  // very first open (before the fetch resolves) and after activation
  // (which re-fetches to refresh the health report).
  if (isLoading || !details) {
    return (
      <DrawerShell onClose={onClose}>
        <div className="flex h-full items-center justify-center p-6">
          <div className="space-y-3 text-center">
            <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
            <p className="text-xs text-muted-foreground">
              Loading provider details…
            </p>
          </div>
        </div>
      </DrawerShell>
    );
  }

  const { provider, manifest, manifest_unavailable_reason, health } = details;
  const displayName = provider.display_name || provider.name || provider.id;

  return (
    <DrawerShell onClose={onClose}>
      {/* ── Header ─────────────────────────────────────────────── */}
      <header className="border-b px-5 py-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-3">
            <div
              className={[
                "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg",
                TRANSPORT_BADGE_CLASSES[provider.transport] ??
                  TRANSPORT_BADGE_CLASSES.native,
              ].join(" ")}
              aria-hidden="true"
            >
              <Plug className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <h2
                className="truncate text-sm font-semibold text-foreground"
                title={displayName}
              >
                {displayName}
              </h2>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                  v{provider.active_version}
                </span>
                <span
                  className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${
                    TRANSPORT_BADGE_CLASSES[provider.transport] ??
                    TRANSPORT_BADGE_CLASSES.native
                  }`}
                >
                  {TRANSPORT_LABELS[provider.transport] ??
                    provider.transport}
                </span>
                {!provider.enabled && (
                  <span className="inline-flex items-center rounded-full border border-muted-foreground/20 bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                    Disabled
                  </span>
                )}
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close details"
            className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </header>

      {/* ── Tab navigation ─────────────────────────────────────── */}
      <nav
        aria-label="Provider details tabs"
        className="flex overflow-x-auto border-b bg-muted/20 px-2 py-1 scrollbar-thin"
      >
        {TABS.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              aria-current={isActive ? "page" : undefined}
              className={[
                "inline-flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
                isActive
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              ].join(" ")}
            >
              <Icon className="h-3.5 w-3.5" />
              {tab.label}
            </button>
          );
        })}
      </nav>

      {/* ── Tab content ────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto scrollbar-thin px-5 py-4">
        {activeTab === "overview" && (
          <OverviewTab provider={provider} />
        )}
        {activeTab === "versions" && (
          <VersionsTab
            provider={provider}
            activating={activating}
            onActivateVersion={onActivateVersion}
          />
        )}
        {activeTab === "features" && (
          <FeaturesTab features={provider.features} />
        )}
        {activeTab === "permissions" && (
          <PermissionsTab manifest={manifest} />
        )}
        {activeTab === "manifest" && (
          <ManifestTab
            manifest={manifest}
            unavailableReason={manifest_unavailable_reason}
          />
        )}
        {activeTab === "health" && <HealthTab health={health} />}
      </div>

      {/* ── Footer actions ─────────────────────────────────────── */}
      <footer className="flex items-center justify-between gap-2 border-t bg-muted/20 px-5 py-3">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onToggleEnabled(provider.id)}
            disabled={toggling || deleting || activating}
            className={[
              "rounded-md border px-3 py-1.5 text-xs font-medium transition-colors",
              toggling
                ? "cursor-wait opacity-60"
                : "hover:bg-muted hover:border-primary/40",
            ].join(" ")}
          >
            {provider.enabled ? "Disable" : "Enable"}
          </button>
        </div>
        <button
          type="button"
          onClick={() => onDelete(provider)}
          disabled={toggling || deleting || activating}
          className={[
            "inline-flex items-center gap-1.5 rounded-md border border-destructive/30 px-3 py-1.5 text-xs font-medium transition-colors",
            deleting
              ? "cursor-wait opacity-60"
              : "text-destructive hover:bg-destructive/10",
          ].join(" ")}
        >
          <Trash2 className="h-3.5 w-3.5" />
          {deleting ? "Deleting…" : "Delete"}
        </button>
      </footer>
    </DrawerShell>
  );
}


// ---------------------------------------------------------------------------
// Shared shell
// ---------------------------------------------------------------------------

/**
 * The fixed-position drawer container. Handles backdrop, width, and
 * the outer flex layout shared by the loading skeleton and the
 * fully-loaded view.
 */
function DrawerShell({
  onClose,
  children,
}: {
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="provider-details-title"
      className="fixed inset-0 z-50 flex justify-end"
    >
      {/* Backdrop */}
      <button
        type="button"
        aria-label="Close details"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-black/50 backdrop-blur-sm"
        tabIndex={-1}
      />

      {/* Drawer panel */}
      <div className="relative z-10 flex h-full w-full max-w-md flex-col border-l bg-card shadow-2xl">
        {children}
      </div>
    </div>
  );
}

export default ProviderDetailsDrawer;
