// frontend/src/app/settings/setup/tabs/llm/components/ProviderIconPicker.tsx
"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { CheckCircle2, ChevronDown, Search, X } from "lucide-react";
import { Icon } from "@/components/Icon/Icon";
import { PROVIDER_LOGO_OPTIONS } from "../constants";
import type { ProviderIconPickerProps } from "../types";

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Searchable dropdown that lets the user pick a provider icon.
 *
 * Visual language (colors, motion, typography) is aligned with the SearchTab
 * reference design:
 *   - `motion.button` anchor + `AnimatePresence` list,
 *   - chevron rotates 0deg / 180deg,
 *   - outside-click closes the popover,
 *   - selected item uses `bg-primary/10` and a `CheckCircle2` marker.
 */
export function ProviderIconPicker({
  value,
  onChange,
  accent = "#3b82f6",
  disabled = false,
}: ProviderIconPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  /* -------------------------- Outside click ----------------------------- */
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  /* -------------------------- Filtered options -------------------------- */
  const filteredOptions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return PROVIDER_LOGO_OPTIONS;
    return PROVIDER_LOGO_OPTIONS.filter(
      (opt) =>
        opt.label.toLowerCase().includes(q) ||
        opt.value.toLowerCase().includes(q)
    );
  }, [query]);

  /* -------------------------- Selected option --------------------------- */
  const selectedOption = useMemo(
    () => PROVIDER_LOGO_OPTIONS.find((opt) => opt.value === value),
    [value]
  );

  const handleSelect = (iconName: string) => {
    onChange(iconName);
    setOpen(false);
    setQuery("");
  };

  const handleClear = (event: React.MouseEvent) => {
    event.stopPropagation();
    onChange("");
    setQuery("");
  };

  return (
    <div className="relative" ref={containerRef}>
      <label className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground block mb-1">
        Provider Logo
      </label>

      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className={`w-full bg-background border border-border rounded-sm px-2.5 py-1.5 text-[11px] text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow flex items-center justify-between gap-2 ${
          disabled ? "opacity-60 cursor-not-allowed" : "cursor-pointer"
        }`}
      >
        <span className="flex items-center gap-2 min-w-0">
          <span
            className="shrink-0 flex items-center justify-center"
            style={{ color: selectedOption?.accent ?? accent }}
          >
            <Icon
              name={value || "rect"}
              size={14}
              title={selectedOption?.label ?? "No logo selected"}
            />
          </span>
          <span className="truncate text-left">
            {selectedOption?.label ?? "Select an icon…"}
          </span>
        </span>

        <span className="flex items-center gap-1 shrink-0">
          {value && !disabled && (
            <span
              role="button"
              tabIndex={-1}
              onClick={handleClear}
              className="p-0.5 rounded text-muted-foreground hover:text-rose-500 cursor-pointer transition-colors"
              title="Clear icon"
            >
              <X size={11} />
            </span>
          )}
          <motion.span
            animate={{ rotate: open ? 180 : 0 }}
            transition={{ duration: 0.2 }}
            className="shrink-0"
          >
            <ChevronDown size={12} className="text-muted-foreground" />
          </motion.span>
        </span>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.15 }}
            className="absolute z-50 mt-1 w-full min-w-[240px] bg-card border border-border rounded-sm shadow-lg overflow-hidden"
          >
            {/* Search field */}
            <div className="p-2 border-b border-border/60 bg-background/60">
              <div className="relative">
                <Search
                  size={11}
                  className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground"
                />
                <input
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search icons…"
                  autoFocus
                  className="w-full bg-background border border-border rounded-sm pl-6 pr-2 py-1 text-[11px] text-foreground font-mono focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow"
                />
              </div>
            </div>

            {/* Options list */}
            <ul
              role="listbox"
              className="max-h-64 overflow-y-auto divide-y divide-border/40"
            >
              {filteredOptions.length === 0 && (
                <li className="px-3 py-4 text-center text-[10px] text-muted-foreground font-mono">
                  No icons match "{query}"
                </li>
              )}

              {filteredOptions.map((opt) => {
                const selected = opt.value === value;
                return (
                  <li key={opt.value}>
                    <button
                      type="button"
                      role="option"
                      aria-selected={selected}
                      onClick={() => handleSelect(opt.value)}
                      className={`w-full flex items-center gap-2.5 px-3 py-2 text-xs cursor-pointer transition-colors text-left ${
                        selected
                          ? "bg-primary/10 text-foreground"
                          : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                      }`}
                    >
                      <span
                        className="shrink-0 flex items-center justify-center"
                        style={{ color: opt.accent }}
                      >
                        <Icon
                          name={opt.value}
                          size={16}
                          title={`${opt.label} logo`}
                        />
                      </span>
                      <span className="flex-1 truncate font-medium">
                        {opt.label}
                      </span>
                      <span className="text-[9px] font-mono text-muted-foreground/70 shrink-0">
                        {opt.value}
                      </span>
                      {selected && (
                        <CheckCircle2
                          size={12}
                          className="text-primary shrink-0"
                        />
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
