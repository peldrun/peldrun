// src/components/Icon.tsx
import { useEffect, useState } from 'react';

// Type definition for the icon manifest structure
export type IconManifest = {
  generatedAt: string;
  count: number;
  icons: string[];
  groups: Record<string, string[]>;
};

// A simple in-memory cache to avoid re-fetching the manifest on every component mount
let manifestCache: IconManifest | null = null;

/**
 * Loads the icon manifest from the public folder.
 * It uses a cache to prevent unnecessary network requests.
 */
export async function loadManifest(): Promise<IconManifest> {
  if (manifestCache) {
    return Promise.resolve(manifestCache);
  }
  try {
    const response = await fetch('/icon/manifest.json', { cache: 'no-cache' });
    if (!response.ok) {
      throw new Error('Manifest file not found. Have you run `npm run icons`?');
    }
    const manifest: IconManifest = await response.json();
    manifestCache = manifest;
    return manifest;
  } catch (error) {
    console.warn('[Icon] Failed to load manifest:', error);
    return { generatedAt: '', count: 0, icons: [], groups: {} };
  }
}

// React hook to access the icon manifest in any component
export function useIcons() {
  const [manifest, setManifest] = useState<IconManifest | null>(manifestCache);

  useEffect(() => {
    loadManifest().then(setManifest);
  }, []);

  return manifest;
}

// Props for the Icon component
type IconProps = Omit<React.ImgHTMLAttributes<HTMLImageElement>, 'src'> & {
  name: string;
  size?: number | string;
  variant?: 'dark' | 'light' | 'mono' | 'color' | 'wordmark';
  fallback?: string;
};

/**
 * A React component to display an icon by its name.
 *
 * Usage:
 * <Icon name="openai-chatgpt" size={32} />
 * <Icon name="anthropic" variant="dark" size={28} />
 */
export function Icon({
  name,
  size = 24,
  variant,
  fallback = 'openai-chatgpt',
  className,
  alt,
  style,
  ...rest
}: IconProps) {
  // Build a list of candidate names to try, in order of priority.
  const candidates = variant ? [`${name}-${variant}`, name] : [name];

  // Track which candidate we're currently attempting.
  const [currentIndex, setCurrentIndex] = useState(0);

  // Reset the attempt index whenever the requested name or variant changes.
  useEffect(() => {
    setCurrentIndex(0);
  }, [name, variant]);

  // If we've exhausted all candidates, switch to the fallback.
  const usingFallback = currentIndex >= candidates.length;
  const finalName = usingFallback ? fallback : candidates[currentIndex];

  // If the fallback itself has failed, render nothing to avoid an error loop.
  const [fallbackFailed, setFallbackFailed] = useState(false);

  // Reset fallbackFailed when the request changes.
  useEffect(() => {
    setFallbackFailed(false);
  }, [name, variant]);

  // Normalize width/height: use numeric attributes when possible,
  // otherwise rely on style so CSS units like "2rem" work.
  const numericSize = typeof size === 'number' || /^\d+$/.test(String(size));
  const dimensionProps = numericSize
    ? { width: size, height: size }
    : { style: { width: size, height: size, ...style } };

  if (fallbackFailed) {
    return null;
  }

  return (
    <img
      src={`/icon/${finalName}.svg`}
      alt={alt ?? finalName}
      className={className}
      loading="lazy"
      decoding="async"
      {...dimensionProps}
      {...rest}
      onError={() => {
        if (currentIndex < candidates.length) {
          // Try the next candidate.
          setCurrentIndex(currentIndex + 1);
        } else {
          // Fallback also failed; stop trying to avoid infinite loop.
          setFallbackFailed(true);
        }
      }}
    />
  );
}
