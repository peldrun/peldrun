// Path: frontend/src/components/chat/composer/submit-command-plugin.tsx

'use client';

import { useEffect } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import {
  $createParagraphNode,
  $createTextNode,
  $getSelection,
  $isRangeSelection,
  $isTextNode,
  COMMAND_PRIORITY_CRITICAL,
  KEY_ENTER_COMMAND,
} from 'lexical';

interface SubmitCommandPluginProps {
  onSubmit: () => void;
  disabled?: boolean;
}

export function SubmitCommandPlugin({
  onSubmit,
  disabled,
}: SubmitCommandPluginProps) {
  const [editor] = useLexicalComposerContext();

  useEffect(() => {
    return editor.registerCommand(
      KEY_ENTER_COMMAND,
      (event: KeyboardEvent | null) => {
        if (!event) {
          return false;
        }

        // Prevent submission during IME composition
        if (event.isComposing || event.keyCode === 229) {
          return false;
        }

        // Shift + Enter: Explicitly breaks out of Heading/Quote blocks into a regular ParagraphNode
        if (event.shiftKey) {
          event.preventDefault();
          editor.update(() => {
            const selection = $getSelection();
            if (!$isRangeSelection(selection)) {
              return;
            }

            const anchor = selection.anchor;
            const anchorNode = anchor.getNode();
            const topElement =
              anchorNode.getKey() === 'root'
                ? anchorNode
                : anchorNode.getTopLevelElementOrThrow();

            let afterText = '';
            if ($isTextNode(anchorNode)) {
              const fullText = anchorNode.getTextContent();
              const offset = anchor.offset;
              anchorNode.setTextContent(fullText.slice(0, offset));
              afterText = fullText.slice(offset);
            }

            // Always create a clean ParagraphNode so line 2 is regular text and never a heading
            const newParagraph = $createParagraphNode();
            if (afterText.length > 0) {
              newParagraph.append($createTextNode(afterText));
            }

            topElement.insertAfter(newParagraph);
            newParagraph.select(0, 0);
          });
          return true;
        }

        // Plain Enter triggers prompt submission
        event.preventDefault();
        if (!disabled) {
          onSubmit();
        }
        return true;
      },
      COMMAND_PRIORITY_CRITICAL
    );
  }, [editor, onSubmit, disabled]);

  return null;
}
