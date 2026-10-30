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

export function MCPDetailDrawer({ open, extension, onClose }: MCPDetailDrawerProps) {
  const [tools, setTools] = useState<CachedTool[]>([]);
  const [loadingTools, setLoadingTools] = useState(false);

  useEffect(() => {
    if (!open || !extension) return;

    let cancelled = false;
    setLoadingTools(true);

    fetch(`/api/mcp/servers/${extension.id}/tools/cached`)
      .then((res) => res.json())
      .then((data) => {
        if (!cancelled && data.cached) {
          setTools(data.tools || []);
        }
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoadingTools(false);
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
            <div className="flex items-start gap-4 mb-6">
              <div className="p-0 shrink-0">
                {iconSvg ? (
                  <div
                    className="w-10 h-10 text-primary"
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
                <p className="text-sm text-muted-foreground mt-1">
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
                  <h2 className="text-sm font-bold text-foreground mb-3 uppercase tracking-wider text-muted-foreground">
                    About
                  </h2>
                  <p className="text-sm text-foreground leading-relaxed whitespace-pre-wrap">
                    {longDesc}
                  </p>
                </section>

                {/* Tools List */}
                <section>
                  <h2 className="text-sm font-bold text-foreground mb-3 uppercase tracking-wider text-muted-foreground flex items-center gap-2">
                    <Wrench size={16} /> Tools
                  </h2>
                  {loadingTools ? (
                    <div className="flex items-center gap-2 text-xs text-muted-foreground py-4">
                      <Loader2 size={14} className="animate-spin" /> Loading tools...
                    </div>
                  ) : tools.length > 0 ? (
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
        <div className="p-6 border-t border-border bg-muted/10 shrink-0">
          <p className="text-[11px] text-muted-foreground leading-relaxed text-center">
            Only use connectors from developers you trust. PELDRUN does not control which tools developers make available and cannot verify that they will work as intended or that they won't change.
          </p>
        </div>
      </div>
    </div>
  );
}
