// frontend/src/app/stores/_sections/SkillsSection.tsx
"use client";

import React, { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  Loader2,
  RefreshCw,
  Sparkles,
  Upload,
} from "lucide-react";
import type {
  SkillCatalogInstallState,
  SkillCatalogItem,
  SkillFilter,
  SkillSummary,
} from "../_types/store";
import { SkillCard } from "../_components/SkillCard";
import { CatalogSkillCard } from "../_components/CatalogSkillCard";
import type { SectionHeaderConfig } from "../_config/sections";

/**
 * Which tab is currently active in the Skills section.
 *
 *   - ``"installed"`` (default): the classic list of installed skills,
 *     each with toggle/delete/edit/details actions.
 *   - ``"catalog"``: a browsable list of catalog items with one-click
 *     install. Only reachable when ``catalogEnabled`` is true.
 *
 * The tab state is intentionally local to this section (not lifted to
 * the page). Switching to another section unmounts the component and
 * resets the tab to "installed" on return, which matches the user's
 * mental model ("I come back to Skills and see what's installed").
 */
type TabId = "installed" | "catalog";


/**
 * Props for {@link SkillsSection}.
 *
 * All action handlers are forwarded from the page orchestrator,
 * which owns `useSkillActions`. The section itself is stateless —
 * every mutation is delegated upward.
 *
 * The catalog-related props are OPTIONAL-ISH: the section renders
 * identically to its pre-catalog version when ``catalogEnabled`` is
 * false (the tab bar is hidden, the installed tab renders normally).
 * The page is expected to pass all catalog props — the section does
 * not fetch anything on its own.
 */
export interface SkillsSectionProps {
  // ── Installed skills (unchanged) ─────────────────────────────
  skills: SkillSummary[];
  searchQuery: string;
  skillFilter: SkillFilter;
  togglingSkillId: string | null;
  deletingSkillId: string | null;
  onToggleSkill: (skillId: string) => void;
  onDeleteSkill: (skillId: string) => void;
  onOpenDetails: (skill: SkillSummary) => void;
  onOpenEditor: (skill: SkillSummary) => void;
  onOpenUpload: () => void;

  // ── Catalog ───────────────────────────────────────────────────
  /** Loaded catalog items, or empty until the first load completes. */
  catalogItems: SkillCatalogItem[];
  /** True during the initial catalog fetch. */
  isCatalogLoading: boolean;
  /** True during a user-triggered refresh (overrides the 24h TTL). */
  isCatalogRefreshing: boolean;
  /** Last catalog fetch error message, or ``null``. */
  catalogError: string | null;
  /** Whether the backend's kill switch allows the Catalog tab. */
  catalogEnabled: boolean;
  /** Item ID currently being installed (drives the card's spinner). */
  installingCatalogId: string | null;

  /** Lazy-loads the catalog; called the first time the tab is opened. */
  onLoadCatalog: () => Promise<void>;
  /** Forces a remote refresh (overrides the 24h TTL). */
  onRefreshCatalog: () => Promise<void>;
  /** Kicks off a catalog install (opens the install modal). */
  onInstallFromCatalog: (item: SkillCatalogItem) => Promise<void>;
  /** Opens the details view for a catalog item. */
  onOpenCatalogDetails: (item: SkillCatalogItem) => void;
}


/**
 * Build the section-specific header configuration for the Skills section.
 *
 * Unchanged by the catalog feature: the header button still opens the
 * manual ZIP upload modal. The Catalog tab is discoverable inside the
 * section itself via the tab bar, so a second header action would be
 * redundant.
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
 *   A tab bar (Installed / Catalog) sits at the top of the section,
 *   followed by a vertical list of {@link SkillCard} rows (Installed
 *   tab) or {@link CatalogSkillCard} rows (Catalog tab).
 *
 * Behavior changes from the pre-catalog version:
 *   - When ``catalogEnabled`` is false, the tab bar is hidden entirely
 *     and the Installed tab renders exactly as before.
 *   - The Catalog tab is loaded lazily on first open. It never blocks
 *     the Installed tab.
 *   - The search box filters BOTH tabs: on Installed it matches the
 *     skill fields; on Catalog it matches the catalog item fields.
 *     The ``skillFilter`` prop (builtin/custom) applies only to the
 *     Installed tab.
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
  onOpenEditor,
  onOpenUpload,
  catalogItems,
  isCatalogLoading,
  isCatalogRefreshing,
  catalogError,
  catalogEnabled,
  installingCatalogId,
  onLoadCatalog,
  onRefreshCatalog,
  onInstallFromCatalog,
  onOpenCatalogDetails,
}: SkillsSectionProps) {
  // ── Tab state ────────────────────────────────────────────────
  const [activeTab, setActiveTab] = useState<TabId>("installed");

  // ── Lazy catalog load ────────────────────────────────────────
  //
  // The catalog is loaded the first time the user clicks the Catalog
  // tab. The dependency array contains only ``activeTab`` (with an
  // exhaustive-deps disable) because re-running on ``catalogItems``
  // change would cause an infinite loop: loading the catalog sets
  // catalogItems to non-empty, which triggers another effect run,
  // which would... etc.
  //
  // An empty result is fine: if the load succeeds but returns zero
  // items, the effect will fire again the next time the user opens
  // the Catalog tab. That is a natural retry-on-revisit behavior.
  useEffect(() => {
    if (
      activeTab === "catalog" &&
      catalogItems.length === 0 &&
      !isCatalogLoading
    ) {
      void onLoadCatalog();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  // ── Installed tab filter (unchanged) ─────────────────────────
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

  // ── Catalog tab filter ───────────────────────────────────────
  //
  // Same search box, different fields. We intentionally ignore
  // ``skillFilter`` here (builtin/custom) because catalog items are
  // all "not yet installed" -- the distinction does not apply.
  const filteredCatalog = useMemo(() => {
    const q = searchQuery.toLowerCase().trim();
    if (!q) return catalogItems;
    return catalogItems.filter((c) => {
      return (
        c.id.toLowerCase().includes(q) ||
        (c.name || "").toLowerCase().includes(q) ||
        (c.display_name || "").toLowerCase().includes(q) ||
        (c.description || "").toLowerCase().includes(q) ||
        (c.tags || []).some((t) => t.toLowerCase().includes(q))
      );
    });
  }, [catalogItems, searchQuery]);

  // ── Installed index (for cross-reference) ────────────────────
  //
  // ``computeInstallState`` is called once per visible catalog card
  // per render. Building the map once and reusing it makes that
  // O(1) per card instead of O(n) -- relevant when a user has 100+
  // installed skills and is browsing a full catalog.
  const installedById = useMemo(() => {
    const map = new Map<string, SkillSummary>();
    for (const s of skills) map.set(s.id, s);
    return map;
  }, [skills]);

  const computeInstallState = (
    item: SkillCatalogItem,
  ): SkillCatalogInstallState => {
    const installed = installedById.get(item.id) ?? null;
    return {
      isInstalled: Boolean(installed),
      isEnabled: installed?.enabled ?? false,
      installedVersion: installed?.version ?? null,
      installedSkill: installed ?? null,
    };
  };

  // ── Tab bar ──────────────────────────────────────────────────
  //
  // Hidden entirely when the backend's kill switch is off. When
  // ``catalogEnabled`` is false, the section renders the Installed
  // tab exactly as it did before the catalog feature shipped.
  const tabBar = catalogEnabled ? (
    <div className="flex items-center justify-between gap-2 border-b mb-3">
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={() => setActiveTab("installed")}
          className={[
            "relative -mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium transition-colors",
            activeTab === "installed"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          ].join(" ")}
        >
          Installed
          <span className="inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold bg-muted text-muted-foreground">
            {skills.length}
          </span>
        </button>
        <button
          type="button"
          onClick={() => setActiveTab("catalog")}
          className={[
            "relative -mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium transition-colors",
            activeTab === "catalog"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          ].join(" ")}
        >
          Catalog
          {isCatalogLoading ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <span className="inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold bg-muted text-muted-foreground">
              {catalogItems.length}
            </span>
          )}
        </button>
      </div>
      {activeTab === "catalog" && (
        <button
          type="button"
          onClick={() => void onRefreshCatalog()}
          disabled={isCatalogRefreshing || isCatalogLoading}
          className={[
            "mb-1 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
            isCatalogRefreshing || isCatalogLoading
              ? "cursor-wait opacity-60"
              : "hover:bg-muted hover:border-primary/40",
          ].join(" ")}
        >
          <RefreshCw
            className={[
              "h-3 w-3",
              isCatalogRefreshing ? "animate-spin" : "",
            ].join(" ")}
          />
          {isCatalogRefreshing ? "Refreshing…" : "Refresh"}
        </button>
      )}
    </div>
  ) : null;

  // ── Catalog tab render ───────────────────────────────────────
  //
  // Four states: initial loading, initial error, empty, populated.
  // The tab bar stays visible in every state so the user can switch
  // back to Installed without losing orientation.
  if (activeTab === "catalog" && catalogEnabled) {
    if (isCatalogLoading && catalogItems.length === 0) {
      return (
        <div>
          {tabBar}
          <div className="flex flex-col items-center justify-center gap-2 py-12">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            <p className="text-xs text-muted-foreground">Loading catalog…</p>
          </div>
        </div>
      );
    }

    if (catalogError && catalogItems.length === 0) {
      return (
        <div>
          {tabBar}
          <div className="flex flex-col items-center justify-center gap-2 p-12 text-center rounded-2xl border border-dashed border-destructive/30 bg-destructive/5">
            <AlertCircle className="h-8 w-8 text-destructive" />
            <p className="text-sm font-medium text-destructive">
              Failed to load catalog
            </p>
            <p className="max-w-md text-xs text-muted-foreground">
              {catalogError}
            </p>
          </div>
        </div>
      );
    }

    if (filteredCatalog.length === 0) {
      return (
        <div>
          {tabBar}
          <div className="flex flex-col items-center justify-center gap-2 p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
            <Sparkles className="h-10 w-10 text-muted-foreground/50" />
            <p className="text-sm font-semibold text-foreground">
              {searchQuery.trim()
                ? "No matching catalog items"
                : "Catalog is empty"}
            </p>
            <p className="max-w-md text-xs text-muted-foreground">
              {searchQuery.trim()
                ? "Try adjusting your search."
                : "No skills are currently published in the catalog. You can still upload a .zip manually from the Installed tab."}
            </p>
          </div>
        </div>
      );
    }

    return (
      <div>
        {tabBar}
        <div className="flex flex-col gap-3">
          {filteredCatalog.map((item) => {
            // Cross-reference the installed skill for the
            // "View Installed" button. Reusing ``onOpenDetails``
            // here is correct: both handlers open the details
            // drawer, and the caller already owns both flows.
            const installed = installedById.get(item.id);
            return (
              <CatalogSkillCard
                key={item.id}
                item={item}
                installState={computeInstallState(item)}
                installing={installingCatalogId === item.id}
                onInstall={() => void onInstallFromCatalog(item)}
                onOpenDetails={() => onOpenCatalogDetails(item)}
                onOpenInstalled={
                  installed
                    ? () => onOpenDetails(installed)
                    : undefined
                }
              />
            );
          })}
        </div>
      </div>
    );
  }

  // ── Installed tab render (existing behavior) ─────────────────

  // Case 1: no skills at all.
  if (skills.length === 0) {
    return (
      <div>
        {tabBar}
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
      </div>
    );
  }

  // Case 2: skills exist, but the filter produced zero matches.
  if (filteredSkills.length === 0) {
    return (
      <div>
        {tabBar}
        <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
          <Sparkles className="h-10 w-10 text-muted-foreground mb-3" />
          <h3 className="text-sm font-semibold text-foreground">
            No skills match your criteria
          </h3>
          <p className="text-xs text-muted-foreground max-w-md mt-1">
            Try adjusting your search query or switching the filter.
          </p>
        </div>
      </div>
    );
  }

  // Case 3: normal list.
  return (
    <div>
      {tabBar}
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
            onOpenEditor={onOpenEditor}
          />
        ))}
      </div>
    </div>
  );
}
