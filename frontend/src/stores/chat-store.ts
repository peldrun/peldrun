"use client";

import { create } from "zustand";
import { AgentManifest } from "@/lib/types";
// Note on removed imports (see docs/state-contracts.md §6.4):
//
//   * `fetchChats`, `ChatSession`, `deleteAllChats` were only used by
//     the (now removed) `loadSessions`, `loadSessionDetail`, and
//     `clearAllSessions` methods. Their responsibilities have moved to
//     `session-store.ts` (`loadSidebarSessions` / `loadMoreSidebarSessions`)
//     and to the individual delete-then-remove call patterns documented
//     in the store boundary section.
//
//   * `storage` was only used inside `syncSessionAgentMetadata`, which
//     has been removed. The `exec_mode` preference is now the single
//     responsibility of the `use-app-storage` hook, and the
//     `omweb:mode-change` event is dispatched directly by the composer
//     and chat-landing components when the user switches modes.

export interface AgentStep {
  id: string;
  step_number: number;
  type: "thought" | "tool_call" | "observation" | "plan";
  content: string;
  timestamp: string;
  tool_name?: string;
  tool_args?: Record<string, any>;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: string;
  status?: "pending" | "running" | "completed" | "failed";
}

interface ChatState {
  // Local active-conversation state. This store owns the *currently
  // focused* chat's UI state only; it is not a catalogue of sessions.
  //
  // Session metadata (the list of chats shown in the sidebar, history,
  // and project pages) lives in `session-store.ts`. See the store
  // boundary section in docs/state-contracts.md §6.4.
  messages: ChatMessage[];
  currentJobId: string | null;
  isRunning: boolean;
  selectedAgentId: string;
  availableAgents: AgentManifest[];
  activeSteps: AgentStep[];

  // Agent selection
  setSelectedAgentId: (agentId: string) => void;
  setAvailableAgents: (agents: AgentManifest[]) => void;
  getAgentMaxSteps: (id?: string) => number;

  // Local message/step management
  addMessage: (message: ChatMessage) => void;
  updateMessageStatus: (id: string, status: ChatMessage["status"]) => void;
  appendStep: (step: AgentStep) => void;
  clearActiveSteps: () => void;
  setCurrentJobId: (jobId: string | null) => void;
  setIsRunning: (isRunning: boolean) => void;
  resetChat: () => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  currentJobId: null,
  isRunning: false,
  selectedAgentId: "peldrun",
  availableAgents: [],
  activeSteps: [],

  setSelectedAgentId: (agentId: string) => set({ selectedAgentId: agentId || "peldrun" }),
  setAvailableAgents: (agents: AgentManifest[]) => set({ availableAgents: agents }),

  getAgentMaxSteps: (id?: string) => {
    const targetId = id || get().selectedAgentId;
    const found = get().availableAgents.find((a) => a.id === targetId);
    if (found && found.max_steps) return found.max_steps;

    // Built-in manifest boundary matching backend registry
    switch (targetId) {
      case "deep_researcher":
        return 35;
      case "data_scientist":
        return 25;
      case "code_architect":
      case "peldrun":
      default:
        return 30;
    }
  },

  addMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),
  updateMessageStatus: (id, status) => set((state) => ({
    messages: state.messages.map((msg) => (msg.id === id ? { ...msg, status } : msg)),
  })),
  appendStep: (step) => set((state) => ({ activeSteps: [...state.activeSteps, step] })),
  clearActiveSteps: () => set({ activeSteps: [] }),
  setCurrentJobId: (jobId) => set({ currentJobId: jobId }),
  setIsRunning: (isRunning) => set({ isRunning }),
  resetChat: () => set({ messages: [], currentJobId: null, isRunning: false, activeSteps: [] }),
}));
