// Path: frontend/src/components/chat/composer/plugins/floating-toolbar-plugin.tsx

'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import {
  $createParagraphNode,$getSelection,
  $isElementNode,$isRangeSelection,
  FORMAT_TEXT_COMMAND,
} from 'lexical';
import { $createHeadingNode,$isHeadingNode, HeadingTagType } from '@lexical/rich-text';
import { Bold, ChevronDown, Italic, Link2 } from 'lucide-react';

export function FloatingToolbarPlugin() {
  const [editor] = useLexicalComposerContext();
  const toolbarRef = useRef<HTMLDivElement | null>(null);
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const [isBold, setIsBold] = useState(false);
  const [isItalic, setIsItalic] = useState(false);
  const [blockType, setBlockType] = useState<string>('paragraph');
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);

  const updateToolbar = useCallback(() => {
    const selection = $getSelection();
    if (!$isRangeSelection(selection) || selection.isCollapsed() || editor.isComposing()) {
      setIsVisible(false);
      setIsDropdownOpen(false);
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

    const anchorNode = selection.anchor.getNode();
    const topElement = anchorNode.getTopLevelElement();

    if (topElement && $isHeadingNode(topElement)) {
      setBlockType(topElement.getTag());
    } else {
      setBlockType('paragraph');
    }

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

  const setHeadingOrParagraph = (type: 'paragraph' | HeadingTagType) => {
    editor.update(() => {
      const selection = $getSelection();
      if (!$isRangeSelection(selection)) return;

      const anchorNode = selection.anchor.getNode();
      const topElement = anchorNode.getTopLevelElement();

      // Guard strictly with $isElementNode to satisfy TypeScript and guarantee getChildren existence
      if (topElement && $isElementNode(topElement)) {
        const children = topElement.getChildren();
        if (type === 'paragraph') {
          const paragraph = $createParagraphNode();
          paragraph.append(...children);
          topElement.replace(paragraph);
          paragraph.select();
        } else {
          const heading = $createHeadingNode(type);
          heading.append(...children);
          topElement.replace(heading);
          heading.select();
        }
      }
    });
    setBlockType(type);
    setIsDropdownOpen(false);
  };

  const handleLinkPrompt = () => {
    const url = window.prompt('Enter link URL (e.g. https://github.com/peldrun/peldrun):');
    if (!url) return;

    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) {
        const text = selection.getTextContent() || url;
        const cleanDisplay = url.replace(/^https?:\/\//, '').replace(/\/$/, '');
        selection.insertText(`[${cleanDisplay || text}](${url})`);
      }
    });
  };

  if (!isVisible) return null;

  const blockLabels: Record<string, string> = {
    paragraph: 'Text',
    h1: 'Heading 1',
    h2: 'Heading 2',
    h3: 'Heading 3',
  };

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
        onClick={handleLinkPrompt}
        className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted cursor-pointer transition-colors"
        title="Add Link"
      >
        <Link2 size={14} />
      </button>

      <button
        type="button"
        onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'bold')}
        className={`p-1.5 rounded-lg cursor-pointer transition-colors ${
          isBold ? 'bg-primary/15 text-primary font-bold' : 'text-muted-foreground hover:text-foreground hover:bg-muted'
        }`}
        title="Bold"
      >
        <Bold size={14} />
      </button>

      <button
        type="button"
        onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'italic')}
        className={`p-1.5 rounded-lg cursor-pointer transition-colors ${
          isItalic ? 'bg-primary/15 text-primary italic' : 'text-muted-foreground hover:text-foreground hover:bg-muted'
        }`}
        title="Italic"
      >
        <Italic size={14} />
      </button>

      <div className="w-[1px] h-4 bg-border/60 mx-0.5" />

      <div className="relative">
        <button
          type="button"
          onClick={() => setIsDropdownOpen(!isDropdownOpen)}
          className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium text-foreground hover:bg-muted cursor-pointer transition-colors"
        >
          <span>{blockLabels[blockType] || 'Heading 3'}</span>
          <ChevronDown size={12} className="text-muted-foreground" />
        </button>

        {isDropdownOpen && (
          <div className="absolute top-full mt-1.5 start-0 min-w-[120px] rounded-lg border border-border/80 bg-popover p-1 shadow-2xl backdrop-blur-md flex flex-col gap-0.5 z-50">
            <button
              type="button"
              onClick={() => setHeadingOrParagraph('paragraph')}
              className={`w-full text-start px-2 py-1.5 rounded-md text-xs cursor-pointer ${
                blockType === 'paragraph' ? 'bg-primary text-primary-foreground font-medium' : 'hover:bg-muted text-foreground'
              }`}
            >
              Normal Text
            </button>
            <button
              type="button"
              onClick={() => setHeadingOrParagraph('h1')}
              className={`w-full text-start px-2 py-1.5 rounded-md text-xs font-bold cursor-pointer ${
                blockType === 'h1' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted text-foreground'
              }`}
            >
              Heading 1
            </button>
            <button
              type="button"
              onClick={() => setHeadingOrParagraph('h2')}
              className={`w-full text-start px-2 py-1.5 rounded-md text-xs font-semibold cursor-pointer ${
                blockType === 'h2' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted text-foreground'
              }`}
            >
              Heading 2
            </button>
            <button
              type="button"
              onClick={() => setHeadingOrParagraph('h3')}
              className={`w-full text-start px-2 py-1.5 rounded-md text-xs font-medium cursor-pointer ${
                blockType === 'h3' ? 'bg-primary text-primary-foreground' : 'hover:bg-muted text-foreground'
              }`}
            >
              Heading 3
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default FloatingToolbarPlugin;
