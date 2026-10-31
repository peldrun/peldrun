// Path: frontend/src/components/chat/composer/plugins/markdown-clipboard-plugin.tsx

'use client';

import { useEffect } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import { $convertFromMarkdownString } from '@lexical/markdown';
import {
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isRangeSelection,
  $isTextNode,
  COMMAND_PRIORITY_CRITICAL,
  COPY_COMMAND,
  PASTE_COMMAND,
  LexicalNode,
} from 'lexical';
import { $createHeadingNode, $createQuoteNode, HeadingNode, QuoteNode } from '@lexical/rich-text';
import { $createLinkNode, $isLinkNode } from '@lexical/link';
import { COMPOSER_TRANSFORMERS, extractCleanText } from '../serialization';

/**
 * Checks if a string represents a standalone direct web URL.
 */
function isStandaloneUrl(text: string): boolean {
  return /^https?:\/\/[^\s]+$/i.test(text.trim());
}

/**
 * Serializes a single lexical node into markdown syntax representation.
 */
function serializeNodeToMarkdown(node: LexicalNode): string {
  if ($isTextNode(node)) {
    let text = node.getTextContent();
    if (!text) return '';
    if (node.hasFormat('code')) {
      text = `\`${text}\``;
    }
    if (node.hasFormat('bold')) {
      text = `**${text}**`;
    }
    if (node.hasFormat('italic')) {
      text = `*${text}*`;
    }
    if (node.hasFormat('strikethrough')) {
      text = `~~${text}~~`;
    }
    return text;
  }

  if ($isLinkNode(node)) {
    const children = node.getChildren();
    const linkText = children.map(serializeNodeToMarkdown).join('');
    const url = node.getURL();
    if (linkText === url) {
      return url;
    }
    return `[${linkText}](${url})`;
  }

  if ($isElementNode(node)) {
    const children = node.getChildren();
    const innerText = children.map(serializeNodeToMarkdown).join('');

    if (node instanceof HeadingNode) {
      const tag = node.getTag();
      const level = parseInt(tag.replace('h', ''), 10) || 1;
      const prefix = '#'.repeat(level);
      return `${prefix} ${innerText}`;
    }

    if (node instanceof QuoteNode) {
      return `> ${innerText}`;
    }

    return innerText;
  }

  return node.getTextContent();
}

/**
 * Serializes an array of extracted nodes from a selection range into valid markdown text.
 */
function serializeNodesToMarkdown(nodes: LexicalNode[]): string {
  const blocks: string[] = [];
  let inlineBuffer = '';

  for (const node of nodes) {
    if ($isTextNode(node) || $isLinkNode(node)) {
      inlineBuffer += serializeNodeToMarkdown(node);
    } else {
      if (inlineBuffer.length > 0) {
        blocks.push(inlineBuffer);
        inlineBuffer = '';
      }
      blocks.push(serializeNodeToMarkdown(node));
    }
  }

  if (inlineBuffer.length > 0) {
    blocks.push(inlineBuffer);
  }

  return blocks.join('\n\n');
}

/**
 * Parses inline markdown format syntax and raw URLs into an array of styled TextNodes and LinkNodes.
 */
function parseInlineMarkdownToNodes(text: string): LexicalNode[] {
  const nodes: LexicalNode[] = [];
  const tokenRegex =
    /(\*\*|__)(.*?)\1|(\*|_)(.*?)\3|(`)(.*?)\5|(~~)(.*?)\7|\[(.*?)\]\((.*?)\)|(https?:\/\/[^\s<]+[^\s`!()\[\]{};:'".,<>?«»“”‘’])/g;

  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = tokenRegex.exec(text)) !== null) {
    const matchIndex = match.index;
    if (matchIndex > lastIndex) {
      const plainSegment = text.slice(lastIndex, matchIndex);
      if (plainSegment) {
        nodes.push($createTextNode(plainSegment));
      }
    }

    if (match[2] !== undefined) {
      const boldNode = $createTextNode(match[2]);
      boldNode.toggleFormat('bold');
      nodes.push(boldNode);
    } else if (match[4] !== undefined) {
      const italicNode = $createTextNode(match[4]);
      italicNode.toggleFormat('italic');
      nodes.push(italicNode);
    } else if (match[6] !== undefined) {
      const codeNode = $createTextNode(match[6]);
      codeNode.toggleFormat('code');
      nodes.push(codeNode);
    } else if (match[8] !== undefined) {
      const strikeNode = $createTextNode(match[8]);
      strikeNode.toggleFormat('strikethrough');
      nodes.push(strikeNode);
    } else if (match[9] !== undefined && match[10] !== undefined) {
      const linkLabel = match[9];
      const linkTargetUrl = match[10];
      const linkNode = $createLinkNode(linkTargetUrl);
      linkNode.append($createTextNode(linkLabel || linkTargetUrl));
      nodes.push(linkNode);
    } else if (match[11] !== undefined) {
      // Raw direct URL auto-link
      const rawUrl = match[11];
      const linkNode = $createLinkNode(rawUrl);
      linkNode.append($createTextNode(rawUrl));
      nodes.push(linkNode);
    }

    lastIndex = tokenRegex.lastIndex;
  }

  if (lastIndex < text.length) {
    const trailingSegment = text.slice(lastIndex);
    if (trailingSegment) {
      nodes.push($createTextNode(trailingSegment));
    }
  }

  return nodes.length > 0 ? nodes : [$createTextNode(text)];
}

export function MarkdownClipboardPlugin() {
  const [editor] = useLexicalComposerContext();

  useEffect(() => {
    // Intercept Paste to convert Markdown text and direct URLs into active LinkNodes cleanly
    const unregisterPaste = editor.registerCommand(
      PASTE_COMMAND,
      (event) => {
        if (!event || !('clipboardData' in event) || !event.clipboardData) {
          return false;
        }

        // Defer to ImagePastePlugin when the clipboard carries image
        // files. This guard is essential for mixed pastes (styled text +
        // image, e.g. a copy from a webpage or Word document): without
        // it, this handler would consume the entire paste as markdown
        // text and the image would be silently lost before the dedicated
        // image handler gets a chance to run.
        const clipboardItems = event.clipboardData.items;
        if (clipboardItems) {
          for (const item of Array.from(clipboardItems)) {
            if (item.kind === 'file' && item.type.startsWith('image/')) {
              return false;
            }
          }
        }

        const text = event.clipboardData.getData('text/plain');
        if (!text) return false;

        const isDirectUrl = isStandaloneUrl(text);
        const hasMarkdown =
          isDirectUrl ||
          /(?:^|\n)\s*(?:#{1,6}\s|>)/.test(text) ||
          /\*\*[\s\S]+?\*\*/.test(text) ||
          /__[\s\S]+?__/.test(text) ||
          /`[^`\n]+?`/.test(text) ||
          /~~[\s\S]+?~~/.test(text) ||
          /\[[^\]]*?\]\([^)]*?\)/.test(text) ||
          /https?:\/\/[^\s]+/i.test(text);

        if (!hasMarkdown) {
          return false;
        }

        const hasValidSelection = editor.getEditorState().read(() => {
          const selection = $getSelection();
          return $isRangeSelection(selection);
        });

        if (!hasValidSelection) {
          return false;
        }

        event.preventDefault();
        editor.update(() => {
          const root = $getRoot();
          const isEditorEmpty = root.getTextContent().trim() === '';

          // 1. Direct URL standalone paste: convert immediately to LinkNode with trailing space
          if (isDirectUrl) {
            const trimmedUrl = text.trim();
            const linkNode = $createLinkNode(trimmedUrl);
            linkNode.append($createTextNode(trimmedUrl));
            const trailingSpace = $createTextNode(' ');

            if (isEditorEmpty) {
              root.clear();
              const paragraph = $createParagraphNode();
              paragraph.append(linkNode);
              paragraph.append(trailingSpace);
              root.append(paragraph);
              trailingSpace.select(1, 1);
            } else {
              const selection = $getSelection();
              if ($isRangeSelection(selection)) {
                if (!selection.isCollapsed()) {
                  selection.removeText();
                }
                selection.insertNodes([linkNode, trailingSpace]);
                trailingSpace.select(1, 1);
              }
            }
            return;
          }

          // 2. Full markdown string in empty editor
          if (isEditorEmpty) {
            root.clear();
            $convertFromMarkdownString(text, COMPOSER_TRANSFORMERS);
            root.selectEnd();
            return;
          }

          // 3. Existing draft insertion preserving document hierarchy
          const selection = $getSelection();
          if (!$isRangeSelection(selection)) {
            return;
          }

          if (!selection.isCollapsed()) {
            selection.removeText();
          }

          const hasBlockMarkdown = /^(?:#{1,6}\s|>)/m.test(text);

          if (!hasBlockMarkdown && !text.includes('\n')) {
            const inlineNodes = parseInlineMarkdownToNodes(text);
            selection.insertNodes(inlineNodes);
            return;
          }

          const anchorNode = selection.anchor.getNode();
          const topElement =
            anchorNode.getKey() === 'root'
              ? anchorNode
              : anchorNode.getTopLevelElementOrThrow();

          let trailingParagraph: LexicalNode | null = null;

          if ($isElementNode(topElement)) {
            const afterNodes: LexicalNode[] = [];
            if ($isTextNode(anchorNode)) {
              const offset = selection.anchor.offset;
              const textLength = anchorNode.getTextContentSize();
              if (offset < textLength) {
                const splitNodes = anchorNode.splitText(offset);
                if (splitNodes.length > 1) {
                  afterNodes.push(splitNodes[1]);
                }
              }
              let nextSibling: LexicalNode | null = anchorNode.getNextSibling();
              while (nextSibling) {
                const next: LexicalNode | null = nextSibling.getNextSibling();
                afterNodes.push(nextSibling);
                nextSibling = next;
              }
            } else {
              const children = anchorNode.getChildren();
              for (let i = selection.anchor.offset; i < children.length; i++) {
                afterNodes.push(children[i]);
              }
            }

            if (afterNodes.length > 0) {
              const p = $createParagraphNode();
              afterNodes.forEach((n) => p.append(n));
              trailingParagraph = p;
            }
          }

          const lines = text.split(/\r?\n/);
          let insertAfterTarget: LexicalNode = topElement;

          lines.forEach((line) => {
            const trimmedLine = line.trim();
            if (!trimmedLine) return;

            let blockNode: LexicalNode;
            const headingMatch = trimmedLine.match(/^(#{1,6})\s+(.*)$/);
            const quoteMatch = trimmedLine.match(/^>\s*(.*)$/);

            if (headingMatch) {
              const level = headingMatch[1].length;
              const tag = `h${level}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6';
              const heading = $createHeadingNode(tag);
              const inlineNodes = parseInlineMarkdownToNodes(headingMatch[2]);
              inlineNodes.forEach((n) => heading.append(n));
              blockNode = heading;
            } else if (quoteMatch) {
              const quote = $createQuoteNode();
              const inlineNodes = parseInlineMarkdownToNodes(quoteMatch[1]);
              inlineNodes.forEach((n) => quote.append(n));
              blockNode = quote;
            } else {
              const paragraph = $createParagraphNode();
              const inlineNodes = parseInlineMarkdownToNodes(trimmedLine);
              inlineNodes.forEach((n) => paragraph.append(n));
              blockNode = paragraph;
            }

            insertAfterTarget.insertAfter(blockNode);
            insertAfterTarget = blockNode;
          });

          if (trailingParagraph) {
            insertAfterTarget.insertAfter(trailingParagraph);
          }

          if (topElement.getTextContent().trim() === '' && topElement !== insertAfterTarget) {
            topElement.remove();
          }

          if ($isElementNode(insertAfterTarget)) {
            insertAfterTarget.selectEnd();
          }
        });

        return true;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    // Intercept Copy to extract only the active selection in markdown format
    const unregisterCopy = editor.registerCommand<ClipboardEvent | KeyboardEvent | null>(
      COPY_COMMAND,
      (event) => {
        if (!event || !('clipboardData' in event) || !event.clipboardData) {
          return false;
        }

        const selection = $getSelection();
        if (!$isRangeSelection(selection) || selection.isCollapsed()) {
          return false;
        }

        const root = $getRoot();
        const selectedText = selection.getTextContent();
        const rootText = root.getTextContent();

        let serializedMarkdown = '';

        if (selectedText.trim() === rootText.trim() && rootText.length > 0) {
          serializedMarkdown = extractCleanText(editor);
        } else {
          try {
            const extractedNodes = selection.extract();
            serializedMarkdown = serializeNodesToMarkdown(extractedNodes);
          } catch {
            serializedMarkdown = selectedText;
          }
        }

        if (serializedMarkdown) {
          event.clipboardData.setData('text/plain', serializedMarkdown);
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
