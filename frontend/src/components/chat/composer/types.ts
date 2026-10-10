// Path: frontend/src/components/chat/composer/types.ts

export interface LexicalEditorHandle {
  clear: () => void;
  focus: () => void;
  getText: () => string;
  setText: (text: string) => void;
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
