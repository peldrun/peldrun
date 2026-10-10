// Path: frontend/src/components/chat/composer/lexical-editor.tsx

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
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import { HeadingNode, QuoteNode } from '@lexical/rich-text';
import {
  $createParagraphNode,
  $createTextNode,$getRoot,
  EditorState,
  LexicalEditor,
} from 'lexical';

import { SubmitCommandPlugin } from './submit-command-plugin';
import { TypeaheadMenuPlugin } from './plugins/typeahead-menu-plugin';
import { MarkdownClipboardPlugin } from './plugins/markdown-clipboard-plugin';
import { FloatingToolbarPlugin } from './plugins/floating-toolbar-plugin';
import { COMPOSER_TRANSFORMERS, extractCleanText } from './serialization';
import { LexicalEditorHandle, LexicalEditorProps } from './types';

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
            const paragraph = $createParagraphNode();
            if (newText) {
              paragraph.append($createTextNode(newText));
            }
            root.append(paragraph);
          });
        }
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
      const text = extractCleanText(editor);
      onChange(text);
    },
    [onChange]
  );

  const initialConfig = useMemo(
    () => ({
      namespace: 'PeldrunComposer',
      nodes: [HeadingNode, QuoteNode],
      editable: !disabled,
      theme: {
        paragraph: 'mb-0 leading-normal text-start text-foreground',
        heading: {
          h1: 'text-2xl font-bold mb-1 mt-1 text-start text-foreground',
          h2: 'text-xl font-bold mb-1 mt-1 text-start text-foreground',
          h3: 'text-lg font-semibold mb-1 mt-0.5 text-start text-foreground',
        },
        quote: 'border-s-2 border-border ps-3 text-muted-foreground italic my-1',
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
    <div className="relative w-full text-start">
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
                className={`w-full resize-none overflow-y-auto bg-transparent text-start text-sm text-foreground focus:outline-none disabled:cursor-not-allowed disabled:opacity-50 font-sans ${contentClassName} ${className}`}
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
        <MarkdownClipboardPlugin />
        <FloatingToolbarPlugin />
        <SubmitCommandPlugin onSubmit={handleSubmit} disabled={disabled} />
        <TypeaheadMenuPlugin onClearEditor={onClearEditor} chatId={chatId} />
      </LexicalComposer>
    </div>
  );
});

export default LexicalEditorComponent;
