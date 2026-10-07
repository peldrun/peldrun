"use client";

import React, { useState, useRef, useEffect } from "react";
import { Brain, Zap, Sparkles, Cpu, ChevronDown, Check } from "lucide-react";
import { useAppStorage } from "@/hooks/use-app-storage";

export type ReasoningEffortLevel = "none" | "low" | "medium" | "high";

interface ReasoningOption {
  id: ReasoningEffortLevel;
  label: string;
  shortLabel: string;
  description: string;
  badgeClass: string;
  itemIcon: React.ElementType;
}

const REASONING_OPTIONS: ReasoningOption[] = [
  {
    id: "none",
    label: "Direct / No Thinking",
    shortLabel: "Off",
    description: "Instant response with zero reasoning tokens. Best for daily chat & quick queries.",
    badgeClass: "text-zinc-600 dark:text-zinc-300 bg-zinc-500/10 border-zinc-500/20",
    itemIcon: Zap,
  },
  {
    id: "low",
    label: "Quick Thinking",
    shortLabel: "Quick",
    description: "Light reasoning budget (~1k tokens). Balances speed with sanity checks.",
    badgeClass: "text-blue-600 dark:text-blue-400 bg-blue-500/10 border-blue-500/20",
    itemIcon: Sparkles,
  },
  {
    id: "medium",
    label: "Balanced Reasoning",
    shortLabel: "Balanced",
    description: "Standard analytical depth (~2k tokens). Great for coding and workflow steps.",
    badgeClass: "text-amber-600 dark:text-amber-400 bg-amber-500/10 border-amber-500/20",
    itemIcon: Brain,
  },
  {
    id: "high",
    label: "Deep Reasoning",
    shortLabel: "Deep",
    description: "Maximum reasoning budget (~4k+ tokens). Deep deduction for complex challenges.",
    badgeClass: "text-violet-600 dark:text-violet-400 bg-violet-500/10 border-violet-500/20",
    itemIcon: Cpu,
  },
];

interface ReasoningEffortSelectorProps {
  disabled?: boolean;
}

export function ReasoningEffortSelector({ disabled = false }: ReasoningEffortSelectorProps) {
  const [reasoningEffort, setReasoningEffort] = useAppStorage("reasoning_effort");
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const currentOption =
    REASONING_OPTIONS.find((opt) => opt.id === reasoningEffort) || REASONING_OPTIONS[0];

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    if (isOpen) {
      document.addEventListener("mousedown", handleClickOutside);
    }
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [isOpen]);

  const handleSelect = (level: ReasoningEffortLevel) => {
    setReasoningEffort(level);
    setIsOpen(false);
    if (typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("peldrun:reasoning-effort-change", { detail: level })
      );
    }
  };

  return (
    <div className="relative inline-block font-sans select-none" ref={dropdownRef}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setIsOpen((prev) => !prev)}
        className={`flex items-center gap-1.5 px-2 py-1 rounded-lg border text-[11px] font-medium transition-all cursor-pointer ${
          currentOption.badgeClass
        } ${
          disabled
            ? "opacity-50 cursor-not-allowed"
            : "hover:opacity-90 hover:scale-[1.02] active:scale-[0.98]"
        }`}
        title={`Model Reasoning Capability & Effort: ${currentOption.label}`}
      >
        <Brain size={12} className="shrink-0" />
        <span className="font-semibold">{currentOption.shortLabel}</span>
        <ChevronDown size={11} className={`transition-transform duration-150 ${isOpen ? "rotate-180" : ""}`} />
      </button>

      {isOpen && (
        <div className="absolute bottom-full mb-1.5 left-0 w-64 rounded-xl border border-border bg-card/95 backdrop-blur-md shadow-xl p-1 z-50 animate-in fade-in zoom-in-95 duration-150">
          <div className="px-2 py-1 text-[10px] uppercase tracking-wider font-semibold text-muted-foreground border-b border-border/60 mb-1 flex items-center justify-between">
            <span>Reasoning Effort</span>
            <Brain size={11} />
          </div>

          <div className="space-y-0.5">
            {REASONING_OPTIONS.map((option) => {
              const OptIcon = option.itemIcon;
              const isSelected = option.id === reasoningEffort;

              return (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => handleSelect(option.id)}
                  className={`w-full flex items-start gap-2.5 p-2 rounded-lg text-left transition-colors cursor-pointer ${
                    isSelected
                      ? "bg-muted text-foreground font-medium"
                      : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                  }`}
                >
                  <div className={`mt-0.5 p-1 rounded-md border shrink-0 ${option.badgeClass}`}>
                    <OptIcon size={12} />
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between text-xs leading-none">
                      <span className="font-semibold text-foreground">{option.label}</span>
                      {isSelected && <Check size={12} className="text-primary shrink-0" />}
                    </div>
                    <p className="text-[10px] text-muted-foreground mt-1 leading-snug line-clamp-2">
                      {option.description}
                    </p>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

export default ReasoningEffortSelector;