// frontend/src/app/stores/_components/MCPDetailDrawer.tsx
"use client";

import React, { useEffect, useState } from "react";
import {
  X,
  ExternalLink,
  ShieldCheck,
  Clock,
  Key,
  Database,
  Wrench,
  Loader2,
  Globe,
  FileText,
  MessageSquare,
} from "lucide-react";
import type { ExtensionDefinition } from "@/lib/types";

interface MCPDetailDrawerProps {
  open: boolean;
  extension: ExtensionDefinition | null;
  onClose: () => void;
}

interface CachedTool {
  name: string;
  description: string;
}

interface CachedResource {
  uri: string;
  name: string;
  description: string;
  mimeType: string;
}

interface CachedPrompt {
  name: string;
  description: string;
  arguments: Array<{
    name: string;
    description: string;
    required: boolean;
  }>;
}

export function MCPDetailDrawer({ open, extension, onClose }: MCPDetailDrawerProps) {
  const [activeTab, setActiveTab] = useState<"tools" | "resources" | "prompts">("tools");
  const [tools, setTools] = useState<CachedTool[]>([]);
  const [resources, setResources] = useState<CachedResource[]>([]);
  const [prompts, setPrompts] = useState<CachedPrompt[]>([]);
  const [loadingData, setLoadingData] = useState(false);

  useEffect(() => {
    if (!open || !extension) return;

    let cancelled = false;
    setLoadingData(true);
    setActiveTab("tools"); // Reset to default tab on open

    // Fetch all three primitives in parallel to minimize latency.
    // The backend caches all three in the same file, so this is
    // effectively one disk read behind the scenes.
    Promise.all([
      fetch(`/api/mcp/servers/${extension.id}/tools/cached`).then((r) => r.json()),
      fetch(`/api/mcp/servers/${extension.id}/resources`).then((r) => r.json()).catch(() => ({})),
      fetch(`/api/mcp/servers/${extension.id}/prompts`).then((r) => r.json()).catch(() => ({})),
    ])
      .then(([toolsData, resourcesData, promptsData]) => {
        if (cancelled) return;

        if (toolsData && toolsData.cached) {
          setTools(toolsData.tools || []);
        } else {
          setTools([]);
        }

        if (resourcesData && resourcesData.status === "ok") {
          setResources(resourcesData.resources || []);
        } else {
          setResources([]);
        }

        if (promptsData && promptsData.status === "ok") {
          setPrompts(promptsData.prompts || []);
        } else {
          setPrompts([]);
        }
      })
      .catch(() => {
        // Fail-soft: leave lists empty if the parallel fetch fails
      })
      .finally(() => {
        if (!cancelled) setLoadingData(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open, extension]);

  if (!open || !extension) return null;

  const ext = extension as any;
  const shortDesc = ext.short_description || extension.description;
  const longDesc = ext.store_description || extension.description;
  const category = ext.category || "General";
  const tags = ext.tags || [];
  const author = ext.author || { name: "Unknown" };
  const iconSvg = ext.icon_svg;
  const isVerified = ext.trust === "verified";
  const hints = ext.hints || {};
  const links = {
    homepage: ext.homepage,
    docs: ext.docs_url,
    privacy: ext.privacy_url,
    support: ext.support_url,
    repo: ext.repository?.url,
  };

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-background/80 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-2xl h-full bg-card shadow-2xl flex flex-col border-l border-border animate-in slide-in-from-right duration-200">

        {/* Header */}
        <div className="px-6 py-4 border-b border-border flex items-center justify-between bg-muted/20">
          <button
            onClick={onClose}
            className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition"
          >
            <X size={16} /> Close
          </button>
        </div>

        {/* Body (Scrollable) */}
        <div className="flex-1 overflow-y-auto scrollbar-thin">
          <div className="px-8 py-6 max-w-2xl mx-auto">

            {/* Title Area */}
            <div className="flex items-center gap-4 mb-6">
              <div className="p-0 shrink-0">
                {iconSvg ? (
                  <div
                    className="w-10 h-full text-primary flex items-center justify-center "
                    dangerouslySetInnerHTML={{ __html: iconSvg }}
                  />
                ) : (
                  <Database size={40} className="text-primary" />
                )}
              </div>
              <div>
                <h1 className="text-2xl font-bold text-foreground flex items-center gap-2">
                  {extension.name}
                  {isVerified && (
                    <span title="Verified Publisher" className="inline-flex items-center">
                      <ShieldCheck size={20} className="text-emerald-500" />
                    </span>
                  )}
                </h1>
                <p className="text-[13px] font-mono  text-muted-foreground mt-1 max-w-md">
                  {shortDesc}
                </p>
              </div>
            </div>

            {/* Stats Grid */}
            <div className="grid grid-cols-2 gap-4 mb-8 p-4 rounded-s-sm border border-card bg-gigbg">
              <div className="flex items-center gap-2 text-xs">
                <Clock size={14} className="text-muted-foreground" />
                <span className="font-medium">Setup:</span> {hints.setup_time || "Unknown"}
              </div>
              <div className="flex items-center gap-2 text-xs">
                <Key size={14} className="text-muted-foreground" />
                <span className="font-medium">Sign-in:</span> {hints.requires_token ? "Required" : "Not Required"}
              </div>
              <div className="flex items-center gap-2 text-xs">
                <Globe size={14} className="text-muted-foreground" />
                <span className="font-medium">Network:</span> {hints.network_access ? "Allowed" : "Local Only"}
              </div>
              <div className="flex items-center gap-2 text-xs">
                <Database size={14} className="text-muted-foreground" />
                <span className="font-medium">Cost:</span> {hints.cost || "Free"}
              </div>
            </div>

            {/* Main Content & Sidebar Layout */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-8">

              {/* Left Column: Description & Tools */}
              <div className="md:col-span-2 space-y-8">
                {/* About */}
                <section>
                  <h2 className="text-sm font-bold text-black mb-3 uppercase tracking-wider ">
                    About
                  </h2>
                  <p className="text-[14px] text-black/60 leading-relaxed whitespace-pre-wrap">
                    {longDesc}
                  </p>
                </section>

                {/* Primitives Section (Tools / Resources / Prompts) */}
                <section>
                  {/* Tab Bar */}
                  <div className="flex border-b border-border mb-4">
                    <button
                      type="button"
                      onClick={() => setActiveTab("tools")}
                      className={`px-4 py-2 text-xs font-semibold border-b-2 transition-colors flex items-center gap-1.5 ${
                        activeTab === "tools"
                          ? "border-primary text-primary"
                          : "border-transparent text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      <Wrench size={14} /> Tools ({tools.length})
                    </button>
                    <button
                      type="button"
                      onClick={() => setActiveTab("resources")}
                      className={`px-4 py-2 text-xs font-semibold border-b-2 transition-colors flex items-center gap-1.5 ${
                        activeTab === "resources"
                          ? "border-primary text-primary"
                          : "border-transparent text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      <FileText size={14} /> Resources ({resources.length})
                    </button>
                    <button
                      type="button"
                      onClick={() => setActiveTab("prompts")}
                      className={`px-4 py-2 text-xs font-semibold border-b-2 transition-colors flex items-center gap-1.5 ${
                        activeTab === "prompts"
                          ? "border-primary text-primary"
                          : "border-transparent text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      <MessageSquare size={14} /> Prompts ({prompts.length})
                    </button>
                  </div>

                  {loadingData ? (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground py-4">
                      <Loader2 size={14} className="animate-spin" /> Loading data...
                    </div>
                  ) : (
                    <>
                      {/* ── Tools Tab ── */}
                      {activeTab === "tools" &&
                        (tools.length > 0 ? (
                          <div className="flex flex-wrap gap-2">
                            {tools.map((t) => (
                              <span
                                key={t.name}
                                className="px-2.5 py-1 rounded-md border border-border bg-muted text-[11px] font-mono text-foreground"
                                title={t.description}
                              >
                                {t.name}
                              </span>
                            ))}
                          </div>
                        ) : (
                          <p className="text-xs text-muted-foreground italic">
                            No tools cached yet. Open "Manage Tools" to fetch them.
                          </p>
                        ))}

                      {/* ── Resources Tab ── */}
                      {activeTab === "resources" &&
                        (resources.length > 0 ? (
                          <div className="flex flex-col gap-3">
                            {resources.map((r) => (
                              <div key={r.uri} className="p-3 rounded-md border border-border bg-card">
                                <div className="flex items-center justify-between mb-1">
                                  <span className="text-sm font-bold text-foreground">{r.name}</span>
                                  <span className="text-[10px] font-mono text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
                                    {r.mimeType}
                                  </span>
                                </div>
                                <p className="text-xs text-muted-foreground mb-2">{r.description}</p>
                                <div className="text-[10px] font-mono text-primary/70 break-all bg-primary/5 p-1.5 rounded">
                                  {r.uri}
                                </div>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p className="text-xs text-muted-foreground italic">
                            This server does not expose any resources.
                          </p>
                        ))}

                      {/* ── Prompts Tab ── */}
                      {activeTab === "prompts" &&
                        (prompts.length > 0 ? (
                          <div className="flex flex-col gap-3">
                            {prompts.map((p) => (
                              <div key={p.name} className="p-3 rounded-md border border-border bg-card">
                                <div className="text-sm font-bold text-foreground mb-1">{p.name}</div>
                                <p className="text-xs text-muted-foreground mb-2">{p.description}</p>
                                {p.arguments && p.arguments.length > 0 && (
                                  <div className="mt-2">
                                    <span className="text-[10px] uppercase font-bold text-muted-foreground">
                                      Arguments:
                                    </span>
                                    <ul className="mt-1 space-y-1">
                                      {p.arguments.map((arg) => (
                                        <li key={arg.name} className="text-xs text-foreground flex items-center gap-1.5">
                                          <code className="font-mono text-[11px] bg-muted px-1 rounded">
                                            {arg.name}
                                          </code>
                                          <span className="text-muted-foreground text-[11px]">
                                            {arg.description}
                                          </span>
                                          {arg.required && (
                                            <span className="text-[9px] text-destructive font-bold uppercase">
                                              required
                                            </span>
                                          )}
                                        </li>
                                      ))}
                                    </ul>
                                  </div>
                                )}
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p className="text-xs text-muted-foreground italic">
                            This server does not expose any prompts.
                          </p>
                        ))}
                    </>
                  )}
                </section>
              </div>

              {/* Right Column: Metadata */}
              <div className="space-y-6">

                {/* Author */}
                <div>
                  <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider mb-1">
                    Made by
                  </h4>
                  <a href={author.url || "#"} target="_blank" rel="noreferrer" className="text-sm text-primary hover:underline flex items-center gap-1">
                    {author.name} <ExternalLink size={12} />
                  </a>
                </div>

                {/* Category & Tags */}
                <div>
                  <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider mb-1">
                    Category
                  </h4>
                  <div className="flex flex-wrap gap-1.5">
                    <span className="px-2 py-0.5 rounded text-xs bg-muted text-foreground border border-border capitalize">
                      {category}
                    </span>
                    {tags.map((tag: string) => (
                      <span key={tag} className="px-2 py-0.5 rounded text-[10px] text-muted-foreground border border-border">
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>

                {/* Links */}
                {(links.homepage || links.docs || links.privacy || links.repo) && (
                  <div>
                    <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider mb-2">
                      More Info
                    </h4>
                    <div className="flex flex-col gap-2 text-xs">
                      {links.docs && (
                        <a href={links.docs} target="_blank" rel="noreferrer" className="text-primary hover:underline flex items-center gap-1">
                          Documentation <ExternalLink size={10} />
                        </a>
                      )}
                      {links.repo && (
                        <a href={links.repo} target="_blank" rel="noreferrer" className="text-primary hover:underline flex items-center gap-1">
                          Repository <ExternalLink size={10} />
                        </a>
                      )}
                      {links.privacy && (
                        <a href={links.privacy} target="_blank" rel="noreferrer" className="text-primary hover:underline flex items-center gap-1">
                          Privacy Policy <ExternalLink size={10} />
                        </a>
                      )}
                      {links.support && (
                        <a href={links.support} target="_blank" rel="noreferrer" className="text-primary hover:underline flex items-center gap-1">
                          Support <ExternalLink size={10} />
                        </a>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Footer Warning */}
        <div className="p-6 border-t border-border bg-custom shrink-0">
          <p className="text-[11px] text-muted-foreground leading-relaxed text-center">
            Only use connectors from developers you trust. PELDRUN does not control which tools developers make available and cannot verify that they will work as intended or that they won't change.
          </p>
        </div>
      </div>
    </div>
  );
}
