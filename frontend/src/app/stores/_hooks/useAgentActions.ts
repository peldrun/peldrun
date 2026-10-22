// frontend/src/app/stores/_hooks/useAgentActions.ts

"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import { useChatStore } from "@/stores/chat-store";
import {
  createStoreAgent,
  updateStoreAgent,
  deleteStoreAgent,
} from "@/lib/chatsApi";
import type { AgentManifest, AgentUpsertPayload } from "@/lib/types";
import { showToast } from "@/components/ui/ToastNotification";
import { getApiUrl } from "./useStoreData";
import { DEFAULT_AGENT_FORM } from "../_types/store";

/**
 * Arguments required by {@link useAgentActions}.
 * The agents collection and setter are injected from `useStoreData`
 * so the hook only owns action-specific state.
 */
export interface UseAgentActionsArgs {
  agents: AgentManifest[];
  setAgents: React.Dispatch<React.SetStateAction<AgentManifest[]>>;
}

/**
 * Return type of {@link useAgentActions}.
 */
export interface UseAgentActionsResult {
  // Toggle lifecycle
  togglingAgentId: string | null;
  handleToggleAgent: (agentId: string) => Promise<void>;

  // Delete
  handleDeleteAgent: (agentId: string) => Promise<void>;

  // Modal state
  isAgentModalOpen: boolean;
  editingAgentId: string | null;
  agentFormData: AgentUpsertPayload;
  setAgentFormData: React.Dispatch<React.SetStateAction<AgentUpsertPayload>>;
  handleOpenCreateAgentModal: () => void;
  handleOpenEditAgentModal: (agent: AgentManifest) => void;
  handleCloseAgentModal: () => void;
  handleToggleAgentToolAssignment: (toolId: string) => void;
  handleSubmitAgentModal: (e: React.FormEvent) => Promise<void>;

  // Chat launch
  handleStartChatWithAgent: (agent: AgentManifest) => void;
}

/**
 * Encapsulates every action related to the Agents section:
 * toggling, deleting, opening the builder modal, editing and submitting,
 * plus launching a chat session with a given agent.
 */
export function useAgentActions({
  agents,
  setAgents,
}: UseAgentActionsArgs): UseAgentActionsResult {
  const router = useRouter();
  const { setSelectedAgentId } = useChatStore();

  const [togglingAgentId, setTogglingAgentId] = useState<string | null>(null);

  const [isAgentModalOpen, setIsAgentModalOpen] = useState(false);
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null);
  const [agentFormData, setAgentFormData] =
    useState<AgentUpsertPayload>(DEFAULT_AGENT_FORM);

  /**
   * Launch a chat session bound to the selected agent.
   * Disabled agents are rejected with a warning.
   */
  const handleStartChatWithAgent = useCallback(
    (agent: AgentManifest) => {
      if (agent.status === "disabled") {
        showToast.warning(
          "Agent Disabled",
          `Agent '${agent.name}' is currently disabled. Toggle it active to use.`
        );
        return;
      }
      setSelectedAgentId(agent.id);
      if (typeof window !== "undefined") {
        localStorage.setItem("omweb_exec_mode", "agent");
        window.dispatchEvent(
          new CustomEvent("omweb:mode-change", { detail: "agent" })
        );
      }
      router.push("/chat");
    },
    [router, setSelectedAgentId]
  );

  /**
   * Toggle the enabled/disabled status of an agent.
   * The core agent `peldrun` is protected and cannot be disabled.
   */
  const handleToggleAgent = useCallback(
    async (agentId: string) => {
      if (agentId === "peldrun") {
        showToast.info(
          "Protected Core",
          "Default primary agent 'peldrun' cannot be disabled."
        );
        return;
      }
      setTogglingAgentId(agentId);
      try {
        const res = await fetch(getApiUrl(`/api/store/agents/${agentId}/toggle`), {
          method: "POST",
        });
        if (res.ok) {
          const data = await res.json();
          const updatedAgent = data.agent;
          setAgents((prev) =>
            prev.map((a) =>
              a.id === agentId ? { ...a, status: updatedAgent.status } : a
            )
          );
          showToast.success(
            "Agent Status Updated",
            `${updatedAgent.name || agentId} is now ${updatedAgent.status}`
          );
        } else {
          const err = await res.json().catch(() => ({}));
          showToast.error(
            "Toggle Failed",
            err.detail || "Could not toggle agent status."
          );
        }
      } catch (err: any) {
        showToast.error("Network Error", err.message);
      } finally {
        setTogglingAgentId(null);
      }
    },
    [setAgents]
  );

  /**
   * Permanently delete a custom agent after confirmation.
   */
  const handleDeleteAgent = useCallback(
    async (agentId: string) => {
      if (!confirm("Are you sure you want to delete this custom agent?")) return;
      try {
        const ok = await deleteStoreAgent(agentId);
        if (ok) {
          setAgents((prev) => prev.filter((a) => a.id !== agentId));
          showToast.success(
            "Agent Deleted",
            `Agent ${agentId} removed successfully.`
          );
        } else {
          showToast.error("Delete Failed", "Cannot delete built-in agent.");
        }
      } catch (err: any) {
        showToast.error("Delete Error", err.message);
      }
    },
    [setAgents]
  );

  /**
   * Open the builder modal in create mode with the default payload.
   */
  const handleOpenCreateAgentModal = useCallback(() => {
    setEditingAgentId(null);
    setAgentFormData(DEFAULT_AGENT_FORM);
    setIsAgentModalOpen(true);
  }, []);

  /**
   * Open the builder modal pre-filled with an existing agent's data.
   */
  const handleOpenEditAgentModal = useCallback((agent: AgentManifest) => {
    setEditingAgentId(agent.id);
    setAgentFormData({
      name: agent.name,
      role: agent.role || "Specialist",
      icon: agent.icon || "Sparkles",
      description: agent.description || "",
      system_prompt: agent.system_prompt,
      tools: agent.tools || [],
      max_steps: agent.max_steps || 30,
    });
    setIsAgentModalOpen(true);
  }, []);

  /**
   * Close the builder modal and clear the current editing target.
   */
  const handleCloseAgentModal = useCallback(() => {
    setIsAgentModalOpen(false);
    setEditingAgentId(null);
  }, []);

  /**
   * Toggle a single tool assignment inside the current agent form.
   */
  const handleToggleAgentToolAssignment = useCallback((toolId: string) => {
    setAgentFormData((prev) => {
      const exists = prev.tools.includes(toolId);
      return {
        ...prev,
        tools: exists
          ? prev.tools.filter((t) => t !== toolId)
          : [...prev.tools, toolId],
      };
    });
  }, []);

  /**
   * Submit the builder modal: creates or updates an agent depending on
   * whether `editingAgentId` is set.
   */
  const handleSubmitAgentModal = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!agentFormData.name.trim() || !agentFormData.system_prompt.trim()) {
        showToast.warning(
          "Validation Error",
          "Agent name and system prompt are required."
        );
        return;
      }

      try {
        if (editingAgentId) {
          const updated = await updateStoreAgent(editingAgentId, agentFormData);
          if (updated) {
            setAgents((prev) =>
              prev.map((a) => (a.id === editingAgentId ? updated : a))
            );
            handleCloseAgentModal();
            showToast.success(
              "Agent Updated",
              `Updated ${agentFormData.name} successfully.`
            );
          }
        } else {
          const created = await createStoreAgent(agentFormData);
          if (created) {
            setAgents((prev) => [...prev, created]);
            handleCloseAgentModal();
            showToast.success(
              "Agent Created",
              `Created ${agentFormData.name} successfully.`
            );
          }
        }
      } catch (err: any) {
        showToast.error(
          "Save Failed",
          err.message || "Could not save agent configuration."
        );
      }
    },
    [agentFormData, editingAgentId, setAgents, handleCloseAgentModal]
  );

  return {
    togglingAgentId,
    handleToggleAgent,
    handleDeleteAgent,
    isAgentModalOpen,
    editingAgentId,
    agentFormData,
    setAgentFormData,
    handleOpenCreateAgentModal,
    handleOpenEditAgentModal,
    handleCloseAgentModal,
    handleToggleAgentToolAssignment,
    handleSubmitAgentModal,
    handleStartChatWithAgent,
  };
}
