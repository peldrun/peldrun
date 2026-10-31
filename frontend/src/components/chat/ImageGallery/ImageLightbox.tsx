// frontend/src/components/chat/ImageGallery/ImageLightbox.tsx
"use client";

import React, { useState, useEffect, useCallback } from "react";
import {
  X,
  ChevronLeft,
  ChevronRight,
  Download,
  ExternalLink,
} from "lucide-react";
import type { ImageResultItem } from "@/lib/types";

interface ImageLightboxProps {
  images: ImageResultItem[];
  initialIndex: number;
  onClose: () => void;
}

/**
 * Full-screen image viewer with prev/next navigation and per-image actions.
 *
 * Accessibility:
 *   * ESC          -> close
 *   * ArrowLeft    -> previous
 *   * ArrowRight   -> next
 *   * Click backdrop -> close
 *
 * Keyboard events are registered on `window` so they work even before the
 * user clicks anywhere inside the modal.
 */
export function ImageLightbox({
  images,
  initialIndex,
  onClose,
}: ImageLightboxProps) {
  const [index, setIndex] = useState(initialIndex);
  const [isLoading, setIsLoading] = useState(true);

  const current = images[index];

  const handlePrev = useCallback(() => {
    if (index > 0) {
      setIndex((i) => i - 1);
      setIsLoading(true);
    }
  }, [index]);

  const handleNext = useCallback(() => {
    if (index < images.length - 1) {
      setIndex((i) => i + 1);
      setIsLoading(true);
    }
  }, [index, images.length]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") handlePrev();
      else if (e.key === "ArrowRight") handleNext();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, handlePrev, handleNext]);

  // Reset loading state when the active image changes.
  useEffect(() => {
    setIsLoading(true);
  }, [index]);

  if (!current) return null;

  const handleDownload = () => {
    if (!current.image_url) return;
    const link = document.createElement("a");
    link.href = current.image_url;
    link.download = current.title || "image";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.click();
  };

  return (
    <div
      className="fixed inset-0 z-[200] bg-black/95 backdrop-blur-sm flex items-center justify-center"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-label="Image viewer"
    >
      {/* Header */}
      <div className="absolute top-0 left-0 right-0 p-3 flex items-start justify-between gap-4 text-white z-10 bg-gradient-to-b from-black/60 to-transparent">
        <div className="text-sm min-w-0 flex-1">
          <div className="font-medium line-clamp-1 text-white">
            {current.title}
          </div>
          <div className="text-[11px] text-white/60 font-mono mt-0.5">
            {index + 1} / {images.length}
            {current.width && current.height
              ? ` · ${current.width}×${current.height}`
              : ""}
            {current.source ? ` · ${current.source}` : ""}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="p-2 rounded-full bg-white/10 hover:bg-white/20 transition-colors cursor-pointer shrink-0"
          title="Close (Esc)"
          aria-label="Close"
        >
          <X size={20} />
        </button>
      </div>

      {/* Prev */}
      {index > 0 && (
        <button
          type="button"
          onClick={handlePrev}
          className="absolute left-4 top-1/2 -translate-y-1/2 p-2 rounded-full bg-white/10 hover:bg-white/20 transition-colors text-white cursor-pointer z-10"
          title="Previous (←)"
          aria-label="Previous image"
        >
          <ChevronLeft size={24} />
        </button>
      )}

      {/* Image container */}
      <div className="max-w-[90vw] max-h-[80vh] flex items-center justify-center relative">
        {isLoading && (
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
            <div className="w-12 h-12 border-2 border-white/30 border-t-white rounded-full animate-spin" />
          </div>
        )}
        <img
          src={current.image_url}
          alt={current.title}
          className={`max-w-[90vw] max-h-[80vh] object-contain rounded-lg shadow-2xl transition-opacity duration-300 ${
            isLoading ? "opacity-0" : "opacity-100"
          }`}
          onLoad={() => setIsLoading(false)}
          onError={() => setIsLoading(false)}
        />
      </div>

      {/* Next */}
      {index < images.length - 1 && (
        <button
          type="button"
          onClick={handleNext}
          className="absolute right-4 top-1/2 -translate-y-1/2 p-2 rounded-full bg-white/10 hover:bg-white/20 transition-colors text-white cursor-pointer z-10"
          title="Next (→)"
          aria-label="Next image"
        >
          <ChevronRight size={24} />
        </button>
      )}

      {/* Bottom actions */}
      <div className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-2 bg-black/70 backdrop-blur-sm rounded-full px-3 py-2 z-10">
        <button
          type="button"
          onClick={handleDownload}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-white hover:bg-white/20 transition-colors text-xs cursor-pointer"
          title="Download"
        >
          <Download size={14} />
          <span>Download</span>
        </button>
        {current.source_url && (
          <a
            href={current.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-white hover:bg-white/20 transition-colors text-xs"
            title="Open source page"
          >
            <ExternalLink size={14} />
            <span>Source</span>
          </a>
        )}
      </div>
    </div>
  );
}
