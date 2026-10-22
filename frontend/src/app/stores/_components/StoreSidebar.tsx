// frontend/src/app/stores/_components/StoreSidebar.tsx

"use client";

import React from "react";
import { SECTIONS, SectionId } from "../_config/sections";

/**
 * Props for the unified store sidebar.
 *
 * The parent page owns:
 *  - which section is active
 *  - per-section item counts (agents / tools / extensions)
 *
 * This component is purely presentational and stateless.
 */
export interface StoreSidebarProps {
  activeSection: SectionId;
  onSectionChange: (id: SectionId) => void;
  /** Map of section id → item count to render as a badge. */
  counts: Record<SectionId, number>;
}

/**
 * Unified vertical sidebar for the store page.
 *
 * Behaviour:
 *  - On `md+` renders as a fixed-width vertical rail on the left.
 *  - On smaller viewports collapses into a horizontal scrollable
 *    tab strip pinned above the content area.
 *  - The active item uses the primary accent to match the rest of
 *    the page design tokens (bg-primary/10, text-primary, border-primary).
 */
export function StoreSidebar({
  activeSection,
  onSectionChange,
  counts,
}: StoreSidebarProps) {
  return (
    <aside
      className="
        w-full md:w-56 md:shrink-0
        border-b md:border-b-0 md:border-r border-border
        bg-custom
      "
    >
      <nav
        className="
          flex md:flex-col
          gap-1 p-2
          overflow-x-auto md:overflow-x-visible
          scrollbar-none
        "
        aria-label="Store sections"
      >
        {SECTIONS.map((section) => {
          const Icon = section.icon;
          const isActive = activeSection === section.id;
          const count = counts[section.id] ?? 0;

          return (
            <button
              key={section.id}
              type="button"
              onClick={() => onSectionChange(section.id)}
              title={section.description}
              aria-current={isActive ? "page" : undefined}
              className={`
                group flex items-center gap-2.5
                px-3 py-2 rounded-sm text-xs font-semibold
                whitespace-nowrap transition-all cursor-pointer
                border-l-2 md:border-l-2
                ${
                  isActive
                    ? "bg-primary/10 text-primary border-primary"
                    : "text-muted-foreground border-transparent hover:text-foreground hover:bg-muted/50"
                }
              `}
            >
              <Icon
                size={15}
                className={
                  isActive
                    ? "text-primary shrink-0"
                    : "text-muted-foreground group-hover:text-foreground shrink-0"
                }
              />
              <span className="flex-1 text-left">{section.shortLabel}</span>
              <span
                className={`
                  hidden sm:inline-flex items-center justify-center
                  min-w-[22px] h-[18px] px-1.5 rounded-full
                  text-[10px] font-mono border
                  ${
                    isActive
                      ? "bg-primary/15 border-primary/30 text-primary"
                      : "bg-muted border-border text-muted-foreground"
                  }
                `}
              >
                {count}
              </span>
            </button>
          );
        })}
      </nav>
    </aside>
  );
}
