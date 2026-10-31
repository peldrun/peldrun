// frontend/src/components/chat/ImageGallery/ImageCard.tsx
"use client";

import React, { useState } from "react";
import { ImageOff, ExternalLink, Download } from "lucide-react";
import type { ImageResultItem } from "@/lib/types";

interface ImageCardProps {
  image: ImageResultItem;
  index: number;
  onClick: () => void;
}

/**
 * Single image tile in the gallery grid.
 *
 * Rendering strategy:
 *   1. Load the low-resolution thumbnail first (fast paint).
 *   2. On error, fall back to the full image_url (proxy).
 *   3. On second error, show a placeholder with a link to the source page.
 *   4. On hover, reveal title + dimensions + quick actions.
 *
 * The `aspect-ratio` CSS property derives from the backend-provided
 * width/height, so the grid never shows reflow jumps while images load.
 */
export function ImageCard({ image, index, onClick }: ImageCardProps) {
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);
  const [currentSrc, setCurrentSrc] = useState<string>(
    image.thumbnail_url || image.image_url
  );

  const aspectRatio =
    image.width && image.height
      ? `${image.width} / ${image.height}`
      : "4 / 3";

  const handleError = () => {
    // First failure: try the full image (thumbnail may be missing on
    // some CDNs even when the image is available).
    if (currentSrc !== image.image_url && image.image_url) {
      setCurrentSrc(image.image_url);
      setIsLoading(true);
      return;
    }
    // Second failure: give up, show placeholder.
    setHasError(true);
    setIsLoading(false);
  };

  const handleDownloadClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!image.image_url) return;
    const link = document.createElement("a");
    link.href = image.image_url;
    link.download = image.title || `image-${index + 1}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.click();
  };

  const handleSourceClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!image.source_url) return;
    window.open(image.source_url, "_blank", "noopener,noreferrer");
  };

  // Guard click and keyboard activation when the image failed to load.
  // The wrapper is a <div role="button">, NOT a <button>, because the
  // hover overlay contains its own <button> elements (Source, Download).
  // Nesting <button> inside <button> is invalid HTML and causes React
  // hydration errors (see React docs on DOM validation warnings).
  const activate = () => {
    if (hasError) return;
    onClick();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (hasError) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onClick();
    }
  };

  return (
    <div
      role="button"
      tabIndex={hasError ? -1 : 0}
      aria-disabled={hasError}
      onClick={activate}
      onKeyDown={handleKeyDown}
      className="group relative overflow-hidden rounded-md border border-border bg-muted/30 hover:border-primary/50 hover:shadow-md transition-all cursor-pointer aria-disabled:cursor-not-allowed aria-disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
      style={{ aspectRatio }}
      title={image.title}
    >
      {/* Loading skeleton */}
      {isLoading && !hasError && (
        <div className="absolute inset-0 flex items-center justify-center bg-muted animate-pulse">
          <div className="w-8 h-8 rounded-full bg-muted-foreground/20" />
        </div>
      )}

      {/* Error placeholder */}
      {hasError && (
        <div className="absolute inset-0 flex flex-col items-center justify-center text-muted-foreground bg-muted/50 p-2 text-center">
          <ImageOff size={20} className="mb-1 opacity-50" />
          <span className="text-[9px] line-clamp-2 leading-tight">
            {image.title}
          </span>
          {image.source_url && (
            <a
              href={image.source_url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="mt-1 text-[9px] text-primary hover:underline inline-flex items-center gap-0.5"
            >
              <ExternalLink size={8} />
              source
            </a>
          )}
        </div>
      )}

      {/* Image */}
      {!hasError && (
        <img
          src={currentSrc}
          alt={image.title}
          loading="lazy"
          className={`w-full h-full object-cover transition-opacity duration-300 ${
            isLoading ? "opacity-0" : "opacity-100"
          }`}
          onLoad={() => setIsLoading(false)}
          onError={handleError}
        />
      )}

      {/* Hover overlay */}
      {!hasError && !isLoading && (
        <div className="absolute inset-0 bg-gradient-to-t from-black/80 via-black/30 to-transparent opacity-0 group-hover:opacity-100 transition-opacity">
          <div className="absolute bottom-0 left-0 right-0 p-1.5 space-y-1">
            <p className="text-[10px] text-white font-medium line-clamp-2 text-left leading-tight">
              {image.title}
            </p>
            <div className="flex items-center justify-between">
              {image.width && image.height && (
                <span className="text-[9px] text-white/70 font-mono">
                  {image.width}×{image.height}
                </span>
              )}
              <div className="flex items-center gap-1">
                {image.source_url && (
                  <button
                    type="button"
                    onClick={handleSourceClick}
                    className="p-1 rounded bg-white/20 hover:bg-white/30 text-white transition-colors cursor-pointer"
                    title="Open source page"
                  >
                    <ExternalLink size={9} />
                  </button>
                )}
                <button
                  type="button"
                  onClick={handleDownloadClick}
                  className="p-1 rounded bg-white/20 hover:bg-white/30 text-white transition-colors cursor-pointer"
                  title="Download image"
                >
                  <Download size={9} />
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
