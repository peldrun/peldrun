"use client";

import React, { useEffect, useState, useRef } from "react";
import { createPortal } from "react-dom";

export type TooltipPlacement = "top" | "bottom" | "left" | "right";

interface TooltipState {
  visible: boolean;
  text: string;
  x: number;
  y: number;
  placement: TooltipPlacement;
}

export const GlobalTooltip: React.FC = () => {
  const [mounted, setMounted] = useState(false);
  const [tooltip, setTooltip] = useState<TooltipState>({
    visible: false,
    text: "",
    x: 0,
    y: 0,
    placement: "top",
  });

  const timerRef = useRef<NodeJS.Timeout | null>(null);
  const currentTargetRef = useRef<HTMLElement | null>(null);

  const hideTooltip = () => {
    const target = currentTargetRef.current;
    if (target) {
      // Restore native title attribute if it was intercepted
      const originalTitle = target.getAttribute("data-peldrun-title");
      if (originalTitle) {
        target.setAttribute("title", originalTitle);
        target.removeAttribute("data-peldrun-title");
      }
    }

    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }

    currentTargetRef.current = null;
    setTooltip((prev) => ({ ...prev, visible: false }));
  };

  const calculateSmartPlacement = (
    rect: DOMRect,
    requestedPosition?: string | null
  ): { x: number; y: number; placement: TooltipPlacement } => {
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    const spaceTop = rect.top;
    const spaceBottom = viewportHeight - rect.bottom;
    const spaceLeft = rect.left;
    const spaceRight = viewportWidth - rect.right;

    const MIN_VERTICAL_SPACE = 40;
    const MIN_HORIZONTAL_SPACE = 120; // Estimated safe width for horizontal tooltips

    let resolvedPlacement: TooltipPlacement;

    if (requestedPosition === "left") {
      resolvedPlacement = spaceLeft >= MIN_HORIZONTAL_SPACE ? "left" : "right";
    } else if (requestedPosition === "right") {
      resolvedPlacement = spaceRight >= MIN_HORIZONTAL_SPACE ? "right" : "left";
    } else if (requestedPosition === "bottom") {
      resolvedPlacement = spaceBottom >= MIN_VERTICAL_SPACE ? "bottom" : "top";
    } else if (requestedPosition === "top") {
      resolvedPlacement = spaceTop >= MIN_VERTICAL_SPACE ? "top" : "bottom";
    } else {
      // Automatic detection based on element screen anchors
      if (spaceLeft < 70 && spaceRight >= MIN_HORIZONTAL_SPACE) {
        resolvedPlacement = "right";
      } else if (spaceRight < 70 && spaceLeft >= MIN_HORIZONTAL_SPACE) {
        resolvedPlacement = "left";
      } else if (spaceTop < MIN_VERTICAL_SPACE) {
        resolvedPlacement = "bottom";
      } else {
        resolvedPlacement = "top";
      }
    }

    let x = 0;
    let y = 0;

    switch (resolvedPlacement) {
      case "left":
        x = rect.left - 8;
        y = rect.top + rect.height / 2;
        break;
      case "right":
        x = rect.right + 8;
        y = rect.top + rect.height / 2;
        break;
      case "bottom":
        x = rect.left + rect.width / 2;
        y = rect.bottom + 8;
        break;
      case "top":
      default:
        x = rect.left + rect.width / 2;
        y = rect.top - 8;
        break;
    }

    return { x, y, placement: resolvedPlacement };
  };

  useEffect(() => {
    setMounted(true);

    const handlePointerOver = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;

      const target = (event.target as HTMLElement)?.closest<HTMLElement>(
        "[title], [data-tooltip]"
      );

      if (!target) return;

      let text = target.getAttribute("data-tooltip") || "";
      const rawTitle = target.getAttribute("title");

      if (rawTitle) {
        text = rawTitle.trim();
        target.setAttribute("data-peldrun-title", rawTitle);
        target.removeAttribute("title");

        if (!target.getAttribute("aria-label")) {
          target.setAttribute("aria-label", text);
        }
      }

      if (!text) return;

      currentTargetRef.current = target;

      if (timerRef.current) clearTimeout(timerRef.current);

      timerRef.current = setTimeout(() => {
        if (!currentTargetRef.current) return;

        const rect = currentTargetRef.current.getBoundingClientRect();
        const requestedPosition = currentTargetRef.current.getAttribute(
          "data-tooltip-position"
        );

        const { x, y, placement } = calculateSmartPlacement(
          rect,
          requestedPosition
        );

        setTooltip({
          visible: true,
          text,
          x,
          y,
          placement,
        });
      }, 150);
    };

    const handlePointerOut = (event: PointerEvent) => {
      const target = currentTargetRef.current;
      if (!target) return;

      const related = event.relatedTarget as Node | null;
      if (related && target.contains(related)) return;

      hideTooltip();
    };

    const handleScrollOrDismiss = () => {
      hideTooltip();
    };

    document.addEventListener("pointerover", handlePointerOver, true);
    document.addEventListener("pointerout", handlePointerOut, true);
    window.addEventListener("scroll", handleScrollOrDismiss, true);
    window.addEventListener("pointerdown", handleScrollOrDismiss, true);

    return () => {
      document.removeEventListener("pointerover", handlePointerOver, true);
      document.removeEventListener("pointerout", handlePointerOut, true);
      window.removeEventListener("scroll", handleScrollOrDismiss, true);
      window.removeEventListener("pointerdown", handleScrollOrDismiss, true);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  if (!mounted || !tooltip.visible || !tooltip.text) {
    return null;
  }

  const getTransformOriginAndOffset = (placement: TooltipPlacement): string => {
    switch (placement) {
      case "left":
        return "translate(-100%, -50%)";
      case "right":
        return "translate(0, -50%)";
      case "bottom":
        return "translate(-50%, 0)";
      case "top":
      default:
        return "translate(-50%, -100%)";
    }
  };

  return createPortal(
    <div
      role="tooltip"
      aria-hidden="true"
      style={{
        left: `${tooltip.x}px`,
        top: `${tooltip.y}px`,
        transform: getTransformOriginAndOffset(tooltip.placement),
      }}
      className="fixed z-[9999] pointer-events-none select-none whitespace-nowrap rounded-md
        bg-zinc-950/95 px-2.5 py-1 text-xs font-medium text-zinc-200
        shadow-[0_4px_20px_rgba(0,0,0,0.55)] border border-zinc-800/80
        backdrop-blur-md transition-opacity duration-150 ease-out
        animate-in fade-in zoom-in-95"
    >
      {tooltip.text}
    </div>,
    document.body
  );
};

export default GlobalTooltip;
