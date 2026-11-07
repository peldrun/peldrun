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
  /**
   * When ``false``, the composer's typeahead menu (``/`` slash
   * commands and ``@`` mentions) is suppressed. The characters remain
   * typeable as plain text.
   *
   * Defaults to ``true``. The chat composer sets this to ``false`` in
   * chat mode, where neither skill invocation nor @-mention agent
   * switching are supported.
   */
  typeaheadEnabled?: boolean;
  /**
   * Invoked once per image file pasted into the editor.
   *
   * When omitted, the ImagePastePlugin is not mounted and image pastes
   * fall through to the browser's default handling (inline base64 in
   * the editor state — generally undesirable). Composer hosts should
   * always supply this callback to route images through the temporary
   * artifact upload pipeline.
   *
   * The plugin does not await the returned promise; the host owns the
   * upload lifecycle and any per-file progress or error UI.
   */
  onImagePasted?: (file: File) => void | Promise<void>;
}
