// frontend/src/components/chat/ImageGallery/index.tsx
"use client";

import React, { useState } from "react";
import { Images, ExternalLink } from "lucide-react";
import type { ImageResultItem } from "@/lib/types";
import { ImageCard } from "./ImageCard";
import { ImageLightbox } from "./ImageLightbox";

interface ImageGalleryProps {
  images: ImageResultItem[];
  query?: string;
  provider?: string;
  /** Max images rendered in the grid. Default 12; the rest are "See source". */
  maxVisible?: number;
  /** Optional source URL for the entire search (e.g. the provider's page). */
  sourceUrl?: string | null;
}

/**
 * Responsive image grid with built-in lightbox.
 *
 * Layout:
 *   * Mobile:  2 columns
 *   * Tablet:  3 columns
 *   * Desktop: 4 columns
 *
 * Behaviour:
 *   * Lazy-loaded thumbnails via ImageCard.
 *   * Click opens ImageLightbox with keyboard navigation.
 *   * Falls back gracefully when individual images fail to load.
 *
 * Both agent-mode (inside ChatTimeline) and chat-mode (inside
 * MarkdownRenderer's image_gallery block) use this exact component, so the
 * visual experience is identical regardless of the run mode.
 */
export function ImageGallery({
  images,
  query,
  provider,
  maxVisible = 12,
  sourceUrl,
}: ImageGalleryProps) {
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

  if (!images || images.length === 0) return null;

  const visibleImages = images.slice(0, maxVisible);
  const hasMore = images.length > maxVisible;

  return (
    <>
      <div className="my-3 rounded-lg border border-border bg-card/60 p-3 shadow-xs">
        {/* Header */}
        <div className="flex items-center justify-between gap-2 mb-2 pb-2 border-b border-border/60">
          <div className="flex items-center gap-2 text-xs min-w-0 flex-1">
            <Images size={14} className="text-primary shrink-0" />
            <span className="font-semibold text-foreground shrink-0">
              {images.length} {images.length === 1 ? "Image" : "Images"}
            </span>
            {query && (
              <span className="text-muted-foreground font-normal truncate">
                · &quot;{query}&quot;
              </span>
            )}
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            {sourceUrl && (
              <a
                href={sourceUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-muted hover:bg-muted/70 text-muted-foreground border border-border transition-colors inline-flex items-center gap-1"
                title="Open source page"
              >
                <ExternalLink size={9} />
                <span className="hidden sm:inline">source</span>
              </a>
            )}
            {provider && (
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-muted text-muted-foreground border border-border">
                via {provider}
              </span>
            )}
          </div>
        </div>

        {/* Grid */}
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
          {visibleImages.map((img, idx) => (
            <ImageCard
              key={`${img.image_url}-${idx}`}
              image={img}
              index={idx}
              onClick={() => setLightboxIndex(idx)}
            />
          ))}
        </div>

        {hasMore && (
          <div className="mt-2 text-center text-[10px] text-muted-foreground">
            +{images.length - maxVisible} more image
            {images.length - maxVisible === 1 ? "" : "s"} not shown
          </div>
        )}
      </div>

      {lightboxIndex !== null && (
        <ImageLightbox
          images={visibleImages}
          initialIndex={lightboxIndex}
          onClose={() => setLightboxIndex(null)}
        />
      )}
    </>
  );
}

export default ImageGallery;
