// Path: frontend/src/components/chat/composer/plugins/typeahead-menu-plugin.tsx

'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import {
  $createParagraphNode,$getRoot,
  $getSelection,$isElementNode,
  $isRangeSelection,$isTextNode,
  COMMAND_PRIORITY_CRITICAL,
  KEY_ARROW_DOWN_COMMAND,
  KEY_ARROW_UP_COMMAND,
  KEY_ENTER_COMMAND,
  KEY_ESCAPE_COMMAND,
  KEY_TAB_COMMAND,
  LexicalEditor,
} from 'lexical';
import { Bot, FileText, MessageSquare, Sparkles, Terminal, Wrench } from 'lucide-react';
import { commandRegistry, MentionItem, SlashCommandItem } from '../registry/command-registry';

interface TypeaheadMenuPluginProps {
  onClearEditor?: () => void;
  chatId?: string;
}

function getTextBeforeCaret(editor: LexicalEditor): string {
  let textBeforeCaret = '';
  editor.getEditorState().read(() => {
    const selection = $getSelection();
    if (!$isRangeSelection(selection) || !selection.isCollapsed()) {
      return;
    }
    const anchor = selection.anchor;
    const anchorNode = anchor.getNode();

    if ($isTextNode(anchorNode)) {       textBeforeCaret = anchorNode.getTextContent().slice(0, anchor.offset);     } else if ($isElementNode(anchorNode)) {
      const children = anchorNode.getChildren();
      let accumulated = '';
      const childIndex = anchor.offset;
      for (let i = 0; i < childIndex && i < children.length; i++) {
        accumulated += children[i].getTextContent();
      }
      textBeforeCaret = accumulated;
    }
  });
  return textBeforeCaret;
}

export function TypeaheadMenuPlugin({ onClearEditor, chatId }: TypeaheadMenuPluginProps) {
  const [editor] = useLexicalComposerContext();
  const [isOpen, setIsOpen] = useState(false);
  const [triggerType, setTriggerType] = useState<'slash' | 'mention' | null>(null);
  const [queryString, setQueryString] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [dynamicMentions, setDynamicMentions] = useState<MentionItem[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [, setRegistryTick] = useState(0);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    return commandRegistry.subscribe(() => {
      setRegistryTick((t) => t + 1);
    });
  }, []);

  const slashResults = useMemo(() => {
    if (triggerType !== 'slash') return [];
    return commandRegistry.getSlashCommands(queryString);
  }, [triggerType, queryString]);

  useEffect(() => {
    let active = true;
    if (triggerType === 'mention') {
      setIsLoading(true);
      commandRegistry.getMentionItems(queryString, { chatId }).then((items) => {
        if (active) {
          setDynamicMentions(items);
          setIsLoading(false);
        }
      });
    } else {
      setDynamicMentions([]);
      setIsLoading(false);
    }
    return () => {
      active = false;
    };
  }, [triggerType, queryString, chatId]);

  const activeResultsCount = triggerType === 'slash' ? slashResults.length : dynamicMentions.length;

  useEffect(() => {
    return editor.registerUpdateListener(() => {
      const textBeforeCaret = getTextBeforeCaret(editor);

      const slashMatch = textBeforeCaret.match(/(?:^|\s)\/([a-zA-Z0-9_-]*)$/);
      if (slashMatch) {
        setTriggerType('slash');
        setQueryString(slashMatch[1]);
        setIsOpen(true);
        setSelectedIndex(0);
        return;
      }

      const mentionMatch = textBeforeCaret.match(/(?:^|\s)@([a-zA-Z0-9_.-]*)$/);
      if (mentionMatch) {
        setTriggerType('mention');
        setQueryString(mentionMatch[1]);
        setIsOpen(true);
        setSelectedIndex(0);
        return;
      }

      setIsOpen(false);
      setTriggerType(null);
    });
  }, [editor]);

  const handleExecuteSelection = useCallback(() => {
    if (!isOpen || activeResultsCount === 0) return false;

    if (triggerType === 'slash') {
      const selectedItem: SlashCommandItem | undefined = slashResults[selectedIndex];
      if (!selectedItem) return false;

      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) {
          const anchorNode = selection.anchor.getNode();
          if ($isTextNode(anchorNode)) {
            const text = anchorNode.getTextContent();
            const newText = text.replace(new RegExp(`/${queryString}$`), '');
            anchorNode.setTextContent(newText);
            anchorNode.select();
          }
        }
      });

      selectedItem.execute({
        clearEditor: () => {
          if (onClearEditor) {
            onClearEditor();
          } else {
            editor.update(() => {
              const root = $getRoot();
              root.clear();
              root.append($createParagraphNode());
            });
          }
        },
        insertText: (textToInsert: string) => {
          editor.update(() => {
            const selection = $getSelection();
            if ($isRangeSelection(selection)) {
              selection.insertText(textToInsert);
            }
          });
        },
        dispatchCustomEvent: (name, detail) => {
          if (typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent(name, { detail }));
          }
        },
      });

      setIsOpen(false);
      return true;
    }

    if (triggerType === 'mention') {
      const selectedItem: MentionItem | undefined = dynamicMentions[selectedIndex];
      if (!selectedItem) return false;

      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) {
          const anchorNode = selection.anchor.getNode();
          if ($isTextNode(anchorNode)) {
            const text = anchorNode.getTextContent();
            const newText = text.replace(new RegExp(`@${queryString}$`), selectedItem.insertText);
            anchorNode.setTextContent(newText);
            anchorNode.select();
          }
        }
      });

      setIsOpen(false);
      return true;
    }

    return false;
  }, [
    isOpen,
    activeResultsCount,
    triggerType,
    slashResults,
    dynamicMentions,
    selectedIndex,
    editor,
    queryString,
    onClearEditor,
  ]);

  useEffect(() => {
    const unregisterEnter = editor.registerCommand(
      KEY_ENTER_COMMAND,
      (event) => {
        if (isOpen && activeResultsCount > 0) {
          if (event) event.preventDefault();
          return handleExecuteSelection();
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    const unregisterTab = editor.registerCommand(
      KEY_TAB_COMMAND,
      (event) => {
        if (isOpen && activeResultsCount > 0) {
          if (event) event.preventDefault();
          return handleExecuteSelection();
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    const unregisterDown = editor.registerCommand(
      KEY_ARROW_DOWN_COMMAND,
      (event) => {
        if (isOpen && activeResultsCount > 0) {
          if (event) event.preventDefault();
          setSelectedIndex((prev) => (prev + 1) % activeResultsCount);
          return true;
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    const unregisterUp = editor.registerCommand(
      KEY_ARROW_UP_COMMAND,
      (event) => {
        if (isOpen && activeResultsCount > 0) {
          if (event) event.preventDefault();
          setSelectedIndex((prev) => (prev - 1 + activeResultsCount) % activeResultsCount);
          return true;
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    const unregisterEscape = editor.registerCommand(
      KEY_ESCAPE_COMMAND,
      (event) => {
        if (isOpen) {
          if (event) event.preventDefault();
          setIsOpen(false);
          return true;
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    return () => {
      unregisterEnter();
      unregisterTab();
      unregisterDown();
      unregisterUp();
      unregisterEscape();
    };
  }, [editor, isOpen, activeResultsCount, handleExecuteSelection]);

  if (!isOpen) return null;

  return (
    <div
      ref={menuRef}
      className="absolute bottom-full mb-2 start-0 z-[9999] w-80 max-h-64 overflow-y-auto rounded-xl border border-border/80 bg-popover/95 p-1 text-popover-foreground shadow-2xl backdrop-blur-md animate-in fade-in zoom-in-95 duration-150 font-sans"
    >
      <div className="px-2 py-1 text-[10px] font-semibold tracking-wider text-muted-foreground uppercase flex items-center justify-between border-b border-border/40 mb-1">
        <span>{triggerType === 'slash' ? 'Slash Commands' : 'Context Mentions'}</span>
        <span className="font-mono text-[9px]">Tab / Enter to select</span>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center gap-2 py-4 text-xs text-muted-foreground">
          <Sparkles size={14} className="animate-spin text-primary" />
          <span>Searching items...</span>
        </div>
      ) : activeResultsCount === 0 ? (
        <div className="py-4 text-center text-xs text-muted-foreground">
          No {triggerType === 'slash' ? 'commands' : 'chat files'} found
        </div>
      ) : (
        <div className="flex flex-col gap-0.5">
          {triggerType === 'slash' &&
            slashResults.map((item, index) => {
              const isSelected = index === selectedIndex;
              return (
                <button
                  key={item.id}
                  type="button"
                  onMouseEnter={() => setSelectedIndex(index)}
                  onClick={handleExecuteSelection}
                  className={`flex items-center gap-2.5 w-full px-2.5 py-1.5 rounded-lg text-start text-xs transition-colors cursor-pointer ${
                    isSelected
                      ? 'bg-primary text-primary-foreground font-medium'
                      : 'hover:bg-muted text-foreground'
                  }`}
                >
                  <div
                    className={`p-1 rounded-md shrink-0 ${
                      isSelected ? 'bg-primary-foreground/20' : 'bg-muted'
                    }`}
                  >
                    {item.category === 'agent' ? (
                      <Bot size={13} />
                    ) : item.category === 'tool' ? (
                      <Wrench size={13} />
                    ) : (
                      <Terminal size={13} />
                    )}
                  </div>
                  <div className="flex flex-col min-w-0 flex-1">
                    <span className="font-mono font-medium truncate">{item.label}</span>
                    <span
                      className={`text-[10px] truncate ${
                        isSelected ? 'text-primary-foreground/80' : 'text-muted-foreground'
                      }`}
                    >
                      {item.description}
                    </span>
                  </div>
                </button>
              );
            })}

          {triggerType === 'mention' &&
            dynamicMentions.map((item, index) => {
              const isSelected = index === selectedIndex;
              return (
                <button
                  key={item.id}
                  type="button"
                  onMouseEnter={() => setSelectedIndex(index)}
                  onClick={handleExecuteSelection}
                  className={`flex items-center gap-2.5 w-full px-2.5 py-1.5 rounded-lg text-start text-xs transition-colors cursor-pointer ${
                    isSelected
                      ? 'bg-primary text-primary-foreground font-medium'
                      : 'hover:bg-muted text-foreground'
                  }`}
                >
                  <div
                    className={`p-1 rounded-md shrink-0 ${
                      isSelected ? 'bg-primary-foreground/20' : 'bg-muted'
                    }`}
                  >
                    {item.category === 'agent' ? (
                      <Bot size={13} />
                    ) : item.category === 'file' ? (
                      <FileText size={13} />
                    ) : (
                      <Sparkles size={13} />
                    )}
                  </div>
                  <div className="flex flex-col min-w-0 flex-1">
                    <span className="font-mono font-medium truncate">{item.label}</span>
                    {item.description && (
                      <span
                        className={`text-[10px] truncate ${
                          isSelected ? 'text-primary-foreground/80' : 'text-muted-foreground'
                        }`}
                      >
                        {item.description}
                      </span>
                    )}
                  </div>
                </button>
              );
            })}
        </div>
      )}
    </div>
  );
}

export default TypeaheadMenuPlugin;
