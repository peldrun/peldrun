// Path: frontend/src/components/chat/composer/serialization.ts

import {
  $convertToMarkdownString,
  HEADING,
  QUOTE,
  TEXT_FORMAT_TRANSFORMERS,
  TextMatchTransformer,
  Transformer,
} from '@lexical/markdown';
import {
  $createTextNode,$isTextNode,
  LexicalEditor,
  LexicalNode,
  TextNode,
} from 'lexical';
import { $createLinkNode,$isLinkNode, LinkNode } from '@lexical/link';

/**
 * Helper to safely extract and replace matched inline markdown tokens with formatted TextNodes.
 * Preserves leading text and handles Arabic/English without boundary corruption.
 */
function replaceWithFormattedText(
  textNode: TextNode,
  rawTarget: string,
  innerText: string,
  format: 'bold' | 'italic' | 'code' | 'strikethrough' | Array<'bold' | 'italic'>
): void {
  const fullText = textNode.getTextContent();
  const matchIndex = fullText.lastIndexOf(rawTarget);
  if (matchIndex === -1) return;

  const beforeText = fullText.slice(0, matchIndex);
  const formattedNode = $createTextNode(innerText);

  if (Array.isArray(format)) {
    format.forEach((f) => formattedNode.toggleFormat(f));
  } else {
    formattedNode.toggleFormat(format);
  }

  if (matchIndex === 0) {
    textNode.replace(formattedNode);
  } else {
    textNode.setTextContent(beforeText);
    textNode.insertAfter(formattedNode);
  }

  formattedNode.select();
}

/**
 * Bold & Italic combined match: ***text***
 */
export const BOLD_ITALIC_STAR_MATCH: TextMatchTransformer = {
  dependencies: [],
  type: 'text-match',
  trigger: '*',
  regExp: /\*\*\*([^*]+)\*\*\*$/,
  replace: (textNode, match) => {
    const innerText = match[1];
    const rawTarget = `***${innerText}***`;
    replaceWithFormattedText(textNode, rawTarget, innerText, ['bold', 'italic']);
  },
};

/**
 * Bold text match: **text**
 */
export const BOLD_STAR_MATCH: TextMatchTransformer = {
  dependencies: [],
  type: 'text-match',
  trigger: '*',
  regExp: /(?:^|[^*])\*\*([^*]+)\*\*$/,
  replace: (textNode, match) => {
    const innerText = match[1];
    const rawTarget = `**${innerText}**`;
    replaceWithFormattedText(textNode, rawTarget, innerText, 'bold');
  },
};

/**
 * Italic text match: *text* (guarded against preceding asterisks)
 */
export const ITALIC_STAR_MATCH: TextMatchTransformer = {
  dependencies: [],
  type: 'text-match',
  trigger: '*',
  regExp: /(?:^|[^*])\*([^*]+)\*$/,
  replace: (textNode, match) => {
    const innerText = match[1];
    const rawTarget = `*${innerText}*`;
    replaceWithFormattedText(textNode, rawTarget, innerText, 'italic');
  },
};

/**
 * Inline code match: `code`
 */
export const INLINE_CODE_MATCH: TextMatchTransformer = {
  dependencies: [],
  type: 'text-match',
  trigger: '`',
  regExp: /(?:^|[^`])`([^`]+)`$/,
  replace: (textNode, match) => {
    const innerText = match[1];
    const rawTarget = `\`${innerText}\``;
    replaceWithFormattedText(textNode, rawTarget, innerText, 'code');
  },
};

/**
 * Strikethrough match: ~~text~~
 */
export const STRIKETHROUGH_MATCH: TextMatchTransformer = {
  dependencies: [],
  type: 'text-match',
  trigger: '~',
  regExp: /(?:^|[^~])~~([^~]+)~~$/,
  replace: (textNode, match) => {
    const innerText = match[1];
    const rawTarget = `~~${innerText}~~`;
    replaceWithFormattedText(textNode, rawTarget, innerText, 'strikethrough');
  },
};

/**
 * Hyperlink match: [label](url)
 * Converts immediately upon typing the closing parenthesis ')'
 */
export const LINK_MATCH: TextMatchTransformer = {
  dependencies: [LinkNode],
  type: 'text-match',
  trigger: ')',
  regExp: /\[([^\]]+)\]\(([^)]+)\)$/,
  replace: (textNode, match) => {
    const label = match[1];
    const url = match[2];
    const rawTarget = `[${label}](${url})`;

    const fullText = textNode.getTextContent();
    const matchIndex = fullText.lastIndexOf(rawTarget);
    if (matchIndex === -1) return;

    const beforeText = fullText.slice(0, matchIndex);
    const linkNode = $createLinkNode(url);
    linkNode.append($createTextNode(label));

    // Append trailing space to allow fluid continuous writing outside the link
    const trailingSpace = $createTextNode(' ');

    if (matchIndex === 0) {
      textNode.replace(linkNode);
      linkNode.insertAfter(trailingSpace);
    } else {
      textNode.setTextContent(beforeText);
      textNode.insertAfter(linkNode);
      linkNode.insertAfter(trailingSpace);
    }

    trailingSpace.select(1, 1);
  },
  export: (node: LexicalNode) => {
    if ($isLinkNode(node)) {
      const label = node.getTextContent();
      const url = node.getURL();
      return `[${label}](${url})`;
    }
    return null;
  },
};

/**
 * Unified transformers supporting both block elements, interactive typing matches,
 * and bi-directional serialization.
 */
export const COMPOSER_TRANSFORMERS: Array<Transformer> = [
  HEADING,
  QUOTE,
  BOLD_ITALIC_STAR_MATCH,
  BOLD_STAR_MATCH,
  ITALIC_STAR_MATCH,
  INLINE_CODE_MATCH,
  STRIKETHROUGH_MATCH,
  LINK_MATCH,
  ...TEXT_FORMAT_TRANSFORMERS,
];

/**
 * Serializes the editor state into clean markdown for backend and LLM ingestion.
 */
export function extractCleanText(editor: LexicalEditor): string {
  let markdownText = '';
  editor.getEditorState().read(() => {
    markdownText = $convertToMarkdownString(COMPOSER_TRANSFORMERS);
  });
  return markdownText.trim();
}
