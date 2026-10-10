// Path: frontend/src/components/chat/composer/submit-command-plugin.tsx

'use client';

import { useEffect } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import {
  $createParagraphNode,
  $getSelection,
  $isElementNode,
  $isRangeSelection,
  $isTextNode,
  COMMAND_PRIORITY_LOW,
  KEY_ENTER_COMMAND,
  LexicalNode,
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
    // Registered at LOW priority so Typeahead and auto-complete plugins intercept Enter first
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

        // Shift + Enter: Safely splits line, preserving siblings and breaking into clean ParagraphNode
        if (event.shiftKey) {
          event.preventDefault();
          editor.update(() => {
            const selection = $getSelection();
            if (!$isRangeSelection(selection)) {
              return;
            }

            if (!selection.isCollapsed()) {
              selection.removeText();
            }

            const anchor = selection.anchor;
            const anchorNode = anchor.getNode();
            const topElement =
              anchorNode.getKey() === 'root'
                ? anchorNode
                : anchorNode.getTopLevelElementOrThrow();

            if (topElement.getTextContent().trim() === '') {
              const emptyParagraph = $createParagraphNode();
              topElement.replace(emptyParagraph);
              emptyParagraph.select(0, 0);
              return;
            }

            const afterNodes: LexicalNode[] = [];

            if ($isTextNode(anchorNode)) {
              const textLength = anchorNode.getTextContentSize();
              const offset = anchor.offset;

              if (offset > 0 && offset < textLength) {
                const splitNodes = anchorNode.splitText(offset);
                afterNodes.push(splitNodes[1]);
              } else if (offset === 0) {
                afterNodes.push(anchorNode);
              }

              let nextSibling: LexicalNode | null = anchorNode.getNextSibling();
              while (nextSibling) {
                const next: LexicalNode | null = nextSibling.getNextSibling();
                afterNodes.push(nextSibling);
                nextSibling = next;
              }
            } else if ($isElementNode(anchorNode)) {
              const children = anchorNode.getChildren();
              for (let i = anchor.offset; i < children.length; i++) {
                afterNodes.push(children[i]);
              }
            }

            const newParagraph = $createParagraphNode();
            for (const node of afterNodes) {
              newParagraph.append(node);
            }

            topElement.insertAfter(newParagraph);
            newParagraph.select(0, 0);
          });
          return true;
        }

        // Plain Enter triggers prompt submission only when no higher-priority plugin handled it
        event.preventDefault();
        if (!disabled) {
          onSubmit();
        }
        return true;
      },
      COMMAND_PRIORITY_LOW
    );
  }, [editor, onSubmit, disabled]);

  return null;
}
