// frontend/src/app/stores/page.tsx

"use client";

import React, { useState } from "react";
import type { BundleEntry } from "@/lib/bundleApi";
import { ToastContainer } from "@/components/ui/ToastNotification";
import { BundleUploadModal } from "@/components/store/BundleUploadModal";
import { BundleDetailsDrawer } from "@/components/store/BundleDetailsDrawer";

import {
  DEFAULT_SECTION,
  SectionId,
  getSectionMeta,
} from "./_config/sections";
import type { AgentFilter, ToolFilter } from "./_types/store";

import { useStoreData } from "./_hooks/useStoreData";
import { useAgentActions } from "./_hooks/useAgentActions";
import { useToolActions } from "./_hooks/useToolActions";
import { useExtensionActions } from "./_hooks/useExtensionActions";
import { useBundleActions } from "./_hooks/useBundleActions";

import { StoreSidebar } from "./_components/StoreSidebar";
import { StoreHeader } from "./_components/StoreHeader";
import { StoreToolbar } from "./_components/StoreToolbar";

import {
  AgentsSection,
  getAgentsHeaderConfig,
} from "./_sections/AgentsSection";
import { ToolsSection, getToolsHeaderConfig } from "./_sections/ToolsSection";
import {
  ExtensionsSection,
  getExtensionsHeaderConfig,
} from "./_sections/ExtensionsSection";

import { AgentBuilderModal } from "./_modals/AgentBuilderModal";
import { UploadModal } from "./_modals/UploadModal";
import { MCPSettingsModal } from "./_modals/MCPSettingsModal";

/**
 * Store page orchestrator.
 *
 * Responsibilities are strictly limited to:
 *  - Owning navigation state (active section, search, filters).
 *  - Owning cross-section state (details drawer, bundle upload modal).
 *  - Calling the action hooks and passing their outputs down.
 *  - Composing the final layout: sidebar + header + toolbar + section
 *    + modals.
 *
 * All business logic lives in the hooks; all rendering lives in the
 * sections and components. Keeping this file thin makes the page easy
 * to reason about and each piece independently testable.
 */
export default function StoresPage() {
  // ── Navigation & shared UI state ────────────────────────────────
  const [activeSection, setActiveSection] = useState<SectionId>(DEFAULT_SECTION);
  const [searchQuery, setSearchQuery] = useState("");
  const [agentFilter, setAgentFilter] = useState<AgentFilter>("all");
  const [toolFilter, setToolFilter] = useState<ToolFilter>("all");

  // Bundle-specific UI state (drawer + upload modal).
  const [detailsBundle, setDetailsBundle] = useState<BundleEntry | null>(null);
  const [isBundleUploadOpen, setIsBundleUploadOpen] = useState(false);

  // ── Data layer ──────────────────────────────────────────────────
  const {
    agents,
    setAgents,
    tools,
    setTools,
    extensions,
    setExtensions,
    bundles,
    loading,
    reload,
  } = useStoreData();

  // ── Action hooks ────────────────────────────────────────────────
  const agentActions = useAgentActions({ agents, setAgents });
  const toolActions = useToolActions({ tools, setTools, reload });
  const extensionActions = useExtensionActions({ extensions, setExtensions });
  const bundleActions = useBundleActions({
    reload,
    detailsBundle,
    setDetailsBundle,
  });

  // ── Derived layout data ─────────────────────────────────────────
  const counts: Record<SectionId, number> = {
    agents: agents.length,
    tools: tools.length,
    extensions: extensions.length,
  };

  /**
   * Build the current section's header configuration.
   * The callback is resolved fresh on every render because the
   * factories return plain objects; the underlying handlers are
   * stable (useCallback in the hooks), so this is cheap.
   */
  const headerConfig = (() => {
    if (activeSection === "agents") {
      return getAgentsHeaderConfig({
        onNewAgent: agentActions.handleOpenCreateAgentModal,
      });
    }
    if (activeSection === "tools") {
      return getToolsHeaderConfig({
        onInstallBundle: () => setIsBundleUploadOpen(true),
        onUploadSingleFile: () => toolActions.openUploadModal("tool"),
      });
    }
    return getExtensionsHeaderConfig({
      onUploadMCP: () => toolActions.openUploadModal("extension"),
    });
  })();

  // ── Render ──────────────────────────────────────────────────────
  return (
    <div className="w-full h-full flex flex-col flex-1 bg-background text-foreground overflow-hidden font-sans">
      <ToastContainer />

      {/* Unified header (driven by the active section's config) */}
      <StoreHeader
        config={headerConfig}
        onRefresh={reload}
        refreshing={loading}
      />

      {/* Body: sidebar + main content */}
      <div className="flex-1 flex flex-col md:flex-row overflow-hidden min-h-0">
        <StoreSidebar
          activeSection={activeSection}
          onSectionChange={(id) => {
            setActiveSection(id);
            setSearchQuery("");
          }}
          counts={counts}
        />

        <main className="flex-1 flex flex-col overflow-hidden min-w-0">
          <StoreToolbar
            activeSection={activeSection}
            searchQuery={searchQuery}
            onSearchChange={setSearchQuery}
            agentFilter={agentFilter}
            onAgentFilterChange={setAgentFilter}
            toolFilter={toolFilter}
            onToolFilterChange={setToolFilter}
          />

          <div className="flex-1 overflow-y-auto scrollbar-thin bg-storbg">
            <div className="max-w-[1240px] mx-auto w-full px-4 sm:px-6 lg:px-8 py-6">
              {activeSection === "agents" && (
                <AgentsSection
                  agents={agents}
                  searchQuery={searchQuery}
                  agentFilter={agentFilter}
                  togglingAgentId={agentActions.togglingAgentId}
                  onToggleAgent={agentActions.handleToggleAgent}
                  onEditAgent={agentActions.handleOpenEditAgentModal}
                  onDeleteAgent={agentActions.handleDeleteAgent}
                  onStartChat={agentActions.handleStartChatWithAgent}
                />
              )}

              {activeSection === "tools" && (
                <ToolsSection
                  tools={tools}
                  bundles={bundles}
                  searchQuery={searchQuery}
                  toolFilter={toolFilter}
                  togglingToolId={toolActions.togglingToolId}
                  onToggleTool={toolActions.handleToggleTool}
                  onDeleteTool={toolActions.handleDeleteTool}
                  onOpenBundleUpload={() => setIsBundleUploadOpen(true)}
                  onOpenToolUpload={() => toolActions.openUploadModal("tool")}
                  onBundleActivate={bundleActions.handleBundleActivate}
                  onBundleDelete={bundleActions.handleBundleDelete}
                  onBundleDetails={setDetailsBundle}
                />
              )}

              {activeSection === "extensions" && (
                <ExtensionsSection
                  extensions={extensions}
                  searchQuery={searchQuery}
                  togglingExtId={extensionActions.togglingExtId}
                  testingExtId={extensionActions.testingExtId}
                  deletingExtId={extensionActions.deletingExtId}
                  testResults={extensionActions.testResults}
                  onToggleExtension={extensionActions.handleToggleExtension}
                  onTestExtension={extensionActions.handleTestExtension}
                  onConfigureExtension={extensionActions.handleOpenConfigure}
                  onDeleteExtension={extensionActions.handleDeleteExtension}
                />
              )}
            </div>
          </div>
        </main>
      </div>

      {/* Agent builder modal */}
      <AgentBuilderModal
        open={agentActions.isAgentModalOpen}
        editingAgentId={agentActions.editingAgentId}
        formData={agentActions.agentFormData}
        setFormData={agentActions.setAgentFormData}
        tools={tools}
        onSubmit={agentActions.handleSubmitAgentModal}
        onClose={agentActions.handleCloseAgentModal}
        onToggleTool={agentActions.handleToggleAgentToolAssignment}
      />

      {/* Unified upload modal (tools + extensions) */}
      <UploadModal
        open={toolActions.isUploadModalOpen}
        category={toolActions.uploadCategory}
        selectedFile={toolActions.selectedUploadFile}
        isUploading={toolActions.isUploading}
        fileInputRef={toolActions.fileInputRef}
        onCategoryChange={(category) => {
          // Reset the file when the category changes so a .py cannot
          // be submitted as an MCP manifest (or vice-versa).
          toolActions.setUploadCategory(category);
          toolActions.setSelectedUploadFile(null);
        }}
        onFileSelect={toolActions.setSelectedUploadFile}
        onSubmit={toolActions.handleUploadSubmit}
        onClose={toolActions.closeUploadModal}
      />

      {/* Bundle upload modal (.zip) */}
      <BundleUploadModal
        open={isBundleUploadOpen}
        onClose={() => setIsBundleUploadOpen(false)}
        onSuccess={() => {
          reload();
          setTimeout(() => setIsBundleUploadOpen(false), 1500);
        }}
      />

      {/* Bundle details drawer */}
      <BundleDetailsDrawer
        open={detailsBundle !== null}
        bundle={detailsBundle}
        onClose={() => setDetailsBundle(null)}
        onUpdated={reload}
      />

      {/* MCP server configuration modal */}
      <MCPSettingsModal
        open={extensionActions.isConfigModalOpen}
        serverId={extensionActions.configuringExtension?.id ?? null}
        serverName={extensionActions.configuringExtension?.name ?? ""}
        onClose={extensionActions.handleCloseConfigure}
        onSaved={reload}
      />
    </div>
  );
}
