// src/components/InlineIcon.tsx
import { useEffect, useState, useRef } from 'react';

// In-memory cache so we never fetch the same SVG twice
const svgCache = new Map<string, string>();
const inflight = new Map<string, Promise<string>>();

async function fetchSvgContent(name: string): Promise<string> {
  if (svgCache.has(name)) return svgCache.get(name)!;
  if (inflight.has(name)) return inflight.get(name)!;

  const promise = fetch(`/icon/${name}.svg`)
    .then((res) => {
      if (!res.ok) throw new Error(`Icon not found: ${name}`);
      return res.text();
    })
    .then((text) => {
      svgCache.set(name, text);
      inflight.delete(name);
      return text;
    })
    .catch((err) => {
      inflight.delete(name);
      throw err;
    });

  inflight.set(name, promise);
  return promise;
}

/**
 * Renders an SVG file as an inline element so it inherits `color`
 * from its parent (supports currentColor).
 *
 * Usage:
 *   <InlineIcon name="anthropic-mono" size={24} className="text-blue-500" />
 */
type InlineIconProps = {
  name: string;
  size?: number | string;
  className?: string;
  /** If true, force all fills/strokes to currentColor */
  forceCurrentColor?: boolean;
  title?: string;
};

export function InlineIcon({
  name,
  size = 24,
  className,
  forceCurrentColor = false,
  title,
}: InlineIconProps) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const wrapperRef = useRef<HTMLSpanElement>(null);

  // Load and cache the SVG text
  useEffect(() => {
    let cancelled = false;
    fetchSvgContent(name)
      .then((text) => {
        if (!cancelled) setContent(text);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [name]);

  // Post-process the SVG string: remove fixed width/height, optionally override fill
  const processed = useMemo(() => {
    if (!content) return null;
    let svg = content;

    // Remove hardcoded width/height so our size prop controls it
    svg = svg.replace(/\s(width|height)="[^"]*"/g, '');

    // Ensure the root <svg> has our size and currentColor-friendly defaults
    svg = svg.replace(
      /<svg\b/,
      `<svg width="${size}" height="${size}" fill="currentColor"`
    );

    // Optionally force every fill/stroke to currentColor
    if (forceCurrentColor) {
      svg = svg.replace(/fill="(?!none|currentColor)[^"]*"/g, 'fill="currentColor"');
      svg = svg.replace(/stroke="(?!none|currentColor)[^"]*"/g, 'stroke="currentColor"');
    }

    return svg;
  }, [content, size, forceCurrentColor]);

  if (error) {
    return <span className="inline-block text-red-500">?</span>;
  }

  if (!processed) {
    // Placeholder while loading (same dimensions to avoid layout shift)
    return (
      <span
        ref={wrapperRef}
        className={className}
        style={{ display: 'inline-block', width: size, height: size }}
      />
    );
  }

  return (
    <span
      ref={wrapperRef}
      className={className}
      style={{ display: 'inline-block', width: size, height: size }}
      title={title ?? name}
      dangerouslySetInnerHTML={{ __html: processed }}
    />
  );
}

import { useMemo } from 'react';
