// frontend/src/app/stores/_config/sections.tsx

import { Bot, LucideIcon, Plug, Puzzle, Sparkles, Wrench } from "lucide-react";

/**
 * Unique identifier for each store section.
 */
export type SectionId = "agents" | "tools" | "skills" | "providers" | "extensions";

/**
 * Sidebar / navigation metadata for a store section.
 */
export interface SectionMeta {
  id: SectionId;
  /** Full label used in headers and tooltips. */
  label: string;
  /** Short label used in the sidebar. */
  shortLabel: string;
  /** Short description shown in sidebar tooltips. */
  description: string;
  /** Lucide icon component. */
  icon: LucideIcon;
}

/**
 * A single button rendered in the unified store header.
 * Each section's header-config factory builds its own list.
 */
export interface HeaderAction {
  id: string;
  label: string;
  icon?: LucideIcon;
  variant?: "primary" | "outline" | "ghost";
  onClick: () => void;
  disabled?: boolean;
  loading?: boolean;
  iconClassName?: string;
}

/**
 * Contract the unified <StoreHeader /> relies on.
 * Every section must export a factory returning this shape.
 */
export interface SectionHeaderConfig {
  title: string;
  subtitle: string;
  badge?: string;
  actions: HeaderAction[];
}

/**
 * Ordered list of all store sections.
 * The first item is the default landing section.
 *
 * Section order rationale:
 *   - Agents first: it is the primary entry point for the product.
 *   - Tools second: fundamental capability layer.
 *   - Skills third: instructional packs assigned to agents or invoked
 *     from chat — sits naturally between Tools and MCP.
 *   - Providers fourth: LLM provider packages. Placed after Skills
 *     because it configures WHERE the model comes from (a
 *     foundational setting usually changed once), while Skills and
 *     Tools are content the user installs and swaps frequently.
 *   - Extensions (MCP) last: most advanced / least frequently used.
 */
export const SECTIONS: SectionMeta[] = [
  {
    id: "agents",
    label: "Agents Hub",
    shortLabel: "Agents",
    description: "Autonomous personas and specialist agents",
    icon: Bot,
  },
  {
    id: "tools",
    label: "Sovereign Tools",
    shortLabel: "Tools",
    description: "Local Python tools and installed bundles",
    icon: Wrench,
  },
  {
    id: "skills",
    label: "Skills Library",
    shortLabel: "Skills",
    description: "Reusable instruction packs for agents and chat",
    icon: Sparkles,
  },
  {
    id: "providers",
    label: "AI Providers",
    shortLabel: "Providers",
    description: "Install LLM provider packages (native, compatible, custom)",
    icon: Plug,
  },
  {
    id: "extensions",
    label: "Extensions & MCP",
    shortLabel: "Extensions",
    description: "MCP protocol servers and extensions",
    icon: Puzzle,
  },
];

export const DEFAULT_SECTION: SectionId = "agents";

/**
 * Look up section metadata by id.
 * Falls back to the first section if the id is unknown.
 */
export function getSectionMeta(id: SectionId): SectionMeta {
  return SECTIONS.find((section) => section.id === id) ?? SECTIONS[0];
}

/**
 * Type-guard to validate an arbitrary string as a SectionId.
 */
export function isSectionId(value: string): value is SectionId {
  return SECTIONS.some((section) => section.id === value);
}
