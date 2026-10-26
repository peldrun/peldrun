// frontend/src/components/chat/AgentSelector.tsx

"use client";

import React, { useState, useEffect, useRef } from "react";
import { useChatStore } from "@/stores/chat-store";
import { fetchStoreAgents } from "@/lib/chatsApi";
import { AgentManifest } from "@/lib/types";
import { ChevronDown, Check } from "lucide-react";
import { AgentIcon } from "@/app/stores/_components/AgentIcon";

interface AgentSelectorProps {
  disabled?: boolean;
}

export function AgentSelector({ disabled = false }: AgentSelectorProps) {
  const { selectedAgentId, setSelectedAgentId, setAvailableAgents } = useChatStore();
  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let isMounted = true;
    fetchStoreAgents()
      .then((data) => {
        if (isMounted && data.length > 0) {
          setAgents(data);
          setAvailableAgents(data);
        }
      })
      .catch(console.error);

    return () => {
      isMounted = false;
    };
  }, [setAvailableAgents]);

  useEffect(() => {
    const handleOutsideClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    if (isOpen) {
      document.addEventListener("mousedown", handleOutsideClick);
    }
    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
    };
  }, [isOpen]);

  const activeAgent = agents.find((a) => a.id === selectedAgentId) || {
    id: "peldrun",
    name: "peldrun Generalist",
    role: "General Autonomous Specialist",
    icon: "Bot",
    max_steps: 30
  };

  return (
    <div className="relative inline-block text-left font-sans" ref={containerRef}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setIsOpen(!isOpen)}
        className={`flex items-center gap-2 px-2.5 py-1 rounded-xs text-xs font-medium transition-all ${
          disabled
            ? "opacity-50 cursor-not-allowed bg-transparent border-border text-muted-foreground"
            : "bg-card hover:bg-muted border-border text-foreground shadow-peldrun-xs cursor-pointer"
        }`}
        title={`Active: ${activeAgent.name} (Max ${activeAgent.max_steps || 30} Steps)`}
      >
        <span className="flex-shrink-0 flex items-center justify-center text-foreground">
          <AgentIcon
            agentId={activeAgent.id}
            iconName={activeAgent.icon}
            size={19}
          />
        </span>

        {/* Render the agent name and arrow only when the dropdown is open */}
        {isOpen && (
          <>
            <span className="font-semibold truncate max-w-[110px]">{activeAgent.name}</span>
            <ChevronDown size={12} className="text-muted-foreground transition-transform rotate-180" />
          </>
        )}
      </button>

      {isOpen && (
        <div className="absolute left-0 bottom-full mb-1.5 w-64 rounded-sm bg-card backdrop-blur-md shadow-xl z-50 overflow-hidden font-sans text-xs animate-in fade-in zoom-in-95 duration-100 border border-border">
          <div className="px-3 py-1.5 border-b border-border bg-muted/50 flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              Select Agent Persona
            </span>
          </div>

          <div className="max-h-56 overflow-y-auto p-1 space-y-0.5">
            {agents.map((ag) => {
              const isSelected = ag.id === activeAgent.id;
              return (
                <button
                  type="button"
                  key={ag.id}
                  onClick={() => {
                    setSelectedAgentId(ag.id);
                    setIsOpen(false);
                  }}
                  className={`flex items-center w-full gap-2 p-1.5 rounded-xs text-left transition ${
                    isSelected
                      ? "bg-muted/60 border-l-[2px] border-rb text-foreground"
                      : "hover:bg-muted text-muted-foreground hover:text-foreground hover:shadow-xs border border-transparent"
                  }`}
                >
                  <div className="shrink-0 mt-0.5 flex items-center justify-center text-foreground">
                    <AgentIcon
                      agentId={ag.id}
                      iconName={ag.icon}
                      size={20}
                    />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-xs text-foreground truncate">{ag.name}</span>
                      <span className="text-[10px] text-muted-foreground font-mono">{ag.max_steps || 30} steps</span>
                    </div>
                    <span className="text-[10px] text-muted-foreground block truncate">{ag.role || "Autonomous Agent"}</span>
                  </div>
                  {isSelected && <Check size={12} className="text-primary shrink-0 self-center ml-1" />}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

export default AgentSelector;
