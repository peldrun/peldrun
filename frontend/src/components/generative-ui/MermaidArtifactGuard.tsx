// frontend/src/components/generative-ui/MermaidArtifactGuard.tsx
//
// Global guard that removes Mermaid render artifacts from document.body.
//
// Mermaid v10+ injects temporary DOM nodes into document.body while a
// diagram is being rendered. These nodes are positioned at the bottom-left
// of the viewport by default and appear as a flickering loading box during
// every render cycle.
//
// CSS rules alone cannot prevent the flash because the node is created and
// painted before a stylesheet can hide it. This component installs a
// MutationObserver on document.body that removes matching nodes the moment
// they are inserted.
//
// The guard is intentionally global: it runs once at the application root
// and protects the entire page, including diagrams rendered far from the
// component tree where they originate.

"use client";

import { useEffect } from "react";

/**
 * Returns true when a DOM node looks like a Mermaid render artifact.
 *
 * Detection rules:
 *   - Element id starts with "dmermaid_", "dmermaid-", "mermaid_" or
 *     "mermaid-". Mermaid uses all four prefixes across versions.
 *   - Element class list contains "mermaid-error" or "mermaidTooltip".
 *   - Element is a direct SVG with an id matching the same prefixes.
 *
 * The check is deliberately narrow: it never matches application-owned
 * elements, which use the "peldrun-" prefix.
 */
function isMermaidArtifact(node: Node): boolean {
  if (!(node instanceof Element)) return false;

  const id = node.id || "";
  const className =
    typeof (node as HTMLElement).className === "string"
      ? (node as HTMLElement).className
      : "";

  const idMatches =
    id.startsWith("dmermaid_svg_") ||
    id.startsWith("dmermaid-") ||
    id.startsWith("mermaid_") ||
    id.startsWith("mermaid-") ||
    id.startsWith("dmermaid_");

  if (idMatches) return true;

  if (className.includes("mermaid-error")) return true;
  if (className.includes("mermaidTooltip")) return true;

  return false;
}

/**
 * Removes any Mermaid artifacts currently present in document.body.
 * Safe to call at any time, including before Mermaid has ever run.
 */
function purgeExistingArtifacts(): void {
  if (typeof document === "undefined") return;

  const candidates = document.body.querySelectorAll(
    'div[id*="mermaid"], svg[id*="mermaid"], iframe[id*="mermaid"], div.mermaid-error'
  );

  candidates.forEach((el) => {
    if (isMermaidArtifact(el)) {
      el.remove();
    }
  });
}

export function MermaidArtifactGuard(): null {
  useEffect(() => {
    if (typeof document === "undefined" || typeof MutationObserver === "undefined") {
      return;
    }

    // Clean up anything left over from a previous session or SSR pass.
    purgeExistingArtifacts();

    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type !== "childList") continue;
        mutation.addedNodes.forEach((node) => {
          if (isMermaidArtifact(node)) {
            (node as Element).remove();
          }
        });
      }
    });

    observer.observe(document.body, {
      childList: true,
      subtree: false,
    });

    return () => {
      observer.disconnect();
    };
  }, []);

  return null;
}

export default MermaidArtifactGuard;
