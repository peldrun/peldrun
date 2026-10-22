// frontend/src/app/stores/_components/StoreToolbar.tsx

"use client";

import React from "react";
import { Search, X } from "lucide-react";
import type { SectionId } from "../_config/sections";
import type { AgentFilter, ToolFilter } from "../_types/store";

/**
 * A single filter option rendered inside the segmented control.
 */
export interface ToolbarFilterOption<T extends string> {
  value: T;
  label: string;
}

/**
 * Props for the unified store toolbar.
 *
 * The toolbar is stateless: the parent owns the search query and the
 * active filter for the current section. When a section does not use
 * a filter (e.g. Extensions) the corresponding prop can be omitted.
 */
export interface StoreToolbarProps {
  activeSection: SectionId;

  // Search
  searchQuery: string;
  onSearchChange: (value: string) => void;

  // Agents filter (only rendered when activeSection === "agents")
  agentFilter?: AgentFilter;
  onAgentFilterChange?: (value: AgentFilter) => void;

  // Tools filter (only rendered when activeSection === "tools")
  toolFilter?: ToolFilter;
  onToolFilterChange?: (value: ToolFilter) => void;
}

const AGENT_FILTER_OPTIONS: ToolbarFilterOption<AgentFilter>[] = [
  { value: "all", label: "All" },
  { value: "builtin", label: "Built-in" },
  { value: "custom", label: "Custom" },
];

const TOOL_FILTER_OPTIONS: ToolbarFilterOption<ToolFilter>[] = [
  { value: "all", label: "All" },
  { value: "builtin", label: "Built-in" },
  { value: "custom", label: "Custom" },
];

/**
 * Small reusable segmented control used for both agent and tool filters.
 * Kept generic so future sections can reuse it with new filter types.
 */
function SegmentedFilter<T extends string>({
  value,
  onChange,
  options,
  accentClass = "text-primary",
}: {
  value: T;
  onChange: (v: T) => void;
  options: ToolbarFilterOption<T>[];
  accentClass?: string;
}) {
  return (
    <div className="flex items-center p-0.5 rounded-lg bg-muted border border-border text-[11px]">
      {options.map((opt) => {
        const isActive = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className={`px-2.5 py-1 rounded-md transition cursor-pointer ${
              isActive
                ? `bg-card shadow-xs font-semibold ${accentClass}`
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * Unified toolbar row: search input + section-specific filter.
 *
 * Renders the correct filter automatically based on `activeSection`.
 * Extensions intentionally have no filter so only the search input is
 * shown for that section, matching the original page design.
 */
export function StoreToolbar({
  activeSection,
  searchQuery,
  onSearchChange,
  agentFilter = "all",
  onAgentFilterChange,
  toolFilter = "all",
  onToolFilterChange,
}: StoreToolbarProps) {
  return (
    <div className="w-full border-b border-border bg-card">
      <div className="max-w-[1240px] mx-auto w-full px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row sm:items-center justify-between gap-3 py-2.5">
        {/* Left spacer keeps the search aligned with the content area
            when the sidebar is present on md+. */}
        <div className="hidden sm:block text-[11px] font-mono text-muted-foreground">
          {activeSection === "agents" && "Browse autonomous personas"}
          {activeSection === "tools" && "Local + bundled capabilities"}
          {activeSection === "extensions" && "MCP server integrations"}
        </div>

        {/* Search input */}
        <div className="flex items-center gap-2.5 w-full sm:w-auto">
          <div className="relative w-full sm:w-64">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground h-3.5 w-3.5" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
              placeholder={`Search ${activeSection}...`}
              className="w-full pl-8 pr-7 py-1.5 text-xs rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary shadow-xs"
            />
            {searchQuery && (
              <button
                type="button"
                onClick={() => onSearchChange("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground cursor-pointer"
                aria-label="Clear search"
              >
                <X size={12} />
              </button>
            )}
          </div>

          {/* Section-specific filter */}
          {activeSection === "agents" && onAgentFilterChange && (
            <SegmentedFilter<AgentFilter>
              value={agentFilter}
              onChange={onAgentFilterChange}
              options={AGENT_FILTER_OPTIONS}
            />
          )}

          {activeSection === "tools" && onToolFilterChange && (
            <SegmentedFilter<ToolFilter>
              value={toolFilter}
              onChange={onToolFilterChange}
              options={TOOL_FILTER_OPTIONS}
            />
          )}
        </div>
      </div>
    </div>
  );
}
