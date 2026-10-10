// Path: frontend/src/components/chat/composer/plugins/markdown-highlight-plugin.tsx

'use client';

import { useEffect } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import {
  $getRoot,
  $isParagraphNode,
  $isTextNode,
  TextNode,
} from 'lexical';

const MARKDOWN_TAG = 'markdown-highlight';

const TOKEN_REGEX = /(`[^`\n]+`|\*\*[^*\n]+\*\*|@[a-zA-Z0-9_.-]+|(?:^|\s)\/[a-zA-Z0-9_-]+)/;

const BOLD_REGEX = /^\*\*[^*\n]+\*\*$/;
const CODE_REGEX = /^`[^`\n]+`$/;
const MENTION_REGEX = /^@[a-zA-Z0-9_.-]+$/;
const SLASH_REGEX = /^\/[a-zA-Z0-9_-]+$/;

function getStyleForToken(text: string): string {
  const trimmed = text.trim();
  if (CODE_REGEX.test(trimmed)) {
    return 'color: #0284c7; font-family: monospace; font-weight: 600; background-color: rgba(2, 132, 199, 0.1); border-radius: 3px; padding: 1px 3px;';
  }
  if (BOLD_REGEX.test(trimmed)) {
    return 'font-weight: 700; color: #0284c7;';
  }
  if (MENTION_REGEX.test(trimmed)) {
    return 'color: #10b981; font-weight: 600;';
  }
  if (SLASH_REGEX.test(trimmed)) {
    return 'color: #8b5cf6; font-weight: 600;';
  }
  return '';
}

function processTextNode(node: TextNode): boolean {
  const text = node.getTextContent();
  const currentStyle = node.getStyle();

  if (currentStyle !== '') {
    const expectedStyle = getStyleForToken(text);
    if (expectedStyle === currentStyle) {
      return false;
    }
    node.setStyle('');
    return true;
  }

  const match = TOKEN_REGEX.exec(text);
  if (!match) return false;

  let start = match.index;
  let tokenText = match[0];

  if (tokenText.startsWith(' ') || tokenText.startsWith('\t')) {
    start += 1;
    tokenText = tokenText.slice(1);
  }

  const end = start + tokenText.length;
  const style = getStyleForToken(tokenText);
  if (!style) return false;

  if (start === 0 && end === text.length) {
    node.setStyle(style);
    return true;
  }

  if (start === 0) {
    const [matchNode] = node.splitText(end);
    matchNode.setStyle(style);
    return true;
  }

  if (end === text.length) {
    const [, matchNode] = node.splitText(start);
    if (matchNode) {
      matchNode.setStyle(style);
    }
    return true;
  }

  const [, matchNode] = node.splitText(start, end);
  if (matchNode) {
    matchNode.setStyle(style);
  }
  return true;
}

export function MarkdownHighlightPlugin() {
  const [editor] = useLexicalComposerContext();

  useEffect(() => {
    return editor.registerUpdateListener(({ dirtyElements, dirtyLeaves, tags }) => {
      if (tags.has(MARKDOWN_TAG)) return;
      if (dirtyElements.size === 0 && dirtyLeaves.size === 0) return;

      editor.update(
        () => {
          const root = $getRoot();
          const paragraphs = root.getChildren();

          for (const paragraph of paragraphs) {
            if (!$isParagraphNode(paragraph)) continue;
            const children = [...paragraph.getChildren()];
            for (const child of children) {
              if ($isTextNode(child)) {
                processTextNode(child);
              }
            }
          }
        },
        { tag: MARKDOWN_TAG }
      );
    });
  }, [editor]);

  return null;
}

export default MarkdownHighlightPlugin;
