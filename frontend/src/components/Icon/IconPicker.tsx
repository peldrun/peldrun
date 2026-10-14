// src/components/IconPicker.tsx
import { useState, useMemo, useRef, useEffect, useCallback } from 'react';
import { useIcons, Icon } from './Icon';
import { InlineIcon } from './InlineIcon';

type IconPickerProps = {
  value?: string;
  onChange: (iconName: string) => void;
  placeholder?: string;
  size?: number;
  /** Maximum height of the dropdown in pixels */
  maxHeight?: number;
  /** Show the variant filter tabs (all / color / mono / wordmark) */
  showVariantFilter?: boolean;
  /**
   * How to render icons in the picker.
   * - "img"    → use <Icon> (default). Displays original colors, works for all SVGs.
   * - "inline" → use <InlineIcon>. Inherits text color, best for "-mono" icons.
   * - "auto"   → "inline" for mono icons, "img" for everything else.
   */
  renderMode?: 'img' | 'inline' | 'auto';
};

type VariantFilter = 'all' | 'color' | 'mono' | 'wordmark';

// Known variant suffixes used to build the filter tabs
const MONO_SUFFIXES = ['-mono'];
const WORDMARK_SUFFIXES = ['-wordmark'];

function classify(name: string): Exclude<VariantFilter, 'all'> {
  if (MONO_SUFFIXES.some((s) => name.endsWith(s))) return 'mono';
  if (WORDMARK_SUFFIXES.some((s) => name.endsWith(s))) return 'wordmark';
  return 'color';
}

/**
 * Renders an icon using the strategy selected by `mode`.
 */
function RenderIcon({
  name,
  size,
  mode,
}: {
  name: string;
  size: number;
  mode: 'img' | 'inline' | 'auto';
}) {
  const useInline =
    mode === 'inline' || (mode === 'auto' && classify(name) === 'mono');

  if (useInline) {
    return <InlineIcon name={name} size={size} forceCurrentColor />;
  }
  return <Icon name={name} size={size} />;
}

/**
 * Highlights the matched portion of the icon name.
 */
function Highlight({ text, query }: { text: string; query: string }) {
  const q = query.trim().toLowerCase();
  if (!q) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(q);
  if (idx === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="rounded bg-yellow-200 px-0.5">
        {text.slice(idx, idx + q.length)}
      </mark>
      {text.slice(idx + q.length)}
    </>
  );
}

/**
 * A searchable icon picker dropdown with keyboard navigation.
 *
 * Usage:
 *   <IconPicker value={icon} onChange={setIcon} />
 *   <IconPicker value={icon} onChange={setIcon} renderMode="inline" />
 *   <IconPicker value={icon} onChange={setIcon} renderMode="auto" />
 */
export function IconPicker({
  value,
  onChange,
  placeholder = 'Search icons...',
  size = 28,
  maxHeight = 320,
  showVariantFilter = true,
  renderMode = 'img',
}: IconPickerProps) {
  const manifest = useIcons();
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [variant, setVariant] = useState<VariantFilter>('all');
  const [activeIndex, setActiveIndex] = useState(0);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Filter icons by search query and variant tab
  const filtered = useMemo(() => {
    if (!manifest) return [];
    const q = query.trim().toLowerCase();
    return manifest.icons.filter((name) => {
      if (q && !name.toLowerCase().includes(q)) return false;
      if (variant !== 'all' && classify(name) !== variant) return false;
      return true;
    });
  }, [manifest, query, variant]);

  // Reset the highlighted index when the filtered list changes
  useEffect(() => {
    setActiveIndex(0);
  }, [query, variant]);

  // Scroll the active item into view
  useEffect(() => {
    if (!isOpen || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(
      `[data-index="${activeIndex}"]`
    );
    el?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, isOpen]);

  const select = useCallback(
    (name: string) => {
      onChange(name);
      setIsOpen(false);
      setQuery('');
    },
    [onChange]
  );

  // Keyboard navigation handler
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (!isOpen) {
      if (e.key === 'ArrowDown' || e.key === 'Enter') {
        e.preventDefault();
        setIsOpen(true);
      }
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      setIsOpen(false);
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const hit = filtered[activeIndex];
      if (hit) select(hit);
    }
  };

  const counts = useMemo(() => {
    if (!manifest) return { all: 0, color: 0, mono: 0, wordmark: 0 };
    const c = { all: manifest.icons.length, color: 0, mono: 0, wordmark: 0 };
    for (const name of manifest.icons) c[classify(name)]++;
    return c;
  }, [manifest]);

  if (!manifest) {
    return <div className="text-sm text-gray-500">Loading icons...</div>;
  }

  return (
    <div ref={wrapperRef} className="relative inline-block w-full">
      {/* Trigger button */}
      <button
        type="button"
        onClick={() => setIsOpen((o) => !o)}
        onKeyDown={handleKeyDown}
        className="flex w-full items-center gap-2 rounded border border-gray-300 bg-white px-3 py-2 text-left text-sm shadow-sm hover:border-gray-400 focus:border-blue-500 focus:outline-none"
      >
        {value ? (
          <>
            <RenderIcon name={value} size={size} mode={renderMode} />
            <span className="flex-1 truncate">{value}</span>
          </>
        ) : (
          <span className="flex-1 text-gray-400">Select an icon...</span>
        )}
        <span className="text-gray-400">▾</span>
      </button>

      {/* Dropdown panel */}
      {isOpen && (
        <div
          className="absolute z-50 mt-1 w-full overflow-hidden rounded border border-gray-200 bg-white shadow-lg"
          onKeyDown={handleKeyDown}
        >
          {/* Search input */}
          <div className="border-b border-gray-100 p-2">
            <input
              autoFocus
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={placeholder}
              className="w-full rounded border border-gray-300 px-2 py-1 text-sm focus:border-blue-500 focus:outline-none"
            />
          </div>

          {/* Variant filter tabs */}
          {showVariantFilter && (
            <div className="flex border-b border-gray-100 bg-gray-50 text-xs">
              {(['all', 'color', 'mono', 'wordmark'] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setVariant(v)}
                  className={`flex-1 px-2 py-1.5 capitalize transition ${
                    variant === v
                      ? 'bg-white font-medium text-blue-600'
                      : 'text-gray-500 hover:text-gray-700'
                  }`}
                >
                  {v} <span className="text-gray-400">({counts[v]})</span>
                </button>
              ))}
            </div>
          )}

          {/* Icon grid */}
          <div
            ref={listRef}
            className="overflow-y-auto p-2"
            style={{ maxHeight }}
          >
            {filtered.length === 0 ? (
              <div className="p-4 text-center text-sm text-gray-500">
                No icons found.
              </div>
            ) : (
              <div className="grid grid-cols-4 gap-1">
                {filtered.map((name, i) => (
                  <button
                    key={name}
                    type="button"
                    data-index={i}
                    onMouseEnter={() => setActiveIndex(i)}
                    onClick={() => select(name)}
                    title={name}
                    className={`flex flex-col items-center rounded p-2 transition ${
                      activeIndex === i ? 'bg-blue-50 ring-1 ring-blue-400' : ''
                    } ${
                      value === name ? 'bg-blue-100 ring-2 ring-blue-500' : ''
                    }`}
                  >
                    <RenderIcon name={name} size={size} mode={renderMode} />
                    <span className="mt-1 w-full truncate text-center text-[10px] text-gray-600">
                      <Highlight text={name} query={query} />
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Footer with count */}
          <div className="border-t border-gray-100 bg-gray-50 px-2 py-1 text-[10px] text-gray-500">
            {filtered.length} of {manifest.icons.length} icons
            <span className="ml-2">↑↓ navigate · Enter select · Esc close</span>
          </div>
        </div>
      )}
    </div>
  );
}
