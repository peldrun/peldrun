// frontend/src/app/stores/_components/ToolIcon.tsx

"use client";

import React, { useEffect, useState } from "react";
import { Code2, FileCode2, Terminal, Wrench } from "lucide-react";
import type { ToolWithMeta } from "./ToolCard";
import { getApiUrl } from "../_hooks/useStoreData";

export interface ToolIconProps {
  tool: ToolWithMeta;
  size?: number;
  className?: string;
}

/** Module-level cache: toolId → sanitized SVG text (or null = failed) */
const svgCache = new Map<string, string | null>();

function resolveLucideIcon(tool: ToolWithMeta, size: number): React.ReactNode {
  if (tool.icon) {
    const base =
      tool.icon.toLowerCase().split("/").pop()?.replace(/\.svg$/, "") ?? "";
    switch (base) {
      case "code":
      case "code2":
      case "python":
        return <Code2 size={size} />;
      case "terminal":
      case "bash":
      case "shell":
        return <Terminal size={size} />;
      case "str_replace_editor":
      case "editor":
      case "file":
        return <FileCode2 size={size} />;
    }
  }
  if (tool.id.includes("python")) return <Code2 size={size} />;
  if (tool.id.includes("bash")) return <Terminal size={size} />;
  if (tool.id.includes("editor")) return <FileCode2 size={size} />;
  return <Wrench size={size} />;
}

/** Minimal hardening: kill <script>, event handlers, and javascript: URLs. */
function sanitizeSvg(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed.includes("<svg")) return null;
  return trimmed
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/\son\w+\s*=\s*(".*?"|'.*?'|[^\s>]+)/gi, "")
    .replace(/(href|src)\s*=\s*("javascript:[^"]*"|'javascript:[^']*')/gi, "");
}

export function ToolIcon({ tool, size = 18, className }: ToolIconProps) {
  const [svgText, setSvgText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    setSvgText(null);

    // Cache hit
    if (svgCache.has(tool.id)) {
      const cached = svgCache.get(tool.id) ?? null;
      if (cached === null) setFailed(true);
      else setSvgText(cached);
      return;
    }

    const url = getApiUrl(
      `/api/store/tools/${encodeURIComponent(tool.id)}/icon.svg`
    );
    const ctrl = new AbortController();

    fetch(url, { signal: ctrl.signal, credentials: "same-origin" })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((text) => {
        const safe = sanitizeSvg(text);
        svgCache.set(tool.id, safe);
        if (cancelled) return;
        if (safe === null) setFailed(true);
        else setSvgText(safe);
      })
      .catch((e) => {
        if (cancelled || e?.name === "AbortError") return;
        svgCache.set(tool.id, null);
        setFailed(true);
      });

    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [tool.id]);

  // ── Fallback: Lucide ─────────────────────────────────────────────
  if (failed) {
    return (
      <span
        className={className}
        aria-hidden="true"
        style={{
          width: size,
          height: size,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {resolveLucideIcon(tool, size)}
      </span>
    );
  }

  // ── Loading placeholder (same box size → no layout shift) ───────
  if (!svgText) {
    return (
      <span
        className={className}
        aria-hidden="true"
        style={{ width: size, height: size, display: "inline-block" }}
      />
    );
  }

  // ── Inline SVG ──────────────────────────────────────────────────
  return (
    <span
      className={className}
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        color: "currentColor", // <- يُحلّ fill="currentColor" داخل الـ SVG
      }}
      // width/height
      dangerouslySetInnerHTML={{ __html: svgText }}
    />
  );
}
