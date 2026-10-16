"use client";

import React, { useState, useEffect, useMemo, useRef } from "react";
import { useRouter } from "next/navigation";
import { useChatStore } from "@/stores/chat-store";
import {
  fetchStoreAgents,
  fetchStoreTools,
  fetchStoreExtensions,
  createStoreAgent,
  updateStoreAgent,
  deleteStoreAgent
} from "@/lib/chatsApi";
import { AgentManifest, ToolDefinition, ExtensionDefinition, AgentUpsertPayload } from "@/lib/types";
import {
  Bot,
  Code2,
  Search,
  BarChart3,
  Sparkles,
  Store,
  Wrench,
  Puzzle,
  Plus,
  Trash2,
  Edit2,
  Check,
  ArrowRight,
  Terminal,
  Cpu,
  RefreshCw,
  X,
  Shield,
  CheckCircle2,
  XCircle,
  Play,
  Loader2,
  Upload,
  FileCode2,
  FileText,
  FileArchive,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ToastContainer, showToast } from "@/components/ui/ToastNotification";


import { BundleUploadModal } from "@/components/store/BundleUploadModal";
import { BundleCard } from "@/components/store/BundleCard";
import { BundleDetailsDrawer } from "@/components/store/BundleDetailsDrawer";
import {
  BundleEntry,
  deleteBundle as apiDeleteBundle,
  listBundles,
  activateVersion as apiActivateVersion,
} from "@/lib/bundleApi";


export default function StoresPage() {
  const router = useRouter();
  const { setSelectedAgentId } = useChatStore();

  const [activeTab, setActiveTab] = useState<"agents" | "tools" | "extensions">("agents");
  const [agents, setAgents] = useState<AgentManifest[]>([]);
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [bundles, setBundles] = useState<BundleEntry[]>([]);
  const [isBundleUploadOpen, setIsBundleUploadOpen] = useState(false);
  const [detailsBundle, setDetailsBundle] = useState<BundleEntry | null>(null);
  const [extensions, setExtensions] = useState<ExtensionDefinition[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [agentFilter, setAgentFilter] = useState<"all" | "builtin" | "custom" | "active" | "disabled">("all");
  const [toolFilter, setToolFilter] = useState<"all" | "builtin" | "custom">("all");

  // Lifecycle & Testing Action States
  const [togglingAgentId, setTogglingAgentId] = useState<string | null>(null);
  const [togglingToolId, setTogglingToolId] = useState<string | null>(null);
  const [togglingExtId, setTogglingExtId] = useState<string | null>(null);
  const [testingExtId, setTestingExtId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message: string }>>({});

  // Agent Builder Modal State
  const [isAgentModalOpen, setIsAgentModalOpen] = useState(false);
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null);
  const [agentFormData, setAgentFormData] = useState<AgentUpsertPayload>({
    name: "",
    role: "Specialist",
    icon: "Sparkles",
    description: "",
    system_prompt: "",
    tools: ["bash", "python_execute", "str_replace_editor", "web_search"],
    max_steps: 30
  });

  // Uploader Modal State (Custom Python Tool / MCP JSON)
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const [uploadCategory, setUploadCategory] = useState<"tool" | "extension">("tool");
  const [selectedUploadFile, setSelectedUploadFile] = useState<File | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const getApiUrl = (endpoint: string) => {
    if (typeof window !== "undefined" && (window.location.port === "3088" || window.location.port === "3000")) {
      return `http://localhost:8088${endpoint}`;
    }
    return endpoint;
  };

const loadData = async () => {
  setLoading(true);
  try {
    const [ag, tl, ex, bd] = await Promise.all([
      fetchStoreAgents(),
      fetchStoreTools(),
      fetchStoreExtensions(),
      listBundles().catch(() => [] as BundleEntry[]),
    ]);
    setAgents(ag || []);
    setTools(tl || []);
    setExtensions(ex || []);
    setBundles(bd || []);
  } catch (err: any) {
    console.error("Failed to load store catalog:", err);
    showToast.error("Store Load Error", err.message || "Failed to load store catalog.");
  } finally {
    setLoading(false);
  }
};

  useEffect(() => {
    loadData();
  }, []);

  const handleStartChatWithAgent = (agent: AgentManifest) => {
    if (agent.status === "disabled") {
      showToast.warning("Agent Disabled", `Agent '${agent.name}' is currently disabled. Toggle it active to use.`);
      return;
    }
    setSelectedAgentId(agent.id);
    if (typeof window !== "undefined") {
      localStorage.setItem("omweb_exec_mode", "agent");
      window.dispatchEvent(new CustomEvent("omweb:mode-change", { detail: "agent" }));
    }
    router.push("/chat");
  };

  // Agent Toggle Action
  const handleToggleAgent = async (agentId: string) => {
    if (agentId === "peldrun") {
      showToast.info("Protected Core", "Default primary agent 'peldrun' cannot be disabled.");
      return;
    }
    setTogglingAgentId(agentId);
    try {
      const res = await fetch(getApiUrl(`/api/store/agents/${agentId}/toggle`), {
        method: "POST"
      });
      if (res.ok) {
        const data = await res.json();
        const updatedAgent = data.agent;
        setAgents((prev) =>
          prev.map((a) => (a.id === agentId ? { ...a, status: updatedAgent.status } : a))
        );
        showToast.success("Agent Status Updated", `${updatedAgent.name || agentId} is now ${updatedAgent.status}`);
      } else {
        const err = await res.json();
        showToast.error("Toggle Failed", err.detail || "Could not toggle agent status.");
      }
    } catch (err: any) {
      showToast.error("Network Error", err.message);
    } finally {
      setTogglingAgentId(null);
    }
  };

  // Tool Toggle Action
  const handleToggleTool = async (toolId: string) => {
    setTogglingToolId(toolId);
    try {
      const res = await fetch(getApiUrl(`/api/store/tools/${toolId}/toggle`), {
        method: "POST"
      });
      if (res.ok) {
        const data = await res.json();
        const updatedTool = data.tool;
        setTools((prev) =>
          prev.map((t) => (t.id === toolId ? { ...t, is_enabled: updatedTool.is_enabled, status: updatedTool.status } : t))
        );
        showToast.success("Tool Status Updated", `${updatedTool.name || toolId} is now ${updatedTool.status}`);
      } else {
        const err = await res.json();
        showToast.error("Toggle Failed", err.detail || "Could not toggle tool status.");
      }
    } catch (err: any) {
      showToast.error("Network Error", err.message);
    } finally {
      setTogglingToolId(null);
    }
  };

  // Delete Custom Tool Action
  const handleDeleteTool = async (toolId: string) => {
    if (!confirm(`Are you sure you want to permanently delete custom tool '${toolId}'?`)) return;
    try {
      const res = await fetch(getApiUrl(`/api/store/tools/${toolId}`), {
        method: "DELETE"
      });
      if (res.ok) {
        setTools((prev) => prev.filter((t) => t.id !== toolId));
        showToast.success("Tool Deleted", `Custom tool '${toolId}' removed successfully.`);
      } else {
        const err = await res.json();
        showToast.error("Delete Failed", err.detail || "Cannot delete tool.");
      }
    } catch (err: any) {
      showToast.error("Delete Error", err.message);
    }
  };

  // ----------------------------------------------------------------
// Bundle lifecycle handlers
// ----------------------------------------------------------------

const handleBundleDelete = async (bundle: BundleEntry) => {
  const hasMultipleVersions = bundle.installed_versions.length > 1;
  const confirmed = confirm(
    hasMultipleVersions
      ? `Delete ALL versions of bundle '${bundle.id}'? This cannot be undone.`
      : `Delete bundle '${bundle.id}' v${bundle.active_version}?`
  );
  if (!confirmed) return;

  try {
    await apiDeleteBundle(bundle.id, hasMultipleVersions);
    showToast.success("Bundle Deleted", `${bundle.id} removed.`);
    loadData();
  } catch (err: any) {
    showToast.error("Delete Failed", err.message || "Could not delete bundle.");
  }
};

const handleBundleActivate = async (bundle: BundleEntry, version: string) => {
  try {
    await apiActivateVersion(bundle.id, version);
    showToast.success("Version Activated", `${bundle.id} → v${version}`);
    loadData();
    // Refresh drawer if open
    if (detailsBundle && detailsBundle.id === bundle.id) {
      setDetailsBundle({ ...bundle, active_version: version });
    }
  } catch (err: any) {
    showToast.error("Activation Failed", err.message || "Could not activate.");
  }
};
  // Extension Toggle Action
  const handleToggleExtension = async (extId: string) => {
    setTogglingExtId(extId);
    try {
      const res = await fetch(getApiUrl(`/api/mcp/servers/${extId}/toggle`), {
        method: "POST"
      });
      if (res.ok) {
        const data = await res.json();
        const newStatus = data.server?.status || "ready";
        setExtensions((prev) =>
          prev.map((e) => (e.id === extId ? { ...e, status: newStatus } : e))
        );
        showToast.success("Extension Updated", `${extId} is now ${newStatus}`);
      } else {
        const err = await res.json();
        showToast.error("Toggle Failed", err.detail || "Could not toggle extension status.");
      }
    } catch (err: any) {
      showToast.error("Network Error", err.message);
    } finally {
      setTogglingExtId(null);
    }
  };

  // Extension Test Connection Action
  const handleTestExtension = async (extId: string) => {
    setTestingExtId(extId);
    try {
      const res = await fetch(getApiUrl("/api/mcp/test"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ server_id: extId })
      });
      const data = await res.json();
      const isOk = data.status === "ok";
      setTestResults((prev) => ({
        ...prev,
        [extId]: { ok: isOk, message: data.message }
      }));
      if (isOk) {
        showToast.success("MCP Test Succeeded", data.message);
      } else {
        showToast.warning("MCP Test Warning", data.message);
      }
    } catch (err: any) {
      setTestResults((prev) => ({
        ...prev,
        [extId]: { ok: false, message: err.message || "Failed to reach backend." }
      }));
      showToast.error("Test Error", err.message || "Failed to reach backend.");
    } finally {
      setTestingExtId(null);
    }
  };

  // Agent Creation / Editing Handlers
  const handleOpenCreateAgentModal = () => {
    setEditingAgentId(null);
    setAgentFormData({
      name: "",
      role: "Specialist",
      icon: "Sparkles",
      description: "",
      system_prompt: "",
      tools: ["bash", "python_execute", "str_replace_editor", "web_search"],
      max_steps: 30
    });
    setIsAgentModalOpen(true);
  };

  const handleOpenEditAgentModal = (ag: AgentManifest) => {
    setEditingAgentId(ag.id);
    setAgentFormData({
      name: ag.name,
      role: ag.role || "Specialist",
      icon: ag.icon || "Sparkles",
      description: ag.description || "",
      system_prompt: ag.system_prompt,
      tools: ag.tools || [],
      max_steps: ag.max_steps || 30
    });
    setIsAgentModalOpen(true);
  };

  const handleDeleteAgent = async (agentId: string) => {
    if (!confirm("Are you sure you want to delete this custom agent?")) return;
    try {
      const ok = await deleteStoreAgent(agentId);
      if (ok) {
        setAgents((prev) => prev.filter((a) => a.id !== agentId));
        showToast.success("Agent Deleted", `Agent ${agentId} removed successfully.`);
      } else {
        showToast.error("Delete Failed", "Cannot delete built-in agent.");
      }
    } catch (err: any) {
      showToast.error("Delete Error", err.message);
    }
  };

  const handleToggleAgentToolAssignment = (toolId: string) => {
    setAgentFormData((prev) => {
      const exists = prev.tools.includes(toolId);
      return {
        ...prev,
        tools: exists ? prev.tools.filter((t) => t !== toolId) : [...prev.tools, toolId]
      };
    });
  };

  const handleSubmitAgentModal = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!agentFormData.name.trim() || !agentFormData.system_prompt.trim()) {
      showToast.warning("Validation Error", "Agent name and system prompt are required.");
      return;
    }

    try {
      if (editingAgentId) {
        const updated = await updateStoreAgent(editingAgentId, agentFormData);
        if (updated) {
          setAgents((prev) => prev.map((a) => (a.id === editingAgentId ? updated : a)));
          setIsAgentModalOpen(false);
          showToast.success("Agent Updated", `Updated ${agentFormData.name} successfully.`);
        }
      } else {
        const created = await createStoreAgent(agentFormData);
        if (created) {
          setAgents((prev) => [...prev, created]);
          setIsAgentModalOpen(false);
          showToast.success("Agent Created", `Created ${agentFormData.name} successfully.`);
        }
      }
    } catch (err: any) {
      showToast.error("Save Failed", err.message || "Could not save agent configuration.");
    }
  };

  // Upload Modal Handler (Python Tool or MCP JSON)
  const handleUploadSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedUploadFile) {
      showToast.warning("No File Selected", "Please choose a file to upload.");
      return;
    }

    setIsUploading(true);
    const formData = new FormData();
    formData.append("file", selectedUploadFile);

    try {
      const endpoint = uploadCategory === "tool" ? "/api/store/tools/upload" : "/api/store/extensions/upload";
      const res = await fetch(getApiUrl(endpoint), {
        method: "POST",
        body: formData
      });

      if (res.ok) {
        showToast.success(
          uploadCategory === "tool" ? "Tool Uploaded" : "Extension Uploaded",
          `Successfully registered ${selectedUploadFile.name}`
        );
        setIsUploadModalOpen(false);
        setSelectedUploadFile(null);
        loadData();
      } else {
        const err = await res.json();
        showToast.error("Upload Failed", err.detail || "Server rejected the upload file.");
      }
    } catch (err: any) {
      showToast.error("Upload Error", err.message || "Failed to upload file.");
    } finally {
      setIsUploading(false);
    }
  };

  const renderAgentIcon = (iconName?: string) => {
    switch ((iconName || "").toLowerCase()) {
      case "code2":
        return <Code2 className="h-5 w-5 text-cyan-600 dark:text-cyan-400" />;
      case "search":
        return <Search className="h-5 w-5 text-amber-600 dark:text-amber-400" />;
      case "barchart3":
        return <BarChart3 className="h-5 w-5 text-purple-600 dark:text-purple-400" />;
      case "sparkles":
        return <Sparkles className="h-5 w-5 text-primary" />;
      default:
        return <Bot className="h-5 w-5 text-primary" />;
    }
  };

  const filteredAgents = useMemo(() => {
    return agents.filter((ag) => {
      const q = searchQuery.toLowerCase().trim();
      const matchesQuery =
        !q ||
        ag.name.toLowerCase().includes(q) ||
        (ag.description || "").toLowerCase().includes(q) ||
        (ag.role || "").toLowerCase().includes(q);

      if (!matchesQuery) return false;
      if (agentFilter === "builtin") return ag.is_builtin;
      if (agentFilter === "custom") return !ag.is_builtin;
      if (agentFilter === "active") return ag.status !== "disabled";
      if (agentFilter === "disabled") return ag.status === "disabled";
      return true;
    });
  }, [agents, searchQuery, agentFilter]);

  const filteredTools = useMemo(() => {
    return tools.filter((tl) => {
      const q = searchQuery.toLowerCase().trim();
      const matchesQuery =
        !q ||
        tl.name.toLowerCase().includes(q) ||
        (tl.description || "").toLowerCase().includes(q) ||
        (tl.category || "").toLowerCase().includes(q);

      if (!matchesQuery) return false;
      if (toolFilter === "builtin") return tl.is_builtin;
      if (toolFilter === "custom") return !tl.is_builtin;
      return true;
    });
  }, [tools, searchQuery, toolFilter]);

  const filteredExtensions = useMemo(() => {
    const q = searchQuery.toLowerCase().trim();
    return extensions.filter(
      (ex) =>
        !q ||
        ex.name.toLowerCase().includes(q) ||
        (ex.description || "").toLowerCase().includes(q)
    );
  }, [extensions, searchQuery]);

  return (
    <div className="w-full h-full flex flex-col flex-1 bg-background text-foreground overflow-hidden font-sans">
      <ToastContainer />

      {/* Header Bar */}
      <header className="w-full border-b border-border bg-custom backdrop-blur-md sticky top-0 z-10">
        <div className="mx-auto w-full px-4 sm:px-6 lg:px-8 py-3.5 flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-sm bg-primary/10 text-primary border border-primary/20 shadow-xs">
              <Store className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-base font-bold text-foreground tracking-tight">
                  Sovereign Capability Store
                </h1>
                <span className="hidden sm:inline-flex px-2 py-0.5 text-[10px] font-mono font-medium rounded-full bg-muted text-muted-foreground border border-border">
                  v3.0
                </span>
              </div>
              <p className="text-xs text-muted-foreground">
                Autonomous Personas, Sovereign Tools, and MCP Protocol Extensions
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2.5">
            <Button
              variant="outline"
              size="sm"
              onClick={loadData}
              disabled={loading}
              className="text-xs border-border bg-muted/50 hover:bg-muted text-foreground transition shadow-xs h-8 cursor-pointer"
            >
              <RefreshCw size={13} className={`mr-1.5 ${loading ? "animate-spin text-primary" : ""}`} />
              Refresh
            </Button>

            {activeTab === "agents" && (
              <Button
                size="sm"
                onClick={handleOpenCreateAgentModal}

                className="primary bg-rb hover:bg-rb/60 text-primary-foreground hover:text-card font-medium text-xs shadow-xs h-8 px-3.5 transition cursor-pointer"
              >
                <Plus size={14} className="mr-1.5" />
                New Agent
              </Button>
            )}

            {activeTab === "tools" && (
              <>
                <Button
                  size="sm"
                  onClick={() => setIsBundleUploadOpen(true)}
                  className="primary bg-rb hover:bg-rb/80 hover:shadow-md text-primary-foreground font-medium text-xs shadow-xs h-8 px-3.5 transition cursor-pointer"
                >
                  <FileArchive size={14} className="mr-1.5" />
                  Install Bundle (.zip)
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setUploadCategory("tool");
                    setSelectedUploadFile(null);
                    setIsUploadModalOpen(true);
                  }}
                  className="text-xs border-border hover:bg-muted h-8 px-3 transition cursor-pointer"
                >
                  <Upload size={14} className="mr-1.5" />
                  Upload Single File
                </Button>
              </>
            )}

            {activeTab === "extensions" && (
              <Button
                size="sm"
                onClick={() => {
                  setUploadCategory("extension");
                  setSelectedUploadFile(null);
                  setIsUploadModalOpen(true);
                }}
                className="primary bg-primary hover:bg-primary/90 text-primary hover:text-primary-foreground font-medium text-xs shadow-xs h-8 px-3.5 transition cursor-pointer"
              >
                <Upload size={14} className="mr-1.5" />
                Upload MCP (.json)
              </Button>
            )}
          </div>
        </div>
      </header>

      {/* Navigation Tabs and Search Bar */}
      <div className="w-full border-b border-border bg-card">
        <div className="max-w-[1240px] mx-auto w-full px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          {/* Main Tabs */}
          <div className="flex items-center gap-1 overflow-x-auto pt-2 scrollbar-none">
            <button
              type="button"
              onClick={() => setActiveTab("agents")}
              className={`flex items-center gap-2 px-4 py-2.5 text-xs font-semibold border-b-2 whitespace-nowrap transition-all cursor-pointer ${
                activeTab === "agents"
                  ? "border-primary text-primary bg-primary/5"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              <Bot size={15} />
              <span>Agents Hub</span>
              <span className="ml-1 px-1.5 py-0.5 rounded-full text-[10px] font-mono bg-muted text-muted-foreground border border-border">
                {agents.length}
              </span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("tools")}
              className={`flex items-center gap-2 px-4 py-2.5 text-xs font-semibold border-b-2 whitespace-nowrap transition-all cursor-pointer ${
                activeTab === "tools"
                  ? "border-primary text-primary bg-primary/5"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              <Wrench size={15} />
              <span>Sovereign Tools</span>
              <span className="ml-1 px-1.5 py-0.5 rounded-full text-[10px] font-mono bg-muted text-muted-foreground border border-border">
                {tools.length}
              </span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("extensions")}
              className={`flex items-center gap-2 px-4 py-2.5 text-xs font-semibold border-b-2 whitespace-nowrap transition-all cursor-pointer ${
                activeTab === "extensions"
                  ? "border-primary text-primary bg-primary/5"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              <Puzzle size={15} />
              <span>Extensions & MCP</span>
              <span className="ml-1 px-1.5 py-0.5 rounded-full text-[10px] font-mono bg-muted text-muted-foreground border border-border">
                {extensions.length}
              </span>
            </button>
          </div>

          {/* Search Box & Filters */}
          <div className="flex items-center gap-2.5 pb-2 sm:pb-0">
            <div className="relative w-full sm:w-64">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground h-3.5 w-3.5" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder={`Search ${activeTab}...`}
                className="w-full pl-8 pr-7 py-1.5 text-xs rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary shadow-xs"
              />
              {searchQuery && (
                <button
                  type="button"
                  onClick={() => setSearchQuery("")}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground cursor-pointer"
                >
                  <X size={12} />
                </button>
              )}
            </div>

            {activeTab === "agents" && (
              <div className="flex items-center p-0.5 rounded-lg bg-muted border border-border text-[11px]">
                <button
                  type="button"
                  onClick={() => setAgentFilter("all")}
                  className={`px-2.5 py-1 rounded-md transition cursor-pointer ${
                    agentFilter === "all" ? "bg-card text-foreground shadow-xs font-semibold" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  All
                </button>
                <button
                  type="button"
                  onClick={() => setAgentFilter("builtin")}
                  className={`px-2.5 py-1 rounded-md transition cursor-pointer ${
                    agentFilter === "builtin" ? "bg-card text-sky-600 dark:text-sky-400 shadow-xs font-semibold" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Built-in
                </button>
                <button
                  type="button"
                  onClick={() => setAgentFilter("custom")}
                  className={`px-2.5 py-1 rounded-md transition cursor-pointer ${
                    agentFilter === "custom" ? "bg-card text-primary shadow-xs font-semibold" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Custom
                </button>
              </div>
            )}

            {activeTab === "tools" && (
              <div className="flex items-center p-0.5 rounded-lg bg-muted border border-border text-[11px]">
                <button
                  type="button"
                  onClick={() => setToolFilter("all")}
                  className={`px-2.5 py-1 rounded-md transition cursor-pointer ${
                    toolFilter === "all" ? "bg-card text-foreground shadow-xs font-semibold" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  All
                </button>
                <button
                  type="button"
                  onClick={() => setToolFilter("builtin")}
                  className={`px-2.5 py-1 rounded-md transition cursor-pointer ${
                    toolFilter === "builtin" ? "bg-card text-sky-600 dark:text-sky-400 shadow-xs font-semibold" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Built-in
                </button>
                <button
                  type="button"
                  onClick={() => setToolFilter("custom")}
                  className={`px-2.5 py-1 rounded-md transition cursor-pointer ${
                    toolFilter === "custom" ? "bg-card text-primary shadow-xs font-semibold" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Custom
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Content Area */}
      <main className="w-full flex-1 overflow-y-auto scrollbar-thin bg-background">
        <div className="max-w-[1240px] mx-auto w-full px-4 sm:px-6 lg:px-8 py-6">
          {/* TAB 1: Agents Hub */}
          {activeTab === "agents" && (
            <div>
              {filteredAgents.length === 0 ? (
                <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
                  <Bot className="h-10 w-10 text-muted-foreground mb-3" />
                  <h3 className="text-sm font-semibold text-foreground">No agents match your criteria</h3>
                  <p className="text-xs text-muted-foreground max-w-sm mt-1">
                    Try adjusting your search query or create a new custom agent.
                  </p>
                </div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
                  {filteredAgents.map((ag) => {
                    const isDisabled = ag.status === "disabled";
                    const isToggling = togglingAgentId === ag.id;

                    return (
                      <div
                        key={ag.id}
                        className={`group relative flex flex-col justify-between p-5 rounded-sm border transition-all duration-200 shadow-sm hover:shadow-md ${
                          isDisabled
                            ? "bg-muted/30 border-border/60 opacity-80"
                            : "bg-card border-transparent hover:border-muted/40 shadow-md"
                        }`}
                      >
                        <div>
                          {/* Card Header & Status Switch */}
                          <div className="flex items-start justify-between mb-3.5">
                            <div className="flex items-center gap-3">
                              <div className="p-2.5 rounded-sm bg-muted border border-border shadow-xs group-hover:scale-105 transition-transform">
                                {renderAgentIcon(ag.icon)}
                              </div>
                              <div>
                                <h3 className="text-sm font-bold text-foreground group-hover:text-primary transition-colors flex items-center gap-2">
                                  {ag.name}
                                </h3>
                                <span className="text-[11px] font-mono text-primary">
                                  {ag.role || "Autonomous Agent"}
                                </span>
                              </div>
                            </div>

                            {/* Toggle Switch */}
                            <div className="flex items-center gap-2">
                              {ag.id !== "peldrun" && (
                                <button
                                  type="button"
                                  onClick={() => handleToggleAgent(ag.id)}
                                  disabled={isToggling}
                                  className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                                    !isDisabled ? "bg-cgood" : "bg-[#777171]"
                                  }`}
                                  title={!isDisabled ? "Disable agent" : "Enable agent"}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-background shadow-md ring-0 transition duration-200 ease-in-out ${
                                      !isDisabled ? "translate-x-4" : "translate-x-0"
                                    }`}
                                  />
                                </button>
                              )}
                            </div>
                          </div>

                          <div className="flex items-center gap-2 mb-3">
                            {ag.is_builtin ? (
                              <span className="px-2 py-0.5 rounded-md text-[10px] font-mono uppercase font-semibold bg-sky-500/10 border border-sky-500/30 text-sky-700 dark:text-sky-300">
                                BUILT-IN
                              </span>
                            ) : (
                              <span className="px-2 py-0.5 rounded-md text-[10px] font-mono uppercase font-semibold bg-primary/10 border border-primary/30 text-primary">
                                CUSTOM
                              </span>
                            )}
                            <span
                              className={`px-2 py-0.5 rounded-md text-[10px] font-mono uppercase font-semibold ${
                                !isDisabled
                                  ? "bg-emerald-500/10 border border-emerald-500/30 text-emerald-600 dark:text-emerald-400"
                                  : "bg-muted text-muted-foreground border border-border"
                              }`}
                            >
                              {!isDisabled ? "ACTIVE" : "DISABLED"}
                            </span>
                          </div>

                          <p className="text-xs text-muted-foreground line-clamp-2 mb-4 leading-relaxed">
                            {ag.description || "Custom configured autonomous specialist."}
                          </p>

                          <div className="mb-4">
                            <span className="text-[10px] uppercase font-semibold text-muted-foreground tracking-wider block mb-1.5">
                              Scoped Tools ({ag.tools?.length || 0})
                            </span>
                            <div className="flex flex-wrap gap-1.5 max-h-16 overflow-y-auto scrollbar-none">
                              {ag.tools && ag.tools.length > 0 ? (
                                ag.tools.map((t) => (
                                  <span
                                    key={t}
                                    className="px-2 py-0.5 rounded-md bg-muted text-[10px] text-muted-foreground font-mono border border-border"
                                  >
                                    {t}
                                  </span>
                                ))
                              ) : (
                                <span className="text-[11px] text-muted-foreground italic">No tools bound</span>
                              )}
                            </div>
                          </div>
                        </div>

                        {/* Card Actions Footer */}
                        <div className="pt-3.5 border-t border-border flex items-center justify-between">
                          <div className="flex items-center gap-1">
                            {!ag.is_builtin && (
                              <>
                                <button
                                  type="button"
                                  onClick={() => handleOpenEditAgentModal(ag)}
                                  className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition cursor-pointer"
                                  title="Edit Agent"
                                >
                                  <Edit2 size={13} />
                                </button>
                                <button
                                  type="button"
                                  onClick={() => handleDeleteAgent(ag.id)}
                                  className="p-1.5 rounded-lg hover:bg-destructive/10 text-muted-foreground hover:text-destructive transition cursor-pointer"
                                  title="Delete Agent"
                                >
                                  <Trash2 size={13} />
                                </button>
                              </>
                            )}
                          </div>

                          <Button
                            size="sm"
                            disabled={isDisabled}
                            onClick={() => handleStartChatWithAgent(ag)}
                            className="bg-primary hover:bg-rb/90 text-primary hover:text-primary-foreground text-primary text-xs px-3.5 h-8 font-medium shadow-xs transition group/btn cursor-pointer disabled:opacity-40"
                          >
                            <span>Start Chat</span>
                            <ArrowRight size={12} className="ml-1 group-hover/btn:translate-x-0.5 transition-transform" />
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {/* TAB 2: Sovereign Tools Catalog */}
          {activeTab === "tools" && (
            <div className="space-y-6">
              {/* ── Bundle Installs Section ────────────────────────────── */}
              {bundles.length > 0 && (
                <div>
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      <FileArchive size={14} className="text-primary" />
                      <h3 className="text-sm font-bold text-foreground">
                        Installed Bundles
                      </h3>
                      <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground border border-border">
                        {bundles.length}
                      </span>
                    </div>
                    <span className="text-[10px] text-muted-foreground font-mono">
                      Content-addressed · Atomic install · Rollback ready
                    </span>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
                    {bundles.map((bd) => (
                      <BundleCard
                        key={bd.id}
                        bundle={bd}
                        onActivate={(version) => handleBundleActivate(bd, version)}
                        onDelete={() => handleBundleDelete(bd)}
                        onViewDetails={() => setDetailsBundle(bd)}
                      />
                    ))}
                  </div>
                </div>
              )}

              {/* ── Legacy Tools Section ──────────────────────────────── */}
              <div>
                {bundles.length > 0 && (
                  <div className="flex items-center gap-2 mb-3">
                    <Wrench size={14} className="text-muted-foreground" />
                    <h3 className="text-sm font-bold text-foreground">Single-File Tools</h3>
                    <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground border border-border">
                      {filteredTools.length}
                    </span>
                  </div>
                )}

                {filteredTools.length === 0 ? (
                  <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
                    <Wrench className="h-10 w-10 text-muted-foreground mb-3" />
                    <h3 className="text-sm font-semibold text-foreground">No tools found</h3>
                    <p className="text-xs text-muted-foreground mt-1">
                      Upload a custom Python tool (.py) or JSON manifest to extend capabilities.
                    </p>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
                    {filteredTools.map((tl) => {
                      const isEnabled = tl.is_enabled !== false && tl.status !== "disabled";
                      const isToggling = togglingToolId === tl.id;

                      return (
                        <div
                          key={tl.id}
                          className={`p-5 rounded-sm border bg-card shadow-sm hover:shadow-md transition-all flex flex-col justify-between ${
                            !isEnabled ? "opacity-75 bg-muted/20 border-border/60" : "border-transparent"
                          }`}
                        >
                          <div>
                            <div className="flex items-center justify-between mb-3">
                              <div className="flex items-center gap-2.5">
                                <div className="p-2 rounded-lg bg-primary/10 text-primary border border-primary/20">
                                  {tl.id.includes("python") ? (
                                    <Code2 size={15} />
                                  ) : tl.id.includes("bash") ? (
                                    <Terminal size={15} />
                                  ) : (
                                    <Wrench size={15} />
                                  )}
                                </div>
                                <h3 className="text-sm font-bold text-foreground font-mono">
                                  {tl.name}
                                </h3>
                              </div>

                              {/* Enable/Disable Toggle */}
                              <button
                                type="button"
                                onClick={() => handleToggleTool(tl.id)}
                                disabled={isToggling}
                                className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                                  isEnabled ? "bg-cgood" : "bg-[#777171]"
                                }`}
                                title={isEnabled ? "Disable tool" : "Enable tool"}
                              >
                                <span
                                  className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-background shadow-md ring-0 transition duration-200 ease-in-out ${
                                    isEnabled ? "translate-x-4" : "translate-x-0"
                                  }`}
                                />
                              </button>
                            </div>

                            <div className="flex items-center gap-2 mb-3">
                              <span className="px-2 py-0.5 rounded-sm text-[10px] font-mono uppercase bg-rby/20 text-black border border-rby">
                                {tl.category || "custom"}
                              </span>
                              {tl.is_builtin ? (
                                <span className="px-1.5 py-0.5  text-[10px] font-mono uppercase bg-sky-500/10 text-sky-700 dark:text-sky-300 border border-sky-500/20">
                                  BUILT-IN
                                </span>
                              ) : (
                                <span className="px-1.5 py-0.5 rounded text-[10px] font-mono uppercase bg-primary/10 text-primary border border-primary/20">
                                  CUSTOM
                                </span>
                              )}
                            </div>

                            <p className="text-xs text-muted-foreground leading-relaxed mb-4">
                              {tl.description}
                            </p>
                          </div>

                          {/* Tool Footer */}
                          <div className="pt-3 border-t border-border flex items-center justify-between text-[11px] font-mono">
                            <div className="flex items-center gap-1.5 text-muted-foreground">
                              <Shield size={12} />
                              <span className="capitalize">{tl.safety_level || "safe"}</span>
                            </div>

                            {!tl.is_builtin && (
                              <button
                                type="button"
                                onClick={() => handleDeleteTool(tl.id)}
                                className="p-1 rounded text-muted-foreground hover:text-destructive transition cursor-pointer"
                                title="Delete custom tool"
                              >
                                <Trash2 size={13} />
                              </button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* TAB 3: Extensions & MCP */}
          {activeTab === "extensions" && (
            <div>
              {filteredExtensions.length === 0 ? (
                <div className="flex flex-col items-center justify-center p-12 text-center rounded-2xl border border-dashed border-border bg-card/40">
                  <Puzzle className="h-10 w-10 text-muted-foreground mb-3" />
                  <h3 className="text-sm font-semibold text-foreground">No extensions found</h3>
                  <p className="text-xs text-muted-foreground mt-1">
                    Upload an MCP server manifest (.json) to connect external tool protocols.
                  </p>
                </div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
                  {filteredExtensions.map((ex) => {
                    const isActive = ex.status === "active";
                    const isToggling = togglingExtId === ex.id;
                    const isTesting = testingExtId === ex.id;
                    const testResult = testResults[ex.id];

                    return (
                      <div
                        key={ex.id}
                        className={`p-5 rounded-sm border bg-card shadow-xs hover:shadow-md transition-all flex flex-col justify-between ${
                          isActive ? "border-primary/40 bg-primary/5" : "border-border"
                        }`}
                      >
                        <div>
                          <div className="flex items-center justify-between mb-3">
                            <div className="flex items-center gap-2.5">
                              <div className="p-2 rounded-lg bg-primary/10 text-primary border border-primary/20">
                                <Cpu size={16} />
                              </div>
                              <h3 className="text-sm font-bold text-foreground">
                                {ex.name}
                              </h3>
                            </div>

                            {/* Live Toggle Switch */}
                            <button
                              type="button"
                              onClick={() => handleToggleExtension(ex.id)}
                              disabled={isToggling}
                              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                                isActive ? "bg-cgood" : "bg-[#777171]"
                              }`}
                              title={isActive ? "Disable extension" : "Activate extension"}
                            >
                              <span
                                className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-background shadow-md ring-0 transition duration-200 ease-in-out ${
                                  isActive ? "translate-x-4" : "translate-x-0"
                                }`}
                              />
                            </button>
                          </div>

                          <p className="text-xs text-muted-foreground mb-4 leading-relaxed">
                            {ex.description}
                          </p>

                          {ex.command && (
                            <div className="p-2.5 rounded-lg bg-muted/50 border border-border text-[11px] font-mono text-muted-foreground truncate select-all mb-3">
                              <span className="text-primary mr-1.5">$</span>
                              {ex.command}
                            </div>
                          )}

                          {/* Inline Test Result Box */}
                          {testResult && (
                            <div
                              className={`p-2.5 rounded-lg text-xs flex items-start gap-2 border mb-3 ${
                                testResult.ok
                                  ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-600 dark:text-emerald-400"
                                  : "bg-destructive/10 border-destructive/20 text-destructive"
                              }`}
                            >
                              {testResult.ok ? (
                                <CheckCircle2 size={14} className="shrink-0 mt-0.5" />
                              ) : (
                                <XCircle size={14} className="shrink-0 mt-0.5" />
                              )}
                              <span className="leading-tight">{testResult.message}</span>
                            </div>
                          )}
                        </div>

                        {/* Extension Card Actions Footer */}
                        <div className="pt-3 border-t border-border flex items-center justify-between">
                          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                            <span
                              className={`h-1.5 w-1.5 rounded-full ${
                                isActive ? "bg-emerald-500 animate-pulse" : "bg-muted-foreground"
                              }`}
                            />
                            <span className="capitalize">{ex.status}</span>
                          </div>

                          <button
                            type="button"
                            onClick={() => handleTestExtension(ex.id)}
                            disabled={isTesting}
                            className="flex items-center gap-1 text-[11px] px-2.5 py-1 rounded bg-secondary hover:bg-secondary/80 text-secondary-foreground font-medium transition cursor-pointer"
                          >
                            {isTesting ? (
                              <Loader2 size={11} className="animate-spin text-primary" />
                            ) : (
                              <Play size={10} className="text-primary fill-primary" />
                            )}
                            <span>Test Connection</span>
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      </main>

      {/* Modal 1: Create / Edit Custom Agent (Agent Builder) */}
      {isAgentModalOpen && (
        <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4">
          <div className="bg-card border border-border rounded-2xl max-w-xl w-full max-h-[90vh] flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150">
            <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-muted/30">
              <div className="flex items-center gap-2">
                <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
                  <Bot size={16} />
                </div>
                <h2 className="text-sm font-bold text-foreground">
                  {editingAgentId ? "Edit Custom Agent Persona" : "Create Autonomous Agent Persona"}
                </h2>
              </div>
              <button
                type="button"
                onClick={() => setIsAgentModalOpen(false)}
                className="p-1 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition cursor-pointer"
              >
                <X size={16} />
              </button>
            </div>

            <form onSubmit={handleSubmitAgentModal} className="flex-1 overflow-y-auto p-6 space-y-4 text-xs">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3.5">
                <div>
                  <label className="block text-foreground font-medium mb-1">
                    Agent Name *
                  </label>
                  <input
                    type="text"
                    required
                    value={agentFormData.name}
                    onChange={(e) => setAgentFormData({ ...agentFormData, name: e.target.value })}
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
                    value={agentFormData.role}
                    onChange={(e) => setAgentFormData({ ...agentFormData, role: e.target.value })}
                    placeholder="e.g. Senior Technical SEO Strategist"
                    className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary transition-all"
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3.5">
                <div>
                  <label className="block text-foreground font-medium mb-1">
                    Persona Icon
                  </label>
                  <select
                    value={agentFormData.icon}
                    onChange={(e) => setAgentFormData({ ...agentFormData, icon: e.target.value })}
                    className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-primary transition-all"
                  >
                    <option value="Sparkles">Sparkles (Specialist)</option>
                    <option value="Code2">Code2 (Developer)</option>
                    <option value="Search">Search (Researcher)</option>
                    <option value="BarChart3">BarChart3 (Analyst)</option>
                    <option value="Bot">Bot (General Purpose)</option>
                  </select>
                </div>
                <div>
                  <label className="block text-foreground font-medium mb-1">
                    Max Steps Execution
                  </label>
                  <input
                    type="number"
                    min={5}
                    max={100}
                    value={agentFormData.max_steps || 30}
                    onChange={(e) => setAgentFormData({ ...agentFormData, max_steps: parseInt(e.target.value) || 30 })}
                    className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-primary transition-all"
                  />
                </div>
              </div>

              <div>
                <label className="block text-foreground font-medium mb-1">
                  Description
                </label>
                <input
                  type="text"
                  value={agentFormData.description}
                  onChange={(e) => setAgentFormData({ ...agentFormData, description: e.target.value })}
                  placeholder="Summary of agent capabilities and goals..."
                  className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary transition-all"
                />
              </div>

              <div>
                <label className="block text-foreground font-medium mb-1">
                  System Prompt Instructions *
                </label>
                <textarea
                  required
                  rows={4}
                  value={agentFormData.system_prompt}
                  onChange={(e) => setAgentFormData({ ...agentFormData, system_prompt: e.target.value })}
                  placeholder="You are an autonomous specialist agent. Your goals are..."
                  className="w-full px-3 py-2 rounded-lg bg-background border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary font-mono text-[11px] leading-relaxed resize-none transition-all"
                />
              </div>

              {/* Scoped Permitted Tools Selection */}
              <div>
                <label className="block text-foreground font-medium mb-2">
                  Scoped Permitted Tools
                </label>
                <div className="grid grid-cols-2 gap-2 max-h-48 overflow-y-auto pr-1">
                  {tools
                    .filter((t) => t.is_enabled !== false && t.status !== "disabled")
                    .map((t) => {
                      const isSelected = agentFormData.tools.includes(t.id);
                      return (
                        <button
                          type="button"
                          key={t.id}
                          onClick={() => handleToggleAgentToolAssignment(t.id)}
                          className={`flex items-center justify-between p-2.5 rounded-lg border text-left transition-all cursor-pointer ${
                            isSelected
                              ? "bg-primary/10 border-primary/40 text-primary hover:text-primary-foreground font-medium shadow-xs"
                              : "bg-background border-border text-muted-foreground hover:border-border/80"
                          }`}
                        >
                          <span className="font-mono text-[11px] truncate">{t.name}</span>
                          {isSelected && <Check size={14} className="text-primary flex-shrink-0" />}
                        </button>
                      );
                    })}

                  {/* MCP Permission Toggle Button */}
                  <button
                    type="button"
                    onClick={() => handleToggleAgentToolAssignment("mcp")}
                    className={`flex items-center justify-between p-2.5 rounded-lg border text-left transition-all cursor-pointer col-span-2 ${
                      agentFormData.tools.includes("mcp")
                        ? "bg-primary/10 border-primary/40 text-primary hover:text-primary-foreground font-medium shadow-xs"
                        : "bg-background border-border text-muted-foreground hover:border-border/80"
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <Puzzle size={13} className="text-primary" />
                      <span className="font-mono text-[11px]">Allow Active MCP Protocol Tools</span>
                    </div>
                    {agentFormData.tools.includes("mcp") && <Check size={14} className="text-primary flex-shrink-0" />}
                  </button>
                </div>
              </div>

              <div className="flex items-center justify-end gap-2.5 pt-4 border-t border-border">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setIsAgentModalOpen(false)}
                  className="text-xs hover:bg-destructive/10 hover:text-destructive transition-colors cursor-pointer"
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  size="sm"
                  className="bg-primary hover:bg-primary/90 text-primary hover:text-primary-foreground font-medium text-xs px-4 shadow-xs cursor-pointer"
                >
                  {editingAgentId ? "Update Agent" : "Save Agent"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Modal 2: Upload Tool (.py / .json) or MCP (.json) */}
      {isUploadModalOpen && (
        <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4">
          <div className="bg-card border border-border rounded-2xl max-w-md w-full flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150">
            <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-muted/30">
              <div className="flex items-center gap-2">
                <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
                  <Upload size={16} />
                </div>
                <h2 className="text-sm font-bold text-foreground">
                  {uploadCategory === "tool" ? "Upload Custom Tool" : "Upload MCP Extension"}
                </h2>
              </div>
              <button
                type="button"
                onClick={() => setIsUploadModalOpen(false)}
                className="p-1 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition cursor-pointer"
              >
                <X size={16} />
              </button>
            </div>

            <form onSubmit={handleUploadSubmit} className="p-6 space-y-4 text-xs">
              <div>
                <span className="block text-foreground font-medium mb-1">
                  Upload Category
                </span>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setUploadCategory("tool");
                      setSelectedUploadFile(null);
                    }}
                    className={`p-2 rounded-lg border text-center font-medium transition cursor-pointer ${
                      uploadCategory === "tool"
                        ? "bg-primary/10 border-primary text-primary shadow-xs"
                        : "bg-muted/40 border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    Custom Tool (.py / .json)
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setUploadCategory("extension");
                      setSelectedUploadFile(null);
                    }}
                    className={`p-2 rounded-lg border text-center font-medium transition cursor-pointer ${
                      uploadCategory === "extension"
                        ? "bg-primary/10 border-primary text-primary shadow-xs"
                        : "bg-muted/40 border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    MCP Server (.json)
                  </button>
                </div>
              </div>

              <input
                ref={fileInputRef}
                type="file"
                accept={uploadCategory === "tool" ? ".py,.json" : ".json"}
                onChange={(e) => {
                  if (e.target.files && e.target.files[0]) {
                    setSelectedUploadFile(e.target.files[0]);
                  }
                }}
                className="hidden"
              />

              <div
                onClick={() => fileInputRef.current?.click()}
                className="border-2 border-dashed border-border hover:border-primary/50 rounded-sm p-6 flex flex-col items-center justify-center text-center cursor-pointer transition bg-muted/10 hover:bg-muted/30"
              >
                {uploadCategory === "tool" ? (
                  <FileCode2 size={28} className="text-primary mb-2 opacity-80" />
                ) : (
                  <FileText size={28} className="text-primary mb-2 opacity-80" />
                )}
                {selectedUploadFile ? (
                  <div>
                    <span className="font-semibold text-foreground text-xs block truncate max-w-[240px]">
                      {selectedUploadFile.name}
                    </span>
                    <span className="text-[10px] text-muted-foreground font-mono">
                      {(selectedUploadFile.size / 1024).toFixed(1)} KB
                    </span>
                  </div>
                ) : (
                  <div>
                    <span className="text-xs font-semibold text-foreground block">
                      Click to choose {uploadCategory === "tool" ? ".py or .json" : ".json"} file
                    </span>
                    <span className="text-[10px] text-muted-foreground mt-0.5 block">
                      File will be verified and hot-reloaded automatically.
                    </span>
                  </div>
                )}
              </div>

              <div className="flex items-center justify-end gap-2.5 pt-3 border-t border-border">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setIsUploadModalOpen(false)}
                  className="text-xs hover:bg-destructive/10 hover:text-destructive transition-colors cursor-pointer"
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  size="sm"
                  disabled={!selectedUploadFile || isUploading}
                  className="bg-primary hover:bg-primary/90 text-primary hover:text-primary-foreground font-medium text-xs px-4 shadow-xs cursor-pointer disabled:opacity-40"
                >
                  {isUploading ? (
                    <>
                      <Loader2 size={12} className="mr-1.5 animate-spin" />
                      <span>Uploading...</span>
                    </>
                  ) : (
                    <span>Register Capability</span>
                  )}
                </Button>
              </div>
            </form>
          </div>
        </div>


      )}

     {/* Modal 3: Upload Bundle (.zip) */}
      <BundleUploadModal
        open={isBundleUploadOpen}
        onClose={() => setIsBundleUploadOpen(false)}
        onSuccess={() => {
          loadData();
          setTimeout(() => setIsBundleUploadOpen(false), 1500);
        }}
      />

      {/* Drawer: Bundle Details */}
      <BundleDetailsDrawer
        open={detailsBundle !== null}
        bundle={detailsBundle}
        onClose={() => setDetailsBundle(null)}
        onUpdated={loadData}
      />

    </div>
  );
}
