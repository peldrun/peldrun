// frontend/src/app/stores/page.tsx

"use client";

import React, { useState, useEffect } from "react";
import type { BundleEntry } from "@/lib/bundleApi";
import { ToastContainer } from "@/components/ui/ToastNotification";
import { BundleUploadModal } from "@/components/store/BundleUploadModal";
import { BundleDetailsDrawer } from "@/components/store/BundleDetailsDrawer";

import {
  DEFAULT_SECTION,
  SectionId,
  getSectionMeta,
} from "./_config/sections";
import type {
  AgentFilter,
  ProviderFilter,
  SkillCatalogInstallState,
  SkillCatalogItem,
  SkillFilter,
  SkillSummary,
  ToolFilter,
} from "./_types/store";
import type { ExtensionDefinition } from "@/lib/types";
import { useStoreData } from "./_hooks/useStoreData";
import { useAgentActions } from "./_hooks/useAgentActions";
import { useToolActions } from "./_hooks/useToolActions";
import { useSkillActions } from "./_hooks/useSkillActions";
import { useExtensionActions } from "./_hooks/useExtensionActions";
import { useBundleActions } from "./_hooks/useBundleActions";
import { useProviderActions } from "./_hooks/useProviderActions";

import { StoreSidebar } from "./_components/StoreSidebar";
import { StoreHeader } from "./_components/StoreHeader";
import { StoreToolbar } from "./_components/StoreToolbar";

import {
  AgentsSection,
  getAgentsHeaderConfig,
} from "./_sections/AgentsSection";
import { ToolsSection, getToolsHeaderConfig } from "./_sections/ToolsSection";
import {
  SkillsSection,
  getSkillsHeaderConfig,
} from "./_sections/SkillsSection";
import {
  ProvidersSection,
  getProvidersHeaderConfig,
} from "./_sections/ProvidersSection";
import {
  ExtensionsSection,
  getExtensionsHeaderConfig,
} from "./_sections/ExtensionsSection";

import { AgentBuilderModal } from "./_modals/AgentBuilderModal";
import { UploadModal } from "./_modals/UploadModal";
import { SkillUploadModal } from "./_modals/SkillUploadModal";
import { SkillDetailsDrawer } from "./_modals/SkillDetailsDrawer";
import { SkillEditorModal } from "./_modals/SkillEditorModal";
import { CatalogSkillInstallModal } from "./_modals/CatalogSkillInstallModal";
import { CatalogSkillDetailsModal } from "./_modals/CatalogSkillDetailsModal";
import { MCPSettingsModal } from "./_modals/MCPSettingsModal";
import { MCPToolsModal } from "@/app/stores/_modals/MCPToolsModal";
import { MCPDetailDrawer } from "./_components/MCPDetailDrawer";
import { ProviderUploadModal } from "./_modals/ProviderUploadModal";
import { ProviderDetailsDrawer } from "./_modals/ProviderDetailsDrawer";
import { ProviderConfigureModal } from "./_modals/ProviderConfigureModal";
import { CertificateManagerModal } from "./_modals/CertificateManagerModal";

/**
 * Compute the catalog install state for a given item.
 *
 * Uses the same matching rule as ``SkillsSection``: strict ID
 * equality against the ``skills`` array. Keeping the derivation in
 * one shared helper guarantees that the details modal and the
 * catalog card agree about whether an item is installed, which
 * avoids a UI inconsistency where the card says "Installed" but
 * the modal still offers an Install button (or vice versa).
 */
function computeCatalogInstallState(
  item: SkillCatalogItem | null,
  skills: SkillSummary[],
): SkillCatalogInstallState {
  if (!item) {
    return {
      isInstalled: false,
      isEnabled: false,
      installedVersion: null,
      installedSkill: null,
    };
  }
  const installed = skills.find((s) => s.id === item.id) ?? null;
  return {
    isInstalled: Boolean(installed),
    isEnabled: installed?.enabled ?? false,
    installedVersion: installed?.version ?? null,
    installedSkill: installed,
  };
}

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
  const [skillFilter, setSkillFilter] = useState<SkillFilter>("all");
  const [providerFilter, setProviderFilter] = useState<ProviderFilter>("all");

  // Bundle-specific UI state (drawer + upload modal).
  const [detailsBundle, setDetailsBundle] = useState<BundleEntry | null>(null);
  const [isBundleUploadOpen, setIsBundleUploadOpen] = useState(false);
  const [isMcpGloballyDisabled, setIsMcpGloballyDisabled] = useState(false);

  // ── Data layer ──────────────────────────────────────────────────
  const {
    agents,
    setAgents,
    tools,
    setTools,
    skills,
    setSkills,
    extensions,
    setExtensions,
    bundles,
    providers,
    setProviders,
    loading,
    reload,
  } = useStoreData();

  // ── Action hooks ────────────────────────────────────────────────
  const agentActions = useAgentActions({ agents, setAgents });
  const toolActions = useToolActions({ tools, setTools, reload });
  const skillActions = useSkillActions({ skills, setSkills, reload });
  const extensionActions = useExtensionActions({ extensions, setExtensions });
  const bundleActions = useBundleActions({
    reload,
    detailsBundle,
    setDetailsBundle,
  });
  const providerActions = useProviderActions({
    providers,
    setProviders,
    reload,
  });

  // ── Effects ─────────────────────────────────────────────────────
  useEffect(() => {
    fetch("/api/mcp/settings")
      .then((res) => res.json())
      .then((data) => {
        setIsMcpGloballyDisabled(data?.settings?.enabled === false);
      })
      .catch(() => {});
  }, [reload]);

  // ── Derived layout data ─────────────────────────────────────────
  const counts: Record<SectionId, number> = {
    agents: agents.length,
    tools: tools.length,
    skills: skills.length,
    providers: providers.length,
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
    if (activeSection === "skills") {
      return getSkillsHeaderConfig({
        onInstallSkill: skillActions.openUploadModal,
      });
    }
    if (activeSection === "providers") {
      return getProvidersHeaderConfig({
        onUploadProvider: providerActions.handleOpenUpload,
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
            skillFilter={skillFilter}
            onSkillFilterChange={setSkillFilter}
            providerFilter={providerFilter}
            onProviderFilterChange={setProviderFilter}
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
                  // Refresh the agents list after the AgentMcpModal
                  // saves MCP server overrides. Without this, the
                  // "N MCP" badge on the card would stay at its
                  // pre-save value because ``effective_mcp_servers``
                  // is computed from ``agent_settings.json`` on the
                  // backend and only re-fetched on ``reload()``.
                  onReloadAgents={reload}
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

              {activeSection === "skills" && (
                <SkillsSection
                  skills={skills}
                  searchQuery={searchQuery}
                  skillFilter={skillFilter}
                  togglingSkillId={skillActions.togglingSkillId}
                  deletingSkillId={skillActions.deletingSkillId}
                  onToggleSkill={skillActions.handleToggleSkill}
                  onDeleteSkill={skillActions.handleDeleteSkill}
                  onOpenDetails={skillActions.handleOpenDetails}
                  onOpenEditor={skillActions.handleOpenEditor}
                  onOpenUpload={skillActions.openUploadModal}
                  // Catalog props (forwarded from useSkillActions).
                  // All ten are required -- SkillsSection is entirely
                  // driven by the parent hook and never fetches on
                  // its own.
                  catalogItems={skillActions.catalogItems}
                  isCatalogLoading={skillActions.isCatalogLoading}
                  isCatalogRefreshing={skillActions.isCatalogRefreshing}
                  catalogError={skillActions.catalogError}
                  catalogEnabled={skillActions.catalogEnabled}
                  installingCatalogId={skillActions.installingCatalogId}
                  onLoadCatalog={skillActions.handleLoadCatalog}
                  onRefreshCatalog={skillActions.handleRefreshCatalog}
                  onInstallFromCatalog={skillActions.handleInstallFromCatalog}
                  onOpenCatalogDetails={skillActions.handleOpenCatalogDetails}
                />
              )}

              {activeSection === "providers" && (
                <ProvidersSection
                  providers={providers}
                  searchQuery={searchQuery}
                  providerFilter={providerFilter}
                  catalogItems={providerActions.catalogItems}
                  isCatalogLoading={providerActions.isCatalogLoading}
                  isRefreshingCatalog={providerActions.isRefreshingCatalog}
                  catalogError={providerActions.catalogError}
                  onLoadCatalog={providerActions.handleLoadCatalog}
                  onRefreshCatalog={providerActions.handleRefreshCatalog}
                  onInstallFromCatalog={
                    providerActions.handleInstallFromCatalog
                  }
                  installingCatalogId={
                    providerActions.installingCatalogId
                  }
                  togglingProviderId={
                    providerActions.togglingProviderId
                  }
                  deletingProviderId={
                    providerActions.deletingProviderId
                  }
                  activatingProviderId={
                    providerActions.activatingProviderId
                  }
                  onToggleProvider={providerActions.handleToggleProvider}
                  onDeleteProvider={providerActions.handleDeleteProvider}
                  onActivateVersion={
                    providerActions.handleActivateVersion
                  }
                  onOpenDetails={providerActions.handleOpenDetails}
                  onOpenConfigure={providerActions.handleOpenConfigure}
                  onManageCertificates={
                    providerActions.handleOpenCertificateManager
                  }
                  onOpenUpload={providerActions.handleOpenUpload}
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
                  onManageToolsExtension={extensionActions.handleOpenManageTools}
                  onOpenDetailsExtension={extensionActions.handleOpenDetails}
                  onDeleteExtension={extensionActions.handleDeleteExtension}
                  isMcpGloballyDisabled={isMcpGloballyDisabled}
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
        // The ``extensions`` collection already carries every MCP
        // server registered in the store (built-in, custom JSON,
        // and bundle). Filtering on ``type`` defends against future
        // non-MCP extensions appearing in the same collection.
        mcpServers={extensions
          .filter(
            (e) =>
              e.type === "mcp_server" ||
              e.type === "mcp" ||
              !e.type, // legacy extensions without a type field
          )
          .map((e) => ({
            id: e.id,
            name: e.name,
            description: e.description,
            transport:
              (e as ExtensionDefinition & { transport?: string })
                .transport || "stdio",
            status: e.status,
            is_builtin:
              (e as ExtensionDefinition & { is_builtin?: boolean })
                .is_builtin,
            is_bundle:
              (e as ExtensionDefinition & { is_bundle?: boolean })
                .is_bundle,
          }))}
        onSubmit={agentActions.handleSubmitAgentModal}
        onClose={agentActions.handleCloseAgentModal}
        onToggleTool={agentActions.handleToggleAgentToolAssignment}
        onToggleMcpServer={agentActions.handleToggleAgentMcpServer}
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

            {/* MCP Tools management modal */}
      <MCPToolsModal
        open={extensionActions.isToolsModalOpen}
        serverId={extensionActions.managingToolsExtension?.id ?? null}
        serverName={extensionActions.managingToolsExtension?.name ?? ""}
        onClose={extensionActions.handleCloseManageTools}
      />

      {/* MCP Details Drawer */}
      <MCPDetailDrawer
        open={extensionActions.isDetailsDrawerOpen}
        extension={extensionActions.detailsExtension}
        onClose={extensionActions.handleCloseDetails}
      />

      {/* Skill install (.zip) */}
      <SkillUploadModal
        open={skillActions.isUploadModalOpen}
        selectedFile={skillActions.selectedUploadFile}
        isUploading={skillActions.isUploading}
        onFileSelect={skillActions.setSelectedUploadFile}
        onSubmit={skillActions.handleUploadSubmit}
        onClose={skillActions.closeUploadModal}
      />

      {/* Skill details drawer */}
      <SkillDetailsDrawer
        open={skillActions.isDetailsDrawerOpen}
        skill={skillActions.detailsSkill}
        onClose={skillActions.handleCloseDetails}
        onEditSubSkill={skillActions.handleOpenEditor}
      />

      {/* Skill editor modal */}
      <SkillEditorModal
        open={skillActions.isEditorOpen}
        skill={skillActions.editorSkill}
        subSkillId={skillActions.editorSubSkillId}
        onClose={skillActions.handleCloseEditor}
        onSaved={skillActions.handleSkillSaved}
      />

      {/* Catalog skill details modal.
          Opened by ``handleOpenCatalogDetails`` (called from the
          "Details" button on CatalogSkillCard). Shows the item's
          install warnings, tags, links, and installability reason.

          The install action closes THIS modal FIRST, then opens the
          install modal -- so the two never overlap. Without the
          close-first step, both backdrops would render simultaneously
          and the z-index order would determine which click lands where
          (undefined behavior across browsers). */}
      <CatalogSkillDetailsModal
        open={skillActions.isCatalogDetailsOpen}
        item={skillActions.catalogDetailsItem}
        installState={computeCatalogInstallState(
          skillActions.catalogDetailsItem,
          skills,
        )}
        installing={
          skillActions.installingCatalogId ===
          skillActions.catalogDetailsItem?.id
        }
        onInstall={() => {
          const item = skillActions.catalogDetailsItem;
          if (!item) return;
          skillActions.handleCloseCatalogDetails();
          void skillActions.handleInstallFromCatalog(item);
        }}
        onClose={skillActions.handleCloseCatalogDetails}
      />

      {/* Catalog skill install modal.
          Opened by ``handleInstallFromCatalog``. Renders a simulated
          multi-phase progress bar while the synchronous install runs
          on the backend, then swaps to a REAL success/error banner
          (drawn from ``catalogInstallResult``). The pattern mirrors
          ``ProviderUploadModal`` in catalog mode. */}
      <CatalogSkillInstallModal
        open={skillActions.catalogInstallItem !== null}
        item={skillActions.catalogInstallItem}
        result={skillActions.catalogInstallResult}
        onClose={skillActions.handleCloseCatalogInstall}
      />

      {/* Provider install modal — serves BOTH the manual ZIP upload
          flow and the catalog-item install flow. The only difference
          is which mode is active; the progress bar, log console, and
          result banners are identical, so users see the same steps
          whether they uploaded a ZIP or clicked "Install" on a
          catalog entry. */}
      <ProviderUploadModal
        open={
          providerActions.isUploadModalOpen ||
          providerActions.catalogInstallItem !== null
        }
        mode={
          providerActions.catalogInstallItem !== null ? "catalog" : "upload"
        }
        catalogItemName={
          providerActions.catalogInstallItem?.display_name ||
          providerActions.catalogInstallItem?.name ||
          undefined
        }
        catalogItemVersion={
          providerActions.catalogInstallItem?.version || undefined
        }
        selectedFile={providerActions.uploadSelectedFile}
        installDependencies={providerActions.uploadInstallDependencies}
        isUploading={providerActions.isUploading}
        errorMessage={providerActions.uploadError}
        installJobStatus={providerActions.installJobStatus}
        onSelectFile={providerActions.setUploadSelectedFile}
        onToggleInstallDependencies={
          providerActions.setUploadInstallDependencies
        }
        onSubmit={providerActions.handleUploadSubmit}
        onClose={providerActions.handleCloseUpload}
      />

      {/* Provider details drawer */}
      <ProviderDetailsDrawer
        open={providerActions.isDetailsDrawerOpen}
        details={providerActions.detailsProvider}
        isLoading={providerActions.isDetailsLoading}
        onClose={providerActions.handleCloseDetails}
        onActivateVersion={providerActions.handleActivateVersion}
        onToggleEnabled={providerActions.handleToggleProvider}
        onDelete={providerActions.handleDeleteProvider}
        toggling={
          providerActions.detailsProvider !== null &&
          providerActions.togglingProviderId ===
            providerActions.detailsProvider.provider.id
        }
        deleting={
          providerActions.detailsProvider !== null &&
          providerActions.deletingProviderId ===
            providerActions.detailsProvider.provider.id
        }
        activating={
          providerActions.detailsProvider !== null &&
          providerActions.activatingProviderId ===
            providerActions.detailsProvider.provider.id
        }
      />

      {/* Provider configure modal */}
      <ProviderConfigureModal
        open={providerActions.isConfigureModalOpen}
        provider={providerActions.configureProvider}
        connections={providerActions.connections}
        isConnectionsLoading={providerActions.isConnectionsLoading}
        connectionsError={providerActions.connectionsError}
        connectionActionId={providerActions.connectionActionId}
        onClose={providerActions.handleCloseConfigure}
        onCreateConnection={providerActions.handleCreateConnection}
        onUpdateConnection={providerActions.handleUpdateConnection}
        onDeleteConnection={providerActions.handleDeleteConnection}
        onEnableConnection={providerActions.handleEnableConnection}
        onDisableConnection={providerActions.handleDisableConnection}
        onActivateConnection={providerActions.handleActivateConnection}
        onTestConnection={providerActions.handleTestConnection}
      />

      {/* Certificate manager modal.
          Opened from the "Certificates" button on a provider card.
          Only used by packages that declare certificates
          (has_certificates === true). Packages without certificates
          never trigger this modal. */}
      <CertificateManagerModal
        open={providerActions.isCertificateModalOpen}
        provider={providerActions.certificateManagerProvider}
        certificates={providerActions.certificates}
        isLoading={providerActions.isLoadingCertificates}
        error={providerActions.certificatesError}
        actionId={providerActions.certificateActionId}
        onClose={providerActions.handleCloseCertificateManager}
        onRefresh={providerActions.handleRefreshCertificates}
        onInstall={providerActions.handleInstallCertificate}
        onTest={providerActions.handleTestCertificate}
        onDelete={providerActions.handleDeleteCertificate}
      />

    </div>
  );
}
