// Path: frontend/src/components/chat/composer/plugins/markdown-clipboard-plugin.tsx

'use client';

import { useEffect } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import { $convertFromMarkdownString } from '@lexical/markdown';
import {
  $getRoot,
  $getSelection,
  $isRangeSelection,
  COMMAND_PRIORITY_CRITICAL,
  COPY_COMMAND,
  PASTE_COMMAND,
} from 'lexical';
import { COMPOSER_TRANSFORMERS, extractCleanText } from '../serialization';

export function MarkdownClipboardPlugin() {
  const [editor] = useLexicalComposerContext();

  useEffect(() => {
    // Intercept Paste to convert Markdown text into rich nodes cleanly
    const unregisterPaste = editor.registerCommand<ClipboardEvent | InputEvent | KeyboardEvent | null>(
      PASTE_COMMAND,
      (event) => {
        if (!event || !('clipboardData' in event) || !event.clipboardData) {
          return false;
        }

        const text = event.clipboardData.getData('text/plain');
        if (!text) return false;

        const hasMarkdown = /(?:^|\n)(?:#{1,6}\s|>|\*\*|`)/.test(text);
        if (!hasMarkdown) {
          return false;
        }

        event.preventDefault();
        editor.update(() => {
          const root = $getRoot();
          const isEditorEmpty = root.getTextContent().trim() === '';

          if (isEditorEmpty) {
            root.clear();
            $convertFromMarkdownString(text, COMPOSER_TRANSFORMERS);
            root.selectEnd();
          } else {
            const selection = $getSelection();
            if ($isRangeSelection(selection)) {
              root.clear();
              $convertFromMarkdownString(text, COMPOSER_TRANSFORMERS);
              root.selectEnd();
            }
          }
        });
        return true;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    // Intercept Copy to ensure plain text clipboard contains valid Markdown
    const unregisterCopy = editor.registerCommand<ClipboardEvent | KeyboardEvent | null>(
      COPY_COMMAND,
      (event) => {
        if (!event || !('clipboardData' in event) || !event.clipboardData) {
          return false;
        }

        const selection = $getSelection();
        if (!$isRangeSelection(selection)) return false;

        const markdownText = extractCleanText(editor);
        if (markdownText) {
          event.clipboardData.setData('text/plain', markdownText);
          event.preventDefault();
          return true;
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    return () => {
      unregisterPaste();
      unregisterCopy();
    };
  }, [editor]);

  return null;
}
