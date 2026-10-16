// frontend/src/components/chat/composer/lexical-editor.tsx

'use client';

import React, {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
} from 'react';
import { LexicalComposer } from '@lexical/react/LexicalComposer';
import { RichTextPlugin } from '@lexical/react/LexicalRichTextPlugin';
import { ContentEditable } from '@lexical/react/LexicalContentEditable';
import { HistoryPlugin } from '@lexical/react/LexicalHistoryPlugin';
import { OnChangePlugin } from '@lexical/react/LexicalOnChangePlugin';
import { LexicalErrorBoundary } from '@lexical/react/LexicalErrorBoundary';
import { MarkdownShortcutPlugin } from '@lexical/react/LexicalMarkdownShortcutPlugin';
import { LinkPlugin } from '@lexical/react/LexicalLinkPlugin';
import { AutoLinkPlugin, LinkMatcher } from '@lexical/react/LexicalAutoLinkPlugin';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import { HeadingNode, QuoteNode } from '@lexical/rich-text';
import { AutoLinkNode, LinkNode } from '@lexical/link';
import {
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  EditorState,
  ElementNode,
  LexicalEditor,
  LexicalNode,
} from 'lexical';
import { $convertFromMarkdownString } from '@lexical/markdown';

import { SubmitCommandPlugin } from './submit-command-plugin';
import { TypeaheadMenuPlugin } from './plugins/typeahead-menu-plugin';
import { MarkdownClipboardPlugin } from './plugins/markdown-clipboard-plugin';
import { FloatingToolbarPlugin } from './plugins/floating-toolbar-plugin';
import { COMPOSER_TRANSFORMERS, extractCleanText } from './serialization';
import { LexicalEditorHandle, LexicalEditorProps } from './types';
import {
  $isFileMentionNode,
  FileMentionNode,
} from './nodes/file-mention-node';

const URL_MATCHER: LinkMatcher = (text: string) => {
  const match = /(?:https?:\/\/|www\.)[^\s<]+[^\s`!()\[\]{};:'".,<>?«»“”‘’]/i.exec(text);
  if (!match) return null;
  const fullUrl = match[0];
  return {
    index: match.index,
    length: fullUrl.length,
    text: fullUrl,
    url: fullUrl.startsWith('http') ? fullUrl : `https://${fullUrl}`,
  };
};

const MATCHERS: LinkMatcher[] = [URL_MATCHER];

interface EditorRefPluginProps {
  onEditorReady: (editor: LexicalEditor) => void;
  autoFocus?: boolean;
}

function EditorRefPlugin({ onEditorReady, autoFocus }: EditorRefPluginProps) {
  const [editor] = useLexicalComposerContext();

  useEffect(() => {
    onEditorReady(editor);
    if (autoFocus) {
      editor.focus();
    }
  }, [editor, onEditorReady, autoFocus]);

  return null;
}

interface DynamicEditablePluginProps {
  disabled?: boolean;
}

function DynamicEditablePlugin({ disabled }: DynamicEditablePluginProps) {
  const [editor] = useLexicalComposerContext();

  useEffect(() => {
    editor.setEditable(!disabled);
  }, [editor, disabled]);

  return null;
}

export const LexicalEditorComponent = forwardRef<
  LexicalEditorHandle,
  LexicalEditorProps
>(function LexicalEditorComponent(
  {
    placeholder = 'Type your prompt here...',
    disabled = false,
    onSubmit,
    onChange,
    className = '',
    contentClassName = 'min-h-[44px] max-h-[220px] px-4 py-3',
    placeholderClassName = 'px-4 py-3',
    autoFocus = false,
    onClearEditor,
    chatId,
  },
  ref
) {
  const editorInstanceRef = useRef<LexicalEditor | null>(null);

  const handleEditorReady = useCallback((editor: LexicalEditor) => {
    editorInstanceRef.current = editor;
  }, []);

  useImperativeHandle(
    ref,
    () => ({
      clear: () => {
        if (editorInstanceRef.current) {
          editorInstanceRef.current.update(() => {
            const root = $getRoot();
            root.clear();
            const paragraph = $createParagraphNode();
            root.append(paragraph);
          });
        }
      },
      focus: () => {
        if (editorInstanceRef.current) {
          editorInstanceRef.current.focus();
        }
      },
      getText: () => {
        if (editorInstanceRef.current) {
          return extractCleanText(editorInstanceRef.current);
        }
        return '';
      },
      setText: (newText: string) => {
        if (editorInstanceRef.current) {
          editorInstanceRef.current.update(() => {
            const root = $getRoot();
            root.clear();
            if (!newText || !newText.trim()) {
              const paragraph = $createParagraphNode();
              root.append(paragraph);
              return;
            }

            try {
              $convertFromMarkdownString(newText, COMPOSER_TRANSFORMERS);
            } catch {
              const paragraph = $createParagraphNode();
              paragraph.append($createTextNode(newText));
              root.append(paragraph);
            }

            if (root.isEmpty()) {
              root.append($createParagraphNode());
            }
          });
        }
      },
      getFileMentions: () => {
        const editor = editorInstanceRef.current;
        if (!editor) return [];
        const mentions: Array<{ display: string; path: string }> = [];
        editor.getEditorState().read(() => {
          const root = $getRoot();
          const walk = (node: LexicalNode) => {
            if ($isFileMentionNode(node)) {
              mentions.push({
                display: node.getTextContent(),
                path: node.getFilePath(),
              });
            }
            const anyNode = node as unknown as ElementNode;
            if (typeof anyNode.getChildren === 'function') {
              anyNode.getChildren().forEach(walk);
            }
          };
          walk(root);
        });
        return mentions;
      },
    }),
    []
  );

  const handleSubmit = useCallback(() => {
    onSubmit();
  }, [onSubmit]);

  const handleChange = useCallback(
    (_editorState: EditorState, editor: LexicalEditor) => {
      if (!onChange) return;
      const currentMarkdown = extractCleanText(editor);
      onChange(currentMarkdown);
    },
    [onChange]
  );

  const initialConfig = useMemo(
    () => ({
      namespace: 'PeldrunComposer',
      nodes: [
        HeadingNode,
        QuoteNode,
        LinkNode,
        AutoLinkNode,
        FileMentionNode,
      ],
      editable: !disabled,
      theme: {
        paragraph: 'mb-0 leading-normal text-start text-foreground',
        heading: {
          h1: 'text-2xl font-bold mb-1 mt-1 text-start text-foreground',
          h2: 'text-xl font-bold mb-1 mt-1 text-start text-foreground',
          h3: 'text-lg font-semibold mb-1 mt-0.5 text-start text-foreground',
        },
        quote: 'border-s-2 border-border ps-3 text-muted-foreground italic my-1',
        link: 'peldrun-editor-link',
        text: {
          bold: 'font-bold text-foreground',
          italic: 'italic text-foreground',
          code: 'font-mono text-xs bg-muted/60 px-1 py-0.5 rounded text-foreground',
        },
      },
      onError: (error: Error) => {
        console.error('[LexicalComposer Error]:', error);
      },
    }),
    [disabled]
  );

  return (
    <div
      className="relative w-full text-start peldrun-composer-root"
      onClick={(event) => {
        // Delegate clicks on file mention spans to the workspace panel by
        // emitting a custom event. Using event delegation on the wrapper
        // (rather than per-node listeners) keeps the handler alive across
        // Lexical DOM mutations.
        const target = event.target as HTMLElement | null;
        if (!target) return;
        const mentionEl = target.closest(
          '.peldrun-file-mention'
        ) as HTMLElement | null;
        if (!mentionEl) return;

        const filePath = mentionEl.dataset.filePath;
        if (!filePath) return;

        event.preventDefault();
        event.stopPropagation();

        if (typeof window !== 'undefined') {
          window.dispatchEvent(
            new CustomEvent('peldrun:open-file-in-workspace', {
              detail: { path: filePath, source: 'composer' },
            })
          );
        }
      }}
    >
      <LexicalComposer initialConfig={initialConfig}>
        <EditorRefPlugin
          onEditorReady={handleEditorReady}
          autoFocus={autoFocus}
        />
        <DynamicEditablePlugin disabled={disabled} />
        <div className="relative text-start">
          <RichTextPlugin
            contentEditable={
              <ContentEditable
                className={`peldrun-id-input-bar w-full resize-none overflow-y-auto bg-transparent text-start text-sm text-foreground focus:outline-none disabled:cursor-not-allowed disabled:opacity-50 font-sans ${contentClassName} ${className}`}
              />
            }
            placeholder={
              <div
                className={`pointer-events-none absolute inset-x-0 top-0 select-none text-start text-sm text-muted-foreground/50 font-sans w-full ${placeholderClassName}`}
              >
                <span>{placeholder}</span>
              </div>
            }
            ErrorBoundary={LexicalErrorBoundary}
          />
        </div>
        <HistoryPlugin />
        <OnChangePlugin onChange={handleChange} />
        <MarkdownShortcutPlugin transformers={COMPOSER_TRANSFORMERS} />
        <LinkPlugin />
        <AutoLinkPlugin matchers={MATCHERS} />
        <MarkdownClipboardPlugin />
        <FloatingToolbarPlugin />
        {/* Typeahead is registered before Submit to ensure menu capture on Enter */}
        <TypeaheadMenuPlugin onClearEditor={onClearEditor} chatId={chatId} />
        <SubmitCommandPlugin onSubmit={handleSubmit} disabled={disabled} />
      </LexicalComposer>
    </div>
  );
});

export default LexicalEditorComponent;
