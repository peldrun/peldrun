// frontend/src/components/chat/composer/types.ts

export interface FileMentionInfo {
  /** Display text of the mention, e.g. "@[todo.md]". */
  display: string;
  /** Canonical workspace-relative path of the referenced file. */
  path: string;
}

export interface LexicalEditorHandle {
  clear: () => void;
  focus: () => void;
  getText: () => string;
  setText: (text: string) => void;
  /**
   * Returns the list of file mention nodes currently present in the editor,
   * in document order, together with their canonical workspace-relative
   * paths. This is the authoritative source used by the composer on submit.
   */
  getFileMentions: () => FileMentionInfo[];
}

export interface LexicalEditorProps {
  placeholder?: string;
  disabled?: boolean;
  onSubmit: () => void;
  onChange?: (text: string) => void;
  className?: string;
  contentClassName?: string;
  placeholderClassName?: string;
  autoFocus?: boolean;
  onClearEditor?: () => void;
  chatId?: string;
}
