// Path: frontend/src/components/skills/editor/skill-floating-toolbar-plugin.tsx

'use client';

/**
 * Floating toolbar that appears above a text selection inside the skill
 * editor.
 *
 * Behavior:
 *   - Shows only when the selection is non-empty and non-collapsed.
 *   - Positions itself above the selection's bounding box.
 *   - Provides the three quick actions users need most often: bold,
 *     italic, and link insertion. Block-level operations (headings,
 *     lists, quotes, code blocks) live in the fixed toolbar above the
 *     editor — that one is always visible so the user never has to
 *     re-open a floating popup to switch paragraph type.
 *
 * Why this is a separate file (not imported from
 * `components/chat/composer/plugins/`):
 *   - The skill editor and the chat composer are independent
 *     subsystems. Sharing a floating-toolbar plugin would couple
 *     their lifecycles (a future change for one could break the
 *     other).
 *   - The composer's toolbar includes a block-type dropdown; the skill
 *     editor's does not (that functionality lives in the fixed
 *     toolbar). Keeping the two implementations separate avoids
 *     conditional branches and keeps each focused.
 *
 * Styling is deliberately identical to the composer toolbar so the
 * editor feels consistent with the rest of the app.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import {
  $getSelection,
  $isRangeSelection,
  FORMAT_TEXT_COMMAND,
} from 'lexical';
import { Bold, Code, Italic, Link2, Strikethrough } from 'lucide-react';

export function SkillFloatingToolbarPlugin() {
  const [editor] = useLexicalComposerContext();
  const toolbarRef = useRef<HTMLDivElement | null>(null);
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const [isBold, setIsBold] = useState(false);
  const [isItalic, setIsItalic] = useState(false);
  const [isStrikethrough, setIsStrikethrough] = useState(false);
  const [isCode, setIsCode] = useState(false);

  const updateToolbar = useCallback(() => {
    const selection = $getSelection();
    if (
      !$isRangeSelection(selection) ||
      selection.isCollapsed() ||
      editor.isComposing()
    ) {
      setIsVisible(false);
      return;
    }

    const domSelection = window.getSelection();
    if (!domSelection || domSelection.rangeCount === 0) {
      setIsVisible(false);
      return;
    }

    const range = domSelection.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) {
      setIsVisible(false);
      return;
    }

    setIsBold(selection.hasFormat('bold'));
    setIsItalic(selection.hasFormat('italic'));
    setIsStrikethrough(selection.hasFormat('strikethrough'));
    setIsCode(selection.hasFormat('code'));

    setPosition({
      top: rect.top - 46,
      left: rect.left + rect.width / 2,
    });
    setIsVisible(true);
  }, [editor]);

  useEffect(() => {
    return editor.registerUpdateListener(({ editorState }) => {
      editorState.read(() => {
        updateToolbar();
      });
    });
  }, [editor, updateToolbar]);

  const handleLinkPrompt = () => {
    const url = window.prompt(
      'Enter link URL (e.g. https://example.com):',
    );
    if (!url) return;

    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) {
        const text = selection.getTextContent() || url;
        const cleanDisplay = url
          .replace(/^https?:\/\//, '')
          .replace(/\/$/, '');
        selection.insertText(
          `[${cleanDisplay || text}](${url})`,
        );
      }
    });
  };

  if (!isVisible) return null;

  return (
    <div
      ref={toolbarRef}
      style={{
        top: `${Math.max(10, position.top)}px`,
        left: `${position.left}px`,
      }}
      className="fixed z-[99999] -translate-x-1/2 flex items-center gap-1 rounded-xl border border-border/80 bg-popover/95 px-1.5 py-1 text-popover-foreground shadow-xl backdrop-blur-md transition-all font-sans animate-in fade-in zoom-in-95"
    >
      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={handleLinkPrompt}
        className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted cursor-pointer transition-colors"
        title="Add Link"
      >
        <Link2 size={14} />
      </button>

      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() =>
          editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'bold')
        }
        className={`p-1.5 rounded-lg cursor-pointer transition-colors ${
          isBold
            ? 'bg-primary/15 text-primary font-bold'
            : 'text-muted-foreground hover:text-foreground hover:bg-muted'
        }`}
        title="Bold"
      >
        <Bold size={14} />
      </button>

      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() =>
          editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'italic')
        }
        className={`p-1.5 rounded-lg cursor-pointer transition-colors ${
          isItalic
            ? 'bg-primary/15 text-primary italic'
            : 'text-muted-foreground hover:text-foreground hover:bg-muted'
        }`}
        title="Italic"
      >
        <Italic size={14} />
      </button>

      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() =>
          editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'strikethrough')
        }
        className={`p-1.5 rounded-lg cursor-pointer transition-colors ${
          isStrikethrough
            ? 'bg-primary/15 text-primary line-through'
            : 'text-muted-foreground hover:text-foreground hover:bg-muted'
        }`}
        title="Strikethrough"
      >
        <Strikethrough size={14} />
      </button>

      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() =>
          editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'code')
        }
        className={`p-1.5 rounded-lg cursor-pointer transition-colors ${
          isCode
            ? 'bg-primary/15 text-primary font-mono'
            : 'text-muted-foreground hover:text-foreground hover:bg-muted'
        }`}
        title="Inline code"
      >
        <Code size={14} />
      </button>
    </div>
  );
}

export default SkillFloatingToolbarPlugin;
