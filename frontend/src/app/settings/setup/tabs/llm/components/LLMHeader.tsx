// frontend/src/app/settings/setup/tabs/llm/components/LLMHeader.tsx
"use client";

import React from "react";
import { motion } from "framer-motion";
import {
  Sparkles,
  Radio,
  Cpu,
  Cloud,
  Terminal,
  Sliders,
} from "lucide-react";
import type { LLMHeaderProps } from "../types";

/* -------------------------------------------------------------------------- */
/* Sub-tab definition                                                         */
/* -------------------------------------------------------------------------- */

interface SubTabDef {
  id: "overview" | "lmstudio" | "cloud" | "custom" | "budgets";
  label: string;
  icon: React.ReactNode;
}

const SUB_TABS: readonly SubTabDef[] = [
  { id: "overview", label: "Overview & Ops", icon: <Radio size={13} /> },
  {
    id: "lmstudio",
    label: "Local Engines (LM Studio & Ollama)",
    icon: <Cpu size={13} />,
  },
  { id: "cloud", label: "Cloud Providers", icon: <Cloud size={13} /> },
  { id: "custom", label: "Custom Endpoints", icon: <Terminal size={13} /> },
  {
    id: "budgets",
    label: "Model Budgets & Context",
    icon: <Sliders size={13} />,
  },
];

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Header strip for the LLM tab: title, contextual description, and the
 * five-way sub-tab navigation.
 *
 * Visual language is aligned with the SearchTab reference design:
 *   - `motion.header` fade-in on mount,
 *   - `Sparkles` accent icon inside an `icon` capsule,
 *   - sub-tabs driven by a 2px bottom border with `border-primary` for the
 *     active state and `border-transparent` for the rest.
 */
export function LLMHeader({ activeSubTab, onSubTabChange }: LLMHeaderProps) {
  return (
    <motion.header
      initial={{ opacity: 0, y: -12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
      className="border-b border-border backdrop-blur-xl w-full bg-muted"
    >
      {/* Title row */}
      <div className="max-w-full mx-auto px-4 pt-4 flex items-center gap-2.5">
        <div className="p-2 icon">
          <Sparkles size={16} className="text-primary" />
        </div>
        <h2 className="text-sm font-semibold font-heading text-foreground uppercase tracking-wide">
          Model Hub &amp; Multi-Provider Architecture
        </h2>
      </div>

      {/* Description */}
      <p className="max-w-full mx-auto px-4 pt-0.5 pb-3 text-xs text-muted-foreground">
        Dedicated engines for local GPUs, Ollama, cloud credentials, custom
        endpoints, and role routing.
      </p>

      {/* Sub-tab navigation */}
      <div className="w-full mx-auto bg-custom">
        <div className="flex items-center gap-1 border-b border-border/60 overflow-x-auto scrollbar-thin">
          {SUB_TABS.map((tab) => {
            const isActive = activeSubTab === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                onClick={() => onSubTabChange(tab.id)}
                className={`flex items-center gap-1.5 px-3 py-2 text-xs font-semibold border-b-2 -mb-[1px] whitespace-nowrap transition-all cursor-pointer ${
                  isActive
                    ? "border-primary text-primary"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                {tab.icon}
                <span>{tab.label}</span>
              </button>
            );
          })}
        </div>
      </div>
    </motion.header>
  );
}
