// frontend/src/components/generative-ui/catalog/PeldrunCarousel.tsx
"use client";

import React, { useState, useEffect, useCallback } from "react";
import useEmblaCarousel from "embla-carousel-react";
import { ChevronLeft, ChevronRight, Layers } from "lucide-react";
import { PeldrunMotionWrapper } from "./PeldrunMotionWrapper";

export interface CarouselSlide {
  title?: string;
  description?: string;
  content: string;
}

interface PeldrunCarouselProps {
  slides: CarouselSlide[];
  title?: string;
}

export function PeldrunCarousel({ slides, title }: PeldrunCarouselProps): React.JSX.Element {
  const [emblaRef, emblaApi] = useEmblaCarousel({ loop: false, align: "start" });
  const [prevBtnEnabled, setPrevBtnEnabled] = useState(false);
  const [nextBtnEnabled, setNextBtnEnabled] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);

  const scrollPrev = useCallback(() => emblaApi && emblaApi.scrollPrev(), [emblaApi]);
  const scrollNext = useCallback(() => emblaApi && emblaApi.scrollNext(), [emblaApi]);
  const scrollTo = useCallback((idx: number) => emblaApi && emblaApi.scrollTo(idx), [emblaApi]);

  const onSelect = useCallback(() => {
    if (!emblaApi) return;
    setSelectedIndex(emblaApi.selectedScrollSnap());
    setPrevBtnEnabled(emblaApi.canScrollPrev());
    setNextBtnEnabled(emblaApi.canScrollNext());
  }, [emblaApi]);

  useEffect(() => {
    if (!emblaApi) return;
    onSelect();
    emblaApi.on("select", onSelect);
    emblaApi.on("reInit", onSelect);
  }, [emblaApi, onSelect]);

  if (!slides || slides.length === 0) return <></>;

  return (
    <PeldrunMotionWrapper className="my-2.5">
      <div className="rounded-xl border border-border/80 bg-card/60 backdrop-blur-sm p-3.5 shadow-xs">
        {/* Navigation Header */}
        <div className="flex items-center justify-between mb-3 border-b border-border/40 pb-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
            <Layers size={14} className="text-primary shrink-0" />
            <span>{title || "Interactive Multi-Card Showcase"}</span>
            <span className="text-[10px] text-muted-foreground font-mono">
              ({selectedIndex + 1}/{slides.length})
            </span>
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={scrollPrev}
              disabled={!prevBtnEnabled}
              className="p-1 rounded-md bg-muted/50 hover:bg-muted text-muted-foreground hover:text-foreground disabled:opacity-30 disabled:pointer-events-none transition-colors"
            >
              <ChevronLeft size={14} />
            </button>
            <button
              type="button"
              onClick={scrollNext}
              disabled={!nextBtnEnabled}
              className="p-1 rounded-md bg-muted/50 hover:bg-muted text-muted-foreground hover:text-foreground disabled:opacity-30 disabled:pointer-events-none transition-colors"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </div>

        {/* Carousel Viewport */}
        <div className="overflow-hidden" ref={emblaRef}>
          <div className="flex gap-3">
            {slides.map((s, idx) => (
              <div
                key={`slide-${idx}`}
                className="flex-[0_0_85%] sm:flex-[0_0_65%] min-w-0 rounded-lg border border-border/60 bg-background/50 p-3.5 flex flex-col justify-between"
              >
                <div>
                  {s.title && (
                    <h4 className="font-semibold text-xs text-foreground mb-1 leading-snug">
                      {s.title}
                    </h4>
                  )}
                  {s.description && (
                    <p className="text-[11px] text-muted-foreground mb-2 leading-relaxed">
                      {s.description}
                    </p>
                  )}
                  <div className="text-xs text-foreground/90 leading-relaxed whitespace-pre-wrap">
                    {s.content}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Dots Pagination */}
        {slides.length > 1 && (
          <div className="flex justify-center items-center gap-1.5 mt-3">
            {slides.map((_, idx) => (
              <button
                key={`dot-${idx}`}
                type="button"
                onClick={() => scrollTo(idx)}
                className={`h-1.5 rounded-full transition-all ${
                  idx === selectedIndex ? "w-5 bg-primary" : "w-1.5 bg-muted-foreground/30 hover:bg-muted-foreground/50"
                }`}
              />
            ))}
          </div>
        )}
      </div>
    </PeldrunMotionWrapper>
  );
}
