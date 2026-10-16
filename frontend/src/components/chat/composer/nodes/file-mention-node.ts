// frontend/src/components/chat/composer/nodes/file-mention-node.ts
/**
 * FileMentionNode — a Lexical TextNode subclass that represents an inline
 * clickable reference to a workspace file inside the composer editor.
 *
 * Renders as a blue, underlined, clickable span. Clicking dispatches a
 * `peldrun:open-file-in-workspace` event handled by ChatContainer, which
 * opens the file in the workspace panel.
 *
 * The node stores the canonical workspace-relative path as a node attribute
 * so downstream consumers (composer submit handler, backend injection) can
 * rely on the correct path rather than a lossy basename extraction.
 */

import {
  $applyNodeReplacement,
  DOMConversionMap,
  EditorConfig,
  LexicalNode,
  NodeKey,
  SerializedTextNode,
  Spread,
  TextNode,
} from "lexical";

export type SerializedFileMentionNode = Spread<
  {
    filePath: string;
  },
  SerializedTextNode
>;

export class FileMentionNode extends TextNode {
  __filePath: string;

  static getType(): string {
    return "file-mention";
  }

  static clone(node: FileMentionNode): FileMentionNode {
    return new FileMentionNode(node.__text, node.__filePath, node.__key);
  }

  constructor(text: string, filePath: string, key?: NodeKey) {
    super(text, key);
    this.__filePath = filePath;
  }

  /** Returns the canonical workspace-relative path of the referenced file. */
  getFilePath(): string {
    return this.getLatest().__filePath;
  }

  /** Human-readable display text (without the @[...] wrapper). */
  getDisplayName(): string {
    const text = this.getTextContent();
    const m = text.match(/^@\[([^\]]+)\]$/);
    return m ? m[1] : text;
  }

  createDOM(config: EditorConfig): HTMLElement {
    const dom = super.createDOM(config);
    dom.classList.add("peldrun-file-mention");
    dom.dataset.filePath = this.__filePath;
    dom.dataset.fileName = this.getDisplayName();
    dom.setAttribute(
      "title",
      `Click to open "${this.__filePath}" in the workspace panel`
    );
    // Inline styles keep the node styled regardless of Tailwind purge.
    // This DOM is created programmatically and would not survive Tailwind's
    // static analysis pass, so utility classes must not be relied upon.
    dom.style.color = "#2563eb";
    dom.style.textDecoration = "underline";
    dom.style.textUnderlineOffset = "2px";
    dom.style.cursor = "pointer";
    dom.style.fontWeight = "500";
    return dom;
  }

  updateDOM(
    prevNode: this,
    dom: HTMLElement,
    config: EditorConfig
  ): boolean {
    const updated = super.updateDOM(prevNode, dom, config);
    if (prevNode.getFilePath() !== this.getFilePath()) {
      const newPath = this.getFilePath();
      dom.dataset.filePath = newPath;
      dom.setAttribute(
        "title",
        `Click to open "${newPath}" in the workspace panel`
      );
    }
    return updated;
  }

  static importDOM(): DOMConversionMap | null {
    return null;
  }

  static importJSON(serialized: SerializedFileMentionNode): FileMentionNode {
    return $createFileMentionNode(serialized.text, serialized.filePath);
  }

  exportJSON(): SerializedFileMentionNode {
    return {
      ...super.exportJSON(),
      filePath: this.__filePath,
      type: "file-mention",
      version: 1,
    };
  }

  /** Treat the mention as an atomic unit for cursor / deletion purposes. */
  isTextEntity(): true {
    return true;
  }

  /** Prevent accidental in-place editing of the mention text. */
  canInsertTextBefore(): false {
    return false;
  }

  canInsertTextAfter(): false {
    return false;
  }
}

export function $createFileMentionNode(
  text: string,
  filePath: string
): FileMentionNode {
  return $applyNodeReplacement(new FileMentionNode(text, filePath));
}

export function $isFileMentionNode(
  node: LexicalNode | null | undefined
): node is FileMentionNode {
  return node instanceof FileMentionNode;
}

/**
 * Extract the display-worthy portion of a canonical workspace-relative path.
 * Strips the `storage/chats/<cid>/files/` prefix (or the `workspace/`
 * prefix) so the mention shows `notes/todo.md` rather than the full
 * internal storage path.
 */
export function getDisplayPathFromRelPath(relPath: string): string {
  if (!relPath) return relPath;
  let clean = relPath.replace(/\\/g, "/");
  if (clean.includes("/files/")) {
    clean = clean.split("/files/").pop() || clean;
  } else if (clean.startsWith("workspace/")) {
    clean = clean.slice("workspace/".length);
  }
  return clean;
}


/**
 * Inject a one-time stylesheet that prepends a small paperclip glyph to
 * every `.peldrun-file-mention` span. This gives the mention a visible
 * icon without requiring an ElementNode wrapper (which would change
 * the node's decoration semantics and complicate the editor tree).
 *
 * The stylesheet is only injected in the browser, guarded by a unique
 * ID so it never duplicates across HMR cycles or multiple component
 * mounts.
 */
if (
  typeof document !== "undefined" &&
  !document.getElementById("peldrun-file-mention-styles")
) {
  const styleEl = document.createElement("style");
  styleEl.id = "peldrun-file-mention-styles";
  styleEl.textContent = `
    .peldrun-file-mention {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      padding: 0 4px;
      border-radius: 4px;
      background: rgba(37, 99, 235, 0.08);
      transition: background-color 120ms ease;
    }
    .peldrun-file-mention:hover {
      background: rgba(37, 99, 235, 0.15);
    }
    .peldrun-file-mention::before {
      content: "";
      display: inline-block;
      width: 12px;
      height: 12px;
      flex-shrink: 0;
      background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%232563eb' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'/><polyline points='14 2 14 8 20 8'/></svg>");
      background-size: contain;
      background-repeat: no-repeat;
      background-position: center;
    }
  `;
  document.head.appendChild(styleEl);
}
