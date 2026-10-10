// Path: frontend/src/components/chat/composer/serialization.ts

import {
  $convertToMarkdownString,
  HEADING,
  QUOTE,
  TEXT_FORMAT_TRANSFORMERS,
  Transformer,
} from '@lexical/markdown';
import { LexicalEditor } from 'lexical';

/**
 * Tailored markdown transformers matching the exact nodes registered in the editor config.
 * Avoids loading list/code-block dependencies while supporting headings, quotes, and inline styles.
 */
export const COMPOSER_TRANSFORMERS: Array<Transformer> = [
  HEADING,
  QUOTE,
  ...TEXT_FORMAT_TRANSFORMERS,
];

/**
 * Serializes the rich editor state into standard, clean Markdown.
 * Converts HeadingNodes (h1, h2, h3) back to markdown syntax (e.g. '## heading')
 * so the backend and LLM receive valid markdown without showing raw symbols to the user.
 */
export function extractCleanText(editor: LexicalEditor): string {
  let markdownText = '';
  editor.getEditorState().read(() => {
    markdownText = $convertToMarkdownString(COMPOSER_TRANSFORMERS);
  });
  return markdownText.trim();
}
