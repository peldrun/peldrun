// frontend/src/app/stores/_modals/AgentBuilderModal.tsx

"use client";

import React from "react";
import { Bot, Check, Server, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { AgentUpsertPayload, ToolDefinition } from "@/lib/types";

export interface McpServerOption {
  id: string;
  name: string;
  description?: string;
  transport?: string;
  status?: string;
  is_builtin?: boolean;
  is_bundle?: boolean;
}

export interface AgentBuilderModalProps {
  open: boolean;
  editingAgentId: string | null;
  formData: AgentUpsertPayload;
  setFormData: React.Dispatch<React.SetStateAction<AgentUpsertPayload>>;
  tools: ToolDefinition[];
  /**
   * MCP protocol servers available in the Store. Rendered as a
   * second selection group below the tools list. Loaded by the
   * parent (page.tsx) via GET /api/mcp/servers so the modal does
   * not need to know how the store backend is laid out.
   */
  mcpServers: McpServerOption[];
  onSubmit: (e: React.FormEvent) => void;
  onClose: () => void;
  onToggleTool: (toolId: string) => void;
  /**
   * Toggle a single MCP server binding on the current agent form.
   * The parent handles the mutation of ``formData.mcp_servers``
   * so this modal stays purely presentational for that group.
   */
  onToggleMcpServer: (serverId: string) => void;
}

// Predefined colors that match the default agent identity palette
const PRESET_COLORS = [
  "#10b981", // Emerald (peldrun)
  "#0ea5e9", // Sky (code_architect)
  "#a855f7", // Purple (data_scientist)
  "#f59e0b", // Amber (deep_researcher)
  "#ef4444", // Red
  "#ec4899", // Pink
  "#14b8a6", // Teal
  "#64748b", // Slate
];

export function AgentBuilderModal({
  open,
  editingAgentId,
  formData,
  setFormData,
  tools,
  mcpServers,
  onSubmit,
  onClose,
  onToggleTool,
  onToggleMcpServer,
}: AgentBuilderModalProps) {
  if (!open) return null;

  const isEditing = editingAgentId !== null;
  const enabledTools = tools.filter(
    (t) => t.is_enabled !== false && t.status !== "disabled"
  );

  // ``core_color`` is now declared in AgentUpsertPayload, so no
  // cast is needed. The field is optional; custom agents that were
  // created before this change fall back to the default palette
  // colour until the next save.
  const currentColor = formData.core_color || "#10b981";

  const handleColorChange = (color: string) => {
    setFormData((prev) => ({ ...prev, core_color: color }) as AgentUpsertPayload);
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4">
      <div className="bg-card border border-border rounded-2xl max-w-xl w-full max-h-[90vh] flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-muted/30">
          <div className="flex items-center gap-2">
            <div
              className="p-1.5 rounded-lg text-primary"
              style={{ backgroundColor: `${currentColor}20`, color: currentColor }}
            >
              <Bot size={16} />
            </div>
            <h2 className="text-sm font-bold text-foreground">
              {isEditing
                ? "Edit Custom Agent Persona"
                : "Create Autonomous Agent Persona"}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition cursor-pointer"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        {/* Body */}
        <form
          onSubmit={onSubmit}
          className="flex-1 overflow-y-auto p-6 space-y-4 text-xs"
        >
          {/* Identity row */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3.5">
            <div>
              <label className="block text-foreground font-medium mb-1">
                Agent Name *
              </label>
              <input
                type="text"
                required
                value={formData.name}
                onChange={(e) =>
                  setFormData({ ...formData, name: e.target.value })
                }
                placeholder="e.g. SEO Specialist"
                className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary transition-all"
              />
            </div>
            <div>
              <label className="block text-foreground font-medium mb-1">
                Role Title
              </label>
              <input
                type="text"
                value={formData.role}
                onChange={(e) =>
                  setFormData({ ...formData, role: e.target.value })
                }
                placeholder="e.g. Senior Technical SEO Strategist"
                className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary transition-all"
              />
            </div>
          </div>

          {/* Visual & runtime row */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3.5">
            <div>
              <label className="block text-foreground font-medium mb-1">
                Persona Icon
              </label>
              <select
                value={formData.icon}
                onChange={(e) =>
                  setFormData({ ...formData, icon: e.target.value })
                }
                className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-primary transition-all"
              >
                <option value="Sparkles">Sparkles</option>
                <option value="Code2">Code2</option>
                <option value="Search">Search</option>
                <option value="BarChart3">BarChart3</option>
                <option value="Bot">Bot</option>
              </select>
            </div>

            <div>
              <label className="block text-foreground font-medium mb-1">
                Core Color
              </label>
              <div className="flex flex-wrap items-center gap-1.5 h-[34px]">
                {PRESET_COLORS.map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() => handleColorChange(c)}
                    className={`w-4 h-4 rounded-full border-2 transition-all cursor-pointer ${
                      currentColor === c
                        ? "border-foreground scale-110"
                        : "border-transparent hover:scale-110"
                    }`}
                    style={{ backgroundColor: c }}
                    title={c}
                  />
                ))}
                {/* Custom Color Picker */}
                <div className="relative w-4 h-4 rounded-full border border-border overflow-hidden cursor-pointer flex items-center justify-center bg-muted hover:scale-110 transition-transform ml-auto">
                  <input
                    type="color"
                    value={currentColor}
                    onChange={(e) => handleColorChange(e.target.value)}
                    className="absolute inset-[-10px] w-[200%] h-[200%] opacity-0 cursor-pointer"
                    title="Custom color"
                  />
                  <span className="text-[9px] font-bold text-muted-foreground pointer-events-none">
                    +
                  </span>
                </div>
              </div>
            </div>

            <div>
              <label className="block text-foreground font-medium mb-1">
                Max Steps
              </label>
              <input
                type="number"
                min={5}
                max={200}
                value={formData.max_steps || 30}
                onChange={(e) =>
                  setFormData({
                    ...formData,
                    max_steps: parseInt(e.target.value) || 30,
                  })
                }
                className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-primary transition-all"
              />
            </div>
          </div>

          {/* Description */}
          <div>
            <label className="block text-foreground font-medium mb-1">
              Description
            </label>
            <input
              type="text"
              value={formData.description}
              onChange={(e) =>
                setFormData({ ...formData, description: e.target.value })
              }
              placeholder="Summary of agent capabilities and goals..."
              className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary transition-all"
            />
          </div>

          {/* System prompt */}
          <div>
            <label className="block text-foreground font-medium mb-1">
              System Prompt Instructions *
            </label>
            <textarea
              required
              rows={4}
              value={formData.system_prompt}
              onChange={(e) =>
                setFormData({ ...formData, system_prompt: e.target.value })
              }
              placeholder="You are an autonomous specialist agent. Your goals are..."
              className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary font-mono text-[11px] leading-relaxed resize-none transition-all"
            />
          </div>

          {/* Scoped tools */}
          <div>
            <label className="block text-foreground font-medium mb-2">
              Scoped Permitted Tools
            </label>
            <div className="grid grid-cols-2 gap-2 max-h-48 overflow-y-auto pr-1">
              {enabledTools.map((t) => {
                const isSelected = formData.tools.includes(t.id);
                return (
                  <button
                    type="button"
                    key={t.id}
                    onClick={() => onToggleTool(t.id)}
                    className={`flex items-center justify-between p-2.5 rounded-lg border text-left transition-all cursor-pointer ${
                      isSelected
                        ? "bg-primary/10 border-primary/40 text-primary font-medium shadow-xs"
                        : "bg-background border-border text-muted-foreground hover:border-border/80"
                    }`}
                    style={isSelected ? { color: currentColor, borderColor: `${currentColor}60`, backgroundColor: `${currentColor}15` } : {}}
                  >
                    <span className="font-mono text-[11px] truncate">
                      {t.name}
                    </span>
                    {isSelected && (
                      <Check size={14} style={{ color: currentColor }} className="flex-shrink-0" />
                    )}
                  </button>
                );
              })}

              {/* (intentionally no "mcp" pseudo-tool here — MCP
                  servers are selected in the dedicated section
                  below, which persists to ``formData.mcp_servers``
                  instead of the tools array. This keeps the two
                  concepts orthogonal: tools are functions the LLM
                  can call, MCP servers are protocol endpoints that
                  contribute their own tools at runtime.) */}
            </div>
          </div>

          {/* MCP protocol servers */}
          <div>
            <label className="block text-foreground font-medium mb-2 flex items-center justify-between">
              <span className="flex items-center gap-1.5">
                <Server size={13} className="text-primary" />
                MCP Protocol Servers
              </span>
              <span className="text-[10px] font-mono text-muted-foreground font-normal">
                {(formData.mcp_servers || []).length} selected
              </span>
            </label>
            <p className="text-[10px] text-muted-foreground mb-2 leading-relaxed">
              External MCP servers whose tools will be available to this
              agent at runtime. Toggle a server here to add or remove it
              from the manifest. Per-user overrides (disable / add extra
              servers) are managed separately from the card's Server
              button.
            </p>

            {mcpServers.length === 0 ? (
              <div className="p-3 text-[11px] text-muted-foreground italic border border-dashed border-border rounded-lg text-center">
                No MCP servers are registered. Install one from the
                Extensions section to make it available here.
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-2 max-h-48 overflow-y-auto pr-1">
                {mcpServers.map((srv) => {
                  const selected = (formData.mcp_servers || []).includes(
                    srv.id
                  );
                  const isActive = srv.status === "active";
                  return (
                    <button
                      type="button"
                      key={srv.id}
                      onClick={() => onToggleMcpServer(srv.id)}
                      className={`flex items-center justify-between p-2.5 rounded-lg border text-left transition-all cursor-pointer ${
                        selected
                          ? "bg-primary/10 border-primary/40 text-primary font-medium shadow-xs"
                          : "bg-background border-border text-muted-foreground hover:border-border/80"
                      }`}
                      style={
                        selected
                          ? {
                              color: currentColor,
                              borderColor: `${currentColor}60`,
                              backgroundColor: `${currentColor}15`,
                            }
                          : {}
                      }
                      title={srv.description || srv.id}
                    >
                      <div className="flex flex-col min-w-0">
                        <span className="font-mono text-[11px] truncate flex items-center gap-1">
                          {srv.name}
                          {srv.is_bundle && (
                            <span className="text-[9px] px-1 rounded bg-primary/10 text-primary font-normal">
                              bundle
                            </span>
                          )}
                          {!isActive && (
                            <span className="text-[9px] px-1 rounded bg-amber-500/10 text-amber-600 font-normal">
                              inactive
                            </span>
                          )}
                        </span>
                      </div>
                      {selected && (
                        <Check
                          size={14}
                          style={{ color: currentColor }}
                          className="flex-shrink-0"
                        />
                      )}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-end gap-2.5 pt-4 border-t border-border">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onClose}
              className="text-xs hover:bg-destructive/10 hover:text-destructive transition-colors cursor-pointer"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              size="sm"
              className="font-medium text-xs px-4 shadow-xs cursor-pointer text-white"
              style={{ backgroundColor: currentColor, hover: { backgroundColor: currentColor } } as any}
            >
              {isEditing ? "Update Agent" : "Save Agent"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
