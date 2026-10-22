// frontend/src/app/stores/_components/StoreHeader.tsx

"use client";

import React from "react";
import { RefreshCw, Store } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { HeaderAction, SectionHeaderConfig } from "../_config/sections";

/**
 * Props for the unified store header.
 *
 * The header is fully driven by `config` — each section supplies its
 * own title, subtitle, badge and action list. Only the shared
 * "Refresh" action is owned by the header itself because every
 * section needs it and it must not be duplicated per section.
 */
export interface StoreHeaderProps {
  config: SectionHeaderConfig;
  onRefresh: () => void;
  /** Disables the shared refresh button while a request is running. */
  refreshing?: boolean;
}

/**
 * Render a single header action button.
 * The variant maps to the visual style used throughout the page.
 */
function ActionButton({ action }: { action: HeaderAction }) {
  const Icon = action.icon;

  const variantClasses: Record<
    NonNullable<HeaderAction["variant"]>,
    string
  > = {
    primary:
      "bg-primary hover:bg-primary/90 text-primary-foreground border border-transparent shadow-xs",
    outline:
      "border border-border bg-muted/50 hover:bg-muted text-foreground shadow-xs",
    ghost:
      "border border-transparent hover:bg-muted text-foreground",
  };

  const variant = action.variant ?? "primary";

  return (
    <Button
      size="sm"
      variant={variant === "primary" ? "outline" : variant}
      onClick={action.onClick}
      disabled={action.disabled || action.loading}
      className={`text-xs h-8 px-3.5 font-medium transition cursor-pointer ${variantClasses[variant]}`}
    >
      {action.loading && !Icon ? (
        <RefreshCw size={13} className="mr-1.5 animate-spin" />
      ) : Icon ? (
        <Icon
          size={14}
          className={`mr-1.5 ${action.iconClassName ?? ""} ${
            action.loading ? "animate-spin" : ""
          }`}
        />
      ) : null}
      <span>{action.label}</span>
    </Button>
  );
}

/**
 * Unified header bar for every store section.
 *
 * Layout:
 *  - Left: fixed brand mark + section title + optional version badge
 *  - Right: shared "Refresh" button followed by the section's own
 *    action buttons (order preserved as supplied by the section).
 *
 * This component is stateless and receives everything via props so
 * that it can be reused unchanged across Agents, Tools and Extensions.
 */
export function StoreHeader({
  config,
  onRefresh,
  refreshing = false,
}: StoreHeaderProps) {
  return (
    <header className="w-full border-b border-border bg-custom backdrop-blur-md sticky top-0 z-10">
      <div className="mx-auto w-full px-4 sm:px-6 lg:px-8 py-3.5 flex flex-wrap items-center justify-between gap-4">
        {/* Brand + section identity */}
        <div className="flex items-center gap-3 min-w-0">
          <div className="p-0 shrink-0 flex items-center justify-center m-auto">

            <span className="w-[45px] text-primary flex items-center justify-center m-auto self-center">
                <svg xmlns="http://www.w3.org/2000/svg" version="1.1"  width={45} viewBox="-5 -2 110 101" className="text-primary/90 inline-block self-center">
                <path fill="currentColor" d="m98.98 73.527c0-2.0742 0-4.1367-0.011719-6.0352v-0.52344c-0.050781-1.8359-0.085938-4.1094-1.1133-5.7969-0.023438-0.050781-0.0625-0.10156-0.10156-0.14844l-0.074218-0.11328c-0.050782-0.074218-0.10156-0.13672-0.16406-0.19922-1.75-2.0117-4.5234-1.9844-6.5469-1.9844h-1.1758-0.13672-9.1953c-5.2852-0.011718-10.742-0.023437-15.676 0.050782-1.7109-0.050782-3.8594 0.11328-5.3828 1.625-1.5352 1.5234-1.6875 3.6992-1.6602 5.3359-0.0625 3.8477-0.050782 7.7578-0.039063 11.543 0.011719 4.4961 0.039063 9.1562-0.074219 13.703v0.21094c0.011719 0.27344 0.023438 0.55078 0.039063 0.83594 0.085937 2.375 0.28906 7.3086 5.3594 7.7188 3.0742 0.19922 6.457 0.30078 10.031 0.30078 3.3477 0 6.8594-0.085937 10.43-0.26172 4.3477-0.085937 8.1211-1.7852 10.93-4.9102 3.3867-3.7617 5.0469-9.2695 4.5703-15.141-0.023437-1.9375-0.011719-4.0586-0.011719-6.1719zm-8.2695 17.965c-1.8867 2.0977-4.3477 3.1836-7.3828 3.2617-7.2969 0.36328-14.191 0.35156-19.926-0.023437-0.22656-0.011719-0.30078-0.0625-0.30078-0.0625-0.32422-0.32422-0.39844-2.0352-0.42578-2.875 0-0.28906-0.023437-0.5625-0.039062-0.83594 0.11328-4.5859 0.085937-9.2461 0.074219-13.742-0.011719-3.7617-0.023438-7.6562 0.039062-11.531-0.023438-1.4102 0.17578-1.7617 0.17578-1.7617s0.375-0.22656 1.8477-0.1875c4.9961-0.074219 10.43-0.0625 15.691-0.039063 3.1367 0 6.2227 0.011719 9.1562 0 0.4375 0.023438 0.89844 0.023438 1.3867 0.011719 0.72266 0 2.1719 0 2.6602 0.21094 0.25 0.69922 0.28906 2.3242 0.30078 3.1484v0.52344c0.023438 1.8477 0.023438 3.8867 0.023438 5.9336 0 2.1602 0 4.3477 0.011718 6.3203v0.17578c0.375 4.4727-0.86328 8.7578-3.2969 11.469z"/>
                <path fill="currentColor" d="m57.707 22.773c-0.011719 3.7852-0.023437 7.6953 0.039063 11.457-0.039063 1.7227 0.125 3.8984 1.6602 5.4219 1.5234 1.5234 3.6719 1.6758 5.2969 1.6367 3 0.039063 6.1445 0.050782 9.3086 0.050782h6.4727c3.1484 0 6.2578-0.011719 9.1953 0h0.13672c0.36328-0.011719 0.76172-0.011719 1.1758 0 2.0234 0 4.7969 0.023437 6.5469-1.9844 0.050782-0.0625 0.10156-0.125 0.14844-0.19922l0.074219-0.11328s0.074219-0.10156 0.10156-0.16406c1.0117-1.6875 1.0625-3.9727 1.0977-5.8086v-0.5625c0.039062-1.8633 0.023437-3.9219 0.023437-5.9961 0-2.1094 0-4.2344 0.011719-6.1719 0.47656-5.8711-1.1875-11.379-4.5703-15.141-2.8125-3.125-6.582-4.8086-10.855-4.9102-5.2109-0.25-12.992-0.46094-20.574 0.039063-5.0352 0.41406-5.2344 5.3477-5.3203 7.7188 0 0.28906-0.023437 0.5625-0.039062 0.83594v0.21094c0.11328 4.5586 0.085937 9.207 0.074219 13.703zm4.9609-14.555c0.039062-0.82422 0.10156-2.5469 0.42578-2.8594 0 0 0.074219-0.050781 0.27344-0.0625 5.7852-0.375 12.68-0.38672 20.023-0.023437 2.9727 0.0625 5.4336 1.1602 7.3203 3.2461 2.4375 2.7109 3.6719 6.9961 3.2969 11.469v0.17578c-0.023437 1.9844-0.023437 4.1602-0.011718 6.3203v5.8945 0.5625c-0.023438 0.8125-0.0625 2.4375-0.3125 3.1367-0.48828 0.23828-1.9375 0.22656-2.6602 0.22656-0.48828 0-0.94922 0-1.3867 0.011718h-9.1562c-5.2578 0.011719-10.691 0.023438-15.789-0.039062h-0.28906c-1.1875 0-1.4375-0.1875-1.4375-0.16406-0.011719-0.023437-0.22656-0.375-0.19922-1.8867-0.0625-3.7969-0.050781-7.6836-0.039063-11.457 0.011719-4.4961 0.039063-9.1562-0.074218-13.742 0.011718-0.27344 0.023437-0.55078 0.039062-0.83594z"/>
                <path fill="currentColor" d="m42.367 8.8555c-0.011719-0.27344-0.023438-0.55078-0.039063-0.83594-0.085937-2.375-0.28906-7.3086-5.3594-7.7188-7.5469-0.48828-15.328-0.28906-20.461-0.039062-4.3477 0.085937-8.1211 1.7852-10.93 4.9102-3.375 3.7461-5.0352 9.2695-4.5742 15.129 0.023438 1.9375 0.011719 4.0586 0.011719 6.1719 0 2.0742 0 4.1367 0.011719 6.0352v0.52344c0.050781 1.8359 0.085937 4.1094 1.1133 5.8086 0.039063 0.050781 0.0625 0.11328 0.10156 0.16406l0.074218 0.11328c0.050782 0.0625 0.10156 0.13672 0.14844 0.19922 1.75 2.0117 4.5078 2 6.5469 1.9844h1.1758 0.13672 9.1953 6.4453c3.1602 0 6.2969 0 9.2461-0.050781 1.7109 0.039062 3.8594-0.11328 5.3828-1.625 1.5352-1.5234 1.6875-3.6992 1.6602-5.3359 0.0625-3.8477 0.050782-7.7578 0.039063-11.543-0.011719-4.4961-0.039063-9.1562 0.074219-13.703v-0.21094zm-5.0703 13.93c0.011719 3.7617 0.023437 7.6562-0.039063 11.543 0.023438 1.4102-0.17578 1.7617-0.17578 1.7617s-0.36328 0.22656-1.8477 0.1875c-4.9961 0.074218-10.43 0.0625-15.691 0.039062h-6.2227-2.9375c-0.4375-0.023437-0.89844-0.023437-1.3867-0.011718-0.72266 0-2.1719 0-2.6602-0.21094-0.25-0.69922-0.28906-2.3242-0.30078-3.1484v-0.52344c-0.023437-1.8477-0.023437-3.8867-0.023437-5.9336 0-2.1602 0-4.3477-0.011719-6.3203v-0.17578c-0.375-4.4727 0.86328-8.7578 3.2969-11.469 1.8867-2.0977 4.3477-3.1836 7.3828-3.2461 7.2812-0.36328 14.18-0.35156 19.926 0.023437 0.22656 0.011719 0.30078 0.0625 0.30078 0.0625 0.32422 0.32422 0.39844 2.0469 0.42578 2.875 0 0.28906 0.023438 0.5625 0.039063 0.82422-0.11328 4.5977-0.085938 9.2461-0.074219 13.742z"/>
                <path fill="currentColor" d="m34.746 60.023c-10.906 4.3086-18.125 4.1602-29.207-0.32422-1.1992-0.42578-3.0742-1.2617-3.9727-0.625l-0.085937 0.085937c-0.63672 0.89844 0.1875 2.7617 0.625 3.9727 4.4844 11.082 4.6328 18.301 0.32422 29.207-1.8359 4.3477-1.8242 6.4453 3.3711 4.2461 11.168-4.5859 18.852-4.4102 29.82 0.074218 1.1367 0.41406 3.1484 1.1875 3.8984 0.46094 0.72266-0.75-0.039062-2.7617-0.46094-3.8984-4.4844-10.969-4.6602-18.664-0.074219-29.82 2.2344-5.2852 0-5.1602-4.2461-3.3711z"/>
                </svg>
            </span>

          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h1 className="text-base font-bold text-foreground tracking-tight truncate">
                {config.title}
              </h1>
              {config.badge && (
                <span className="hidden sm:inline-flex px-2 py-0.5 text-[10px] font-mono font-medium rounded-full bg-muted text-muted-foreground border border-border">
                  {config.badge}
                </span>
              )}
            </div>
            <p className="text-xs text-muted-foreground truncate">
              {config.subtitle}
            </p>
          </div>
        </div>

        {/* Actions: shared refresh + section-specific buttons */}
        <div className="flex items-center gap-2.5">
          <Button
            variant="outline"
            size="sm"
            onClick={onRefresh}
            disabled={refreshing}
            className="text-xs border-border bg-muted/50 hover:bg-muted text-foreground transition shadow-xs h-8 cursor-pointer"
          >
            <RefreshCw
              size={13}
              className={`mr-1.5 ${refreshing ? "animate-spin text-primary" : ""}`}
            />
            Refresh
          </Button>

          {config.actions.map((action) => (
            <ActionButton key={action.id} action={action} />
          ))}
        </div>
      </div>
    </header>
  );
}
